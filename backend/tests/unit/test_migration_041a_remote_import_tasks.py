"""041a 可恢复远程导入任务表迁移契约。"""

import importlib.util
from pathlib import Path

from alembic.operations import Operations
from alembic.runtime.migration import MigrationContext
from sqlalchemy import create_engine, inspect


def _migration_module():
    path = Path(__file__).resolve().parents[2] / "alembic/versions/041a_remote_import_tasks.py"
    spec = importlib.util.spec_from_file_location("migration_041a_remote_import_tasks", path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


def test_remote_import_migration_is_explicitly_based_on_current_040_head() -> None:
    module = _migration_module()

    assert module.revision == "041a_remote_import_tasks"
    assert module.down_revision == "040"


def test_upgrade_creates_import_task_table_with_queue_and_idempotency_indexes() -> None:
    module = _migration_module()
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as connection:
        context = MigrationContext.configure(connection)
        Operations.context = context
        module.op = Operations(context)
        module.upgrade()

    inspector = inspect(engine)
    columns = {item["name"] for item in inspector.get_columns("project_import_task")}
    assert {
        "public_id",
        "user_id",
        "idempotency_key_hash",
        "request_fingerprint",
        "request_json",
        "status",
        "attempt_count",
        "max_attempts",
        "next_attempt_at",
        "lease_token",
        "lease_expires_at",
        "project_id",
        "result_json",
        "error_code",
        "error_message",
    } <= columns
    indexes = {item["name"] for item in inspector.get_indexes("project_import_task")}
    assert "ix_project_import_task_queue" in indexes
    assert "ix_project_import_task_owner_status" in indexes
