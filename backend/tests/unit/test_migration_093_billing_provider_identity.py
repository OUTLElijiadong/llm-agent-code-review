import importlib.util
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect, text


def test_093_keeps_legacy_identity_unknown_and_rejects_partial_or_untrusted_identity():
    backend = Path(__file__).resolve().parents[2]
    path = backend / "alembic/versions/093_billing_provider_identity.py"
    spec = importlib.util.spec_from_file_location("migration_093_billing_provider_identity", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.revision == "093_billing_provider_identity"
    assert len(module.revision) <= 32
    assert module.down_revision == "092_billing_rate_snapshot"

    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "CREATE TABLE billing_reservation (id INTEGER PRIMARY KEY, model_name VARCHAR(80) NOT NULL)"
        )
        connection.execute(text("INSERT INTO billing_reservation (id, model_name) VALUES (1, 'deepseek-flash')"))
        module.op = Operations(MigrationContext.configure(connection))
        module.upgrade()

        columns = {item["name"] for item in inspect(connection).get_columns("billing_reservation")}
        assert {
            "provider_identity_version", "provider_name", "provider_endpoint_host", "credential_source",
        }.issubset(columns)
        legacy = connection.execute(text(
            "SELECT provider_identity_version, provider_name, provider_endpoint_host, credential_source "
            "FROM billing_reservation WHERE id=1"
        )).one()
        assert legacy == (None, None, None, None)
        assert "ck_billing_reservation_provider_identity_pair" in {
            item["name"] for item in inspect(connection).get_check_constraints("billing_reservation")
        }

        connection.execute(text(
            "INSERT INTO billing_reservation (id, model_name, provider_identity_version, provider_name, "
            "provider_endpoint_host, credential_source) VALUES "
            "(2, 'deepseek-v4-flash', 'deepseek-official-v1', 'deepseek', 'api.deepseek.com', 'system')"
        ))
        for identity_values in (
            (3, "partial", "deepseek", "api.deepseek.com", "system"),
            (4, "deepseek-official-v1", "custom", "proxy.example", "global"),
            (5, "deepseek-official-v1", "deepseek", "api.deepseek.com", "user"),
        ):
            try:
                connection.execute(text(
                    "INSERT INTO billing_reservation (id, model_name, provider_identity_version, provider_name, "
                    "provider_endpoint_host, credential_source) VALUES (:id, 'deepseek-flash', :version, :provider, "
                    ":host, :source)"
                ), {"id": identity_values[0], "version": identity_values[1], "provider": identity_values[2],
                    "host": identity_values[3], "source": identity_values[4]})
            except Exception as exc:
                assert "check constraint" in str(exc).lower()
            else:
                raise AssertionError("计费身份必须匹配已验证的 DeepSeek 官方端点和托管凭据")
    engine.dispose()


def test_093_refuses_to_drop_financial_provider_identity():
    backend = Path(__file__).resolve().parents[2]
    path = backend / "alembic/versions/093_billing_provider_identity.py"
    spec = importlib.util.spec_from_file_location("migration_093_no_downgrade", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    try:
        module.downgrade()
    except RuntimeError as exc:
        assert "财务审计依据" in str(exc)
    else:
        raise AssertionError("不能自动删除计费供应商身份快照")
