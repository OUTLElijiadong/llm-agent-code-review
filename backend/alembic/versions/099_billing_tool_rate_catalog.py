"""Add immutable per-success Search/Fetch rates to reviewed billing catalogs.

Revision ID: 099_billing_tool_rate_catalog
Revises: 098_billing_rate_catalog
"""

import sqlalchemy as sa

from alembic import op

revision = "099_billing_tool_rate_catalog"
down_revision = "098_billing_rate_catalog"
branch_labels = None
depends_on = None


def _install_catalog_triggers() -> None:
    bind = op.get_bind()
    op.execute("DROP TRIGGER IF EXISTS trg_billing_rate_catalog_snapshot_immutable")
    op.execute("DROP TRIGGER IF EXISTS trg_billing_rate_catalog_no_delete")
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
            "OR NEW.tool_cny_source_url IS NOT OLD.tool_cny_source_url "
            "OR NEW.tool_cny_source_checked_at IS NOT OLD.tool_cny_source_checked_at "
            "OR NEW.tool_usd_source_url IS NOT OLD.tool_usd_source_url "
            "OR NEW.tool_usd_source_checked_at IS NOT OLD.tool_usd_source_checked_at "
            "OR NEW.tool_search_cost_cny_micros_per_success IS NOT OLD.tool_search_cost_cny_micros_per_success "
            "OR NEW.tool_search_price_milli_per_success IS NOT OLD.tool_search_price_milli_per_success "
            "OR NEW.tool_fetch_cost_cny_micros_per_success IS NOT OLD.tool_fetch_cost_cny_micros_per_success "
            "OR NEW.tool_fetch_price_milli_per_success IS NOT OLD.tool_fetch_price_milli_per_success "
            "OR NEW.change_note IS NOT OLD.change_note OR NEW.created_by IS NOT OLD.created_by "
            "OR NEW.created_at IS NOT OLD.created_at "
            "BEGIN SELECT RAISE(ABORT, 'billing rate catalog snapshot is immutable'); END"
        )
        op.execute(
            "CREATE TRIGGER trg_billing_rate_catalog_no_delete BEFORE DELETE ON billing_rate_catalog "
            "BEGIN SELECT RAISE(ABORT, 'billing rate catalog rows are immutable'); END"
        )
        rate_checks = (
            "((NEW.tool_search_cost_cny_micros_per_success = 0 AND NEW.tool_search_price_milli_per_success = 0) OR "
            "(NEW.tool_search_cost_cny_micros_per_success > 0 AND NEW.tool_search_price_milli_per_success > 0)) "
            "AND ((NEW.tool_fetch_cost_cny_micros_per_success = 0 AND NEW.tool_fetch_price_milli_per_success = 0) OR "
            "(NEW.tool_fetch_cost_cny_micros_per_success > 0 AND NEW.tool_fetch_price_milli_per_success > 0)) "
            "AND ((NEW.tool_cny_source_url IS NULL AND NEW.tool_cny_source_checked_at IS NULL "
            "AND NEW.tool_usd_source_url IS NULL AND NEW.tool_usd_source_checked_at IS NULL "
            "AND NEW.tool_search_cost_cny_micros_per_success = 0 "
            "AND NEW.tool_search_price_milli_per_success = 0 "
            "AND NEW.tool_fetch_cost_cny_micros_per_success = 0 "
            "AND NEW.tool_fetch_price_milli_per_success = 0) OR "
            "(NEW.tool_cny_source_url IS NOT NULL AND NEW.tool_cny_source_checked_at IS NOT NULL "
            "AND NEW.tool_usd_source_url IS NOT NULL AND NEW.tool_usd_source_checked_at IS NOT NULL))"
        )
        for action in ("INSERT", "UPDATE"):
            suffix = "insert" if action == "INSERT" else "update"
            op.execute(
                f"CREATE TRIGGER trg_billing_rate_catalog_tool_rates_check_{suffix} "
                f"BEFORE {action} ON billing_rate_catalog WHEN NOT ({rate_checks}) "
                "BEGIN SELECT RAISE(ABORT, 'billing rate catalog tool pricing check failed'); END"
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
            "AND OLD.credit_pack_credits <=> NEW.credit_pack_credits "
            "AND OLD.tool_cny_source_url <=> NEW.tool_cny_source_url "
            "AND OLD.tool_cny_source_checked_at <=> NEW.tool_cny_source_checked_at "
            "AND OLD.tool_usd_source_url <=> NEW.tool_usd_source_url "
            "AND OLD.tool_usd_source_checked_at <=> NEW.tool_usd_source_checked_at "
            "AND OLD.tool_search_cost_cny_micros_per_success <=> NEW.tool_search_cost_cny_micros_per_success "
            "AND OLD.tool_search_price_milli_per_success <=> NEW.tool_search_price_milli_per_success "
            "AND OLD.tool_fetch_cost_cny_micros_per_success <=> NEW.tool_fetch_cost_cny_micros_per_success "
            "AND OLD.tool_fetch_price_milli_per_success <=> NEW.tool_fetch_price_milli_per_success "
            "AND OLD.change_note <=> NEW.change_note AND OLD.created_by <=> NEW.created_by "
            "AND OLD.created_at <=> NEW.created_at) "
            "THEN SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'billing rate catalog snapshot is immutable'; "
            "END IF; END"
        )
        op.execute(
            "CREATE TRIGGER trg_billing_rate_catalog_no_delete BEFORE DELETE ON billing_rate_catalog "
            "FOR EACH ROW SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'billing rate catalog rows are immutable'"
        )


def upgrade() -> None:
    bind = op.get_bind()
    columns = (
        sa.Column("tool_cny_source_url", sa.String(512), nullable=True),
        sa.Column("tool_cny_source_checked_at", sa.DateTime(), nullable=True),
        sa.Column("tool_usd_source_url", sa.String(512), nullable=True),
        sa.Column("tool_usd_source_checked_at", sa.DateTime(), nullable=True),
        sa.Column("tool_search_cost_cny_micros_per_success", sa.BigInteger(), nullable=False,
                  server_default="0"),
        sa.Column("tool_search_price_milli_per_success", sa.BigInteger(), nullable=False,
                  server_default="0"),
        sa.Column("tool_fetch_cost_cny_micros_per_success", sa.BigInteger(), nullable=False,
                  server_default="0"),
        sa.Column("tool_fetch_price_milli_per_success", sa.BigInteger(), nullable=False,
                  server_default="0"),
    )
    for column in columns:
        op.add_column("billing_rate_catalog", column)
    if bind.dialect.name == "mysql":
        op.create_check_constraint(
            "ck_billing_catalog_search_cost_price_pair", "billing_rate_catalog",
            "(tool_search_cost_cny_micros_per_success = 0 AND tool_search_price_milli_per_success = 0) OR "
            "(tool_search_cost_cny_micros_per_success > 0 AND tool_search_price_milli_per_success > 0)",
        )
        op.create_check_constraint(
            "ck_billing_catalog_fetch_cost_price_pair", "billing_rate_catalog",
            "(tool_fetch_cost_cny_micros_per_success = 0 AND tool_fetch_price_milli_per_success = 0) OR "
            "(tool_fetch_cost_cny_micros_per_success > 0 AND tool_fetch_price_milli_per_success > 0)",
        )
        op.create_check_constraint(
            "ck_billing_catalog_tool_source_pair", "billing_rate_catalog",
            "(tool_cny_source_url IS NULL AND tool_cny_source_checked_at IS NULL "
            "AND tool_usd_source_url IS NULL AND tool_usd_source_checked_at IS NULL "
            "AND tool_search_cost_cny_micros_per_success = 0 "
            "AND tool_search_price_milli_per_success = 0 "
            "AND tool_fetch_cost_cny_micros_per_success = 0 "
            "AND tool_fetch_price_milli_per_success = 0) OR "
            "(tool_cny_source_url IS NOT NULL AND tool_cny_source_checked_at IS NOT NULL "
            "AND tool_usd_source_url IS NOT NULL AND tool_usd_source_checked_at IS NOT NULL)",
        )
    _install_catalog_triggers()


def downgrade() -> None:
    raise RuntimeError("不得删除计费费率目录工具历史、成本与双人审批快照")
