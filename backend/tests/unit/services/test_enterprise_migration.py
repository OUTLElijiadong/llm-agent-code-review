"""在独立 SQLite 中执行迁移并核对企业模型字段。"""
import importlib.util
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect

from app.models.enterprise_org import (
    Enterprise,
    EnterpriseAuditEvent,
    EnterpriseGroup,
    EnterpriseGroupMember,
    EnterpriseInvitation,
    EnterpriseMember,
)


def test_enterprise_migration_creates_declared_tables():
    path = Path(__file__).resolve().parents[3] / "alembic/versions/066_enterprise_org.py"
    spec = importlib.util.spec_from_file_location("migration_066_enterprise", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.down_revision == "065_review_rule_lang_severity"
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as connection:
        with Operations.context(MigrationContext.configure(connection)):
            module.upgrade()
        inspector = inspect(connection)
        for model in (Enterprise, EnterpriseMember, EnterpriseGroup, EnterpriseGroupMember,
                      EnterpriseInvitation, EnterpriseAuditEvent):
            columns = {column["name"] for column in inspector.get_columns(model.__tablename__)}
            current_columns = set(model.__table__.columns.keys())
            # 066 is historical; later migrations add billing and proactive-chat fields.
            if model is Enterprise:
                current_columns.difference_update({
                    "billing_suspended", "proactive_enabled", "proactive_monthly_credits",
                    "proactive_version", "proactive_day", "proactive_daily_count",
                    "clarification_authority_version",
                })
            elif model is EnterpriseMember:
                current_columns.difference_update({"billing_group_version", "clarification_authority_version"})
            elif model is EnterpriseGroupMember:
                current_columns.discard("clarification_authority_version")
            assert columns == current_columns
    engine.dispose()
