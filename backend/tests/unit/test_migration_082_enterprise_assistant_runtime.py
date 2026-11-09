"""082 creates durable enterprise-scoped Responses checkpoints and approvals."""

import importlib.util
from pathlib import Path

import pytest
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect, text


def test_082_remains_in_the_single_migration_chain_and_links_to_081():
    backend = Path(__file__).resolve().parents[2]
    path = backend / "alembic/versions/082_enterprise_assistant_runtime.py"
    spec = importlib.util.spec_from_file_location("migration_082_enterprise_assistant_runtime", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    config = Config(str(backend / "alembic.ini"))
    config.set_main_option("script_location", str(backend / "alembic"))
    script = ScriptDirectory.from_config(config)
    assert len(script.get_heads()) == 1
    assert script.get_revision("088_enterprise_kb_attach").down_revision == "087_enterprise_remote_project"
    assert script.get_revision("087_enterprise_remote_project").down_revision == "086_server_tool_usage"
    assert module.down_revision == "081_remote_live_recheck"
    assert len(module.revision) <= 32
    assert len(module.down_revision) <= 32


def test_082_creates_tenant_checkpoint_and_exact_call_approval_tables():
    backend = Path(__file__).resolve().parents[2]
    path = backend / "alembic/versions/082_enterprise_assistant_runtime.py"
    spec = importlib.util.spec_from_file_location("migration_082_enterprise_assistant_runtime_apply", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE enterprise_assistant_run (id INTEGER PRIMARY KEY)"))
        module.op = Operations(MigrationContext.configure(connection))
        module.upgrade()
        inspector = inspect(connection)
        runtime_columns = {item["name"] for item in inspector.get_columns("enterprise_assistant_runtime_state")}
        approval_columns = {item["name"] for item in inspector.get_columns("enterprise_assistant_approval")}
        assert {
            "assistant_run_id", "enterprise_id", "conversation_id", "initiator_user_id", "source_message_id",
            "status", "checkpoint_json", "permission_snapshot_json", "permission_snapshot_sha256",
            "stop_requested", "lease_generation", "lease_expires_at", "billing_invocation_count",
        } <= runtime_columns
        assert {
            "assistant_run_id", "enterprise_id", "conversation_id", "initiator_user_id", "source_message_id",
            "call_id", "tool_name", "required_role", "status", "arguments_json", "arguments_sha256",
            "decided_by", "decided_at",
        } <= approval_columns
        assert {item["name"] for item in inspector.get_indexes("enterprise_assistant_runtime_state")} >= {
            "ix_ent_assistant_runtime_scope", "ix_ent_assistant_runtime_lease",
        }
        assert {item["name"] for item in inspector.get_indexes("enterprise_assistant_approval")} >= {
            "ix_ent_assistant_approval_queue", "ix_ent_assistant_approval_run",
        }
        with pytest.raises(RuntimeError, match="迁移|回退|删除"):
            module.downgrade()
    engine.dispose()
