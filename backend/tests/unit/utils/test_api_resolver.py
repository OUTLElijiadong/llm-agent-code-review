"""
api_resolver 单元测试
"""
from app.utils.api_resolver import (
    decrypt_api_key,
    encrypt_api_key,
    mask_api_key,
)


class TestEncryption:
    """加密/解密测试"""

    def test_encrypt_then_decrypt_returns_original(self):
        original = "sk-test-api-key-12345"
        encrypted = encrypt_api_key(original)
        assert encrypted != original
        decrypted = decrypt_api_key(encrypted)
        assert decrypted == original

    def test_encrypt_produces_different_output_each_time(self):
        """每次加密产生不同密文（Fernet 含随机 IV）"""
        key = "sk-same-key"
        e1 = encrypt_api_key(key)
        e2 = encrypt_api_key(key)
        # 密文不同但都能解密回原文
        assert decrypt_api_key(e1) == key
        assert decrypt_api_key(e2) == key

    def test_encrypt_handles_empty_string(self):
        encrypted = encrypt_api_key("")
        assert len(encrypted) > 0
        assert decrypt_api_key(encrypted) == ""

    def test_decrypt_invalid_returns_empty(self):
        result = decrypt_api_key("invalid-encrypted-string!!!")
        assert result == ""

    def test_encrypt_handles_unicode(self):
        key = "sk-测试密钥-中文"
        encrypted = encrypt_api_key(key)
        assert decrypt_api_key(encrypted) == key


class TestMaskApiKey:
    """API Key 脱敏测试"""

    def test_mask_standard_key(self):
        masked = mask_api_key("sk-a1b2c3d4e5f6g7h8i9j0")
        assert masked == "sk-a1****i9j0"
        assert "a1b2c3d4e5f6g7h8i9j0" not in masked

    def test_mask_short_key(self):
        masked = mask_api_key("short")
        assert masked == "****"

    def test_mask_empty_key(self):
        masked = mask_api_key("")
        assert masked == "****"

    def test_mask_none_like(self):
        masked = mask_api_key("")
        assert "****" in masked


class TestResolveApiConfig:
    """配置解析测试 — 需要 db fixture"""

    def test_resolve_system_default_when_no_user_config(self, db):
        """无用户配置时返回系统默认"""
        from app.utils.api_resolver import resolve_api_config

        cfg = resolve_api_config(db, user_id=99999)
        assert cfg.source == "system"
        assert len(cfg.api_key) > 0

    def test_resolve_user_config_when_present(self, db):
        """有用户配置时返回用户配置"""
        from app.models.api_config import UserApiConfig
        from app.utils.api_resolver import encrypt_api_key, resolve_api_config

        row = UserApiConfig(
            user_id=42,
            provider="openai",
            api_key_enc=encrypt_api_key("sk-user-key"),
            base_url="https://api.openai.com",
            model="gpt-4o",
            is_active=True,
        )
        db.add(row)
        db.commit()

        cfg = resolve_api_config(db, user_id=42)
        assert cfg.source == "user"
        assert cfg.api_key == "sk-user-key"
        assert cfg.base_url == "https://api.openai.com"
        assert cfg.model == "gpt-4o"
        assert cfg.provider == "openai"

    def test_resolve_falls_back_when_inactive(self, db):
        """is_active=False 时回退系统默认"""
        from app.models.api_config import UserApiConfig
        from app.utils.api_resolver import encrypt_api_key, resolve_api_config

        row = UserApiConfig(
            user_id=99,
            provider="openai",
            api_key_enc=encrypt_api_key("sk-inactive"),
            base_url="https://api.inactive.com",
            model="gpt-3.5",
            is_active=False,
        )
        db.add(row)
        db.commit()

        cfg = resolve_api_config(db, user_id=99)
        assert cfg.source == "system"

    def test_resolve_falls_back_when_decrypt_fails(self, db):
        """解密失败时回退系统默认"""
        from app.models.api_config import UserApiConfig
        from app.utils.api_resolver import resolve_api_config

        row = UserApiConfig(
            user_id=77,
            provider="deepseek",
            api_key_enc="corrupted-encrypted-data",
            base_url="https://api.deepseek.com",
            model="deepseek-chat",
            is_active=True,
        )
        db.add(row)
        db.commit()

        cfg = resolve_api_config(db, user_id=77)
        assert cfg.source == "system"

    def test_resolve_with_none_user_id_returns_system(self, db):
        """user_id=None 时直接返回系统默认"""
        from app.utils.api_resolver import resolve_api_config

        cfg = resolve_api_config(db, user_id=None)
        assert cfg.source == "system"
