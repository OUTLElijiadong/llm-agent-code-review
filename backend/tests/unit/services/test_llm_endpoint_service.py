"""通用 LLM 端点发现、连接测试与回退契约。"""

from types import SimpleNamespace

import pytest

from app.schemas.api_config import ApiConfigTestIn
from app.schemas.llm_config import LlmModelsIn
from app.services import api_config_service


class FakeResponse:
    def __init__(self, status_code: int, body=None, text: str = ""):
        self.status_code = status_code
        self._body = body
        self.text = text

    def json(self):
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


class FakeClient:
    responses: list[FakeResponse] = []
    calls: list[tuple[str, str, dict]] = []

    def __init__(self, *_args, **_kwargs):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def get(self, url, **kwargs):
        self.calls.append(("GET", url, kwargs))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    def post(self, url, **kwargs):
        self.calls.append(("POST", url, kwargs))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


@pytest.fixture(autouse=True)
def fake_public_http(monkeypatch):
    FakeClient.responses = []
    FakeClient.calls = []
    monkeypatch.setattr(api_config_service.httpx, "Client", FakeClient)
    monkeypatch.setattr(
        api_config_service,
        "pin_public_http_url",
        lambda url: SimpleNamespace(
            request_url=url.replace("api.example.com", "93.184.216.34"),
            host_header="api.example.com",
            ip_address="93.184.216.34",
            request_extensions={"sni_hostname": "api.example.com"},
        ),
    )
    monkeypatch.setattr(api_config_service.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(
        "app.utils.api_resolver._resolve_host",
        lambda _host: {"93.184.216.34"},
    )


def test_fetch_models_normalizes_endpoint_and_deduplicates_ids():
    FakeClient.responses = [
        FakeResponse(200, {"data": [{"id": "gpt-4o"}, {"id": "gpt-4o"}, {"id": "qwen"}]})
    ]

    result = api_config_service.fetch_models(LlmModelsIn(
        provider="openai",
        api_key="sk-test",
        base_url="https://api.example.com/v1/chat/completions",
        model="qwen",
    ))

    assert result.success is True
    assert result.models == ["gpt-4o", "qwen"]
    assert result.selected_model == "qwen"
    assert FakeClient.calls[0][1].endswith("/v1/models")
    assert FakeClient.calls[0][2]["headers"]["Authorization"] == "Bearer sk-test"


def test_fetch_models_unsupported_endpoint_keeps_manual_model_without_blocking():
    FakeClient.responses = [FakeResponse(404, {"error": {"message": "not found"}})]

    result = api_config_service.fetch_models(LlmModelsIn(
        api_key="sk-test",
        base_url="https://api.example.com/v1",
        model="manual-model",
    ))

    assert result.success is True
    assert result.fallback is True
    assert result.models == ["manual-model"]
    assert "手工" in result.message


def test_fetch_models_retries_transient_status_then_succeeds():
    FakeClient.responses = [
        FakeResponse(503, text="busy"),
        FakeResponse(429, text="rate"),
        FakeResponse(200, ["model-a", {"id": "model-b"}]),
    ]

    result = api_config_service.fetch_models(LlmModelsIn(
        api_key="sk-test",
        base_url="https://api.example.com/v1",
        max_retries=2,
    ))

    assert result.success is True
    assert result.attempts == 3
    assert result.models == ["model-a", "model-b"]


def test_fetch_models_auth_failure_is_fast_and_does_not_retry():
    FakeClient.responses = [FakeResponse(401, text="secret should not echo")]

    result = api_config_service.fetch_models(LlmModelsIn(
        api_key="sk-secret",
        base_url="https://api.example.com/v1",
        model="manual-model",
        max_retries=5,
    ))

    assert result.success is False
    assert result.attempts == 1
    assert "sk-secret" not in result.message
    assert len(FakeClient.calls) == 1


def test_test_connection_uses_configured_runtime_options(db, admin_user):
    FakeClient.responses = [
        FakeResponse(200, {"id": "chatcmpl", "model": "actual-model", "choices": [{}]})
    ]

    result = api_config_service.test_connection(ApiConfigTestIn(
        provider="custom",
        api_key="sk-test",
        base_url="https://api.example.com/v1",
        model="manual-model",
        timeout_seconds=12,
        max_retries=0,
        temperature=1.2,
    ), db=db, user_id=admin_user.id)

    assert result.success is True
    assert result.model == "actual-model"
    assert result.attempts == 1
    payload = FakeClient.calls[0][2]["json"]
    assert payload["temperature"] == 1.2


def test_test_connection_rejects_incompatible_success_body(db, admin_user):
    FakeClient.responses = [FakeResponse(200, {"status": "ok", "model": "not-chat"})]

    result = api_config_service.test_connection(ApiConfigTestIn(
        api_key="sk-test",
        base_url="https://api.example.com/v1",
        model="manual-model",
        max_retries=0,
    ), db=db, user_id=admin_user.id)

    assert result.success is False
    assert "响应结构不兼容" in result.message


def test_schema_rejects_runtime_values_outside_safe_bounds():
    with pytest.raises(ValueError):
        LlmModelsIn(base_url="https://api.example.com", timeout_seconds=4)
    with pytest.raises(ValueError):
        LlmModelsIn(base_url="https://api.example.com", max_retries=6)
    with pytest.raises(ValueError):
        LlmModelsIn(base_url="https://api.example.com", temperature=2.1)


def _test_payload(**kwargs):
    return ApiConfigTestIn(
        api_key="sk-private-ping", base_url="https://api.example.com/v1", model="manual-model", **kwargs,
    )


def test_connection_records_each_http_attempt_and_preserves_unknown_usage(db, admin_user):
    import httpx

    from app.models.ai_call_log import AiCallLog
    from app.models.api_config import UserApiConfig

    FakeClient.responses = [
        FakeResponse(503, {"usage": {"prompt_tokens": 2, "completion_tokens": 5, "total_tokens": 7}}),
        httpx.ReadTimeout("do not record sk-private-ping"),
        FakeResponse(200, {
            "choices": [{}], "model": "actual-model",
            "usage": {"prompt_tokens": 0, "completion_tokens": 3, "total_tokens": 3},
        }),
    ]
    result = api_config_service.test_connection(_test_payload(max_retries=2), db=db, user_id=admin_user.id)
    assert result.success and result.attempts == 3
    rows = db.query(AiCallLog).order_by(AiCallLog.id).all()
    assert len(rows) == len(FakeClient.calls) == 3
    assert [row.status for row in rows] == ["retry", "retry", "success"]
    assert [row.total_tokens for row in rows] == [7, None, 3]
    assert rows[-1].prompt_tokens == 0
    assert {row.user_id for row in rows} == {admin_user.id}
    assert {row.agent_label for row in rows} == {"api_connection_test"}
    assert all("sk-private-ping" not in str(row.__dict__) for row in rows)
    assert db.query(UserApiConfig).count() == 0


def test_connection_validates_network_before_recording_and_models_get_has_no_usage(db, admin_user, monkeypatch):
    from app.core.exceptions import ValidationError
    from app.models.ai_call_log import AiCallLog
    from app.services.ai_usage_context import usage_context

    original = api_config_service.pin_public_http_url
    def deny(_url):
        raise ValidationError("端点拒绝")
    monkeypatch.setattr(api_config_service, "pin_public_http_url", deny)
    result = api_config_service.test_connection(_test_payload(), db=db, user_id=admin_user.id)
    assert not result.success
    assert not FakeClient.calls and db.query(AiCallLog).count() == 0
    monkeypatch.setattr(api_config_service, "pin_public_http_url", original)
    FakeClient.responses = [FakeResponse(200, {"data": [{"id": "model"}], "usage": {"total_tokens": 999}})]
    with usage_context(admin_user.id, {}, db=db):
        api_config_service.fetch_models(LlmModelsIn(api_key="sk-private-ping", base_url="https://api.example.com/v1"))
    assert FakeClient.calls[0][0] == "GET"
    assert db.query(AiCallLog).count() == 0


@pytest.mark.parametrize("body, expected_success, expected_total", [
    ({"choices": [{}]}, True, None),
    ({"usage": {"total_tokens": 9}}, False, 9),
    (ValueError("invalid JSON sk-private-ping"), False, None),
])
def test_connection_missing_or_invalid_response_retains_actual_usage(
    db, admin_user, body, expected_success, expected_total,
):
    from app.models.ai_call_log import AiCallLog

    FakeClient.responses = [FakeResponse(200, body)]
    result = api_config_service.test_connection(_test_payload(max_retries=0), db=db, user_id=admin_user.id)
    log = db.query(AiCallLog).one()
    assert result.success is expected_success
    assert log.total_tokens == expected_total
    assert log.status == ("success" if expected_success else "failed")
    assert "sk-private-ping" not in str(log.__dict__)


def test_connection_accounting_failure_never_retries_an_executed_request(db, admin_user, monkeypatch):
    from app.services.ai_usage_context import UsageAccountingError

    FakeClient.responses = [FakeResponse(503, {"usage": {"total_tokens": 9}})]
    def fail_log(**_kwargs):
        raise UsageAccountingError("审计存储失败")
    monkeypatch.setattr(api_config_service, "record_usage_attempt", fail_log)
    with pytest.raises(UsageAccountingError):
        api_config_service.test_connection(_test_payload(max_retries=3), db=db, user_id=admin_user.id)
    assert len(FakeClient.calls) == 1


@pytest.mark.parametrize("endpoint", ["/api/api-config/test", "/api/admin/llm/test"])
async def test_connection_api_uses_authenticated_owner_and_does_not_save_config(endpoint):
    import httpx
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    from app.core.database import Base, get_db
    from app.core.security import create_access_token
    from app.main import app
    from app.models.ai_call_log import AiCallLog
    from app.models.api_config import UserApiConfig
    from app.models.rbac import Role, UserRole
    from app.models.user import User

    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine, expire_on_commit=False)()
    actor = User(id=7, username="admin", password="unit", role="super_admin", status=1)
    db.add(actor)
    role = Role(code="super_admin", name="unit", status="active")
    db.add(role)
    db.flush()
    db.add(UserRole(user_id=actor.id, role_id=role.id))
    db.commit()
    previous = dict(app.dependency_overrides)
    app.dependency_overrides.clear()
    def local_db():
        yield db
    app.dependency_overrides[get_db] = local_db
    payload = _test_payload(max_retries=0).model_dump()
    payload["user_id"] = 999  # 载荷不得决定账本所属用户。
    FakeClient.responses = [FakeResponse(200, {"choices": [{}], "usage": {"total_tokens": 5}})]
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://unit") as client:
            anonymous = await client.post(endpoint, json=payload)
            assert anonymous.status_code == 401
            assert not FakeClient.calls
            result = await client.post(
                endpoint, json=payload,
                headers={"Authorization": "Bearer " + create_access_token(7, "super_admin", 0)},
            )
        assert result.status_code == 200
        assert result.json()["data"]["success"] is True
        assert len(FakeClient.calls) == 1
        log = db.query(AiCallLog).one()
        assert (log.user_id, log.total_tokens, log.agent_label) == (7, 5, "api_connection_test")
        assert "sk-private-ping" not in str(log.__dict__)
        assert db.query(UserApiConfig).count() == 0
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(previous)
        db.close()
        engine.dispose()
