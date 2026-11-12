"""Keyset paging must remain stable, scoped and bounded for enterprise content."""
import pytest
from fastapi import FastAPI, Header
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from test_enterprise_chat_service import orgdb as enterprise_db_fixture

from app.api.v1.enterprise_content import router
from app.core.database import get_db
from app.core.dependencies import get_current_user
from app.core.exceptions import AppError, ForbiddenError, NotFoundError
from app.models.enterprise_content import EnterpriseAnnouncement, EnterpriseForumPost, EnterpriseForumReply
from app.models.user import User
from app.services import enterprise_content_service as content

orgdb = enterprise_db_fixture


def test_content_pages_use_stable_ids_and_enforce_enterprise_scope(orgdb):
    orgdb.add_all([EnterpriseForumPost(enterprise_id=10, author_id=2, title=f"post-{i}", content="正文")
                   for i in range(55)])
    orgdb.add_all([EnterpriseAnnouncement(enterprise_id=10, author_id=1, title=f"notice-{i}", content="正文")
                   for i in range(55)])
    orgdb.add(EnterpriseForumPost(enterprise_id=20, author_id=4, title="other", content="other"))
    orgdb.commit()
    for listing in (content.posts, content.announcements):
        first = listing(orgdb, 10, 2, limit=50)
        older = listing(orgdb, 10, 2, limit=50, before_id=first[-1].id)
        assert len(first) == 50 and len(older) == 5
        assert all(row.enterprise_id == 10 for row in first + older)
        assert [row.id for row in first + older] == sorted((row.id for row in first + older), reverse=True)
        assert listing(orgdb, 10, 2, before_id=older[-1].id) == []
        with pytest.raises(ForbiddenError):
            listing(orgdb, 10, 4, before_id=first[-1].id)


def test_reply_pages_are_ordered_and_limited_to_the_post_and_enterprise(orgdb):
    post = content.create_post(orgdb, 10, 2, "帖子", "正文")
    another = content.create_post(orgdb, 10, 2, "另一个", "正文")
    orgdb.add_all([EnterpriseForumReply(enterprise_id=10, post_id=post.id, author_id=2, content=f"reply-{i}")
                   for i in range(105)])
    orgdb.add(EnterpriseForumReply(enterprise_id=10, post_id=another.id, author_id=2, content="other"))
    orgdb.commit()
    first = content.replies(orgdb, 10, 2, post.id, limit=100)
    later = content.replies(orgdb, 10, 2, post.id, limit=100, after_id=first[-1].id)
    assert len(first) == 100 and len(later) == 5
    assert [row.content for row in first + later] == [f"reply-{i}" for i in range(105)]
    assert content.replies(orgdb, 10, 2, post.id, after_id=later[-1].id) == []
    with pytest.raises(ForbiddenError):
        content.replies(orgdb, 10, 4, post.id, after_id=first[-1].id)
    with pytest.raises(NotFoundError):
        content.replies(orgdb, 20, 4, post.id, after_id=first[-1].id)


def test_content_http_cursor_contract_and_auth(orgdb):
    orgdb.add_all([EnterpriseForumPost(enterprise_id=10, author_id=2, title=f"post-{i}", content="正文")
                   for i in range(3)])
    orgdb.commit()
    app = FastAPI()
    app.include_router(router)

    @app.exception_handler(AppError)
    async def on_error(_, exc):
        return JSONResponse(status_code=exc.http_status, content={"message": exc.message})

    def current_user(x_test_user: int = Header()):
        return orgdb.get(User, x_test_user)

    app.dependency_overrides[get_current_user] = current_user
    app.dependency_overrides[get_db] = lambda: orgdb
    with TestClient(app) as client:
        url = "/enterprises/10/forum/posts"
        headers = {"x-test-user": "2"}
        result = client.get(url, headers=headers, params={"limit": 2}).json()["data"]["items"]
        older = client.get(url, headers=headers, params={"limit": 2, "before_id": result[-1]["id"]})
        assert len(result) == 2 and len(older.json()["data"]["items"]) == 1
        assert client.get(url, headers=headers, params={"before_id": 0}).status_code == 422
        assert client.get(url, headers={"x-test-user": "4"}, params={"before_id": result[-1]["id"]}).status_code == 403
        detail_url = f'{url}/{result[0]["id"]}'
        assert client.get(detail_url, headers=headers, params={"after_reply_id": -1}).status_code == 422
        assert client.get(detail_url, headers=headers, params={"after_reply_id": 0}).status_code == 200
        payload = {"title": "网络重试", "content": "内容", "request_key": "request-http-1"}
        first = client.post(url, headers=headers, json=payload)
        retry = client.post(url, headers=headers, json=payload)
        assert first.status_code == retry.status_code == 200
        assert first.json()["data"]["id"] == retry.json()["data"]["id"]
        conflict = client.post(url, headers=headers, json={**payload, "content": "不同内容"})
        assert conflict.status_code == 409
