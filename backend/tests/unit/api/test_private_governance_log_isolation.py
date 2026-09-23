"""治理日志保留运营元数据，不通过管理员页面暴露他人的私人输入输出。"""

import json
from types import SimpleNamespace

import pytest

from app.api.v1 import agent_governance
from app.models.agent_governance import PolicyDecisionLog, ToolCallLog
from app.models.agent_response_run import AgentResponseRun, AgentToolExecution


def _rows(db, *, owner=8, anchored=True, agent="chat_assistant"):
    if anchored:
        db.add(AgentResponseRun(run_id="private-run", user_id=owner, surface="user", session_key="s",
                                status="completed", checkpoint_json="{}"))
        db.add(AgentToolExecution(request_id="private-request", run_id="private-run", call_id="call",
                                  user_id=owner, tool_name="user_execute_capability", status="success",
                                  arguments_json="{}"))
    policy = PolicyDecisionLog(subject=f"agent:{agent}", action="user.analysis", resource="PRIVATE-RESOURCE",
                               decision="allow", risk_level="low", risk_score=0,
                               reason="PRIVATE-REASON", context_json=json.dumps({
                                   "copilot_request_id": "private-request", "owner_user_id": 7,
                                   "private": "PRIVATE-CONTEXT",
                               }))
    db.add(policy)
    db.flush()
    tool = ToolCallLog(agent_code=agent, tool_code="user_execute_capability", action="user.analysis",
                       resource="PRIVATE-RESOURCE", status="success", risk_level="low", decision="allow",
                       input_summary="PRIVATE-INPUT", output_summary="PRIVATE-OUTPUT", error="PRIVATE-ERROR",
                       policy_decision_id=policy.id, copilot_request_id="private-request")
    db.add(tool)
    db.commit()
    return policy, tool


@pytest.mark.parametrize("endpoint", ["policy", "tool"])
@pytest.mark.parametrize("anchored", [True, False])
@pytest.mark.parametrize("agent", ["chat_assistant", "custom_specialist"])
def test_governance_private_logs_require_persisted_owner_not_context_claim(db, endpoint, anchored, agent):
    _rows(db, anchored=anchored, agent=agent)
    actor = SimpleNamespace(id=7, role="super_admin")
    response = (agent_governance.list_policy_decisions if endpoint == "policy"
                else agent_governance.list_tool_calls)(db, actor)
    data = [row.model_dump() for row in response.data]
    assert len(data) == 1
    assert "PRIVATE-" not in str(data)
    assert data[0]["content_redacted"] is True
    assert data[0]["decision"] == "allow"


@pytest.mark.parametrize("endpoint", ["policy", "tool"])
def test_governance_owner_keeps_private_logs_and_projection_does_not_change_rows(db, endpoint):
    policy, tool = _rows(db, owner=7)
    actor = SimpleNamespace(id=7, role="admin")
    response = (agent_governance.list_policy_decisions if endpoint == "policy"
                else agent_governance.list_tool_calls)(db, actor)
    data = [row.model_dump() for row in response.data]
    assert "PRIVATE-" in str(data)
    db.expire_all()
    assert db.get(PolicyDecisionLog, policy.id).reason == "PRIVATE-REASON"
    assert db.get(ToolCallLog, tool.id).input_summary == "PRIVATE-INPUT"


def test_nonprivate_business_governance_metadata_and_detail_remain_visible(db):
    db.add(PolicyDecisionLog(subject="agent:code_reviewer", action="review.start", resource="project:5",
                             decision="allow", risk_level="low", risk_score=0, reason="BUSINESS-POLICY",
                             context_json="{}"))
    db.add(ToolCallLog(agent_code="code_reviewer", tool_code="review", action="review.start",
                       resource="project:5", status="success", input_summary="BUSINESS-INPUT"))
    db.commit()
    actor = SimpleNamespace(id=7, role="admin")
    assert "BUSINESS-POLICY" in str(agent_governance.list_policy_decisions(db, actor).data)
    assert "BUSINESS-INPUT" in str(agent_governance.list_tool_calls(db, actor).data)
