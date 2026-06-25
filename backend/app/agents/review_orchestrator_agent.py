"""
代码审查调度 Agent — 负责启动审查/查询任务/列出问题
"""
from typing import List, Optional

from loguru import logger
from sqlalchemy.orm import Session

from app.agents.base import AgentContext, AgentResult, BaseAgent
from app.models.user import User
from app.schemas.review import ReviewStartIn
from app.services import review_service


class ReviewOrchestratorAgent(BaseAgent):
    """审查调度 Agent — 启动审查并监控进度"""

    name = "review_orchestrator"
    description = "启动代码审查任务/查询审查记录/列出审查问题"
    icon = "review_orchestrator"
    color = "#F4A261"
    category = "orchestrator"
    skills = ("启动审查", "审查进度跟踪", "问题汇总")

    def __init__(self):
        super().__init__(temperature=0.1, max_tokens=256)
        self._db: Optional[Session] = None
        self._user = None

    def _init_skills(self) -> None:
        """子类 override:挂载 ReviewOrchestratorSelfImprovementSkill + ReviewOrchestratorProactiveSkill

        将审查调度 Agent 的自进化与主动监测能力下沉到 Skill,通过 SkillRegistry
        统一注册,供 Orchestrator.invoke_skill / ChatPlanner 查询调用。
        """
        from app.agents.skills.review_orchestrator import (
            ReviewOrchestratorProactiveSkill,
            ReviewOrchestratorSelfImprovementSkill,
        )

        self.attach_skill(ReviewOrchestratorSelfImprovementSkill(self.name))
        self.attach_skill(ReviewOrchestratorProactiveSkill(self.name))

    def inject(self, db: Session, user=None) -> None:
        self._db = db
        self._user = user

    def start_review(self, project_id: int, file_ids: List[int],
                     review_type: str = "quick", task_name: str = "",
                     user: Optional[User] = None,
                     ctx: Optional[AgentContext] = None) -> AgentResult:
        if not self._db:
            return AgentResult(success=False, error="DB 未注入")
        try:
            payload = ReviewStartIn(
                project_id=project_id, file_ids=file_ids,
                review_type=review_type, task_name=task_name or None,
            )
            task = review_service.start(self._db, user=user or self._user, payload=payload)
            return AgentResult(success=True, data={
                "task_id": task.id, "status": task.status,
                "total_files": task.total_files or len(file_ids),
            })
        except Exception as e:
            logger.warning(f"[review_orchestrator] 启动失败: {e}")
            return AgentResult(success=False, error=str(e))

    def list_tasks(self, project_id: Optional[int] = None,
                   status: str = "", page: int = 1, page_size: int = 20,
                   ctx: Optional[AgentContext] = None) -> AgentResult:
        if not self._db:
            return AgentResult(success=False, error="DB 未注入")
        result = review_service.list_tasks(
            self._db, user=self._user, project_id=project_id, status=status,
            page=page, page_size=page_size,
        )
        items = [
            {"id": i["id"], "task_name": i.get("task_name", ""),
             "review_type": i["review_type"], "status": i["status"],
             "score": i["score"], "total_issues": i["total_issues"],
             "duration_ms": i.get("duration_ms", 0)}
            for i in result["items"]
        ]
        return AgentResult(success=True, data={
            "total": result["total"], "items": items,
        })

    def get_task_detail(self, task_id: int,
                        ctx: Optional[AgentContext] = None) -> AgentResult:
        if not self._db:
            return AgentResult(success=False, error="DB 未注入")
        try:
            data = review_service.get_task_detail(self._db, user=self._user, task_id=task_id)
            return AgentResult(success=True, data=data)
        except Exception as e:
            return AgentResult(success=False, error=str(e))

    def list_issues(self, task_id: int, severity: str = "",
                    issue_type: str = "", page: int = 1, page_size: int = 50,
                    ctx: Optional[AgentContext] = None) -> AgentResult:
        if not self._db:
            return AgentResult(success=False, error="DB 未注入")
        result = review_service.list_task_issues(
            self._db, user=self._user, task_id=task_id, severity=severity,
            issue_type=issue_type, page=page, page_size=page_size,
        )
        items = [
            {"id": i["id"], "file_name": i.get("file_name", ""),
             "line_number": i["line_number"], "severity": i["severity"],
             "issue_type": i["issue_type"], "title": i.get("title", ""),
             "status": i["status"]}
            for i in result["items"]
        ]
        return AgentResult(success=True, data={
            "total": result["total"], "items": items,
        })
