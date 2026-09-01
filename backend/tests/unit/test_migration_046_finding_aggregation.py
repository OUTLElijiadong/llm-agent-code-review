"""Migration 046 adds reversible finding aggregation metadata."""

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
    / "046_finding_aggregation.py"
)


def _load_migration():
    spec = importlib.util.spec_from_file_location("migration_046_finding_aggregation", MIGRATION_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_finding_aggregation_migration_is_reversible(monkeypatch) -> None:
    engine = sa.create_engine("sqlite:///:memory:")
    metadata = sa.MetaData()
    sa.Table(
        "review_issue",
        metadata,
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("task_id", sa.Integer, nullable=False),
    )
    metadata.create_all(engine)
    migration = _load_migration()

    with engine.begin() as connection:
        operations = Operations(MigrationContext.configure(connection))
        monkeypatch.setattr(migration, "op", operations)
        migration.upgrade()

        columns = {column["name"] for column in sa.inspect(connection).get_columns("review_issue")}
        assert {
            "aggregation_version",
            "evidence_quality",
            "conflict_status",
            "human_review_status",
            "risk_score",
            "aggregation_json",
        } <= columns

        migration.downgrade()
        columns = {column["name"] for column in sa.inspect(connection).get_columns("review_issue")}
        assert "aggregation_json" not in columns


def test_revision_follows_current_head() -> None:
    migration = _load_migration()
    assert migration.revision == "046_finding_aggregation"
    assert migration.down_revision == "045_skill_asset_user_grant"
