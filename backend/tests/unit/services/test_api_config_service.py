"""
api_config_service 单元测试
"""
import pytest

from app.schemas.api_config import ApiConfigSaveIn


@pytest.fixture(autouse=True)
def _resolve_config_hosts_to_public_ip(monkeypatch):
    """单元测试固定公网 DNS 结果，保留生产存储时的强制 SSRF 校验。"""
    monkeypatch.setattr(
        "app.utils.api_resolver._resolve_host",
        lambda _host: {"8.8.8.8"},
    )


class TestGetConfig:
    """获取配置测试"""

    def test_get_default_when_no_config(self, db, admin_user):
        """无自定义配置时返回系统默认信息"""
        from app.services.api_config_service import get_config

        cfg = get_config(db, admin_user.id)
        assert cfg.is_custom is False
        assert cfg.provider == "deepseek"
        assert cfg.is_active is True
        # key 应该脱敏
        assert "****" in cfg.api_key_masked

    def test_get_user_config_after_save(self, db, admin_user):
        """保存后返回自定义配置"""
        from app.services.api_config_service import get_config, save_config

        save_config(db, admin_user.id, ApiConfigSaveIn(
            provider="openai",
            api_key="sk-personal-key",
            base_url="https://api.openai.com",
            model="gpt-4o",
        ))

        cfg = get_config(db, admin_user.id)
        assert cfg.is_custom is True
        assert cfg.provider == "openai"
        assert cfg.model == "gpt-4o"
        assert cfg.base_url == "https://api.openai.com"
        # key 必须脱敏
        assert "sk-personal-key" not in cfg.api_key_masked
        assert "****" in cfg.api_key_masked

    def test_get_config_disables_corrupted_ciphertext(self, db, admin_user):
        """坏密文不应继续显示为已启用的自定义配置。"""
        from app.models.api_config import UserApiConfig
        from app.services.api_config_service import get_config

        row = UserApiConfig(
            user_id=admin_user.id,
            provider="custom",
            api_key_enc="corrupted-user-ciphertext",
            base_url="https://llm.example.com",
            model="example-model",
            is_active=True,
        )
        db.add(row)
        db.commit()

        config = get_config(db, admin_user.id)
        db.refresh(row)

        assert config.is_custom is True
        assert config.is_active is False
        assert config.api_key_masked == "****"
        assert row.is_active is False


class TestSaveConfig:
    """保存配置测试"""

    def test_save_creates_new_config(self, db, admin_user):
        from app.services.api_config_service import save_config

        cfg = save_config(db, admin_user.id, ApiConfigSaveIn(
            provider="custom",
            api_key="sk-new-key",
            base_url="https://my-llm.com",
            model="my-model",
        ))

        assert cfg.is_custom is True
        assert cfg.model == "my-model"
        assert cfg.base_url == "https://my-llm.com"

    def test_save_updates_existing_config(self, db, admin_user):
        from app.services.api_config_service import save_config

        # 第一次保存
        save_config(db, admin_user.id, ApiConfigSaveIn(
            provider="deepseek",
            api_key="sk-first",
            base_url="https://v1.api.com",
            model="model-v1",
        ))

        # 第二次保存更新
        cfg = save_config(db, admin_user.id, ApiConfigSaveIn(
            provider="openai",
            api_key="sk-second",
            base_url="https://v2.api.com",
            model="model-v2",
        ))

        assert cfg.model == "model-v2"
        assert cfg.base_url == "https://v2.api.com"
        assert cfg.provider == "openai"

    def test_save_reactivates_inactive_config(self, db, admin_user):
        """保存应自动将 is_active 设为 True"""
        from app.models.api_config import UserApiConfig
        from app.services.api_config_service import save_config

        # 先手动创建一个 inactive 记录
        row = UserApiConfig(
            user_id=admin_user.id,
            provider="deepseek",
            api_key_enc="dummy",
            base_url="https://old.api.com",
            model="old-model",
            is_active=False,
        )
        db.add(row)
        db.commit()

        # 保存应重新激活
        save_config(db, admin_user.id, ApiConfigSaveIn(
            provider="deepseek",
            api_key="sk-reactived",
            base_url="https://new.api.com",
            model="new-model",
        ))

        from app.services.api_config_service import get_config
        cfg = get_config(db, admin_user.id)
        assert cfg.is_active is True
        assert cfg.is_custom is True


class TestDeleteConfig:
    """删除配置测试"""

    def test_delete_removes_config(self, db, admin_user):
        from app.services.api_config_service import delete_config, get_config, save_config

        # 先保存
        save_config(db, admin_user.id, ApiConfigSaveIn(
            provider="openai",
            api_key="sk-to-delete",
            base_url="https://api.openai.com",
            model="gpt-4o",
        ))

        # 删除
        delete_config(db, admin_user.id)

        # 应回退到默认
        cfg = get_config(db, admin_user.id)
        assert cfg.is_custom is False

    def test_delete_noop_when_no_config(self, db):
        """无配置时删除不抛异常"""
        from app.services.api_config_service import delete_config

        # 不应抛异常
        delete_config(db, user_id=99999)
