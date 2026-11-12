"""企业远测审批 API；只有审批通过的固定范围可进入现有只读执行器。"""
from datetime import datetime, timedelta, timezone
from typing import Literal

from fastapi import APIRouter, Depends, Query
from pydantic import Field, field_validator
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.database import get_db
from app.core.dependencies import get_current_user
from app.models.user import User
from app.schemas.common import Resp, StrictInputModel
from app.services import audit_service
from app.services import enterprise_remote_approval_service as service
from app.utils.input_validation import normalize_plain_text

router = APIRouter(prefix="/enterprises/{enterprise_id}/remote-test-requests", tags=["enterprise-remote-approval"])


class RequestIn(StrictInputModel):
    group_id: int = Field(gt=0, strict=True)
    project_id: int = Field(gt=0, strict=True)
    target_url: str = Field(min_length=8, max_length=512)
    authorization_basis: str = Field(min_length=12, max_length=1000)
    scope_notes: str = Field(default="", max_length=2000)
    requested_window_hours: int = Field(default=4, ge=1, le=24, strict=True)
    ack_rules: bool = Field(strict=True)

    @field_validator("authorization_basis", "scope_notes")
    @classmethod
    def normalize_text(cls, value: str) -> str:
        return normalize_plain_text(value, field_name="远测申请字段", allow_empty=True, allow_newlines=True)


class DecisionIn(StrictInputModel):
    decision: Literal["approved", "rejected"]
    reason: str = Field(min_length=1, max_length=500)

    @field_validator("reason")
    @classmethod
    def normalize_reason(cls, value: str) -> str:
        return normalize_plain_text(value, field_name="审批原因", allow_empty=False, allow_newlines=True)


def _serialize(row):
    execution_available = bool(
        row.project_id and row.status == "approved" and row.execution_task_public_id and row.decided_at
        and settings.sandbox_remote_targets_enabled
        and datetime.now(timezone.utc).replace(tzinfo=None)
        < row.decided_at + timedelta(hours=row.requested_window_hours)
    )
    return {"id": row.id, "enterprise_id": row.enterprise_id, "group_id": row.group_id,
            "project_id": row.project_id, "owner_id": row.owner_id,
            "target_url": row.target_url, "target_host": row.target_host,
            "authorization_basis": row.authorization_basis, "scope_notes": row.scope_notes,
            "requested_window_hours": row.requested_window_hours, "scope_version": row.scope_version,
            "scope_digest": row.scope_digest, "status": row.status, "decided_by": row.decided_by,
            "decision_reason": row.decision_reason, "decided_at": row.decided_at,
            "create_time": row.create_time, "execution_task_public_id": row.execution_task_public_id,
            "execution_available": execution_available}


@router.post("", response_model=Resp[dict])
def submit(enterprise_id: int, payload: RequestIn, db: Session = Depends(get_db),
           user: User = Depends(get_current_user)):
    return Resp(data=_serialize(service.submit_request(db, enterprise_id, payload.group_id, user.id,
                                                       project_id=payload.project_id,
                                                       target_url=payload.target_url,
                                                       authorization_basis=payload.authorization_basis,
                                                       scope_notes=payload.scope_notes,
                                                       requested_window_hours=payload.requested_window_hours,
                                                       ack_rules=payload.ack_rules)))


@router.get("", response_model=Resp[dict])
def listing(enterprise_id: int, limit: int = Query(50, ge=1, le=100), offset: int = Query(0, ge=0),
            db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    rows, total = service.list_requests(db, enterprise_id, user.id, limit=limit, offset=offset)
    return Resp(data={"items": [_serialize(row) for row in rows], "total": total, "execution_available": False})


@router.get("/{request_id}", response_model=Resp[dict])
def detail(enterprise_id: int, request_id: int, db: Session = Depends(get_db),
           user: User = Depends(get_current_user)):
    return Resp(data=_serialize(service.get_request(db, enterprise_id, request_id, user.id)))


@router.post("/{request_id}/decision", response_model=Resp[dict])
def decide(enterprise_id: int, request_id: int, payload: DecisionIn, db: Session = Depends(get_db),
           user: User = Depends(get_current_user)):
    row = service.decide_request(db, enterprise_id, request_id, user.id,
                                 decision=payload.decision, reason=payload.reason)
    audit_service.log(db, user, "enterprise_remote_approval_decision", target_type="enterprise_remote_approval",
                      target_id=str(row.id), detail=f"{row.status}: {row.target_host}; fixed scope digest recorded")
    return Resp(data=_serialize(row))


@router.post("/{request_id}/cancel", response_model=Resp[dict])
def cancel(enterprise_id: int, request_id: int, db: Session = Depends(get_db),
           user: User = Depends(get_current_user)):
    return Resp(data=_serialize(service.cancel_request(db, enterprise_id, request_id, user.id)))


@router.post("/{request_id}/start", response_model=Resp[dict])
def start(enterprise_id: int, request_id: int, db: Session = Depends(get_db),
          user: User = Depends(get_current_user)):
    request, task = service.start_request(db, enterprise_id, request_id, user.id)
    audit_service.log(db, user, "remote_pentest_start", target_type="remote_pentest",
                      target_id=task["public_id"],
                      detail=f"企业申请 {request.id} 的固定 HTTPS 根路径只读评估已启动")
    return Resp(data={"request": _serialize(request), "execution": task})
