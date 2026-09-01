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


def test_test_connection_uses_configured_runtime_options():
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
    ))

    assert result.success is True
    assert result.model == "actual-model"
    assert result.attempts == 1
    payload = FakeClient.calls[0][2]["json"]
    assert payload["temperature"] == 1.2


def test_test_connection_rejects_incompatible_success_body():
    FakeClient.responses = [FakeResponse(200, {"status": "ok", "model": "not-chat"})]

    result = api_config_service.test_connection(ApiConfigTestIn(
        api_key="sk-test",
        base_url="https://api.example.com/v1",
        model="manual-model",
        max_retries=0,
    ))

    assert result.success is False
    assert "响应结构不兼容" in result.message


def test_schema_rejects_runtime_values_outside_safe_bounds():
    with pytest.raises(ValueError):
        LlmModelsIn(base_url="https://api.example.com", timeout_seconds=4)
    with pytest.raises(ValueError):
        LlmModelsIn(base_url="https://api.example.com", max_retries=6)
    with pytest.raises(ValueError):
        LlmModelsIn(base_url="https://api.example.com", temperature=2.1)
