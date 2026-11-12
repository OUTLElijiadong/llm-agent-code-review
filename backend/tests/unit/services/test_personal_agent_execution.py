"""Private personal Agent paid execution: owner, billing, idempotency and ambiguity."""

import asyncio
from datetime import timedelta

import httpx
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.database import Base
from app.core.exceptions import ConflictError, NotFoundError
from app.models.billing_ledger import BillingCreditBatch, BillingReservation
from app.models.personal_agent import PersonalAgent, PersonalAgentRun
from app.models.system_config import SystemConfig
from app.models.user import User
from app.services import billing_ledger_service as ledger
from app.services import billing_rate_catalog_service as catalog
from app.services import billing_reservation_reconciliation_service as reconciliation
from app.services import personal_agent_execution_service as execution
from app.services import personal_agent_service
from app.utils.api_resolver import ApiConfig


@pytest.fixture
def context(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'personal-execution.db'}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    with factory.begin() as db:
        db.add_all([User(id=1, username="owner", password="x", role="user", status=1),
                    User(id=2, username="other", password="x", role="user", status=1)])
        db.flush()
        db.add(SystemConfig(config_key="llm_provider", config_value='{"active":false}'))
        catalog.seed_test_baseline(db)
        wallet = ledger.get_or_create_wallet(db, "user", 1)
        ledger.grant(db, wallet_id=wallet.id, grant_key="personal-execution-credit", kind="purchase",
                     amount_milli=2_000_000)
    with factory() as db:
        agent = personal_agent_service.create(db, 1, "助手", "简洁回答", "agent-1")
        agent_id = agent.id
    monkeypatch.setattr(execution.settings, "billing_commercial_metering_ready", True)
    monkeypatch.setattr(execution, "resolve_api_config", lambda _db: ApiConfig(
        api_key="sk-test", base_url="https://api.deepseek.com", model="deepseek-flash", source="system",
    ))
    yield factory, agent_id, monkeypatch
    engine.dispose()


def test_success_is_private_once_and_charged_from_saved_usage(context):
    factory, agent_id, monkeypatch = context
    sends = []
    def send(_config, payload):
        sends.append(payload)
        return {"choices": [{"finish_reason": "stop", "message": {"content": "完成"}}],
                "usage": {"prompt_tokens": 12, "completion_tokens": 7}}
    monkeypatch.setattr(execution, "_send_once", send)
    result = execution.execute(factory, owner_id=1, agent_id=agent_id, version_number=1,
                               request_key="one", input_text="你好")
    assert result["status"] == "succeeded" and result["output"] == "完成"
    assert execution.execute(factory, owner_id=1, agent_id=agent_id, version_number=1,
                             request_key="one", input_text="你好")["id"] == result["id"]
    assert len(sends) == 1 and not sends[0].get("tools")
    assert sends[0]["thinking"] == {"type": "disabled"}
    with pytest.raises(ConflictError):
        execution.execute(factory, owner_id=1, agent_id=agent_id, version_number=1,
                          request_key="one", input_text="不同")
    with pytest.raises(NotFoundError):
        execution.get_run(factory, owner_id=2, run_id=result["id"])
    with factory() as db:
        reservation = db.query(BillingReservation).one()
        assert reservation.status == "settled" and (reservation.input_tokens, reservation.output_tokens) == (12, 7)
        batch = db.query(BillingCreditBatch).one()
        assert batch.reserved_milli == 0 and batch.remaining_milli == 2_000_000 - reservation.actual_milli


def test_timeout_freezes_hold_and_same_key_never_sends_again(context):
    factory, agent_id, monkeypatch = context
    sends = []
    def send(*_args):
        sends.append(1)
        raise httpx.ReadTimeout("unknown")
    monkeypatch.setattr(execution, "_send_once", send)
    row = execution.execute(factory, owner_id=1, agent_id=agent_id, version_number=1,
                            request_key="timeout", input_text="输入")
    assert row["status"] == "awaiting_reconciliation"
    assert execution.execute(factory, owner_id=1, agent_id=agent_id, version_number=1,
                             request_key="timeout", input_text="输入")["id"] == row["id"]
    assert len(sends) == 1
    with factory() as db:
        reservation = db.query(BillingReservation).one()
        assert reservation.status == "unknown" and reservation.actual_milli is None
        assert db.query(BillingCreditBatch).one().reserved_milli > 0


def test_gate_disabled_and_cross_owner_never_call_provider(context):
    factory, agent_id, monkeypatch = context
    monkeypatch.setattr(execution.settings, "billing_commercial_metering_ready", False)
    monkeypatch.setattr(execution, "_send_once", lambda *_: pytest.fail("provider must not be called"))
    with pytest.raises(ledger.BillingError):
        execution.execute(factory, owner_id=1, agent_id=agent_id, version_number=1,
                          request_key="closed", input_text="输入")
    with factory() as db:
        assert db.query(BillingReservation).count() == 0
        assert db.query(PersonalAgent).count() == 1


def test_disable_keeps_previously_completed_owner_result_but_blocks_new_runs(context):
    factory, agent_id, monkeypatch = context
    sends = []
    def send(*_args):
        sends.append(1)
        return {"choices": [{"finish_reason": "stop", "message": {"content": "已完成"}}],
                "usage": {"prompt_tokens": 12, "completion_tokens": 7}}
    monkeypatch.setattr(execution, "_send_once", send)
    result = execution.execute(factory, owner_id=1, agent_id=agent_id, version_number=1,
                               request_key="before-disable", input_text="内容")
    with factory() as db:
        personal_agent_service.disable(db, 1, agent_id, 1)
    assert execution.get_run(factory, owner_id=1, run_id=result["id"])["output"] == "已完成"
    with pytest.raises(ConflictError):
        execution.execute(factory, owner_id=1, agent_id=agent_id, version_number=1,
                          request_key="after-disable", input_text="内容")
    assert sends == [1]


def test_stale_run_stays_unknown_and_cannot_release_as_unsent(context):
    factory, agent_id, monkeypatch = context
    def send(_config, _payload):
        with factory.begin() as db:
            run = db.query(PersonalAgentRun).one()
            run.create_time = ledger._now() - timedelta(minutes=5)
        expired = execution.get_by_request(factory, owner_id=1, agent_id=agent_id, request_key="slow")
        assert expired["status"] == "awaiting_reconciliation"
        return {"choices": [{"finish_reason": "stop", "message": {"content": "迟到"}}],
                "usage": {"prompt_tokens": 12, "completion_tokens": 7}}
    monkeypatch.setattr(execution, "_send_once", send)
    result = execution.execute(factory, owner_id=1, agent_id=agent_id, version_number=1,
                               request_key="slow", input_text="内容")
    assert result["status"] == "awaiting_reconciliation" and result["output"] is None
    with factory() as db:
        reservation = db.query(BillingReservation).one()
        assert reservation.status == "unknown" and reservation.provider_attempt_started_at is not None
        with pytest.raises(reconciliation.ReconciliationBlocked):
            reconciliation.propose_reconciliation(
                db, reservation_id=reservation.id, actor_id=10, resolution="release_not_sent",
                evidence_reference="证据", evidence_sha256="b" * 64, note="请求释放",
                provider_request_id=None, input_tokens=None, output_tokens=None,
            )
        db.rollback()
        with pytest.raises(ledger.BillingError):
            ledger.release_verified_not_sent(db, reservation.request_key)


def test_missing_usage_freezes_and_replay_by_key_is_private(context):
    factory, agent_id, monkeypatch = context
    monkeypatch.setattr(execution, "_send_once", lambda *_: {"choices": [{"message": {"content": "x"}}]})
    result = execution.execute(factory, owner_id=1, agent_id=agent_id, version_number=1,
                               request_key="no-usage", input_text="内容")
    assert result["status"] == "awaiting_reconciliation" and result["actual_milli"] is None
    recovered = execution.get_by_request(factory, owner_id=1, agent_id=agent_id, request_key="no-usage")
    assert recovered["id"] == result["id"]
    with pytest.raises(NotFoundError):
        execution.get_by_request(factory, owner_id=2, agent_id=agent_id, request_key="no-usage")


@pytest.mark.parametrize(
    "reason", ["length", "content_filter", "tool_calls", "insufficient_system_resource", "aborted", None],
)
def test_non_stop_results_are_not_reported_as_success_but_trusted_usage_is_charged(context, reason):
    factory, agent_id, monkeypatch = context
    monkeypatch.setattr(execution, "_send_once", lambda *_: {
        "choices": [{"finish_reason": reason, "message": {"content": "部分结果"}}],
        "usage": {"prompt_tokens": 12, "completion_tokens": 7},
    })
    result = execution.execute(factory, owner_id=1, agent_id=agent_id, version_number=1,
                               request_key=f"reason-{str(reason).lower()}", input_text="内容")
    assert result["status"] == "result_unavailable" and result["output"] is None
    assert result["result_reason"] == (reason if reason is not None else "invalid_response")
    assert result["actual_milli"] is not None
    with factory() as db:
        assert db.query(BillingReservation).one().status == "settled"


def test_malformed_finish_reason_still_settles_trusted_usage(context):
    factory, agent_id, monkeypatch = context
    monkeypatch.setattr(execution, "_send_once", lambda *_: {
        "choices": [{"finish_reason": {"unexpected": "value"}, "message": {"content": "x"}}],
        "usage": {"prompt_tokens": 12, "completion_tokens": 7},
    })
    result = execution.execute(factory, owner_id=1, agent_id=agent_id, version_number=1,
                               request_key="bad-finish", input_text="内容")
    assert result["status"] == "result_unavailable"
    assert result["result_reason"] == "invalid_response"
    assert result["actual_milli"] is not None


def test_model_provider_attempt_is_shown_as_not_releasable(context):
    from app.api.v1.billing import _reservation_reconciliation_view

    factory, agent_id, monkeypatch = context
    monkeypatch.setattr(execution, "_send_once", lambda *_: (_ for _ in ()).throw(httpx.ReadTimeout("unknown")))
    execution.execute(factory, owner_id=1, agent_id=agent_id, version_number=1,
                      request_key="view-unknown", input_text="内容")
    with factory() as db:
        reservation = db.query(BillingReservation).one()
        view = _reservation_reconciliation_view(db, reservation, held_milli=reservation.requested_milli)
        assert any("provider attempt" in item for item in view["release_block_reasons"])


def test_changed_global_configuration_is_rejected_before_hold_and_http(context):
    factory, agent_id, monkeypatch = context
    def changed_config(db):
        db.query(SystemConfig).filter_by(config_key="llm_provider").one().config_value = '{"active":true}'
        db.commit()
        return ApiConfig(api_key="old-key", base_url="https://api.deepseek.com", model="deepseek-flash",
                         source="system")
    monkeypatch.setattr(execution, "resolve_api_config", changed_config)
    monkeypatch.setattr(execution, "_send_once", lambda *_: pytest.fail("must not send after config changes"))
    with pytest.raises(ledger.BillingError):
        execution.execute(factory, owner_id=1, agent_id=agent_id, version_number=1,
                          request_key="config-changed", input_text="内容")
    with factory() as db:
        assert db.query(BillingReservation).count() == 0


def test_missing_platform_configuration_row_is_fail_closed(context):
    factory, agent_id, monkeypatch = context
    with factory.begin() as db:
        db.query(SystemConfig).delete()
    monkeypatch.setattr(execution, "_send_once", lambda *_: pytest.fail("missing config must not send"))
    with pytest.raises(ledger.BillingError, match="平台模型配置尚未完成"):
        execution.execute(factory, owner_id=1, agent_id=agent_id, version_number=1,
                          request_key="config-missing", input_text="内容")
    with factory() as db:
        assert db.query(BillingReservation).count() == 0


def test_absolute_deadline_includes_waiting_for_response_headers(monkeypatch):
    async def handler(_request):
        await asyncio.sleep(0.2)
        return httpx.Response(200, json={})

    original = httpx.AsyncClient
    monkeypatch.setattr(execution.httpx, "Client", lambda **_kw: pytest.fail("sync client is not total-deadline safe"))
    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(execution.httpx, "AsyncClient", lambda **kw: original(transport=transport, **kw))
    monkeypatch.setattr(execution, "_MAX_REQUEST_SECONDS", 0.03)
    with pytest.raises(TimeoutError):
        execution._send_once(ApiConfig(api_key="sk-test", base_url="https://api.deepseek.com",
                                       model="deepseek-flash"), {"model": "deepseek-flash"})


def test_absolute_deadline_includes_slow_body_chunks(monkeypatch):
    class SlowBody(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'{'  # first byte arrives promptly
            await asyncio.sleep(0.2)
            yield b'}'

    original = httpx.AsyncClient
    monkeypatch.setattr(execution.httpx, "Client", lambda **_kw: pytest.fail("sync client is not total-deadline safe"))
    transport = httpx.MockTransport(lambda _request: httpx.Response(200, stream=SlowBody()))
    monkeypatch.setattr(execution.httpx, "AsyncClient", lambda **kw: original(transport=transport, **kw))
    monkeypatch.setattr(execution, "_MAX_REQUEST_SECONDS", 0.03)
    with pytest.raises(TimeoutError):
        execution._send_once(ApiConfig(api_key="sk-test", base_url="https://api.deepseek.com",
                                       model="deepseek-flash"), {"model": "deepseek-flash"})
