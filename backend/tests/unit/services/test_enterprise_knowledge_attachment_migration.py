"""企业知识附件迁移只增企业表，并与 ORM 字段和 MySQL 存储类型对齐。"""

import importlib.util
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect
from sqlalchemy.dialects import mysql
from sqlalchemy.schema import CreateTable

from app.models.enterprise_knowledge import EnterpriseKnowledgeAttachment, EnterpriseKnowledgeAttachmentChunk


def _migration():
    path = Path(__file__).resolve().parents[3] / "alembic/versions/088_enterprise_knowledge_attachments.py"
    spec = importlib.util.spec_from_file_location("migration_088_enterprise_knowledge_attachments", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_attachment_migration_creates_exact_tables_columns_and_indexes():
    module = _migration()
    assert module.down_revision == "087_enterprise_remote_project"
    assert len(module.revision) <= 32  # Alembic's default version_num is VARCHAR(32).
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as connection:
        with Operations.context(MigrationContext.configure(connection)):
            module.upgrade()
        inspector = inspect(connection)
        assert set(inspector.get_table_names()) == {
            "enterprise_knowledge_attachment", "enterprise_knowledge_attachment_chunk",
        }
        for model in (EnterpriseKnowledgeAttachment, EnterpriseKnowledgeAttachmentChunk):
            table = model.__table__
            assert {column["name"] for column in inspector.get_columns(table.name)} == set(table.columns.keys())
            assert {index["name"] for index in inspector.get_indexes(table.name)} == {
                index.name for index in table.indexes
            }
    engine.dispose()


def test_attachment_mysql_storage_is_medium_and_downgrade_refuses_data_loss():
    original = str(CreateTable(EnterpriseKnowledgeAttachment.__table__).compile(dialect=mysql.dialect()))
    chunks = str(CreateTable(EnterpriseKnowledgeAttachmentChunk.__table__).compile(dialect=mysql.dialect()))
    assert "MEDIUMBLOB" in original and "MEDIUMTEXT" in original
    assert "uq_enterprise_knowledge_attachment_create" in original
    assert "uq_enterprise_knowledge_attachment_chunk_seq" in chunks
    with pytest.raises(RuntimeError, match="禁止将 Alembic 版本号单独回退"):
        _migration().downgrade()
