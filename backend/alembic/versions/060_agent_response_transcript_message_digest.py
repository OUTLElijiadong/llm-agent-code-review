"""Add per-message integrity digests to the Responses transcript ledger.

Revision ID: 060_transcript_message_sha256
Revises: 059_response_transcript_ledger
"""

from hashlib import sha256

import sqlalchemy as sa

from alembic import op

revision = "060_transcript_message_sha256"
down_revision = "059_response_transcript_ledger"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "agent_response_transcript_message",
        sa.Column("message_sha256", sa.String(length=64), nullable=True),
    )

    connection = op.get_bind()
    last_id = 0
    select_batch = sa.text(
        "SELECT id, message_json FROM agent_response_transcript_message "
        "WHERE message_sha256 IS NULL AND id > :last_id ORDER BY id LIMIT 250"
    )
    update_batch = sa.text(
        "UPDATE agent_response_transcript_message SET message_sha256 = :digest WHERE id = :row_id"
    )
    while True:
        rows = connection.execute(select_batch, {"last_id": last_id}).mappings().all()
        if not rows:
            break
        updates = []
        for row in rows:
            row_id = int(row["id"])
            message_json = str(row["message_json"])
            updates.append({
                "row_id": row_id,
                "digest": sha256(message_json.encode("utf-8")).hexdigest(),
            })
            last_id = row_id
        connection.execute(update_batch, updates)

    op.alter_column(
        "agent_response_transcript_message",
        "message_sha256",
        existing_type=sa.String(length=64),
        nullable=False,
    )


def downgrade() -> None:
    raise RuntimeError(
        "060 is forward-only: removing message digests would disable transcript integrity checks"
    )
