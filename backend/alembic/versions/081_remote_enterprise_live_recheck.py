"""Keep enterprise remote approval mandatory throughout execution.

Revision ID: 081_remote_live_recheck
Revises: 080_assistant_recovery
"""

import sqlalchemy as sa

from alembic import op

revision = "081_remote_live_recheck"
down_revision = "080_assistant_recovery"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("remote_pentest_engagement") as batch:
        batch.add_column(sa.Column(
            "enterprise_approval_required", sa.Boolean(), nullable=False, server_default=sa.false(),
        ))


def downgrade():
    raise RuntimeError("企业远测持续授权标记属于安全审计状态；请按备份恢复，禁止单独回退迁移")
