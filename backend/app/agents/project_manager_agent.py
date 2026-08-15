"""
项目管理 Agent — 负责项目的创建/查询/更新/删除
"""
from dataclasses import dataclass
from typing import Optional

from loguru import logger
from sqlalchemy.orm import Session

from app.agents.base import AgentContext, AgentResult, BaseAgent
from app.models.project import Project
from app.schemas.project import ProjectIn
from app.services import project_service


@dataclass
class ProjectOps:
    db: Session


class ProjectManagerAgent(BaseAgent):
    """项目管理 Agent

    通过 Orchestrator 注入依赖后, 执行真实数据库操作。
    """

    name = "project_manager"
    description = "项目管理员:创建/修改/删除项目,支持直接导入 GitHub 仓库"
    icon = "project_manager"
    color = "#9F7AEA"
    category = "manager"
    skills = ("项目创建", "项目查询", "项目编辑", "项目删除")

    def __init__(self):
        super().__init__(temperature=0.1, max_tokens=256)
        self._ops: Optional[ProjectOps] = None

    def _init_skills(self) -> None:
        """子类 override:挂载 ProjectManagerSelfImprovementSkill + ProjectManagerProactiveSkill

        将项目管理 Agent 的自进化与主动监测能力下沉到 Skill,通过 SkillRegistry
        统一注册,供 Orchestrator.invoke_skill / ChatPlanner 查询调用。
        """
        from app.agents.skills.project_manager import (
            ProjectManagerProactiveSkill,
            ProjectManagerSelfImprovementSkill,
        )

        self.attach_skill(ProjectManagerSelfImprovementSkill(self.name))
        self.attach_skill(ProjectManagerProactiveSkill(self.name))

    def inject(self, db: Session, user=None) -> None:
        self._ops = ProjectOps(db=db)
        self._user = user

    def create_project(self, project_name: str, description: str = "",
                       language: str = "plaintext",
                       ctx: Optional[AgentContext] = None) -> AgentResult:
        if not self._ops:
            return AgentResult(success=False, error="DB 未注入")
        try:
            payload = ProjectIn(
                project_name=project_name[:50],
                description=description[:200] if description else None,
                language=language if language else None,
            )
            project = project_service.create_project(
                self._ops.db, user=self._user, payload=payload,
            )
            return AgentResult(success=True, data={
                "id": project.id,
                "project_name": project.project_name,
                "language": project.language,
                "status": project.status,
            })
        except Exception as e:
            logger.warning(f"[project_manager] 创建失败: {e}")
            return AgentResult(success=False, error=str(e))

    def list_projects(self, keyword: str = "", language: str = "",
                      status: str = "active", page: int = 1,
                      page_size: int = 20,
                      ctx: Optional[AgentContext] = None) -> AgentResult:
        if not self._ops:
            return AgentResult(success=False, error="DB 未注入")
        result = project_service.list_projects(
            self._ops.db, user=self._user, keyword=keyword,
            language=language, status=status, page=page, page_size=page_size,
        )
        is_admin = bool(self._user and self._user.role in {"admin", "super_admin"})
        items = []
        for i in result["items"]:
            if is_admin:
                own = i["id"] in {
                    row[0]
                    for row in self._ops.db.query(Project.id)
                    .filter(Project.user_id == self._user.id, Project.status != "deleted")
                    .all()
                }
                can_write = own
            else:
                can_write = bool(i.get("can_update", False) or i.get("can_delete", False))
            items.append({
                "id": i["id"], "project_name": i["project_name"],
                "language": i.get("language"), "status": i["status"],
                "file_count": i["file_count"],
                "can_update": can_write,
                "can_delete": can_write,
            })
        return AgentResult(success=True, data={
            "total": result["total"], "items": items,
        })

    def get_project(self, project_id: int,
                    ctx: Optional[AgentContext] = None) -> AgentResult:
        if not self._ops:
            return AgentResult(success=False, error="DB 未注入")
        try:
            detail = project_service.get_project(
                self._ops.db, user=self._user, project_id=project_id)
            return AgentResult(success=True, data=detail)
        except Exception as e:
            return AgentResult(success=False, error=str(e))

    def _assert_writable_own_project(self, project_id: int) -> Optional[str]:
        """管理员在对话中只能写自己拥有的项目，用户项目只读。

        系统级 RBAC 允许 admin 全量写用于后台监管；但普通对话 Agent 的写操作
        收敛为「仅自有项目」，避免把用户项目当成管理员的项目操作。非 admin
        用户仍按 project_service 的 owner/member 权限判断。

        Args:
            project_id: 目标项目ID。

        Returns:
            Optional[str]: 权限不足时的错误消息，否则返回 None。
        """
        if self._ops is None or self._user is None:
            return "DB 或用户上下文未注入"
        if self._user.role not in {"admin", "super_admin"}:
            return None
        project = self._ops.db.get(Project, project_id)
        if project is None or project.status == "deleted":
            return f"项目 #{project_id} 不存在"
        if project.user_id != self._user.id:
            return (
                f"项目 #{project_id}「{project.project_name}」不是管理员自有项目，"
                "管理员对话中仅可修改自己拥有的项目；其他用户的项目只读。"
            )
        return None

    def delete_project(self, project_id: int,
                       ctx: Optional[AgentContext] = None) -> AgentResult:
        if not self._ops:
            return AgentResult(success=False, error="DB 未注入")
        denied = self._assert_writable_own_project(project_id)
        if denied:
            return AgentResult(success=False, error=denied)
        try:
            project_service.delete_project(
                self._ops.db, user=self._user, project_id=project_id)
            return AgentResult(success=True, data={"deleted": project_id})
        except Exception as e:
            return AgentResult(success=False, error=str(e))
