"""Add MCP governance, capability aliases and sandbox lifecycle.

Revision ID: 023
Revises: 022
"""

from typing import Sequence, Union

import sqlalchemy as sa
from sqlalchemy.dialects.mysql import LONGTEXT

from alembic import op

revision: str = "023"
down_revision: Union[str, None] = "022"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

LONG_TEXT = LONGTEXT().with_variant(sa.Text(), "sqlite")


def upgrade() -> None:
    op.create_table(
        "mcp_server",
        sa.Column("id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), primary_key=True),
        sa.Column("code", sa.String(80), nullable=False),
        sa.Column("name", sa.String(160), nullable=False),
        sa.Column("description", sa.Text()),
        sa.Column("transport", sa.String(30), nullable=False, server_default="streamable_http"),
        sa.Column("url", sa.String(500)),
        sa.Column("auth_type", sa.String(30), nullable=False, server_default="none"),
        sa.Column("encrypted_headers", LONG_TEXT),
        sa.Column("managed_kind", sa.String(50)),
        sa.Column("status", sa.String(30), nullable=False, server_default="disabled"),
        sa.Column("enabled", sa.SmallInteger(), nullable=False, server_default="0"),
        sa.Column("credential_required", sa.SmallInteger(), nullable=False, server_default="0"),
        sa.Column("last_health_at", sa.DateTime()),
        sa.Column("last_error", sa.String(1000)),
        sa.Column("create_time", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("update_time", sa.DateTime(), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_mcp_server_code", "mcp_server", ["code"], unique=True)
    op.create_table(
        "mcp_tool",
        sa.Column("id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), primary_key=True),
        sa.Column("server_id", sa.BigInteger(), nullable=False),
        sa.Column("tool_name", sa.String(160), nullable=False),
        sa.Column("model_name", sa.String(160), nullable=False),
        sa.Column("display_name", sa.String(200), nullable=False),
        sa.Column("description", sa.Text()),
        sa.Column("input_schema_json", LONG_TEXT, nullable=False),
        sa.Column("annotations_json", sa.Text()),
        sa.Column("schema_sha256", sa.String(64), nullable=False),
        sa.Column("risk_level", sa.String(30), nullable=False, server_default="low"),
        sa.Column("enabled", sa.SmallInteger(), nullable=False, server_default="1"),
        sa.Column("create_time", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("update_time", sa.DateTime(), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_mcp_tool_server", "mcp_tool", ["server_id"])
    op.create_index("ix_mcp_tool_model_name", "mcp_tool", ["model_name"], unique=True)
    op.create_index("uk_mcp_tool_server_name", "mcp_tool", ["server_id", "tool_name"], unique=True)
    op.create_table(
        "agent_mcp_binding",
        sa.Column("id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), primary_key=True),
        sa.Column("agent_code", sa.String(80), nullable=False),
        sa.Column("tool_id", sa.BigInteger(), nullable=False),
        sa.Column("permission", sa.String(30), nullable=False, server_default="allow"),
        sa.Column("requires_approval", sa.SmallInteger(), nullable=False, server_default="0"),
        sa.Column("bound_schema_sha256", sa.String(64), nullable=False),
        sa.Column("enabled", sa.SmallInteger(), nullable=False, server_default="1"),
        sa.Column("create_time", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("update_time", sa.DateTime(), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_agent_mcp_binding_agent", "agent_mcp_binding", ["agent_code"])
    op.create_index("uk_agent_mcp_binding", "agent_mcp_binding", ["agent_code", "tool_id"], unique=True)
    op.create_table(
        "agent_capability_alias",
        sa.Column("id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), primary_key=True),
        sa.Column("capability_code", sa.String(255), nullable=False),
        sa.Column("alias", sa.String(160), nullable=False),
        sa.Column("normalized_alias", sa.String(160), nullable=False),
        sa.Column("locale", sa.String(20), nullable=False, server_default="zh-CN"),
        sa.Column("weight", sa.Float(), nullable=False, server_default="1"),
        sa.Column("enabled", sa.SmallInteger(), nullable=False, server_default="1"),
        sa.Column("create_time", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("update_time", sa.DateTime(), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_agent_capability_alias_code", "agent_capability_alias", ["capability_code"])
    op.create_index(
        "uk_agent_capability_alias",
        "agent_capability_alias",
        ["capability_code", "locale", "normalized_alias"],
        unique=True,
    )
    op.create_table(
        "sandbox_worker",
        sa.Column("id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), primary_key=True),
        sa.Column("code", sa.String(80), nullable=False),
        sa.Column("name", sa.String(160), nullable=False),
        sa.Column("worker_type", sa.String(30), nullable=False),
        sa.Column("transport", sa.String(30), nullable=False, server_default="unix"),
        sa.Column("endpoint", sa.String(500), nullable=False),
        sa.Column("encrypted_token", sa.Text()),
        sa.Column("supported_languages_json", sa.Text(), nullable=False),
        sa.Column("supported_modes_json", sa.Text(), nullable=False),
        sa.Column("runtime", sa.String(50), nullable=False, server_default="runsc"),
        sa.Column("max_concurrency", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("priority", sa.Integer(), nullable=False, server_default="50"),
        sa.Column("status", sa.String(30), nullable=False, server_default="offline"),
        sa.Column("enabled", sa.SmallInteger(), nullable=False, server_default="0"),
        sa.Column("last_seen_at", sa.DateTime()),
        sa.Column("last_error", sa.String(1000)),
        sa.Column("fingerprint_json", sa.Text()),
        sa.Column("create_time", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("update_time", sa.DateTime(), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_sandbox_worker_code", "sandbox_worker", ["code"], unique=True)
    op.create_index("ix_sandbox_worker_status", "sandbox_worker", ["status", "enabled"])
    op.create_table(
        "sandbox_environment",
        sa.Column("id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), primary_key=True),
        sa.Column("public_id", sa.String(40), nullable=False),
        sa.Column("project_id", sa.BigInteger(), nullable=False),
        sa.Column("owner_id", sa.BigInteger(), nullable=False),
        sa.Column("worker_id", sa.BigInteger()),
        sa.Column("agent_code", sa.String(80), nullable=False),
        sa.Column("purpose", sa.String(30), nullable=False),
        sa.Column("language", sa.String(30), nullable=False),
        sa.Column("test_mode", sa.String(30), nullable=False, server_default="whitebox"),
        sa.Column("status", sa.String(30), nullable=False, server_default="queued"),
        sa.Column("runtime", sa.String(50), nullable=False, server_default="runsc"),
        sa.Column("image_ref", sa.String(300), nullable=False),
        sa.Column("image_digest", sa.String(100)),
        sa.Column("source_sha256", sa.String(64), nullable=False),
        sa.Column("resource_policy_json", sa.Text(), nullable=False),
        sa.Column("agent_config_json", LONG_TEXT, nullable=False),
        sa.Column("executor_ref", sa.String(160)),
        sa.Column("preview_path", sa.String(240)),
        sa.Column("remote_target_url", sa.String(500)),
        sa.Column("remote_target_authorized_at", sa.DateTime()),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("started_at", sa.DateTime()),
        sa.Column("stopped_at", sa.DateTime()),
        sa.Column("result_json", LONG_TEXT),
        sa.Column("error", sa.Text()),
        sa.Column("create_time", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("update_time", sa.DateTime(), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_sandbox_environment_public", "sandbox_environment", ["public_id"], unique=True)
    op.create_index("ix_sandbox_environment_owner", "sandbox_environment", ["owner_id", "status"])
    op.create_index("ix_sandbox_environment_project", "sandbox_environment", ["project_id", "status"])
    op.create_index("ix_sandbox_environment_expiry", "sandbox_environment", ["status", "expires_at"])
    op.create_table(
        "sandbox_event",
        sa.Column("id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), primary_key=True),
        sa.Column("environment_id", sa.BigInteger(), nullable=False),
        sa.Column("event_type", sa.String(40), nullable=False),
        sa.Column("stage", sa.String(50), nullable=False),
        sa.Column("message", sa.String(500), nullable=False),
        sa.Column("payload_json", LONG_TEXT),
        sa.Column("create_time", sa.DateTime(), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_sandbox_event_environment", "sandbox_event", ["environment_id", "id"])
    op.create_table(
        "sandbox_artifact",
        sa.Column("id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), primary_key=True),
        sa.Column("environment_id", sa.BigInteger(), nullable=False),
        sa.Column("artifact_type", sa.String(50), nullable=False),
        sa.Column("file_name", sa.String(240), nullable=False),
        sa.Column("mime_type", sa.String(120), nullable=False, server_default="application/octet-stream"),
        sa.Column("byte_size", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("storage_ref", sa.String(500), nullable=False),
        sa.Column("content_base64", LONG_TEXT, nullable=False),
        sa.Column("create_time", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("update_time", sa.DateTime(), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_sandbox_artifact_environment", "sandbox_artifact", ["environment_id"])


def downgrade() -> None:
    for table in (
        "sandbox_artifact",
        "sandbox_event",
        "sandbox_environment",
        "sandbox_worker",
        "agent_capability_alias",
        "agent_mcp_binding",
        "mcp_tool",
        "mcp_server",
    ):
        op.drop_table(table)
