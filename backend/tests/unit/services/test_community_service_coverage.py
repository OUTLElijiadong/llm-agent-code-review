"""社区服务覆盖率补全测试。

覆盖开发者论坛、维修工单和用户反馈服务中的正常流程、输入边界、
用户隔离、管理员权限以及资源不存在分支。数据库统一使用内存 SQLite。
"""
from __future__ import annotations

import pytest

from app.core.exceptions import ForbiddenError, NotFoundError, ValidationError
from app.models.user import User
from app.services import forum_service, maintenance_service, user_feedback_service


def _make_user(
    db,
    username: str,
    *,
    role: str = "user",
    nickname: str | None = None,
) -> User:
    """创建并持久化社区服务测试用户。

    Args:
        db: 测试数据库会话。
        username: 唯一用户名。
        role: 用户角色，默认为普通用户。
        nickname: 可选昵称；未传入时保留为空以验证用户名兜底。

    Returns:
        User: 已持久化并刷新主键的用户。
    """
    user = User(
        username=username,
        password="stored-hash",
        email=f"{username}@example.com",
        nickname=nickname,
        role=role,
        status=1,
        token_version=0,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def test_forum_post_lifecycle_filters_authors_and_admin_pin(db):
    """帖子创建、筛选、查看、编辑和置顶应遵守数据与权限规则。

    Args:
        db: 测试数据库会话。

    Returns:
        None: 断言帖子生命周期、作者名称和管理员置顶结果。
    """
    author = _make_user(db, "forum-author")
    admin = _make_user(db, "forum-admin", role="admin", nickname="论坛管理员")
    outsider = _make_user(db, "forum-outsider", nickname="其他用户")

    assert forum_service._author_names(db, []) == {}
    with pytest.raises(ValidationError):
        forum_service.create_post(db, author, {"title": " ", "content": "正文"})

    post = forum_service.create_post(
        db,
        author,
        {
            "title": f"  {'A' * 205}  ",
            "content": "  原始正文  ",
            "category": "invalid",
        },
    )
    pinned = forum_service.create_post(
        db,
        admin,
        {"title": "Pinned 技术分享", "content": "管理员正文", "category": "tech"},
    )
    assert len(post.title) == 200
    assert post.content == "原始正文"
    assert post.category == "qa"

    with pytest.raises(ForbiddenError):
        forum_service.pin_post(db, outsider, pinned.id, True)
    pinned_data = forum_service.pin_post(db, admin, pinned.id, True)
    assert pinned_data["is_pinned"] is True
    assert pinned_data["replies"] == []

    listed = forum_service.list_posts(db, page=1, page_size=10)
    assert listed["total"] == 2
    assert listed["items"][0]["id"] == pinned.id
    assert listed["items"][1]["author_name"] == "forum-author"
    filtered = forum_service.list_posts(db, keyword="Pinned", category="tech")
    assert [item["id"] for item in filtered["items"]] == [pinned.id]

    without_replies = forum_service.get_post(db, post.id, with_replies=False)
    assert without_replies["content"] == "原始正文"
    assert without_replies["replies"] == []
    assert without_replies["view_count"] == 1

    with pytest.raises(ForbiddenError):
        forum_service.update_post(db, outsider, post.id, {"title": "越权编辑"})
    updated = forum_service.update_post(
        db,
        author,
        post.id,
        {"title": "  新标题  ", "content": "  新正文  ", "category": "share"},
    )
    assert updated["title"] == "新标题"
    assert updated["content"] == "新正文"
    assert updated["category"] == "share"

    admin_updated = forum_service.update_post(
        db,
        admin,
        post.id,
        {"title": "管理员修订", "category": "not-allowed"},
    )
    assert admin_updated["title"] == "管理员修订"
    assert admin_updated["category"] == "share"


def test_forum_reply_and_delete_permissions_cover_missing_resources(db):
    """回复计数和删除操作应限制作者或管理员并处理缺失资源。

    Args:
        db: 测试数据库会话。

    Returns:
        None: 断言回复增减、软删除、越权和重复删除分支。
    """
    author = _make_user(db, "reply-author", nickname="作者")
    replier = _make_user(db, "reply-user", nickname="回复者")
    outsider = _make_user(db, "reply-outsider")
    admin = _make_user(db, "reply-admin", role="admin")
    post = forum_service.create_post(
        db,
        author,
        {"title": "回复测试", "content": "帖子正文", "category": "qa"},
    )

    with pytest.raises(ValidationError):
        forum_service.create_reply(db, replier, post.id, "  ")
    with pytest.raises(NotFoundError):
        forum_service.create_reply(db, replier, 999999, "找不到帖子")

    first = forum_service.create_reply(db, replier, post.id, "  第一条回复  ")
    second = forum_service.create_reply(db, author, post.id, "第二条回复")
    detail = forum_service.get_post(db, post.id)
    assert [item["content"] for item in detail["replies"]] == ["第一条回复", "第二条回复"]
    assert detail["replies"][0]["author_name"] == "回复者"
    assert detail["reply_count"] == 2

    with pytest.raises(ForbiddenError):
        forum_service.delete_reply(db, outsider, first.id)
    forum_service.delete_reply(db, replier, first.id)
    db.refresh(post)
    assert post.reply_count == 1
    with pytest.raises(NotFoundError):
        forum_service.delete_reply(db, replier, first.id)

    forum_service.delete_reply(db, admin, second.id)
    db.refresh(post)
    assert post.reply_count == 0

    with pytest.raises(ForbiddenError):
        forum_service.delete_post(db, outsider, post.id)
    forum_service.delete_post(db, author, post.id)
    assert forum_service.list_posts(db)["total"] == 0
    with pytest.raises(NotFoundError):
        forum_service.get_post(db, post.id)
    with pytest.raises(NotFoundError):
        forum_service.update_post(db, author, post.id, {"title": "已删除"})
    with pytest.raises(NotFoundError):
        forum_service.delete_post(db, author, post.id)
    with pytest.raises(NotFoundError):
        forum_service.pin_post(db, admin, post.id, True)
    with pytest.raises(NotFoundError):
        forum_service.delete_reply(db, admin, 999999)


def test_maintenance_create_list_and_view_enforce_user_isolation(db):
    """工单创建与列表查看应校验输入并隔离普通用户数据。

    Args:
        db: 测试数据库会话。

    Returns:
        None: 断言默认值、分页筛选和本人/管理员可见范围。
    """
    owner = _make_user(db, "ticket-owner")
    other = _make_user(db, "ticket-other")
    admin = _make_user(db, "ticket-admin", role="admin")

    with pytest.raises(ValidationError):
        maintenance_service.create_ticket(db, owner, {"title": "", "description": "描述"})

    owner_ticket = maintenance_service.create_ticket(
        db,
        owner,
        {
            "title": f" {'故' * 160} ",
            "description": "  页面无法打开  ",
            "category": "unknown",
            "priority": "urgent",
        },
    )
    other_ticket = maintenance_service.create_ticket(
        db,
        other,
        {
            "title": "账号异常",
            "description": "无法登录",
            "category": "account",
            "priority": "high",
        },
    )
    admin_ticket = maintenance_service.create_ticket(
        db,
        admin,
        {"title": "后台优化", "description": "管理员自己的工单", "category": "feature"},
    )
    assert len(owner_ticket.title) == 150
    assert owner_ticket.description == "页面无法打开"
    assert owner_ticket.category == "bug"
    assert owner_ticket.priority == "medium"

    owner_rows = maintenance_service.list_tickets(db, owner, mine=False)
    assert [item["id"] for item in owner_rows["items"]] == [owner_ticket.id]
    admin_all = maintenance_service.list_tickets(db, admin, mine=False, status="pending")
    assert admin_all["total"] == 3
    admin_mine = maintenance_service.list_tickets(db, admin, mine=True)
    assert [item["id"] for item in admin_mine["items"]] == [admin_ticket.id]

    assert maintenance_service.get_ticket(db, owner, owner_ticket.id)["description"] == "页面无法打开"
    assert maintenance_service.get_ticket(db, admin, other_ticket.id)["user_id"] == other.id
    with pytest.raises(ForbiddenError):
        maintenance_service.get_ticket(db, owner, other_ticket.id)
    with pytest.raises(NotFoundError):
        maintenance_service.get_ticket(db, owner, 999999)


def test_maintenance_admin_flow_close_and_stats_cover_errors(db):
    """管理员处理、用户关闭和统计应校验角色、状态与资源存在性。

    Args:
        db: 测试数据库会话。

    Returns:
        None: 断言受理副作用、非法状态、关闭权限和状态统计。
    """
    owner = _make_user(db, "handle-owner")
    other = _make_user(db, "handle-other")
    admin = _make_user(db, "handle-admin", role="admin")
    ticket = maintenance_service.create_ticket(
        db,
        owner,
        {"title": "性能问题", "description": "接口响应缓慢", "category": "performance"},
    )
    other_ticket = maintenance_service.create_ticket(
        db,
        other,
        {"title": "功能建议", "description": "增加导出", "category": "feature"},
    )

    with pytest.raises(ForbiddenError):
        maintenance_service.handle_ticket(db, owner, ticket.id, {"status": "processing"})
    with pytest.raises(NotFoundError):
        maintenance_service.handle_ticket(db, admin, 999999, {})
    with pytest.raises(ValidationError):
        maintenance_service.handle_ticket(db, admin, ticket.id, {"status": "invalid"})

    handled = maintenance_service.handle_ticket(
        db,
        admin,
        ticket.id,
        {"status": "resolved", "admin_reply": "已扩容", "priority": "high"},
    )
    assert handled["status"] == "resolved"
    assert handled["admin_reply"] == "已扩容"
    assert handled["priority"] == "high"
    assert handled["handled_by"] == admin.id
    assert handled["handled_at"] is not None

    with pytest.raises(ForbiddenError):
        maintenance_service.close_own_ticket(db, other, ticket.id)
    closed = maintenance_service.close_own_ticket(db, owner, ticket.id)
    assert closed["status"] == "closed"
    assert maintenance_service.close_own_ticket(db, admin, other_ticket.id)["status"] == "closed"
    with pytest.raises(NotFoundError):
        maintenance_service.close_own_ticket(db, owner, 999999)

    with pytest.raises(ForbiddenError):
        maintenance_service.stats_for_admin(db, owner)
    stats = maintenance_service.stats_for_admin(db, admin)
    assert stats["closed"] == 2
    assert stats["total"] == 2


def test_feedback_create_list_and_admin_read_enforce_user_isolation(db):
    """反馈创建、筛选和管理员阅读应执行默认值与数据隔离规则。

    Args:
        db: 测试数据库会话。

    Returns:
        None: 断言反馈类型、联系方式截断、列表范围与已读状态。
    """
    owner = _make_user(db, "feedback-owner")
    other = _make_user(db, "feedback-other")
    admin = _make_user(db, "feedback-admin", role="admin")

    with pytest.raises(ValidationError):
        user_feedback_service.create_feedback(db, owner, {"content": "  "})

    feedback = user_feedback_service.create_feedback(
        db,
        owner,
        {
            "content": "  希望增加批量导出  ",
            "feedback_type": "unknown",
            "contact": f" {'x' * 110} ",
        },
    )
    other_feedback = user_feedback_service.create_feedback(
        db,
        other,
        {"content": "界面很好用", "feedback_type": "praise"},
    )
    assert feedback.content == "希望增加批量导出"
    assert feedback.feedback_type == "suggestion"
    assert len(feedback.contact) == 100
    assert other_feedback.contact is None

    owner_rows = user_feedback_service.list_feedback(db, owner, mine=False)
    assert [item["id"] for item in owner_rows["items"]] == [feedback.id]
    admin_rows = user_feedback_service.list_feedback(
        db,
        admin,
        mine=False,
        status="new",
        feedback_type="praise",
    )
    assert [item["id"] for item in admin_rows["items"]] == [other_feedback.id]

    assert user_feedback_service.get_feedback(db, owner, feedback.id)["status"] == "new"
    with pytest.raises(ForbiddenError):
        user_feedback_service.get_feedback(db, owner, other_feedback.id)
    with pytest.raises(NotFoundError):
        user_feedback_service.get_feedback(db, owner, 999999)

    opened = user_feedback_service.get_feedback(db, admin, feedback.id)
    assert opened["status"] == "read"
    db.refresh(feedback)
    assert feedback.status == "read"


def test_feedback_reply_and_stats_cover_permissions_and_status_precedence(db):
    """反馈回复和统计应限制管理员并正确处理状态覆盖与异常分支。

    Args:
        db: 测试数据库会话。

    Returns:
        None: 断言回复副作用、合法/非法状态以及管理员汇总。
    """
    owner = _make_user(db, "reply-feedback-owner")
    admin = _make_user(db, "reply-feedback-admin", role="admin")
    first = user_feedback_service.create_feedback(
        db,
        owner,
        {"content": "发现一个问题", "feedback_type": "bug"},
    )
    second = user_feedback_service.create_feedback(
        db,
        owner,
        {"content": "建议增加快捷键", "feedback_type": "suggestion"},
    )

    with pytest.raises(ForbiddenError):
        user_feedback_service.reply_feedback(db, owner, first.id, {"admin_reply": "越权"})
    with pytest.raises(NotFoundError):
        user_feedback_service.reply_feedback(db, admin, 999999, {})

    replied = user_feedback_service.reply_feedback(
        db,
        admin,
        first.id,
        {"admin_reply": "已修复", "status": "closed"},
    )
    assert replied["admin_reply"] == "已修复"
    assert replied["status"] == "closed"
    assert replied["handled_by"] == admin.id
    assert replied["handled_at"] is not None

    invalid_status = user_feedback_service.reply_feedback(
        db,
        admin,
        second.id,
        {"admin_reply": "已记录", "status": "invalid"},
    )
    assert invalid_status["status"] == "replied"

    with pytest.raises(ForbiddenError):
        user_feedback_service.stats_for_admin(db, owner)
    stats = user_feedback_service.stats_for_admin(db, admin)
    assert stats["closed"] == 1
    assert stats["replied"] == 1
    assert stats["total"] == 2
