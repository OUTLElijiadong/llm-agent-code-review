"""企业挂起必须在每次工具和模型尝试前阻断企业小菱运行。"""

import asyncio
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.database import Base
from app.core.exceptions import ForbiddenError
from app.models.billing_ledger import (
    BillingRateCatalog,
    BillingRateCatalogEvent,
    BillingRateCatalogItem,
    BillingReservation,
    BillingToolExecutionResult,
    BillingWallet,
)
from app.models.enterprise_asset import EnterpriseAssistantAssetInvocation
from app.models.enterprise_assistant_runtime import EnterpriseAssistantRuntimeState
from app.models.enterprise_chat import EnterpriseAssistantRun, EnterpriseConversation
from app.models.enterprise_org import Enterprise
from app.services import billing_runtime_gate
from app.services import enterprise_assistant_runtime_service as runtime
from app.services.deepseek_responses_runtime import ToolCall


def test_enterprise_agent_team_tools_expose_only_bounded_registered_agents():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    factory = sessionmaker(bind=engine)
    with factory() as db:
        executor = runtime.EnterpriseAssistantToolExecutor(
            db,
            enterprise_id=10,
            conversation_id=20,
            run_public_id="team-tools",
            lease_generation=1,
            actor_user_id=2,
        )
        schemas = asyncio.run(executor.tool_schemas())

    tools = {item["name"]: item for item in schemas}
    assert {
        "create_registered_enterprise_agent_team",
        "get_enterprise_agent_team",
        "cancel_enterprise_agent_team",
        "retry_enterprise_agent_team_task",
    } <= tools.keys()
    create = tools["create_registered_enterprise_agent_team"]
    member_agent = create["parameters"]["properties"]["members"]["items"]["properties"]["agent_code"]
    assert member_agent["enum"] == ["language_detector", "code_reviewer"]
    description = create["description"]
    assert "不可传项目 ID、URL、MCP、远测、运维或写操作" in description
    assert "持久队列异步执行" in description
    assert "取消父运行、撤权或权限快照变化" in description
    engine.dispose()


def test_enterprise_web_tools_are_exposed_only_with_matching_active_rate_snapshots(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    factory = sessionmaker(bind=engine)
    monkeypatch.setattr(runtime.settings, "billing_commercial_metering_ready", True)
    snapshots = {"search": ("catalog-v1", 1, 2), "fetch": ("catalog-v1", 1, 2)}
    monkeypatch.setattr(
        runtime.billing_rate_catalog_service,
        "tool_rate_snapshot",
        lambda _db, name: snapshots[name],
    )
    with factory() as db:
        executor = runtime.EnterpriseAssistantToolExecutor(
            db, enterprise_id=10, conversation_id=20, run_public_id="web-tools",
            lease_generation=1, actor_user_id=2,
        )
        schemas = asyncio.run(executor.tool_schemas())
        tools = {item["name"]: item for item in schemas}
        assert "search_enterprise_web" in tools and "fetch_enterprise_web_page" in tools
        assert tools["search_enterprise_web"]["parameters"]["properties"]["query"]["maxLength"] == 128
        assert tools["fetch_enterprise_web_page"]["parameters"]["properties"]["url"]["maxLength"] == 2048

        snapshots["fetch"] = ("catalog-v2", 1, 2)
        hidden = {item["name"] for item in asyncio.run(executor.tool_schemas())}
        assert "search_enterprise_web" not in hidden and "fetch_enterprise_web_page" not in hidden
    engine.dispose()


def test_enterprise_web_tool_dispatch_rechecks_original_initiator_and_never_approves(monkeypatch):
    from types import SimpleNamespace

    from app.services.deepseek_responses_runtime import ToolCall

    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    factory = sessionmaker(bind=engine)
    calls = []
    monkeypatch.setattr(runtime.EnterpriseAssistantToolExecutor, "_action_clarification", lambda *_args: None)
    with factory() as db:
        executor = runtime.EnterpriseAssistantToolExecutor(
            db, enterprise_id=10, conversation_id=20, run_public_id="web-dispatch",
            lease_generation=1, actor_user_id=2,
        )
        monkeypatch.setattr(executor, "_current", lambda: (SimpleNamespace(initiator_user_id=2), {}))

        async def execute_search(call, state, *, tool_name, arguments):
            calls.append((call.call_id, state.initiator_user_id, tool_name, arguments))
            return runtime.ToolExecutionResult.success({"billable": True})

        monkeypatch.setattr(executor, "_execute_search_fetch", execute_search)
        call = ToolCall(
            call_id="web-call", name="search_enterprise_web", arguments={"query": "latest"},
            raw_arguments='{"query":"latest"}',
        )
        result = asyncio.run(executor.execute(call))
        assert result.status == "success"
        assert calls == [("web-call", 2, "search_enterprise_web", {"query": "latest"})]
        denied = asyncio.run(executor.execute(call, approved=True))
        assert denied.status == "error" and denied.failure_kind == "authorization"
    engine.dispose()


@pytest.mark.parametrize("started_attempts", [0, 3])
def test_enterprise_search_fetch_runtime_wires_fresh_provider_and_enforces_run_hard_limit(
    monkeypatch, started_attempts, tmp_path,
):
    from datetime import datetime
    from types import SimpleNamespace

    from app.services.billing_search_fetch_service import SearchFetchResult
    from app.services.deepseek_responses_runtime import ToolCall

    engine = create_engine(f"sqlite:///{tmp_path / 'enterprise-search-fetch.db'}")
    Base.metadata.create_all(
        engine,
        tables=[BillingReservation.__table__, BillingToolExecutionResult.__table__, BillingWallet.__table__],
    )
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    provider_config = SimpleNamespace(
        provider="deepseek", base_url="https://api.deepseek.com", billing_credential_source="system",
        api_key="sk-runtime-test", billing_eligible=True,
    )
    calls = []
    resolved_sessions = []
    authorization_sessions = []

    def resolver_with_security_commit(db):
        # API-key rotation/deactivation is allowed to commit its own Session.
        # It must never receive the locked billing authorization Session.
        resolved_sessions.append(db)
        db.commit()
        return provider_config

    monkeypatch.setattr(runtime.EnterpriseAssistantToolExecutor, "_action_clarification", lambda *_args: None)
    monkeypatch.setattr(runtime.settings, "billing_commercial_metering_ready", True)
    monkeypatch.setattr(runtime, "resolve_api_config", resolver_with_security_commit)
    monkeypatch.setattr(
        runtime.billing_rate_catalog_service, "tool_rate_snapshot",
        lambda _db, name: ("catalog-v1", 5_038, 1_800) if name in {"search", "fetch"} else None,
    )
    state = SimpleNamespace(initiator_user_id=42, group_id=9)
    monkeypatch.setattr(runtime, "_validated_billing_group", lambda _db, _state: 9)

    async def fake_execute_tool(**kwargs):
        with kwargs["session_factory"].begin() as check_db:
            authorization_sessions.append(check_db)
            kwargs["authorize"](check_db)
        calls.append(kwargs)
        return SearchFetchResult("search", "success", True, {"results": [{"title": "安全结果"}]})

    monkeypatch.setattr(runtime.billing_search_fetch_service, "execute_tool", fake_execute_tool)
    with factory() as db:
        db.add(BillingWallet(id=1, owner_type="enterprise", owner_id=10))
        for index in range(started_attempts):
            db.add(BillingReservation(
                request_key=f"enterprise-assistant:web-hard-limit:tool:search:{index:032x}",
                model_name="deepseek-flash", billable_kind="search", tool_name="search",
                tool_cost_cny_micros_per_success=5_038, tool_price_milli_per_success=1_800,
                tool_successful_calls=1, provider_attempt_started_at=datetime(2026, 11, 7),
                provider_identity_version="deepseek-official-v1", provider_name="deepseek",
                provider_endpoint_host="api.deepseek.com", credential_source="system",
                pricing_version="catalog-v1", enterprise_id=10, group_id=9, user_id=42,
                usage_kind="member", enterprise_wallet_id=1, requested_milli=1_800,
                actual_milli=1_800, usage_state="reported", status="settled",
                created_at=datetime(2026, 11, 7),
            ))
        db.commit()
        executor = runtime.EnterpriseAssistantToolExecutor(
            db, enterprise_id=10, conversation_id=20, run_public_id="web-hard-limit",
            lease_generation=1, actor_user_id=42,
        )
        monkeypatch.setattr(executor, "_current", lambda **_kwargs: (state, {}))
        call = ToolCall(
            call_id="search-runtime-call", name="search_enterprise_web", arguments={"query": "最新"},
            raw_arguments='{"query":"最新"}',
        )
        result = asyncio.run(executor.execute(call))

    if started_attempts == 0:
        assert result.status == "success"
        assert result.output["results"] == [{"title": "安全结果"}]
        assert len(calls) == 1
        assert calls[0]["enterprise_wallet_id"] == 1
        assert calls[0]["group_id"] == 9
        assert calls[0]["provider_name"] == "deepseek"
        assert calls[0]["api_key"] == "sk-runtime-test"
        assert resolved_sessions
        assert all(resolved is not auth for resolved in resolved_sessions for auth in authorization_sessions)
    else:
        assert result.status == "error" and result.failure_kind == "billing"
        assert calls == []
    engine.dispose()


def test_enterprise_admin_can_control_group_run_as_higher_authority(monkeypatch):
    calls = []

    def capture_group_manager(_db, _enterprise_id, _group_id, _user_id, **kwargs):
        calls.append(kwargs)

    monkeypatch.setattr(runtime.enterprise_org_service, "require_group_manager", capture_group_manager)

    runtime._require_run_controller(
        object(),
        SimpleNamespace(enterprise_id=10, initiator_user_id=20),
        30,
        SimpleNamespace(kind="group", group_id=40),
    )

    assert calls == [{"allow_admin": True}]


def test_xiaoling_can_search_approved_asset_metadata_without_exposing_definition(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    monkeypatch.setattr(runtime.EnterpriseAssistantToolExecutor, "_action_clarification", lambda *_args: None)
    factory = sessionmaker(bind=engine)
    enterprise_rows = [
        SimpleNamespace(
            id=21,
            kind="agent",
            scope="enterprise",
            title="测试报告归纳",
            version_number=2,
            status="approved",
            revoked_at=None,
            content_sha256="a" * 64,
            definition_json='{"schema_version":1,"runtime":"prompt","system_prompt":"private system prompt"}',
        )
    ]
    global_rows = [
        SimpleNamespace(
            id=31,
            kind="skill",
            scope="global",
            title="报告翻译",
            version_number=1,
            status="approved",
            revoked_at=None,
            content_sha256="b" * 64,
            definition_json='{"schema_version":1,"skill_type":"llm_transform","prompt":"private transform prompt"}',
        )
    ]
    legacy_rows = [
        SimpleNamespace(
            id=41,
            kind="skill",
            scope="global",
            title="旧版 Skill",
            version_number=1,
            status="approved",
            revoked_at=None,
            content_sha256="d" * 64,
            definition_json='{"prompt":"legacy transform"}',
        )
    ]
    lookups = []
    monkeypatch.setattr(
        runtime.enterprise_asset_service,
        "search_runtime_catalog",
        lambda _db, **kwargs: (lookups.append(kwargs) or [*global_rows, *enterprise_rows, *legacy_rows]),
    )
    with factory() as db:
        executor = runtime.EnterpriseAssistantToolExecutor(
            db,
            enterprise_id=10,
            conversation_id=20,
            run_public_id="asset-search",
            lease_generation=4,
            actor_user_id=2,
        )
        monkeypatch.setattr(executor, "_current", lambda: (SimpleNamespace(initiator_user_id=2), {}))
        schemas = asyncio.run(executor.tool_schemas())
        schemas_by_name = {row["name"]: row for row in schemas}
        assert "附件片段" in schemas_by_name["search_enterprise_knowledge"]["description"]
        assert "search_published_enterprise_assets" in schemas_by_name
        assert "invoke_published_enterprise_asset" in schemas_by_name
        assert (
            schemas_by_name["invoke_published_enterprise_asset"]["parameters"]["properties"]["task"]["maxLength"]
            == 2500
        )
        assert (
            "invoke_published_enterprise_asset" in schemas_by_name["search_published_enterprise_assets"]["description"]
        )
        instructions = runtime._instructions()
        assert "资产执行能力尚未开放" not in instructions
        assert "只有工具成功返回后才能声称运行完成" in instructions
        assert "并不保证企业当前已开计量、订阅、额度、权限或模型配置可用" in instructions
        result = asyncio.run(
            executor.execute(
                ToolCall(
                    call_id="call-search",
                    name="search_published_enterprise_assets",
                    arguments={"query": "报告"},
                    raw_arguments='{"query":"报告"}',
                )
            )
        )

    assert result.status == "success"
    assert [item["publication_id"] for item in result.output["items"]] == [31, 21, 41]
    assert all(item["execution_available"] is False for item in result.output["items"])
    assert [item["xiaoling_runtime_config_compatible"] for item in result.output["items"]] == [True, True, False]
    assert all("definition" not in item and "definition_json" not in item for item in result.output["items"])
    assert lookups == [{"query": "报告", "user_id": 2, "enterprise_id": 10, "kind": None, "limit": 10}]
    engine.dispose()


def test_enterprise_knowledge_tool_returns_only_current_scope_text_and_attachment_excerpts(monkeypatch):
    from types import SimpleNamespace

    from app.services.deepseek_responses_runtime import ToolCall

    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    monkeypatch.setattr(runtime.EnterpriseAssistantToolExecutor, "_action_clarification", lambda *_args: None)
    factory = sessionmaker(bind=engine)
    calls = []
    monkeypatch.setattr(
        runtime.enterprise_knowledge_service,
        "search_entries",
        lambda *_args, **_kwargs: [
            SimpleNamespace(title="发布制度", content="上线需要审批"),
        ],
    )
    monkeypatch.setattr(
        runtime.enterprise_knowledge_attachment_service,
        "search_attachments",
        lambda db, enterprise_id, user_id, query, **kwargs: (
            calls.append((enterprise_id, user_id, query, kwargs))
            or [
                {
                    "attachment_id": 8,
                    "file_name": "运维手册.pdf",
                    "excerpt": "变更窗口需要双人复核",
                    "search_mode": "local_hash_vector_and_terms",
                    "version": 2,
                }
            ]
        ),
    )
    with factory() as db:
        executor = runtime.EnterpriseAssistantToolExecutor(
            db,
            enterprise_id=10,
            conversation_id=20,
            run_public_id="knowledge-tool",
            lease_generation=4,
            actor_user_id=2,
        )
        monkeypatch.setattr(executor, "_current", lambda: (SimpleNamespace(initiator_user_id=2), {}))
        result = asyncio.run(
            executor.execute(
                ToolCall(
                    call_id="knowledge-search",
                    name="search_enterprise_knowledge",
                    arguments={"query": "审批"},
                    raw_arguments='{"query":"审批"}',
                )
            )
        )
    assert result.status == "success"
    assert result.output["scope"] == "current_enterprise_shared_knowledge"
    assert result.output["text_items"] == [{"title": "发布制度", "content": "上线需要审批"}]
    assert result.output["attachment_items"][0]["file_name"] == "运维手册.pdf"
    assert result.output["attachment_items"][0]["version"] == 2
    assert calls == [(10, 2, "审批", {"limit": 3})]
    engine.dispose()


def test_enterprise_knowledge_tool_rejects_non_initiator_before_search(monkeypatch):
    from types import SimpleNamespace

    from app.services.deepseek_responses_runtime import ToolCall

    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    factory = sessionmaker(bind=engine)
    calls = []
    monkeypatch.setattr(
        runtime.enterprise_knowledge_service, "search_entries", lambda *_args, **_kwargs: calls.append("text")
    )
    monkeypatch.setattr(
        runtime.enterprise_knowledge_attachment_service,
        "search_attachments",
        lambda *_args, **_kwargs: calls.append("attachment"),
    )
    with factory() as db:
        executor = runtime.EnterpriseAssistantToolExecutor(
            db,
            enterprise_id=10,
            conversation_id=20,
            run_public_id="knowledge-tool-other-actor",
            lease_generation=4,
            actor_user_id=2,
        )
        monkeypatch.setattr(executor, "_current", lambda: (SimpleNamespace(initiator_user_id=3), {}))
        result = asyncio.run(
            executor.execute(
                ToolCall(
                    call_id="knowledge-search",
                    name="search_enterprise_knowledge",
                    arguments={"query": "secret"},
                    raw_arguments='{"query":"secret"}',
                )
            )
        )
    assert result.status == "error" and "原发起人" in result.error
    assert calls == []
    engine.dispose()


def test_suspending_enterprise_blocks_next_model_attempt_before_reservation(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(
        engine,
        tables=[
            Enterprise.__table__,
            EnterpriseConversation.__table__,
            EnterpriseAssistantRun.__table__,
            EnterpriseAssistantRuntimeState.__table__,
            EnterpriseAssistantAssetInvocation.__table__,
            BillingReservation.__table__,
            BillingRateCatalog.__table__,
            BillingRateCatalogItem.__table__,
            BillingRateCatalogEvent.__table__,
        ],
    )
    factory = sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)
    with factory.begin() as db:
        from app.services.billing_rate_catalog_service import seed_test_baseline

        seed_test_baseline(db)
        db.add(
            Enterprise(
                id=10,
                name="甲企业",
                owner_id=1,
                seat_limit=5,
                status="active",
                paid_order_id="paid-10",
            )
        )
        run = EnterpriseAssistantRun(
            public_id="ear_suspend",
            request_key="enterprise-assistant:suspend",
            enterprise_id=10,
            conversation_id=20,
            initiator_user_id=2,
            source_message_id=30,
            status="running",
            attempt_count=1,
            prompt_sha256="a" * 64,
        )
        db.add(run)
        db.flush()
        state = EnterpriseAssistantRuntimeState(
            assistant_run_id=run.id,
            enterprise_id=10,
            conversation_id=20,
            initiator_user_id=2,
            source_message_id=30,
            status="running",
            checkpoint_json="{}",
            permission_snapshot_json="{}",
            permission_snapshot_sha256="b" * 64,
            stop_requested=False,
            cancel_reason="",
            lease_generation=4,
            billing_invocation_count=1,
            version=1,
        )
        db.add(state)

    with factory.begin() as db:
        db.query(Enterprise).filter_by(id=10).one().status = "suspended"
        run = db.query(EnterpriseAssistantRun).filter_by(public_id="ear_suspend").one()
        state = db.query(EnterpriseAssistantRuntimeState).filter_by(assistant_run_id=run.id).one()

    monkeypatch.setattr(runtime, "_live_role_snapshot", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(runtime.billing_checkout_service, "enterprise_subscription_status", lambda *_: "active")
    monkeypatch.setattr(runtime.assistant_service, "_require_active_limit", lambda *_args, **_kwargs: None)
    reservations = []
    monkeypatch.setattr(billing_runtime_gate.ledger, "reserve", lambda *_args, **_kwargs: reservations.append(True))
    provider_calls = []
    scope = runtime._billing_scope(factory, run=run, state=state, wallet_id=99)
    with scope:
        try:
            billing_runtime_gate.begin_attempt(
                model_name="deepseek-v4-flash",
                payload={"model": "deepseek-v4-flash", "input": "下一步请求", "max_output_tokens": 32},
                provider_name="deepseek",
                base_url="https://api.deepseek.com/v1",
                credential_source="system",
            )
        except ForbiddenError as exc:
            assert "企业已暂停" in str(exc)
        else:
            provider_calls.append("should not be reached")

    assert reservations == []
    assert provider_calls == []

    with factory() as db:
        executor = runtime.EnterpriseAssistantToolExecutor(
            db,
            enterprise_id=10,
            conversation_id=20,
            run_public_id="ear_suspend",
            lease_generation=4,
            actor_user_id=2,
        )
        try:
            executor._current()
        except ForbiddenError as exc:
            assert "企业已暂停" in str(exc)
        else:
            raise AssertionError("暂停企业的小菱工具入口必须在执行任何工具前拒绝")
    engine.dispose()


def test_group_leader_retry_is_revalidated_before_each_billing_reservation(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    monkeypatch.setattr(runtime, "_require_model_send_clearance", lambda *_args, **_kwargs: None)
    Base.metadata.create_all(
        engine,
        tables=[
            Enterprise.__table__,
            EnterpriseConversation.__table__,
            EnterpriseAssistantRun.__table__,
            EnterpriseAssistantRuntimeState.__table__,
            EnterpriseAssistantAssetInvocation.__table__,
            BillingReservation.__table__,
            BillingRateCatalog.__table__,
            BillingRateCatalogItem.__table__,
            BillingRateCatalogEvent.__table__,
        ],
    )
    factory = sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)
    room = EnterpriseConversation(
        id=20,
        enterprise_id=10,
        kind="group",
        conversation_key="group:40",
        group_id=40,
        created_by=30,
    )
    with factory.begin() as db:
        from app.services.billing_rate_catalog_service import seed_test_baseline

        seed_test_baseline(db)
        db.add(
            Enterprise(
                id=10,
                name="甲企业",
                owner_id=1,
                seat_limit=5,
                status="active",
                paid_order_id="paid-retry-10",
            )
        )
        db.add(room)
        run = EnterpriseAssistantRun(
            public_id="ear_retry",
            request_key="enterprise-assistant:retry",
            enterprise_id=10,
            conversation_id=20,
            group_id=40,
            initiator_user_id=2,
            source_message_id=30,
            status="running",
            attempt_count=1,
            prompt_sha256="c" * 64,
        )
        db.add(run)
        db.flush()
        state = EnterpriseAssistantRuntimeState(
            assistant_run_id=run.id,
            enterprise_id=10,
            conversation_id=20,
            group_id=40,
            initiator_user_id=2,
            source_message_id=30,
            status="running",
            checkpoint_json="{}",
            permission_snapshot_json="{}",
            permission_snapshot_sha256="d" * 64,
            stop_requested=False,
            cancel_reason="",
            lease_generation=4,
            billing_invocation_count=2,
            version=1,
        )
        db.add(state)

    monkeypatch.setattr(runtime, "_live_role_snapshot", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(runtime.billing_checkout_service, "enterprise_subscription_status", lambda *_: "active")
    monkeypatch.setattr(runtime.assistant_service, "_require_active_limit", lambda *_args, **_kwargs: None)
    controller_active = {"value": True}
    controller_checks = []

    def require_retry_controller(_db, _run, actor_id, current_room):
        controller_checks.append((actor_id, current_room.group_id))
        if not controller_active["value"]:
            raise ForbiddenError("小组长权限已撤销")

    monkeypatch.setattr(runtime, "_require_run_retry_controller", require_retry_controller)
    reservations = []
    monkeypatch.setattr(billing_runtime_gate.ledger, "reserve", lambda *_args, **_kwargs: reservations.append(True))
    scope = runtime._billing_scope(
        factory,
        run=run,
        state=state,
        wallet_id=99,
        controller_user_id=9,
        controller_room=room,
    )
    with scope:
        billing_runtime_gate.begin_attempt(
            model_name="deepseek-v4-flash",
            payload={"model": "deepseek-v4-flash", "input": "继续处理", "max_output_tokens": 32},
            provider_name="deepseek",
            base_url="https://api.deepseek.com/v1",
            credential_source="system",
        )
        # Model the prior response checkpoint being durably saved before the
        # next attempt; otherwise the pending-provider fence correctly stops
        # a second request before rechecking the group-leader control.
        with factory.begin() as db:
            current = db.query(EnterpriseAssistantRuntimeState).one()
            current.error_code = None
        controller_active["value"] = False
        try:
            billing_runtime_gate.begin_attempt(
                model_name="deepseek-v4-flash",
                payload={"model": "deepseek-v4-flash", "input": "继续处理下一步", "max_output_tokens": 32},
                provider_name="deepseek",
                base_url="https://api.deepseek.com/v1",
                credential_source="system",
            )
        except ForbiddenError as exc:
            assert "权限已撤销" in str(exc)
        else:
            raise AssertionError("小组长撤权后不得产生新的模型计费预占")

    assert controller_checks == [(9, 40), (9, 40)]
    assert reservations == [True]
    engine.dispose()
