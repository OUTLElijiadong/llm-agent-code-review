"""置顶展示与最新查询有明确、稳定的不同排序契约。"""
from datetime import datetime, timedelta

import pytest

from app.core.exceptions import NotFoundError, ValidationError
from app.models.forum_post import ForumPost
from app.models.user import User
from app.schemas.common import PageOut, Resp
from app.schemas.forum import PostDetailOut, PostListItemOut
from app.services import forum_service
from app.services.admin_capability_registry import operation_contract
from app.services.user_capability_registry import CAPABILITY_BY_CODE


def test_newest_excludes_pin_priority_and_pages_equal_timestamps_stably(db):
    actor = User(username="forum-order-owner", password="unused", role="user", status=1)
    db.add(actor)
    db.flush()
    when = datetime(2026, 10, 14)
    posts = [ForumPost(user_id=actor.id, title=f"帖子{i}", content="正文",
                       category="tech", status="normal", is_pinned=(i == 0),
                       create_time=when - timedelta(days=1) if i == 0 else when)
             for i in range(5)]
    db.add_all(posts)
    db.commit()
    ids = [p.id for p in posts]
    assert forum_service.list_posts(db)["items"][0]["id"] == ids[0]
    for _ in range(3):
        first = forum_service.list_posts(db, page_size=2, sort="newest")
        second = forum_service.list_posts(db, page=2, page_size=2, sort="newest")
        assert [p["id"] for p in first["items"] + second["items"]] == ids[:0:-1]
    with pytest.raises(ValidationError):
        forum_service.list_posts(db, sort="unregistered")


def test_model_discovery_uses_same_api_sort_enum():
    from fastapi import FastAPI

    from app.api.v1.forum import router

    app = FastAPI()
    app.include_router(router, prefix="/api/forum")
    contract = operation_contract(CAPABILITY_BY_CODE["forum.posts.list"], app.openapi())
    assert contract.schema["properties"]["sort"]["enum"] == ["default", "newest"]


@pytest.mark.parametrize("round_index", [1, 2, 3])
@pytest.mark.parametrize("sort", ["default", "newest"])
def test_forum_authoritative_routes_exclude_hidden_posts_and_get_stays_readonly(db, sort, round_index):
    actor = User(username=f"forum-route-{sort}-{round_index}", password="unused", role="user", status=1)
    db.add(actor)
    db.flush()
    when = datetime(2026, 10, 14)
    visible = [ForumPost(user_id=actor.id, title=f"精准命中{i}", content="正文", category="tech",
                         status="normal", is_pinned=(i == 0),
                         create_time=when - timedelta(days=1) if i == 0 else when)
               for i in range(4)]
    unavailable = [ForumPost(user_id=actor.id, title="精准命中不可见", content="不可见正文", category="tech",
                            status=status, is_pinned=True, create_time=when + timedelta(days=1))
                   for status in ("deleted", "hidden")]
    db.add_all([*visible, *unavailable])
    db.commit()
    listed = forum_service.list_posts(db, keyword="精准命中", category="tech", page_size=2, sort=sort)
    second = forum_service.list_posts(db, keyword="精准命中", category="tech", page=2, page_size=2, sort=sort)
    rows = [*listed["items"], *second["items"]]
    assert listed["total"] == second["total"] == 4
    expected = [visible[0].id, *[post.id for post in reversed(visible[1:])]] if sort == "default" else [post.id for post in reversed(visible)]
    assert [row["id"] for row in rows] == expected
    for row in rows:
        assert row["detail_route"] == f'/forum/{row["id"]}'
        stored = db.get(ForumPost, row["id"])
        # SQLite 读回时间为 naive；两次均从数据库刷新，避免与创建时的 UTC Python 对象比较。
        db.refresh(stored)
        before = (stored.view_count, stored.update_time)
        detail = forum_service.get_post(db, row["id"])
        assert detail["detail_route"] == row["detail_route"]
        db.refresh(stored)
        assert (stored.view_count, stored.update_time) == before
    for post in unavailable:
        with pytest.raises(NotFoundError):
            forum_service.get_post(db, post.id)


def test_forum_response_schemas_preserve_server_detail_route():
    post = {
        "id": 44, "user_id": 8, "author_name": "测试作者", "category": "tech", "title": "真实链接",
        "create_time": datetime(2026, 10, 14), "update_time": datetime(2026, 10, 14),
        "detail_route": "/forum/44", "content": "正文", "replies": [],
    }
    page = Resp[PageOut[PostListItemOut]](data=PageOut[PostListItemOut](items=[post], total=1, page=1, page_size=10, pages=1))
    detail = Resp[PostDetailOut](data=PostDetailOut(**post))
    assert page.model_dump()["data"]["items"][0]["detail_route"] == "/forum/44"
    assert detail.model_dump()["data"]["detail_route"] == "/forum/44"


@pytest.mark.parametrize("round_index", [1, 2, 3])
def test_forum_http_responses_keep_routes_and_reject_hidden_posts(db, round_index):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.api.v1.forum import router
    from app.core.database import get_db
    from app.core.dependencies import get_current_user
    from app.core.error_handlers import register_handlers

    actor = User(username=f"forum-http-{round_index}", password="unused", role="user", status=1)
    db.add(actor)
    db.flush()
    public = ForumPost(user_id=actor.id, title="HTTP公开帖子", content="正文", category="tech", status="normal")
    hidden = ForumPost(user_id=actor.id, title="HTTP不可见帖子", content="不可见正文", category="tech", status="deleted")
    db.add_all([public, hidden])
    db.commit()
    # 固定已有会话连接，让同步 TestClient 线程复用这一份内存 SQLite。
    db.connection()
    app = FastAPI()
    app.include_router(router, prefix="/api/forum")
    register_handlers(app)
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_user] = lambda: actor
    with TestClient(app) as client:
        listing = client.get("/api/forum/posts", params={"sort": "newest", "category": "tech"})
        assert listing.status_code == 200
        rows = listing.json()["data"]["items"]
        assert [(row["id"], row["detail_route"]) for row in rows] == [(public.id, f"/forum/{public.id}")]
        detail = client.get(f"/api/forum/posts/{public.id}")
        assert detail.status_code == 200
        assert detail.json()["data"]["detail_route"] == f"/forum/{public.id}"
        db.refresh(public)
        assert public.view_count == 0
        denied = client.get(f"/api/forum/posts/{hidden.id}")
        assert denied.status_code == 404
        assert "detail_route" not in denied.text
        assert client.get("/api/forum/posts", params={"sort": "unsupported"}).status_code == 400
