"""Migration 096 enforces tenant scope and append-only report storage."""

import importlib.util
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import BigInteger, Column, Integer, MetaData, Table, UniqueConstraint, create_engine, event, text
from sqlalchemy.exc import IntegrityError

MIGRATION = Path(__file__).resolve().parents[3] / "alembic" / "versions" / "096_enterprise_security_artifacts.py"


def _load_migration():
    spec = importlib.util.spec_from_file_location("migration_096_enterprise_security_artifacts", MIGRATION)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_enterprise_security_artifact_migration_enforces_scope_and_immutability():
    migration = _load_migration()
    engine = create_engine("sqlite:///:memory:")

    @event.listens_for(engine, "connect")
    def enable_foreign_keys(connection, _record):
        connection.execute("PRAGMA foreign_keys=ON")

    metadata = MetaData()
    Table(
        "enterprise_project", metadata,
        Column("id", Integer, primary_key=True),
        Column("enterprise_id", BigInteger, nullable=False),
        Column("project_id", BigInteger, nullable=False),
        UniqueConstraint("project_id", name="uk_enterprise_project_project"),
    )
    Table(
        "enterprise_agent_team", metadata,
        Column("id", Integer, primary_key=True),
        Column("enterprise_id", BigInteger, nullable=False),
        Column("conversation_id", BigInteger, nullable=False),
        UniqueConstraint("id", "enterprise_id", "conversation_id", name="uq_ent_agent_team_scope_ref"),
    )
    Table(
        "enterprise_agent_team_task", metadata,
        Column("id", Integer, primary_key=True),
        Column("team_id", BigInteger, nullable=False),
        UniqueConstraint("id", "team_id", name="uq_ent_agent_team_task_scope"),
    )
    metadata.create_all(engine)

    with engine.begin() as connection:
        with Operations.context(MigrationContext.configure(connection)):
            migration.op = Operations(MigrationContext.configure(connection))
            migration.upgrade()
        connection.execute(text(
            "INSERT INTO enterprise_project (id, enterprise_id, project_id) VALUES (10, 1, 100)"
        ))
        connection.execute(text(
            "INSERT INTO enterprise_agent_team (id, enterprise_id, conversation_id) VALUES (20, 1, 30)"
        ))
        connection.execute(text("INSERT INTO enterprise_agent_team_task (id, team_id) VALUES (40, 20)"))
        connection.execute(text(
            "INSERT INTO enterprise_security_artifact "
            "(id, team_id, task_id, enterprise_id, project_id, conversation_id, attempt_no, "
            "lease_generation, audit_run_id, source_sha256, report_sha256, coverage_status, "
            "termination_reason, summary_json, report_json) "
            "VALUES (50, 20, 40, 1, 100, 30, 1, 2, 'run-1', :hash, :hash, 'complete', '', '{}', '{}')"
        ), {"hash": "a" * 64})

    with engine.begin() as connection:
        with pytest.raises(IntegrityError, match="immutable"):
            connection.execute(text("UPDATE enterprise_security_artifact SET report_json = '{}' WHERE id = 50"))
    with engine.begin() as connection:
        with pytest.raises(IntegrityError, match="immutable"):
            connection.execute(text("DELETE FROM enterprise_security_artifact WHERE id = 50"))
    with engine.begin() as connection:
        with pytest.raises(IntegrityError):
            connection.execute(text(
                "INSERT INTO enterprise_security_artifact "
                "(id, team_id, task_id, enterprise_id, project_id, conversation_id, attempt_no, "
                "lease_generation, audit_run_id, source_sha256, report_sha256, coverage_status, "
                "termination_reason, summary_json, report_json) "
                "VALUES (51, 20, 40, 2, 100, 30, 2, 2, 'run-2', :hash, :hash, 'complete', '', '{}', '{}')"
            ), {"hash": "b" * 64})
    engine.dispose()
