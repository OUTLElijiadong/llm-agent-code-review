"""077 退款表、066–081 前向数据迁移保护。"""

import importlib.util
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect, text


def test_migration_adds_refund_audit_table_and_credit_lock_column():
    path = Path(__file__).resolve().parents[2] / "alembic/versions/077_payment_refunds.py"
    spec = importlib.util.spec_from_file_location("migration_077_payment_refunds", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.down_revision == "076_reviewer_legacy_entitlements"

    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE billing_credit_batch (id INTEGER PRIMARY KEY, remaining_milli BIGINT)"))
        module.op = Operations(MigrationContext.configure(connection))
        module.upgrade()
        columns = {column["name"]: column for column in inspect(connection).get_columns("billing_credit_batch")}
        assert columns["refund_reserved_milli"]["nullable"] is False
        assert "billing_refund" in inspect(connection).get_table_names()
        unique_sets = inspect(connection).get_unique_constraints("billing_refund")
        assert {tuple(item["column_names"]) for item in unique_sets} == {("order_no",), ("refund_no",)}
    engine.dispose()


@pytest.mark.parametrize("revision", [
    "066_enterprise_org", "067_billing_ledger", "068_enterprise_chat", "069_enterprise_project",
    "070_billing_subscription", "071_permanent_reviewer", "072_enterprise_knowledge",
    "073_enterprise_asset_drafts", "074_enterprise_remote_approval", "075_payment_checkout_state",
    "076_reviewer_legacy_entitlements", "077_payment_refunds", "078_enterprise_remote_execution",
    "079_enterprise_assistant_runs", "080_assistant_recovery", "081_remote_enterprise_live_recheck",
])
def test_business_data_migrations_refuse_silent_alembic_downgrade(revision):
    path = Path(__file__).resolve().parents[2] / f"alembic/versions/{revision}.py"
    spec = importlib.util.spec_from_file_location(f"migration_{revision}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    with pytest.raises(RuntimeError, match="回退|降级|恢复"):
        module.downgrade()
