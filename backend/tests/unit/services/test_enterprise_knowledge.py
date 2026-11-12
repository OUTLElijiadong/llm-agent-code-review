"""企业文本知识库按企业与当前成员身份授权。"""

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
from app.core.exceptions import AppError, ConflictError, ForbiddenError, NotFoundError
from app.models.billing_ledger import (
    BillingOrder, BillingPaymentEvent, BillingRefund, BillingSubscription, BillingSubscriptionPeriod,
)
from app.models.enterprise_knowledge import EnterpriseKnowledgeEntry
from app.models.enterprise_org import Enterprise, EnterpriseAuditEvent, EnterpriseMember
from app.models.user import User
from app.services import enterprise_knowledge_service as knowledge


@pytest.fixture
def db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(
        engine,
        tables=[
            User.__table__,
            Enterprise.__table__,
            EnterpriseMember.__table__,
            EnterpriseAuditEvent.__table__,
            EnterpriseKnowledgeEntry.__table__,
            BillingOrder.__table__,
            BillingPaymentEvent.__table__,
            BillingSubscription.__table__,
            BillingSubscriptionPeriod.__table__,
            BillingRefund.__table__,
        ],
    )
    session = sessionmaker(bind=engine, expire_on_commit=False)()
    session.add_all(
        [
            User(
                id=i,
                username=f"u{i}",
                password="x",
                email=f"u{i}@test.invalid",
                role="admin" if i == 3 else "user",
                status=1,
            )
            for i in range(1, 5)
        ]
    )
    session.add_all(
        [
            Enterprise(id=10, name="甲", owner_id=1, seat_limit=5, status="active", paid_order_id="order-a"),
            Enterprise(id=20, name="乙", owner_id=4, seat_limit=5, status="active", paid_order_id="order-b"),
            EnterpriseMember(enterprise_id=10, user_id=1, role="owner", status="active"),
            EnterpriseMember(enterprise_id=10, user_id=2, role="member", status="active"),
            EnterpriseMember(enterprise_id=20, user_id=4, role="owner", status="active"),
        ]
    )
    session.commit()
    yield session
    session.close()
    engine.dispose()


def test_enterprise_auth_create_read_update_delete_and_search(db):
    row = knowledge.create_entry(db, 10, 1, "发布规则", "部署前执行安全检查", client_request_id="req-1")
    db.commit()
    assert knowledge.get_entry(db, 10, 2, row.id).content == "部署前执行安全检查"
    assert [e.id for e in knowledge.search_entries(db, 10, 2, "安全检查")] == [row.id]
    assert knowledge.search_entries(db, 10, 2, "%") == []  # SQL 通配符必须按字面搜索
    with pytest.raises(ForbiddenError):
        knowledge.create_entry(db, 10, 2, "无权限", "正文", client_request_id="req-2")
    with pytest.raises(ForbiddenError):
        knowledge.update_entry(db, 10, 2, row.id, "篡改", "正文", expected_version=1)
    with pytest.raises(ForbiddenError):
        knowledge.delete_entry(db, 10, 2, row.id, expected_version=1)
    with pytest.raises(ForbiddenError):
        knowledge.search_entries(db, 10, 3, "安全")  # 平台管理员不具有企业成员身份
    with pytest.raises(NotFoundError):
        knowledge.get_entry(db, 20, 4, row.id)
    assert knowledge.search_entries(db, 20, 4, "安全") == []
    knowledge.update_entry(db, 10, 1, row.id, "新标题", "变更正文", expected_version=1)
    db.commit()
    assert knowledge.get_entry(db, 10, 2, row.id).title == "新标题"
    knowledge.delete_entry(db, 10, 1, row.id, expected_version=2)
    db.commit()
    assert knowledge.search_entries(db, 10, 2, "正文") == []
    with pytest.raises(NotFoundError):
        knowledge.get_entry(db, 10, 2, row.id)


def test_removed_member_immediately_loses_access(db):
    row = knowledge.create_entry(db, 10, 1, "文档", "保密内容", client_request_id="req-1")
    db.commit()
    member = db.query(EnterpriseMember).filter_by(enterprise_id=10, user_id=2).one()
    member.status = "removed"
    db.commit()
    for action in (
        lambda: knowledge.get_entry(db, 10, 2, row.id),
        lambda: knowledge.list_entries(db, 10, 2),
        lambda: knowledge.search_entries(db, 10, 2, "保密"),
    ):
        with pytest.raises(ForbiddenError):
            action()


def test_create_request_key_is_idempotent_and_bound_to_content(db):
    first = knowledge.create_entry(db, 10, 1, "规则", "正文", client_request_id="create-1")
    db.commit()
    again = knowledge.create_entry(db, 10, 1, "规则", "正文", client_request_id="create-1")
    assert again.id == first.id
    with pytest.raises(ConflictError):
        knowledge.create_entry(db, 10, 1, "规则", "其他正文", client_request_id="create-1")
    other = knowledge.create_entry(db, 20, 4, "规则", "正文", client_request_id="create-1")
    assert other.id != first.id
    knowledge.delete_entry(db, 10, 1, first.id, expected_version=1)
    db.commit()
    with pytest.raises(ConflictError):
        knowledge.create_entry(db, 10, 1, "规则", "正文", client_request_id="create-1")


def test_list_and_search_keyset_page_stays_in_enterprise(db):
    for index in range(35):
        knowledge.create_entry(db, 10, 1, f"规则{index}", "共同词", client_request_id=f"page-{index}")
    knowledge.create_entry(db, 20, 4, "隔离", "共同词", client_request_id="other")
    db.commit()
    for query in (
        lambda **kwargs: knowledge.list_entries(db, 10, 2, **kwargs),
        lambda **kwargs: knowledge.search_entries(db, 10, 2, "共同词", **kwargs),
    ):
        first = query(limit=30)
        second = query(before_id=first[-1].id, limit=30)
        assert len(first) == 30 and len(second) == 5
        assert all(entry.enterprise_id == 10 for entry in first + second)
        assert len({entry.id for entry in first + second}) == 35


def test_stale_knowledge_update_or_delete_cannot_overwrite_newer_version(db):
    first = knowledge.create_entry(db, 10, 1, "标题", "初始正文", client_request_id="version-test")
    db.commit()
    assert first.version == 1
    knowledge.update_entry(db, 10, 1, first.id, "标题", "较新正文", expected_version=1)
    db.commit()
    with pytest.raises(ConflictError):
        knowledge.update_entry(db, 10, 1, first.id, "标题", "迟到旧正文", expected_version=1)
    with pytest.raises(ConflictError):
        knowledge.delete_entry(db, 10, 1, first.id, expected_version=1)
    assert knowledge.get_entry(db, 10, 1, first.id).content == "较新正文"
    knowledge.delete_entry(db, 10, 1, first.id, expected_version=2)
    db.commit()


def test_http_identity_scope_and_validation(db):
    app = FastAPI()
    app.include_router(router)

    @app.exception_handler(AppError)
    async def app_error(_, exc):
        return JSONResponse(status_code=exc.http_status, content={"message": exc.message})

    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_user] = lambda x_test_user=Header(): db.get(User, int(x_test_user))
    with TestClient(app) as client:
        path = "/enterprises/10/knowledge/entries"
        assert (
            client.post(
                path,
                headers={"x-test-user": "2"},
                json={"title": "无权", "content": "正文", "client_request_id": "req-1"},
            ).status_code
            == 403
        )
        created = client.post(
            path,
            headers={"x-test-user": "1"},
            json={"title": "规定", "content": "系统安全", "client_request_id": "req-1"},
        )
        assert created.status_code == 200
        entry_id = created.json()["data"]["id"]
        assert client.get(f"{path}/{entry_id}", headers={"x-test-user": "2"}).json()["data"]["content"] == "系统安全"
        assert client.get(f"{path}/{entry_id}", headers={"x-test-user": "3"}).status_code == 403
        assert (
            client.get(f"/enterprises/20/knowledge/entries/{entry_id}", headers={"x-test-user": "4"}).status_code == 404
        )
        assert (
            client.get("/enterprises/10/knowledge/search", headers={"x-test-user": "2"}, params={"q": "安全"}).json()[
                "data"
            ]["items"][0]["id"]
            == entry_id
        )
        assert client.post(
            path, headers={"x-test-user": "1"}, json={"title": " ", "content": "正文", "client_request_id": "req-2"}
        ).status_code in (
            400,
            422,
        )
        update_url = f"{path}/{entry_id}"
        updated = client.put(update_url, headers={"x-test-user": "1"},
                             json={"title": "规定", "content": "新正文", "expected_version": 1})
        assert updated.status_code == 200 and updated.json()["data"]["version"] == 2
        stale = client.put(update_url, headers={"x-test-user": "1"},
                           json={"title": "规定", "content": "迟到旧正文", "expected_version": 1})
        assert stale.status_code == 409
        stale_delete = client.delete(
            update_url, headers={"x-test-user": "1"}, params={"expected_version": 1}
        )
        valid_delete = client.delete(
            update_url, headers={"x-test-user": "1"}, params={"expected_version": 2}
        )
        assert stale_delete.status_code == 409
        assert valid_delete.status_code == 200
