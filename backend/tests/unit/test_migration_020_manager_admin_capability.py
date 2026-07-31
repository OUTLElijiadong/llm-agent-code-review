"""020 manager capability data migration regression tests."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import sqlalchemy as sa


def _migration_module():
    path = Path(__file__).resolve().parents[2] / "alembic" / "versions" / "020_manager_admin_capability_contract.py"
    spec = importlib.util.spec_from_file_location("migration_020", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_migration_020_revision_and_exact_config_round_trip():
    migration = _migration_module()
    original = {
        "governance_boundary": {
            "scope": "existing",
            "allowed_tools": ["governance_reader"],
            "approval_tools": ["admin_execute_capability", "workflow_dispatch"],
            "blocked_tools": ["admin_execute_capability", "shell"],
        },
        "governance_boundary_version": "existing_v2",
        "custom": {"keep": [1, 2, 3]},
    }

    upgraded = migration._upgrade_config(original)

    assert migration.revision == "020"
    assert migration.down_revision == "019"
    assert upgraded["governance_boundary"]["allowed_tools"] == [
        "admin_execute_capability",
        "governance_reader",
    ]
    assert upgraded["governance_boundary"]["approval_tools"] == ["workflow_dispatch"]
    assert upgraded["governance_boundary"]["blocked_tools"] == ["shell"]
    assert migration._downgrade_config(upgraded) == original


def test_migration_020_is_idempotent_and_restores_missing_boundary():
    migration = _migration_module()
    original = {"custom": True}
    once = migration._upgrade_config(original)
    twice = migration._upgrade_config(once)

    assert twice == once
    assert migration._downgrade_config(twice) == original


def test_migration_020_downgrade_cleans_profile_created_after_upgrade():
    migration = _migration_module()
    created_after_upgrade = {
        "governance_boundary": {
            "scope": "new manager",
            "allowed_tools": ["admin_execute_capability", "governance_reader"],
            "approval_tools": [],
            "blocked_tools": ["shell"],
        },
        "manager_admin_capability_boundary_version": "manager_admin_capability_v1",
        "custom": True,
    }

    downgraded = migration._downgrade_config(created_after_upgrade)

    assert downgraded == {
        "governance_boundary": {
            "scope": "new manager",
            "allowed_tools": ["governance_reader"],
            "approval_tools": [],
            "blocked_tools": ["shell"],
        },
        "custom": True,
    }


def test_migration_020_executes_sql_and_restores_existing_rows(monkeypatch):
    migration = _migration_module()
    engine = sa.create_engine("sqlite:///:memory:")
    metadata = sa.MetaData()
    profile = sa.Table(
        "agent_profile",
        metadata,
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("code", sa.String(80), nullable=False),
        sa.Column("config_json", sa.Text),
        sa.Column("update_time", sa.DateTime, server_default=sa.func.now()),
    )
    permission = sa.Table(
        "agent_tool_permission",
        metadata,
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("agent_code", sa.String(80), nullable=False),
        sa.Column("tool_code", sa.String(120), nullable=False),
        sa.Column("permission", sa.String(30), nullable=False),
        sa.Column("risk_level", sa.String(30), nullable=False),
        sa.Column("enabled", sa.SmallInteger, nullable=False),
        sa.Column("note", sa.String(300)),
    )
    metadata.create_all(engine)
    original = {
        "governance_boundary": {
            "scope": "existing",
            "allowed_tools": ["governance_reader"],
            "approval_tools": ["workflow_dispatch"],
            "blocked_tools": ["shell"],
        },
        "custom": "preserved",
    }

    with engine.begin() as conn:
        conn.execute(
            profile.insert().values(
                id=1,
                code="manager",
                config_json=json.dumps(original, ensure_ascii=False),
            )
        )
        monkeypatch.setattr(migration.op, "get_bind", lambda: conn)

        migration.upgrade()
        migration.upgrade()
        upgraded = json.loads(
            conn.execute(sa.select(profile.c.config_json).where(profile.c.id == 1)).scalar_one()
        )
        permissions = conn.execute(sa.select(permission)).mappings().all()
        assert "admin_execute_capability" in upgraded["governance_boundary"]["allowed_tools"]
        assert len(permissions) == 1
        assert permissions[0]["permission"] == "allow"

        migration.downgrade()
        restored = json.loads(
            conn.execute(sa.select(profile.c.config_json).where(profile.c.id == 1)).scalar_one()
        )
        assert restored == original
        assert conn.execute(sa.select(sa.func.count()).select_from(permission)).scalar_one() == 0

    engine.dispose()
