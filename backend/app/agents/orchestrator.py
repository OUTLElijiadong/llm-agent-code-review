from typing import Any, Dict, List, Optional

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
from app.agents.operations_agent import OperationsAgent
from app.agents.project_agent import ProjectAnalyzerAgent
from app.agents.project_manager_agent import ProjectManagerAgent
from app.agents.registry import AgentRegistry
from app.agents.report_agent import ReportAgent
from app.agents.review_agent import CodeReviewerAgent
from app.agents.review_orchestrator_agent import ReviewOrchestratorAgent
from app.agents.rule_agent import RuleManagerAgent
from app.agents.sandbox_agents import SandboxDeployerAgent, TestVerifierAgent
from app.agents.security_sentinel_agent import SecuritySentinelAgent
from app.agents.tool_contracts import (
    FixedToolArgumentError,
    fixed_tool_accepts_ctx,
    is_fixed_tool,
    validate_fixed_tool_arguments,
)
from app.core.permission_codes import PermissionCode
from app.models.user import User
from app.schemas.project import ProjectUpdateIn
from app.services import project_service, project_source_service
from app.services.rbac_service import check_permission
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

    def __init__(self, register: bool = True):
        super().__init__(temperature=0.5, max_tokens=256)
        # register=True: 启动时构造的"元数据"实例,把所有 Agent 登记进全局注册中心,
        #   供 /api/agents 等只读元数据端点消费。
        # register=False: 每个请求构造的独立实例,持有请求级 db/user,绝不写全局
        #   注册中心 —— 避免并发请求互相覆盖注入态导致跨用户数据/身份串号。
        self._register = register
        self._registry = AgentRegistry.instance()
        self._api_config: Optional[ApiConfig] = None
        self._db: Optional[Session] = None
        self._user: Optional[User] = None
        self._init_agents()

    def _init_skills(self) -> None:
        """子类 override:挂载 OrchestratorSelfImprovementSkill + OrchestratorProactiveSkill

        将主调度 Agent 的自进化与主动监测能力下沉到 Skill,通过 SkillRegistry
        统一注册,供 Orchestrator.invoke_skill / ChatPlanner 查询调用。

        注意:本方法在 super().__init__() 末尾被调用,先于 _init_agents() 执行,
        挂载的是 Orchestrator 自身的 Skill(orchestrator.self_improve + orchestrator.proactive),
        与子 Agent 无关,子 Agent 各自在自己的 _init_skills() 中挂载专属 Skill。
        """
        from app.agents.skills.orchestrator_skill import (
            OrchestratorProactiveSkill,
            OrchestratorSelfImprovementSkill,
        )

        self.attach_skill(OrchestratorSelfImprovementSkill(self.name))
        self.attach_skill(OrchestratorProactiveSkill(self.name))

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
        self.test_verifier = TestVerifierAgent()
        self.sandbox_deployer = SandboxDeployerAgent()
        self.evolution_agent = EvolutionAgent()
        self.operations_agent = OperationsAgent()

        self.chat_agent = ChatAssistantAgent()
        self.chat_agent.set_orchestrator(self)

        # 请求级实例(register=False)不写全局注册中心,避免并发覆盖。
        if not self._register:
            return

        for a in [
            self.lang_agent, self.project_agent, self.code_reviewer,
            self.project_mgr, self.review_orch, self.file_mgr,
            self.dashboard_agent, self.rule_mgr, self.reporter,
            self.ai_prompt, self.security_sentinel, self.test_verifier,
            self.sandbox_deployer, self.evolution_agent,
            self.operations_agent, self.chat_agent,
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
        """注入请求级数据库会话与真实登录用户。

        Args:
            db: 当前请求使用的数据库会话。
            user: 当前认证用户;请求级 Agent 操作必须显式传入。

        Raises:
            ValueError: 未传入真实用户,避免回退为伪 admin 身份。
        """
        if user is None:
            raise ValueError("请求级 Orchestrator 必须注入真实用户,禁止使用演示/兜底身份")
        for a in [
            self.project_mgr, self.review_orch, self.file_mgr,
            self.dashboard_agent, self.rule_mgr, self.reporter,
            self.ai_prompt, self.security_sentinel, self.test_verifier,
            self.sandbox_deployer, self.evolution_agent,
        ]:
            a.inject(db, user=user)
        self._db = db
        self._user = user

        # v3.1: 自动解析用户 API 配置
        cfg = resolve_api_config(db, user.id)
        self.set_api_config(cfg)

        logger.info("[Orchestrator] DB 已注入到所有操作类 Agent")

    def _disabled_result(self, agent_code: str) -> Optional[AgentResult]:
        """返回停用结果；未注入 DB 或未配置画像时保持历史兼容。"""
        if self._db is None or not hasattr(self._db, "query"):
            return None
        from app.services import agent_governance_service

        if agent_governance_service.is_runtime_enabled(self._db, agent_code):
            return None
        return AgentResult(success=False, error=f"Agent {agent_code} 已停用，操作未执行")

    def detect_language(self, *args, **kw) -> AgentResult:
        if disabled := self._disabled_result("language_detector"):
            return disabled
        kw.pop("ctx", None)
        return self.lang_agent.execute(*args, **kw)

    def analyze_project(self, *args, **kw) -> AgentResult:
        if disabled := self._disabled_result("project_analyzer"):
            return disabled
        kw.pop("ctx", None)
        return self.project_agent.execute(*args, **kw)

    def review_code(self, *args, **kw) -> AgentResult:
        if disabled := self._disabled_result("code_reviewer"):
            return disabled
        kw.pop("ctx", None)
        return self.code_reviewer.execute(*args, **kw)

    def create_project(self, *args, **kw) -> AgentResult:
        if disabled := self._disabled_result("project_manager"):
            return disabled
        return self.project_mgr.create_project(*args, **kw)

    def list_projects(self, *args, **kw) -> AgentResult:
        if disabled := self._disabled_result("project_manager"):
            return disabled
        return self.project_mgr.list_projects(*args, **kw)

    def get_project_detail(self, project_id: int,
                           ctx: Optional[AgentContext] = None) -> AgentResult:
        """返回项目完整详情(含源码修复副本 source_revisions,供选用副本跑沙箱)。"""
        if self._db is None or self._user is None:
            return AgentResult(success=False, error="DB 或用户上下文未注入")
        if not check_permission(self._db, self._user.id, PermissionCode.PROJECT_VIEW):
            return AgentResult(success=False, error="当前用户没有 project:view 权限")
        try:
            data = project_service.get_project(self._db, self._user, project_id)
            return AgentResult(success=True, data=data)
        except Exception as exc:
            return AgentResult(success=False, error=str(exc))

    def delete_project(self, *args, **kw) -> AgentResult:
        if disabled := self._disabled_result("project_manager"):
            return disabled
        return self.project_mgr.delete_project(*args, **kw)

    def update_project(self, project_id: int, project_name: Optional[str] = None,
                       description: Optional[str] = None, language: Optional[str] = None,
                       status: Optional[str] = None,
                       ctx: Optional[AgentContext] = None) -> AgentResult:
        """通过当前请求用户更新项目元数据。"""
        if self._db is None or self._user is None:
            return AgentResult(success=False, error="DB 或用户上下文未注入")
        if self._user.role in {"admin", "super_admin"}:
            from app.models.project import Project as _Project
            target = self._db.get(_Project, project_id)
            if target is None or target.status == "deleted":
                return AgentResult(success=False, error=f"项目 #{project_id} 不存在")
            if target.user_id != self._user.id:
                return AgentResult(
                    success=False,
                    error=(
                        f"项目 #{project_id}「{target.project_name}」不是管理员自有项目，"
                        "管理员对话中仅可修改自己拥有的项目；其他用户的项目只读。"
                    ),
                )
        try:
            project = project_service.update_project(
                self._db, self._user, project_id,
                ProjectUpdateIn(project_name=project_name, description=description,
                                language=language, status=status),
            )
            return AgentResult(success=True, data={"id": project.id, "project_name": project.project_name,
                                                   "status": project.status})
        except Exception as exc:
            return AgentResult(success=False, error=str(exc))

    def import_remote_project(self, url: str, project_name: str, description: str = "",
                              language: Optional[str] = None,
                              audit_mode: bool = False,
                              ctx: Optional[AgentContext] = None) -> AgentResult:
        """通过当前用户导入公开远程源码归档。"""
        if self._db is None or self._user is None:
            return AgentResult(success=False, error="DB 或用户上下文未注入")
        if not check_permission(self._db, self._user.id, PermissionCode.PROJECT_IMPORT):
            return AgentResult(success=False, error="当前用户没有 project:import 权限")
        try:
            data = project_source_service.import_remote_project(
                self._db, self._user, url=url, project_name=project_name,
                description=description, language=language, audit_mode=audit_mode,
            )
            return AgentResult(success=True, data=data)
        except Exception as exc:
            return AgentResult(success=False, error=str(exc))

    def download_project_source(self, project_id: int,
                                ctx: Optional[AgentContext] = None) -> AgentResult:
        """返回项目源码 ZIP 的同源下载地址；实际下载仍经过 HTTP 鉴权。"""
        if self._db is None or self._user is None:
            return AgentResult(success=False, error="DB 或用户上下文未注入")
        if not check_permission(self._db, self._user.id, PermissionCode.PROJECT_VIEW):
            return AgentResult(success=False, error="当前用户没有 project:view 权限")
        if not check_permission(self._db, self._user.id, PermissionCode.FILE_DOWNLOAD):
            return AgentResult(success=False, error="当前用户没有 file:download 权限")
        try:
            project = project_service.get_project(self._db, self._user, project_id)
            archive = project_source_service.get_source_archive_metadata(
                self._db, self._user, project_id,
            )
            data = {
                "project_id": project_id,
                "project_name": project["project_name"],
                "download_path": f"/api/projects/{project_id}/source-archive",
                "download_url": f"/api/projects/{project_id}/source-archive",
                "authentication": "current_user",
                "source_mode": "audit_archive" if archive else "files",
            }
            if archive:
                data.update({
                    "archive_sha256": archive["archive_sha256"],
                    "malware_status": archive["malware_status"],
                    "audit_status": archive["audit_status"],
                    "file_count": archive["file_count"],
                })
            return AgentResult(success=True, data=data)
        except Exception as exc:
            return AgentResult(success=False, error=str(exc))

    def start_review(self, project_id: int, file_ids: Optional[List[int]] = None,
                     review_type: str = "quick", task_name: str = "",
                     user: Optional[User] = None,
                     ctx: Optional[AgentContext] = None) -> AgentResult:
        """通过统一入口启动项目审查并补齐缺省文件列表。

        ChatAssistant 直接 handler、ChatPlanner 固定工具和其他调用方都经过此
        方法。显式文件列表原样下传；缺失或为空时查询请求级数据库，仅选择同
        项目 `status="active"` 的文件并按 ID 升序。ReviewService 继续负责最终
        权限、归属和数量校验。

        Args:
            project_id: 待审查项目 ID。
            file_ids: 可选文件 ID 列表；为空时自动解析 active 文件。
            review_type: 审查类型，默认 quick。
            task_name: 可选任务名称。
            user: 可选显式用户；通常由已注入的专业 Agent 使用请求用户。
            ctx: 当前 Agent 调用上下文。

        Returns:
            AgentResult: 成功时返回下游任务结果；无数据库、无 active 文件或
            查询异常时返回安全失败且不调用下游。
        """
        if disabled := self._disabled_result("review_orchestrator"):
            return disabled
        resolved_file_ids = list(file_ids or [])
        if not resolved_file_ids:
            try:
                if self._db is not None:
                    from app.models.code_file import CodeFile
                    rows = self._db.query(CodeFile.id).filter(
                        CodeFile.project_id == project_id,
                        CodeFile.status == "active",
                    ).order_by(CodeFile.id.asc()).all()
                    resolved_file_ids = [row[0] for row in rows]
            except Exception as exc:
                logger.warning(f"[Orchestrator] 自动获取审查文件失败: {exc}")

        if not resolved_file_ids:
            return AgentResult(
                success=False,
                error=f"项目 #{project_id} 下没有可审查的代码文件，请先上传代码文件后再发起审查。",
            )
        return self.review_orch.start_review(
            project_id, resolved_file_ids, review_type, task_name, user, ctx)

    def list_review_tasks(self, *args, **kw) -> AgentResult:
        if disabled := self._disabled_result("review_orchestrator"):
            return disabled
        return self.review_orch.list_tasks(*args, **kw)

    def list_review_issues(self, *args, **kw) -> AgentResult:
        if disabled := self._disabled_result("review_orchestrator"):
            return disabled
        return self.review_orch.list_issues(*args, **kw)

    def list_code_files(self, *args, **kw) -> AgentResult:
        if disabled := self._disabled_result("code_file_manager"):
            return disabled
        return self.file_mgr.list_files(*args, **kw)

    def dashboard_summary(self, *args, **kw) -> AgentResult:
        if disabled := self._disabled_result("dashboard"):
            return disabled
        return self.dashboard_agent.summary(*args, **kw)

    def list_rules(self, *args, **kw) -> AgentResult:
        if disabled := self._disabled_result("rule_manager"):
            return disabled
        return self.rule_mgr.list_rules(*args, **kw)

    def list_reports(self, *args, **kw) -> AgentResult:
        if disabled := self._disabled_result("reporter"):
            return disabled
        return self.reporter.list_reports(*args, **kw)

    def generate_ai_prompt_for_issue(self, issue_id: int, target_tool: str = "generic",
                                     use_llm: bool = True,
                                     ctx: Optional[AgentContext] = None) -> AgentResult:
        if disabled := self._disabled_result("ai_prompt"):
            return disabled
        return self.ai_prompt.execute_for_issue(issue_id, target_tool, use_llm, ctx)

    def generate_ai_prompt_for_task(self, task_id: int, target_tool: str = "generic",
                                    severity_filter: Optional[List[str]] = None,
                                    use_llm: bool = True,
                                    ctx: Optional[AgentContext] = None) -> AgentResult:
        if disabled := self._disabled_result("ai_prompt"):
            return disabled
        return self.ai_prompt.execute_for_task(
            task_id, target_tool, severity_filter, use_llm, ctx)

    def generate_ai_prompt_for_project(self, project_id: int, target_tool: str = "generic",
                                       top_n: int = 30, use_llm: bool = True,
                                       ctx: Optional[AgentContext] = None) -> AgentResult:
        if disabled := self._disabled_result("ai_prompt"):
            return disabled
        return self.ai_prompt.execute_for_project(
            project_id, target_tool, top_n, use_llm, ctx)

    def audit_security_for_file(self, file_id: int, scan_depth: str = "standard",
                                ctx: Optional[AgentContext] = None) -> AgentResult:
        if disabled := self._disabled_result("security_sentinel"):
            return disabled
        if self._db is None or self._user is None:
            return AgentResult(success=False, error="DB 或用户上下文未注入")
        if not check_permission(self._db, self._user.id, PermissionCode.SECURITY_SCAN):
            return AgentResult(success=False, error="当前用户没有 security:scan 权限")
        return self.security_sentinel.scan_file(file_id, scan_depth, ctx)

    def audit_security_for_task(self, task_id: int,
                                ctx: Optional[AgentContext] = None) -> AgentResult:
        if disabled := self._disabled_result("security_sentinel"):
            return disabled
        if self._db is None or self._user is None:
            return AgentResult(success=False, error="DB 或用户上下文未注入")
        if not check_permission(self._db, self._user.id, PermissionCode.SECURITY_SCAN):
            return AgentResult(success=False, error="当前用户没有 security:scan 权限")
        return self.security_sentinel.scan_task(task_id, ctx)

    def audit_security_for_project(self, project_id: int, top_n: int = 50,
                                   trace_dataflow: bool = True,
                                   ctx: Optional[AgentContext] = None,
                                   scan_mode: str = "static_full") -> AgentResult:
        if disabled := self._disabled_result("security_sentinel"):
            return disabled
        if self._db is None or self._user is None:
            return AgentResult(success=False, error="DB 或用户上下文未注入")
        if not check_permission(self._db, self._user.id, PermissionCode.SECURITY_SCAN):
            return AgentResult(success=False, error="当前用户没有 security:scan 权限")
        if scan_mode == "full":
            # 保持旧版注入式测试/扩展 Agent 的四参数兼容；SecuritySentinel
            # 默认 full，因此不需要额外传递新参数。
            return self.security_sentinel.scan_project(project_id, top_n, trace_dataflow, ctx)
        return self.security_sentinel.scan_project(
            project_id, top_n, trace_dataflow, ctx, scan_mode)

    def run_project_tests(self, *args, **kwargs) -> AgentResult:
        if disabled := self._disabled_result("test_verifier"):
            return disabled
        return self.test_verifier.run_project_tests(*args, **kwargs)

    def deploy_project_sandbox(self, *args, **kwargs) -> AgentResult:
        if disabled := self._disabled_result("sandbox_deployer"):
            return disabled
        return self.sandbox_deployer.deploy_project_sandbox(*args, **kwargs)

    def close_sandbox(self, *args, **kwargs) -> AgentResult:
        if disabled := self._disabled_result("sandbox_deployer"):
            return disabled
        return self.sandbox_deployer.close_sandbox(*args, **kwargs)

    def extend_sandbox(self, *args, **kwargs) -> AgentResult:
        if disabled := self._disabled_result("sandbox_deployer"):
            return disabled
        return self.sandbox_deployer.extend_sandbox(*args, **kwargs)

    # ── 管理员 AI 代管后台工具(仅管理员;写操作强制审批)──

    def _require_admin_db(self) -> tuple:
        """校验请求级 DB 与用户已注入,返回 (db, user)。"""
        if self._db is None or self._user is None:
            raise RuntimeError("管理员代管工具需要请求级 DB 与用户上下文")
        return self._db, self._user

    def admin_list_users(self, keyword: str = "", role: str = "", page: int = 1,
                         page_size: int = 20, ctx: Optional[AgentContext] = None) -> AgentResult:
        from app.services import admin_agent_tools
        db, user = self._require_admin_db()
        return admin_agent_tools.admin_list_users(db, user, keyword, role, page, page_size, ctx)

    def admin_list_roles(self, ctx: Optional[AgentContext] = None) -> AgentResult:
        from app.services import admin_agent_tools
        db, user = self._require_admin_db()
        return admin_agent_tools.admin_list_roles(db, user, ctx)

    def admin_governance_overview(self, ctx: Optional[AgentContext] = None) -> AgentResult:
        from app.services import admin_agent_tools
        db, user = self._require_admin_db()
        return admin_agent_tools.admin_governance_overview(db, user, ctx)

    def admin_list_agents(self, ctx: Optional[AgentContext] = None) -> AgentResult:
        from app.services import admin_agent_tools
        db, user = self._require_admin_db()
        return admin_agent_tools.admin_list_agents(db, user, ctx)

    def admin_list_approvals(self, status: str = "pending", ctx: Optional[AgentContext] = None) -> AgentResult:
        from app.services import admin_agent_tools
        db, user = self._require_admin_db()
        return admin_agent_tools.admin_list_approvals(db, user, status, ctx)

    def admin_list_agent_release_approvals(
        self,
        approval_id: Optional[int] = None,
        status: str = "pending",
        limit: int = 50,
        ctx: Optional[AgentContext] = None,
    ) -> AgentResult:
        from app.services import admin_agent_tools
        db, user = self._require_admin_db()
        return admin_agent_tools.admin_list_agent_release_approvals(
            db,
            user,
            approval_id=approval_id,
            status=status,
            limit=limit,
            ctx=ctx,
        )

    def admin_system_status(self, ctx: Optional[AgentContext] = None) -> AgentResult:
        from app.services import admin_agent_tools
        db, user = self._require_admin_db()
        return admin_agent_tools.admin_system_status(db, user, ctx)

    def admin_set_user_role(self, user_id: int, role: str, ctx: Optional[AgentContext] = None) -> AgentResult:
        from app.services import admin_agent_tools
        db, user = self._require_admin_db()
        return admin_agent_tools.admin_set_user_role(db, user, user_id, role, ctx)

    def admin_delete_user(self, user_id: int, ctx: Optional[AgentContext] = None) -> AgentResult:
        from app.services import admin_agent_tools
        db, user = self._require_admin_db()
        return admin_agent_tools.admin_delete_user(db, user, user_id, ctx)

    def admin_delete_users(
        self,
        user_ids: List[int],
        ctx: Optional[AgentContext] = None,
    ) -> AgentResult:
        del user_ids, ctx
        return AgentResult(success=False, error="批量删除只能通过 Responses 审批执行器调用")

    def admin_toggle_agent(self, agent_code: str, enable: bool, ctx: Optional[AgentContext] = None) -> AgentResult:
        from app.services import admin_agent_tools
        db, user = self._require_admin_db()
        return admin_agent_tools.admin_toggle_agent(db, user, agent_code, enable, ctx)

    def admin_decide_agent_release(
        self,
        approval_id: int,
        decision: str,
        note: str = "",
        ctx: Optional[AgentContext] = None,
    ) -> AgentResult:
        del approval_id, decision, note, ctx
        return AgentResult(success=False, error="发布审批决策只能通过 Responses 审批执行器调用")

    def search_published_agents(
        self,
        query: str = "",
        limit: int = 8,
        ctx: Optional[AgentContext] = None,
    ) -> AgentResult:
        del ctx
        from app.services import published_agent_tools
        db, user = self._require_admin_db()
        try:
            return AgentResult(
                success=True,
                data=published_agent_tools.search_published_agents(
                    db,
                    user,
                    query=query,
                    limit=limit,
                ),
            )
        except Exception as exc:
            return AgentResult(success=False, error=str(exc))

    def invoke_published_agent(
        self,
        agent_code: str,
        code: str,
        language: str = "plaintext",
        file_name: str = "snippet.txt",
        rules: Optional[List[Dict[str, Any]]] = None,
        line_offset: int = 0,
        experience: str = "",
        ctx: Optional[AgentContext] = None,
    ) -> AgentResult:
        del ctx
        from app.services import published_agent_tools
        db, user = self._require_admin_db()
        try:
            return AgentResult(
                success=True,
                data=published_agent_tools.invoke_published_agent(
                    db,
                    user,
                    agent_code=agent_code,
                    code=code,
                    language=language,
                    file_name=file_name,
                    rules=rules or [],
                    line_offset=line_offset,
                    experience=experience,
                ),
            )
        except Exception as exc:
            return AgentResult(success=False, error=str(exc))

    def chat(self, messages: List[dict],
             ctx: Optional[AgentContext] = None) -> AgentResult:
        """v2.0: 主控分发聊天调用,生成 trace_id 并广播 DISPATCH 事件"""
        if disabled := self._disabled_result("chat_assistant"):
            return disabled
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
        """列出全局注册中心中的 Agent 元数据。"""
        return self._registry.list()

    def get_agent(self, name: str):
        return self._registry.get(name)

    # ── AgentSkill 自进化与总调度升级:通用工具入口 ──

    def invoke_tool(
        self,
        tool_name: str,
        arguments: Dict[str, Any],
        ctx: Optional[AgentContext] = None,
    ) -> AgentResult:
        """通用工具调用入口(双层调度执行器使用)

        支持:
        - tool_name 形如 "code_reviewer.self_improve" → 调 SkillRegistry 中的 Skill
        - tool_name 形如 "list_projects" / "start_review" → 调 Orchestrator 固定方法

        Args:
            tool_name: 工具名(Skill name 或固定方法名)
            arguments: 工具参数 dict
            ctx: 上下文

        Returns:
            AgentResult: 调用结果
        """
        # 1. 优先在 SkillRegistry 中查找(所有已注册 Skill)
        from app.agents.skills.registry import SkillRegistry

        for skill in SkillRegistry.instance().list_all():
            if skill.name == tool_name:
                return self.invoke_skill(
                    skill.agent_name, tool_name, arguments, ctx
                )

        if tool_name == "trigger_evolution" and not self._can_configure_agents():
            return AgentResult(success=False, error="当前用户缺少 Agent 配置权限")

        # 2. 固定工具只允许调用 tool_contracts 注册表中的显式白名单。
        if not is_fixed_tool(tool_name):
            return AgentResult(
                success=False,
                error=f"工具 {tool_name} 不存在(既非 Skill 也非固定工具)",
            )

        try:
            validated_arguments = validate_fixed_tool_arguments(tool_name, arguments)
        except FixedToolArgumentError as exc:
            return AgentResult(
                success=False,
                data={"tool_name": tool_name, "validation_errors": exc.issues},
                error=str(exc),
            )

        method = getattr(self, tool_name, None)
        if method is None or not callable(method):
            return AgentResult(
                success=False,
                error=f"固定工具 {tool_name} 未实现或不可调用",
            )

        if fixed_tool_accepts_ctx(tool_name):
            validated_arguments["ctx"] = ctx

        try:
            result = method(**validated_arguments)
        except Exception as e:
            # 不再把 handler 内部 TypeError 误判为签名问题并重试，避免重复副作用。
            logger.exception(f"[Orchestrator] 固定工具 {tool_name} 调用异常")
            return AgentResult(success=False, error=f"固定工具 {tool_name} 调用异常: {e}")

        if isinstance(result, AgentResult):
            return result
        return AgentResult(success=True, data=result)

    def invoke_skill(
        self,
        agent_name: str,
        skill_name: str,
        params: Dict[str, Any],
        ctx: Optional[AgentContext] = None,
        trigger_type: str = "orchestrator",
        trigger_source: str = "",
    ) -> AgentResult:
        """调用指定 Agent 的指定 Skill

        通过 skill_service 统一入口调用,自动写 agent_skill_record 与 audit_log。

        Args:
            agent_name: Agent name(如 code_reviewer)
            skill_name: Skill name(如 code_reviewer.self_improve)
            params: Skill 参数
            ctx: 上下文
            trigger_type: 触发类型(manual/scheduled/event/proactive/orchestrator),
                默认 "orchestrator"(ChatPlanner 调用),其他场景由调用方显式传入
            trigger_source: 触发来源描述(如 "api:POST /agents/.../invoke" /
                "event:REVIEW_ISSUE_STATUS_CHANGED" / "scheduler:cron:0 3 * * *"),
                留空则按 trigger_type 自动生成默认值

        Returns:
            AgentResult: data 为 skill_service 返回的 dict
        """
        if not self._db:
            return AgentResult(
                success=False,
                error="DB 未注入,无法调用 Skill(请使用 get_request_orchestrator)",
            )
        if trigger_type not in {"scheduled", "event", "proactive"} and not self._can_configure_agents():
            return AgentResult(success=False, error="当前用户缺少 Agent 配置权限")
        from app.services import skill_service

        # trigger_source 默认值
        if not trigger_source:
            trigger_source = f"orchestrator.invoke_skill:{trigger_type}"

        user = getattr(self, "_user", None)
        result = skill_service.invoke_skill_with_record(
            db=self._db,
            agent_name=agent_name,
            skill_name=skill_name,
            params=params,
            trigger_type=trigger_type,
            trigger_source=trigger_source,
            user=user,
            ctx=ctx,
        )
        return AgentResult(
            success=result["success"],
            data=result,
            error=result["error"],
            duration_ms=result["duration_ms"],
        )

    def _can_configure_agents(self) -> bool:
        """判断请求用户是否拥有全局 Agent 配置权限。"""

        if self._db is None or self._user is None:
            return False
        return check_permission(self._db, self._user.id, PermissionCode.AGENT_CONFIGURE)

    def list_agent_skills(self, agent_name: Optional[str] = None) -> List[Dict[str, Any]]:
        """列出 Agent 挂载的所有 Skill 元数据

        Args:
            agent_name: 可选 Agent name；为空时返回全部 Skill

        Returns:
            list[dict]: Skill 元数据列表
                [{"name", "description", "type", "invocable", "agent_name"}]
        """
        from app.agents.skills.registry import SkillRegistry

        return SkillRegistry.instance().list_meta(agent_name)

    def trigger_evolution(
        self,
        agent_name: str = "evolution",
        window_days: int = 90,
        ctx: Optional[AgentContext] = None,
    ) -> AgentResult:
        """触发指定 Agent 的自进化

        调用 invoke_skill(agent_name, "{agent_name}.self_improve", {"action": "evolve", ...})。

        Args:
            agent_name: Agent name(默认 evolution)
            window_days: 反馈窗口天数
            ctx: 上下文

        Returns:
            AgentResult: 自进化执行结果
        """
        skill_name = f"{agent_name}.self_improve"
        logger.info(
            f"[Orchestrator] 触发自进化: agent={agent_name} window={window_days}d"
        )
        return self.invoke_skill(
            agent_name,
            skill_name,
            {"action": "evolve", "window_days": window_days},
            ctx,
        )


_orchestrator: Optional[Orchestrator] = None


def get_orchestrator() -> Orchestrator:
    """返回进程级"元数据"单例 — 只用于只读元数据(如 /api/agents 列表)。

    ⚠️ 不要在请求处理里对它调用 inject_db():该实例被所有请求共享,
    注入请求级 db/user 会被并发请求互相覆盖,造成跨用户数据/身份串号。
    需要执行带 db 的 Agent 操作时,请用 get_request_orchestrator()。
    """
    global _orchestrator
    if _orchestrator is None:
        _orchestrator = Orchestrator()
    return _orchestrator


def get_request_orchestrator(db: Session, user: Optional[User] = None) -> Orchestrator:
    """为单个请求构造独立的 Orchestrator,并注入该请求的 db/user。

    每个请求拿到全新的 Agent 实例,彼此隔离,从根上消除共享单例的并发串号问题。
    构造成本很低(仅属性赋值,无网络/IO),远小于一次 LLM 调用。
    """
    orch = Orchestrator(register=False)
    orch.inject_db(db, user=user)
    return orch
