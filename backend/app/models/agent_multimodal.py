"""小菱多模态消息资产 ORM(输入/输出图片留档)

多模态请求中的图片以二进制留档于本表供审计与回放;运行检查点
(checkpoint_json)只存 prism-asset://<sha256> 占位符,真实 data URL 仅在
运行期活于内存,避免巨型 base64 反复写库。
"""
from sqlalchemy import BigInteger, Column, Index, LargeBinary, String
from sqlalchemy.dialects.mysql import MEDIUMBLOB

from app.core.database import Base
from app.models.base import IdMixin, TimestampMixin


class AgentMultimodalAsset(Base, IdMixin, TimestampMixin):
    __tablename__ = "agent_multimodal_asset"
    __table_args__ = (
        Index("ix_agent_multimodal_asset_run", "run_id"),
        Index("ix_agent_multimodal_asset_user", "user_id"),
    )

    run_id = Column(String(80), nullable=False, comment="所属运行 run_id")
    user_id = Column(BigInteger, nullable=False, comment="归属用户")
    surface = Column(String(20), nullable=False, default="user", comment="user/admin")
    role = Column(String(10), nullable=False, default="input", comment="input/output")
    mime = Column(String(32), nullable=False, comment="图片 MIME(服务端嗅探)")
    sha256 = Column(String(64), nullable=False, comment="内容摘要(占位符关联键)")
    data = Column(LargeBinary().with_variant(MEDIUMBLOB(), "mysql"),
                  nullable=False, comment="图片二进制(≤1.5MB)")
