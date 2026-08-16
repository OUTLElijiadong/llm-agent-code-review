"""一次性内测邀请码生成、校验、消费与撤销。"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.exceptions import ConflictError, NotFoundError, ValidationError
from app.models.beta_invite_code import BetaInviteCode

_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
_CODE_PATTERN = re.compile(r"^PRISM(?:-[A-Z2-9]{5}){4}$")
_INVALID_MESSAGE = "内测码无效、已使用或已过期"


def _utcnow_naive() -> datetime:
    """数据库统一使用无时区 UTC，避免 MySQL DateTime 比较歧义。"""

    return datetime.now(timezone.utc).replace(tzinfo=None)


def _pepper() -> bytes:
    value = settings.beta_code_pepper.strip()
    if not value:
        raise RuntimeError("BETA_CODE_PEPPER 未配置")
    return value.encode("utf-8")


def normalize_code(code: str) -> str:
    """清理用户输入并校验固定格式。"""

    normalized = (code or "").strip().upper()
    if not _CODE_PATTERN.fullmatch(normalized):
        raise ValidationError(_INVALID_MESSAGE, code=42220)
    return normalized


def digest_code(code: str) -> str:
    """使用独立 Pepper 计算不可逆 HMAC-SHA256 摘要。"""

    normalized = normalize_code(code)
    return hmac.new(_pepper(), normalized.encode("ascii"), hashlib.sha256).hexdigest()


def _new_plain_code() -> str:
    groups = ["".join(secrets.choice(_ALPHABET) for _ in range(5)) for _ in range(4)]
    return "PRISM-" + "-".join(groups)


def effective_status(invite: BetaInviteCode, *, now: datetime | None = None) -> str:
    """将过期的 active 状态在输出层映射为 expired。"""

    current = now or _utcnow_naive()
    if invite.status == "active" and invite.expires_at <= current:
        return "expired"
    return invite.status


def generate_codes(
    db: Session,
    *,
    creator_id: int,
    count: int,
    expiry_days: int,
    label: str | None,
) -> tuple[list[str], list[BetaInviteCode]]:
    """批量生成邀请码，明文仅由本次调用返回。"""

    expires_at = _utcnow_naive() + timedelta(days=expiry_days)
    plain_codes: list[str] = []
    rows: list[BetaInviteCode] = []
    pending_hashes: set[str] = set()
    for _ in range(count):
        for _attempt in range(10):
            plain = _new_plain_code()
            code_hash = digest_code(plain)
            exists = db.query(BetaInviteCode.id).filter(BetaInviteCode.code_hash == code_hash).first()
            if code_hash not in pending_hashes and not exists:
                break
        else:
            raise ConflictError("内测码生成冲突，请重试", code=40920)
        pending_hashes.add(code_hash)
        row = BetaInviteCode(
            code_hash=code_hash,
            display_prefix=f"PRISM-{plain.split('-')[1]}-*****-*****-*****",
            label=(label or "").strip() or None,
            status="active",
            expires_at=expires_at,
            created_by=creator_id,
        )
        db.add(row)
        plain_codes.append(plain)
        rows.append(row)
    db.commit()
    for row in rows:
        db.refresh(row)
    return plain_codes, rows


def list_codes(
    db: Session,
    *,
    status: str = "",
    page: int = 1,
    page_size: int = 20,
) -> dict:
    """分页列出脱敏邀请码，不返回摘要或明文。"""

    query = db.query(BetaInviteCode)
    now = _utcnow_naive()
    if status == "expired":
        query = query.filter(BetaInviteCode.status == "active", BetaInviteCode.expires_at <= now)
    elif status == "active":
        query = query.filter(BetaInviteCode.status == "active", BetaInviteCode.expires_at > now)
    elif status:
        query = query.filter(BetaInviteCode.status == status)
    total = query.count()
    rows = (
        query.order_by(BetaInviteCode.create_time.desc(), BetaInviteCode.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
        .all()
    )
    return {
        "items": rows,
        "total": total,
        "page": page,
        "page_size": page_size,
        "pages": (total + page_size - 1) // page_size,
    }


def lock_valid_code(db: Session, plain_code: str) -> BetaInviteCode:
    """在注册事务中锁定一个仍可使用的邀请码。"""

    code_hash = digest_code(plain_code)
    invite = db.query(BetaInviteCode).filter(BetaInviteCode.code_hash == code_hash).with_for_update().first()
    now = _utcnow_naive()
    if not invite or invite.status != "active" or invite.used_by is not None or invite.expires_at <= now:
        raise ValidationError(_INVALID_MESSAGE, code=42220)
    return invite


def consume_locked_code(db: Session, invite: BetaInviteCode, *, user_id: int) -> None:
    """条件更新邀请码，确保并发竞争时最多一个事务成功。"""

    now = _utcnow_naive()
    updated = (
        db.query(BetaInviteCode)
        .filter(
            BetaInviteCode.id == invite.id,
            BetaInviteCode.status == "active",
            BetaInviteCode.used_by.is_(None),
            BetaInviteCode.expires_at > now,
        )
        .update(
            {
                BetaInviteCode.status: "used",
                BetaInviteCode.used_by: user_id,
                BetaInviteCode.used_at: now,
                BetaInviteCode.update_time: now,
            },
            synchronize_session=False,
        )
    )
    if updated != 1:
        raise ValidationError(_INVALID_MESSAGE, code=42220)
    # bulk update 绕过 identity map；显式失效，避免同 Session 后续读取旧状态。
    db.expire(invite)


def revoke_code(db: Session, invite_id: int) -> BetaInviteCode:
    """撤销一个尚未使用且未过期的邀请码。"""

    invite = db.query(BetaInviteCode).filter(BetaInviteCode.id == invite_id).with_for_update().first()
    if not invite:
        raise NotFoundError("内测码不存在", code=40420)
    if effective_status(invite) != "active":
        raise ConflictError("仅可撤销未使用且未过期的内测码", code=40921)
    now = _utcnow_naive()
    updated = (
        db.query(BetaInviteCode)
        .filter(
            BetaInviteCode.id == invite.id,
            BetaInviteCode.status == "active",
            BetaInviteCode.used_by.is_(None),
            BetaInviteCode.expires_at > now,
        )
        .update(
            {BetaInviteCode.status: "revoked", BetaInviteCode.update_time: now},
            synchronize_session=False,
        )
    )
    if updated != 1:
        db.rollback()
        raise ConflictError("仅可撤销未使用且未过期的内测码", code=40921)
    db.expire(invite)
    db.commit()
    db.refresh(invite)
    return invite


def delete_code(db: Session, invite_id: int) -> BetaInviteCode:
    """物理删除一个内测码记录(超级管理员专用)。

    与 revoke 不同:任意状态(active/used/revoked/expired)都可删除,
    用于清理无用记录。删除前返回行快照供审计留痕;码本身已不可用
    的安全语义由调用方审计保证。
    """

    invite = db.query(BetaInviteCode).filter(BetaInviteCode.id == invite_id).with_for_update().first()
    if not invite:
        raise NotFoundError("内测码不存在", code=40420)
    snapshot = BetaInviteCode(
        id=invite.id,
        code_hash=invite.code_hash,
        label=invite.label,
        status=invite.status,
    )
    snapshot.display_prefix = invite.display_prefix
    db.delete(invite)
    db.commit()
    return snapshot
