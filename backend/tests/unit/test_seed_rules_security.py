"""种子脚本数据库凭据门禁测试。"""

import pytest

import seed_rules


def test_database_config_requires_explicit_credentials(monkeypatch) -> None:
    monkeypatch.delenv("DB_USER", raising=False)
    monkeypatch.delenv("DB_PASSWORD", raising=False)

    with pytest.raises(RuntimeError, match="DB_USER"):
        seed_rules._database_config()

    monkeypatch.setenv("DB_USER", "seed-user")
    with pytest.raises(RuntimeError, match="DB_PASSWORD"):
        seed_rules._database_config()


def test_database_config_uses_environment_without_credential_defaults(monkeypatch) -> None:
    monkeypatch.setenv("DB_HOST", "database.internal")
    monkeypatch.setenv("DB_PORT", "3308")
    monkeypatch.setenv("DB_USER", "seed-user")
    monkeypatch.setenv("DB_PASSWORD", "test-only-password")
    monkeypatch.setenv("DB_NAME", "review_test")

    config = seed_rules._database_config()

    assert config == {
        "host": "database.internal",
        "port": 3308,
        "user": "seed-user",
        "password": "test-only-password",
        "database": "review_test",
        "charset": "utf8mb4",
        "connect_timeout": 5,
    }


@pytest.mark.parametrize("value", ["invalid", "0", "65536"])
def test_database_config_rejects_invalid_port(monkeypatch, value: str) -> None:
    monkeypatch.setenv("DB_PORT", value)
    monkeypatch.setenv("DB_USER", "seed-user")
    monkeypatch.setenv("DB_PASSWORD", "test-only-password")

    with pytest.raises(RuntimeError, match="DB_PORT"):
        seed_rules._database_config()
