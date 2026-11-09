"""Permanent reviewer grants with explicit audited administration.

Revision ID: 071_permanent_reviewer
Revises: 070_billing_subscription
"""
import sqlalchemy as sa

from alembic import op

revision = "071_permanent_reviewer"
down_revision = "070_billing_subscription"
branch_labels = None
depends_on = None


def _id():
    return sa.Column("id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), primary_key=True, autoincrement=True)


def upgrade():
    op.create_table("billing_permanent_reviewer", _id(),
                    sa.Column("user_id", sa.BigInteger(), nullable=False),
                    sa.Column("status", sa.String(16), nullable=False),
                    sa.Column("granted_by", sa.BigInteger(), nullable=False),
                    sa.Column("granted_at", sa.DateTime(), nullable=False),
                    sa.Column("revoked_by", sa.BigInteger(), nullable=True),
                    sa.Column("revoked_at", sa.DateTime(), nullable=True),
                    sa.UniqueConstraint("user_id", name="uq_billing_permanent_reviewer_user"))
    op.create_table("billing_permanent_reviewer_event", _id(),
                    sa.Column("user_id", sa.BigInteger(), nullable=False),
                    sa.Column("actor_id", sa.BigInteger(), nullable=False),
                    sa.Column("action", sa.String(16), nullable=False),
                    sa.Column("reason", sa.String(500), nullable=False),
                    sa.Column("created_at", sa.DateTime(), nullable=False))


def downgrade():
    # Preserve financial and entitlement audit history.
    raise RuntimeError("永久权益和审计账本是前向迁移；请通过备份恢复，禁止将 Alembic 版本号单独回退")
