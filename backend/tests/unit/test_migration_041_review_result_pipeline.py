"""Migration 041 adds reversible review result provenance metadata."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

MIGRATION_PATH = (
    Path(__file__).parents[2]
    / "alembic"
    / "versions"
    / "041_review_result_pipeline.py"
)


def _load_migration():
    spec = importlib.util.spec_from_file_location("migration_041_review_result_pipeline", MIGRATION_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_review_result_pipeline_migration_is_reversible(monkeypatch) -> None:
    engine = sa.create_engine("sqlite:///:memory:")
    metadata = sa.MetaData()
    sa.Table(
        "review_issue",
        metadata,
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("task_id", sa.Integer, nullable=False),
    )
    sa.Table(
        "review_task",
        metadata,
        sa.Column("id", sa.Integer, primary_key=True),
    )
    metadata.create_all(engine)
    migration = _load_migration()

    with engine.begin() as connection:
        operations = Operations(MigrationContext.configure(connection))
        monkeypatch.setattr(migration, "op", operations)
        migration.upgrade()

        inspector = sa.inspect(connection)
        issue_columns = {column["name"] for column in inspector.get_columns("review_issue")}
        task_columns = {column["name"] for column in inspector.get_columns("review_task")}
        indexes = {index["name"] for index in inspector.get_indexes("review_issue")}
        assert {
            "source_details",
            "confirmation_count",
            "finding_fingerprint",
            "cvss_version",
            "cvss_source",
        } <= issue_columns
        assert {"score_version", "score_breakdown"} <= task_columns
        assert "ix_review_issue_task_fingerprint" in indexes

        migration.downgrade()

        inspector = sa.inspect(connection)
        issue_columns = {column["name"] for column in inspector.get_columns("review_issue")}
        task_columns = {column["name"] for column in inspector.get_columns("review_task")}
        assert "source_details" not in issue_columns
        assert "score_breakdown" not in task_columns


def test_review_result_pipeline_revision_follows_actual_040_revision() -> None:
    migration = _load_migration()

    assert migration.revision == "041_review_result_pipeline"
    assert migration.down_revision == "040"
