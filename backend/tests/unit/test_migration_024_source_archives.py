"""024 隔离源码归档迁移的数据保护与 MySQL 类型回归。"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.dialects import mysql
from sqlalchemy.schema import CreateTable


def _migration_module():
    path = Path(__file__).resolve().parents[2] / "alembic/versions/024_quarantined_source_archives.py"
    spec = importlib.util.spec_from_file_location("migration_024", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _install_operations(module, connection) -> None:
    module.op = Operations(MigrationContext.configure(connection))


def test_migration_024_nonempty_archive_table_refuses_downgrade() -> None:
    module = _migration_module()
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as connection:
        _install_operations(module, connection)
        module.upgrade()

        columns = {item["name"] for item in inspect(connection).get_columns("project_source_archive")}
        assert {
            "project_id",
            "archive_sha256",
            "scan_summary_json",
            "archive_blob",
            "audit_result_json",
            "audit_run_id",
            "audit_started_at",
            "audit_heartbeat_at",
            "audit_completed_at",
        } <= columns
        connection.execute(
            text(
                """
                INSERT INTO project_source_archive (
                    project_id, owner_id, original_filename, media_type,
                    archive_sha256, compressed_size, expanded_size, file_count,
                    max_member_size, max_compression_ratio, storage_status,
                    malware_status, audit_status, threat_count,
                    scan_summary_json, archive_blob
                ) VALUES (
                    7, 3, 'source.zip', 'application/zip',
                    :digest, 4, 4, 1,
                    4, 1, 'active',
                    'clean', 'not_started', 0,
                    '{}', :archive_blob
                )
                """
            ),
            {"digest": "a" * 64, "archive_blob": b"PK00"},
        )

        with pytest.raises(RuntimeError, match="表非空|拒绝降级"):
            module.downgrade()

        assert "project_source_archive" in inspect(connection).get_table_names()
        assert connection.execute(text("SELECT COUNT(*) FROM project_source_archive")).scalar_one() == 1
    engine.dispose()


def test_migration_024_empty_archive_table_can_downgrade() -> None:
    module = _migration_module()
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as connection:
        _install_operations(module, connection)
        module.upgrade()
        assert "project_source_archive" in inspect(connection).get_table_names()

        module.downgrade()

        assert "project_source_archive" not in inspect(connection).get_table_names()
    engine.dispose()


def test_migration_024_mysql_archive_ddl_uses_longblob() -> None:
    module = _migration_module()
    assert str(module.LONG_BLOB.compile(dialect=mysql.dialect())).upper() == "LONGBLOB"

    metadata = sa.MetaData()
    table = sa.Table(
        "project_source_archive",
        metadata,
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("scan_summary_json", module.LONG_TEXT, nullable=False),
        sa.Column("audit_result_json", module.LONG_TEXT, nullable=True),
        sa.Column("archive_blob", module.LONG_BLOB, nullable=False),
    )
    ddl = " ".join(str(CreateTable(table).compile(dialect=mysql.dialect())).upper().split())

    assert "ID BIGINT NOT NULL AUTO_INCREMENT" in ddl
    assert "SCAN_SUMMARY_JSON LONGTEXT NOT NULL" in ddl
    assert "AUDIT_RESULT_JSON LONGTEXT" in ddl
    assert "ARCHIVE_BLOB LONGBLOB NOT NULL" in ddl
