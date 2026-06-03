"""
仪表盘 Agent — 负责获取平台统计数据
"""
from typing import Optional

from sqlalchemy.orm import Session

from app.agents.base import AgentContext, AgentResult, BaseAgent
from app.services import dashboard_service


class DashboardAgent(BaseAgent):
    """仪表盘 Agent"""

    name = "dashboard"
    description = "获取平台统计数据: 汇总指标/风险分布/评分趋势/审查频次"
    icon = "dashboard"
    color = "#2A9D8F"
    category = "analytics"
    skills = ("仪表盘汇总", "风险分布", "评分趋势", "频次统计")

    def __init__(self):
        super().__init__(temperature=0.1, max_tokens=256)
        self._db: Optional[Session] = None
        self._user: Optional[object] = None

    def inject(self, db: Session, user=None) -> None:
        self._db = db
        self._user = user

    def _ensure_user(self):
        from app.models.user import User
        if self._user is None:
            self._user = User(id=1, role="admin", status=1)
        return self._user

    def summary(self, ctx: Optional[AgentContext] = None) -> AgentResult:
        if not self._db:
            return AgentResult(success=False, error="DB 未注入")
        data = dashboard_service.get_summary(self._db, user=self._ensure_user())
        return AgentResult(success=True, data=data)

    def risk_distribution(self, days: int = 30,
                          ctx: Optional[AgentContext] = None) -> AgentResult:
        if not self._db:
            return AgentResult(success=False, error="DB 未注入")
        data = dashboard_service.get_risk_distribution(self._db, user=self._ensure_user(), days=days)
        return AgentResult(success=True, data=data)

    def score_trend(self, limit: int = 10,
                    ctx: Optional[AgentContext] = None) -> AgentResult:
        if not self._db:
            return AgentResult(success=False, error="DB 未注入")
        data = dashboard_service.get_score_trend(self._db, user=self._ensure_user(), limit=limit)
        return AgentResult(success=True, data=data)
