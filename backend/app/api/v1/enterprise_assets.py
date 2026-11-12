"""企业 Agent/Skill 私有草稿、版本绑定发布审批与只读目录 API。"""
import json
from typing import Any, Literal

from fastapi import APIRouter, Depends, Query
from pydantic import Field
from sqlalchemy.orm import Session

from app.api.v1.personal_agents import _run_factory
from app.core.database import get_db
from app.core.dependencies import get_current_user, require_super_admin
from app.core.exceptions import PaymentRequiredError, ServiceUnavailableError
from app.models.user import User
from app.schemas.common import Resp, StrictInputModel
from app.services import billing_ledger_service as ledger
from app.services import enterprise_asset_service as service
from app.services import personal_agent_execution_service as execution

router = APIRouter(prefix="/enterprises/{enterprise_id}/asset-drafts", tags=["企业资产草稿"])
publication_router = APIRouter(tags=["企业资产发布"])
platform_publication_router = APIRouter(tags=["平台全局资产发布审批"])


class CreateIn(StrictInputModel):
    kind: Literal["agent", "skill"]
    title: str = Field(min_length=1, max_length=120)
    definition: dict[str, Any]
    request_key: str = Field(min_length=1, max_length=80)


class ReviseIn(StrictInputModel):
    title: str = Field(min_length=1, max_length=120)
    definition: dict[str, Any]
    expected_version: int = Field(ge=1, strict=True)
    request_key: str = Field(min_length=1, max_length=80)


class PublicationRequestIn(StrictInputModel):
    scope: Literal["enterprise", "global"]
    expected_version: int = Field(ge=1, strict=True)
    request_key: str = Field(min_length=1, max_length=80)


class PublicationDecisionIn(StrictInputModel):
    decision: Literal["approve", "reject"]
    reason: str = Field(default="", max_length=500)


class PublicationRevokeIn(StrictInputModel):
    reason: str = Field(default="", max_length=500)


class GlobalAssetRunIn(StrictInputModel):
    request_key: str = Field(min_length=1, max_length=80)
    input_text: str = Field(min_length=1, max_length=12000)


def _draft(row, published_scopes=None):
    published_scopes = published_scopes or []
    return {"id": row.id, "enterprise_id": row.enterprise_id, "creator_id": row.creator_id,
            "kind": row.kind, "title": row.title, "definition": json.loads(row.definition_json),
            "current_version": row.current_version, "status": row.status,
            "create_time": row.create_time, "update_time": row.update_time,
            "published_scopes": published_scopes, "sharing_available": bool(published_scopes),
            "execution_available": False}


def _version(row):
    return {"id": row.id, "draft_id": row.draft_id, "version_number": row.version_number,
            "title": row.title, "definition": json.loads(row.definition_json), "create_time": row.create_time}


@router.get("", response_model=Resp[list[dict]])
def list_drafts(enterprise_id: int, offset: int = Query(0, ge=0), limit: int = Query(100, ge=1, le=100),
                db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    rows = service.list_drafts(db, enterprise_id, user.id, offset=offset, limit=limit)
    return Resp(data=[_draft(row, service.published_scopes_for_draft(
        db, enterprise_id, user.id, row.id,
    )) for row in rows])


@router.post("", response_model=Resp[dict])
def create_draft(enterprise_id: int, payload: CreateIn, db: Session = Depends(get_db),
                 user: User = Depends(get_current_user)):
    row = service.create_draft(db, enterprise_id, user.id, **payload.model_dump())
    return Resp(data=_draft(row, service.published_scopes_for_draft(db, enterprise_id, user.id, row.id)))


@router.get("/{draft_id}", response_model=Resp[dict])
def get_draft(enterprise_id: int, draft_id: int, db: Session = Depends(get_db),
              user: User = Depends(get_current_user)):
    row = service.get_draft(db, enterprise_id, user.id, draft_id)
    return Resp(data=_draft(row, service.published_scopes_for_draft(db, enterprise_id, user.id, row.id)))


@router.get("/{draft_id}/versions", response_model=Resp[list[dict]])
def list_versions(enterprise_id: int, draft_id: int, offset: int = Query(0, ge=0),
                  limit: int = Query(100, ge=1, le=100), db: Session = Depends(get_db),
                  user: User = Depends(get_current_user)):
    return Resp(data=[_version(row) for row in service.list_versions(
        db, enterprise_id, user.id, draft_id, offset=offset, limit=limit)])


@router.post("/{draft_id}/versions", response_model=Resp[dict])
def revise_draft(enterprise_id: int, draft_id: int, payload: ReviseIn, db: Session = Depends(get_db),
                 user: User = Depends(get_current_user)):
    return Resp(data=_version(service.revise_draft(db, enterprise_id, user.id, draft_id, **payload.model_dump())))


@router.delete("/{draft_id}", response_model=Resp[None])
def delete_draft(enterprise_id: int, draft_id: int, db: Session = Depends(get_db),
                 user: User = Depends(get_current_user)):
    service.delete_draft(db, enterprise_id, user.id, draft_id)
    return Resp(data=None)


def _publication(row, *, include_enterprise=True):
    return service._publication_payload(row, include_enterprise=include_enterprise)


@publication_router.post("/enterprises/{enterprise_id}/asset-drafts/{draft_id}/publication-requests",
                         response_model=Resp[dict])
def request_publication(enterprise_id: int, draft_id: int, payload: PublicationRequestIn,
                        db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    row = service.request_publication(db, enterprise_id, user.id, draft_id, **payload.model_dump())
    return Resp(data=_publication(row))


@publication_router.get("/enterprises/{enterprise_id}/asset-publications/mine",
                        response_model=Resp[list[dict]])
def list_my_publications(enterprise_id: int, offset: int = Query(0, ge=0),
                         limit: int = Query(100, ge=1, le=100), db: Session = Depends(get_db),
                         user: User = Depends(get_current_user)):
    rows = service.list_my_publications(db, enterprise_id, user.id, offset=offset, limit=limit)
    return Resp(data=[_publication(row) for row in rows])


@publication_router.post("/enterprises/{enterprise_id}/asset-publications/{publication_id}/withdraw",
                         response_model=Resp[dict])
def withdraw_publication(enterprise_id: int, publication_id: int, db: Session = Depends(get_db),
                         user: User = Depends(get_current_user)):
    row = service.withdraw_publication(db, enterprise_id, publication_id, user.id)
    return Resp(data=_publication(row))


@publication_router.get("/enterprises/{enterprise_id}/asset-publications/pending",
                        response_model=Resp[list[dict]])
def list_pending_enterprise_publications(enterprise_id: int, offset: int = Query(0, ge=0),
                                         limit: int = Query(100, ge=1, le=100), db: Session = Depends(get_db),
                                         user: User = Depends(get_current_user)):
    rows = service.list_pending_enterprise_publications(
        db, enterprise_id, user.id, offset=offset, limit=limit,
    )
    return Resp(data=[_publication(row) for row in rows])


@publication_router.post("/enterprises/{enterprise_id}/asset-publications/{publication_id}/decision",
                         response_model=Resp[dict])
def decide_enterprise_publication(enterprise_id: int, publication_id: int,
                                  payload: PublicationDecisionIn, db: Session = Depends(get_db),
                                  user: User = Depends(get_current_user)):
    row = service.decide_enterprise_publication(
        db, enterprise_id, publication_id, user.id,
        approve=payload.decision == "approve", reason=payload.reason,
    )
    return Resp(data=_publication(row))


@publication_router.post("/enterprises/{enterprise_id}/asset-publications/{publication_id}/revoke",
                         response_model=Resp[dict])
def revoke_enterprise_publication(enterprise_id: int, publication_id: int,
                                  payload: PublicationRevokeIn, db: Session = Depends(get_db),
                                  user: User = Depends(get_current_user)):
    row = service.revoke_enterprise_publication(
        db, enterprise_id, publication_id, user.id, reason=payload.reason,
    )
    return Resp(data=_publication(row))


@publication_router.get("/enterprises/{enterprise_id}/asset-catalog", response_model=Resp[list[dict]])
def list_enterprise_catalog(enterprise_id: int, offset: int = Query(0, ge=0),
                            limit: int = Query(100, ge=1, le=100), db: Session = Depends(get_db),
                            user: User = Depends(get_current_user)):
    member = service.require_enterprise_member(db, enterprise_id, user.id)
    rows = service.list_enterprise_catalog(db, enterprise_id, user.id, offset=offset, limit=limit)
    can_manage = member.role in {"owner", "admin"}
    return Resp(data=[service._catalog_payload(
        row, can_revoke=int(row.requested_by) == int(user.id) or can_manage,
    ) for row in rows])


@publication_router.get("/enterprise-asset-catalog", response_model=Resp[list[dict]])
def list_global_catalog(offset: int = Query(0, ge=0), limit: int = Query(100, ge=1, le=100),
                        before_id: int | None = Query(None, gt=0, le=2**63 - 1),
                        db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    rows = service.list_global_catalog(db, offset=offset, limit=limit, before_id=before_id)
    ready = execution.is_available(_run_factory(db)) if rows else False
    return Resp(data=[{**service._catalog_payload(row), "personal_execution_available": (
        ready and service.xiaoling_runtime_config_compatible(row)
    )} for row in rows])


@publication_router.post("/enterprise-asset-catalog/{publication_id}/runs", response_model=Resp[dict])
def run_global_asset(publication_id: int, payload: GlobalAssetRunIn, db: Session = Depends(get_db),
                     user: User = Depends(get_current_user)):
    try:
        return Resp(data=execution.execute_enterprise_publication(
            _run_factory(db), owner_id=int(user.id), publication_id=publication_id,
            token_version=int(user.token_version or 0), **payload.model_dump(),
        ))
    except ledger.InsufficientCredits as exc:
        raise PaymentRequiredError(str(exc)) from exc
    except ledger.BillingError as exc:
        raise ServiceUnavailableError(str(exc)) from exc


@publication_router.get("/enterprise-asset-catalog/{publication_id}/runs/by-request/{request_key}",
                        response_model=Resp[dict])
def global_asset_run_by_request(publication_id: int, request_key: str, db: Session = Depends(get_db),
                                user: User = Depends(get_current_user)):
    return Resp(data=execution.get_enterprise_publication_run(
        _run_factory(db), owner_id=int(user.id), publication_id=publication_id, request_key=request_key,
        token_version=int(user.token_version or 0),
    ))


@publication_router.get("/enterprise-asset-catalog/{publication_id}/runs/{run_id}", response_model=Resp[dict])
def get_global_asset_run(publication_id: int, run_id: int, db: Session = Depends(get_db),
                         user: User = Depends(get_current_user)):
    return Resp(data=execution.get_enterprise_publication_run(
        _run_factory(db), owner_id=int(user.id), publication_id=publication_id, run_id=run_id,
        token_version=int(user.token_version or 0),
    ))


@platform_publication_router.get("/admin/enterprise-asset-publications", response_model=Resp[list[dict]])
def list_global_publications(status: Literal["pending", "approved", "rejected", "revoked"] = "pending",
                             offset: int = Query(0, ge=0), limit: int = Query(100, ge=1, le=100),
                             db: Session = Depends(get_db), user: User = Depends(require_super_admin)):
    rows = service.list_global_publications(db, user.id, status=status, offset=offset, limit=limit)
    return Resp(data=[_publication(row) for row in rows])


@platform_publication_router.post("/admin/enterprise-asset-publications/{publication_id}/decision",
                                  response_model=Resp[dict])
def decide_global_publication(publication_id: int, payload: PublicationDecisionIn,
                              db: Session = Depends(get_db), user: User = Depends(require_super_admin)):
    row = service.decide_global_publication(
        db, publication_id, user.id, approve=payload.decision == "approve", reason=payload.reason,
    )
    return Resp(data=_publication(row))


@platform_publication_router.post("/admin/enterprise-asset-publications/{publication_id}/revoke",
                                  response_model=Resp[dict])
def revoke_global_publication(publication_id: int, payload: PublicationRevokeIn,
                              db: Session = Depends(get_db), user: User = Depends(require_super_admin)):
    row = service.revoke_global_publication(db, publication_id, user.id, reason=payload.reason)
    return Resp(data=_publication(row))
