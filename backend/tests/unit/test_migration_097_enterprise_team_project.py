"""Migration 097 pins optional team project scope to the same enterprise."""

import importlib.util
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import (
    BigInteger,
    Column,
    Integer,
    MetaData,
    Table,
    UniqueConstraint,
    create_engine,
    event,
    inspect,
    text,
)
from sqlalchemy.exc import IntegrityError

MIGRATION = Path(__file__).resolve().parents[2] / "alembic" / "versions" / "097_enterprise_team_project.py"


def _load_migration():
    spec = importlib.util.spec_from_file_location("migration_097_enterprise_team_project", MIGRATION)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_097_binds_team_project_to_same_enterprise_and_preserves_legacy_nulls():
    migration = _load_migration()
    engine = create_engine("sqlite:///:memory:")

    @event.listens_for(engine, "connect")
    def enable_foreign_keys(connection, _record):
        connection.execute("PRAGMA foreign_keys=ON")

    metadata = MetaData()
    Table(
        "enterprise_project",
        metadata,
        Column("id", Integer, primary_key=True),
        Column("enterprise_id", BigInteger, nullable=False),
        Column("project_id", BigInteger, nullable=False),
        UniqueConstraint("enterprise_id", "project_id", name="uq_enterprise_project_tenant_scope"),
    )
    Table(
        "enterprise_agent_team",
        metadata,
        Column("id", Integer, primary_key=True),
        Column("enterprise_id", BigInteger, nullable=False),
    )
    metadata.create_all(engine)

    with engine.begin() as connection:
        migration.op = Operations(MigrationContext.configure(connection))
        migration.upgrade()
        connection.execute(text(
            "INSERT INTO enterprise_project (id, enterprise_id, project_id) VALUES (1, 10, 100)"
        ))
        connection.execute(text(
            "INSERT INTO enterprise_agent_team (id, enterprise_id, project_id) VALUES (1, 10, 100)"
        ))
        connection.execute(text(
            "INSERT INTO enterprise_agent_team (id, enterprise_id, project_id) VALUES (2, 10, NULL)"
        ))
        with pytest.raises(IntegrityError):
            connection.execute(text(
                "INSERT INTO enterprise_agent_team (id, enterprise_id, project_id) VALUES (3, 20, 100)"
            ))
        foreign_keys = inspect(connection).get_foreign_keys("enterprise_agent_team")
        assert any(
            item["name"] == "fk_ent_agent_team_project_scope"
            and item["constrained_columns"] == ["enterprise_id", "project_id"]
            and item["referred_table"] == "enterprise_project"
            and item["referred_columns"] == ["enterprise_id", "project_id"]
            for item in foreign_keys
        )
        migration.downgrade()
        assert "project_id" not in {column["name"] for column in inspect(connection).get_columns(
            "enterprise_agent_team",
        )}
    engine.dispose()
