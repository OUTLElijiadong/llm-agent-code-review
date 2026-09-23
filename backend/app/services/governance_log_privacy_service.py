"""治理日志中的私人内容按持久化运行归属投影，不信任上下文自报 user_id。"""

import json

from sqlalchemy.orm import Session

from app.models.admin_chat import OpsExecution
from app.models.agent_governance import PolicyDecisionLog, ToolCallLog
from app.models.agent_response_run import AgentResponseRun, AgentToolExecution
from app.models.user import User
from app.schemas.agent_governance import PolicyDecisionOut, ToolCallLogOut

_CHAT_AGENTS = {"chat_assistant", "manager", "admin_copilot"}
_PRIVATE_CONTEXT_KEYS = {"copilot_request_id", "run_id", "session_id", "session_key", "surface"}


def _context(row: PolicyDecisionLog | None) -> dict:
    try:
        value = json.loads(row.context_json or "{}") if row is not None else {}
    except (ValueError, TypeError):
        return {}
    return value if isinstance(value, dict) else {}


def _owner(db: Session, context: dict, request_id: str = "") -> int | None:
    """只有现存执行/运行账本可以证明归属；冲突或失联记录一律不回传原文。"""
    owners = set()
    request_ids = {value for value in (request_id, context.get("copilot_request_id"))
                   if isinstance(value, str) and value}
    for key in request_ids:
        execution = db.query(AgentToolExecution).filter(AgentToolExecution.request_id == key).first()
        operation = db.query(OpsExecution).filter(OpsExecution.request_id == key).first()
        for value in (execution.user_id if execution else None, operation.actor_id if operation else None):
            if isinstance(value, int) and not isinstance(value, bool) and value > 0:
                owners.add(value)
    run_id = context.get("run_id")
    if isinstance(run_id, str) and run_id:
        run = db.query(AgentResponseRun).filter(AgentResponseRun.run_id == run_id).first()
        if run is not None:
            owners.add(int(run.user_id))
    return next(iter(owners)) if len(owners) == 1 else None


def policy_log_view(db: Session, viewer: User, row: PolicyDecisionLog) -> PolicyDecisionOut:
    context = _context(row)
    is_private = (str(row.subject or "").removeprefix("agent:") in _CHAT_AGENTS
                  or bool(_PRIVATE_CONTEXT_KEYS.intersection(context)))
    value = PolicyDecisionOut.model_validate(row)
    if is_private and _owner(db, context) != int(viewer.id):
        return value.model_copy(update={
            "context_json": None, "reason": None, "resource": "[按账号隔离]", "content_redacted": True,
        })
    return value


def tool_log_view(db: Session, viewer: User, row: ToolCallLog) -> ToolCallLogOut:
    policy = db.get(PolicyDecisionLog, row.policy_decision_id) if row.policy_decision_id else None
    context = _context(policy)
    is_private = (row.agent_code in _CHAT_AGENTS or bool(row.copilot_request_id)
                  or bool(_PRIVATE_CONTEXT_KEYS.intersection(context)))
    value = ToolCallLogOut.model_validate(row)
    if is_private and _owner(db, context, row.copilot_request_id or "") != int(viewer.id):
        return value.model_copy(update={
            "input_summary": None, "output_summary": None, "error": None,
            "resource": "[按账号隔离]", "content_redacted": True,
        })
    return value
