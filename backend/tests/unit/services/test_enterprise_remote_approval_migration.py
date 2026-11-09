"""074 migration creates the isolated approval schema and keeps its predecessor."""
import importlib.util
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect

from app.models.enterprise_remote_approval import EnterpriseRemoteApproval


def test_remote_approval_migration_matches_model_and_is_forward_only():
    path = Path(__file__).resolve().parents[3] / "alembic/versions/074_enterprise_remote_approval.py"
    spec = importlib.util.spec_from_file_location("migration_enterprise_remote", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    assert migration.down_revision == "073_enterprise_asset_drafts"
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as connection:
        migration.op = Operations(MigrationContext.configure(connection))
        migration.upgrade()
        inspector = inspect(connection)
        actual = {col["name"] for col in inspector.get_columns("enterprise_remote_approval")}
        assert actual == set(EnterpriseRemoteApproval.__table__.columns.keys()) - {
            "execution_task_public_id", "project_id",
        }
        assert {idx["name"] for idx in inspector.get_indexes("enterprise_remote_approval")} == {
            "ix_enterprise_remote_approval_status", "ix_enterprise_remote_approval_group",
            "ix_enterprise_remote_approval_owner",
        }
        with pytest.raises(RuntimeError):
            migration.downgrade()
    engine.dispose()


def test_enterprise_remote_project_migration_adds_nullable_compatibility_columns():
    base_path = Path(__file__).resolve().parents[3] / "alembic/versions"
    first_spec = importlib.util.spec_from_file_location(
        "migration_074_enterprise_remote_project", base_path / "074_enterprise_remote_approval.py",
    )
    first = importlib.util.module_from_spec(first_spec)
    first_spec.loader.exec_module(first)
    link_spec = importlib.util.spec_from_file_location(
        "migration_078_enterprise_remote_project", base_path / "078_enterprise_remote_execution.py",
    )
    link = importlib.util.module_from_spec(link_spec)
    link_spec.loader.exec_module(link)
    path = base_path / "087_enterprise_remote_project.py"
    spec = importlib.util.spec_from_file_location("migration_087_enterprise_remote_project", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    assert migration.down_revision == "086_server_tool_usage"

    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as connection:
        context = MigrationContext.configure(connection)
        first.op = Operations(context)
        first.upgrade()
        link.op = Operations(context)
        link.upgrade()
        Operations(context).create_table(
            "remote_pentest_engagement",
            sa.Column("id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), primary_key=True),
            sa.Column("owner_id", sa.BigInteger(), nullable=False),
        )
        migration.op = Operations(context)
        migration.upgrade()

        inspector = inspect(connection)
        approval_columns = {item["name"]: item for item in inspector.get_columns("enterprise_remote_approval")}
        execution_columns = {item["name"]: item for item in inspector.get_columns("remote_pentest_engagement")}
        assert approval_columns["project_id"]["nullable"] is True
        assert execution_columns["project_id"]["nullable"] is True
        assert "ix_enterprise_remote_approval_project" in {
            item["name"] for item in inspector.get_indexes("enterprise_remote_approval")
        }
        assert {item["name"] for item in inspector.get_indexes("enterprise_remote_approval")} == {
            item.name for item in EnterpriseRemoteApproval.__table__.indexes
        }
        assert "ix_remote_pentest_project" in {
            item["name"] for item in inspector.get_indexes("remote_pentest_engagement")
        }
        with pytest.raises(RuntimeError):
            migration.downgrade()
    engine.dispose()


def test_execution_link_migration_adds_unique_nullable_task_reference():
    base_path = Path(__file__).resolve().parents[3] / "alembic/versions"
    previous_spec = importlib.util.spec_from_file_location("migration_074_enterprise_remote", base_path /
                                                            "074_enterprise_remote_approval.py")
    previous = importlib.util.module_from_spec(previous_spec)
    previous_spec.loader.exec_module(previous)
    path = base_path / "078_enterprise_remote_execution.py"
    spec = importlib.util.spec_from_file_location("migration_078_enterprise_remote_execution", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    assert migration.down_revision == "077_payment_refunds"

    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as connection:
        context = MigrationContext.configure(connection)
        previous.op = Operations(context)
        previous.upgrade()
        migration.op = Operations(context)
        migration.upgrade()
        inspector = inspect(connection)
        columns = {item["name"] for item in inspector.get_columns("enterprise_remote_approval")}
        assert "execution_task_public_id" in columns
        unique = {
            tuple(item["column_names"])
            for item in inspector.get_unique_constraints("enterprise_remote_approval")
        }
        assert ("execution_task_public_id",) in unique
        with pytest.raises(RuntimeError):
            migration.downgrade()
    engine.dispose()
