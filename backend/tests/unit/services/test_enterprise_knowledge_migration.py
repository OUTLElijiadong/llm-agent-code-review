"""企业知识库迁移保持 ORM 一致且不改动个人知识库。"""

import importlib.util
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect
from sqlalchemy.dialects import mysql
from sqlalchemy.schema import CreateTable

from app.models.enterprise_knowledge import EnterpriseKnowledgeEntry


def test_enterprise_knowledge_migration_creates_only_enterprise_table():
    path = Path(__file__).resolve().parents[3] / "alembic/versions/072_enterprise_knowledge.py"
    spec = importlib.util.spec_from_file_location("migration_072_enterprise_knowledge", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.down_revision == "071_permanent_reviewer"
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as connection:
        with Operations.context(MigrationContext.configure(connection)):
            module.upgrade()
        inspector = inspect(connection)
        assert inspector.get_table_names() == ["enterprise_knowledge_entry"]
        assert {col["name"] for col in inspector.get_columns("enterprise_knowledge_entry")} == set(
            EnterpriseKnowledgeEntry.__table__.columns.keys()
        )
        assert {idx["name"] for idx in inspector.get_indexes("enterprise_knowledge_entry")} == {
            "ix_enterprise_knowledge_scope"
        }
    engine.dispose()


def test_enterprise_knowledge_mysql_table_uses_mediumtext():
    ddl = str(CreateTable(EnterpriseKnowledgeEntry.__table__).compile(dialect=mysql.dialect()))
    assert "MEDIUMTEXT" in ddl
    assert "uq_enterprise_knowledge_create" in ddl
