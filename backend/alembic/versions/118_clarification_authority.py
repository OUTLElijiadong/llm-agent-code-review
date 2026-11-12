"""Clarification-only monotonic authority evidence and append-only reviews.

115 records predate version evidence and deliberately remain stale.
"""
import sqlalchemy as sa

from alembic import op

revision = "118_clarification_authority"
down_revision = "117_wechat_fund_statement"
branch_labels = None
depends_on = None

_VERSION_TARGETS = {
    "user": ("status",),
    "enterprise": ("owner_id",),
    "enterprise_member": ("role", "status"),
    "enterprise_group_member": ("role", "status"),
}


def install_version_triggers(connection):
    dialect = connection.dialect.name
    if dialect not in {"mysql", "sqlite"}:
        raise RuntimeError("clarification authority triggers require MySQL or SQLite")
    for table, columns in _VERSION_TARGETS.items():
        trigger = f"trg_clarify_authority_{table}"
        if dialect == "mysql":
            changed = " OR ".join(f"NOT (OLD.`{c}` <=> NEW.`{c}`)" for c in columns)
            sql = (f"CREATE TRIGGER `{trigger}` BEFORE UPDATE ON `{table}` FOR EACH ROW "
                   f"SET NEW.`clarification_authority_version` = OLD.`clarification_authority_version` "
                   f"+ IF({changed}, 1, 0)")
        else:
            changed = " OR ".join(f'OLD."{c}" IS NOT NEW."{c}"' for c in columns)
            update_of = ", ".join(f'"{c}"' for c in columns)
            sql = (f'CREATE TRIGGER "{trigger}" AFTER UPDATE OF {update_of} ON "{table}" '
                   f'FOR EACH ROW WHEN {changed} BEGIN UPDATE "{table}" '
                   f'SET clarification_authority_version = OLD.clarification_authority_version + 1 '
                   f'WHERE id = NEW.id; END')
        connection.exec_driver_sql(sql)
        # These identities are soft-deleted by the application. Physical
        # delete/reinsert (including REPLACE) would reset the per-row epoch and
        # could revive old evidence; native UPDATE/UPSERT remains supported.
        delete_name = f"trg_clarify_authority_no_delete_{table}"
        if dialect == "mysql":
            delete_sql = (f"CREATE TRIGGER `{delete_name}` BEFORE DELETE ON `{table}` FOR EACH ROW "
                          "SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'clarification authority row is immutable'")
        else:
            delete_sql = (f'CREATE TRIGGER "{delete_name}" BEFORE DELETE ON "{table}" '
                          "BEGIN SELECT RAISE(ABORT, 'clarification authority row is immutable'); END")
        connection.exec_driver_sql(delete_sql)
        if dialect == "sqlite":
            # An explicit update may preserve the value or advance it exactly
            # once. The authority trigger's internal increment follows the
            # same rule, including with recursive_triggers both off and on.
            # Reject rewinds and arbitrary jumps before touching the row.
            connection.exec_driver_sql(
                f'CREATE TRIGGER "trg_clarify_version_guard_{table}" '
                f'BEFORE UPDATE OF clarification_authority_version ON "{table}" '
                "FOR EACH ROW WHEN NEW.clarification_authority_version IS NOT OLD.clarification_authority_version "
                "AND NEW.clarification_authority_version IS NOT OLD.clarification_authority_version + 1 "
                "BEGIN SELECT RAISE(ABORT, 'clarification authority version is monotonic'); END"
            )
    for operation in ("UPDATE", "DELETE"):
        name = f"trg_clarify_review_no_{operation.lower()}"
        if dialect == "mysql":
            sql = (f"CREATE TRIGGER `{name}` BEFORE {operation} ON enterprise_assistant_clarification_review "
                   "FOR EACH ROW SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'clarification review is immutable'")
        else:
            sql = (f'CREATE TRIGGER "{name}" BEFORE {operation} ON enterprise_assistant_clarification_review '
                   "BEGIN SELECT RAISE(ABORT, 'clarification review is immutable'); END")
        connection.exec_driver_sql(sql)
    for operation in ("UPDATE", "DELETE"):
        name = f"trg_clarify_decided_no_{operation.lower()}"
        if dialect == "mysql":
            sql = (f"CREATE TRIGGER `{name}` BEFORE {operation} ON enterprise_assistant_clarification_decision "
                   "FOR EACH ROW BEGIN IF OLD.decision <> 'pending' THEN "
                   "SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'clarification decision is immutable'; END IF; END")
        else:
            sql = (f'CREATE TRIGGER "{name}" BEFORE {operation} ON enterprise_assistant_clarification_decision '
                   "WHEN OLD.decision <> 'pending' BEGIN "
                   "SELECT RAISE(ABORT, 'clarification decision is immutable'); END")
        connection.exec_driver_sql(sql)


def upgrade():
    for table in _VERSION_TARGETS:
        op.add_column(
            table,
            sa.Column("clarification_authority_version", sa.BigInteger(), nullable=False, server_default="0"),
        )
    op.add_column("enterprise_assistant_clarification", sa.Column("authority_version_sha256", sa.String(64)))
    op.add_column("enterprise_assistant_clarification_decision", sa.Column("decider_version_sha256", sa.String(64)))
    ident = sa.BigInteger().with_variant(sa.Integer(), "sqlite")
    op.create_table(
        "enterprise_assistant_clarification_review",
        sa.Column("id", ident, primary_key=True, autoincrement=True),
        sa.Column("request_id", sa.BigInteger(), nullable=False),
        sa.Column("enterprise_id", sa.BigInteger(), nullable=False),
        sa.Column("conversation_id", sa.BigInteger(), nullable=False),
        sa.Column("source_message_id", sa.BigInteger(), nullable=False),
        sa.Column("decided_by", sa.BigInteger(), nullable=False),
        sa.Column("review_kind", sa.String(24), nullable=False),
        sa.Column("decider_rank", sa.Integer(), nullable=False),
        sa.Column("decider_version_sha256", sa.String(64), nullable=False),
        sa.Column("decision", sa.String(16), nullable=False),
        sa.Column("decided_at", sa.DateTime(), nullable=False),
        sa.Column("create_time", sa.DateTime(), nullable=False),
        sa.Column("update_time", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["request_id", "enterprise_id", "conversation_id"],
                                ["enterprise_assistant_clarification.id",
                                 "enterprise_assistant_clarification.enterprise_id",
                                 "enterprise_assistant_clarification.conversation_id"],
                                name="fk_ent_clarify_review_scope", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["request_id", "source_message_id"],
                                ["enterprise_assistant_clarification_decision.request_id",
                                 "enterprise_assistant_clarification_decision.source_message_id"],
                                name="fk_ent_clarify_review_decision", ondelete="RESTRICT"),
        sa.CheckConstraint("decision IN ('allow', 'deny', 'unrelated')", name="ck_ent_clarify_review_decision"),
        sa.CheckConstraint("review_kind IN ('higher_review', 'author_revision')", name="ck_ent_clarify_review_kind"),
    )
    install_version_triggers(op.get_bind())


def downgrade():
    raise RuntimeError("澄清证据不能自动降级，请按备份恢复流程处理")
