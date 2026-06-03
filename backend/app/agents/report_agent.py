"""
报告 Agent — 负责审查报告的查询
"""
from typing import Optional

from sqlalchemy.orm import Session

from app.agents.base import AgentContext, AgentResult, BaseAgent
from app.services import report_service


class ReportAgent(BaseAgent):
    """报告 Agent"""

    name = "reporter"
    description = "查询审查报告列表和详情"
    icon = "reporter"
    color = "#D9A857"
    category = "output"
    skills = ("报告查询", "报告导出", "报告汇总")

    def __init__(self):
        super().__init__(temperature=0.1, max_tokens=256)
        self._db: Optional[Session] = None
        self._user = None

    def inject(self, db: Session, user=None) -> None:
        self._db = db
        self._user = user

    def list_reports(self, project_id: Optional[int] = None,
                     page: int = 1, page_size: int = 20,
                     ctx: Optional[AgentContext] = None) -> AgentResult:
        if not self._db:
            return AgentResult(success=False, error="DB 未注入")
        result = report_service.list_reports(
            self._db, user=self._user, project_id=project_id,
            page=page, page_size=page_size,
        )
        return AgentResult(success=True, data={
            "total": result["total"],
            "items": result["items"],
        })

    def get_report_detail(self, task_id: int,
                          ctx: Optional[AgentContext] = None) -> AgentResult:
        if not self._db:
            return AgentResult(success=False, error="DB 未注入")
        try:
            data = report_service.get_report_detail(
                self._db, user=self._user, task_id=task_id)
            return AgentResult(success=True, data=data)
        except Exception as e:
            return AgentResult(success=False, error=str(e))
