"""WeChat fund flows, separate from transaction order reconciliation."""

from sqlalchemy import BigInteger, Column, Date, DateTime, ForeignKey, Index, Integer, String, UniqueConstraint

from app.core.database import Base
from app.models.base import IdMixin


class WechatFundBatch(Base, IdMixin):
    __tablename__ = 'wechat_fund_batch'
    __table_args__ = (
        UniqueConstraint('bill_date', 'account_type', 'source_origin', 'merchant_fingerprint', 'file_sha256',
                         name='uq_wechat_fund_source'),
        Index('ix_wechat_fund_date', 'bill_date', 'id'),
    )
    bill_date = Column(Date, nullable=False)
    account_type = Column(String(16), nullable=False)
    source_origin = Column(String(24), nullable=False)
    merchant_fingerprint = Column(String(64), nullable=False)
    file_sha256 = Column(String(64), nullable=False)
    file_size = Column(Integer, nullable=False)
    provider_hash_type = Column(String(16))
    provider_hash_value = Column(String(40))
    row_count = Column(Integer, nullable=False)
    income_count = Column(Integer, nullable=False)
    income_fen = Column(BigInteger, nullable=False)
    expense_count = Column(Integer, nullable=False)
    expense_fen = Column(BigInteger, nullable=False)
    fee_debit_fen = Column(BigInteger, nullable=False)
    fee_refund_fen = Column(BigInteger, nullable=False)
    imported_by = Column(BigInteger, nullable=False)
    imported_at = Column(DateTime, nullable=False)


class WechatFundRow(Base, IdMixin):
    __tablename__ = 'wechat_fund_row'
    __table_args__ = (
        UniqueConstraint('batch_id', 'source_line_number', name='uq_wechat_fund_line'),
        Index('ix_wechat_fund_row_batch', 'batch_id', 'id'),
    )
    batch_id = Column(BigInteger, ForeignKey('wechat_fund_batch.id', ondelete='RESTRICT'), nullable=False)
    source_line_number = Column(Integer, nullable=False)
    booked_at = Column(DateTime, nullable=False)
    provider_business_no = Column(String(128), nullable=False)
    funding_flow_no = Column(String(128), nullable=False)
    business_name = Column(String(128), nullable=False)
    business_type = Column(String(128), nullable=False)
    direction = Column(String(8), nullable=False)
    amount_fen = Column(BigInteger, nullable=False)
    balance_fen = Column(BigInteger, nullable=False)
    business_voucher_no = Column(String(128), nullable=False)


class WechatFundGate(Base):
    __tablename__ = 'wechat_fund_gate'
    id = Column(Integer, primary_key=True)
