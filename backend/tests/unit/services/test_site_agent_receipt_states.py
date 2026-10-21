"""业务状态畸形时仍保留脱敏回执和幂等账本，不泄漏解析异常。"""

import asyncio
import json

import pytest

from app.models.agent_response_run import AgentToolExecution
from app.models.user import User
from app.services import agent_team_service, site_agent_service


@pytest.mark.parametrize("field,value", [
    ("status", {"value": "completed"}), ("status", ["completed"]),
    ("execution_status", {"value": "running"}), ("execution_status", ["failed"]),
    ("agent.status", {"value": "completed"}), ("agent.status", ["completed"]),
    ("status", True), ("status", 1), ("status", None), ("status", ""),
])
def test_malformed_business_state_keeps_receipt_and_fails_closed(db, monkeypatch, field, value):
    actor = User(username="receipt-shape", password="unused", role="admin", status=1)
    db.add(actor)
    db.commit()
    monkeypatch.setattr("app.services.rbac_service.check_permission", lambda *_a, **_k: True)
    payload = site_agent_service.build_capability_team_payload(
        scope="user", capability="forum.posts.list", params={"page": 1, "page_size": 3},
        surface="user", session_key="receipt-shape", trace_id="receipt-shape",
    )
    created = agent_team_service.create_team_from_xiaoling(db, actor, payload)
    claim = agent_team_service.claim_next_task(db, created["team_id"], lease_seconds=60)
    data = {"agent": {"status": value}} if field == "agent.status" else {field: value}
    calls = []

    async def api(*_args, **_kwargs):
        calls.append(True)
        return {"status_code": 200, "data": data}

    monkeypatch.setattr(site_agent_service.admin_capability_service, "execute_api", api)

    def execute():
        return asyncio.run(site_agent_service.execute_site_task(
            db, actor, team_id=created["team_id"], task_id=claim["task_id"],
            lease_token=claim["lease_token"],
        ))

    result, replayed = execute(), execute()
    assert result == replayed
    assert result["status"] in {"failed", "blocked"}
    assert result["result_receipt"]["output"]["data"] == data
    assert "TypeError" not in result["summary"]
    assert result["retryable"] is False and calls == [True]
    ledger = db.query(AgentToolExecution).one()
    assert ledger.status == "failed" and json.loads(ledger.result_json) == result
