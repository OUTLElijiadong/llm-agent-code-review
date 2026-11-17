"""小菱安全事件导出与趋势接口。"""

from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.dependencies import require_admin
from app.core.permission_codes import PermissionCode
from app.core.rbac_dependency import require_permission
from app.schemas.common import Resp
from app.services import copilot_security_export_service as export_service

router = APIRouter()
_READ_GUARDS = [
    Depends(require_admin),
    Depends(require_permission(PermissionCode.SECURITY_VIEW)),
]


@router.get(
    "/events/export",
    dependencies=_READ_GUARDS,
    response_class=StreamingResponse,
)
def export_security_events(
    format: str = Query(default="csv", pattern="^(csv|json)$"),
    days: int = Query(default=7, ge=1, le=3650),
    kind: str = Query(default="", max_length=80),
    status: str = Query(default="", max_length=20),
    db: Session = Depends(get_db),
):
    content, media_type, filename = export_service.export_events(
        db, kind=kind, status=status, days=days, fmt=format,
    )
    return StreamingResponse(
        iter([content]),
        media_type=media_type,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get(
    "/events/trend",
    response_model=Resp[dict],
    dependencies=_READ_GUARDS,
)
def get_security_event_trend(
    days: int = Query(default=7, ge=1, le=3650),
    db: Session = Depends(get_db),
):
    return Resp(data={
        "trend": export_service.build_trend(db, days=days),
        "by_kind": export_service.summarize_by_kind(db, days=days),
    })
