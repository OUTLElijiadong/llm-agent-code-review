import importlib.util
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError


def _migration(name: str):
    backend = Path(__file__).resolve().parents[2]
    path = backend / f"alembic/versions/{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_101_102_create_durable_results_and_preserve_reconciliation_history():
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as connection:
        connection.execute(text("""
            CREATE TABLE billing_reservation_reconciliation_event (
                id INTEGER PRIMARY KEY,
                reservation_id BIGINT NOT NULL,
                related_event_id BIGINT,
                actor_id BIGINT NOT NULL,
                decision VARCHAR(16) NOT NULL,
                resolution VARCHAR(24),
                provider_request_id VARCHAR(128),
                input_tokens BIGINT,
                output_tokens BIGINT,
                actual_milli BIGINT,
                evidence_reference VARCHAR(240),
                evidence_sha256 VARCHAR(64),
                note VARCHAR(500) NOT NULL,
                created_at DATETIME NOT NULL,
                tool_successful_calls BIGINT,
                CONSTRAINT ck_billing_res_recon_decision CHECK (
                    decision IN ('proposed', 'approved', 'rejected', 'blocked')
                ),
                CONSTRAINT ck_billing_res_recon_resolution CHECK (
                    (decision = 'blocked' AND (resolution IS NULL OR resolution IN ('settle', 'release_not_sent'))) OR
                    (decision <> 'blocked' AND resolution IS NOT NULL AND resolution IN ('settle', 'release_not_sent'))
                ),
                CONSTRAINT ck_billing_res_recon_related_event CHECK (
                    (decision = 'proposed' AND related_event_id IS NULL) OR
                    (decision IN ('approved', 'rejected') AND related_event_id IS NOT NULL) OR
                    decision = 'blocked'
                ),
                CONSTRAINT ck_billing_res_recon_evidence_hash_length CHECK (
                    (decision = 'blocked' AND provider_request_id IS NULL AND input_tokens IS NULL AND
                     output_tokens IS NULL AND actual_milli IS NULL AND evidence_reference IS NULL AND
                     evidence_sha256 IS NULL) OR
                    (decision <> 'blocked' AND evidence_reference IS NOT NULL AND evidence_sha256 IS NOT NULL AND
                     length(evidence_sha256) = 64)
                ),
                CONSTRAINT ck_billing_res_recon_tool_success_count CHECK (
                    tool_successful_calls IS NULL OR tool_successful_calls IN (0, 1)
                )
            )
        """))
        connection.execute(text("""
            INSERT INTO billing_reservation_reconciliation_event
            (id, reservation_id, actor_id, decision, resolution, note, created_at)
            VALUES (1, 4, 9, 'blocked', NULL, '旧阻断事件', CURRENT_TIMESTAMP)
        """))

        migration_101 = _migration("101_billing_tool_execution_result")
        migration_101.op = Operations(MigrationContext.configure(connection))
        migration_101.upgrade()
        migration_102 = _migration("102_billing_tool_stale_recovery")
        migration_102.op = Operations(MigrationContext.configure(connection))
        migration_102.upgrade()

        assert {column["name"] for column in inspect(connection).get_columns("billing_tool_execution_result")} == {
            "id", "request_key", "tool_name", "input_sha256", "result_json", "created_at",
        }
        assert connection.execute(text(
            "SELECT decision, note FROM billing_reservation_reconciliation_event WHERE id=1"
        )).one() == ("blocked", "旧阻断事件")
        connection.execute(text("""
            INSERT INTO billing_reservation_reconciliation_event
            (reservation_id, related_event_id, actor_id, decision, resolution, note, created_at)
            VALUES (4, NULL, 10, 'recovered', NULL, 'stale reserved -> unknown', CURRENT_TIMESTAMP)
        """))
        connection.execute(text("""
            INSERT INTO billing_tool_execution_result
            (request_key, tool_name, input_sha256, result_json, created_at)
            VALUES (:request_key, :tool_name, :input_sha256, :result_json, CURRENT_TIMESTAMP)
        """), {
            "request_key": "enterprise-assistant:run:tool:search:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            "tool_name": "search",
            "input_sha256": "a" * 64,
            "result_json": '{"status":"empty","billable":false}',
        })
        with pytest.raises(IntegrityError):
            with connection.begin_nested():
                connection.execute(text("""
                    INSERT INTO billing_reservation_reconciliation_event
                    (reservation_id, actor_id, decision, resolution, note, created_at)
                    VALUES (4, 11, 'recovered', 'settle', 'invalid', CURRENT_TIMESTAMP)
                """))

    engine.dispose()


def test_101_102_refuse_downgrade():
    for name in ("101_billing_tool_execution_result", "102_billing_tool_stale_recovery"):
        with pytest.raises(RuntimeError, match="不得删除"):
            _migration(name).downgrade()
