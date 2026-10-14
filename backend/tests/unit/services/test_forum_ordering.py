"""置顶展示与最新查询有明确、稳定的不同排序契约。"""
from datetime import datetime, timedelta

import pytest

from app.core.exceptions import ValidationError
from app.models.forum_post import ForumPost
from app.models.user import User
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
