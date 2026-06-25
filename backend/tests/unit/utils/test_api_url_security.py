"""AI API base_url SSRF 防护测试。"""
import pytest

from app.core.exceptions import ValidationError
from app.models.api_config import UserApiConfig
from app.schemas.api_config import ApiConfigSaveIn
from app.services.api_config_service import save_config
from app.utils.api_resolver import encrypt_api_key, resolve_api_config, validate_ai_base_url


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
