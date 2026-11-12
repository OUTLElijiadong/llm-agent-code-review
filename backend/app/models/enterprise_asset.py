"""隔离存储的企业 Agent/Skill 私有草稿；不属于旧全局发布模型。"""
from sqlalchemy import BigInteger, Column, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint

from app.core.database import Base
from app.models.base import IdMixin, TimestampMixin


class EnterpriseAssetDraft(Base, IdMixin, TimestampMixin):
    __tablename__ = "enterprise_asset_draft"
    __table_args__ = (
        UniqueConstraint("enterprise_id", "creator_id", "request_key", name="uq_ent_asset_create"),
        Index("ix_ent_asset_owner", "enterprise_id", "creator_id", "status", "id"),
    )

    enterprise_id = Column(BigInteger, nullable=False)
    creator_id = Column(BigInteger, nullable=False)
    kind = Column(String(16), nullable=False)
    title = Column(String(120), nullable=False)
    definition_json = Column(Text, nullable=False)
    current_version = Column(Integer, nullable=False, default=1)
    request_key = Column(String(80), nullable=False)
    request_digest = Column(String(64), nullable=False)
    status = Column(String(16), nullable=False, default="active")


class EnterpriseAssetDraftVersion(Base, IdMixin, TimestampMixin):
    __tablename__ = "enterprise_asset_draft_version"
    __table_args__ = (
        UniqueConstraint("draft_id", "version_number", name="uq_ent_asset_version"),
        UniqueConstraint("draft_id", "request_key", name="uq_ent_asset_version_req"),
    )

    draft_id = Column(BigInteger, nullable=False, index=True)
    version_number = Column(Integer, nullable=False)
    title = Column(String(120), nullable=False)
    definition_json = Column(Text, nullable=False)
    request_key = Column(String(80), nullable=False)
    request_digest = Column(String(64), nullable=False)


class EnterpriseAssetPublication(Base, IdMixin, TimestampMixin):
    """Immutable approval snapshot; only compatible assets can run through metered Xiaoling calls."""

    __tablename__ = "enterprise_asset_publication"
    __table_args__ = (
        UniqueConstraint("enterprise_id", "requested_by", "request_key", name="uq_ent_asset_pub_request"),
        Index("ix_ent_asset_pub_queue", "enterprise_id", "scope", "status", "id"),
        Index("ix_ent_asset_pub_draft", "draft_id", "scope", "status", "id"),
    )

    enterprise_id = Column(BigInteger, nullable=False)
    draft_id = Column(BigInteger, ForeignKey("enterprise_asset_draft.id", ondelete="RESTRICT"), nullable=False)
    version_id = Column(
        BigInteger, ForeignKey("enterprise_asset_draft_version.id", ondelete="RESTRICT"), nullable=False,
    )
    version_number = Column(Integer, nullable=False)
    requested_by = Column(BigInteger, nullable=False)
    kind = Column(String(16), nullable=False)
    scope = Column(String(16), nullable=False)  # enterprise / global
    status = Column(String(16), nullable=False, default="pending", server_default="pending")
    title = Column(String(120), nullable=False)
    definition_json = Column(Text, nullable=False)
    content_sha256 = Column(String(64), nullable=False)
    request_key = Column(String(80), nullable=False)
    request_digest = Column(String(64), nullable=False)
    decided_by = Column(BigInteger, nullable=True)
    decided_at = Column(DateTime, nullable=True)
    decision_reason = Column(String(500), nullable=False, default="", server_default="")
    revoked_by = Column(BigInteger, nullable=True)
    revoked_at = Column(DateTime, nullable=True)
    revocation_reason = Column(String(500), nullable=False, default="", server_default="")
    withdrawn_at = Column(DateTime, nullable=True)


class EnterpriseAssistantAssetInvocation(Base, IdMixin, TimestampMixin):
    """Idempotent result cache for a single published asset tool call."""

    __tablename__ = "enterprise_assistant_asset_invocation"
    __table_args__ = (
        UniqueConstraint("assistant_run_id", "tool_call_id", name="uq_ent_asset_call_invocation"),
        UniqueConstraint("invocation_id", name="uq_ent_asset_invocation_public_id"),
        UniqueConstraint("request_key_prefix", name="uq_ent_asset_invocation_request"),
        Index("ix_ent_asset_invocation_scope", "enterprise_id", "conversation_id", "status", "id"),
    )

    enterprise_id = Column(BigInteger, nullable=False)
    conversation_id = Column(BigInteger, nullable=False)
    assistant_run_id = Column(
        BigInteger, ForeignKey("enterprise_assistant_run.id", ondelete="RESTRICT"), nullable=False,
    )
    initiator_user_id = Column(BigInteger, nullable=False)
    tool_call_id = Column(String(120), nullable=False)
    invocation_id = Column(String(80), nullable=False)
    publication_id = Column(
        BigInteger, ForeignKey("enterprise_asset_publication.id", ondelete="RESTRICT"), nullable=False,
    )
    publication_version_id = Column(BigInteger, nullable=False)
    publication_sha256 = Column(String(64), nullable=False)
    input_sha256 = Column(String(64), nullable=False)
    request_key_prefix = Column(String(120), nullable=False)
    status = Column(String(24), nullable=False, default="running", server_default="running")
    execution_token = Column(String(80), nullable=False)
    lease_expires_at = Column(DateTime, nullable=False)
    output_text = Column(Text, nullable=True)
    error_code = Column(String(64), nullable=True)
