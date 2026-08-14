"""031 Agent Mesh 消费租约迁移测试。"""

import importlib.util
from pathlib import Path
from types import SimpleNamespace


def _migration():
    path = Path(__file__).resolve().parents[2] / "alembic/versions/031_agent_mesh_dispatch_lease.py"
    spec = importlib.util.spec_from_file_location("migration_031_agent_mesh_dispatch_lease", path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


def test_upgrade_and_downgrade_manage_dispatch_lease_columns(monkeypatch):
    migration = _migration()
    added = []
    dropped = []
    fake_op = SimpleNamespace(
        add_column=lambda table, column: added.append((table, column.name)),
        drop_column=lambda table, column: dropped.append((table, column)),
    )
    monkeypatch.setattr(migration, "op", fake_op)

    migration.upgrade()
    assert added == [
        ("agent_mesh_message", "lease_token"),
        ("agent_mesh_message", "lease_expires_at"),
        ("agent_mesh_message", "next_attempt_at"),
    ]

    migration.downgrade()
    assert dropped == [
        ("agent_mesh_message", "next_attempt_at"),
        ("agent_mesh_message", "lease_expires_at"),
        ("agent_mesh_message", "lease_token"),
    ]
