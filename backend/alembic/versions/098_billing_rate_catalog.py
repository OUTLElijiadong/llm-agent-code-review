"""Add reviewed, scheduled model pricing catalogs.

Revision ID: 098_billing_rate_catalog
Revises: 097_ent_team_project
"""

from datetime import datetime
from hashlib import sha256

import sqlalchemy as sa
from sqlalchemy import insert, select

from alembic import op

revision = "098_billing_rate_catalog"
down_revision = "097_ent_team_project"
branch_labels = None
depends_on = None


def _id_type():
    return sa.BigInteger().with_variant(sa.Integer(), "sqlite")


def _install_immutable_triggers() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        op.execute(
            "CREATE TRIGGER trg_billing_rate_catalog_snapshot_immutable BEFORE UPDATE ON billing_rate_catalog "
            "WHEN NEW.version_key IS NOT OLD.version_key OR NEW.effective_at IS NOT OLD.effective_at "
            "OR NEW.source_url IS NOT OLD.source_url OR NEW.source_checked_at IS NOT OLD.source_checked_at "
            "OR NEW.fx_source_url IS NOT OLD.fx_source_url "
            "OR NEW.fx_source_checked_at IS NOT OLD.fx_source_checked_at "
            "OR NEW.source_sha256 IS NOT OLD.source_sha256 "
            "OR NEW.fx_cny_per_usd_scaled IS NOT OLD.fx_cny_per_usd_scaled "
            "OR NEW.fx_scale IS NOT OLD.fx_scale OR NEW.credit_pack_price_fen IS NOT OLD.credit_pack_price_fen "
            "OR NEW.credit_pack_credits IS NOT OLD.credit_pack_credits "
            "OR NEW.change_note IS NOT OLD.change_note OR NEW.created_by IS NOT OLD.created_by "
            "OR NEW.created_at IS NOT OLD.created_at "
            "BEGIN SELECT RAISE(ABORT, 'billing rate catalog snapshot is immutable'); END"
        )
        op.execute(
            "CREATE TRIGGER trg_billing_rate_catalog_no_delete BEFORE DELETE ON billing_rate_catalog "
            "BEGIN SELECT RAISE(ABORT, 'billing rate catalog rows are immutable'); END"
        )
        for table in ("billing_rate_catalog_item", "billing_rate_catalog_event"):
            op.execute(
                f"CREATE TRIGGER trg_{table}_no_update BEFORE UPDATE ON {table} "
                f"BEGIN SELECT RAISE(ABORT, '{table} rows are immutable'); END"
            )
            op.execute(
                f"CREATE TRIGGER trg_{table}_no_delete BEFORE DELETE ON {table} "
                f"BEGIN SELECT RAISE(ABORT, '{table} rows are immutable'); END"
            )
        return
    if bind.dialect.name == "mysql":
        op.execute(
            "CREATE TRIGGER trg_billing_rate_catalog_snapshot_immutable BEFORE UPDATE ON billing_rate_catalog "
            "FOR EACH ROW BEGIN IF NOT (OLD.version_key <=> NEW.version_key "
            "AND OLD.effective_at <=> NEW.effective_at AND OLD.source_url <=> NEW.source_url "
            "AND OLD.source_checked_at <=> NEW.source_checked_at AND OLD.source_sha256 <=> NEW.source_sha256 "
            "AND OLD.fx_source_url <=> NEW.fx_source_url "
            "AND OLD.fx_source_checked_at <=> NEW.fx_source_checked_at "
            "AND OLD.fx_cny_per_usd_scaled <=> NEW.fx_cny_per_usd_scaled AND OLD.fx_scale <=> NEW.fx_scale "
            "AND OLD.credit_pack_price_fen <=> NEW.credit_pack_price_fen "
            "AND OLD.credit_pack_credits <=> NEW.credit_pack_credits AND OLD.change_note <=> NEW.change_note "
            "AND OLD.created_by <=> NEW.created_by AND OLD.created_at <=> NEW.created_at) "
            "THEN SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'billing rate catalog snapshot is immutable'; "
            "END IF; END"
        )
        op.execute(
            "CREATE TRIGGER trg_billing_rate_catalog_no_delete BEFORE DELETE ON billing_rate_catalog "
            "FOR EACH ROW SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'billing rate catalog rows are immutable'"
        )
        for table in ("billing_rate_catalog_item", "billing_rate_catalog_event"):
            op.execute(
                f"CREATE TRIGGER trg_{table}_no_update BEFORE UPDATE ON {table} FOR EACH ROW "
                f"SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = '{table} rows are immutable'"
            )
            op.execute(
                f"CREATE TRIGGER trg_{table}_no_delete BEFORE DELETE ON {table} FOR EACH ROW "
                f"SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = '{table} rows are immutable'"
            )


def upgrade():
    op.create_table(
        "billing_rate_catalog",
        sa.Column("id", _id_type(), primary_key=True, autoincrement=True),
        sa.Column("version_key", sa.String(64), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="draft"),
        sa.Column("effective_at", sa.DateTime(), nullable=False),
        sa.Column("approved_effective_at", sa.DateTime(), nullable=True),
        sa.Column("source_url", sa.String(512), nullable=False),
        sa.Column("source_checked_at", sa.DateTime(), nullable=False),
        sa.Column("fx_source_url", sa.String(512), nullable=False),
        sa.Column("fx_source_checked_at", sa.DateTime(), nullable=False),
        sa.Column("source_sha256", sa.String(64), nullable=False),
        sa.Column("fx_cny_per_usd_scaled", sa.BigInteger(), nullable=False),
        sa.Column("fx_scale", sa.Integer(), nullable=False, server_default="10000"),
        sa.Column("credit_pack_price_fen", sa.BigInteger(), nullable=False),
        sa.Column("credit_pack_credits", sa.BigInteger(), nullable=False),
        sa.Column("change_note", sa.String(500), nullable=False),
        sa.Column("created_by", sa.BigInteger(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("approved_by", sa.BigInteger(), nullable=True),
        sa.Column("approved_at", sa.DateTime(), nullable=True),
        sa.Column("rejected_by", sa.BigInteger(), nullable=True),
        sa.Column("rejected_at", sa.DateTime(), nullable=True),
        sa.Column("canceled_by", sa.BigInteger(), nullable=True),
        sa.Column("canceled_at", sa.DateTime(), nullable=True),
        sa.Column("transition_note", sa.String(500), nullable=True),
        sa.UniqueConstraint("version_key", name="uq_billing_rate_catalog_version"),
        sa.UniqueConstraint("approved_effective_at", name="uq_billing_rate_catalog_approved_effective_at"),
        sa.CheckConstraint("status IN ('draft', 'approved', 'rejected', 'canceled')",
                           name="ck_billing_rate_catalog_status"),
        sa.CheckConstraint(
            "(status = 'approved' AND approved_effective_at = effective_at) OR "
            "(status <> 'approved' AND approved_effective_at IS NULL)",
            name="ck_billing_rate_catalog_approved_time_pair",
        ),
        sa.CheckConstraint("length(version_key) = 64", name="ck_billing_rate_catalog_version_length"),
        sa.CheckConstraint("length(source_sha256) = 64", name="ck_billing_rate_catalog_source_digest_length"),
        sa.CheckConstraint(
            "fx_cny_per_usd_scaled > 0 AND fx_scale = 10000 AND credit_pack_price_fen > 0 "
            "AND credit_pack_credits > 0",
            name="ck_billing_rate_catalog_financial_basis",
        ),
    )
    op.create_index("ix_billing_rate_catalog_status_effective", "billing_rate_catalog",
                    ["status", "effective_at"])
    op.create_table(
        "billing_rate_catalog_item",
        sa.Column("id", _id_type(), primary_key=True, autoincrement=True),
        sa.Column("catalog_id", _id_type(), nullable=False),
        sa.Column("model_name", sa.String(80), nullable=False),
        sa.Column("input_rate_numerator", sa.BigInteger(), nullable=False),
        sa.Column("input_rate_denominator", sa.BigInteger(), nullable=False),
        sa.Column("output_rate_numerator", sa.BigInteger(), nullable=False),
        sa.Column("output_rate_denominator", sa.BigInteger(), nullable=False),
        sa.Column("provider_input_usd_micros_per_million", sa.BigInteger(), nullable=False),
        sa.Column("provider_output_usd_micros_per_million", sa.BigInteger(), nullable=False),
        sa.ForeignKeyConstraint(["catalog_id"], ["billing_rate_catalog.id"],
                                name="fk_billing_rate_catalog_item_catalog", ondelete="RESTRICT"),
        sa.UniqueConstraint("catalog_id", "model_name", name="uq_billing_rate_catalog_item_model"),
        sa.CheckConstraint(
            "input_rate_numerator > 0 AND input_rate_denominator > 0 "
            "AND output_rate_numerator > 0 AND output_rate_denominator > 0 "
            "AND provider_input_usd_micros_per_million >= 0 "
            "AND provider_output_usd_micros_per_million >= 0",
            name="ck_billing_rate_catalog_item_rates_positive",
        ),
    )
    op.create_index("ix_billing_rate_catalog_item_catalog", "billing_rate_catalog_item",
                    ["catalog_id", "id"])
    op.create_table(
        "billing_rate_catalog_event",
        sa.Column("id", _id_type(), primary_key=True, autoincrement=True),
        sa.Column("catalog_id", _id_type(), nullable=False),
        sa.Column("actor_id", sa.BigInteger(), nullable=True),
        sa.Column("action", sa.String(16), nullable=False),
        sa.Column("note", sa.String(500), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["catalog_id"], ["billing_rate_catalog.id"],
                                name="fk_billing_rate_catalog_event_catalog", ondelete="RESTRICT"),
        sa.CheckConstraint("action IN ('created', 'approved', 'rejected', 'canceled')",
                           name="ck_billing_rate_catalog_event_action"),
    )
    op.create_index("ix_billing_rate_catalog_event_catalog", "billing_rate_catalog_event",
                    ["catalog_id", "id"])

    # Preserve the current runtime price version so existing reservation snapshots
    # continue to identify exactly the same customer debit rates.
    baseline_version = "9f87b58904f48a93c8ca9aed56436659bf18568064991be275969530e0b480cf"
    checked_at = datetime(2026, 11, 7, 5, 30)
    provider_source_url = "https://api-docs.deepseek.com/quick_start/pricing/"
    fx_source_url = "https://www.boc.cn/sourcedb/whpj/index.html"
    source_facts = [
        f"source_url={provider_source_url}",
        f"source_checked_at_utc={checked_at.isoformat(timespec='seconds')}",
        f"fx_source_url={fx_source_url}",
        f"fx_source_checked_at_utc={checked_at.isoformat(timespec='seconds')}",
        "fx_cny_per_usd_scaled=67168",
        "credit_pack_price_fen=400",
        "credit_pack_credits=1000",
    ]
    baseline_items = [
        {"model_name": model, "input_rate_numerator": 1, "input_rate_denominator": 1,
         "output_rate_numerator": 4, "output_rate_denominator": 1,
         "provider_input_usd_micros_per_million": 300000,
         "provider_output_usd_micros_per_million": 1200000}
        for model in ("deepseek-flash", "deepseek-v4-flash", "deepseek-v4-flash-vision-exp")
    ]
    source_facts.extend(
        ":".join(str(item[key]) for key in (
            "model_name", "input_rate_numerator", "input_rate_denominator",
            "output_rate_numerator", "output_rate_denominator",
            "provider_input_usd_micros_per_million", "provider_output_usd_micros_per_million",
        )) for item in baseline_items
    )
    source_digest = sha256("\n".join(source_facts).encode("utf-8")).hexdigest()
    catalog = sa.table(
        "billing_rate_catalog",
        sa.column("id", _id_type()), sa.column("version_key", sa.String(64)),
        sa.column("status", sa.String(16)), sa.column("effective_at", sa.DateTime()),
        sa.column("approved_effective_at", sa.DateTime()),
        sa.column("source_url", sa.String(512)), sa.column("source_checked_at", sa.DateTime()),
        sa.column("fx_source_url", sa.String(512)), sa.column("fx_source_checked_at", sa.DateTime()),
        sa.column("source_sha256", sa.String(64)), sa.column("fx_cny_per_usd_scaled", sa.BigInteger()),
        sa.column("fx_scale", sa.Integer()), sa.column("credit_pack_price_fen", sa.BigInteger()),
        sa.column("credit_pack_credits", sa.BigInteger()), sa.column("change_note", sa.String(500)),
        sa.column("created_by", sa.BigInteger()), sa.column("created_at", sa.DateTime()),
        sa.column("approved_by", sa.BigInteger()), sa.column("approved_at", sa.DateTime()),
    )
    bind = op.get_bind()
    bind.execute(insert(catalog).values(
        version_key=baseline_version, status="approved", effective_at=datetime(1970, 1, 1),
        approved_effective_at=datetime(1970, 1, 1),
        source_url=provider_source_url,
        source_checked_at=checked_at,
        fx_source_url=fx_source_url,
        fx_source_checked_at=checked_at, source_sha256=source_digest,
        fx_cny_per_usd_scaled=67168, fx_scale=10000, credit_pack_price_fen=400,
        credit_pack_credits=1000,
        change_note="从代码内已批准费率初始化；供应商价和汇率为 2026-11-07 官方页面核验快照。",
        created_by=None, created_at=datetime(2026, 11, 7, 5, 30),
        approved_by=None, approved_at=datetime(2026, 11, 7, 5, 30),
    ))
    catalog_id = bind.execute(select(catalog.c.id).where(catalog.c.version_key == baseline_version)).scalar_one()
    items = sa.table(
        "billing_rate_catalog_item",
        sa.column("catalog_id", _id_type()), sa.column("model_name", sa.String(80)),
        sa.column("input_rate_numerator", sa.BigInteger()), sa.column("input_rate_denominator", sa.BigInteger()),
        sa.column("output_rate_numerator", sa.BigInteger()), sa.column("output_rate_denominator", sa.BigInteger()),
        sa.column("provider_input_usd_micros_per_million", sa.BigInteger()),
        sa.column("provider_output_usd_micros_per_million", sa.BigInteger()),
    )
    bind.execute(insert(items), [dict(item, catalog_id=catalog_id) for item in baseline_items])
    event = sa.table(
        "billing_rate_catalog_event", sa.column("catalog_id", _id_type()),
        sa.column("actor_id", sa.BigInteger()), sa.column("action", sa.String(16)),
        sa.column("note", sa.String(500)), sa.column("created_at", sa.DateTime()),
    )
    bind.execute(insert(event).values(
        catalog_id=catalog_id, actor_id=None, action="created",
        note="迁移初始化当前代码内计费价格目录。", created_at=datetime(2026, 11, 7, 5, 30),
    ))
    bind.execute(insert(event).values(
        catalog_id=catalog_id, actor_id=None, action="approved",
        note="保留当前生产基线费率；管理员后续变更须经双人审批。", created_at=datetime(2026, 11, 7, 5, 30),
    ))
    _install_immutable_triggers()


def downgrade():
    raise RuntimeError("不得删除计费价格版本、供应商成本快照或审批审计记录")
