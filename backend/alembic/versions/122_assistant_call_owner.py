"""Add detached assistant execution ownership records.

Revision ID: 122_assistant_call_owner
Revises: 121_billing_model_evidence
"""

import sqlalchemy as sa
from sqlalchemy.dialects import mysql

from alembic import op

revision = "122_assistant_call_owner"
down_revision = "121_billing_model_evidence"
branch_labels = None
depends_on = None


def _install_model_attempt_immutability():
    dialect = op.get_bind().dialect.name
    for action in ("UPDATE", "DELETE"):
        name = f"trg_ent_model_owner_no_{action.lower()}"
        if dialect == "sqlite":
            op.execute(
                f"CREATE TRIGGER {name} BEFORE {action} ON enterprise_assistant_model_attempt_owner "
                "BEGIN SELECT RAISE(ABORT, 'model attempt ownership is immutable'); END"
            )
        elif dialect == "mysql":
            op.execute(
                f"CREATE TRIGGER {name} BEFORE {action} ON enterprise_assistant_model_attempt_owner "
                "FOR EACH ROW SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'model attempt ownership is immutable'"
            )
        else:
            raise RuntimeError(f"unsupported database for model attempt ownership: {dialect}")


def upgrade():
    pk = sa.BigInteger().with_variant(sa.Integer, "sqlite")
    digest = sa.LargeBinary(32).with_variant(mysql.VARBINARY(32), "mysql")
    op.create_table(
        "enterprise_assistant_run_owner",
        sa.Column("id", pk, primary_key=True, autoincrement=True),
        sa.Column(
            "assistant_run_id",
            sa.BigInteger(),
            sa.ForeignKey("enterprise_assistant_run.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("owner_digest", digest, nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("assistant_run_id", "generation", name="uq_ent_run_owner_generation"),
        sa.CheckConstraint("generation > 0", name="ck_ent_run_owner_generation"),
        sa.CheckConstraint("length(owner_digest) = 32", name="ck_ent_run_owner_digest"),
    )
    op.create_table(
        "enterprise_assistant_tool_owner",
        sa.Column("id", pk, primary_key=True, autoincrement=True),
        sa.Column(
            "assistant_run_id",
            sa.BigInteger(),
            sa.ForeignKey("enterprise_assistant_run.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "run_owner_id",
            sa.BigInteger(),
            sa.ForeignKey("enterprise_assistant_run_owner.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("enterprise_id", sa.BigInteger(), nullable=False),
        sa.Column("conversation_id", sa.BigInteger(), nullable=False),
        sa.Column("initiator_user_id", sa.BigInteger(), nullable=False),
        sa.Column("billing_group_id", sa.BigInteger(), nullable=True),
        sa.Column(
            "wallet_id", sa.BigInteger(), sa.ForeignKey("billing_wallet.id", ondelete="RESTRICT"), nullable=False
        ),
        sa.Column("tool_name", sa.String(16), nullable=False),
        sa.Column("call_digest", digest, nullable=False),
        sa.Column("call_bytes", sa.LargeBinary(480), nullable=False),
        sa.Column("request_digest", digest, nullable=False),
        sa.Column("request_bytes", sa.LargeBinary(640), nullable=False),
        sa.Column("arguments_digest", digest, nullable=False),
        sa.Column("owner_digest", digest, nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("assistant_run_id", "call_digest", name="uq_ent_tool_owner_call"),
        sa.UniqueConstraint("request_digest", name="uq_ent_tool_owner_request"),
        sa.CheckConstraint("generation > 0", name="ck_ent_tool_owner_generation"),
        sa.CheckConstraint("tool_name IN ('search', 'fetch')", name="ck_ent_tool_owner_kind"),
        sa.CheckConstraint(
            "length(owner_digest) = 32 AND length(call_digest) = 32 AND "
            "length(request_digest) = 32 AND length(arguments_digest) = 32",
            name="ck_ent_tool_owner_digests",
        ),
    )
    op.create_table(
        "enterprise_assistant_model_attempt_owner",
        sa.Column("id", pk, primary_key=True, autoincrement=True),
        sa.Column(
            "assistant_run_id",
            sa.BigInteger(),
            sa.ForeignKey("enterprise_assistant_run.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "run_owner_id",
            sa.BigInteger(),
            sa.ForeignKey("enterprise_assistant_run_owner.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "reservation_id",
            sa.BigInteger(),
            sa.ForeignKey("billing_reservation.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("enterprise_id", sa.BigInteger(), nullable=False),
        sa.Column("conversation_id", sa.BigInteger(), nullable=False),
        sa.Column("initiator_user_id", sa.BigInteger(), nullable=False),
        sa.Column("billing_group_id", sa.BigInteger(), nullable=True),
        sa.Column(
            "wallet_id", sa.BigInteger(), sa.ForeignKey("billing_wallet.id", ondelete="RESTRICT"), nullable=False
        ),
        sa.Column("payer_user_id", sa.BigInteger(), nullable=True),
        sa.Column("usage_kind", sa.String(20), nullable=False),
        sa.Column("model_name", sa.String(80), nullable=False),
        sa.Column("round_digest", digest, nullable=False),
        sa.Column("round_bytes", sa.LargeBinary(480), nullable=False),
        sa.Column("request_digest", digest, nullable=False),
        sa.Column("request_bytes", sa.LargeBinary(640), nullable=False),
        sa.Column("payload_digest", digest, nullable=False),
        sa.Column("billing_digest", digest, nullable=False),
        sa.Column("owner_digest", digest, nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("assistant_run_id", "round_digest", name="uq_ent_model_owner_round"),
        sa.UniqueConstraint("reservation_id", name="uq_ent_model_owner_reservation"),
        sa.UniqueConstraint("request_digest", name="uq_ent_model_owner_request"),
        sa.CheckConstraint("generation > 0", name="ck_ent_model_owner_generation"),
        sa.CheckConstraint(
            "(usage_kind = 'member' AND payer_user_id IS NOT NULL AND payer_user_id = initiator_user_id) "
            "OR (usage_kind = 'enterprise_auto' AND payer_user_id IS NULL)",
            name="ck_ent_model_owner_payer",
        ),
        sa.CheckConstraint(
            "length(owner_digest) = 32 AND length(round_digest) = 32 AND "
            "length(request_digest) = 32 AND length(payload_digest) = 32 AND length(billing_digest) = 32",
            name="ck_ent_model_owner_digests",
        ),
    )
    _install_model_attempt_immutability()


def downgrade():
    raise RuntimeError("执行所有权证据不能自动降级，请按备份恢复流程处理")
