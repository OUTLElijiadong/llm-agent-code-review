"""
api_resolver 单元测试
"""
import pytest
from cryptography.fernet import Fernet, InvalidToken

from app.core.config import settings
from app.utils.api_resolver import (
    _derive_fernet_key,
    decrypt_api_key,
    decrypt_api_key_with_metadata,
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

    def test_primary_independent_key_is_used_dynamically(self, monkeypatch):
        """新写入必须使用密钥环第一项且无需迁移。"""
        monkeypatch.setattr(settings, "api_key_encryption_keys", ["current-independent-secret-1234567890"])

        encrypted = encrypt_api_key("sk-current-key")
        result = decrypt_api_key_with_metadata(encrypted)

        assert result is not None
        assert result.plaintext == "sk-current-key"
        assert result.source == "configured"
        assert result.key_index == 0
        assert result.needs_rotation is False

    def test_previous_independent_key_can_decrypt(self, monkeypatch):
        """密钥环后续项只用于兼容解密并标记待轮换。"""
        current = "current-independent-secret-1234567890"
        previous = "previous-independent-secret-123456789"
        monkeypatch.setattr(settings, "api_key_encryption_keys", [current, previous])
        encrypted = Fernet(_derive_fernet_key(previous)).encrypt(b"sk-previous-key").decode()

        result = decrypt_api_key_with_metadata(encrypted)

        assert result is not None
        assert result.plaintext == "sk-previous-key"
        assert result.source == "configured"
        assert result.key_index == 1
        assert result.needs_rotation is True

    def test_legacy_jwt_key_remains_a_decrypt_only_fallback(self, monkeypatch):
        """历史 JWT 派生密文在迁移期仍可读取。"""
        legacy_jwt = "legacy-jwt-secret-for-api-key-migration"
        monkeypatch.setattr(settings, "jwt_secret", legacy_jwt)
        monkeypatch.setattr(settings, "api_key_encryption_keys", ["new-independent-secret-1234567890123"])
        encrypted = Fernet(_derive_fernet_key(legacy_jwt)).encrypt(b"sk-legacy-key").decode()

        result = decrypt_api_key_with_metadata(encrypted)

        assert result is not None
        assert result.plaintext == "sk-legacy-key"
        assert result.source == "legacy_jwt"
        assert result.needs_rotation is True

    def test_new_ciphertext_cannot_be_decrypted_by_previous_key(self, monkeypatch):
        """新密文不得继续依赖旧密钥。"""
        current = "current-independent-secret-1234567890"
        previous = "previous-independent-secret-123456789"
        monkeypatch.setattr(settings, "api_key_encryption_keys", [current, previous])

        encrypted = encrypt_api_key("sk-new-ciphertext")

        with pytest.raises(InvalidToken):
            Fernet(_derive_fernet_key(previous)).decrypt(encrypted.encode())

    def test_decrypt_failure_log_does_not_contain_ciphertext(self, monkeypatch):
        """解密失败日志不得泄露待解密密文。"""
        ciphertext = "sensitive-invalid-ciphertext"
        messages = []
        monkeypatch.setattr(
            "app.utils.api_resolver.logger.warning",
            lambda message: messages.append(str(message)),
        )

        assert decrypt_api_key(ciphertext) == ""
        assert messages
        assert all(ciphertext not in message for message in messages)


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
        db.refresh(row)

        assert cfg.source == "system"
        assert row.is_active is False

    def test_resolve_with_none_user_id_returns_system(self, db):
        """user_id=None 时直接返回系统默认"""
        from app.utils.api_resolver import resolve_api_config

        cfg = resolve_api_config(db, user_id=None)
        assert cfg.source == "system"

    def test_resolve_rotates_previous_key_ciphertext(self, db, monkeypatch):
        """读取旧密钥密文后应机会式重加密并提交。"""
        from app.models.api_config import UserApiConfig
        from app.utils.api_resolver import resolve_api_config

        current = "current-independent-secret-1234567890"
        previous = "previous-independent-secret-123456789"
        monkeypatch.setattr(settings, "api_key_encryption_keys", [current, previous])
        old_ciphertext = Fernet(_derive_fernet_key(previous)).encrypt(b"sk-rotate-me").decode()
        row = UserApiConfig(
            user_id=1001,
            provider="openai",
            api_key_enc=old_ciphertext,
            base_url="https://api.openai.com",
            model="gpt-4o",
            is_active=True,
        )
        db.add(row)
        db.commit()

        cfg = resolve_api_config(db, user_id=1001)
        db.refresh(row)
        rotated = decrypt_api_key_with_metadata(row.api_key_enc)

        assert cfg.source == "user"
        assert cfg.api_key == "sk-rotate-me"
        assert row.api_key_enc != old_ciphertext
        assert rotated is not None
        assert rotated.key_index == 0
        assert rotated.needs_rotation is False


    def test_rotation_commit_failure_falls_back_without_logging_secrets(
        self,
        db,
        monkeypatch,
    ):
        """旧密钥轮换无法提交时应回退且日志不含明文或密文。"""
        from app.models.api_config import UserApiConfig
        from app.utils.api_resolver import resolve_api_config

        current = "current-independent-secret-1234567890"
        previous = "previous-independent-secret-123456789"
        plaintext = "sk-never-log-rotation-secret"
        monkeypatch.setattr(settings, "api_key_encryption_keys", [current, previous])
        old_ciphertext = Fernet(_derive_fernet_key(previous)).encrypt(plaintext.encode()).decode()
        row = UserApiConfig(
            user_id=1002,
            provider="openai",
            api_key_enc=old_ciphertext,
            base_url="https://api.openai.com",
            model="gpt-4o",
            is_active=True,
        )
        db.add(row)
        db.commit()
        messages = []
        monkeypatch.setattr(
            "app.utils.api_resolver.logger.warning",
            lambda message: messages.append(str(message)),
        )
        monkeypatch.setattr(db, "commit", lambda: (_ for _ in ()).throw(RuntimeError("write failed")))

        config = resolve_api_config(db, user_id=1002)

        assert config.source == "system"
        assert messages
        assert all(plaintext not in message for message in messages)
        assert all(old_ciphertext not in message for message in messages)
