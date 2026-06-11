from typing import List, Optional

from loguru import logger
from sqlalchemy.orm import Session

from app.agents.ai_prompt_agent import AiPromptAgent
from app.agents.base import AgentContext, AgentResult, BaseAgent
from app.agents.chat_agent import ChatAssistantAgent
from app.agents.dashboard_agent import DashboardAgent
from app.agents.event_bus import emit_event
from app.agents.events import AgentEventType, new_trace_id
from app.agents.evolution_agent import EvolutionAgent
from app.agents.file_agent import CodeFileManagerAgent
from app.agents.language_agent import LanguageDetectorAgent
from app.agents.project_agent import ProjectAnalyzerAgent
from app.agents.project_manager_agent import ProjectManagerAgent
from app.agents.registry import AgentRegistry
from app.agents.report_agent import ReportAgent
from app.agents.review_agent import CodeReviewerAgent
from app.agents.review_orchestrator_agent import ReviewOrchestratorAgent
from app.agents.rule_agent import RuleManagerAgent
from app.agents.security_sentinel_agent import SecuritySentinelAgent
from app.models.user import User
from app.utils.api_resolver import ApiConfig, resolve_api_config


class Orchestrator(BaseAgent):
    """主调度 Agent — 宏观调控层

    管理全部专业 Agent(含 v3.0 新增的自进化代理), 注入 DB 依赖,
    通过 ChatAgent 统一入口接收用户指令。

    v3.1: 支持用户自定义 API 配置; 通过 set_api_config() 注入,
    chat() 和 start_review() 自动传递。
    """

    name = "orchestrator"
    description = "主调度 Agent, 协调所有子 Agent 完成全平台功能"
    icon = "orchestrator"
    color = "#5B58E8"
    category = "meta"
    skills = ("Agent 路由", "依赖注入", "调度编排", "执行结果汇总")

    def __init__(self):
        super().__init__(temperature=0.5, max_tokens=256)
        self._registry = AgentRegistry.instance()
        self._api_config: Optional[ApiConfig] = None
        self._db: Optional[Session] = None
        self._init_agents()

    def _init_agents(self):
        self.lang_agent = LanguageDetectorAgent()
        self.project_agent = ProjectAnalyzerAgent()
        self.code_reviewer = CodeReviewerAgent()

        self.project_mgr = ProjectManagerAgent()
        self.review_orch = ReviewOrchestratorAgent()
        self.file_mgr = CodeFileManagerAgent()
        self.dashboard_agent = DashboardAgent()
        self.rule_mgr = RuleManagerAgent()
        self.reporter = ReportAgent()
        self.ai_prompt = AiPromptAgent()
        self.security_sentinel = SecuritySentinelAgent()
        self.evolution_agent = EvolutionAgent()

        self.chat_agent = ChatAssistantAgent()
        self.chat_agent.set_orchestrator(self)

        for a in [
            self.lang_agent, self.project_agent, self.code_reviewer,
            self.project_mgr, self.review_orch, self.file_mgr,
            self.dashboard_agent, self.rule_mgr, self.reporter,
            self.ai_prompt, self.security_sentinel, self.evolution_agent,
            self.chat_agent,
        ]:
            self._registry.register(a)
        self._registry.register(self)

        logger.info(
            f"[Orchestrator] 已注册 {len(self._registry.list())} 个 Agent: "
            f"{', '.join(self._registry.list().keys())}"
        )

    def set_api_config(self, api_config: Optional[ApiConfig]) -> None:
        """v3.1: 注入用户自定义 API 配置

        聊天和审查流程将在调用 LLM 时自动使用该配置。
        传入 None 恢复系统默认。
        """
        self._api_config = api_config
        if api_config:
            # 将配置应用到 chat_agent 的底层属性
            self.chat_agent._base_url = api_config.base_url.rstrip("/")
            self.chat_agent._api_key = api_config.api_key
            self.chat_agent._model = api_config.model
            logger.info(
                f"[Orchestrator] API 配置已注入: "
                f"source={api_config.source} model={api_config.model}"
            )
        else:
            # 恢复默认
            from app.core.config import settings as s
            self.chat_agent._base_url = s.deepseek_base_url.rstrip("/")
            self.chat_agent._api_key = s.deepseek_api_key
            self.chat_agent._model = s.deepseek_model

    def get_api_config(self) -> Optional[ApiConfig]:
        """获取当前注入的 API 配置"""
        return self._api_config

    def inject_db(self, db: Session, user: Optional[User] = None) -> None:
        inject_user = user or self._make_dummy_user()
        for a in [
            self.project_mgr, self.review_orch, self.file_mgr,
            self.dashboard_agent, self.rule_mgr, self.reporter,
            self.ai_prompt, self.security_sentinel, self.evolution_agent,
        ]:
            a.inject(db, user=inject_user)
        self._db = db

        # v3.1: 自动解析用户 API 配置
        if user:
            cfg = resolve_api_config(db, user.id)
            self.set_api_config(cfg)

        logger.info("[Orchestrator] DB 已注入到所有操作类 Agent")

    @staticmethod
    def _make_dummy_user():
        from app.models.user import User
        return User(id=1, role="admin", status=1, username="agent")

    def detect_language(self, *args, **kw) -> AgentResult:
        kw.pop("ctx", None)
        return self.lang_agent.execute(*args, **kw)

    def analyze_project(self, *args, **kw) -> AgentResult:
        kw.pop("ctx", None)
        return self.project_agent.execute(*args, **kw)

    def review_code(self, *args, **kw) -> AgentResult:
        return self.code_reviewer.execute(*args, **kw)

    def create_project(self, *args, **kw) -> AgentResult:
        return self.project_mgr.create_project(*args, **kw)

    def list_projects(self, *args, **kw) -> AgentResult:
        return self.project_mgr.list_projects(*args, **kw)

    def delete_project(self, *args, **kw) -> AgentResult:
        return self.project_mgr.delete_project(*args, **kw)

    def start_review(self, project_id: int, file_ids: List[int],
                     review_type: str = "quick", task_name: str = "",
                     user: Optional[User] = None,
                     ctx: Optional[AgentContext] = None) -> AgentResult:
        return self.review_orch.start_review(
            project_id, file_ids, review_type, task_name, user, ctx)

    def list_review_tasks(self, *args, **kw) -> AgentResult:
        return self.review_orch.list_tasks(*args, **kw)

    def list_review_issues(self, *args, **kw) -> AgentResult:
        return self.review_orch.list_issues(*args, **kw)

    def list_code_files(self, *args, **kw) -> AgentResult:
        return self.file_mgr.list_files(*args, **kw)

    def dashboard_summary(self, *args, **kw) -> AgentResult:
        return self.dashboard_agent.summary(*args, **kw)

    def list_rules(self, *args, **kw) -> AgentResult:
        return self.rule_mgr.list_rules(*args, **kw)

    def list_reports(self, *args, **kw) -> AgentResult:
        return self.reporter.list_reports(*args, **kw)

    def generate_ai_prompt_for_issue(self, issue_id: int, target_tool: str = "generic",
                                     use_llm: bool = True,
                                     ctx: Optional[AgentContext] = None) -> AgentResult:
        return self.ai_prompt.execute_for_issue(issue_id, target_tool, use_llm, ctx)

    def generate_ai_prompt_for_task(self, task_id: int, target_tool: str = "generic",
                                    severity_filter: Optional[List[str]] = None,
                                    use_llm: bool = True,
                                    ctx: Optional[AgentContext] = None) -> AgentResult:
        return self.ai_prompt.execute_for_task(
            task_id, target_tool, severity_filter, use_llm, ctx)

    def generate_ai_prompt_for_project(self, project_id: int, target_tool: str = "generic",
                                       top_n: int = 30, use_llm: bool = True,
                                       ctx: Optional[AgentContext] = None) -> AgentResult:
        return self.ai_prompt.execute_for_project(
            project_id, target_tool, top_n, use_llm, ctx)

    def audit_security_for_file(self, file_id: int, scan_depth: str = "standard",
                                ctx: Optional[AgentContext] = None) -> AgentResult:
        return self.security_sentinel.scan_file(file_id, scan_depth, ctx)

    def audit_security_for_task(self, task_id: int,
                                ctx: Optional[AgentContext] = None) -> AgentResult:
        return self.security_sentinel.scan_task(task_id, ctx)

    def audit_security_for_project(self, project_id: int, top_n: int = 50,
                                   trace_dataflow: bool = True,
                                   ctx: Optional[AgentContext] = None) -> AgentResult:
        return self.security_sentinel.scan_project(
            project_id, top_n, trace_dataflow, ctx)

    def chat(self, messages: List[dict],
             ctx: Optional[AgentContext] = None) -> AgentResult:
        """v2.0: 主控分发聊天调用,生成 trace_id 并广播 DISPATCH 事件"""
        if ctx is None:
            ctx = AgentContext()
        trace_id = (ctx.extra or {}).get("trace_id") or new_trace_id()
        ctx.extra = {**(ctx.extra or {}), "trace_id": trace_id}
        emit_event(
            AgentEventType.DISPATCH,
            agent=self.name,
            trace_id=trace_id,
            message="主控接收用户请求,准备调度 ChatAgent",
            payload={"messages": len(messages)},
        )
        return self.chat_agent.execute(messages, ctx)

    def list_agents(self) -> dict:
        return self._registry.list()

    def get_agent(self, name: str):
        return self._registry.get(name)


_orchestrator: Optional[Orchestrator] = None


def get_orchestrator() -> Orchestrator:
    global _orchestrator
    if _orchestrator is None:
        _orchestrator = Orchestrator()
    return _orchestrator
