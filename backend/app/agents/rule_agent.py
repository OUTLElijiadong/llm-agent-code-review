"""
审查规则 Agent — 负责规则的列表/创建/切换
"""
from typing import Optional

from sqlalchemy.orm import Session

from app.agents.base import AgentContext, AgentResult, BaseAgent
from app.services import rule_service


class RuleManagerAgent(BaseAgent):
    """审查规则 Agent"""

    name = "rule_manager"
    description = "管理审查规则: 列出/创建/启用/禁用"
    icon = "rule_manager"
    color = "#264653"
    category = "manager"
    skills = ("规则列表", "规则配置", "启停控制")

    def __init__(self):
        super().__init__(temperature=0.1, max_tokens=256)
        self._db: Optional[Session] = None
        self._user = None

    def inject(self, db: Session, user=None) -> None:
        self._db = db
        self._user = user

    def list_rules(self, ctx: Optional[AgentContext] = None) -> AgentResult:
        if not self._db:
            return AgentResult(success=False, error="DB 未注入")
        uid = self._user.id if self._user else 1
        rules = rule_service.list_rules(self._db, user_id=uid)
        items = [
            {"id": r.id, "rule_name": r.rule_name, "rule_type": r.rule_type,
             "enabled": bool(r.enabled), "is_builtin": bool(r.is_builtin)}
            for r in rules
        ]
        return AgentResult(success=True, data={"total": len(items), "items": items})

    def toggle_rule(self, rule_id: int, enabled: bool,
                    ctx: Optional[AgentContext] = None) -> AgentResult:
        if not self._db:
            return AgentResult(success=False, error="DB 未注入")
        try:
            rule_service.toggle_rule(self._db, rule_id, 1 if enabled else 0)
            return AgentResult(success=True, data={"rule_id": rule_id, "enabled": enabled})
        except Exception as e:
            return AgentResult(success=False, error=str(e))
