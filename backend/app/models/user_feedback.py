"""
用户反馈表 ORM 模型

用户向管理员提交的意见/建议/表扬/问题反馈。
注意: 与 services/feedback_service.py(Agent 自进化的反馈聚合)语义不同,
此处是「用户 → 管理员」的产品反馈。
"""
from sqlalchemy import BigInteger, Column, DateTime, Index, String, Text

from app.core.database import Base
from app.models.base import IdMixin, TimestampMixin


class UserFeedback(Base, IdMixin, TimestampMixin):
    __tablename__ = "user_feedback"
    __table_args__ = (
        # 我的反馈(按状态) + 管理员按状态处理(对应 init.sql idx_user_status / idx_status)
        Index("ix_user_feedback_user_status", "user_id", "status"),
        Index("ix_user_feedback_status", "status"),
    )

    user_id = Column(BigInteger, nullable=False, comment="反馈人")
    feedback_type = Column(
        String(20), nullable=False, default="suggestion",
        comment="suggestion/complaint/praise/bug/other",
    )
    content = Column(Text, nullable=False, comment="反馈内容")
    contact = Column(String(100), comment="可选联系方式")
    status = Column(
        String(20), nullable=False, default="new",
        comment="new/read/replied/closed",
    )
    admin_reply = Column(Text, comment="管理员回复")
    handled_by = Column(BigInteger, comment="处理管理员ID")
    handled_at = Column(DateTime, comment="处理时间(UTC)")
