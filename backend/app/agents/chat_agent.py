import json as json_lib
from typing import TYPE_CHECKING, Any, List, Optional

from loguru import logger

from app.agents.base import AgentContext, AgentResult, BaseAgent

if TYPE_CHECKING:
    from app.agents.orchestrator import Orchestrator


_INTENT_SYSTEM = (
    "你是一个意图分类器。分析用户消息,判断用户意图并输出JSON。\n\n"
    "支持的意图类型:\n"
    "- chat: 普通对话/问答/咨询\n"
    "- detect_language: 用户想识别项目或代码的编程语言\n"
    "- analyze_project: 用户想根据文件列表分析项目信息\n"
    "- review_code: 用户发送了代码片段要求审查\n"
    "- list_agents: 用户想查看有哪些可用的Agent\n"
    "- list_projects: 用户想看项目列表/有哪些项目\n"
    "- create_project: 用户要创建新项目(提供项目名/语言/描述)\n"
    "- delete_project: 用户要删除某个项目\n"
    "- start_review: 用户要对项目启动代码审查\n"
    "- list_review_tasks: 用户想看审查任务记录\n"
    "- list_review_issues: 用户想看审查发现的问题\n"
    "- list_code_files: 用户想看某个项目的代码文件\n"
    "- dashboard: 用户想看平台统计数据/仪表盘\n"
    "- list_rules: 用户想看审查规则列表\n"
    "- list_reports: 用户想看审查报告列表\n"
    "- generate_ai_prompt: 用户想生成可粘贴给 Cursor/Copilot/ChatGPT/Claude Code 的 AI 修复提示词\n"
    "- security_audit: 用户想做网络安全审计/漏洞扫描/威胁建模/敏感信息扫描/OWASP 检查\n"
    "- evolution_trigger: 用户想触发某个 Agent 的自进化(从反馈蒸馏规则/进化策略)\n"
    "- agent_skill_invoke: 用户想手动调用某个 Agent 的某个 Skill(自进化/主动监测)\n"
    "- agent_status: 用户想查看 Agent 运行状态/Skill 列表/调用记录\n\n"
    "输出格式: 严格JSON对象:\n"
    '{"intent": "chat", "reason": "简短理由", "payload": {}}\n\n'
    "常用payload:\n"
    "- detect_language: {\"project_name\":\"...\",\"description\":\"...\"}\n"
    "- analyze_project: {\"folder_name\":\"...\",\"file_names\":[...]}\n"
    "- review_code: {\"code\":\"...\",\"language\":\"...\"}\n"
    "- create_project: {\"project_name\":\"...\",\"description\":\"...\",\"language\":\"...\"}\n"
    "- delete_project: {\"project_id\": 数字}\n"
    "- start_review: {\"project_id\": 数字, \"review_type\": \"quick\"}\n"
    "- list_review_tasks: {\"project_id\": 数字或null}\n"
    "- list_review_issues: {\"task_id\": 数字}\n"
    "- list_code_files: {\"project_id\": 数字}\n"
    "- dashboard: {}\n"
    "- list_rules: {}\n"
    "- list_reports: {}\n"
    "- list_projects: {}\n"
    "- list_agents: {}\n"
    "- generate_ai_prompt: {\"scope\":\"issue|task|project\", \"issue_id\":数字, \"task_id\":数字, "
    "\"project_id\":数字, \"target_tool\":\"generic|cursor|copilot|chatgpt|claude_code\"}\n"
    "- security_audit: {\"scope\":\"file|task|project\", \"file_id\":数字, \"task_id\":数字, "
    "\"project_id\":数字, \"scan_depth\":\"quick|standard|deep\", \"top_n\":数字, \"trace_dataflow\":布尔}\n"
    "- evolution_trigger: {\"agent_name\":\"code_reviewer|security_sentinel|evolution|...\", \"window_days\":数字}\n"
    "- agent_skill_invoke: {\"agent_name\":\"...\", \"skill_name\":\"<agent>.self_improve|<agent>.proactive\", "
    "\"action\":\"evolve|check_proactive|scan_domain|reflect_from_logs\", \"params\":{...}}\n"
    "- agent_status: {\"agent_name\":\"...\", \"detail\":\"skills|records|all\"}\n"
    "- chat: {}\n\n"
    "关键判断规则:\n"
    "如果用户消息中包含代码块(```...```), intent=review_code\n"
    "如果用户说'项目列表''有哪些项目''我的项目', intent=list_projects\n"
    "如果用户说'创建项目''新建项目', intent=create_project\n"
    "如果用户说'删除项目''删掉项目', intent=delete_project\n"
    "如果用户说'审查项目''开始审查''代码审查', intent=start_review\n"
    "如果用户说'审查记录''审查任务''审查列表', intent=list_review_tasks\n"
    "如果用户说'审查问题''存在的问题', intent=list_review_issues\n"
    "如果用户说'代码文件''文件列表''有什么文件', intent=list_code_files\n"
    "如果用户说'仪表盘''统计数据''情况怎么样''概览', intent=dashboard\n"
    "如果用户说'规则''审查规则''有哪些规则', intent=list_rules\n"
    "如果用户说'报告''审查报告''报告列表', intent=list_reports\n"
    "如果用户问'语言是什么''什么语言', intent=detect_language\n"
    "如果用户问'有哪些Agent''有什么功能''能做什么', intent=list_agents\n"
    "如果用户提到'分析项目''分析文件夹', intent=analyze_project\n"
    "如果用户提到'AI提示词''Cursor修复''Copilot修复''让ChatGPT修''让Claude修''AI修复包',"
    "intent=generate_ai_prompt; 根据上下文推断 scope: 单条问题→issue; 一个任务→task; 一个项目→project\n"
    "如果用户提到'安全扫描''安全审计''漏洞扫描''威胁建模''密钥泄漏''OWASP''渗透测试''敏感信息''CWE',"
    "intent=security_audit; 根据上下文推断 scope: 单个文件→file; 一个任务→task; 一个项目→project\n"
    "如果用户说'触发进化''自进化''让XX Agent进化''跑一轮进化''蒸馏规则', intent=evolution_trigger; "
    "payload.agent_name 从消息推断(默认 evolution)\n"
    "如果用户说'调用XX的Skill''手动触发Skill''跑一下XX.self_improve', intent=agent_skill_invoke; "
    "payload 需含 agent_name 与 skill_name\n"
    "如果用户说'Agent状态''有哪些Skill''Skill调用记录''Agent运行情况', intent=agent_status\n"
    "其他情况默认为chat"
)


class ChatAssistantAgent(BaseAgent):
    """聊天助手 Agent — 用户与 Agent 体系的统一入口

    通过对话识别用户意图, 自动委派 Orchestrator 调度专业子 Agent 执行任务:
    - 语言检测 → LanguageDetectorAgent
    - 项目分析 → ProjectAnalyzerAgent
    - 代码审查 → CodeReviewerAgent
    - Agent列表 → Orchestrator.list_agents()
    """

    name = "chat_assistant"
    description = "PRISM 平台智能聊天助手, 可通过对话调控所有 Agent"
    icon = "chat_assistant"
    color = "#3DBCD9"
    category = "frontline"
    skills = ("自然语言入口", "意图分类", "多 Agent 调度", "结果整合")

    def __init__(self):
        super().__init__(temperature=0.7, max_tokens=4096)
        self._orchestrator: Optional["Orchestrator"] = None
        # 双层调度第二层:LLM 动态规划调用链
        # 延迟 import 避免循环依赖(ChatPlanner 内部 TYPE_CHECKING 引用 BaseAgent)
        from app.agents.chat_planner import ChatPlanner
        self._planner = ChatPlanner(self)
        # 最近一次规划的 plan_steps(供前端 step tree 展示)
        self._last_plan_steps: List[dict] = []

    def _init_skills(self) -> None:
        """子类 override:挂载 ChatAssistantSelfImprovementSkill + ChatAssistantProactiveSkill

        将聊天助手 Agent 的自进化与主动监测能力下沉到 Skill,通过 SkillRegistry
        统一注册,供 Orchestrator.invoke_skill / ChatPlanner 查询调用。
        """
        from app.agents.skills.chat_assistant import (
            ChatAssistantProactiveSkill,
            ChatAssistantSelfImprovementSkill,
        )

        self.attach_skill(ChatAssistantSelfImprovementSkill(self.name))
        self.attach_skill(ChatAssistantProactiveSkill(self.name))

    def set_orchestrator(self, orch: "Orchestrator") -> None:
        self._orchestrator = orch

    @property
    def system_prompt(self) -> str:
        agents_desc = ""
        if self._orchestrator:
            agents_desc = "\n当前可调度的Agent:\n"
            for name, desc in self._orchestrator.list_agents().items():
                agents_desc += f"- {name}: {desc}\n"

        return (
            "你是PRISM智能代码审查平台的Agent助手,"
            "你是连接用户和所有专业Agent的统一入口。\n\n"
            "你可以:\n"
            "1. 与用户对话,解答代码审查、代码质量、最佳实践等问题\n"
            "2. **调度专业Agent执行任务**: 你的背后有多个专业Agent,"
            "系统会自动判断用户意图并路由到合适的Agent处理\n"
            "3. 当用户发送代码片段时,自动调度「代码审查Agent」分析\n"
            "4. 当用户询问项目语言时,自动调度「语言检测Agent」识别\n"
            "5. 当用户提供文件列表时,自动调度「项目分析Agent」生成项目元数据\n\n"
            f"{agents_desc}"
            "请用专业、简洁、友好的方式回答问题。使用Markdown格式组织回复。"
            "当任务被委派给其他Agent执行后,用自然语言向用户解释结果。"
        )

    def execute(self, messages: List[dict],
                ctx: Optional[AgentContext] = None) -> AgentResult:
        """处理用户消息,双层调度:意图分类 → LLM 规划 → 顺序执行

        双层调度流程:
            1. 第一层: LLM 意图分类(已有逻辑)
            2. 第二层: LLM 动态规划调用链(ChatPlanner.plan)
            3. 执行器: _execute_plan 顺序执行 ToolCall 链

        降级路径:
            - CHAT_DOUBLE_LAYER_ENABLED=false → 直接走单层 handler
            - planner 抛 TimeoutError/ValueError → 降级到单层 handler

        Args:
            messages: 消息列表(最后一条为用户当前消息)
            ctx: 上下文

        Returns:
            AgentResult: data 字段为最终回复内容(字符串)或结构化 dict
        """
        from app.agents.events import AgentEventType
        if not messages:
            return AgentResult(success=False, error="消息列表为空")

        last_msg = messages[-1]["content"]
        intent = self._classify_intent(last_msg, messages)

        handler_name = intent.get("intent", "chat")
        self._emit(AgentEventType.DISPATCH, ctx,
                   message=f"意图识别为 {handler_name},准备执行",
                   payload={"intent": handler_name, "reason": intent.get("reason", "")},
                   parent="orchestrator")

        # v2.0: 关键 intent 缺字段时主动追问,不再猜测
        clarify = self._maybe_clarify(handler_name, intent.get("payload", {}), ctx)
        if clarify is not None:
            return clarify

        # AgentSkill 升级:双层调度总开关
        # 普通对话(chat intent)不走双层,避免无谓的 LLM 规划开销
        if self._double_layer_enabled() and handler_name != "chat":
            try:
                plan = self._planner.plan(intent, user_message=last_msg, ctx=ctx)
                self._emit(AgentEventType.DISPATCH, ctx,
                           message=f"双层调度规划完成,调用链 {len(plan)} 步",
                           payload={
                               "plan_steps": [
                                   {"tool_name": s.tool_name,
                                    "reason": s.reason,
                                    "arguments": s.arguments}
                                   for s in plan
                               ],
                           },
                           parent="orchestrator")
                return self._execute_plan(plan, messages, ctx)
            except (TimeoutError, ValueError) as e:
                logger.warning(
                    f"[ChatAgent] 双层调度降级到单层 handler: "
                    f"{type(e).__name__}: {e}"
                )
                self._last_plan_steps = []
            except Exception as e:
                logger.exception(
                    f"[ChatAgent] 双层调度未知异常,降级到单层 handler: {e}"
                )
                self._last_plan_steps = []
        else:
            self._last_plan_steps = []

        # 单层 fallback:走原 handler 路由
        return self._dispatch_single(intent, messages, ctx)

    def _dispatch_single(
        self,
        intent: dict,
        messages: List[dict],
        ctx: Optional[AgentContext],
    ) -> AgentResult:
        """单层调度 fallback:按 intent 路由到固定 handler

        双层调度关闭或规划失败时使用,保持与升级前完全兼容的路由逻辑。

        Args:
            intent: 意图 dict {intent, reason, payload}
            messages: 消息列表(普通对话 intent 用)
            ctx: 上下文

        Returns:
            AgentResult: handler 执行结果
        """
        handler_name = intent.get("intent", "chat")
        handlers = {
            "list_agents": lambda i, c: self._handle_list_agents(c),
            "detect_language": self._handle_detect_language,
            "analyze_project": self._handle_analyze_project,
            "review_code": self._handle_review_code,
            "list_projects": lambda i, c: self._handle_list_projects(c),
            "create_project": self._handle_create_project,
            "delete_project": self._handle_delete_project,
            "start_review": self._handle_start_review,
            "list_review_tasks": self._handle_list_review_tasks,
            "list_review_issues": self._handle_list_review_issues,
            "list_code_files": self._handle_list_code_files,
            "dashboard": lambda i, c: self._handle_dashboard(c),
            "list_rules": lambda i, c: self._handle_list_rules(c),
            "list_reports": lambda i, c: self._handle_list_reports(c),
            "generate_ai_prompt": self._handle_generate_ai_prompt,
            "security_audit": self._handle_security_audit,
            # AgentSkill 升级:3 种新 intent handler
            "evolution_trigger": self._handle_evolution_trigger,
            "agent_skill_invoke": self._handle_agent_skill_invoke,
            "agent_status": self._handle_agent_status,
        }

        handler = handlers.get(handler_name, None)
        if handler:
            return handler(intent, ctx)
        return self._handle_chat(messages, ctx)

    def _double_layer_enabled(self) -> bool:
        """双层调度总开关(读 settings.chat_double_layer_enabled)

        出问题时可在 .env 设 CHAT_DOUBLE_LAYER_ENABLED=false 快速降级,
        不影响主流程,只走单层 handler。

        Returns:
            bool: True=启用双层调度, False=回退单层
        """
        try:
            from app.core.config import settings
            return bool(getattr(settings, "chat_double_layer_enabled", True))
        except Exception:
            return True

    def _execute_plan(
        self,
        plan: List["ToolCall"],
        messages: List[dict],
        ctx: Optional[AgentContext],
    ) -> AgentResult:
        """顺序执行 ToolCall 链(双层调度执行器)

        执行规则:
            - 按 plan 顺序依次调用 Orchestrator.invoke_tool
            - 上一步输出作为下一步上下文(写入 ctx.extra["prev_output"])
            - 任一步失败则终止,返回已执行的步骤摘要 + 错误信息
            - 普通对话(chat intent)不走本方法,直接 _handle_chat

        Args:
            plan: ChatPlanner 规划的调用链
            messages: 原始消息列表(用于普通对话兜底)
            ctx: 上下文

        Returns:
            AgentResult: 最终结果(含 plan_steps 供前端展示)
        """
        if not self._check_orch():
            return AgentResult(success=False, error="Orchestrator 未注入")
        if not plan:
            return self._handle_chat(messages, ctx)

        from app.agents.events import AgentEventType

        executed_steps: List[dict] = []
        prev_output: Any = None
        last_result: Optional[AgentResult] = None

        for idx, step in enumerate(plan):
            step_label = f"步骤 {idx + 1}/{len(plan)}: {step.tool_name}"
            self._emit(AgentEventType.DISPATCH, ctx,
                       message=step_label,
                       payload={"tool_name": step.tool_name,
                                "reason": step.reason,
                                "step_index": idx},
                       parent="orchestrator")
            # 把上一步输出注入 ctx.extra,供后续 Tool 参考
            if ctx is None:
                ctx = AgentContext()
            ctx.extra["prev_output"] = prev_output
            ctx.extra["step_index"] = idx

            t0 = __import__("time").time()
            try:
                step_result = self._orchestrator.invoke_tool(
                    tool_name=step.tool_name,
                    arguments=step.arguments,
                    ctx=ctx,
                )
            except Exception as e:
                logger.exception(
                    f"[ChatAgent] 调用链 {step_label} 异常: {e}"
                )
                step_result = AgentResult(success=False, error=str(e))

            duration_ms = int((__import__("time").time() - t0) * 1000)
            executed_steps.append({
                "step_index": idx,
                "tool_name": step.tool_name,
                "reason": step.reason,
                "arguments": step.arguments,
                "success": step_result.success,
                "duration_ms": duration_ms,
                "error": step_result.error,
                "data_preview": (
                    str(step_result.data)[:200] if step_result.data else None
                ),
            })

            if not step_result.success:
                # 失败终止:返回已执行步骤 + 错误
                logger.warning(
                    f"[ChatAgent] 调用链在 {step_label} 失败: {step_result.error}"
                )
                self._last_plan_steps = executed_steps
                return AgentResult(
                    success=False,
                    error=f"调用链在 {step.tool_name} 步骤失败: {step_result.error}",
                    data={"plan_steps": executed_steps,
                          "failed_at_step": idx + 1},
                )
            prev_output = step_result.data
            last_result = step_result

        self._last_plan_steps = executed_steps

        # 把最后一步结果转换为用户可读回复
        # 如果最后一步是普通对话工具,直接返回 data 字符串
        if last_result and isinstance(last_result.data, str):
            return AgentResult(
                success=True,
                data=last_result.data,
                model=last_result.model,
                duration_ms=sum(s["duration_ms"] for s in executed_steps),
            )
        # 结构化结果:附带 plan_steps 供前端 step tree 展示
        return AgentResult(
            success=True,
            data={
                "content": self._format_plan_result(executed_steps, last_result),
                "plan_steps": executed_steps,
            },
            model=last_result.model if last_result else "",
            duration_ms=sum(s["duration_ms"] for s in executed_steps),
        )

    def _format_plan_result(
        self, steps: List[dict], last_result: Optional[AgentResult]
    ) -> str:
        """把调用链执行结果格式化为用户可读的 Markdown 回复

        Args:
            steps: 已执行步骤列表
            last_result: 最后一步的 AgentResult

        Returns:
            str: Markdown 格式回复
        """
        lines = [f"**调用链执行完成** (共 {len(steps)} 步)\n"]
        for s in steps:
            icon = "✅" if s["success"] else "❌"
            lines.append(
                f"{icon} **步骤 {s['step_index'] + 1}**: `{s['tool_name']}` "
                f"({s['duration_ms']}ms)"
            )
            if s.get("reason"):
                lines.append(f"   _理由: {s['reason']}_")
        lines.append("")
        if last_result and last_result.data:
            data = last_result.data
            if isinstance(data, dict) and "content" in data:
                lines.append(str(data["content"]))
            elif isinstance(data, str):
                lines.append(data)
            else:
                lines.append(f"```json\n{json_lib.dumps(data, ensure_ascii=False, indent=2)[:500]}\n```")
        return "\n".join(lines)

    # ============ v2.0 Clarify 协议 ============

    INTENT_REQUIRED_FIELDS = {
        "delete_project": ["project_id"],
        "start_review": ["project_id"],
        "list_review_issues": ["task_id"],
        "list_code_files": ["project_id"],
        "create_project": ["project_name"],
        "review_code": ["code"],
        # v2.0 A1: AI 提示词意图按 scope 动态决定必填字段
        # 由 _maybe_clarify 特殊处理(基础列表保留为空)
        "generate_ai_prompt": [],
        # v2.1: 安全审计意图同样按 scope 动态决定
        "security_audit": [],
    }

    QUESTION_TEMPLATES = {
        "project_id": {
            "label": "请告诉我具体是哪个项目?",
            "type": "select_project",
            "hint": "从你的项目列表中选一个",
        },
        "task_id": {
            "label": "请指定审查任务 ID?",
            "type": "select_task",
            "hint": "可以在「审查记录」页查到",
        },
        "issue_id": {
            "label": "请告诉我具体的问题 ID?",
            "type": "number",
            "hint": "可以在审查详情页问题卡片右上角找到",
        },
        "file_id": {
            "label": "请告诉我具体是哪个代码文件?",
            "type": "select_file",
            "hint": "需要先在前置问题里选择所属项目",
        },
        "code": {
            "label": "请粘贴要审查的代码片段",
            "type": "code",
            "hint": "支持任何语言,用三个反引号包裹更佳",
        },
        "project_name": {
            "label": "新项目叫什么名字?",
            "type": "text",
            "hint": "2-50 字,中英文均可",
        },
        "scope": {
            "label": "操作要覆盖哪种范围?",
            "type": "select",
            "hint": "issue/file=单条问题或文件 · task=整个审查任务 · project=整个项目",
            "options": [
                {"value": "issue", "label": "单条问题 (issue)"},
                {"value": "file", "label": "单个文件 (file)"},
                {"value": "task", "label": "审查任务 (task)"},
                {"value": "project", "label": "整个项目 (project)"},
            ],
        },
        "scan_depth": {
            "label": "安全扫描深度?",
            "type": "select",
            "hint": "quick=只跑正则 · standard=正则+LLM · deep=深度审查(更多 token)",
            "options": [
                {"value": "quick", "label": "快速"},
                {"value": "standard", "label": "标准"},
                {"value": "deep", "label": "深度"},
            ],
        },
        "target_tool": {
            "label": "要给哪个 AI 工具用?",
            "type": "select",
            "hint": "决定提示词格式与末尾的快捷键提示",
            "options": [
                {"value": "generic", "label": "通用 AI"},
                {"value": "cursor", "label": "Cursor"},
                {"value": "copilot", "label": "GitHub Copilot Chat"},
                {"value": "chatgpt", "label": "ChatGPT"},
                {"value": "claude_code", "label": "Claude Code"},
            ],
        },
    }

    def _maybe_clarify(self, intent_name: str, payload: dict,
                       ctx: Optional[AgentContext]) -> Optional[AgentResult]:
        """v2.0: 若关键字段缺失,主动追问而非猜测;返回非空表示需要追问"""
        from app.agents.events import AgentEventType

        # v2.0 A1 / v2.1: scope-动态意图按 scope 推导必填字段
        if intent_name == "generate_ai_prompt":
            required = self._ai_prompt_required(payload)
        elif intent_name == "security_audit":
            required = self._security_audit_required(payload)
        else:
            required = self.INTENT_REQUIRED_FIELDS.get(intent_name, [])
        missing = [k for k in required if not payload.get(k)]
        if not missing:
            return None
        questions = []
        for k in missing:
            tpl = self.QUESTION_TEMPLATES.get(k, {})
            questions.append({
                "key": k,
                "label": tpl.get("label", f"请补充 {k}"),
                "type": tpl.get("type", "text"),
                "hint": tpl.get("hint", ""),
                "required": True,
            })
        import uuid as _uuid
        clarify_id = f"clr_{_uuid.uuid4().hex[:12]}"
        from app.agents.clarify_store import ClarifyStore
        ClarifyStore.instance().put(clarify_id, {
            "intent": intent_name,
            "payload": payload,
            "user_id": ctx.user_id if ctx else None,
        })
        self._emit(AgentEventType.CLARIFY, ctx,
                   message=f"等待用户补充: {', '.join(missing)}",
                   payload={"clarify_id": clarify_id, "missing": missing})
        message = (
            "我想确认一下信息后再执行,这样不会做错决定。请回答下面的问题:"
        )
        return AgentResult(
            success=True,
            data={
                "content": message,
                "clarify": {
                    "clarify_id": clarify_id,
                    "intent": intent_name,
                    "questions": questions,
                },
            },
            model=self._model,
        )

    def _ai_prompt_required(self, payload: dict) -> List[str]:
        """根据 scope 推导 generate_ai_prompt 的必填字段"""
        scope = (payload.get("scope") or "").lower()
        if not scope:
            return ["scope"]
        base = ["target_tool"]
        if scope == "issue":
            return base + ["issue_id"]
        if scope == "task":
            return base + ["task_id"]
        if scope == "project":
            return base + ["project_id"]
        return ["scope"]

    def _security_audit_required(self, payload: dict) -> List[str]:
        """根据 scope 推导 security_audit 的必填字段"""
        scope = (payload.get("scope") or "").lower()
        if not scope:
            return ["scope"]
        if scope == "file":
            return ["file_id"]
        if scope == "task":
            return ["task_id"]
        if scope == "project":
            return ["project_id"]
        return ["scope"]

    def _handle_generate_ai_prompt(self, intent: dict,
                                    ctx: Optional[AgentContext]) -> AgentResult:
        """委派 Orchestrator → AiPromptAgent"""
        if not self._check_orch():
            return AgentResult(success=False, error="Orchestrator 未注入")
        p = intent.get("payload", {}) or {}
        scope = (p.get("scope") or "").lower()
        target_tool = p.get("target_tool", "generic")
        if scope == "issue":
            result = self._orchestrator.generate_ai_prompt_for_issue(
                issue_id=int(p["issue_id"]),
                target_tool=target_tool, use_llm=True, ctx=ctx,
            )
        elif scope == "task":
            result = self._orchestrator.generate_ai_prompt_for_task(
                task_id=int(p["task_id"]),
                target_tool=target_tool,
                severity_filter=p.get("severity"),
                use_llm=True, ctx=ctx,
            )
        elif scope == "project":
            result = self._orchestrator.generate_ai_prompt_for_project(
                project_id=int(p["project_id"]),
                target_tool=target_tool,
                top_n=int(p.get("top_n", 30)),
                use_llm=True, ctx=ctx,
            )
        else:
            return AgentResult(success=False, error=f"未知 scope: {scope}")
        if not result.success:
            return result
        data = result.data
        prompts = data.get("prompts", [])
        lines = [
            f"**AI 提示词已生成** ⚡ (共 {len(prompts)} 条 · 目标 "
            f"{prompts[0]['target_label'] if prompts else target_tool})\n",
            data.get("summary", ""),
            "",
        ]
        for i, p_ in enumerate(prompts[:3]):
            lines.append(f"### {i + 1}. {p_['title']}")
            lines.append(f"`{p_['file_path']}` · {p_['lines']}")
            lines.append("```")
            lines.append(p_["prompt_text"][:600] + ("\n..." if len(p_["prompt_text"]) > 600 else ""))
            lines.append("```")
        if len(prompts) > 3:
            lines.append(f"\n...还有 {len(prompts) - 3} 条提示词,请到「审查详情 → AI 修复包」查看完整列表。")
        lines.append("\n由 **`ai_prompt` Agent** 完成。")
        return AgentResult(
            success=True, data="\n".join(lines),
            model=result.model, duration_ms=result.duration_ms,
        )

    def _handle_security_audit(self, intent: dict,
                                ctx: Optional[AgentContext]) -> AgentResult:
        """委派 Orchestrator → SecuritySentinelAgent (v2.1)"""
        if not self._check_orch():
            return AgentResult(success=False, error="Orchestrator 未注入")
        p = intent.get("payload", {}) or {}
        scope = (p.get("scope") or "").lower()
        if scope == "file":
            result = self._orchestrator.audit_security_for_file(
                file_id=int(p["file_id"]),
                scan_depth=p.get("scan_depth", "standard"),
                ctx=ctx,
            )
            target = f"文件 #{p['file_id']}"
        elif scope == "task":
            result = self._orchestrator.audit_security_for_task(
                task_id=int(p["task_id"]), ctx=ctx,
            )
            target = f"任务 #{p['task_id']}"
        elif scope == "project":
            result = self._orchestrator.audit_security_for_project(
                project_id=int(p["project_id"]),
                top_n=int(p.get("top_n", 50)),
                trace_dataflow=bool(p.get("trace_dataflow", True)),
                ctx=ctx,
            )
            target = f"项目 #{p['project_id']}"
        else:
            return AgentResult(success=False, error=f"未知 scope: {scope}")

        if not result.success:
            return result
        data = result.data or {}
        findings = data.get("findings", []) or []
        risk_score = data.get("risk_score", 100)
        summary = data.get("summary", "")
        sev_counts = {"严重": 0, "高": 0, "中": 0, "低": 0}
        for f in findings:
            sev = f.get("severity", "中")
            if sev in sev_counts:
                sev_counts[sev] += 1
        score_icon = "🛡" if risk_score >= 80 else ("⚠️" if risk_score >= 50 else "🚨")
        lines = [
            f"**{score_icon} 安全审计完成** (风险评分 {risk_score}/100, 范围: {target})\n",
            summary,
            "",
            (
                f"严重 🔴 {sev_counts['严重']} · "
                f"高 🟠 {sev_counts['高']} · "
                f"中 🟡 {sev_counts['中']} · "
                f"低 🟢 {sev_counts['低']}"
            ),
            "",
        ]
        for i, f in enumerate(findings[:5]):
            sev_icon = {
                "严重": "🔴", "高": "🟠", "中": "🟡", "低": "🟢",
            }.get(f.get("severity", "中"), "⚪")
            owasp = f.get("owasp", "")
            cwe = f.get("cwe", "")
            lines.append(
                f"{sev_icon} **[{f.get('severity', '?')}]** {f.get('title', '')} "
                f"`{f.get('file_path', '')}` · {f.get('lines', '')}"
            )
            tags = " ".join(t for t in [owasp, cwe] if t)
            if tags:
                lines.append(f"   _{tags}_")
        if len(findings) > 5:
            lines.append(f"\n...还有 {len(findings) - 5} 处风险,请到「安全审计」页查看完整结果。")
        threat = data.get("threat_model") or {}
        if threat.get("data_flows"):
            lines.append(
                f"\n跨文件数据流: 检出 {len(threat['data_flows'])} 条可达攻击路径。"
            )
        lines.append("\n由 **`security_sentinel` Agent** 完成。")
        return AgentResult(
            success=True, data="\n".join(lines),
            model=result.model, duration_ms=result.duration_ms,
        )

    # ============ AgentSkill 升级:3 种新 intent handler ============

    def _handle_evolution_trigger(
        self, intent: dict, ctx: Optional[AgentContext]
    ) -> AgentResult:
        """委派 Orchestrator.trigger_evolution 触发指定 Agent 的自进化

        将"触发进化/跑一轮进化/蒸馏规则"等意图路由到对应 Agent 的
        SelfImprovement Skill(action=evolve),由 Orchestrator 统一调用
        invoke_skill,自动写入 agent_skill_record 与 audit_log。

        Args:
            intent: 意图 dict, payload 含:
                - agent_name (str): 目标 Agent, 默认 "evolution"
                - window_days (int): 反馈窗口天数, 默认 90
            ctx: 上下文

        Returns:
            AgentResult: data 为 Markdown 格式的进化结果摘要
        """
        if not self._check_orch():
            return AgentResult(success=False, error="Orchestrator 未注入")

        payload = intent.get("payload", {}) or {}
        agent_name = payload.get("agent_name") or "evolution"
        try:
            window_days = int(payload.get("window_days", 90))
        except (TypeError, ValueError):
            window_days = 90
        if window_days <= 0:
            window_days = 90

        result = self._orchestrator.trigger_evolution(
            agent_name=agent_name,
            window_days=window_days,
            ctx=ctx,
        )

        if not result.success:
            return result

        data = result.data if isinstance(result.data, dict) else {"raw": result.data}
        lines = [
            f"**自进化已触发** 🧬 (Agent: `{agent_name}`, 窗口 {window_days} 天)\n",
        ]
        # 兼容 skill_service 返回的多种字段格式
        summary = (
            data.get("summary")
            or data.get("message")
            or data.get("reason")
            or ""
        )
        if summary:
            lines.append(str(summary))
            lines.append("")

        proposals = data.get("proposals") or data.get("rules") or []
        if isinstance(proposals, list) and proposals:
            lines.append(f"**生成建议/规则 {len(proposals)} 条:**")
            for i, item in enumerate(proposals[:5]):
                if isinstance(item, dict):
                    title = item.get("title") or item.get("rule_name") or item.get("name", "")
                    desc = item.get("description") or item.get("summary", "")
                    lines.append(f"{i + 1}. **{title}** — {desc}")
                else:
                    lines.append(f"{i + 1}. {item}")
            if len(proposals) > 5:
                lines.append(f"\n...还有 {len(proposals) - 5} 条,详见日志/数据库。")

        applied = data.get("applied") or data.get("applied_count")
        if applied is not None:
            lines.append(f"\n已应用: **{applied}** 条")

        lines.append(
            f"\n由 **`{agent_name}` Agent** 的 SelfImprovement Skill 完成。"
        )
        return AgentResult(
            success=True,
            data="\n".join(lines),
            model=result.model,
            duration_ms=result.duration_ms,
        )

    def _handle_agent_skill_invoke(
        self, intent: dict, ctx: Optional[AgentContext]
    ) -> AgentResult:
        """委派 Orchestrator.invoke_skill 手动调用任意 Agent 的任意 Skill

        支持"调用 XX 的 Skill/手动触发 Skill/跑一下 XX.self_improve"等意图,
        通过 Orchestrator.invoke_skill 统一入口调用 skill_service,自动写
        agent_skill_record 与 audit_log。

        Args:
            intent: 意图 dict, payload 含:
                - agent_name (str): 目标 Agent name
                - skill_name (str): Skill name(形如 "<agent>.self_improve")
                - action (str, 可选): Skill 子动作, 如 evolve/check_proactive/
                  scan_domain/reflect_from_logs
                - params (dict, 可选): 透传给 Skill 的额外参数
            ctx: 上下文

        Returns:
            AgentResult: data 为 Markdown 格式的 Skill 调用结果摘要
        """
        if not self._check_orch():
            return AgentResult(success=False, error="Orchestrator 未注入")

        payload = intent.get("payload", {}) or {}
        agent_name = payload.get("agent_name")
        skill_name = payload.get("skill_name")
        if not agent_name or not skill_name:
            return AgentResult(
                success=False,
                error="缺少 agent_name 或 skill_name,无法调用 Skill",
            )

        # 组装 Skill 参数: action 优先,合并 params
        params: dict = {}
        action = payload.get("action")
        if action:
            params["action"] = action
        extra = payload.get("params") or {}
        if isinstance(extra, dict):
            params.update(extra)

        result = self._orchestrator.invoke_skill(
            agent_name=agent_name,
            skill_name=skill_name,
            params=params,
            ctx=ctx,
        )

        if not result.success:
            return result

        data = result.data if isinstance(result.data, dict) else {"raw": result.data}
        lines = [
            f"**Skill 调用完成** ⚙️ (`{skill_name}` on `{agent_name}`)\n",
        ]
        summary = (
            data.get("summary")
            or data.get("message")
            or data.get("reason")
            or ""
        )
        if summary:
            lines.append(str(summary))
            lines.append("")

        # 通用字段渲染:proposals / findings / actions / metrics
        for key in ("proposals", "findings", "actions", "rules", "items"):
            items = data.get(key)
            if isinstance(items, list) and items:
                lines.append(f"**{key} ({len(items)}):**")
                for i, item in enumerate(items[:5]):
                    if isinstance(item, dict):
                        title = (
                            item.get("title")
                            or item.get("rule_name")
                            or item.get("name")
                            or item.get("type", "")
                        )
                        desc = item.get("description") or item.get("summary", "")
                        lines.append(f"{i + 1}. **{title}** — {desc}")
                    else:
                        lines.append(f"{i + 1}. {item}")
                if len(items) > 5:
                    lines.append(f"\n...还有 {len(items) - 5} 条。")
                lines.append("")

        metrics = data.get("metrics") or data.get("stats")
        if isinstance(metrics, dict) and metrics:
            lines.append("**指标:**")
            for k, v in metrics.items():
                lines.append(f"- `{k}`: {v}")

        skill_type = "SelfImprovement" if "self_improve" in skill_name else "Proactive"
        lines.append(
            f"\n由 **`{agent_name}` Agent** 的 {skill_type} Skill 完成。"
        )
        return AgentResult(
            success=True,
            data="\n".join(lines),
            model=result.model,
            duration_ms=result.duration_ms,
        )

    def _handle_agent_status(
        self, intent: dict, ctx: Optional[AgentContext]
    ) -> AgentResult:
        """委派 Orchestrator.list_agent_skills 展示 Agent 状态/Skill 列表

        支持"Agent 状态/有哪些 Skill/Skill 调用记录/Agent 运行情况"等意图,
        返回当前已注册的 Skill 元数据列表(name/description/type/invocable/
        agent_name),便于运维人员快速排查 Skill 挂载是否正常。

        Args:
            intent: 意图 dict, payload 含:
                - agent_name (str, 可选): 指定 Agent, None=全部
                - detail (str, 可选): "skills"|"records"|"all",默认 "skills"
            ctx: 上下文

        Returns:
            AgentResult: data 为 Markdown 格式的 Skill 元数据列表
        """
        if not self._check_orch():
            return AgentResult(success=False, error="Orchestrator 未注入")

        payload = intent.get("payload", {}) or {}
        agent_name = payload.get("agent_name")  # None=全部
        detail = (payload.get("detail") or "skills").lower()
        if detail not in ("skills", "records", "all"):
            detail = "skills"

        # skill 元数据
        skills = self._orchestrator.list_agent_skills(agent_name)
        lines: List[str] = []
        target_label = f"`{agent_name}`" if agent_name else "全部 Agent"
        lines.append(f"**Agent 状态 · {target_label}** (共 {len(skills)} 个 Skill)\n")

        if not skills:
            lines.append(
                "未找到 Skill。请确认 Agent 已注册,Skill 已通过 _init_skills() 挂载。"
            )
            return AgentResult(success=True, data="\n".join(lines))

        # 按 agent_name 分组渲染
        by_agent: dict = {}
        for sk in skills:
            by_agent.setdefault(sk.get("agent_name", "?"), []).append(sk)
        for a_name, group in sorted(by_agent.items()):
            lines.append(f"### `{a_name}` ({len(group)} Skill)")
            for sk in group:
                invocable_icon = "✅" if sk.get("invocable") else "⛔"
                sk_type = sk.get("type", "?")
                lines.append(
                    f"- {invocable_icon} **`{sk.get('name', '?')}`** ({sk_type}) — "
                    f"{sk.get('description', '')}"
                )
            lines.append("")

        # records/all: 追加最近调用记录(若 DB 已注入)
        if detail in ("records", "all") and getattr(self._orchestrator, "_db", None):
            try:
                from app.services import skill_service
                records = skill_service.list_recent_records(
                    db=self._orchestrator._db,
                    agent_name=agent_name,
                    limit=10,
                )
                lines.append(f"**最近调用记录 ({len(records)} 条):**")
                for r in records:
                    lines.append(
                        f"- `{r.get('skill_name', '?')}` on `{r.get('agent_name', '?')}` "
                        f"· {r.get('trigger_type', '?')} "
                        f"· {'✅' if r.get('success') else '❌'} "
                        f"· {r.get('duration_ms', '?')}ms "
                        f"· {r.get('create_time', '?')}"
                    )
            except Exception as e:
                logger.warning(f"[ChatAgent] 读取 Skill 调用记录失败: {e}")
                lines.append(f"_读取调用记录失败: {e}_")

        return AgentResult(success=True, data="\n".join(lines))

    def dispatch_with_payload(self, intent_name: str, payload: dict,
                              ctx: Optional[AgentContext]) -> AgentResult:
        """供 /api/agents/clarify 回填后继续执行,统一走 handler

        Args:
            intent_name: 意图名称(与 _dispatch_single 中 handlers key 对齐)
            payload: 回填后的完整 payload(含已澄清字段)
            ctx: 上下文

        Returns:
            AgentResult: handler 执行结果
        """
        intent = {"intent": intent_name, "payload": payload}
        handlers = {
            "list_agents": lambda i, c: self._handle_list_agents(c),
            "detect_language": self._handle_detect_language,
            "analyze_project": self._handle_analyze_project,
            "review_code": self._handle_review_code,
            "list_projects": lambda i, c: self._handle_list_projects(c),
            "create_project": self._handle_create_project,
            "delete_project": self._handle_delete_project,
            "start_review": self._handle_start_review,
            "list_review_tasks": self._handle_list_review_tasks,
            "list_review_issues": self._handle_list_review_issues,
            "list_code_files": self._handle_list_code_files,
            "dashboard": lambda i, c: self._handle_dashboard(c),
            "list_rules": lambda i, c: self._handle_list_rules(c),
            "list_reports": lambda i, c: self._handle_list_reports(c),
            "generate_ai_prompt": self._handle_generate_ai_prompt,
            "security_audit": self._handle_security_audit,
            # AgentSkill 升级:3 种新 intent handler(与 _dispatch_single 保持一致)
            "evolution_trigger": self._handle_evolution_trigger,
            "agent_skill_invoke": self._handle_agent_skill_invoke,
            "agent_status": self._handle_agent_status,
        }
        handler = handlers.get(intent_name)
        if not handler:
            return AgentResult(success=False, error=f"不支持的 intent: {intent_name}")
        return handler(intent, ctx)

    def _classify_intent(self, last_msg: str,
                         messages: List[dict]) -> dict:
        """使用 LLM 分析用户意图"""
        context = "\n".join(
            f"{m['role']}: {m['content'][:200]}"
            for m in messages[-3:]
        )
        user_msg = f"对话上下文:\n{context}\n\n请判断用户意图:"

        self._system_prompt = _INTENT_SYSTEM
        self._temperature = 0.1
        self._max_tokens = 400

        try:
            result = self.call_json(user_msg)
            if result.success and isinstance(result.data, dict):
                logger.info(
                    f"[ChatAgent] 意图识别: {result.data.get('intent')} "
                    f"→ {result.data.get('reason', '')}"
                )
                return result.data
        except Exception as e:
            logger.warning(f"[ChatAgent] 意图识别失败, fallback chat: {e}")

        return {"intent": "chat", "reason": "fallback", "payload": {}}

    def _handle_list_agents(self, ctx: Optional[AgentContext]) -> AgentResult:
        """委派 Orchestrator 列出所有 Agent"""
        if not self._orchestrator:
            return AgentResult(success=False, error="Orchestrator 未注入")

        agents = self._orchestrator.list_agents()
        lines = ["**已注册的 Agent 列表:**\n"]
        for name, desc in agents.items():
            lines.append(f"- **`{name}`** — {desc}")

        content = "\n".join(lines) + (
            "\n\n你可以通过对话直接调度这些 Agent。\n"
            "例如发送代码片段让我调用「代码审查Agent」分析,"
            "或提供文件列表让「项目分析Agent」生成项目信息。"
        )
        return AgentResult(success=True, data=content, model=self._model)

    def _handle_detect_language(self, intent: dict,
                                 ctx: Optional[AgentContext]) -> AgentResult:
        """委派 Orchestrator → LanguageDetectorAgent"""
        if not self._orchestrator:
            return AgentResult(success=False, error="Orchestrator 未注入")

        payload = intent.get("payload", {})
        name = payload.get("project_name", "未知项目")
        desc = payload.get("description", "")

        result = self._orchestrator.detect_language(name, desc, ctx=ctx)
        if not result.success:
            return result

        data = result.data
        content = (
            f"**语言检测结果**\n\n"
            f"- 项目: `{name}`\n"
            f"- 主要语言: **{data['language_name']}** (`{data['language']}`)\n"
            f"- 置信度: {data['confidence']}\n"
            f"- 理由: {data['reason']}\n\n"
            f"语言检测由 **`language_detector` Agent** 完成。"
        )
        return AgentResult(
            success=True, data=content,
            model=result.model, duration_ms=result.duration_ms,
            tokens=result.tokens,
        )

    def _handle_analyze_project(self, intent: dict,
                                 ctx: Optional[AgentContext]) -> AgentResult:
        """委派 Orchestrator → ProjectAnalyzerAgent"""
        if not self._orchestrator:
            return AgentResult(success=False, error="Orchestrator 未注入")

        payload = intent.get("payload", {})
        folder = payload.get("folder_name", "")
        files = payload.get("file_names", [])

        result = self._orchestrator.analyze_project(folder, files, ctx=ctx)
        if not result.success:
            return result

        data = result.data
        content = (
            f"**项目分析结果**\n\n"
            f"- 项目名称: **{data['project_name']}**\n"
            f"- 主语言: **{data['language_name']}** (`{data['language']}`)\n"
            f"- 描述: {data['description']}\n"
            f"- 分析文件数: {len(files)}\n\n"
            f"项目分析由 **`project_analyzer` Agent** 完成。"
        )
        return AgentResult(
            success=True, data=content,
            model=result.model, duration_ms=result.duration_ms,
            tokens=result.tokens,
        )

    def _handle_review_code(self, intent: dict,
                             ctx: Optional[AgentContext]) -> AgentResult:
        """委派 Orchestrator → CodeReviewerAgent"""
        if not self._orchestrator:
            return AgentResult(success=False, error="Orchestrator 未注入")

        payload = intent.get("payload", {})
        code = payload.get("code", "")
        language = payload.get("language", "plaintext")

        if not code:
            return AgentResult(
                success=False,
                error="未检测到代码片段，请将代码用 ``` 包裹发送给我",
            )

        rules = "检查代码质量、潜在Bug、安全漏洞、性能问题和可维护性"
        result = self._orchestrator.review_code(
            code=code,
            rules=rules,
            language=language,
            file_name=payload.get("file_name", "代码片段"),
            ctx=ctx,
        )
        if not result.success:
            return result

        data = result.data
        if isinstance(data, str):
            data = json_lib.loads(data)
        issues = data if isinstance(data, list) else data.get("issues", [])
        total = len(issues) if isinstance(issues, list) else 0

        lines = [
            f"**代码审查结果** (共发现 {total} 个问题)\n",
            f"审查 Agent: `code_reviewer` | 语言: {language} | 代码行数: {len(code.splitlines())}\n",
        ]
        if isinstance(issues, list):
            for i, iss in enumerate(issues[:8]):
                sev = iss.get("severity", "?")
                sev_icon = {"severe": "🔴", "high": "🟠", "medium": "🟡", "low": "🟢"}.get(sev, "⚪")
                lines.append(
                    f"{sev_icon} **[{sev}]** {iss.get('issue_type', '')} "
                    f"L{iss.get('line', '?')} — {iss.get('description', '')}"
                )
        if total > 8:
            lines.append(f"\n...还有 {total - 8} 个问题未显示")

        content = "\n".join(lines)
        return AgentResult(
            success=True, data=content,
            model=result.model, duration_ms=result.duration_ms,
            tokens=result.tokens,
        )

    def _check_orch(self) -> bool:
        return self._orchestrator is not None

    def _handle_list_projects(self, ctx: Optional[AgentContext]) -> AgentResult:
        if not self._check_orch():
            return AgentResult(success=False, error="Orchestrator 未注入")
        result = self._orchestrator.list_projects()
        if not result.success:
            return result
        items = result.data.get("items", [])
        total = result.data.get("total", 0)
        lines = [f"**项目列表** (共 {total} 个)\n"]
        for p in items:
            lang = p.get("language", "—")
            lines.append(
                f"- **#{p['id']}** {p['project_name']} "
                f"| 语言: `{lang}` | 文件: {p['file_count']} | 状态: {p['status']}"
            )
        if not items:
            lines.append("还没有项目，去新建一个吧！")
        return AgentResult(success=True, data="\n".join(lines))

    def _handle_create_project(self, intent: dict,
                                ctx: Optional[AgentContext]) -> AgentResult:
        if not self._check_orch():
            return AgentResult(success=False, error="Orchestrator 未注入")
        p = intent.get("payload", {})
        name = p.get("project_name", "未命名项目")
        desc = p.get("description", "")
        lang = p.get("language", "plaintext")
        result = self._orchestrator.create_project(name, desc, lang, ctx=ctx)
        if not result.success:
            return result
        data = result.data
        return AgentResult(success=True, data=(
            f"**项目已创建** ✅\n\n"
            f"- ID: `{data['id']}`\n"
            f"- 名称: **{data['project_name']}**\n"
            f"- 语言: `{data.get('language', '—')}`\n"
            f"- 状态: {data.get('status', 'active')}\n\n"
            f"由 **`project_manager` Agent** 完成。"
        ))

    def _handle_delete_project(self, intent: dict,
                                ctx: Optional[AgentContext]) -> AgentResult:
        if not self._check_orch():
            return AgentResult(success=False, error="Orchestrator 未注入")
        pid = intent.get("payload", {}).get("project_id")
        if not pid:
            return AgentResult(success=False, error="请指定要删除的项目ID")
        result = self._orchestrator.delete_project(pid, ctx=ctx)
        if not result.success:
            return result
        return AgentResult(success=True, data=(
            f"**项目 #{pid} 已删除** ✅\n\n由 **`project_manager` Agent** 完成。"
        ))

    def _handle_list_review_tasks(self, intent: dict,
                                   ctx: Optional[AgentContext]) -> AgentResult:
        if not self._check_orch():
            return AgentResult(success=False, error="Orchestrator 未注入")
        pid = intent.get("payload", {}).get("project_id")
        result = self._orchestrator.list_review_tasks(
            project_id=pid, page_size=20, ctx=ctx)
        if not result.success:
            return result
        items = result.data.get("items", [])
        total = result.data.get("total", 0)
        lines = [f"**审查任务列表** (共 {total} 个)\n"]
        for t in items:
            lines.append(
                f"- **#{t['id']}** {t.get('task_name', '—')} "
                f"| 评分: {t.get('score', '—')} | 问题: {t.get('total_issues', 0)} "
                f"| 状态: `{t['status']}`"
            )
        if not items:
            lines.append("暂无审查记录")
        lines.append("\n由 **`review_orchestrator` Agent** 完成。")
        return AgentResult(success=True, data="\n".join(lines))

    def _handle_list_review_issues(self, intent: dict,
                                    ctx: Optional[AgentContext]) -> AgentResult:
        if not self._check_orch():
            return AgentResult(success=False, error="Orchestrator 未注入")
        tid = intent.get("payload", {}).get("task_id")
        if not tid:
            return AgentResult(success=False, error="请指定审查任务ID")
        result = self._orchestrator.list_review_issues(tid, page_size=20, ctx=ctx)
        if not result.success:
            return result
        items = result.data.get("items", [])
        total = result.data.get("total", 0)
        lines = [f"**审查问题列表** (共 {total} 个)\n"]
        for i in items:
            sev = i.get("severity", "?")
            sev_icon = {"severe": "🔴", "high": "🟠", "medium": "🟡", "low": "🟢"}.get(sev, "⚪")
            lines.append(
                f"{sev_icon} **[{sev}]** {i.get('issue_type', '')} "
                f"— {i.get('title', '')} "
                f"({i.get('file_name', '')} L{i.get('line_number', '?')})"
            )
        if not items:
            lines.append("该任务暂无问题记录")
        lines.append("\n由 **`review_orchestrator` Agent** 完成。")
        return AgentResult(success=True, data="\n".join(lines))

    def _handle_list_code_files(self, intent: dict,
                                 ctx: Optional[AgentContext]) -> AgentResult:
        if not self._check_orch():
            return AgentResult(success=False, error="Orchestrator 未注入")
        pid = intent.get("payload", {}).get("project_id")
        if not pid:
            return AgentResult(success=False, error="请指定项目ID")
        result = self._orchestrator.list_code_files(pid, page_size=50, ctx=ctx)
        if not result.success:
            return result
        items = result.data.get("items", [])
        total = result.data.get("total", 0)
        lines = [f"**代码文件列表** (共 {total} 个)\n"]
        for f in items:
            lines.append(
                f"- **`{f['file_name']}`** "
                f"| 语言: {f.get('language', '?')} "
                f"| {f.get('line_count', 0)} 行"
            )
        if not items:
            lines.append("该项目暂无代码文件")
        lines.append("\n由 **`code_file_manager` Agent** 完成。")
        return AgentResult(success=True, data="\n".join(lines))

    def _handle_dashboard(self, ctx: Optional[AgentContext]) -> AgentResult:
        if not self._check_orch():
            return AgentResult(success=False, error="Orchestrator 未注入")
        result = self._orchestrator.dashboard_summary(ctx=ctx)
        if not result.success:
            return result
        d = result.data
        return AgentResult(success=True, data=(
            f"**平台仪表盘** 📊\n\n"
            f"- 项目总数: **{d.get('total_projects', 0)}**\n"
            f"- 审查任务总数: **{d.get('total_tasks', 0)}**\n"
            f"- 平均评分: **{d.get('avg_score', 0)}**\n"
            f"- 问题总数: **{d.get('total_issues', 0)}**\n"
            f"- 严重问题: 🔴 {d.get('severe_issues', 0)} | "
            f"高: 🟠 {d.get('high_issues', 0)} | "
            f"中: 🟡 {d.get('medium_issues', 0)} | "
            f"低: 🟢 {d.get('low_issues', 0)}\n\n"
            f"由 **`dashboard` Agent** 完成。"
        ))

    def _handle_list_rules(self, ctx: Optional[AgentContext]) -> AgentResult:
        if not self._check_orch():
            return AgentResult(success=False, error="Orchestrator 未注入")
        result = self._orchestrator.list_rules(ctx=ctx)
        if not result.success:
            return result
        items = result.data.get("items", [])
        total = result.data.get("total", 0)
        lines = [f"**审查规则列表** (共 {total} 条)\n"]
        for r in items:
            builtin = "🏗️ 内置" if r.get("is_builtin") else "✏️ 自定义"
            status = "✅ 启用" if r.get("enabled") else "⏸️ 禁用"
            lines.append(f"- **{r['rule_name']}** `{r.get('rule_type', '')}` {builtin} {status}")
        lines.append("\n由 **`rule_manager` Agent** 完成。")
        return AgentResult(success=True, data="\n".join(lines))

    def _handle_list_reports(self, ctx: Optional[AgentContext]) -> AgentResult:
        if not self._check_orch():
            return AgentResult(success=False, error="Orchestrator 未注入")
        result = self._orchestrator.list_reports(ctx=ctx)
        if not result.success:
            return result
        items = result.data.get("items", [])
        total = result.data.get("total", 0)
        lines = [f"**审查报告列表** (共 {total} 份)\n"]
        for r in items[:10]:
            lines.append(
                f"- **#{r.get('task_id', '?')}** {r.get('project_name', '')} "
                f"| 评分: {r.get('score', '—')} | 类型: {r.get('review_type', '')}"
            )
        if not items:
            lines.append("暂无审查报告")
        lines.append("\n由 **`reporter` Agent** 完成。")
        return AgentResult(success=True, data="\n".join(lines))

    def _handle_start_review(self, intent: dict,
                              ctx: Optional[AgentContext]) -> AgentResult:
        if not self._check_orch():
            return AgentResult(success=False, error="Orchestrator 未注入")
        p = intent.get("payload", {})
        pid = p.get("project_id")
        review_type = p.get("review_type", "quick")
        name = p.get("task_name", "")

        if not pid:
            return AgentResult(success=False, error="请指定要审查的项目ID")

        result = self._orchestrator.start_review(
            project_id=pid, file_ids=[],
            review_type=review_type, task_name=name, ctx=ctx,
        )
        if not result.success:
            return result
        d = result.data
        return AgentResult(success=True, data=(
            f"**审查任务已启动** 🚀\n\n"
            f"- 任务ID: `{d['task_id']}`\n"
            f"- 审查类型: `{review_type}`\n"
            f"- 文件数: {d.get('total_files', 0)}\n"
            f"- 状态: `{d['status']}`\n\n"
            f"由 **`review_orchestrator` Agent** 完成。\n"
            f"审查完成后可在「审查记录」页面查看结果。"
        ))

    def _handle_chat(self, messages: List[dict],
                     ctx: Optional[AgentContext]) -> AgentResult:
        """普通对话,直接回复"""
        self._system_prompt = self.system_prompt
        self._temperature = 0.7
        self._max_tokens = 4096

        # 个性化注入(画像 + 个人知识库 RAG);任何异常都降级为不注入
        persona_block = ""
        try:
            db = getattr(self._orchestrator, "_db", None)
            uid = ctx.user_id if ctx else None
            if db is not None and uid:
                from app.services import personalization_service
                query = messages[-1]["content"] if messages else ""
                persona_block = personalization_service.chat_context_for_agent(db, uid, query)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[chat_agent] 个性化注入失败,降级: {e}")
        system_content = self._system_prompt + (persona_block or "")

        history = []
        for msg in messages[-10:]:
            history.append({"role": msg["role"], "content": msg["content"]})

        messages_for_api = [
            {"role": "system", "content": system_content},
        ] + history

        import time

        import httpx

        last_error = None
        for attempt in range(self._max_retries + 1):
            t0 = time.time()
            try:
                with httpx.Client(timeout=self._timeout) as client:
                    resp = client.post(
                        f"{self._base_url}/chat/completions",
                        headers={
                            "Authorization": f"Bearer {self._api_key}",
                            "Content-Type": "application/json",
                        },
                        json={
                            "model": self._model,
                            "messages": messages_for_api,
                            "temperature": self._temperature,
                            "max_tokens": self._max_tokens,
                        },
                    )
                duration_ms = int((time.time() - t0) * 1000)
                if resp.status_code == 200:
                    body = resp.json()
                    return AgentResult(
                        success=True,
                        data=body["choices"][0]["message"]["content"],
                        model=body.get("model", self._model),
                        duration_ms=duration_ms,
                    )
                if resp.status_code == 429:
                    last_error = "请求过于频繁"
                elif resp.status_code >= 500:
                    last_error = f"服务异常({resp.status_code})"
                else:
                    last_error = f"调用失败({resp.status_code})"
            except Exception as e:
                last_error = str(e)
            if attempt < self._max_retries:
                time.sleep(2 ** (attempt + 1))

        return AgentResult(success=False, error=f"聊天失败: {last_error}")
