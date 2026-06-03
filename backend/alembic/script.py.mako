"""Alembic迁移脚本模板"""
revision = "${up_revision}"
down_revision = ${down_revision}
branch_labels = ${repr(branch_labels)}
depends_on = ${repr(depends_on)}


def upgrade():
    ${upgrades if upgrades else "pass"}


def downgrade():
    ${downgrades if downgrades else "pass"}
