"""Test 121 -> 122 preservation using a dedicated empty, loopback MySQL server."""

import os
import secrets
import subprocess
import sys
from datetime import timedelta

import pytest
import test_fresh_database_chain as chain
from sqlalchemy import create_engine, text
from test_mysql_enterprise_chat_authority import _seed

from app.models.billing_ledger import BillingReservation
from app.models.enterprise_assistant_runtime import EnterpriseAssistantRuntimeState
from app.models.enterprise_chat import EnterpriseAssistantRun
from app.services import enterprise_assistant_runtime_service as runtime

fresh_chain_url = chain.fresh_chain_url


_create_isolated_database = chain._create_isolated_database
_cleanup_isolated_database = chain._cleanup_isolated_database


def test_existing_121_run_and_unknown_charge_survive_forward_migration(fresh_chain_url):
    admin = chain._connect(fresh_chain_url)
    engine = None
    marker = None
    try:
        # CREATE without IF NOT EXISTS fails atomically even for an existing empty schema.
        marker = _create_isolated_database(admin)
        chain._execute_sql_file(admin, chain.INIT_SQL)
        chain._execute_sql_file(admin, chain.SEED_SQL)
        chain._alembic_upgrade(fresh_chain_url, "121_billing_model_evidence")
        engine = create_engine(fresh_chain_url.set(drivername="mysql+pymysql", database=chain.DATABASE))
        sessions, enterprise, group, _owner, _author, conversation, public = _seed(engine, include_prior_message=False)
        with sessions.begin() as db:
            run = db.query(EnterpriseAssistantRun).filter_by(public_id=public).one()
            state = db.query(EnterpriseAssistantRuntimeState).filter_by(assistant_run_id=run.id).one()
            state.lease_expires_at = runtime._now() + timedelta(minutes=5)
            state.status, run.status = "running", "running"
            row = BillingReservation(
                request_key=f"enterprise-assistant:{public}:old:1",
                model_name="test",
                requested_milli=1000,
                enterprise_id=enterprise,
                group_id=group,
                user_id=run.initiator_user_id,
                status="unknown",
                usage_state="pending",
            )
            db.add(row)
            db.flush()
            run_id, state_id, row_id = int(run.id), int(state.id), int(row.id)
            before = (state.lease_generation, state.checkpoint_json, row.status, row.actual_milli, row.request_key)
        chain._alembic_upgrade(fresh_chain_url, "122_assistant_call_owner")
        with sessions() as db:
            state = db.get(EnterpriseAssistantRuntimeState, state_id)
            row = db.get(BillingReservation, row_id)
            assert db.get(EnterpriseAssistantRun, run_id).status == "running"
            assert (
                state.lease_generation,
                state.checkpoint_json,
                row.status,
                row.actual_milli,
                row.request_key,
            ) == before
            assert db.execute(text("SELECT COUNT(*) FROM enterprise_assistant_run_owner")).scalar_one() == 0
            assert db.execute(text("SELECT COUNT(*) FROM enterprise_assistant_tool_owner")).scalar_one() == 0
        bootstrap = chain._ALEMBIC_BOOTSTRAP.replace('"upgrade", sys.argv[1]', '"downgrade", sys.argv[1]')
        assert bootstrap != chain._ALEMBIC_BOOTSTRAP
        env = {
            **os.environ,
            "DB_HOST": fresh_chain_url.host,
            "DB_PORT": str(fresh_chain_url.port or 3306),
            "DB_USER": fresh_chain_url.username,
            "DB_PASSWORD": fresh_chain_url.password or "",
            "DB_NAME": chain.DATABASE,
            "INITIAL_ADMIN_PASSWORD": secrets.token_urlsafe(18),
        }
        attempted = subprocess.run(
            [sys.executable, "-I", "-X", "utf8", "-c", bootstrap, "121_billing_model_evidence"],
            cwd=chain.BACKEND,
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        assert attempted.returncode != 0
        assert "执行所有权证据不能自动降级" in attempted.stderr
        with sessions() as db:
            assert (
                db.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == "122_assistant_call_owner"
            )
            assert db.get(BillingReservation, row_id).status == "unknown"
            assert db.execute(text("SELECT COUNT(*) FROM enterprise_assistant_tool_owner")).scalar_one() == 0
    finally:
        if engine is not None:
            engine.dispose()
        _cleanup_isolated_database(admin, marker)
        admin.close()


@pytest.mark.parametrize("failed_operation", ["CREATE DATABASE", "CREATE TABLE", "INSERT INTO"])
def test_safety_create_or_marker_failure_never_drops_existing_schema(monkeypatch, failed_operation):
    statements = []

    class Cursor:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, sql, *_args):
            statements.append(sql)
            if sql.startswith(failed_operation):
                raise RuntimeError("simulated schema conflict or setup failure")

    class Connection:
        def cursor(self):
            return Cursor()

        def close(self):
            pass

    monkeypatch.setattr(chain, "_connect", lambda *_args, **_kwargs: Connection())
    with pytest.raises(RuntimeError, match="simulated schema conflict"):
        test_existing_121_run_and_unknown_charge_survive_forward_migration(object())
    assert not any(statement.startswith("DROP DATABASE") for statement in statements)


def test_safety_cleanup_must_verify_exact_marker_before_drop():
    statements = []

    class Cursor:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, sql, *_args):
            statements.append(sql)

        def fetchall(self):
            return (("some-other-owner",),)

    class Connection:
        def cursor(self):
            return Cursor()

    _cleanup_isolated_database(Connection(), "our-marker")
    assert not any(statement.startswith("DROP DATABASE") for statement in statements)


def test_safety_shared_fixture_refuses_existing_schema_without_drop(monkeypatch):
    statements = []

    class Cursor:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, sql, *_args):
            statements.append(sql)
            raise RuntimeError("schema already exists")

    class Connection:
        def cursor(self):
            return Cursor()

        def close(self):
            pass

    monkeypatch.setattr(chain, "_connect", lambda *_args, **_kwargs: Connection())
    fixture = chain.fresh_chain.__wrapped__(object())
    with pytest.raises(RuntimeError, match="schema already exists"):
        next(fixture)
    assert not any(statement.startswith("DROP DATABASE") for statement in statements)
