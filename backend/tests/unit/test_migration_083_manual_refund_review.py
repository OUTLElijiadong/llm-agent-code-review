"""083 keeps billing suspension separate from enterprise lifecycle status."""

import importlib.util
from pathlib import Path

import pytest
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError


def _migration():
    backend = Path(__file__).resolve().parents[2]
    path = backend / "alembic/versions/083_manual_refund_review.py"
    spec = importlib.util.spec_from_file_location("migration_083_manual_refund_review", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return backend, module


def test_083_remains_in_the_single_chain_and_links_to_082():
    backend, module = _migration()
    config = Config(str(backend / "alembic.ini"))
    config.set_main_option("script_location", str(backend / "alembic"))

    script = ScriptDirectory.from_config(config)
    heads = script.get_heads()
    assert heads == ["118_clarification_authority"]
    assert any(revision.revision == module.revision for revision in script.iterate_revisions(heads, "base"))
    assert script.get_revision("088_enterprise_kb_attach").down_revision == "087_enterprise_remote_project"
    assert script.get_revision("087_enterprise_remote_project").down_revision == "086_server_tool_usage"
    assert script.get_revision("086_server_tool_usage").down_revision == "085_ent_asset_invocations"
    assert script.get_revision("085_ent_asset_invocations").down_revision == "084_ent_asset_publications"
    assert script.get_revision(module.revision).down_revision == "082_enterprise_assistant_runtime"
    assert module.down_revision == "082_enterprise_assistant_runtime"
    assert len(module.revision) <= 32
    assert len(module.down_revision) <= 32


def test_083_adds_default_unsuspended_billing_flag_and_refuses_downgrade():
    _, module = _migration()
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE enterprise (id INTEGER PRIMARY KEY)"))
        module.op = Operations(MigrationContext.configure(connection))
        module.upgrade()

        column = next(item for item in inspect(connection).get_columns("enterprise")
                      if item["name"] == "billing_suspended")
        assert not column["nullable"]
        assert str(column["default"]).strip("()'") in {"0", "false", "FALSE"}
        assert {item["name"] for item in inspect(connection).get_columns("enterprise")} >= {
            "id", "billing_suspended",
        }
        with pytest.raises(RuntimeError, match="迁移|回退|删除"):
            module.downgrade()
    engine.dispose()


def test_086_adds_nullable_search_fetch_audit_counts_and_refuses_downgrade():
    backend = Path(__file__).resolve().parents[2]
    path = backend / "alembic/versions/086_server_tool_usage_audit.py"
    spec = importlib.util.spec_from_file_location("migration_086_server_tool_usage_audit", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE billing_reservation (id INTEGER PRIMARY KEY)"))
        module.op = Operations(MigrationContext.configure(connection))
        module.upgrade()
        columns = {item["name"]: item for item in inspect(connection).get_columns("billing_reservation")}
        assert {"id", "server_tool_search_calls", "server_tool_fetch_calls"} <= set(columns)
        assert columns["server_tool_search_calls"]["nullable"]
        assert columns["server_tool_fetch_calls"]["nullable"]
        checks = {item["name"] for item in inspect(connection).get_check_constraints("billing_reservation")}
        assert "ck_billing_reservation_server_tool_usage_pair" in checks
        with pytest.raises(IntegrityError):
            connection.execute(text(
                "INSERT INTO billing_reservation (id, server_tool_search_calls) VALUES (1, 1)"
            ))
        with pytest.raises(RuntimeError, match="对账|删除|恢复"):
            module.downgrade()
    engine.dispose()
