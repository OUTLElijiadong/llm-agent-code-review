"""Resolver -> admin runtime -> native transport billing-boundary tests.

The HTTP transport is replaced before iteration; this suite never calls a provider.
It exercises existing customer-wallet gates, not the disabled platform-cost WIP.
"""

import json
from contextlib import nullcontext
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy.orm import sessionmaker

from app.models.api_config import UserApiConfig
from app.models.billing_ledger import BillingReservation
from app.models.user import User
from app.services import agent_model_service, agent_responses_service, api_config_service, system_config_service
from app.services.billing_ledger_service import BillingError, get_or_create_wallet, grant
from app.services.billing_runtime_gate import billing_scope
from app.utils.api_resolver import encrypt_api_key, resolve_api_config


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mode", "vision", "scoped", "expected_source", "sent", "reserved", "error"),
    [
        ("user", False, False, "user", True, False, None),
        ("user", False, True, "user", False, False, "平台托管"),
        ("invalid_user", False, False, "fallback", False, False, "未进入可信计费作用域"),
        ("invalid_user", False, True, "fallback", False, False, "凭据来源"),
        ("invalid_global", False, True, "fallback", False, False, "凭据来源"),
        ("user", True, False, "system", False, False, "未进入可信计费作用域"),
        ("user", True, True, "system", True, True, None),
        ("system", False, True, "system", True, True, None),
        ("global", False, True, "global", True, True, None),
        ("foreign_provider", False, True, "global", False, False, "供应商未通过"),
        ("foreign_endpoint", False, True, "global", False, False, "官方 HTTPS API 端点"),
    ],
)
async def test_admin_resolved_credential_reaches_native_billing_boundary(
    db, monkeypatch, mode, vision, scoped, expected_source, sent, reserved, error,
):
    """A simulated user wallet exists only to observe the current billing gate."""
    settings = agent_responses_service.settings
    monkeypatch.setattr(settings, "billing_commercial_metering_ready", False)
    monkeypatch.setattr(settings, "billing_allow_unmetered_external_calls", False)
    monkeypatch.setattr(settings, "enforce_ai_base_url_dns_check", False)
    monkeypatch.setattr(settings, "deepseek_api_key", "isolated-system-key")
    monkeypatch.setattr(settings, "deepseek_base_url", "https://api.deepseek.com/v1")
    monkeypatch.setattr(settings, "deepseek_model", "deepseek-v4-flash")
    monkeypatch.setattr(agent_responses_service, "_is_admin_actor", lambda *_: True)
    monkeypatch.setattr(agent_responses_service, "_is_super_admin_actor", lambda *_: True)
    monkeypatch.setattr(agent_model_service, "resolve_agent_model", lambda _db, **kw: kw["config"].model)

    global_config = None
    if mode in {"global", "invalid_global", "foreign_provider", "foreign_endpoint"}:
        global_config = {
            "active": True, "api_key": "isolated-global-key", "model": "deepseek-v4-flash",
            "provider": "openai" if mode == "foreign_provider" else "deepseek",
            "base_url": "https://proxy.example/v1" if mode == "foreign_endpoint"
            else "https://api.deepseek.com/v1",
        }
        if mode == "invalid_global":
            global_config["api_key"] = ""
    monkeypatch.setattr(system_config_service, "get_llm_config", lambda _db: global_config)
    db.add(User(id=22, username="admin-provenance", password="test-only", role="super_admin", status=1))
    if mode in {"user", "invalid_user"}:
        db.add(UserApiConfig(
            user_id=22, provider="deepseek", base_url="https://api.deepseek.com/v1",
            model="deepseek-v4-flash", is_active=True,
            api_key_enc=encrypt_api_key("isolated-user-key") if mode == "user" else "invalid-ciphertext",
        ))
    wallet = get_or_create_wallet(db, "user", 22)
    grant(db, wallet_id=wallet.id, grant_key="provenance-test", kind="purchase", amount_milli=1000)
    db.commit()
    wallet_id = wallet.id
    session_factory = sessionmaker(bind=db.get_bind(), expire_on_commit=False)
    service = agent_responses_service.AgentResponsesService(
        db, SimpleNamespace(id=22, username="admin-provenance", role="super_admin"),
        surface="admin", session_key="provenance-test",
    )
    _, runtime = await service._runtime(
        "isolated-provenance-run", None, vision_model="deepseek-v4-flash" if vision else "",
    )
    transport = runtime._transport
    assert transport._config.billing_credential_source == expected_source
    observed = []
    payload = {"model": "deepseek-v4-flash", "input": "isolated", "max_output_tokens": 50,
               "tool_choice": "auto", "reasoning": {"effort": "high"}}

    def fake_upstream(request):
        with session_factory() as check:
            holds = check.query(BillingReservation).all()
            observed.append((json.loads(request.content), request.headers.get("Authorization"),
                             [(row.user_id, row.status) for row in holds]))
        event = {"type": "response.completed", "response": {"usage": {
            "input_tokens": 10, "output_tokens": 5,
        }}}
        return httpx.Response(200, headers={"Content-Type": "text/event-stream"},
                              content=f"event: response.completed\ndata: {json.dumps(event)}\n\n")

    original_client = httpx.AsyncClient
    mock_transport = httpx.MockTransport(fake_upstream)
    monkeypatch.setattr(agent_responses_service.httpx, "AsyncClient",
                        lambda *args, **kwargs: original_client(*args, transport=mock_transport, **kwargs))
    scope = billing_scope(session_factory=session_factory, user_id=22, personal_wallet_id=wallet_id,
                          max_input_tokens=512, max_output_tokens=50) if scoped else nullcontext()
    with scope:
        if error:
            with pytest.raises(BillingError, match=error):
                _ = [event async for event in transport._stream(payload)]
        else:
            _ = [event async for event in transport._stream(payload)]
    assert bool(observed) is sent
    if sent:
        assert observed[0][0] == {key: value for key, value in payload.items() if key != "tool_choice"}
        expected_key = {"user": "isolated-user-key", "system": "isolated-system-key",
                        "global": "isolated-global-key"}[expected_source]
        assert observed[0][1] == f"Bearer {expected_key}"
        assert observed[0][2] == ([(22, "reserved")] if reserved else [])
    with session_factory() as check:
        rows = check.query(BillingReservation).all()
        assert len(rows) == int(reserved)
        if reserved:
            assert rows[0].status == "settled"
            assert rows[0].credential_source == expected_source


@pytest.mark.asyncio
@pytest.mark.xfail(strict=True, raises=AssertionError,
                   reason="deactivated invalid BYOK loses fallback marker on subsequent resolution")
async def test_invalid_user_credential_remains_nonbillable_on_next_runtime(db, monkeypatch):
    """Do not silently turn a failed BYOK fallback into a later platform charge."""
    settings = agent_responses_service.settings
    monkeypatch.setattr(settings, "enforce_ai_base_url_dns_check", False)
    monkeypatch.setattr(settings, "deepseek_api_key", "isolated-system-key")
    monkeypatch.setattr(settings, "deepseek_base_url", "https://api.deepseek.com/v1")
    monkeypatch.setattr(settings, "deepseek_model", "deepseek-v4-flash")
    monkeypatch.setattr(agent_responses_service, "_is_admin_actor", lambda *_: True)
    monkeypatch.setattr(agent_responses_service, "_is_super_admin_actor", lambda *_: True)
    monkeypatch.setattr(agent_model_service, "resolve_agent_model", lambda _db, **kw: kw["config"].model)
    monkeypatch.setattr(system_config_service, "get_llm_config", lambda _db: None)
    db.add(User(id=22, username="admin-retry", password="test-only", role="super_admin", status=1))
    db.add(UserApiConfig(user_id=22, provider="deepseek", base_url="https://api.deepseek.com/v1",
                         model="deepseek-v4-flash", is_active=True, api_key_enc="invalid-ciphertext"))
    db.commit()
    service = agent_responses_service.AgentResponsesService(
        db, SimpleNamespace(id=22, username="admin-retry", role="super_admin"),
        surface="admin", session_key="retry-test",
    )
    _, first = await service._runtime("first-retry", None)
    _, second = await service._runtime("second-retry", None)
    assert first._transport._config.billing_credential_source == "fallback"
    assert second._transport._config.billing_credential_source == "fallback"


@pytest.mark.asyncio
@pytest.mark.xfail(strict=True, raises=AssertionError,
                   reason="config read deactivates invalid BYOK before runtime can mark fallback")
async def test_config_read_failure_remains_nonbillable_on_first_runtime(db, monkeypatch):
    """Reading configuration must not erase the failed credential's provenance."""
    settings = agent_responses_service.settings
    monkeypatch.setattr(settings, "enforce_ai_base_url_dns_check", False)
    monkeypatch.setattr(settings, "deepseek_api_key", "isolated-system-key")
    monkeypatch.setattr(settings, "deepseek_base_url", "https://api.deepseek.com/v1")
    monkeypatch.setattr(settings, "deepseek_model", "deepseek-v4-flash")
    monkeypatch.setattr(agent_responses_service, "_is_admin_actor", lambda *_: True)
    monkeypatch.setattr(agent_responses_service, "_is_super_admin_actor", lambda *_: True)
    monkeypatch.setattr(agent_model_service, "resolve_agent_model", lambda _db, **kw: kw["config"].model)
    monkeypatch.setattr(system_config_service, "get_llm_config", lambda _db: None)
    db.add(User(id=22, username="admin-config-read", password="test-only", role="super_admin", status=1))
    db.add(UserApiConfig(user_id=22, provider="deepseek", base_url="https://api.deepseek.com/v1",
                         model="deepseek-v4-flash", is_active=True, api_key_enc="invalid-ciphertext"))
    db.commit()
    config = api_config_service.get_config(db, 22)
    assert not config.is_active
    service = agent_responses_service.AgentResponsesService(
        db, SimpleNamespace(id=22, username="admin-config-read", role="super_admin"),
        surface="admin", session_key="config-read-test",
    )
    _, runtime = await service._runtime("first-after-config-read", None)
    assert runtime._transport._config.billing_credential_source == "fallback"


@pytest.mark.parametrize("case", ["no_config", "explicit_delete", "already_inactive"])
def test_existing_system_selection_paths_remain_distinguishable_in_repair_design(db, monkeypatch, case):
    """Current source selection is observed only; it is not a paid-use authorization."""
    settings = agent_responses_service.settings
    monkeypatch.setattr(settings, "enforce_ai_base_url_dns_check", False)
    monkeypatch.setattr(settings, "deepseek_api_key", "isolated-system-key")
    monkeypatch.setattr(settings, "deepseek_base_url", "https://api.deepseek.com/v1")
    monkeypatch.setattr(settings, "deepseek_model", "deepseek-v4-flash")
    monkeypatch.setattr(system_config_service, "get_llm_config", lambda _db: None)
    db.add(User(id=22, username="admin-reset", password="test-only", role="super_admin", status=1))
    if case != "no_config":
        db.add(UserApiConfig(
            user_id=22, provider="deepseek", base_url="https://api.deepseek.com/v1",
            model="deepseek-v4-flash", is_active=case == "explicit_delete",
            api_key_enc=encrypt_api_key("isolated-user-key"),
        ))
    db.commit()
    if case == "explicit_delete":
        assert resolve_api_config(db, 22).source == "user"
        api_config_service.delete_config(db, 22)
        assert db.query(UserApiConfig).filter_by(user_id=22).one_or_none() is None
    elif case == "already_inactive":
        assert db.query(UserApiConfig).filter_by(user_id=22).one().is_active is False
    assert resolve_api_config(db, 22).billing_credential_source == "system"
