"""管理员内测邀请码 API。"""

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.rbac_dependency import require_admin
from app.models.beta_invite_code import BetaInviteCode
from app.models.user import User
from app.schemas.beta_invite import BetaInviteGenerateIn, BetaInviteGenerateOut, BetaInviteOut
from app.schemas.common import PageOut, Resp
from app.services import audit_service, beta_invite_service

router = APIRouter()


def _client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else ""


def _to_out(row: BetaInviteCode) -> BetaInviteOut:
    data = BetaInviteOut.model_validate(row)
    return data.model_copy(update={"status": beta_invite_service.effective_status(row)})


@router.post("", response_model=Resp[BetaInviteGenerateOut])
def generate_beta_codes(
    payload: BetaInviteGenerateIn,
    request: Request,
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
):
    """生成 1 到 100 个一次性内测码；明文仅本次响应可见。"""

    codes, rows = beta_invite_service.generate_codes(
        db,
        creator_id=admin.id,
        count=payload.count,
        expiry_days=payload.expiry_days,
        label=payload.label,
    )
    audit_service.log(
        db,
        admin,
        "beta_invite",
        target_type="beta_invite_code",
        detail=f"生成 {len(rows)} 个内测码，有效期 {payload.expiry_days} 天",
        ip=_client_ip(request),
    )
    return Resp(data=BetaInviteGenerateOut(codes=codes, items=[_to_out(row) for row in rows]))


@router.get("", response_model=Resp[PageOut[BetaInviteOut]])
def list_beta_codes(
    status: str = Query("", pattern="^(|active|used|revoked|expired)$"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
):
    """分页查看内测码状态，永不返回明文或摘要。"""

    result = beta_invite_service.list_codes(db, status=status, page=page, page_size=page_size)
    return Resp(
        data=PageOut(
            items=[_to_out(row) for row in result["items"]],
            total=result["total"],
            page=result["page"],
            page_size=result["page_size"],
            pages=result["pages"],
        )
    )


@router.post("/{invite_id}/revoke", response_model=Resp[BetaInviteOut])
def revoke_beta_code(
    invite_id: int,
    request: Request,
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
):
    """撤销一个尚未消费的邀请码。"""

    row = beta_invite_service.revoke_code(db, invite_id)
    audit_service.log(
        db,
        admin,
        "beta_invite",
        target_type="beta_invite_code",
        target_id=row.id,
        detail=f"撤销内测码 {row.display_prefix}",
        ip=_client_ip(request),
    )
    return Resp(data=_to_out(row))
