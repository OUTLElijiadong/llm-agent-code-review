"""Responses 主控与后台恢复共用的个人/平台计费作用域。"""

from __future__ import annotations

from contextlib import contextmanager
from hashlib import sha256
from uuid import uuid4

from app.core.config import settings
from app.models.billing_ledger import BillingWallet
from app.models.user import User
from app.services import billing_ledger_service as ledger
from app.services import rbac_service
from app.services.billing_runtime_gate import billing_scope, platform_cost_scope


def authorize_platform_admin(db, user_id: int) -> None:
    actor = (db.query(User).filter(User.id == user_id).populate_existing().with_for_update().one_or_none())
    if actor is None or int(actor.status or 0) != 1 or not rbac_service.is_admin_user(
        db, user_id, lock=True,
    ):
        raise ledger.BillingError("平台管理员权限已失效，模型请求已阻断")


@contextmanager
def trusted_response_billing_scope(session_factory, user: User, surface: str, *, run_id: str | None = None):
    """Bind admin calls to platform cost; keep user calls on the existing wallet path."""
    user_id = getattr(user, "id", None)
    if surface == "admin":
        if (not callable(session_factory) or type(user_id) is not int or user_id <= 0
                or (run_id is not None and (not isinstance(run_id, str) or len(run_id) > 128))):
            raise ledger.BillingError("管理员小菱缺少有效账号或运行上下文")

        def guard(db, _request_key):
            authorize_platform_admin(db, int(user_id))

        digest = sha256((run_id or "admin-session").encode("utf-8")).hexdigest()[:20]
        prefix = f"xiaoling-admin:{digest}:{uuid4().hex}"
        with platform_cost_scope(
            session_factory=session_factory,
            actor_user_id=int(user_id),
            run_id=run_id,
            max_input_tokens=int(settings.deepseek_context_window_tokens),
            max_output_tokens=int(settings.deepseek_max_output_tokens),
            authorization_guard=guard,
            request_key_prefix=prefix,
        ):
            yield
        return

    if surface != "user":
        raise ledger.BillingError("当前 Agent 入口没有已配置的可信付款归属")
    if not settings.billing_commercial_metering_ready:
        yield
        return
    if (not callable(session_factory) or type(user_id) is not int or user_id <= 0):
        raise ledger.BillingError("当前 Agent 请求缺少可验证的账号或数据库会话")

    with session_factory() as billing_db:
        wallet = billing_db.query(BillingWallet).filter_by(owner_type="user", owner_id=user_id).first()
        wallet_id = int(wallet.id) if wallet is not None else None
    if wallet_id is None:
        raise ledger.BillingError("个人积分账户尚未开通，请先购买积分或会员")

    with billing_scope(
        session_factory=session_factory,
        user_id=int(user_id),
        personal_wallet_id=wallet_id,
        max_input_tokens=int(settings.deepseek_context_window_tokens),
        max_output_tokens=int(settings.deepseek_max_output_tokens),
        allow_personal=False,
        usage_kind="member",
    ):
        yield
