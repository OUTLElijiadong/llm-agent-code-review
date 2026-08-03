"""ChatPlanner — 双层调度的第二层 LLM 动态规划

在第一层意图分类之后,用 LLM function calling 规划具体调用链(≤5 步),
输出 list[ToolCall] 供 ChatAssistantAgent._execute_plan 顺序执行。

设计要点:
- LLM 调用复用 BaseAgent.call_json 机制(由注入的 agent 提供)
- 超时用 concurrent.futures.ThreadPoolExecutor + future.result(timeout=10) 保护
- _validate_plan 校验 tool_name 必须在 tools 白名单中,非法则抛 ValueError
- tools 白名单 = SkillRegistry.list_tools() + Orchestrator 固定方法名
"""
import json
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from loguru import logger

from app.agents.tool_contracts import (
    FixedToolArgumentError,
    get_fixed_tool_description,
    get_fixed_tool_names,
    get_fixed_tool_schema,
    is_fixed_tool,
    validate_fixed_tool_arguments,
)

if TYPE_CHECKING:
    from app.agents.base import Agent, AgentContext


@dataclass
class ToolCall:
    """LLM 规划的单步调用

    Attributes:
        tool_name: 工具名(Skill name 或 Orchestrator 固定方法名)
        arguments: 工具参数 dict(作为 kwargs 传入 invoke_tool)
        reason: LLM 给出的调用理由(便于前端 step tree 展示)
    """

    tool_name: str
    arguments: Dict[str, Any] = field(default_factory=dict)
    reason: str = ""


class ChatPlanner:
    """LLM 动态规划调用链

    在第一层意图分类后,用 LLM function calling 规划具体调用链。
    通过 concurrent.futures 实现超时保护,避免 LLM 卡死阻塞用户请求。

    Attributes:
        MAX_STEPS: 单次规划最大步数(防 LLM 失控产出过长调用链)
        TIMEOUT_SECONDS: LLM 规划超时阈值(超时抛 TimeoutError 触发降级)
    """

    MAX_STEPS = 5
    TIMEOUT_SECONDS = 10

    # 固定工具名称、Schema 与执行校验均来自 tool_contracts 单一注册表。
    _FIXED_TOOLS: List[str] = get_fixed_tool_names()

    def __init__(self, agent: "Agent"):
        """初始化规划器

        Args:
            agent: ChatAssistantAgent 实例(用于调 LLM 与读取上下文)
        """
        self._agent = agent

    def plan(
        self,
        intent: Dict[str, Any],
        user_message: str = "",
        ctx: Optional["AgentContext"] = None,
    ) -> List[ToolCall]:
        """规划调用链

        Args:
            intent: 第一层输出的 {intent, reason, payload}
            user_message: 用户原始消息(供 LLM 理解上下文)
            ctx: 上下文

        Returns:
            list[ToolCall]: 调用链(≤ MAX_STEPS 步)

        Raises:
            TimeoutError: LLM 规划超时(>TIMEOUT_SECONDS)
            ValueError: LLM 输出非法 tool_name 或 JSON 解析失败
        """
        tools = self._collect_tools()
        prompt = self._build_plan_prompt(intent, tools, user_message)

        # 用线程池 + future.result(timeout) 实现硬超时
        # 注:BaseAgent.call_json 内部 httpx 已有 timeout,但 LLM 可能因网络抖动卡死,
        # 这里加第二层保护,超时即抛 TimeoutError 触发双层调度降级。
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(self._call_llm_for_plan, prompt, ctx)
            try:
                raw = future.result(timeout=self.TIMEOUT_SECONDS)
            except FutureTimeoutError as e:
                logger.warning(
                    f"[ChatPlanner] LLM 规划超时(>{self.TIMEOUT_SECONDS}s),"
                    f"intent={intent.get('intent')}"
                )
                raise TimeoutError(
                    f"ChatPlanner LLM 规划超时(>{self.TIMEOUT_SECONDS}s)"
                ) from e

        if not raw:
            raise ValueError("ChatPlanner LLM 返回空结果")
        plan = self._parse_plan(raw)
        self._validate_plan(plan, tools)
        return plan[: self.MAX_STEPS]

    # ── 内部方法 ──

    def _collect_tools(self) -> List[Dict[str, Any]]:
        """收集所有可用工具的白名单(供 LLM 规划与校验)

        Returns:
            list[dict]: 工具元数据列表
                [{"name", "description", "parameters", "type": "skill|fixed"}]
        """
        tools: List[Dict[str, Any]] = []
        orchestrator = getattr(self._agent, "_orchestrator", None)
        can_configure = bool(
            orchestrator is not None
            and getattr(orchestrator, "_can_configure_agents", lambda: False)()
        )

        # 1. SkillRegistry 中所有 invocable Skill(OpenAI tools 格式)
        try:
            from app.agents.skills.registry import SkillRegistry

            for s in SkillRegistry.instance().list_all():
                if not s.invocable:
                    continue
                if not can_configure:
                    continue
                tools.append({
                    "name": s.name,
                    "description": s.description or s.name,
                    "parameters": s._params_schema(),
                    "type": "skill",
                })
        except Exception as e:
            logger.warning(f"[ChatPlanner] 收集 Skill 工具失败: {e}")

        # 2. Orchestrator 固定方法：直接复用执行器的严格参数契约。
        for name in self._FIXED_TOOLS:
            if name == "trigger_evolution" and not can_configure:
                continue
            tools.append({
                "name": name,
                "description": get_fixed_tool_description(name),
                "parameters": get_fixed_tool_schema(name),
                "type": "fixed",
            })

        return tools

    def _build_plan_prompt(
        self,
        intent: Dict[str, Any],
        tools: List[Dict[str, Any]],
        user_message: str,
    ) -> str:
        """构建规划 prompt

        Args:
            intent: 第一层意图 {intent, reason, payload}
            tools: 工具白名单(_collect_tools 输出)
            user_message: 用户原始消息

        Returns:
            str: 发给 LLM 的 user prompt
        """
        tools_brief = []
        for t in tools:
            params = t.get("parameters", {}) or {}
            props = list((params.get("properties") or {}).keys())
            tools_brief.append(
                f"- {t['name']}({', '.join(props)}): {t['description']}"
            )
        tools_text = "\n".join(tools_brief)

        intent_payload = intent.get("payload", {}) or {}
        return (
            "你是 PRISM 平台的调用链规划器。基于用户意图,从可用工具中规划调用链,"
            f"最多 {self.MAX_STEPS} 步,输出 JSON 数组。\n\n"
            f"用户原始消息:\n{user_message[:500]}\n\n"
            f"已识别意图: {intent.get('intent', 'chat')}\n"
            f"意图理由: {intent.get('reason', '')}\n"
            f"意图载荷: {json.dumps(intent_payload, ensure_ascii=False)}\n\n"
            f"可用工具列表:\n{tools_text}\n\n"
            "输出格式: 严格 JSON 数组,每个元素:\n"
            '{"tool_name":"工具名","arguments":{参数kv},"reason":"调用理由"}\n\n'
            "规则:\n"
            "1. tool_name 必须来自上面的工具列表,不得编造\n"
            "2. arguments 只允许 JSON 字面量,不得使用 $... 等动态引用语法\n"
            "3. 如果意图只需单步即可完成,输出长度为 1 的数组\n"
            "4. start_review 只有项目 ID 时必须省略 file_ids,"
            "服务端会自动选择项目 active 文件,不要先调用 list_code_files\n"
            "5. 不要输出任何额外文本,只输出 JSON 数组"
        )

    def _call_llm_for_plan(
        self, prompt: str, ctx: Optional["AgentContext"]
    ) -> Optional[dict]:
        """调用 LLM 生成规划(在线程池中执行)

        复用 BaseAgent.call_json 机制,但临时切换 system_prompt 与采样参数,
        避免污染 ChatAgent 的对话态 system_prompt。

        Args:
            prompt: 规划 prompt
            ctx: 上下文

        Returns:
            dict|None: LLM 返回的原始 JSON dict(可能包含 plan 字段或直接是数组)
        """
        # 临时切换 agent 的 system_prompt 与采样参数,调用后恢复
        original_prompt = self._agent._system_prompt
        original_temp = self._agent._temperature
        original_max_tokens = self._agent._max_tokens

        self._agent._system_prompt = (
            "你是 PRISM 平台的调用链规划器,只输出 JSON 数组,不要额外文本。"
        )
        self._agent._temperature = 0.1
        self._agent._max_tokens = 1024

        try:
            result = self._agent.call_json(prompt, ctx=ctx)
            if result.success and isinstance(result.data, (dict, list)):
                return result.data
            logger.warning(
                f"[ChatPlanner] LLM 调用失败或返回非 JSON: "
                f"{result.error or type(result.data)}"
            )
            return None
        finally:
            self._agent._system_prompt = original_prompt
            self._agent._temperature = original_temp
            self._agent._max_tokens = original_max_tokens

    def _parse_plan(self, raw: Any) -> List[ToolCall]:
        """解析 LLM 返回的 JSON 为 list[ToolCall]

        LLM 可能返回:
        - 直接是 list[dict]
        - {"plan": list[dict]}
        - {"steps": list[dict]}

        Args:
            raw: LLM 返回的 JSON(dict 或 list)

        Returns:
            list[ToolCall]: 解析后的调用链

        Raises:
            ValueError: 解析失败或格式不合法
        """
        items: List[Dict[str, Any]] = []
        if isinstance(raw, list):
            items = raw
        elif isinstance(raw, dict):
            # 兼容多种字段名
            for key in ("plan", "steps", "actions", "tool_calls"):
                if key in raw and isinstance(raw[key], list):
                    items = raw[key]
                    break
            if not items and "tool_name" in raw:
                # 单步直接返回
                items = [raw]
        else:
            raise ValueError(f"ChatPlanner LLM 返回类型不合法: {type(raw)}")

        if not items:
            raise ValueError("ChatPlanner LLM 返回空调用链")

        plan: List[ToolCall] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            tool_name = item.get("tool_name") or item.get("name") or ""
            if not tool_name:
                continue
            plan.append(ToolCall(
                tool_name=tool_name,
                arguments=item.get("arguments") or item.get("args") or {},
                reason=item.get("reason", "") or "",
            ))

        if not plan:
            raise ValueError("ChatPlanner 解析后调用链为空")
        return plan

    def _validate_plan(
        self, plan: List[ToolCall], tools: List[Dict[str, Any]]
    ) -> None:
        """校验工具白名单，并用同一固定工具契约规范化参数。

        Args:
            plan: 待校验的调用链
            tools: 工具白名单(_collect_tools 输出)

        Raises:
            ValueError: 存在非法 tool_name 或固定工具参数
        """
        valid_names = {t["name"] for t in tools}
        for i, step in enumerate(plan):
            if step.tool_name not in valid_names:
                raise ValueError(
                    f"ChatPlanner 步骤 {i + 1} 非法 tool_name: "
                    f"{step.tool_name}(不在 {len(valid_names)} 个可用工具中)"
                )
            if is_fixed_tool(step.tool_name):
                try:
                    normalized_arguments = self._normalize_dynamic_arguments(
                        step.tool_name,
                        step.arguments,
                    )
                    step.arguments = validate_fixed_tool_arguments(
                        step.tool_name,
                        normalized_arguments,
                    )
                except FixedToolArgumentError as exc:
                    raise ValueError(
                        f"ChatPlanner 步骤 {i + 1} 参数不合法: {exc}"
                    ) from exc
        if len(plan) > self.MAX_STEPS:
            logger.warning(
                f"[ChatPlanner] 调用链 {len(plan)} 步超过 MAX_STEPS={self.MAX_STEPS},"
                f"将截断"
            )

    @staticmethod
    def _normalize_dynamic_arguments(
        tool_name: str,
        arguments: Dict[str, Any],
    ) -> Dict[str, Any]:
        """规范化 Planner 产生的可选动态参数。

        `start_review` 已由 Orchestrator 统一解析项目 active 文件。
        Planner 在无显式文件 ID 时偶尔仍会生成 `$...` 上游输出引用，
        而当前执行器不支持该引用语法。此处只删除 `None` 或以 `$`
        开头的模型动态引用；其他非数组字符串仍交由严格契约拒绝。

        Args:
            tool_name: 当前固定工具名称。
            arguments: Planner 生成的原始参数字典。

        Returns:
            Dict[str, Any]: 供固定工具契约继续严格校验的副本。
        """
        normalized = dict(arguments)
        file_ids = normalized.get("file_ids")
        is_dynamic_reference = (
            isinstance(file_ids, str) and file_ids.strip().startswith("$")
        )
        if tool_name == "start_review" and (
            file_ids is None or is_dynamic_reference
        ):
            normalized.pop("file_ids", None)
        return normalized
