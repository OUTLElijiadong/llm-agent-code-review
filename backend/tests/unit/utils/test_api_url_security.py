"""AI API base_url SSRF 防护测试。"""
from types import SimpleNamespace

import pytest

from app.ai import deepseek_agent
from app.core.exceptions import ValidationError
from app.models.api_config import UserApiConfig
from app.schemas.api_config import ApiConfigSaveIn, ApiConfigTestIn
from app.services import api_config_service, embedding_service, system_config_service
from app.services.api_config_service import save_config
from app.utils.api_resolver import encrypt_api_key, resolve_api_config, validate_ai_base_url
from app.utils.public_http import pin_public_http_url


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost:11434",
        "http://127.0.0.1:11434",
        "http://10.0.0.2",
        "http://169.254.169.254/latest/meta-data",
        "http://[::1]:11434",
        "http://ollama:11434",
        "http://model.internal/v1",
        "ftp://api.example.com",
        "https://user:pass@api.example.com",
        "https://api.example.com/chat?token=x",
    ],
)
def test_validate_ai_base_url_rejects_unsafe_urls(url):
    """默认拒绝本机、内网、链路本地、非法协议和带凭据/query 的 URL。"""
    with pytest.raises(ValidationError):
        validate_ai_base_url(url, allow_private=False)


def test_validate_ai_base_url_accepts_public_https():
    """公网 HTTPS API 地址应继续可用。"""
    assert validate_ai_base_url("https://api.deepseek.com/") == "https://api.deepseek.com"
    assert validate_ai_base_url("https://api.openai.com/v1") == "https://api.openai.com/v1"


def test_validate_ai_base_url_dns_check_rejects_private_alias(monkeypatch):
    """域名字符串看似公网时，任一私网解析结果也必须拒绝。"""

    monkeypatch.setattr("app.utils.api_resolver._resolve_host", lambda _host: {"127.0.0.1"})
    with pytest.raises(ValidationError, match="内网或保留地址"):
        validate_ai_base_url(
            "https://public-looking.example/v1",
            resolve_host=True,
            allow_private=False,
        )


def test_pinned_public_url_preserves_host_and_sni(monkeypatch):
    """公网 URL 必须使用已验证 IP 连接，同时保留原域名 Host 与 TLS SNI。"""

    monkeypatch.setattr(
        "app.utils.public_http.socket.getaddrinfo",
        lambda *_args, **_kwargs: [(None, None, None, None, ("93.184.216.34", 443))],
    )

    target = pin_public_http_url("https://api.example.com/v1/responses")

    assert target.request_url == "https://93.184.216.34/v1/responses"
    assert target.host_header == "api.example.com"
    assert target.request_extensions == {"sni_hostname": "api.example.com"}


def test_pinned_public_url_formats_ipv6_host_header(monkeypatch):
    """IPv6 目标的请求 URL 和 Host 头都必须保留方括号。"""

    monkeypatch.setattr(
        "app.utils.public_http.socket.getaddrinfo",
        lambda *_args, **_kwargs: [(None, None, None, None, ("2606:4700:4700::1111", 443))],
    )

    target = pin_public_http_url("https://[2606:4700:4700::1111]/v1")

    assert target.request_url == "https://[2606:4700:4700::1111]/v1"
    assert target.host_header == "[2606:4700:4700::1111]"
    assert target.request_extensions == {"sni_hostname": "2606:4700:4700::1111"}


def test_deepseek_agent_connects_to_pinned_ip(monkeypatch):
    """审查模型的真实请求必须连接固定 IP，并保留 Host/SNI。"""

    monkeypatch.setattr(
        "app.utils.public_http.socket.getaddrinfo",
        lambda *_args, **_kwargs: [(None, None, None, None, ("93.184.216.34", 443))],
    )
    captured = {}

    class FakeClient:
        def post(self, url, **kwargs):
            captured.update({"url": url, **kwargs})
            return SimpleNamespace(status_code=200)

    monkeypatch.setattr(deepseek_agent, "_get_http_client", lambda _key: (FakeClient(), False))
    agent = deepseek_agent.DeepSeekAgent.__new__(deepseek_agent.DeepSeekAgent)
    agent.timeout = 30

    response, _duration = agent._do_request(
        "https://api.example.com/v1/chat/completions",
        {"Authorization": "Bearer test"},
        {"model": "test"},
    )

    assert response.status_code == 200
    assert captured["url"] == "https://93.184.216.34/v1/chat/completions"
    assert captured["headers"]["Host"] == "api.example.com"
    assert captured["extensions"] == {"sni_hostname": "api.example.com"}


def test_save_and_test_connection_reject_private_dns_alias(db, admin_user, monkeypatch):
    """保存与连通性测试都不得向解析到私网的别名发请求。"""

    monkeypatch.setattr("app.utils.api_resolver._resolve_host", lambda _host: {"127.0.0.1"})
    posted = False

    class NoHttpClient:
        def __init__(self, *_args, **_kwargs):
            nonlocal posted
            posted = True

    monkeypatch.setattr(api_config_service.httpx, "Client", NoHttpClient)
    payload = ApiConfigSaveIn(
        provider="custom",
        api_key="sk-test-private-alias",
        base_url="https://public-looking.example/v1",
        model="model",
    )

    with pytest.raises(ValidationError, match="内网或保留地址"):
        api_config_service.save_config(db, admin_user.id, payload)
    tested = api_config_service.test_connection(ApiConfigTestIn(**payload.model_dump()))
    assert tested.success is False
    assert "内网或保留地址" in tested.message
    assert posted is False


def test_global_llm_and_embedding_updates_reject_private_dns_alias(db, monkeypatch):
    """绕过路由直接调用服务层也不能落库私网别名。"""

    monkeypatch.setattr("app.utils.api_resolver._resolve_host", lambda _host: {"10.0.0.8"})
    with pytest.raises(ValidationError, match="内网或保留地址"):
        system_config_service.update_llm_config(
            db,
            base_url="https://public-looking.example/v1",
        )
    with pytest.raises(ValidationError, match="内网或保留地址"):
        system_config_service.update_embedding_config(
            db,
            base_url="https://public-looking.example/v1",
        )


def test_embedding_runtime_revalidates_and_never_opens_client(monkeypatch):
    """旧库中的恶意 embedding URL 在实际请求前仍会被阻断。"""

    monkeypatch.setattr("app.utils.api_resolver._resolve_host", lambda _host: {"169.254.169.254"})
    opened = False

    class NoHttpClient:
        def __init__(self, *_args, **_kwargs):
            nonlocal opened
            opened = True

    monkeypatch.setattr("httpx.Client", NoHttpClient)
    with pytest.raises(ValidationError, match="内网或保留地址"):
        embedding_service._api_embed(
            ["text"],
            {
                "base_url": "https://public-looking.example/v1",
                "api_key": "secret",
                "model": "embed",
            },
        )
    assert opened is False


def test_save_config_rejects_private_base_url(db, admin_user):
    """保存用户配置时应阻止明显的私有地址。"""
    with pytest.raises(ValidationError):
        save_config(db, admin_user.id, ApiConfigSaveIn(
            provider="custom",
            api_key="sk-local",
            base_url="http://127.0.0.1:11434",
            model="local-model",
        ))


def test_resolve_config_falls_back_when_saved_url_is_unsafe(db, admin_user):
    """历史库中若已有不安全地址,运行时解析应回退系统默认。"""
    row = UserApiConfig(
        user_id=admin_user.id,
        provider="custom",
        api_key_enc=encrypt_api_key("sk-local"),
        base_url="http://127.0.0.1:11434",
        model="local-model",
        is_active=True,
    )
    db.add(row)
    db.commit()

    cfg = resolve_api_config(db, user_id=admin_user.id)
    assert cfg.source == "system"
    assert cfg.base_url != "http://127.0.0.1:11434"
