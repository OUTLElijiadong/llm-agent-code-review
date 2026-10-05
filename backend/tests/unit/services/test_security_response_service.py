"""自动封禁只采信 root 回执，保持模型与通用运维写权限隔离。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.models.admin_chat import OpsExecution
from app.services import agent_responses_service, ops_service, security_response_service
from app.services.deepseek_responses_runtime import ToolCall


def _snapshot(*, enabled=False, verified=True, active=None):
    return {
        "available": True,
        "verified": verified,
        "enabled": enabled,
        "policy": {
            "enabled": enabled, "ai_anomaly_enabled": False, "duration_seconds": 900, "window_seconds": 300,
            "ssh_threshold": 20, "web_threshold": 30, "allowlist_cidrs": [],
            "activated_at": "2026-10-05T00:00:00+00:00" if enabled else None,
        },
        "protected_sources": [{"cidr": "8.8.8.8/32", "reason": "administrator"}],
        "active_blocks": active or [], "recent_blocks": [],
        "last_evaluated_at": None, "errors": [], "backend": "ipset",
    }


def _success(payload):
    return {"status": "success", "request_id": "security-unit-request", "result": {"ok": True, "result": payload}}


def test_status_missing_verification_never_claims_blocking(db, super_admin_user, monkeypatch):
    monkeypatch.setattr(
        ops_service, "execute", lambda *_args, **_kwargs: _success(_snapshot(enabled=True, verified=False)),
    )
    result = security_response_service.get_status(db, super_admin_user)
    assert result["available"] is False
    assert result["enabled"] is False
    assert result["active_blocks"] == []
    assert result["errors"]


@pytest.mark.parametrize("status", ["failed", "running"])
def test_failed_or_unknown_configure_does_not_claim_enabled(db, super_admin_user, monkeypatch, status):
    monkeypatch.setattr(ops_service, "execute", lambda *_args, **_kwargs: {
        "status": status, "request_id": "security-unit-request", "error": "执行结果未确认",
        "result": {"ok": True, "result": _snapshot(enabled=True)},
    })
    values = {key: value for key, value in _snapshot(enabled=True)["policy"].items() if key != "activated_at"}
    result = security_response_service.configure(db, super_admin_user, values, protected_ip="8.8.8.8")
    assert result["available"] is False
    assert result["enabled"] is False
    assert result["request_id"] == "security-unit-request"


def test_configure_forwards_only_policy_and_trusted_source(db, super_admin_user, monkeypatch):
    captured = {}

    def execute(_db, actor, **kwargs):
        captured.update(kwargs)
        assert actor is super_admin_user
        return _success(_snapshot(enabled=True))

    monkeypatch.setattr(ops_service, "execute", execute)
    values = {key: value for key, value in _snapshot(enabled=True)["policy"].items() if key != "activated_at"}
    result = security_response_service.configure(db, super_admin_user, values, protected_ip="8.8.8.8")
    assert result["enabled"] is True
    assert captured["action"] == "security_block_configure"
    assert captured["source"] == "security_center"
    assert captured["params"] == {**values, "protected_ip": "8.8.8.8"}


def test_reconcile_does_not_forward_ips_counts_or_model_text(db, monkeypatch):
    calls = []
    monkeypatch.setattr(ops_service, "execute", lambda *_args, **kwargs: calls.append(kwargs) or _success(_snapshot()))
    result = security_response_service.reconcile(db)
    assert result["available"] is True
    assert calls[0]["action"] == "security_block_reconcile"
    assert calls[0]["params"] == {}
    assert calls[0]["source"] == "security_response"


@pytest.mark.parametrize("action", sorted(ops_service.INTERNAL_SECURITY_ACTIONS))
def test_internal_security_actions_are_not_advertised_to_model(action):
    variants = agent_responses_service._operations_tool_schema()["parameters"]["oneOf"]
    assert action not in {variant["properties"]["action"]["const"] for variant in variants}


@pytest.mark.asyncio
async def test_model_cannot_forge_approved_security_action(db, super_admin_user, monkeypatch):
    monkeypatch.setattr(
        agent_responses_service, "get_request_orchestrator", lambda *_args, **_kwargs: SimpleNamespace(),
    )
    executor = agent_responses_service.PrismToolExecutor(
        db, super_admin_user, surface="admin", run_id="run-forged-autoblock", mcp_provider=SimpleNamespace(),
    )
    monkeypatch.setattr(ops_service, "execute", lambda *_args, **_kwargs: pytest.fail("模型不得调用内部封禁动作"))
    for action in ops_service.INTERNAL_SECURITY_ACTIONS:
        result = await executor._execute_operation(
            ToolCall(f"call-{action}", "admin_execute_operation", {"action": action, "params": {}}, ""), approved=True,
        )
        assert result.status == "error"
        assert "安全中心" in result.error


@pytest.mark.parametrize("action", ["security_block_configure", "security_block_release"])
def test_system_identity_cannot_execute_policy_or_manual_release(db, monkeypatch, action):
    params = {"enabled": False} if action == "security_block_configure" else {"ip": "8.8.8.8", "reason": "操作员解封"}
    monkeypatch.setattr(ops_service, "_call_executor", lambda *_args: pytest.fail("不应调用root"))
    with pytest.raises(PermissionError):
        ops_service.execute(db, None, action=action, params=params, source="security_response")


def test_reconcile_automatic_write_is_limited_to_security_response(db, monkeypatch):
    monkeypatch.setattr(ops_service, "_call_executor", lambda *_args: {"ok": True, "result": _snapshot()})
    for source in ("responses_admin_agent", "admin_copilot", "other"):
        with pytest.raises(PermissionError):
            ops_service.execute(db, None, action="security_block_reconcile", source=source)
    execution = ops_service.execute(db, None, action="security_block_reconcile", source="security_response")
    assert execution["status"] == "success"
    assert db.query(OpsExecution).count() == 1


def test_verified_snapshot_does_not_return_extra_host_payload(db, super_admin_user, monkeypatch):
    payload = {**_snapshot(enabled=True), "token": "PRIVATE", "host_file": "PRIVATE"}
    payload["policy"]["secret"] = "PRIVATE"
    monkeypatch.setattr(ops_service, "execute", lambda *_args, **_kwargs: _success(payload))
    result = security_response_service.get_status(db, super_admin_user)
    assert "PRIVATE" not in str(result)


def test_status_preserves_verified_kernel_family_and_block_source(db, super_admin_user, monkeypatch):
    payload = _snapshot(enabled=True, active=[{
        "id": "block1", "ip": "8.8.8.8", "rule": "web_sensitive_probe", "source": "xiaoling_anomaly",
        "evidence_count": 12, "scope": "web_and_ssh", "status": "active",
        "started_at": "2026-10-05T00:00:00Z", "expires_at": "2026-10-05T00:02:00Z", "reason": "多个敏感目标",
    }])
    payload["family_support"] = {"ipv4": True, "ipv6": False, "secret": "PRIVATE"}
    monkeypatch.setattr(ops_service, "execute", lambda *_args, **_kwargs: _success(payload))
    result = security_response_service.get_status(db, super_admin_user)
    assert result["family_support"] == {"ipv4": True, "ipv6": False}
    assert result["active_blocks"][0]["source"] == "xiaoling_anomaly"


@pytest.mark.parametrize("params", [
    {"decisions": [{"ip": "8.8.8.8", "reason": "任意IP"}]},
    {"decisions": [{"candidate_id": "a" * 32, "reason": "有意义"}] * 4},
    {"decisions": [{"candidate_id": "a" * 32, "reason": "重复"}] * 2},
    {"decisions": [{"candidate_id": "a" * 32, "reason": "   "}]},
])
def test_apply_anomalies_contract_rejects_ips_duplicates_or_unbounded_scope(params):
    with pytest.raises(ValueError):
        ops_service.validate_action_params("security_block_apply_anomalies", params)
