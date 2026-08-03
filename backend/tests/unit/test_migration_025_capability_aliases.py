"""025 持久近义词种子迁移的幂等性与回滚验证。"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, text


def _migration_module():
    path = Path(__file__).resolve().parents[2] / "alembic/versions/025_seed_capability_aliases.py"
    spec = importlib.util.spec_from_file_location("migration_025", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_migration_025_is_idempotent_and_preserves_existing_rows(monkeypatch) -> None:
    module = _migration_module()
    engine = create_engine("sqlite:///:memory:")
    metadata = sa.MetaData()
    sa.Table(
        "agent_capability_alias",
        metadata,
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("capability_code", sa.String(255), nullable=False),
        sa.Column("alias", sa.String(160), nullable=False),
        sa.Column("normalized_alias", sa.String(160), nullable=False),
        sa.Column("locale", sa.String(20), nullable=False, server_default="zh-CN"),
        sa.Column("weight", sa.Float(), nullable=False, server_default="1"),
        sa.Column("enabled", sa.SmallInteger(), nullable=False, server_default="1"),
        sa.UniqueConstraint("capability_code", "locale", "normalized_alias"),
    )
    with engine.begin() as connection:
        metadata.create_all(connection)
        monkeypatch.setattr(module, "op", Operations(MigrationContext.configure(connection)))
        capability_code, alias = module.SEEDED_ALIASES[0]
        connection.execute(
            text(
                "INSERT INTO agent_capability_alias "
                "(capability_code, alias, normalized_alias, locale, weight, enabled) "
                "VALUES (:code, :alias, :normalized, 'zh-CN', 0.25, 0)"
            ),
            {
                "code": capability_code,
                "alias": alias,
                "normalized": module._normalize(alias),
            },
        )

        module.upgrade()
        module.upgrade()

        count = connection.execute(text("SELECT COUNT(*) FROM agent_capability_alias")).scalar_one()
        codes = set(
            connection.execute(text("SELECT DISTINCT capability_code FROM agent_capability_alias")).scalars()
        )
        assert count == len(module.SEEDED_ALIASES)
        assert codes == {
            "agent:sandbox_deployer",
            "agent:security_sentinel",
            "agent:test_verifier",
            "mcp:prism-code:download_project_source",
            "sandbox:close",
            "sandbox:create_deploy",
            "sandbox:create_test",
            "sandbox:extend",
        }
        preserved = connection.execute(
            text(
                "SELECT weight, enabled FROM agent_capability_alias "
                "WHERE capability_code=:code AND normalized_alias=:normalized"
            ),
            {"code": capability_code, "normalized": module._normalize(alias)},
        ).one()
        assert preserved == (0.25, 0)

        module.downgrade()
        assert (
            connection.execute(text("SELECT COUNT(*) FROM agent_capability_alias")).scalar_one()
            == len(module.SEEDED_ALIASES)
        )
