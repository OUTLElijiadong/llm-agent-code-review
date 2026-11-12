"""Add explicit model CNY source and per-model supplier costs.

Existing historical catalogs stay NULL; they are not evidence of CNY verification.
"""

import sqlalchemy as sa

from alembic import op

revision = "105_model_cny_cost_sources"
down_revision = "104_c4_reviewer_grant"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("billing_rate_catalog", sa.Column("model_cny_source_url", sa.String(512), nullable=True))
    op.add_column("billing_rate_catalog", sa.Column("model_cny_source_checked_at", sa.DateTime(), nullable=True))
    op.add_column("billing_rate_catalog_item", sa.Column(
        "provider_input_cny_micros_per_million", sa.BigInteger(), nullable=True,
    ))
    op.add_column("billing_rate_catalog_item", sa.Column(
        "provider_output_cny_micros_per_million", sa.BigInteger(), nullable=True,
    ))
    dialect = op.get_bind().dialect.name
    if dialect == "sqlite":
        op.execute("CREATE TRIGGER trg_cny_model_source_immutable BEFORE UPDATE ON billing_rate_catalog "
                   "WHEN NEW.model_cny_source_url IS NOT OLD.model_cny_source_url "
                   "OR NEW.model_cny_source_checked_at IS NOT OLD.model_cny_source_checked_at "
                   "BEGIN SELECT RAISE(ABORT, 'model CNY source is immutable'); END")
        for action in ("INSERT", "UPDATE"):
            op.execute(f"CREATE TRIGGER trg_cny_model_source_pair_{action.lower()} "
                       f"BEFORE {action} ON billing_rate_catalog "
                       "WHEN (NEW.model_cny_source_url IS NULL) <> (NEW.model_cny_source_checked_at IS NULL) "
                       "BEGIN SELECT RAISE(ABORT, 'model CNY source pair is incomplete'); END")
            op.execute(f"CREATE TRIGGER trg_cny_model_cost_pair_{action.lower()} "
                       f"BEFORE {action} ON billing_rate_catalog_item "
                       "WHEN (NEW.provider_input_cny_micros_per_million IS NULL) <> "
                       "(NEW.provider_output_cny_micros_per_million IS NULL) "
                       "OR NEW.provider_input_cny_micros_per_million <= 0 "
                       "OR NEW.provider_output_cny_micros_per_million <= 0 "
                       "BEGIN SELECT RAISE(ABORT, 'model CNY cost pair is invalid'); END")
    elif dialect == "mysql":
        op.create_check_constraint("ck_bill_model_cny_source_pair", "billing_rate_catalog",
            "(model_cny_source_url IS NULL AND model_cny_source_checked_at IS NULL) OR "
            "(model_cny_source_url IS NOT NULL AND model_cny_source_checked_at IS NOT NULL)")
        op.create_check_constraint("ck_bill_model_cny_cost_pair", "billing_rate_catalog_item",
            "(provider_input_cny_micros_per_million IS NULL AND provider_output_cny_micros_per_million IS NULL) OR "
            "(provider_input_cny_micros_per_million IS NOT NULL AND provider_output_cny_micros_per_million IS NOT NULL "
            "AND provider_input_cny_micros_per_million > 0 AND provider_output_cny_micros_per_million > 0)")
        op.execute("CREATE TRIGGER trg_cny_model_source_immutable BEFORE UPDATE ON billing_rate_catalog "
                   "FOR EACH ROW BEGIN IF NOT (OLD.model_cny_source_url <=> NEW.model_cny_source_url "
                   "AND OLD.model_cny_source_checked_at <=> NEW.model_cny_source_checked_at) THEN "
                   "SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'model CNY source is immutable'; END IF; END")


def downgrade():
    raise RuntimeError("费率来源不可自动降级，请使用备份恢复流程")
