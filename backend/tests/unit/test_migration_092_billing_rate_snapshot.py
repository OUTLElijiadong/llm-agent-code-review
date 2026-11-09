import importlib.util
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect, text


def test_092_migration_preserves_legacy_rows_without_guessing_rates_and_guards_shape():
    backend = Path(__file__).resolve().parents[2]
    path = backend / "alembic/versions/092_billing_rate_snapshot.py"
    spec = importlib.util.spec_from_file_location("migration_092_billing_rate_snapshot", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.revision == "092_billing_rate_snapshot"
    assert len(module.revision) <= 32
    assert module.down_revision == "091_statement_refund_provider"

    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "CREATE TABLE billing_reservation (id INTEGER PRIMARY KEY, model_name VARCHAR(80) NOT NULL)"
        )
        connection.execute(text("INSERT INTO billing_reservation (id, model_name) VALUES "
                                "(1, 'deepseek-flash'), (2, 'deepseek-v4-flash'), "
                                "(3, 'deepseek-v4-pro'), (4, 'deepseek-v4-flash-vision-exp'), "
                                "(5, 'old-unpriced-model')"))
        module.op = Operations(MigrationContext.configure(connection))
        module.upgrade()

        columns = {item["name"]: item for item in inspect(connection).get_columns("billing_reservation")}
        assert {
            "pricing_version", "input_rate_numerator", "input_rate_denominator",
            "output_rate_numerator", "output_rate_denominator",
        }.issubset(columns)
        rows = connection.execute(text(
            "SELECT id, pricing_version, input_rate_numerator, input_rate_denominator, "
            "output_rate_numerator, output_rate_denominator FROM billing_reservation ORDER BY id"
        )).all()
        assert rows[:4] == [
            (1, None, None, None, None, None),
            (2, None, None, None, None, None),
            (3, None, None, None, None, None),
            (4, None, None, None, None, None),
        ]
        assert rows[4] == (5, None, None, None, None, None)
        checks = {item["name"] for item in inspect(connection).get_check_constraints("billing_reservation")}
        assert "ck_billing_reservation_rate_snapshot_pair" in checks

        try:
            connection.execute(text(
                "UPDATE billing_reservation SET pricing_version='partial', input_rate_numerator=1 WHERE id=5"
            ))
        except Exception as exc:
            assert "check constraint" in str(exc).lower()
        else:
            raise AssertionError("费率快照必须整体为空或整体有效")
        try:
            connection.execute(text(
                "UPDATE billing_reservation SET pricing_version='', input_rate_numerator=1, "
                "input_rate_denominator=1, output_rate_numerator=1, output_rate_denominator=1 WHERE id=5"
            ))
        except Exception as exc:
            assert "check constraint" in str(exc).lower()
        else:
            raise AssertionError("费率版本不能为空")
    engine.dispose()


def test_092_migration_refuses_to_drop_financial_rate_snapshots():
    backend = Path(__file__).resolve().parents[2]
    path = backend / "alembic/versions/092_billing_rate_snapshot.py"
    spec = importlib.util.spec_from_file_location("migration_092_no_downgrade", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    try:
        module.downgrade()
    except RuntimeError as exc:
        assert "财务结算依据" in str(exc)
    else:
        raise AssertionError("不能静默删除模型预占费率快照")
