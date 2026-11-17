"""平台管理员托管模型调用的供应商成本估算与核对流水。"""

from datetime import datetime, timezone

from sqlalchemy import BigInteger, CheckConstraint, Column, DateTime, Index, String, UniqueConstraint

from app.core.database import Base
from app.models.base import IdMixin


def _utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


class PlatformModelCostAttempt(Base, IdMixin):
    """独立于个人/企业积分账本的平台模型成本尝试记录。"""

    __tablename__ = "platform_model_cost_attempt"
    __table_args__ = (
        UniqueConstraint("request_key", name="uq_platform_model_cost_request"),
        Index("ix_platform_cost_actor_created", "actor_user_id", "created_at", "id"),
        Index("ix_platform_cost_run_status", "run_id", "status", "id"),
        CheckConstraint(
            "status IN ('reserved', 'unknown', 'settled', 'not_sent')",
            name="ck_platform_model_cost_status",
        ),
        CheckConstraint(
            "provider_identity_version = 'deepseek-platform-cost-v1' AND provider_name = 'deepseek' "
            "AND endpoint_host = 'api.deepseek.com' AND credential_source IN ('system', 'global')",
            name="ck_platform_model_cost_provider",
        ),
        CheckConstraint(
            "input_cost_cny_micros_per_million > 0 AND output_cost_cny_micros_per_million > 0 "
            "AND max_input_tokens > 0 AND max_output_tokens > 0 AND reserved_cost_cny_micros > 0",
            name="ck_platform_model_cost_bounds",
        ),
        CheckConstraint(
            "(status = 'settled' AND actual_input_tokens IS NOT NULL AND actual_output_tokens IS NOT NULL "
            "AND actual_cost_cny_micros IS NOT NULL AND settled_at IS NOT NULL) OR "
            "(status <> 'settled' AND actual_input_tokens IS NULL AND actual_output_tokens IS NULL "
            "AND actual_cost_cny_micros IS NULL AND settled_at IS NULL)",
            name="ck_platform_model_cost_settlement_pair",
        ),
        CheckConstraint(
            "status <> 'not_sent' OR provider_attempt_started_at IS NULL",
            name="ck_platform_model_cost_not_sent",
        ),
        CheckConstraint(
            "actual_cost_cny_micros IS NULL OR actual_cost_cny_micros <= reserved_cost_cny_micros",
            name="ck_platform_model_cost_actual_limit",
        ),
        CheckConstraint(
            "actual_input_tokens IS NULL OR (actual_input_tokens >= 0 AND actual_output_tokens >= 0 "
            "AND actual_cost_cny_micros >= 0 AND actual_input_tokens <= max_input_tokens "
            "AND actual_output_tokens <= max_output_tokens)",
            name="ck_platform_model_cost_token_limit",
        ),
    )

    request_key = Column(String(160), nullable=False)
    actor_user_id = Column(BigInteger, nullable=False)
    run_id = Column(String(128), nullable=True)
    model_name = Column(String(80), nullable=False)
    pricing_version = Column(String(64), nullable=False)
    pricing_source_url = Column(String(512), nullable=False)
    pricing_source_checked_at = Column(DateTime, nullable=False)
    provider_identity_version = Column(String(40), nullable=False)
    provider_name = Column(String(32), nullable=False)
    endpoint_host = Column(String(253), nullable=False)
    credential_source = Column(String(16), nullable=False)
    input_cost_cny_micros_per_million = Column(BigInteger, nullable=False)
    output_cost_cny_micros_per_million = Column(BigInteger, nullable=False)
    max_input_tokens = Column(BigInteger, nullable=False)
    max_output_tokens = Column(BigInteger, nullable=False)
    reserved_cost_cny_micros = Column(BigInteger, nullable=False)
    actual_input_tokens = Column(BigInteger, nullable=True)
    actual_output_tokens = Column(BigInteger, nullable=True)
    actual_cost_cny_micros = Column(BigInteger, nullable=True)
    status = Column(String(16), nullable=False, default="reserved", server_default="reserved")
    provider_attempt_started_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, nullable=False, default=_utcnow)
    settled_at = Column(DateTime, nullable=True)
