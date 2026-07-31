"""全局 LLM API Key 密钥轮换测试。"""
import json

from cryptography.fernet import Fernet

from app.core.config import settings
from app.models.system_config import SystemConfig
from app.services.system_config_service import LLM_KEY, get_llm_config
from app.utils.api_resolver import _derive_fernet_key, decrypt_api_key_with_metadata


def test_get_llm_config_rotates_previous_key_ciphertext(db, monkeypatch):
    """全局配置由旧密钥解密后应写回当前密钥密文。"""
    current = "current-independent-secret-1234567890"
    previous = "previous-independent-secret-123456789"
    monkeypatch.setattr(settings, "api_key_encryption_keys", [current, previous])
    old_ciphertext = Fernet(_derive_fernet_key(previous)).encrypt(b"sk-global-old").decode()
    row = SystemConfig(
        config_key=LLM_KEY,
        config_value=json.dumps({
            "provider": "custom",
            "base_url": "https://llm.example.com",
            "model": "example-model",
            "api_key_enc": old_ciphertext,
            "active": True,
        }),
    )
    db.add(row)
    db.commit()

    config = get_llm_config(db)
    db.refresh(row)
    stored = json.loads(row.config_value)
    rotated = decrypt_api_key_with_metadata(stored["api_key_enc"])

    assert config is not None
    assert config["api_key"] == "sk-global-old"
    assert config["active"] is True
    assert stored["api_key_enc"] != old_ciphertext
    assert rotated is not None
    assert rotated.needs_rotation is False


def test_get_llm_config_disables_corrupted_ciphertext(db):
    """完全无法解密的全局配置必须被停用并安全回退。"""
    row = SystemConfig(
        config_key=LLM_KEY,
        config_value=json.dumps({
            "provider": "custom",
            "base_url": "https://llm.example.com",
            "model": "example-model",
            "api_key_enc": "corrupted-global-ciphertext",
            "active": True,
        }),
    )
    db.add(row)
    db.commit()

    config = get_llm_config(db)
    db.refresh(row)
    stored = json.loads(row.config_value)

    assert config is not None
    assert config["api_key"] == ""
    assert config["active"] is False
    assert stored["active"] is False
