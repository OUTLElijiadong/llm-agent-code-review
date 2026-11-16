"""Real enterprise Search/Fetch executor attribution against isolated MySQL.

Only the provider HTTP, pricing catalog, model credentials, subscription check and
conversation clarification are substitutes. The parent run, authorization,
reservation and tool executor use the real implementation.
"""

import asyncio
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from threading import Event
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
import test_fresh_database_chain as chain
from sqlalchemy import event, text
from sqlalchemy.orm import sessionmaker

from app.models.billing_ledger import BillingCreditBatch, BillingLimit, BillingReservation
from app.models.enterprise_assistant_runtime import EnterpriseAssistantRuntimeState
from app.models.enterprise_chat import EnterpriseAssistantRun, EnterpriseChatMessage, EnterpriseConversation
from app.models.enterprise_org import Enterprise, EnterpriseMember
from app.models.user import User
from app.services import billing_ledger_service as ledger
from app.services import billing_search_fetch_service as search_fetch
from app.services import enterprise_assistant_clarification_service as clarification_service
from app.services import enterprise_assistant_runtime_service as runtime
from app.services import enterprise_org_service as org
from app.services.deepseek_responses_runtime import ToolCall

fresh_chain = chain.fresh_chain
fresh_chain_url = chain.fresh_chain_url


@pytest.mark.parametrize("room_kind", ["company", "direct"])
@pytest.mark.parametrize("tool_name", ["search", "fetch"])
@pytest.mark.parametrize("change", [
    "none", "default_aba", "remove_member", "clarification", "late_authority", "decision_revised",
    "approved_initiator_removed", "outer_run_lock", "concurrent_before_marker", "concurrent_after_marker",
])
def test_mysql_persisted_executor_tool_attribution_before_send(fresh_chain, monkeypatch, room_kind, tool_name, change):
    suffix = uuid4().hex[:12]
    sessions = sessionmaker(bind=fresh_chain, autoflush=False, expire_on_commit=False)
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    name = "search_enterprise_web" if tool_name == "search" else "fetch_enterprise_web_page"
    args = {"query": "test"} if tool_name == "search" else {"url": "https://example.com/page"}
    previous_id = None
    with sessions.begin() as db:
        owner = User(username=f"sf-run-owner-{suffix}", password="x", role="user", status=1)
        member = User(username=f"sf-run-member-{suffix}", password="x", role="user", status=1)
        db.add_all([owner, member])
        db.flush()
        company = Enterprise(name=f"SF runtime {suffix}", owner_id=owner.id, seat_limit=3,
                             status="active", billing_suspended=False, paid_order_id=f"sf-run-{suffix}")
        db.add(company)
        db.flush()
        enterprise_id, owner_id, member_id = int(company.id), int(owner.id), int(member.id)
        db.add_all([EnterpriseMember(enterprise_id=enterprise_id, user_id=owner_id, role="owner", status="active"),
                    EnterpriseMember(enterprise_id=enterprise_id, user_id=member_id, role="member", status="active")])
        db.flush()
        a = org.create_group(db, enterprise_id, owner_id, f"A-{suffix}")
        b = org.create_group(db, enterprise_id, owner_id, f"B-{suffix}")
        a_id, b_id = int(a.id), int(b.id)
        org.set_group_member(db, enterprise_id, a_id, owner_id, member_id, "member")
        org.set_group_member(db, enterprise_id, b_id, owner_id, member_id, "member")
        org.set_default_group(db, enterprise_id, owner_id, member_id, a_id)
        member_row = db.query(EnterpriseMember).filter_by(enterprise_id=enterprise_id, user_id=member_id).one()
        frozen_version = int(member_row.billing_group_version)
        room = EnterpriseConversation(enterprise_id=enterprise_id, kind=room_kind,
            conversation_key=f"{room_kind}:{suffix}", group_id=None,
            participant_low_id=min(owner_id, member_id) if room_kind == "direct" else None,
            participant_high_id=max(owner_id, member_id) if room_kind == "direct" else None, created_by=owner_id)
        db.add(room)
        db.flush()
        room_id = int(room.id)
        earlier = None
        if change in {"decision_revised", "approved_initiator_removed"}:
            earlier = EnterpriseChatMessage(
                enterprise_id=enterprise_id, conversation_id=room_id, sender_id=owner_id,
                sender_type="human", client_message_id=f"sf-earlier-owner-{suffix}",
                content="是否允许查询外部信息须确认", sender_authority_rank=3,
            )
            db.add(earlier)
            db.flush()
            previous_id = int(earlier.id)
        message = EnterpriseChatMessage(enterprise_id=enterprise_id, conversation_id=room_id,
            sender_id=member_id, sender_type="human", client_message_id=f"sf-msg-{suffix}",
            content="@小菱 查询", sender_authority_rank=1)
        db.add(message)
        db.flush()
        snapshot = runtime._role_snapshot(db, enterprise_id=enterprise_id, room=room,
                                          messages=[earlier, message] if earlier else [message],
                                          initiator_id=member_id)
        public_id = f"sf_runtime_{suffix}"
        run = EnterpriseAssistantRun(public_id=public_id, request_key=f"sf-run:{suffix}",
            enterprise_id=enterprise_id, conversation_id=room_id, group_id=None, initiator_user_id=member_id,
            source_message_id=message.id, status="running", attempt_count=1, prompt_sha256="c" * 64,
            billing_attribution_version=1, billing_group_id=a_id, billing_membership_version=frozen_version,
            billing_user_token_version=int(member.token_version))
        db.add(run)
        db.flush()
        db.add(EnterpriseAssistantRuntimeState(assistant_run_id=run.id, enterprise_id=enterprise_id,
            conversation_id=room_id, group_id=None, initiator_user_id=member_id, source_message_id=message.id,
            status="running", checkpoint_json="{}", permission_snapshot_json=runtime._json(snapshot),
            permission_snapshot_sha256=runtime._digest(snapshot), stop_requested=False, cancel_reason="",
            lease_generation=4, lease_expires_at=now + timedelta(minutes=5), billing_invocation_count=1, version=1))
        wallet = ledger.get_or_create_wallet(db, "enterprise", enterprise_id)
        wallet_id = int(wallet.id)
        ledger.grant(db, wallet_id=wallet_id, grant_key=f"sf-credit:{suffix}", kind="purchase", amount_milli=10_000)
        db.add(BillingLimit(limit_key=f"sf-limit:{suffix}", enterprise_id=enterprise_id, category="normal",
            group_id=a_id, user_id=member_id, period_start=now - timedelta(days=1),
            period_end=now + timedelta(days=30), limit_milli=10_000, used_milli=0, reserved_milli=0))

    clarification_id = None
    if change in {"decision_revised", "approved_initiator_removed"}:
        with sessions.begin() as db:
            saved_run = db.query(EnterpriseAssistantRun).filter_by(public_id=public_id).one()
            request = clarification_service.create_request(
                db, enterprise_id=enterprise_id, conversation_id=room_id, run_id=int(saved_run.id),
                phase="tool", call_id=f"call-{suffix}", action=name,
                target={"enterprise_id": enterprise_id, "conversation_id": room_id}, arguments=args,
                expires_at=now + timedelta(minutes=30),
            )
            clarification_id = int(request.id)
            clarification_service.decide(db, enterprise_id=enterprise_id, conversation_id=room_id,
                request_id=clarification_id, source_message_id=previous_id, user_id=owner_id, decision="allow")
        with sessions() as db:
            assert clarification_service.status(db, enterprise_id=enterprise_id, conversation_id=room_id,
                                                request_id=clarification_id) == "recorded"

    monkeypatch.setattr(runtime, "_CROSS_SCOPE_ATTRIBUTION_VERIFIED", True)
    monkeypatch.setattr(runtime.settings, "billing_commercial_metering_ready", True)
    checks = []

    def clarification(_executor, _state, _call, *_args):
        if change == "clarification" and len(checks) >= 2:
            return runtime.ToolExecutionResult.failure("澄清已变化", failure_kind="clarification_closed")
        return None

    if change == "clarification":
        monkeypatch.setattr(runtime.EnterpriseAssistantToolExecutor, "_action_clarification", clarification)
    monkeypatch.setattr(org, "require_enterprise_execution", lambda *_a, **_kw: None)
    monkeypatch.setattr(runtime.billing_rate_catalog_service, "tool_rate_snapshot",
                        lambda *_a, **_kw: ("rate-v1", 5038, 1800))
    monkeypatch.setattr(runtime, "resolve_api_config", lambda _db: SimpleNamespace(
        provider="deepseek", base_url="https://api.deepseek.com", billing_credential_source="system",
        api_key="sk-test", billing_eligible=True))
    outbound = []
    first_waiting = Event()
    allow_first = Event()
    original_client = httpx.AsyncClient
    response = {"results": [{"title": "result"}]} if tool_name == "search" else {"content": "result"}

    def provider(request):
        outbound.append(request)
        if change == "concurrent_after_marker":
            first_waiting.set()
            assert allow_first.wait(10), "second executor did not finish while first provider request was in flight"
        return httpx.Response(200, json=response)

    transport = httpx.MockTransport(provider)
    monkeypatch.setattr(search_fetch.httpx, "AsyncClient", lambda **kw: original_client(transport=transport, **kw))
    original_authorize = search_fetch._authorize

    def change_before_second_authorization(authorize, db):
        checks.append(1)
        if len(checks) == 2 and change == "concurrent_before_marker":
            first_waiting.set()
            assert allow_first.wait(10), "second executor did not finish before first send marker"
        if len(checks) == 2 and change in {
            "default_aba", "remove_member", "late_authority", "decision_revised", "approved_initiator_removed",
        }:
            with sessions.begin() as mutation:
                if change == "default_aba":
                    org.set_default_group(mutation, enterprise_id, owner_id, member_id, b_id)
                    org.set_default_group(mutation, enterprise_id, owner_id, member_id, a_id)
                elif change in {"remove_member", "approved_initiator_removed"}:
                    org.remove_enterprise_member(mutation, enterprise_id, owner_id, member_id)
                elif change == "late_authority":
                    mutation.add(EnterpriseChatMessage(
                        enterprise_id=enterprise_id, conversation_id=room_id, sender_id=owner_id,
                        sender_type="human", client_message_id=f"sf-late-owner-{suffix}",
                        content="不要执行搜索", sender_authority_rank=3,
                    ))
                else:
                    clarification_service.decide(mutation, enterprise_id=enterprise_id, conversation_id=room_id,
                        request_id=clarification_id, source_message_id=previous_id, user_id=owner_id, decision="deny")
        return original_authorize(authorize, db)

    monkeypatch.setattr(search_fetch, "_authorize", change_before_second_authorization)
    # Bound contention to identify any self-wait without changing server-wide settings.
    def lock_timeout(dbapi_connection, _record, _proxy):
        with dbapi_connection.cursor() as cursor:
            cursor.execute("SET SESSION innodb_lock_wait_timeout=3")
    event.listen(fresh_chain, "checkout", lock_timeout)
    errors = []
    waits = []
    stop_observer = Event()

    def capture_db_error(context):
        errors.append((str(context.original_exception), context.statement))

    def observe_waits():
        while not stop_observer.is_set():
            with fresh_chain.connect() as observer:
                rows = observer.execute(text(
                    "SELECT requested.OBJECT_NAME, requested.INDEX_NAME, requested.LOCK_MODE, "
                    "blocking.OBJECT_NAME, blocking.INDEX_NAME, blocking.LOCK_MODE "
                    "FROM performance_schema.data_lock_waits AS w "
                    "JOIN performance_schema.data_locks AS requested "
                    "ON requested.ENGINE_LOCK_ID = w.REQUESTING_ENGINE_LOCK_ID "
                    "JOIN performance_schema.data_locks AS blocking "
                    "ON blocking.ENGINE_LOCK_ID = w.BLOCKING_ENGINE_LOCK_ID "
                    "WHERE requested.OBJECT_SCHEMA = DATABASE()"
                )).all()
                waits.extend(tuple(row) for row in rows)
            time.sleep(0.05)

    event.listen(fresh_chain, "handle_error", capture_db_error)
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            observation = pool.submit(observe_waits)
            try:
                with sessions() as db:
                    uncommitted = User(username=f"sf-uncommitted-{suffix}", password="x", role="user", status=1)
                    db.add(uncommitted)
                    db.flush()
                    if change == "outer_run_lock":
                        db.query(EnterpriseAssistantRun).filter_by(public_id=public_id).with_for_update().one()
                    executor = runtime.EnterpriseAssistantToolExecutor(db, enterprise_id=enterprise_id,
                        conversation_id=room_id, run_public_id=public_id, lease_generation=4, actor_user_id=member_id)
                    call = ToolCall(call_id=f"call-{suffix}", name=name, arguments=args,
                                    raw_arguments=runtime._json(args))
                    second_result = None
                    if change in {"concurrent_before_marker", "concurrent_after_marker"}:
                        def run_first():
                            with sessions() as first_db:
                                first_executor = runtime.EnterpriseAssistantToolExecutor(
                                    first_db, enterprise_id=enterprise_id, conversation_id=room_id,
                                    run_public_id=public_id, lease_generation=4, actor_user_id=member_id,
                                )
                                return asyncio.run(first_executor.execute(call))

                        with ThreadPoolExecutor(max_workers=1) as workers:
                            first = workers.submit(run_first)
                            try:
                                assert first_waiting.wait(10), "first executor never reached the targeted boundary"
                                with sessions() as second_db:
                                    second_executor = runtime.EnterpriseAssistantToolExecutor(
                                        second_db, enterprise_id=enterprise_id, conversation_id=room_id,
                                        run_public_id=public_id, lease_generation=4, actor_user_id=member_id,
                                    )
                                    second_result = asyncio.run(second_executor.execute(call))
                            finally:
                                allow_first.set()
                            result = first.result(timeout=15)
                    elif change == "late_authority":
                        with pytest.raises(runtime.SupersededByAuthorityDirectiveError):
                            asyncio.run(executor.execute(call))
                        result = runtime.ToolExecutionResult.failure("已被高权限消息替代")
                    else:
                        result = asyncio.run(executor.execute(call))
                    with sessions() as verify:
                        assert verify.query(User).filter_by(username=f"sf-uncommitted-{suffix}").first() is None
                    assert db.query(User).filter_by(username=f"sf-uncommitted-{suffix}").first() is not None
                    db.rollback()
            finally:
                stop_observer.set()
                observation.result(timeout=10)
        if change == "outer_run_lock":
            assert result.status == "error" and "OperationalError" in result.error
            assert not outbound and not checks
            assert any(row[0] == "enterprise_assistant_run" for row in waits), waits
            with sessions() as verify:
                assert verify.query(BillingReservation).filter_by(enterprise_id=enterprise_id).count() == 0
            return
        assert len(checks) >= 2, (result.status, result.error, errors, waits[:10])
        with sessions() as db:
            reservation = db.query(BillingReservation).filter_by(enterprise_id=enterprise_id).one()
            batch = db.query(BillingCreditBatch).filter_by(wallet_id=wallet_id).one()
            limit = db.query(BillingLimit).filter_by(limit_key=f"sf-limit:{suffix}").one()
            assert (reservation.enterprise_id, reservation.user_id, reservation.group_id) == (
                enterprise_id, member_id, a_id,
            )
            if change == "none":
                assert result.status == "success" and len(outbound) == 1
                assert (reservation.status, reservation.tool_successful_calls) == ("settled", 1)
                assert (batch.reserved_milli, limit.reserved_milli, limit.used_milli) == (0, 0, 1800)
            elif change == "concurrent_after_marker":
                assert result.status == "error" and second_result.status == "error"
                assert second_result.failure_kind == "usage_unknown" and len(outbound) == 1
                assert (reservation.status, reservation.usage_state) == ("unknown", "unknown")
                assert batch.reserved_milli == limit.reserved_milli == 1800
            elif change == "concurrent_before_marker":
                assert result.status == "error" and second_result.status == "error" and not outbound
                assert second_result.failure_kind == "tool_result_missing"
                assert (reservation.status, reservation.usage_state, reservation.provider_attempt_started_at) == (
                    "released", "not_sent", None)
                assert (batch.reserved_milli, limit.reserved_milli, limit.used_milli) == (0, 0, 0)
            else:
                assert result.status == "error" and outbound == []
                assert (reservation.status, reservation.usage_state, reservation.provider_attempt_started_at) == (
                    "released", "not_sent", None)
                assert (batch.reserved_milli, limit.reserved_milli, limit.used_milli) == (0, 0, 0)
    finally:
        stop_observer.set()
        event.remove(fresh_chain, "handle_error", capture_db_error)
        event.remove(fresh_chain, "checkout", lock_timeout)
