"""Create isolated platform model cost attempt ledger.

Revision ID: 122_platform_model_cost_ledger
Revises: 121_billing_model_evidence
"""

import sqlalchemy as sa

from alembic import op

revision = "122_platform_model_cost_ledger"
down_revision = "121_billing_model_evidence"
branch_labels = None
depends_on = None


def _id_type():
    return sa.BigInteger().with_variant(sa.Integer(), "sqlite")


def upgrade():
    op.create_table(
        "platform_model_cost_attempt",
        sa.Column("id", _id_type(), primary_key=True, autoincrement=True),
        sa.Column("request_key", sa.String(160), nullable=False),
        sa.Column("actor_user_id", sa.BigInteger(), nullable=False),
        sa.Column("run_id", sa.String(128), nullable=True),
        sa.Column("model_name", sa.String(80), nullable=False),
        sa.Column("pricing_version", sa.String(64), nullable=False),
        sa.Column("pricing_source_url", sa.String(512), nullable=False),
        sa.Column("pricing_source_checked_at", sa.DateTime(), nullable=False),
        sa.Column("provider_identity_version", sa.String(40), nullable=False),
        sa.Column("provider_name", sa.String(32), nullable=False),
        sa.Column("endpoint_host", sa.String(253), nullable=False),
        sa.Column("credential_source", sa.String(16), nullable=False),
        sa.Column("input_cost_cny_micros_per_million", sa.BigInteger(), nullable=False),
        sa.Column("output_cost_cny_micros_per_million", sa.BigInteger(), nullable=False),
        sa.Column("max_input_tokens", sa.BigInteger(), nullable=False),
        sa.Column("max_output_tokens", sa.BigInteger(), nullable=False),
        sa.Column("reserved_cost_cny_micros", sa.BigInteger(), nullable=False),
        sa.Column("actual_input_tokens", sa.BigInteger(), nullable=True),
        sa.Column("actual_output_tokens", sa.BigInteger(), nullable=True),
        sa.Column("actual_cost_cny_micros", sa.BigInteger(), nullable=True),
        sa.Column("status", sa.String(16), nullable=False, server_default="reserved"),
        sa.Column("provider_attempt_started_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("settled_at", sa.DateTime(), nullable=True),
        sa.UniqueConstraint("request_key", name="uq_platform_model_cost_request"),
        sa.CheckConstraint(
            "status IN ('reserved', 'unknown', 'settled', 'not_sent')",
            name="ck_platform_model_cost_status",
        ),
        sa.CheckConstraint(
            "provider_identity_version = 'deepseek-platform-cost-v1' AND provider_name = 'deepseek' "
            "AND endpoint_host = 'api.deepseek.com' AND credential_source IN ('system', 'global')",
            name="ck_platform_model_cost_provider",
        ),
        sa.CheckConstraint(
            "input_cost_cny_micros_per_million > 0 AND output_cost_cny_micros_per_million > 0 "
            "AND max_input_tokens > 0 AND max_output_tokens > 0 AND reserved_cost_cny_micros > 0",
            name="ck_platform_model_cost_bounds",
        ),
        sa.CheckConstraint(
            "(status = 'settled' AND actual_input_tokens IS NOT NULL AND actual_output_tokens IS NOT NULL "
            "AND actual_cost_cny_micros IS NOT NULL AND settled_at IS NOT NULL) OR "
            "(status <> 'settled' AND actual_input_tokens IS NULL AND actual_output_tokens IS NULL "
            "AND actual_cost_cny_micros IS NULL AND settled_at IS NULL)",
            name="ck_platform_model_cost_settlement_pair",
        ),
        sa.CheckConstraint(
            "status <> 'not_sent' OR provider_attempt_started_at IS NULL",
            name="ck_platform_model_cost_not_sent",
        ),
        sa.CheckConstraint(
            "actual_cost_cny_micros IS NULL OR actual_cost_cny_micros <= reserved_cost_cny_micros",
            name="ck_platform_model_cost_actual_limit",
        ),
        sa.CheckConstraint(
            "actual_input_tokens IS NULL OR (actual_input_tokens >= 0 AND actual_output_tokens >= 0 "
            "AND actual_cost_cny_micros >= 0 AND actual_input_tokens <= max_input_tokens "
            "AND actual_output_tokens <= max_output_tokens)",
            name="ck_platform_model_cost_token_limit",
        ),
    )
    op.create_index(
        "ix_platform_cost_actor_created", "platform_model_cost_attempt", ["actor_user_id", "created_at", "id"],
    )
    op.create_index(
        "ix_platform_cost_run_status", "platform_model_cost_attempt", ["run_id", "status", "id"],
    )


def downgrade():
    connection = op.get_bind()
    if connection.execute(sa.text("SELECT COUNT(*) FROM platform_model_cost_attempt")).scalar_one():
        raise RuntimeError("平台模型成本流水非空，拒绝降级删除历史记录")
    op.drop_index("ix_platform_cost_run_status", table_name="platform_model_cost_attempt")
    op.drop_index("ix_platform_cost_actor_created", table_name="platform_model_cost_attempt")
    op.drop_table("platform_model_cost_attempt")
