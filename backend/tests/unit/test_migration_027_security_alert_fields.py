"""Migration 027 agent_alert 安全监控字段回归测试。

验证：
1. revision/down_revision 正确（027 → 026）
2. upgrade() 在 SQLite 上为 agent_alert 增加 5 列与 2 个索引（幂等）
3. downgrade() 可完整回滚
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations


def _migration_module():
    path = (
        Path(__file__).resolve().parents[2]
        / "alembic"
        / "versions"
        / "027_add_security_alert_fields.py"
    )
    spec = importlib.util.spec_from_file_location("migration_027", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _create_agent_alert(engine):
    """按 026 之前的结构建 agent_alert 表（含既有索引）。"""
    metadata = sa.MetaData()
    sa.Table(
        "agent_alert",
        metadata,
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("alert_type", sa.String(80), nullable=False),
        sa.Column("severity", sa.String(30), nullable=False),
        sa.Column("status", sa.String(30), nullable=False),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("detail_json", sa.Text, nullable=True),
        sa.Column("resolved_by", sa.BigInteger, nullable=True),
        sa.Column("resolved_at", sa.DateTime, nullable=True),
        sa.Column("create_time", sa.DateTime, nullable=False),
        sa.Column("update_time", sa.DateTime, nullable=False),
        sa.Index("ix_agent_alert_status", "status"),
        sa.Index("ix_agent_alert_severity", "severity"),
    )
    metadata.create_all(engine)
    return metadata


def test_migration_027_metadata() -> None:
    module = _migration_module()
    assert module.revision == "027"
    assert module.down_revision == "026"


def _run_with_operations(module, conn, fn) -> None:
    """在真实 Alembic MigrationContext 上执行迁移函数（支持 batch_alter_table）。"""
    ctx = MigrationContext.configure(conn)
    op = Operations(ctx)
    original_op = module.op
    module.op = op
    try:
        fn()
    finally:
        module.op = original_op


def test_migration_027_upgrade_and_downgrade_sqlite(monkeypatch) -> None:
    module = _migration_module()
    engine = sa.create_engine("sqlite:///:memory:")
    _create_agent_alert(engine)

    def _columns(conn):
        return {item["name"] for item in sa.inspect(conn).get_columns("agent_alert")}

    def _indexes(conn):
        return {item["name"] for item in sa.inspect(conn).get_indexes("agent_alert")}

    with engine.begin() as conn:
        assert "category" not in _columns(conn)

        # 幂等 upgrade
        _run_with_operations(module, conn, module.upgrade)
        _run_with_operations(module, conn, module.upgrade)
        assert {"category", "source", "user_id", "read_at", "fingerprint"} <= _columns(conn)
        assert "ix_agent_alert_user_read" in _indexes(conn)
        assert "ix_agent_alert_fingerprint" in _indexes(conn)
        # 既有索引保留
        assert "ix_agent_alert_status" in _indexes(conn)
        assert "ix_agent_alert_severity" in _indexes(conn)

        # 回滚
        _run_with_operations(module, conn, module.downgrade)
        assert {"category", "source", "user_id", "read_at", "fingerprint"} & _columns(conn) == set()
        assert "ix_agent_alert_user_read" not in _indexes(conn)
        assert "ix_agent_alert_fingerprint" not in _indexes(conn)

    engine.dispose()


def test_agent_alert_model_has_security_fields() -> None:
    from app.models.agent_governance import AgentAlert

    assert AgentAlert.__table__.c.category is not None
    assert AgentAlert.__table__.c.source is not None
    assert AgentAlert.__table__.c.user_id is not None
    assert AgentAlert.__table__.c.read_at is not None
    assert AgentAlert.__table__.c.fingerprint is not None
    index_names = {index.name for index in AgentAlert.__table__.indexes}
    assert "ix_agent_alert_user_read" in index_names
    assert "ix_agent_alert_fingerprint" in index_names
