"""企业共享文字与附件知识接口；附件检索只使用服务端本地向量。"""

from urllib.parse import quote

from fastapi import APIRouter, Depends, File, Form, Query, UploadFile
from fastapi.responses import Response as FastResponse
from pydantic import Field
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.dependencies import get_current_user
from app.core.exceptions import NotFoundError
from app.models.user import User
from app.schemas.common import Resp, StrictInputModel
from app.services import enterprise_knowledge_attachment_service as attachment_service
from app.services import enterprise_knowledge_service as service

router = APIRouter(prefix="/enterprises/{enterprise_id}/knowledge", tags=["企业知识库"])


class EntryIn(StrictInputModel):
    title: str = Field(min_length=1, max_length=200)
    content: str = Field(min_length=1, max_length=service.MAX_CONTENT)


class EntryCreateIn(EntryIn):
    client_request_id: str = Field(min_length=1, max_length=80)


class EntryUpdateIn(EntryIn):
    expected_version: int = Field(ge=1)


def _out(row, *, detail=False):
    result = {
        "id": row.id,
        "enterprise_id": row.enterprise_id,
        "author_id": row.author_id,
        "updated_by": row.updated_by,
        "title": row.title,
        "version": row.version,
        "create_time": row.create_time,
        "update_time": row.update_time,
    }
    if detail:
        result["content"] = row.content
    return result


def _attachment_out(row):
    return {
        "id": row.id,
        "enterprise_id": row.enterprise_id,
        "author_id": row.author_id,
        "updated_by": row.updated_by,
        "file_name": row.file_name,
        "file_ext": row.file_ext,
        "mime": row.mime,
        "file_size": row.file_size,
        "chunk_count": row.chunk_count,
        "version": row.version,
        "create_time": row.create_time,
        "update_time": row.update_time,
    }


@router.get("/attachments", response_model=Resp[dict])
def attachments(
    enterprise_id: int,
    before_id: int | None = Query(None, gt=0),
    limit: int = Query(30, ge=1, le=100),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    rows = attachment_service.list_attachments(db, enterprise_id, user.id, before_id=before_id, limit=limit)
    return Resp(data={"items": [_attachment_out(row) for row in rows]})


@router.get("/attachments/search", response_model=Resp[dict])
def search_attachments(
    enterprise_id: int,
    q: str = Query(..., min_length=1, max_length=100),
    limit: int = Query(3, ge=1, le=10),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    items = attachment_service.search_attachments(db, enterprise_id, user.id, q, limit=limit)
    return Resp(data={"items": items, "search_mode": "local_hash_vector_and_terms"})


@router.post("/attachments", response_model=Resp[dict])
def upload_attachment(
    enterprise_id: int,
    file: UploadFile = File(...),
    client_request_id: str = Form(..., min_length=1, max_length=80),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    try:
        raw = file.file.read(attachment_service.MAX_ATTACHMENT_BYTES + 1)
        row = attachment_service.create_attachment(
            db,
            enterprise_id,
            user.id,
            file.filename or "",
            raw,
            client_request_id=client_request_id,
        )
        db.commit()
        db.refresh(row)
        return Resp(data=_attachment_out(row), message="企业知识附件已上传")
    except BaseException:
        db.rollback()
        raise
    finally:
        file.file.close()


@router.get("/attachments/{attachment_id}/download")
def download_attachment(
    enterprise_id: int,
    attachment_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    row = attachment_service.get_attachment(db, enterprise_id, user.id, attachment_id)
    if not row.original_data:
        raise NotFoundError("企业知识附件不存在")
    encoded_name = quote(row.file_name, safe="")
    return FastResponse(
        content=row.original_data,
        media_type="application/octet-stream",
        headers={
            "Content-Disposition": f"attachment; filename*=UTF-8''{encoded_name}",
            "X-Content-Type-Options": "nosniff",
            "Cache-Control": "private, no-store",
        },
    )


@router.delete("/attachments/{attachment_id}", response_model=Resp[None])
def delete_attachment(
    enterprise_id: int,
    attachment_id: int,
    expected_version: int = Query(..., ge=1),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    attachment_service.delete_attachment(
        db, enterprise_id, user.id, attachment_id, expected_version=expected_version,
    )
    db.commit()
    return Resp(data=None)


@router.get("/entries", response_model=Resp[dict])
def entries(
    enterprise_id: int,
    before_id: int | None = Query(None, gt=0),
    limit: int = Query(30, ge=1, le=100),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    rows = service.list_entries(db, enterprise_id, user.id, before_id=before_id, limit=limit)
    return Resp(data={"items": [_out(row) for row in rows]})


@router.get("/search", response_model=Resp[dict])
def search(
    enterprise_id: int,
    q: str = Query(..., min_length=1, max_length=100),
    before_id: int | None = Query(None, gt=0),
    limit: int = Query(30, ge=1, le=100),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    rows = service.search_entries(db, enterprise_id, user.id, q, before_id=before_id, limit=limit)
    return Resp(data={"items": [_out(row) for row in rows], "search_mode": "literal"})


@router.post("/entries", response_model=Resp[dict])
def create(
    enterprise_id: int, payload: EntryCreateIn, db: Session = Depends(get_db), user: User = Depends(get_current_user)
):
    row = service.create_entry(
        db, enterprise_id, user.id, payload.title, payload.content, client_request_id=payload.client_request_id
    )
    db.commit()
    return Resp(data=_out(row, detail=True))


@router.get("/entries/{entry_id}", response_model=Resp[dict])
def detail(enterprise_id: int, entry_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    return Resp(data=_out(service.get_entry(db, enterprise_id, user.id, entry_id), detail=True))


@router.put("/entries/{entry_id}", response_model=Resp[dict])
def update(
    enterprise_id: int,
    entry_id: int,
    payload: EntryUpdateIn,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    row = service.update_entry(db, enterprise_id, user.id, entry_id, payload.title, payload.content,
                               expected_version=payload.expected_version)
    db.commit()
    return Resp(data=_out(row, detail=True))


@router.delete("/entries/{entry_id}", response_model=Resp[None])
def delete(enterprise_id: int, entry_id: int, expected_version: int = Query(..., ge=1),
           db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    service.delete_entry(db, enterprise_id, user.id, entry_id, expected_version=expected_version)
    db.commit()
    return Resp(data=None)
