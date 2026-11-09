"""企业知识附件的租户隔离、撤销、幂等和 HTTP 边界。"""

import io

import pytest
from fastapi import FastAPI, Header
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.v1.enterprise_knowledge import router
from app.core.database import Base, get_db
from app.core.dependencies import get_current_user
from app.core.exceptions import (
    AppError,
    ConflictError,
    ForbiddenError,
    NotFoundError,
    PayloadTooLargeError,
    ValidationError,
)
from app.models.enterprise_knowledge import EnterpriseKnowledgeAttachment, EnterpriseKnowledgeAttachmentChunk
from app.models.enterprise_org import Enterprise, EnterpriseAuditEvent, EnterpriseMember
from app.models.user import User
from app.services import enterprise_knowledge_attachment_service as attachments
from app.services import enterprise_org_service as org


@pytest.fixture
def db(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine, tables=[
        User.__table__, Enterprise.__table__, EnterpriseMember.__table__, EnterpriseAuditEvent.__table__,
        EnterpriseKnowledgeAttachment.__table__, EnterpriseKnowledgeAttachmentChunk.__table__,
    ])
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    session = factory()
    session.add_all([
        User(id=i, username=f"u{i}", password="x", email=f"u{i}@test.invalid", role="user", status=1)
        for i in range(1, 5)
    ])
    session.add_all([
        Enterprise(id=10, name="甲", owner_id=1, seat_limit=5, status="active", paid_order_id="order-a"),
        Enterprise(id=20, name="乙", owner_id=4, seat_limit=5, status="active", paid_order_id="order-b"),
        EnterpriseMember(enterprise_id=10, user_id=1, role="owner", status="active"),
        EnterpriseMember(enterprise_id=10, user_id=2, role="member", status="active"),
        EnterpriseMember(enterprise_id=20, user_id=4, role="owner", status="active"),
    ])
    session.commit()
    monkeypatch.setattr(org, "_billing_access_suspended", lambda *_args, **_kwargs: False)
    monkeypatch.setattr("app.services.billing_checkout_service.enterprise_subscription_status",
                        lambda *_args, **_kwargs: "active")
    yield session
    session.close()
    engine.dispose()


def test_text_upload_is_scanned_idempotent_and_searchable_only_inside_enterprise(db, monkeypatch):
    scans = []
    embedded = []
    monkeypatch.setattr(attachments, "_scan_upload", lambda name, raw: scans.append((name, raw)))
    monkeypatch.setattr(attachments.embedding_service, "embed_local_texts",
                        lambda parts: (embedded.append(parts) or ([[1.0, 0.0] for _ in parts], "local:hash-v1:2")))
    raw = "部署规则：生产发布前先审批".encode()
    first = attachments.create_attachment(db, 10, 1, "发布规则.md", raw, client_request_id="request-1")
    db.commit()
    repeated = attachments.create_attachment(db, 10, 1, "发布规则.md", raw, client_request_id="request-1")
    assert repeated.id == first.id
    assert scans == [("发布规则.md", raw)]
    assert len(embedded) == 1
    assert attachments.search_attachments(db, 10, 2, "生产发布")
    assert attachments.search_attachments(db, 20, 4, "生产发布") == []
    with pytest.raises(ForbiddenError):
        attachments.create_attachment(db, 10, 2, "不允许.txt", b"private", client_request_id="request-2")
    with pytest.raises(NotFoundError):
        attachments.get_attachment(db, 20, 4, first.id)
    with pytest.raises(ConflictError):
        attachments.create_attachment(db, 10, 1, "其他内容.md", b"different", client_request_id="request-1")


def test_idempotent_upload_rechecks_admin_before_returning_existing_attachment(db, monkeypatch):
    monkeypatch.setattr(attachments, "_scan_upload", lambda *_args: None)
    existing = attachments.create_attachment(
        db, 10, 1, "幂等.txt", "已保存".encode(), client_request_id="retry-1",
    )
    db.commit()

    # Simulate an RR snapshot that still sees the actor as admin on the
    # optimistic preflight; the current-lock recheck must reject the retry.
    real_require_admin = org.require_enterprise_admin

    def stale_snapshot_preflight(session, enterprise_id, actor_id, *, lock=False, owner_only=False):
        if actor_id == 1 and not lock:
            return EnterpriseMember(enterprise_id=enterprise_id, user_id=actor_id,
                                    role="owner", status="active")
        return real_require_admin(session, enterprise_id, actor_id, lock=lock, owner_only=owner_only)

    monkeypatch.setattr(org, "require_enterprise_admin", stale_snapshot_preflight)
    # Explicitly revoke the owner only to model an administrative account
    # revocation without changing any unrelated member or attachment rows.
    db.query(EnterpriseMember).filter_by(enterprise_id=10, user_id=1).update({"status": "removed"})
    db.commit()

    with pytest.raises(ForbiddenError):
        attachments.create_attachment(
            db, 10, 1, "幂等.txt", "已保存".encode(), client_request_id="retry-1",
        )
    assert db.get(EnterpriseKnowledgeAttachment, existing.id).status == "active"


def test_delete_erases_blob_text_and_chunks_and_immediately_hides_search(db, monkeypatch):
    monkeypatch.setattr(attachments, "_scan_upload", lambda *_args: None)
    row = attachments.create_attachment(db, 10, 1, "撤权.txt", "秘密发布计划".encode(), client_request_id="delete-1")
    db.commit()
    assert attachments.search_attachments(db, 10, 2, "秘密")
    attachments.delete_attachment(db, 10, 1, row.id, expected_version=1)
    db.commit()
    assert db.query(EnterpriseKnowledgeAttachmentChunk).filter_by(attachment_id=row.id).count() == 0
    deleted = db.get(EnterpriseKnowledgeAttachment, row.id)
    assert (deleted.status == "deleted" and deleted.original_data is None
            and deleted.extracted_text is None and deleted.chunk_count == 0)
    assert attachments.search_attachments(db, 10, 2, "秘密") == []
    with pytest.raises(NotFoundError):
        attachments.get_attachment(db, 10, 2, row.id)


@pytest.mark.parametrize("filename,raw", [
    ("bad.exe", b"MZ"), ("bad.txt", b"\xff"), ("bad.txt", b"\x00binary"),
    ("empty.md", b"  \n"),
])
def test_rejects_unsupported_or_non_text_content_before_storage(db, monkeypatch, filename, raw):
    scanned = []
    monkeypatch.setattr(attachments, "_scan_upload", lambda *_args: scanned.append(True))
    with pytest.raises(ValidationError):
        attachments.create_attachment(db, 10, 1, filename, raw, client_request_id="invalid-1")
    assert db.query(EnterpriseKnowledgeAttachment).count() == 0
    if filename.endswith(".exe"):
        assert not scanned


def test_rejects_extracted_text_over_limit_as_validation_error(db, monkeypatch):
    monkeypatch.setattr(attachments, "_scan_upload", lambda *_args: None)
    with pytest.raises(ValidationError, match="50 万字符"):
        attachments.create_attachment(
            db, 10, 1, "large.txt", b"a" * (attachments.MAX_EXTRACTED_CHARS + 1), client_request_id="too-large",
        )
    assert db.query(EnterpriseKnowledgeAttachment).count() == 0


def test_enterprise_active_chunk_quota_is_serialized_under_company_write_lock(db, monkeypatch):
    monkeypatch.setattr(attachments, "_scan_upload", lambda *_args: None)
    monkeypatch.setattr(attachments, "MAX_ENTERPRISE_ACTIVE_CHUNKS", 1)
    attachments.create_attachment(db, 10, 1, "one.txt", b"one", client_request_id="quota-1")
    db.commit()
    with pytest.raises(PayloadTooLargeError, match="最多 1 个有效片段"):
        attachments.create_attachment(db, 10, 1, "two.txt", b"two", client_request_id="quota-2")
    db.rollback()
    assert db.query(EnterpriseKnowledgeAttachment).filter_by(status="active").count() == 1


def test_docx_is_extracted_in_limited_worker_and_scanned_before_storage(db, monkeypatch):
    docx = pytest.importorskip("docx")
    document = docx.Document()
    document.add_paragraph("员工手册：远程测试必须先审批")
    buffer = io.BytesIO()
    document.save(buffer)
    raw = buffer.getvalue()
    scans = []
    monkeypatch.setattr(attachments, "_scan_upload", lambda name, data: scans.append((name, data)))
    row = attachments.create_attachment(db, 10, 1, "员工手册.docx", raw, client_request_id="docx-1")
    assert row.extracted_text == "员工手册：远程测试必须先审批"
    assert scans == [("员工手册.docx", raw)]


def test_http_upload_permissions_download_headers_and_delete(db, monkeypatch):
    monkeypatch.setattr(attachments, "_scan_upload", lambda *_args: None)
    app = FastAPI()
    app.include_router(router)

    @app.exception_handler(AppError)
    async def app_error(_, exc):
        return JSONResponse(status_code=exc.http_status, content={"code": exc.code, "message": exc.message})

    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_user] = lambda x_test_user=Header(): db.get(User, int(x_test_user))
    with TestClient(app) as client:
        path = "/enterprises/10/knowledge/attachments"
        denied = client.post(path, headers={"x-test-user": "2"}, data={"client_request_id": "http-denied"},
                             files={"file": ("shared.txt", b"content")})
        assert denied.status_code == 403
        uploaded = client.post(path, headers={"x-test-user": "1"}, data={"client_request_id": "http-1"},
                               files={"file": ("规范.md", "部署前检查".encode(), "text/markdown")})
        assert uploaded.status_code == 200
        attachment_id = uploaded.json()["data"]["id"]
        listed = client.get(path, headers={"x-test-user": "2"})
        assert listed.status_code == 200 and listed.json()["data"]["items"][0]["id"] == attachment_id
        downloaded = client.get(f"{path}/{attachment_id}/download", headers={"x-test-user": "2"})
        assert downloaded.status_code == 200 and downloaded.content == "部署前检查".encode()
        assert downloaded.headers["x-content-type-options"] == "nosniff"
        assert downloaded.headers["cache-control"] == "private, no-store"
        assert "filename*=UTF-8''" in downloaded.headers["content-disposition"]
        cross_tenant = client.get(f"/enterprises/20/knowledge/attachments/{attachment_id}/download",
                                  headers={"x-test-user": "4"})
        assert cross_tenant.status_code == 404
        member_delete = client.delete(f"{path}/{attachment_id}", headers={"x-test-user": "2"},
                                      params={"expected_version": 1})
        assert member_delete.status_code == 403
        removed = client.delete(f"{path}/{attachment_id}", headers={"x-test-user": "1"},
                                params={"expected_version": 1})
        assert removed.status_code == 200
