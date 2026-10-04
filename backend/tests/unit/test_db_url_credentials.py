"""数据库凭据中的 URL 保留字符必须原样交给驱动。"""

import runpy
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from alembic.config import Config
from sqlalchemy.engine import make_url

from app.core.config import Settings


@pytest.mark.parametrize(
    "username,password",
    [("reviewer", "sample@host:123/#?%"), ("user:name", "口令@/?:#%"), ("ordinary", "plain")],
)
def test_mysql_url_preserves_credentials_and_connection_target(username, password):
    settings = Settings(
        _env_file=None, app_env="dev", db_host="127.0.0.1", db_port=3308,
        db_name="review_validation", db_user=username, db_password=password,
    )
    parsed = make_url(settings.db_url)
    assert parsed.username == username
    assert parsed.password == password
    assert parsed.host == "127.0.0.1"
    assert parsed.port == 3308
    assert parsed.database == "review_validation"
    assert parsed.query == {"charset": "utf8mb4"}


def test_existing_sqlite_target_is_preserved():
    settings = Settings(_env_file=None, app_env="dev", db_host="sqlite", db_name="isolated-validation")
    assert settings.db_url == "sqlite:///./isolated-validation.db"


def test_alembic_initializes_with_encoded_database_password(monkeypatch):
    from alembic import context
    from app.core.config import settings

    monkeypatch.setattr(settings, "db_host", "127.0.0.1")
    monkeypatch.setattr(settings, "db_name", "review_validation")
    monkeypatch.setattr(settings, "db_password", "sample@/#%")
    config = Config()
    monkeypatch.setattr(context, "config", config, raising=False)
    monkeypatch.setattr(context, "is_offline_mode", lambda: False)
    monkeypatch.setattr(context, "configure", MagicMock())
    monkeypatch.setattr(context, "begin_transaction", MagicMock())
    monkeypatch.setattr(context, "run_migrations", MagicMock())
    monkeypatch.setattr("sqlalchemy.engine_from_config", MagicMock())
    runpy.run_path(str(Path(__file__).parents[2] / "alembic" / "env.py"))
    assert make_url(config.get_main_option("sqlalchemy.url")).password == "sample@/#%"
