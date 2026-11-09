import importlib.util
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError


def _migration_module():
    backend = Path(__file__).resolve().parents[2]
    path = backend / "alembic/versions/095_billing_reservation_reconciliation.py"
    spec = importlib.util.spec_from_file_location("migration_095_billing_reservation_reconciliation", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_095_adds_append_only_reconciliation_event_contract():
    module = _migration_module()
    assert module.revision == "095_billing_res_reconcile"
    assert len(module.revision) <= 32
    assert module.down_revision == "094_enterprise_agent_teams"

    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as connection:
        module.op = Operations(MigrationContext.configure(connection))
        module.upgrade()
        assert "billing_reservation_reconciliation_event" in inspect(connection).get_table_names()
        indexes = {item["name"] for item in inspect(connection).get_indexes(
            "billing_reservation_reconciliation_event",
        )}
        assert indexes == {"ix_billing_res_recon_reservation"}
        row = {
            "id": 1, "reservation_id": 42, "related_event_id": None, "actor_id": 7,
            "decision": "proposed", "resolution": "settle", "provider_request_id": "resp_42",
            "input_tokens": 100, "output_tokens": 25, "actual_milli": 200,
            "evidence_reference": "provider-case-42", "evidence_sha256": "a" * 64,
            "note": "对照供应商用量", "created_at": "2026-11-06 00:00:00",
        }
        connection.execute(text(
            "INSERT INTO billing_reservation_reconciliation_event "
            "(id, reservation_id, related_event_id, actor_id, decision, resolution, provider_request_id, "
            "input_tokens, output_tokens, actual_milli, evidence_reference, evidence_sha256, note, created_at) "
            "VALUES (:id, :reservation_id, :related_event_id, :actor_id, :decision, :resolution, "
            ":provider_request_id, :input_tokens, :output_tokens, :actual_milli, :evidence_reference, "
            ":evidence_sha256, :note, :created_at)"
        ), row)
        connection.execute(text(
            "INSERT INTO billing_reservation_reconciliation_event "
            "(id, reservation_id, actor_id, decision, resolution, note, created_at) "
            "VALUES (2, 42, 8, 'blocked', NULL, '没有待审提案', '2026-11-06 00:00:01')"
        ))
        with pytest.raises(IntegrityError):
            connection.execute(text(
                "INSERT INTO billing_reservation_reconciliation_event "
                "(id, reservation_id, actor_id, decision, resolution, "
                "evidence_reference, evidence_sha256, note, created_at) "
                "VALUES (3, 43, 7, 'arbitrary', 'settle', 'provider-case', :hash, '说明', '2026-11-06 00:00:00')"
            ), {"hash": "b" * 64})
        with pytest.raises(IntegrityError):
            connection.execute(text(
                "INSERT INTO billing_reservation_reconciliation_event "
                "(id, reservation_id, actor_id, decision, resolution, "
                "evidence_reference, evidence_sha256, note, created_at) "
                "VALUES (4, 44, 7, 'proposed', 'settle', 'provider-case', 'bad', '说明', '2026-11-06 00:00:00')"
            ))
        with pytest.raises(IntegrityError):
            connection.execute(text(
                "INSERT INTO billing_reservation_reconciliation_event "
                "(id, reservation_id, actor_id, decision, resolution, provider_request_id, note, created_at) "
                "VALUES (5, 44, 7, 'blocked', NULL, 'must-not-copy', '说明', '2026-11-06 00:00:00')"
            ))
        with pytest.raises(IntegrityError):
            connection.execute(text(
                "INSERT INTO billing_reservation_reconciliation_event "
                "(id, reservation_id, related_event_id, actor_id, decision, resolution, "
                "evidence_reference, evidence_sha256, note, created_at) "
                "VALUES (6, 44, 1, 7, 'approved', NULL, 'provider-case', :hash, '说明', "
                "'2026-11-06 00:00:00')"
            ), {"hash": "c" * 64})
        module.downgrade()
        assert "billing_reservation_reconciliation_event" not in inspect(connection).get_table_names()
    engine.dispose()
