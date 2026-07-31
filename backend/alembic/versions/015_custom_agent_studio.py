"""add declarative custom agent studio

Revision ID: 015
Revises: 014
Create Date: 2026-07-31
"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "015"
down_revision: Union[str, None] = "014"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _id_column() -> sa.Column:
    return sa.Column(
        "id",
        sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
        primary_key=True,
        autoincrement=True,
    )


def _timestamps() -> tuple[sa.Column, sa.Column]:
    return (
        sa.Column("create_time", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("update_time", sa.DateTime(), nullable=False, server_default=sa.func.now()),
    )


def upgrade() -> None:
    op.create_table(
        "custom_agent",
        _id_column(),
        sa.Column("code", sa.String(80), nullable=False),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("description", sa.Text()),
        sa.Column("owner_id", sa.BigInteger(), nullable=False),
        sa.Column("current_published_version_id", sa.BigInteger()),
        sa.Column("status", sa.String(30), nullable=False, server_default="draft"),
        sa.Column("is_enabled", sa.SmallInteger(), nullable=False, server_default="0"),
        *_timestamps(),
        sa.UniqueConstraint("code", name="uk_custom_agent_code"),
    )
    op.create_index("ix_custom_agent_owner", "custom_agent", ["owner_id"])
    op.create_index("ix_custom_agent_enabled", "custom_agent", ["is_enabled"])

    op.create_table(
        "custom_agent_version",
        _id_column(),
        sa.Column("agent_id", sa.BigInteger(), nullable=False),
        sa.Column("version_number", sa.Integer(), nullable=False),
        sa.Column("prompt", sa.Text(), nullable=False),
        sa.Column("review_focus", sa.Text(), nullable=False),
        sa.Column("model_config_json", sa.Text(), nullable=False),
        sa.Column("input_schema_json", sa.Text(), nullable=False),
        sa.Column("output_schema_json", sa.Text(), nullable=False),
        sa.Column("checksum", sa.String(64), nullable=False),
        sa.Column("status", sa.String(30), nullable=False, server_default="draft"),
        sa.Column("original_author_id", sa.BigInteger(), nullable=False),
        sa.Column("revised_by", sa.BigInteger()),
        sa.Column("revision_note", sa.String(500)),
        sa.Column("test_evidence_json", sa.Text()),
        sa.Column("tested_checksum", sa.String(64)),
        sa.Column("submitted_at", sa.DateTime()),
        *_timestamps(),
        sa.UniqueConstraint("agent_id", "version_number", name="uk_custom_agent_version"),
    )
    op.create_index("ix_custom_agent_version_status", "custom_agent_version", ["status"])

    op.create_table(
        "custom_skill",
        _id_column(),
        sa.Column("code", sa.String(100), nullable=False),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("description", sa.Text()),
        sa.Column("owner_id", sa.BigInteger(), nullable=False),
        sa.Column("current_published_version_id", sa.BigInteger()),
        sa.Column("status", sa.String(30), nullable=False, server_default="draft"),
        *_timestamps(),
        sa.UniqueConstraint("code", name="uk_custom_skill_code"),
    )
    op.create_index("ix_custom_skill_owner", "custom_skill", ["owner_id"])

    op.create_table(
        "custom_skill_version",
        _id_column(),
        sa.Column("skill_id", sa.BigInteger(), nullable=False),
        sa.Column("version_number", sa.Integer(), nullable=False),
        sa.Column("skill_type", sa.String(30), nullable=False),
        sa.Column("definition_json", sa.Text(), nullable=False),
        sa.Column("requested_capabilities_json", sa.Text(), nullable=False),
        sa.Column("checksum", sa.String(64), nullable=False),
        sa.Column("status", sa.String(30), nullable=False, server_default="draft"),
        sa.Column("original_author_id", sa.BigInteger(), nullable=False),
        sa.Column("revised_by", sa.BigInteger()),
        sa.Column("revision_note", sa.String(500)),
        sa.Column("test_evidence_json", sa.Text()),
        sa.Column("tested_checksum", sa.String(64)),
        *_timestamps(),
        sa.UniqueConstraint("skill_id", "version_number", name="uk_custom_skill_version"),
    )
    op.create_index("ix_custom_skill_version_status", "custom_skill_version", ["status"])

    op.create_table(
        "custom_agent_skill_binding",
        _id_column(),
        sa.Column("agent_version_id", sa.BigInteger(), nullable=False),
        sa.Column("skill_version_id", sa.BigInteger(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("config_json", sa.Text(), nullable=False),
        *_timestamps(),
        sa.UniqueConstraint("agent_version_id", "skill_version_id", name="uk_custom_agent_skill_binding"),
        sa.UniqueConstraint("agent_version_id", "position", name="uk_custom_agent_skill_position"),
    )

    op.create_table(
        "custom_agent_release",
        _id_column(),
        sa.Column("agent_id", sa.BigInteger(), nullable=False),
        sa.Column("agent_version_id", sa.BigInteger(), nullable=False),
        sa.Column("approval_id", sa.BigInteger()),
        sa.Column("previous_release_id", sa.BigInteger()),
        sa.Column("rollback_of_release_id", sa.BigInteger()),
        sa.Column("package_manifest_json", sa.Text(), nullable=False),
        sa.Column("package_checksum", sa.String(64), nullable=False),
        sa.Column("status", sa.String(30), nullable=False, server_default="published"),
        sa.Column("published_by", sa.BigInteger(), nullable=False),
        sa.Column("published_at", sa.DateTime(), nullable=False),
        sa.Column("disabled_at", sa.DateTime()),
        *_timestamps(),
        sa.UniqueConstraint("approval_id", name="uk_custom_agent_release_approval"),
    )
    op.create_index("ix_custom_agent_release_agent", "custom_agent_release", ["agent_id", "status"])

    op.create_table(
        "review_task_agent_release",
        _id_column(),
        sa.Column("task_id", sa.BigInteger(), nullable=False),
        sa.Column("release_id", sa.BigInteger(), nullable=False),
        sa.Column("agent_version_id", sa.BigInteger(), nullable=False),
        sa.Column("package_manifest_json", sa.Text(), nullable=False),
        sa.Column("status", sa.String(30), nullable=False, server_default="snapshotted"),
        *_timestamps(),
        sa.UniqueConstraint("task_id", "release_id", name="uk_review_task_agent_release"),
    )
    op.create_index("ix_review_task_agent_release_task", "review_task_agent_release", ["task_id"])
    _seed_permissions()


def _seed_permissions() -> None:
    conn = op.get_bind()
    permission = sa.table(
        "permission",
        sa.column("id", sa.BigInteger()),
        sa.column("code", sa.String()),
        sa.column("name", sa.String()),
        sa.column("module", sa.String()),
        sa.column("type", sa.String()),
        sa.column("description", sa.String()),
    )
    rows = [
        ("agent_asset:create", "创建 Agent 资产", "审查员创建声明式 Agent"),
        ("agent_asset:update_own", "修改自有 Agent", "审查员修改自有 Agent 草稿"),
        ("agent_asset:test", "测试 Agent", "测试声明式 Agent 发布包"),
        ("agent_asset:submit", "提交 Agent", "提交 Agent 发布审批"),
        ("skill_asset:create", "创建 Skill 资产", "审查员创建受控 Skill"),
        ("skill_asset:update_own", "修改自有 Skill", "审查员修改自有 Skill 草稿"),
        ("custom_agent:invoke", "调用全局 Agent", "调用已发布自定义 Agent"),
        ("agent_asset:approve", "审批 Agent", "审批或驳回 Agent 发布包"),
        ("agent_asset:publish", "发布 Agent", "原子发布 Agent 和 Skill 版本"),
        ("agent_asset:disable", "停用 Agent", "停用已发布 Agent"),
        ("agent_asset:rollback", "回滚 Agent", "回滚 Agent 发布版本"),
    ]
    existing = set(
        conn.execute(sa.select(permission.c.code).where(permission.c.code.in_([row[0] for row in rows]))).scalars()
    )
    conn.execute(
        permission.insert(),
        [
            {"code": code, "name": name, "module": "agent", "type": "api", "description": desc}
            for code, name, desc in rows
            if code not in existing
        ],
    )
    role = sa.table("role", sa.column("id", sa.BigInteger()), sa.column("code", sa.String()))
    role_permission = sa.table(
        "role_permission", sa.column("role_id", sa.BigInteger()), sa.column("permission_id", sa.BigInteger())
    )
    role_ids = dict(
        conn.execute(
            sa.select(role.c.code, role.c.id).where(role.c.code.in_(["user", "reviewer", "admin", "super_admin"]))
        ).all()
    )
    permission_ids = dict(
        conn.execute(
            sa.select(permission.c.code, permission.c.id).where(permission.c.code.in_([row[0] for row in rows]))
        ).all()
    )
    creator_codes = {row[0] for row in rows[:7]}
    grants = {
        "user": {"custom_agent:invoke"},
        "reviewer": creator_codes,
        "admin": set(permission_ids),
        "super_admin": set(permission_ids),
    }
    existing_pairs = set(conn.execute(sa.select(role_permission.c.role_id, role_permission.c.permission_id)).all())
    links = [
        {"role_id": role_ids[role_code], "permission_id": permission_ids[code]}
        for role_code, codes in grants.items()
        if role_code in role_ids
        for code in codes
        if (role_ids[role_code], permission_ids[code]) not in existing_pairs
    ]
    if links:
        conn.execute(role_permission.insert(), links)


def downgrade() -> None:
    conn = op.get_bind()
    codes = [
        "agent_asset:create",
        "agent_asset:update_own",
        "agent_asset:test",
        "agent_asset:submit",
        "skill_asset:create",
        "skill_asset:update_own",
        "custom_agent:invoke",
        "agent_asset:approve",
        "agent_asset:publish",
        "agent_asset:disable",
        "agent_asset:rollback",
    ]
    permission = sa.table("permission", sa.column("id", sa.BigInteger()), sa.column("code", sa.String()))
    role_permission = sa.table("role_permission", sa.column("permission_id", sa.BigInteger()))
    ids = list(conn.execute(sa.select(permission.c.id).where(permission.c.code.in_(codes))).scalars())
    if ids:
        conn.execute(role_permission.delete().where(role_permission.c.permission_id.in_(ids)))
        conn.execute(permission.delete().where(permission.c.id.in_(ids)))
    op.drop_index("ix_review_task_agent_release_task", table_name="review_task_agent_release")
    op.drop_table("review_task_agent_release")
    op.drop_index("ix_custom_agent_release_agent", table_name="custom_agent_release")
    op.drop_table("custom_agent_release")
    op.drop_table("custom_agent_skill_binding")
    op.drop_index("ix_custom_skill_version_status", table_name="custom_skill_version")
    op.drop_table("custom_skill_version")
    op.drop_index("ix_custom_skill_owner", table_name="custom_skill")
    op.drop_table("custom_skill")
    op.drop_index("ix_custom_agent_version_status", table_name="custom_agent_version")
    op.drop_table("custom_agent_version")
    op.drop_index("ix_custom_agent_enabled", table_name="custom_agent")
    op.drop_index("ix_custom_agent_owner", table_name="custom_agent")
    op.drop_table("custom_agent")
