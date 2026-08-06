"""MCP、能力目录与代码沙箱 ORM 模型。"""

from sqlalchemy import BigInteger, Column, DateTime, Float, Index, Integer, SmallInteger, String, Text
from sqlalchemy.dialects.mysql import LONGTEXT

from app.core.database import Base
from app.models.base import IdMixin, TimestampMixin


class McpServer(Base, IdMixin, TimestampMixin):
    __tablename__ = "mcp_server"
    __table_args__ = (Index("ix_mcp_server_code", "code", unique=True),)

    code = Column(String(80), nullable=False)
    name = Column(String(160), nullable=False)
    description = Column(Text)
    transport = Column(String(30), nullable=False, default="streamable_http")
    url = Column(String(500))
    auth_type = Column(String(30), nullable=False, default="none")
    encrypted_headers = Column(LONGTEXT().with_variant(Text, "sqlite"))
    managed_kind = Column(String(50))
    status = Column(String(30), nullable=False, default="disabled")
    enabled = Column(SmallInteger, nullable=False, default=0)
    credential_required = Column(SmallInteger, nullable=False, default=0)
    last_health_at = Column(DateTime)
    last_error = Column(String(1000))


class McpTool(Base, IdMixin, TimestampMixin):
    __tablename__ = "mcp_tool"
    __table_args__ = (
        Index("ix_mcp_tool_server", "server_id"),
        Index("ix_mcp_tool_model_name", "model_name", unique=True),
        Index("uk_mcp_tool_server_name", "server_id", "tool_name", unique=True),
    )

    server_id = Column(BigInteger, nullable=False)
    tool_name = Column(String(160), nullable=False)
    model_name = Column(String(160), nullable=False)
    display_name = Column(String(200), nullable=False)
    description = Column(Text)
    input_schema_json = Column(LONGTEXT().with_variant(Text, "sqlite"), nullable=False)
    annotations_json = Column(Text)
    schema_sha256 = Column(String(64), nullable=False)
    risk_level = Column(String(30), nullable=False, default="low")
    enabled = Column(SmallInteger, nullable=False, default=1)


class AgentMcpBinding(Base, IdMixin, TimestampMixin):
    __tablename__ = "agent_mcp_binding"
    __table_args__ = (
        Index("ix_agent_mcp_binding_agent", "agent_code"),
        Index("uk_agent_mcp_binding", "agent_code", "tool_id", unique=True),
    )

    agent_code = Column(String(80), nullable=False)
    tool_id = Column(BigInteger, nullable=False)
    permission = Column(String(30), nullable=False, default="allow")
    requires_approval = Column(SmallInteger, nullable=False, default=0)
    bound_schema_sha256 = Column(String(64), nullable=False)
    enabled = Column(SmallInteger, nullable=False, default=1)


class AgentCapabilityAlias(Base, IdMixin, TimestampMixin):
    __tablename__ = "agent_capability_alias"
    __table_args__ = (
        Index("ix_agent_capability_alias_code", "capability_code"),
        Index(
            "uk_agent_capability_alias",
            "capability_code",
            "locale",
            "normalized_alias",
            unique=True,
        ),
    )

    capability_code = Column(String(255), nullable=False)
    alias = Column(String(160), nullable=False)
    normalized_alias = Column(String(160), nullable=False)
    locale = Column(String(20), nullable=False, default="zh-CN")
    weight = Column(Float, nullable=False, default=1.0)
    enabled = Column(SmallInteger, nullable=False, default=1)


class SandboxWorker(Base, IdMixin, TimestampMixin):
    __tablename__ = "sandbox_worker"
    __table_args__ = (
        Index("ix_sandbox_worker_code", "code", unique=True),
        Index("ix_sandbox_worker_status", "status", "enabled"),
    )

    code = Column(String(80), nullable=False)
    name = Column(String(160), nullable=False)
    worker_type = Column(String(30), nullable=False, comment="local/managed/production_fallback")
    transport = Column(String(30), nullable=False, default="unix")
    endpoint = Column(String(500), nullable=False)
    encrypted_token = Column(Text)
    supported_languages_json = Column(Text, nullable=False)
    supported_modes_json = Column(Text, nullable=False)
    runtime = Column(String(50), nullable=False, default="runsc")
    max_concurrency = Column(Integer, nullable=False, default=1)
    priority = Column(Integer, nullable=False, default=50)
    status = Column(String(30), nullable=False, default="offline")
    enabled = Column(SmallInteger, nullable=False, default=0)
    last_seen_at = Column(DateTime)
    last_error = Column(String(1000))
    fingerprint_json = Column(Text)


class SandboxEnvironment(Base, IdMixin, TimestampMixin):
    __tablename__ = "sandbox_environment"
    __table_args__ = (
        Index("ix_sandbox_environment_public", "public_id", unique=True),
        Index("ix_sandbox_environment_owner", "owner_id", "status"),
        Index("ix_sandbox_environment_project", "project_id", "status"),
        Index("ix_sandbox_environment_expiry", "status", "expires_at"),
    )

    public_id = Column(String(40), nullable=False)
    project_id = Column(BigInteger, nullable=False)
    owner_id = Column(BigInteger, nullable=False)
    worker_id = Column(BigInteger)
    agent_code = Column(String(80), nullable=False)
    purpose = Column(String(30), nullable=False, comment="test/deploy")
    language = Column(String(30), nullable=False)
    test_mode = Column(String(30), nullable=False, default="whitebox")
    status = Column(String(30), nullable=False, default="queued")
    runtime = Column(String(50), nullable=False, default="runsc")
    image_ref = Column(String(300), nullable=False)
    image_digest = Column(String(100))
    source_sha256 = Column(String(64), nullable=False)
    resource_policy_json = Column(Text, nullable=False)
    agent_config_json = Column(LONGTEXT().with_variant(Text, "sqlite"), nullable=False)
    executor_ref = Column(String(160))
    preview_path = Column(String(240))
    remote_target_url = Column(String(500))
    remote_target_authorized_at = Column(DateTime)
    expires_at = Column(DateTime, nullable=False)
    started_at = Column(DateTime)
    stopped_at = Column(DateTime)
    result_json = Column(LONGTEXT().with_variant(Text, "sqlite"))
    error = Column(Text)


class SandboxEvent(Base, IdMixin):
    __tablename__ = "sandbox_event"
    __table_args__ = (Index("ix_sandbox_event_environment", "environment_id", "id"),)

    environment_id = Column(BigInteger, nullable=False)
    event_type = Column(String(40), nullable=False)
    stage = Column(String(50), nullable=False)
    message = Column(String(500), nullable=False)
    payload_json = Column(LONGTEXT().with_variant(Text, "sqlite"))
    create_time = Column(DateTime, nullable=False)


class SandboxArtifact(Base, IdMixin, TimestampMixin):
    __tablename__ = "sandbox_artifact"
    __table_args__ = (Index("ix_sandbox_artifact_environment", "environment_id"),)

    environment_id = Column(BigInteger, nullable=False)
    artifact_type = Column(String(50), nullable=False)
    file_name = Column(String(240), nullable=False)
    mime_type = Column(String(120), nullable=False, default="application/octet-stream")
    byte_size = Column(Integer, nullable=False, default=0)
    sha256 = Column(String(64), nullable=False)
    storage_ref = Column(String(500), nullable=False)
    content_base64 = Column(LONGTEXT().with_variant(Text, "sqlite"), nullable=False)
