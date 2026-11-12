"""Persist normalized, un-reconciled WeChat fund statements."""

import sqlalchemy as sa

from alembic import op

revision = '117_wechat_fund_statement'
down_revision = '116_ent_billing_group'
branch_labels = None
depends_on = None


def upgrade():
    ident = sa.BigInteger().with_variant(sa.Integer(), 'sqlite')
    op.create_table('wechat_fund_batch',
        sa.Column('id', ident, primary_key=True, autoincrement=True),
        sa.Column('bill_date', sa.Date(), nullable=False),
        sa.Column('account_type', sa.String(16), nullable=False),
        sa.Column('source_origin', sa.String(24), nullable=False),
        sa.Column('merchant_fingerprint', sa.String(64), nullable=False),
        sa.Column('file_sha256', sa.String(64), nullable=False),
        sa.Column('file_size', sa.Integer(), nullable=False),
        sa.Column('provider_hash_type', sa.String(16)),
        sa.Column('provider_hash_value', sa.String(40)),
        sa.Column('row_count', sa.Integer(), nullable=False),
        sa.Column('income_count', sa.Integer(), nullable=False),
        sa.Column('income_fen', sa.BigInteger(), nullable=False),
        sa.Column('expense_count', sa.Integer(), nullable=False),
        sa.Column('expense_fen', sa.BigInteger(), nullable=False),
        sa.Column('fee_debit_fen', sa.BigInteger(), nullable=False),
        sa.Column('fee_refund_fen', sa.BigInteger(), nullable=False),
        sa.Column('imported_by', sa.BigInteger(), nullable=False),
        sa.Column('imported_at', sa.DateTime(), nullable=False),
        sa.UniqueConstraint('bill_date', 'account_type', 'source_origin', 'merchant_fingerprint', 'file_sha256',
                            name='uq_wechat_fund_source'),
    )
    op.create_index('ix_wechat_fund_date', 'wechat_fund_batch', ['bill_date', 'id'])
    op.create_table('wechat_fund_row',
        sa.Column('id', ident, primary_key=True, autoincrement=True),
        sa.Column('batch_id', sa.BigInteger(), sa.ForeignKey('wechat_fund_batch.id', ondelete='RESTRICT'),
                  nullable=False),
        sa.Column('source_line_number', sa.Integer(), nullable=False),
        sa.Column('booked_at', sa.DateTime(), nullable=False),
        sa.Column('provider_business_no', sa.String(128), nullable=False),
        sa.Column('funding_flow_no', sa.String(128), nullable=False),
        sa.Column('business_name', sa.String(128), nullable=False),
        sa.Column('business_type', sa.String(128), nullable=False),
        sa.Column('direction', sa.String(8), nullable=False),
        sa.Column('amount_fen', sa.BigInteger(), nullable=False),
        sa.Column('balance_fen', sa.BigInteger(), nullable=False),
        sa.Column('business_voucher_no', sa.String(128), nullable=False),
        sa.UniqueConstraint('batch_id', 'source_line_number', name='uq_wechat_fund_line'),
    )
    op.create_index('ix_wechat_fund_row_batch', 'wechat_fund_row', ['batch_id', 'id'])
    gate = op.create_table('wechat_fund_gate', sa.Column('id', sa.Integer(), primary_key=True))
    op.bulk_insert(gate, [{'id': 1}])


def downgrade():
    raise RuntimeError('资金流水证据不能自动降级，请按备份恢复流程处理')
