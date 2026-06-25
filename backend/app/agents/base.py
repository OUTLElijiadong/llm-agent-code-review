import json as json_lib
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Dict, Optional

import httpx
from loguru import logger

from app.core.config import settings

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from app.utils.api_resolver import ApiConfig


@dataclass
class AgentContext:
    user_id: Optional[int] = None
    task_id: Optional[int] = None
    project_id: Optional[int] = None
    file_id: Optional[int] = None
    extra: Dict[str, Any] = field(default_factory=dict)


@dataclass
class AgentResult:
    success: bool
    data: Any = None
    error: Optional[str] = None
    model: str = ""
    duration_ms: int = 0
    tokens: Dict[str, int] = field(default_factory=dict)


class BaseAgent:
    """智能体基类

    封装 DeepSeek API 调用、重试、日志。
    每个继承的子类是一个独立智能体,负责单一职责。

    v2.0 在 v1.0 基础上新增统一元数据字段（icon/color/category/skills），
    供前端 Agent 办公室与态势感知面板渲染使用。
    """

    name: str = "base"
    description: str = ""

    # v2.0 元数据字段（前端展示用）
    icon: str = "base"
    color: str = "#5B58E8"
    category: str = "general"
    skills: tuple = ()

    def __init__(self, system_prompt: str = "", temperature: float = 0.3,
                 max_tokens: int = 4096, model: Optional[str] = None):
        self._system_prompt = system_prompt
        self._temperature = temperature
        self._max_tokens = max_tokens
        self._model = model or settings.deepseek_model
        self._base_url = settings.deepseek_base_url
        self._api_key = settings.deepseek_api_key
        self._max_retries = settings.deepseek_max_retries
        self._timeout = settings.deepseek_timeout
        # AgentSkill 自进化与总调度升级:Skill 实例列表,由 _init_skills() 填充
        self._skills: list = []
        self._init_skills()

    def _trace_id(self, ctx: Optional[AgentContext]) -> str:
        """从 ctx 中读取调用链 trace_id,没有则生成一个孤儿 trace"""
        from app.agents.events import new_trace_id
        if ctx and ctx.extra and ctx.extra.get("trace_id"):
            return ctx.extra["trace_id"]
        return new_trace_id()

    def _emit(self, type_, ctx: Optional[AgentContext], message: str = "",
              payload: Optional[dict] = None, parent: str = "") -> None:
        """统一 emit 入口,避免每个 Agent 都 import"""
        try:
            from app.agents.event_bus import emit_event
            from app.agents.events import AgentEventType
            if not isinstance(type_, AgentEventType):
                type_ = AgentEventType(type_)
            emit_event(
                type_=type_,
                agent=self.name,
                trace_id=self._trace_id(ctx),
                parent=parent,
                message=message,
                payload=payload or {},
            )
        except Exception as e:
            logger.warning(f"[{self.name}] emit 事件失败: {e}")

    def call(self, user_message: str, ctx: Optional[AgentContext] = None,
             json_mode: bool = False,
             api_config: Optional["ApiConfig"] = None) -> AgentResult:
        """调用 API (支持用户自定义配置)

        Args:
            user_message: 用户消息
            ctx: 上下文
            json_mode: 是否要求 JSON 输出
            api_config: 可选，用户自定义 API 配置；为 None 时使用系统默认

        Returns:
            AgentResult
        """
        # 解析最终使用的 API 配置：传入 > 实例默认
        from app.utils.api_resolver import validate_ai_base_url

        base_url = validate_ai_base_url(api_config.base_url if api_config else self._base_url)
        api_key = api_config.api_key if api_config else self._api_key
        model = api_config.model if api_config else self._model

        from app.agents.events import AgentEventType
        self._emit(AgentEventType.THINKING, ctx,
                   message=f"{self.name} 正在调用模型",
                   payload={"model": model, "json_mode": json_mode})

        messages = [{"role": "system", "content": self._system_prompt}]
        messages.append({"role": "user", "content": user_message})

        payload = {
            "model": model,
            "messages": messages,
            "temperature": self._temperature,
            "max_tokens": self._max_tokens,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}

        last_error = None
        for attempt in range(self._max_retries + 1):
            t0 = time.time()
            try:
                with httpx.Client(timeout=self._timeout, trust_env=False) as client:
                    resp = client.post(
                        f"{base_url}/chat/completions",
                        headers={
                            "Authorization": f"Bearer {api_key}",
                            "Content-Type": "application/json",
                        },
                        json=payload,
                    )
                duration_ms = int((time.time() - t0) * 1000)

                if resp.status_code == 200:
                    body = resp.json()
                    content = body["choices"][0]["message"]["content"]
                    usage = body.get("usage", {})
                    logger.debug(
                        f"[{self.name}] 调用成功 duration={duration_ms}ms "
                        f"tokens={usage.get('total_tokens', '?')}"
                    )
                    self._emit(AgentEventType.COMPLETE, ctx,
                               message=f"{self.name} 调用完成",
                               payload={
                                   "duration_ms": duration_ms,
                                   "total_tokens": usage.get("total_tokens", 0),
                               })
                    return AgentResult(
                        success=True,
                        data=content,
                        model=body.get("model", self._model),
                        duration_ms=duration_ms,
                        tokens={
                            "prompt": usage.get("prompt_tokens", 0),
                            "completion": usage.get("completion_tokens", 0),
                            "total": usage.get("total_tokens", 0),
                        },
                    )

                if resp.status_code == 429:
                    last_error = "请求过于频繁"
                elif resp.status_code >= 500:
                    last_error = f"服务异常({resp.status_code})"
                else:
                    last_error = f"调用失败({resp.status_code}): {resp.text[:200]}"

            except Exception as e:
                last_error = str(e)
                duration_ms = int((time.time() - t0) * 1000)

            logger.warning(f"[{self.name}] 第 {attempt+1} 次尝试失败: {last_error}")
            if attempt < self._max_retries:
                time.sleep(2 ** (attempt + 1))

        self._emit(AgentEventType.FAILED, ctx,
                   message=f"{self.name} 全部 {self._max_retries + 1} 次重试均失败",
                   payload={"error": last_error})
        return AgentResult(
            success=False,
            error=f"[{self.name}] 全部 {self._max_retries + 1} 次重试均失败: {last_error}",
        )

    def call_json(self, user_message: str, ctx: Optional[AgentContext] = None,
                  api_config: Optional["ApiConfig"] = None) -> AgentResult:
        """调用并解析为 JSON"""
        result = self.call(user_message, ctx, json_mode=True, api_config=api_config)
        if not result.success:
            return result
        try:
            result.data = json_lib.loads(result.data)
        except json_lib.JSONDecodeError as e:
            return AgentResult(
                success=False,
                error=f"[{self.name}] JSON 解析失败: {e}",
                model=result.model,
                duration_ms=result.duration_ms,
            )
        return result

    def _log_call(
        self,
        db: "Session",
        *,
        task_id: Optional[int] = None,
        user_id: Optional[int] = None,
        file_id: Optional[int] = None,
        chunk_index: Optional[int] = None,
        result: Optional[AgentResult] = None,
        status: str = "success",
        error: Optional[str] = None,
        user_prompt: str = "",
        response_text: str = "",
    ) -> None:
        """将本次 Agent 调用写入 ai_call_log 表,agent_label 填充为 self.name

        BaseAgent.call() 本身不写日志,调用方(如 review_service)在 Agent 调用
        完成后调用此方法补写 AiCallLog,实现 Agent 调用归因(AC6)。
        agent_label 字段固定为 self.name(如 code_reviewer / security_sentinel),
        使 SkillRegistry / 运维面板能按 Agent 维度统计调用情况。

        Args:
            db: 数据库会话(用于写入 AiCallLog)
            task_id: 审查任务 ID(可空)
            user_id: 发起用户 ID(可空)
            file_id: 关联文件 ID(可空)
            chunk_index: 分片索引(可空)
            result: AgentResult 对象(异常时可为 None)
            status: 日志状态(success/failed)
            error: 错误信息(失败时)
            user_prompt: 送入 LLM 的 user prompt(BaseAgent.call 路径可留空)
            response_text: LLM 原始响应文本(失败时留空)

        Returns:
            None
        """
        from app.models.ai_call_log import AiCallLog

        tokens_dict: Dict[str, int] = {}
        if result is not None and getattr(result, "tokens", None):
            tokens_dict = result.tokens if isinstance(result.tokens, dict) else {}

        model_name = (getattr(result, "model", None) if result else "") or self._model
        duration_ms = (getattr(result, "duration_ms", None) if result else None) or 0

        rec = AiCallLog(
            task_id=task_id,
            user_id=user_id,
            file_id=file_id,
            chunk_index=chunk_index,
            agent_label=self.name,
            model_name=model_name,
            prompt=(user_prompt or "")[:200_000] or None,
            response=(response_text or "")[:200_000] or None,
            status=status,
            error_message=error,
            prompt_tokens=tokens_dict.get("prompt", 0) if tokens_dict else None,
            completion_tokens=tokens_dict.get("completion", 0) if tokens_dict else None,
            total_tokens=tokens_dict.get("total", 0) if tokens_dict else None,
            duration_ms=duration_ms,
            create_time=datetime.now(timezone.utc),
        )
        try:
            db.add(rec)
            db.flush()
        except Exception as e:
            logger.debug(f"[{self.name}] _log_call 写入 AiCallLog 失败: {e}")

    # ── AgentSkill 自进化与总调度升级:Skill 挂载接口 ──

    def attach_skill(self, skill: "BaseSkill") -> None:
        """挂载 Skill 并注册到 SkillRegistry

        将 Skill 实例加入 self._skills,并同步注册到全局 SkillRegistry,
        供 Orchestrator.invoke_skill / ChatPlanner 查询调用。

        Args:
            skill: Skill 实例(BaseSkill 子类)
        """
        self._skills.append(skill)
        from app.agents.skills.registry import SkillRegistry

        SkillRegistry.instance().register(self.name, skill)

    def _init_skills(self) -> None:
        """子类 override:初始化并挂载专属 Skill

        默认空实现。子类按需 override,在方法内构造 SelfImprovement + Proactive Skill
        并调用 self.attach_skill() 挂载。此方法在 __init__ 末尾自动调用。

        Note:
            - 子类 override 时不应调用 super()._init_skills()(基类为空操作)
            - 挂载顺序建议:先 SelfImprovement,后 Proactive
        """
        pass
