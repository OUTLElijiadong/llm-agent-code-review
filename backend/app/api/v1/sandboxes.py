"""用户沙箱测试、持续部署和能力搜索 API。"""

from __future__ import annotations

import urllib.parse

from fastapi import APIRouter, Cookie, Depends, Query, Request, Response
from fastapi.responses import Response as RawResponse
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.database import get_db
from app.core.dependencies import get_current_user, require_super_admin
from app.models.user import User
from app.schemas.agent_capability import (
    CapabilitySearchOut,
    SandboxCreateIn,
    SandboxEnvironmentOut,
    SandboxExtendIn,
    SandboxWorkerUpsertIn,
)
from app.schemas.common import Resp
from app.services import capability_catalog_service, sandbox_service

router = APIRouter()


@router.get("/workers", response_model=Resp[list[dict]])
def list_workers(
    db: Session = Depends(get_db),
    _: User = Depends(require_super_admin),
):
    return Resp(data=sandbox_service.list_workers(db))


@router.post("/workers", response_model=Resp[dict])
def create_worker(
    payload: SandboxWorkerUpsertIn,
    db: Session = Depends(get_db),
    admin: User = Depends(require_super_admin),
):
    row = sandbox_service.upsert_worker(db, admin, payload.model_dump())
    return Resp(data=sandbox_service.worker_to_dict(row))


@router.put("/workers/{worker_id}", response_model=Resp[dict])
def update_worker(
    worker_id: int,
    payload: SandboxWorkerUpsertIn,
    db: Session = Depends(get_db),
    admin: User = Depends(require_super_admin),
):
    row = sandbox_service.upsert_worker(db, admin, payload.model_dump(), worker_id=worker_id)
    return Resp(data=sandbox_service.worker_to_dict(row))


@router.post("/workers/{worker_id}/health", response_model=Resp[dict])
def check_worker(
    worker_id: int,
    db: Session = Depends(get_db),
    _: User = Depends(require_super_admin),
):
    return Resp(data=sandbox_service.check_worker(db, worker_id))


@router.post("/workers/seed-production", response_model=Resp[dict])
def seed_production_worker(
    db: Session = Depends(get_db),
    admin: User = Depends(require_super_admin),
):
    row = sandbox_service.seed_production_worker(db, admin)
    return Resp(data=sandbox_service.worker_to_dict(row))


@router.get("/capabilities/search", response_model=Resp[list[CapabilitySearchOut]])
def search_capabilities(
    q: str = Query("", max_length=200),
    limit: int = Query(5, ge=1, le=10),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    matches = capability_catalog_service.search_capabilities(db, user, q, limit)
    return Resp(data=[CapabilitySearchOut(**item) for item in matches])


@router.get("", response_model=Resp[list[SandboxEnvironmentOut]])
def list_sandboxes(
    limit: int = Query(50, ge=1, le=100),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    return Resp(data=[SandboxEnvironmentOut(**item) for item in sandbox_service.list_environments(db, user, limit)])


@router.post("", response_model=Resp[SandboxEnvironmentOut])
def create_sandbox(
    payload: SandboxCreateIn,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    data = payload.model_dump(mode="json")
    row = sandbox_service.create_environment(db, user, data)
    return Resp(data=SandboxEnvironmentOut(**sandbox_service.environment_to_dict(db, row)))


@router.get("/{public_id}", response_model=Resp[SandboxEnvironmentOut])
def get_sandbox(
    public_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    return Resp(data=SandboxEnvironmentOut(**sandbox_service.get_environment(db, user, public_id)))


@router.post("/{public_id}/stop", response_model=Resp[SandboxEnvironmentOut])
def stop_sandbox(
    public_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    return Resp(data=SandboxEnvironmentOut(**sandbox_service.stop_environment(db, user, public_id)))


@router.post("/{public_id}/extend", response_model=Resp[SandboxEnvironmentOut])
def extend_sandbox(
    public_id: str,
    payload: SandboxExtendIn,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    return Resp(data=SandboxEnvironmentOut(**sandbox_service.extend_environment(db, user, public_id, payload.hours)))


@router.get("/{public_id}/artifacts/{artifact_id}")
def download_sandbox_artifact(
    public_id: str,
    artifact_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    content, file_name, mime_type = sandbox_service.get_artifact_download(
        db,
        user,
        public_id,
        artifact_id,
    )
    encoded_name = urllib.parse.quote(file_name, safe="")
    return RawResponse(
        content=content,
        media_type=mime_type,
        headers={
            "content-disposition": f"attachment; filename*=UTF-8''{encoded_name}",
            "cache-control": "private, no-store",
            "x-content-type-options": "nosniff",
        },
    )


@router.post("/{public_id}/preview-session", response_model=Resp[dict])
def create_preview_session(
    public_id: str,
    response: Response,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    session = sandbox_service.create_preview_session(db, user, public_id)
    response.set_cookie(
        key=sandbox_service.PREVIEW_COOKIE_NAME,
        value=session.pop("token"),
        max_age=session["max_age"],
        httponly=True,
        secure=settings.app_env == "prod",
        samesite="strict",
        path=f"/api/sandboxes/{public_id}/preview",
    )
    return Resp(data=session)


@router.api_route("/{public_id}/preview/{preview_path:path}", methods=["GET", "HEAD", "POST"], include_in_schema=False)
async def preview_sandbox(
    public_id: str,
    preview_path: str,
    request: Request,
    db: Session = Depends(get_db),
    preview_token: str = Cookie(default="", alias=sandbox_service.PREVIEW_COOKIE_NAME),
):
    body = await request.body() if request.method == "POST" else b""
    status_code, headers, content = sandbox_service.proxy_preview(
        db,
        public_id,
        preview_token,
        preview_path,
        request.url.query,
        request.method,
        {name: request.headers.get(name, "") for name in ("Accept", "Accept-Language", "Content-Type")},
        body,
    )
    location = headers.pop("location", "")
    if location:
        parsed = urllib.parse.urlsplit(location)
        if not parsed.scheme and not parsed.netloc:
            suffix = location if location.startswith("/") else "/" + location
            headers["location"] = f"/api/sandboxes/{public_id}/preview{suffix}"
    headers.update({
        "content-security-policy": (
            "sandbox allow-scripts allow-forms; default-src 'self' data: blob:; "
            "connect-src 'self'; img-src 'self' data: blob:; "
            "style-src 'self' 'unsafe-inline'"
        ),
        "x-content-type-options": "nosniff",
        "referrer-policy": "no-referrer",
        "permissions-policy": "camera=(), microphone=(), geolocation=(), payment=(), usb=()",
        "x-frame-options": "SAMEORIGIN",
    })
    return RawResponse(content=content, status_code=status_code, headers=headers)
