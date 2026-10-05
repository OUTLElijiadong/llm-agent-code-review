"""小菱主动异常研判只处理脱敏、短时 root 候选，异常即保持观察。"""

from __future__ import annotations

import json
from contextlib import contextmanager

import pytest

from app.agents.base import AgentResult
from app.models.audit_log import AuditLog
from app.models.system_config import SystemConfig
from app.services import ops_service, security_anomaly_service, security_response_service


def _candidate(candidate_id="a" * 32):
    return {
        "candidate_id": candidate_id, "rule": "web_sensitive_probe", "evidence_count": 12,
        "target_kinds": 3, "window_seconds": 300, "expires_at": "2099-01-01T00:00:00+00:00",
        "evidence_is_lower_bound": False,
        "ip": "PRIVATE-IP", "raw_log": "PRIVATE-LOG", "path": "PRIVATE-PATH", "api_key": "PRIVATE-KEY",
    }


def _snapshot(*, ai=True, enabled=True, candidates=None):
    return {
        "available": True, "verified": True, "enabled": enabled,
        "policy": {
            "enabled": enabled, "ai_anomaly_enabled": ai,
            "duration_seconds": 900, "window_seconds": 300,
            "ssh_threshold": 20, "web_threshold": 30, "allowlist_cidrs": [], "activated_at": None,
        },
        "protected_sources": [], "active_blocks": [], "recent_blocks": [],
        "last_evaluated_at": None, "errors": [], "backend": "ipset", "candidates": candidates or [],
    }


@pytest.fixture
def fake_root(monkeypatch):
    calls = []
    state = _snapshot(candidates=[_candidate()])

    def execute(_db, actor, **kwargs):
        calls.append(kwargs)
        assert actor is None
        return {"status": "success", "result": {"ok": True, "result": state}}

    monkeypatch.setattr(ops_service, "execute", execute)
    monkeypatch.setattr(security_response_service, "cached_status", lambda _db: state)
    return state, calls


def test_disabled_anomaly_mode_never_calls_model_or_root_candidates(db, fake_root, monkeypatch):
    state, calls = fake_root
    state["policy"]["ai_anomaly_enabled"] = False
    monkeypatch.setattr(security_anomaly_service, "_model_review", lambda *_args: pytest.fail("禁用不能调用模型"))
    result = security_anomaly_service.run_review(db)
    assert result["status"] == "skipped"
    assert calls == []


def test_model_receives_only_candidate_metadata_and_root_receives_only_known_ids(db, fake_root, monkeypatch):
    _, calls = fake_root
    received = []

    def review(_db, candidates):
        received.extend(candidates)
        return AgentResult(success=True, data={"decisions": [{
            "candidate_id": "a" * 32, "action": "block", "reason": "短窗内重复探测多个敏感目标",
        }]})

    monkeypatch.setattr(security_anomaly_service, "_model_review", review)
    result = security_anomaly_service.run_review(db)
    assert "PRIVATE" not in json.dumps(received)
    assert received[0]["target_kinds"] == 3
    assert calls[-1]["action"] == "security_block_apply_anomalies"
    assert calls[-1]["params"] == {"decisions": [{"candidate_id": "a" * 32, "reason": "短窗内重复探测多个敏感目标"}]}
    assert result["status"] == "submitted"
    assert db.query(AuditLog).filter(AuditLog.action == "security_anomaly_review").count() == 1
    second = security_anomaly_service.run_review(db)
    assert second["status"] == "skipped"
    assert len(received) == 1


@pytest.mark.parametrize("bad_data", [
    {"decisions": [{"candidate_id": "b" * 32, "action": "block", "reason": "未知来源"}]},
    {"decisions": [{"candidate_id": "a" * 32, "action": "block", "reason": "注入IP", "ip": "8.8.8.8"}]},
    {"decisions": [{"candidate_id": "a" * 32, "action": "execute", "reason": "未知操作"}]},
    {"decisions": [{"candidate_id": "a" * 32, "action": "block", "reason": "x" * 201}]},
    {"decisions": [{"candidate_id": "a" * 32, "action": "block", "reason": "重复"}] * 2},
    {"ip": "8.8.8.8", "action": "block"},
])
def test_hallucinated_or_malformed_model_result_never_applies(db, fake_root, monkeypatch, bad_data):
    _, calls = fake_root
    monkeypatch.setattr(
        security_anomaly_service, "_model_review", lambda *_args: AgentResult(success=True, data=bad_data),
    )
    result = security_anomaly_service.run_review(db)
    assert result["status"] == "observe"
    assert all(call["action"] != "security_block_apply_anomalies" for call in calls)


@pytest.mark.parametrize("failure_kind", ["output_truncated", "invalid_json", "upstream_error"])
def test_failed_model_result_stays_observation(db, fake_root, monkeypatch, failure_kind):
    _, calls = fake_root
    monkeypatch.setattr(security_anomaly_service, "_model_review", lambda *_args: AgentResult(
        success=False, failure_kind=failure_kind, error="模型结果不完整",
    ))
    result = security_anomaly_service.run_review(db)
    assert result["status"] == "observe"
    assert all(call["action"] != "security_block_apply_anomalies" for call in calls)


def test_daily_24_attempt_budget_is_persistent_and_fail_closed(db, fake_root, monkeypatch):
    _, calls = fake_root
    key = security_anomaly_service._budget_key()
    db.add(SystemConfig(config_key=key, config_value=json.dumps({"attempts": 24, "seen": []})))
    db.commit()
    monkeypatch.setattr(security_anomaly_service, "_model_review", lambda *_args: pytest.fail("超过日预算不能调用模型"))
    result = security_anomaly_service.run_review(db)
    assert result["status"] == "skipped"
    assert "24" in result["reason"]
    assert all(call["action"] != "security_block_apply_anomalies" for call in calls)


def test_corrupt_daily_budget_never_resets_counter_or_calls_model(db, fake_root, monkeypatch):
    db.add(SystemConfig(config_key=security_anomaly_service._budget_key(), config_value='{"attempts":"bad","seen":[]}'))
    db.commit()
    monkeypatch.setattr(security_anomaly_service, "_model_review", lambda *_args: pytest.fail("预算损坏不得调用模型"))
    result = security_anomaly_service.run_review(db)
    assert result["status"] == "observe"
    assert "预算记录损坏" in result["reason"]
    assert '"bad"' in db.query(SystemConfig).one().config_value


def test_root_candidate_unverified_never_calls_model(db, fake_root, monkeypatch):
    state, calls = fake_root
    state["verified"] = False
    monkeypatch.setattr(security_anomaly_service, "_model_review", lambda *_args: pytest.fail("候选未核验不得调用模型"))
    result = security_anomaly_service.run_review(db)
    assert result["status"] == "skipped"
    assert all(call["action"] != "security_block_apply_anomalies" for call in calls)


def test_failed_root_apply_does_not_claim_blocked(db, fake_root, monkeypatch):
    state, calls = fake_root
    original_execute = ops_service.execute

    def execute(*args, **kwargs):
        if kwargs["action"] == "security_block_apply_anomalies":
            calls.append(kwargs)
            return {"status": "running", "request_id": "root-unknown", "result": {"ok": True, "result": state}}
        return original_execute(*args, **kwargs)

    monkeypatch.setattr(ops_service, "execute", execute)
    monkeypatch.setattr(
        security_anomaly_service, "_model_review", lambda *_args: AgentResult(success=True, data={"decisions": [{
            "candidate_id": "a" * 32, "action": "block", "reason": "连续多次探测敏感目标",
        }]}),
    )
    result = security_anomaly_service.run_review(db)
    assert result["status"] == "observe"
    assert result["root_snapshot"]["available"] is False
    assert result["root_snapshot"]["active_blocks"] == []
    assert result["execution_request_id"] == "root-unknown"


def test_model_config_has_single_attempt_and_1000_token_cap(db, monkeypatch):
    from app.agents.operations_agent import OperationsAgent
    from app.services import agent_cost_budget_service
    from app.utils.api_resolver import ApiConfig

    captured = {}
    monkeypatch.setattr(
        security_anomaly_service, "resolve_api_config",
        lambda *_args: ApiConfig("key", "https://provider.invalid", "configured"),
    )
    monkeypatch.setattr(security_anomaly_service, "resolve_subagent_config", lambda _db, config, **_kwargs: config)

    @contextmanager
    def guard(*_args, **_kwargs):
        yield None

    monkeypatch.setattr(agent_cost_budget_service, "guard_automatic_model_call", guard)

    def call_json(_self, prompt, _ctx, **kwargs):
        captured.update(kwargs)
        assert "PRIVATE" not in prompt
        return AgentResult(success=True, data={"decisions": []}, http_attempts=0)

    monkeypatch.setattr(OperationsAgent, "call_json", call_json)
    result = security_anomaly_service._model_review(db, [{
        key: value for key, value in _candidate().items() if key in security_anomaly_service._CANDIDATE_KEYS
    }])
    assert result.success
    assert captured["max_tokens"] == 1000
    assert captured["api_config"].max_retries == 0
    assert captured["api_config"].temperature == 0
    assert captured["api_config"].timeout_seconds <= 30
