"""Migration 026 capacity regression tests."""

import importlib.util
from pathlib import Path

import sqlalchemy as sa
from sqlalchemy.dialects import mysql


def _load_module():
    path = Path(__file__).resolve().parents[2] / "alembic/versions/026_expand_source_binary_capacity.py"
    spec = importlib.util.spec_from_file_location("migration_026", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_migration_026_mysql_types_expand_binary_capacity() -> None:
    module = _load_module()
    assert str(mysql.LONGBLOB().compile(dialect=mysql.dialect())).upper() == "LONGBLOB"
    assert str(mysql.BIGINT().compile(dialect=mysql.dialect())).upper() == "BIGINT"
    assert module.revision == "026"
    assert module.down_revision == "025"


def test_code_file_metadata_uses_longblob_and_bigint() -> None:
    from app.models.code_file import CodeFile
    from app.models.project_source_archive import ProjectSourceArchive

    dialect = mysql.dialect()
    assert str(CodeFile.__table__.c.original_blob.type.compile(dialect=dialect)).upper() == "LONGBLOB"
    assert isinstance(CodeFile.__table__.c.size_bytes.type, sa.BigInteger)
    assert isinstance(CodeFile.__table__.c.raw_size.type, sa.BigInteger)
    assert isinstance(ProjectSourceArchive.__table__.c.compressed_size.type, sa.BigInteger)
