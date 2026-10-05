"""产品反馈 HTTP 契约、本人回读和处理审计回归，使用隔离内存库。"""
import json
from datetime import datetime
from hashlib import sha256

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.v1 import feedback
from app.core.database import Base, get_db
from app.core.error_handlers import register_handlers
from app.core.security import create_access_token
from app.models.audit_log import AuditLog
from app.models.user import User
from app.models.user_feedback import UserFeedback


@pytest.fixture
def feedback_client():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    db = factory()
    users = {}
    for role in ("admin", "reviewer", "user"):
        user = User(username=f"feedback-{role}", password="local-test", role=role, status=1, token_version=0)
        db.add(user)
        users[role] = user
    db.commit()
    headers = {role: {"Authorization": f"Bearer {create_access_token(user.id, role)}"} for role, user in users.items()}
    app = FastAPI()
    register_handlers(app)
    app.include_router(feedback.router, prefix="/api/feedback")

    def isolated_db():
        with factory() as request_db:
            yield request_db

    app.dependency_overrides[get_db] = isolated_db
    with TestClient(app, raise_server_exceptions=False) as client:
        yield client, db, headers, users
    db.close()
    engine.dispose()


def submit(client, headers, *, content="反馈正文", feedback_type="bug"):
    response = client.post("/api/feedback", headers=headers, json={"content": content, "feedback_type": feedback_type})
    assert response.status_code == 200, response.text
    return response.json()["data"]["id"]


@pytest.mark.parametrize("role", ["reviewer", "user"])
def test_member_stats_and_lists_never_include_other_roles(feedback_client, role):
    client, _, headers, users = feedback_client
    ids = {actor: submit(client, auth, content=f"私有-{actor}") for actor, auth in headers.items()}
    listed = client.get("/api/feedback?scope=all", headers=headers[role]).json()["data"]
    assert [item["id"] for item in listed["items"]] == [ids[role]]
    assert listed["items"][0]["user_id"] == users[role].id
    foreign_role = "user" if role == "reviewer" else "reviewer"
    assert client.get(f"/api/feedback/{ids[foreign_role]}", headers=headers[role]).status_code == 403
    stats = client.get("/api/feedback/stats", headers=headers[role])
    assert stats.status_code == 200, stats.text
    assert stats.json()["data"] == {"new": 1, "read": 0, "replied": 0, "closed": 0, "total": 1}
    assert client.get("/api/feedback/stats", headers=headers["admin"]).json()["data"]["total"] == 3


@pytest.mark.parametrize("payload", [{"admin_reply": "", "status": "replied"},
                                    {"admin_reply": " \n ", "status": "replied"}, {"status": "replied"}])
def test_blank_reply_cannot_be_recorded_as_replied(feedback_client, payload):
    client, db, headers, _ = feedback_client
    feedback_id = submit(client, headers["user"])
    rejected = client.put(f"/api/feedback/{feedback_id}/reply", headers=headers["admin"], json=payload)
    assert rejected.status_code == 400, rejected.text
    row = db.get(UserFeedback, feedback_id)
    assert row.status == "new" and row.admin_reply is None and row.handled_by is None
    assert db.query(AuditLog).filter(AuditLog.target_type == "user_feedback").count() == 0


@pytest.mark.parametrize("role", ["reviewer", "user"])
def test_non_admin_cannot_mark_read_reply_or_reopen_even_own_record(feedback_client, role):
    client, _, headers, _ = feedback_client
    feedback_id = submit(client, headers[role])
    assert client.post(f"/api/feedback/{feedback_id}/read", headers=headers[role]).status_code == 403
    for payload in ({"admin_reply": "越权", "status": "replied"}, {"status": "new"}):
        assert client.put(f"/api/feedback/{feedback_id}/reply", headers=headers[role], json=payload).status_code == 403


def test_explicit_read_is_idempotent_and_get_remains_read_only(feedback_client):
    client, db, headers, users = feedback_client
    feedback_id = submit(client, headers["user"])
    assert client.get(f"/api/feedback/{feedback_id}", headers=headers["admin"]).json()["data"]["status"] == "new"
    first = client.post(f"/api/feedback/{feedback_id}/read", headers=headers["admin"]).json()["data"]
    assert first["status"] == "read" and first["handled_by"] == users["admin"].id
    assert first["handled_at"] is not None
    second = client.post(f"/api/feedback/{feedback_id}/read", headers=headers["admin"]).json()["data"]
    assert second["handled_at"] == first["handled_at"]
    assert client.get(f"/api/feedback/{feedback_id}", headers=headers["user"]).json()["data"]["status"] == "read"
    assert db.query(AuditLog).filter(AuditLog.action == "feedback.mark_read").count() == 1


def test_reply_close_and_existing_status_reopen_are_visible_to_owner_and_audited(feedback_client):
    client, db, headers, users = feedback_client
    feedback_id = submit(client, headers["user"], content="私人反馈正文")
    endpoint = f"/api/feedback/{feedback_id}/reply"
    for reply in ("第一条私人回复", "第二条私人回复"):
        response = client.put(endpoint, headers=headers["admin"], json={"admin_reply": reply, "status": "replied"})
        assert response.status_code == 200, response.text
    owner = client.get(f"/api/feedback/{feedback_id}", headers=headers["user"]).json()["data"]
    assert owner["admin_reply"] == "第二条私人回复" and owner["status"] == "replied"
    assert owner["handled_by"] == users["admin"].id and owner["handled_at"] is not None
    for status in ("closed", "new"):
        assert client.put(endpoint, headers=headers["admin"], json={"status": status}).status_code == 200
        assert client.get(f"/api/feedback/{feedback_id}", headers=headers["user"]).json()["data"]["status"] == status
    logs = db.query(AuditLog).filter(AuditLog.action == "feedback.update").order_by(AuditLog.id).all()
    assert len(logs) == 4
    overwritten = json.loads(logs[1].detail)
    assert overwritten["reply_changed"] is True
    assert overwritten["previous_reply_sha256"] == sha256("第一条私人回复".encode()).hexdigest()
    assert overwritten["reply_sha256"] == sha256("第二条私人回复".encode()).hexdigest()
    assert not any("私人" in log.detail for log in logs)


def test_feedback_update_and_audit_commit_together(feedback_client):
    client, db, headers, _ = feedback_client
    feedback_id = submit(client, headers["user"])

    def fail_audit_insert(_, __, statement, ___, ____, _____):
        if statement.lstrip().lower().startswith("insert into audit_log"):
            raise RuntimeError("isolated audit failure")

    event.listen(db.bind, "before_cursor_execute", fail_audit_insert)
    try:
        response = client.put(f"/api/feedback/{feedback_id}/reply", headers=headers["admin"],
                              json={"admin_reply": "不能单独提交", "status": "replied"})
    finally:
        event.remove(db.bind, "before_cursor_execute", fail_audit_insert)
    assert response.status_code == 500
    restored = client.get(f"/api/feedback/{feedback_id}", headers=headers["user"]).json()["data"]
    assert restored["status"] == "new" and restored["admin_reply"] is None


def test_state_only_close_and_read_of_closed_record_do_not_invent_a_reply(feedback_client):
    client, db, headers, _ = feedback_client
    feedback_id = submit(client, headers["user"])
    unchanged = client.put(f"/api/feedback/{feedback_id}/reply", headers=headers["admin"], json={"status": "new"})
    assert unchanged.json()["data"]["handled_at"] is None
    closed = client.put(f"/api/feedback/{feedback_id}/reply", headers=headers["admin"],
                        json={"admin_reply": " \n ", "status": "closed"}).json()["data"]
    assert closed["status"] == "closed" and not closed["admin_reply"]
    after_read = client.post(f"/api/feedback/{feedback_id}/read", headers=headers["admin"]).json()["data"]
    assert after_read == closed
    assert db.query(AuditLog).filter(AuditLog.target_type == "user_feedback").count() == 1


def test_existing_nonempty_reply_allows_status_only_update_and_repeated_save_is_noop(feedback_client):
    client, db, headers, _ = feedback_client
    feedback_id = submit(client, headers["reviewer"])
    endpoint = f"/api/feedback/{feedback_id}/reply"
    first = client.put(endpoint, headers=headers["admin"], json={"admin_reply": "已处理"}).json()["data"]
    assert first["status"] == "replied"
    assert client.put(endpoint, headers=headers["admin"], json={"status": "new"}).status_code == 200
    replied = client.put(endpoint, headers=headers["admin"], json={"status": "replied"}).json()["data"]
    repeated = client.put(endpoint, headers=headers["admin"], json={"status": "replied"}).json()["data"]
    assert replied == repeated and replied["admin_reply"] == "已处理"
    assert db.query(AuditLog).filter(AuditLog.action == "feedback.update").count() == 3


def test_status_type_filters_and_same_time_pagination_preserve_owner_scope(feedback_client):
    client, db, headers, _ = feedback_client
    first = submit(client, headers["user"])
    second = submit(client, headers["user"])
    submit(client, headers["user"], feedback_type="suggestion")
    submit(client, headers["reviewer"])
    db.query(UserFeedback).update({UserFeedback.create_time: datetime(2026, 10, 6)})
    db.commit()
    params = {"scope": "all", "status": "new", "feedback_type": "bug", "page_size": 1}
    for page_number, expected in ((1, second), (2, first)):
        response = client.get("/api/feedback", headers=headers["user"], params={**params, "page": page_number})
        result = response.json()["data"]
        assert [row["id"] for row in result["items"]] == [expected]
        assert result["total"] == 2 and result["pages"] == 2
    assert client.get("/api/feedback", headers=headers["admin"], params=params).json()["data"]["total"] == 3


@pytest.mark.parametrize("query", ["status=processing", "feedback_type=invalid"])
def test_unknown_feedback_filters_are_rejected(feedback_client, query):
    client, _, headers, _ = feedback_client
    assert client.get(f"/api/feedback?{query}", headers=headers["user"]).status_code == 400


@pytest.mark.parametrize("method,path,payload", [("get", "", None), ("get", "/stats", None),
                                                ("get", "/1", None), ("post", "", {"content": "匿名"}),
                                                ("post", "/1/read", None), ("put", "/1/reply", {"status": "read"})])
def test_feedback_endpoints_require_login(feedback_client, method, path, payload):
    client, _, _, _ = feedback_client
    assert getattr(client, method)(f"/api/feedback{path}", **({"json": payload} if payload else {})).status_code == 401
