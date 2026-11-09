"""企业项目迁移结构必须与 ORM 保持一致。"""
import importlib.util
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect

from app.models.enterprise_project import EnterpriseProject, EnterpriseProjectGrant


def test_enterprise_project_migration_creates_expected_columns():
    path = Path(__file__).resolve().parents[3] / "alembic/versions/069_enterprise_project.py"
    spec = importlib.util.spec_from_file_location("migration_069_enterprise_project", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.down_revision == "068_enterprise_chat"
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as connection:
        with Operations.context(MigrationContext.configure(connection)):
            module.upgrade()
        inspector = inspect(connection)
        for model in (EnterpriseProject, EnterpriseProjectGrant):
            columns = {column["name"] for column in inspector.get_columns(model.__tablename__)}
            assert columns == set(model.__table__.columns.keys())
    engine.dispose()
