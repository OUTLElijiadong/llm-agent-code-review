"""Capture the authority of explicit Xiaoling directives when sent.

Revision ID: 119_chat_message_authority
Revises: 118_clarification_authority
"""

import sqlalchemy as sa

from alembic import op

revision = "119_chat_message_authority"
down_revision = "118_clarification_authority"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "enterprise_chat_message",
        sa.Column("sender_authority_rank", sa.Integer(), nullable=True),
    )
    if op.get_bind().dialect.name == "mysql":
        op.execute(
            "CREATE TRIGGER trg_ent_chat_authority_validate BEFORE INSERT ON enterprise_chat_message "
            "FOR EACH ROW BEGIN IF NEW.sender_authority_rank IS NOT NULL "
            "AND NEW.sender_authority_rank NOT BETWEEN 1 AND 4 THEN "
            "SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'chat message authority rank is invalid'; END IF; "
            "IF NEW.sender_type = 'human' AND LOCATE('@小菱', NEW.content) > 0 "
            "AND NEW.sender_authority_rank IS NULL THEN "
            "SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'chat directive requires authority snapshot'; END IF; END"
        )
        op.execute(
            "CREATE TRIGGER trg_ent_chat_authority_immutable BEFORE UPDATE ON enterprise_chat_message "
            "FOR EACH ROW BEGIN "
            "IF NOT (NEW.sender_authority_rank <=> OLD.sender_authority_rank) THEN "
            "SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'chat message authority snapshot is immutable'; END IF; "
            "IF NEW.sender_authority_rank IS NOT NULL AND NEW.sender_authority_rank NOT BETWEEN 1 AND 4 THEN "
            "SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'chat message authority rank is invalid'; END IF; "
            "IF NEW.sender_type = 'human' AND LOCATE('@小菱', NEW.content) > 0 "
            "AND NEW.sender_authority_rank IS NULL THEN "
            "SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'chat directive requires authority snapshot'; END IF; "
            "IF ((OLD.sender_type = 'human' AND LOCATE('@小菱', OLD.content) > 0) "
            "OR (NEW.sender_type = 'human' AND LOCATE('@小菱', NEW.content) > 0)) AND "
            "(NOT (NEW.id <=> OLD.id) OR NOT (NEW.enterprise_id <=> OLD.enterprise_id) "
            "OR NOT (NEW.conversation_id <=> OLD.conversation_id) "
             "OR NOT (NEW.sender_id <=> OLD.sender_id) "
             "OR NOT (CAST(NEW.sender_type AS BINARY) <=> CAST(OLD.sender_type AS BINARY)) "
             "OR NOT (CAST(NEW.client_message_id AS BINARY) <=> CAST(OLD.client_message_id AS BINARY)) "
             "OR NOT (CAST(NEW.content AS BINARY) <=> CAST(OLD.content AS BINARY))) THEN "
            "SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'chat directive identity is immutable'; END IF; END"
        )
        op.execute(
            "CREATE TRIGGER trg_ent_chat_authority_delete BEFORE DELETE ON enterprise_chat_message "
            "FOR EACH ROW BEGIN IF OLD.sender_type = 'human' AND LOCATE('@小菱', OLD.content) > 0 THEN "
            "SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'chat directive cannot be deleted'; END IF; END"
        )
    elif op.get_bind().dialect.name == "sqlite":
        op.execute(
            "CREATE TRIGGER trg_ent_chat_authority_validate BEFORE INSERT ON enterprise_chat_message "
            "WHEN NEW.sender_authority_rank IS NOT NULL AND NEW.sender_authority_rank NOT BETWEEN 1 AND 4 "
            "BEGIN SELECT RAISE(ABORT, 'chat message authority rank is invalid'); END"
        )
        op.execute(
            "CREATE TRIGGER trg_ent_chat_directive_validate BEFORE INSERT ON enterprise_chat_message "
            "WHEN NEW.sender_type = 'human' AND instr(NEW.content, '@小菱') > 0 "
            "AND NEW.sender_authority_rank IS NULL "
            "BEGIN SELECT RAISE(ABORT, 'chat directive requires authority snapshot'); END"
        )
        op.execute(
            "CREATE TRIGGER trg_ent_chat_authority_immutable BEFORE UPDATE OF sender_authority_rank "
            "ON enterprise_chat_message WHEN NEW.sender_authority_rank IS NOT OLD.sender_authority_rank "
            "BEGIN SELECT RAISE(ABORT, 'chat message authority snapshot is immutable'); END"
        )
        op.execute(
            "CREATE TRIGGER trg_ent_chat_directive_update_guard BEFORE UPDATE ON enterprise_chat_message "
            "WHEN (NEW.sender_type = 'human' AND instr(NEW.content, '@小菱') > 0 "
            "AND (NEW.sender_authority_rank IS NULL OR NEW.sender_authority_rank NOT BETWEEN 1 AND 4)) "
            "OR (((OLD.sender_type = 'human' AND instr(OLD.content, '@小菱') > 0) "
            "OR (NEW.sender_type = 'human' AND instr(NEW.content, '@小菱') > 0)) AND "
            "(NEW.id IS NOT OLD.id OR NEW.enterprise_id IS NOT OLD.enterprise_id "
            "OR NEW.conversation_id IS NOT OLD.conversation_id OR NEW.sender_id IS NOT OLD.sender_id "
            "OR NEW.sender_type IS NOT OLD.sender_type OR NEW.client_message_id IS NOT OLD.client_message_id "
            "OR NEW.content IS NOT OLD.content)) "
            "BEGIN SELECT RAISE(ABORT, 'chat directive identity is immutable'); END"
        )
        op.execute(
            "CREATE TRIGGER trg_ent_chat_authority_delete BEFORE DELETE ON enterprise_chat_message "
            "WHEN OLD.sender_type = 'human' AND instr(OLD.content, '@小菱') > 0 "
            "BEGIN SELECT RAISE(ABORT, 'chat directive cannot be deleted'); END"
        )
    else:
        raise RuntimeError("chat message authority snapshots require MySQL or SQLite")


def downgrade():
    raise RuntimeError("聊天权限快照不能自动降级，请按备份恢复流程处理")
