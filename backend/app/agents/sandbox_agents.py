"""沙箱测试与持续部署专用 Agent。"""

from __future__ import annotations

from typing import Optional

from sqlalchemy.orm import Session

from app.agents.base import AgentContext, AgentResult, BaseAgent
from app.models.user import User
from app.services import sandbox_service


class _SandboxAgent(BaseAgent):
    """共享请求级依赖注入，所有执行仍由 sandbox_service 重新校验。"""

    def __init__(self) -> None:
        super().__init__(temperature=0.1, max_tokens=256)
        self._db: Optional[Session] = None
        self._user: Optional[User] = None

    def inject(self, db: Session, user: Optional[User] = None) -> None:
        self._db = db
        self._user = user

    def _context(self) -> tuple[Session, User]:
        if self._db is None or self._user is None:
            raise RuntimeError("沙箱 Agent 未注入请求级 DB 和用户")
        return self._db, self._user


class TestVerifierAgent(_SandboxAgent):
    name = "test_verifier"
    description = "调用隔离 worker 执行项目级动态白盒、黑盒或组合测试"
    icon = "test_verifier"
    color = "#16866F"
    category = "review"
    skills = ("项目动态白盒测试", "授权远程黑盒测试", "原始证据归档")

    def run_project_tests(
        self,
        project_id: int,
        language: str,
        test_mode: str = "whitebox",
        worker_code: str = "",
        remote_target_url: str = "",
        remote_target_authorized: bool = False,
        ctx: Optional[AgentContext] = None,
    ) -> AgentResult:
        del ctx
        try:
            db, user = self._context()
            row = sandbox_service.create_environment(db, user, {
                "project_id": project_id,
                "purpose": "test",
                "language": language,
                "test_mode": test_mode,
                "worker_code": worker_code,
                "remote_target_url": remote_target_url or None,
                "remote_target_authorized": remote_target_authorized,
                "ttl_hours": 24,
            })
            return AgentResult(success=True, data=sandbox_service.environment_to_dict(db, row))
        except Exception as exc:
            return AgentResult(success=False, error=str(exc))


class SandboxDeployerAgent(_SandboxAgent):
    name = "sandbox_deployer"
    description = "在隔离 worker 部署项目并管理预览、续期和关闭生命周期"
    icon = "sandbox_deployer"
    color = "#C87823"
    category = "operations"
    skills = ("沙箱项目部署", "健康验证", "环境续期", "到期回收")

    def deploy_project_sandbox(
        self,
        project_id: int,
        language: str,
        ttl_hours: int = 72,
        worker_code: str = "",
        ctx: Optional[AgentContext] = None,
    ) -> AgentResult:
        del ctx
        try:
            db, user = self._context()
            row = sandbox_service.create_environment(db, user, {
                "project_id": project_id,
                "purpose": "deploy",
                "language": language,
                "test_mode": "deploy",
                "worker_code": worker_code,
                "ttl_hours": ttl_hours,
            })
            return AgentResult(success=True, data=sandbox_service.environment_to_dict(db, row))
        except Exception as exc:
            return AgentResult(success=False, error=str(exc))

    def close_sandbox(self, public_id: str, ctx: Optional[AgentContext] = None) -> AgentResult:
        del ctx
        try:
            db, user = self._context()
            return AgentResult(success=True, data=sandbox_service.stop_environment(db, user, public_id))
        except Exception as exc:
            return AgentResult(success=False, error=str(exc))

    def extend_sandbox(self, public_id: str, hours: int, ctx: Optional[AgentContext] = None) -> AgentResult:
        del ctx
        try:
            db, user = self._context()
            return AgentResult(success=True, data=sandbox_service.extend_environment(db, user, public_id, hours))
        except Exception as exc:
            return AgentResult(success=False, error=str(exc))
