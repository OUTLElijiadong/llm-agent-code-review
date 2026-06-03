"""
项目管理 Agent — 负责项目的创建/查询/更新/删除
"""
from dataclasses import dataclass
from typing import Optional

from loguru import logger
from sqlalchemy.orm import Session

from app.agents.base import AgentContext, AgentResult, BaseAgent
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
    description = "管理项目: 创建/查询/编辑/删除项目"
    icon = "project_manager"
    color = "#9F7AEA"
    category = "manager"
    skills = ("项目创建", "项目查询", "项目编辑", "项目删除")

    def __init__(self):
        super().__init__(temperature=0.1, max_tokens=256)
        self._ops: Optional[ProjectOps] = None

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
        items = [
            {"id": i["id"], "project_name": i["project_name"],
             "language": i.get("language"), "status": i["status"],
             "file_count": i["file_count"]}
            for i in result["items"]
        ]
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

    def delete_project(self, project_id: int,
                       ctx: Optional[AgentContext] = None) -> AgentResult:
        if not self._ops:
            return AgentResult(success=False, error="DB 未注入")
        try:
            project_service.delete_project(
                self._ops.db, user=self._user, project_id=project_id)
            return AgentResult(success=True, data={"deleted": project_id})
        except Exception as e:
            return AgentResult(success=False, error=str(e))
