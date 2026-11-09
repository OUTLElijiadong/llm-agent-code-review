"""An uncertain write can be retried without duplicating enterprise content."""
import pytest
from test_enterprise_chat_service import orgdb as enterprise_db_fixture

from app.core.exceptions import ConflictError, ForbiddenError
from app.models.enterprise_content import EnterpriseAnnouncement, EnterpriseForumPost, EnterpriseForumReply
from app.models.enterprise_org import EnterpriseMember
from app.services import enterprise_content_service as content

orgdb = enterprise_db_fixture


def test_post_idempotency_survives_edit_and_isolated_by_user_and_enterprise(orgdb):
    first = content.create_post(orgdb, 10, 2, "标题", "正文", request_key="key")
    assert content.create_post(orgdb, 10, 2, "标题", "正文", request_key="key").id == first.id
    with pytest.raises(ConflictError):
        content.create_post(orgdb, 10, 2, "标题", "不同正文", request_key="key")
    content.update_post(orgdb, 10, 2, first.id, "已修改", "不同内容")
    assert content.create_post(orgdb, 10, 2, "标题", "正文", request_key="key").id == first.id
    other_user = content.create_post(orgdb, 10, 3, "标题", "正文", request_key="key")
    other_enterprise = content.create_post(orgdb, 20, 4, "标题", "正文", request_key="key")
    assert len({first.id, other_user.id, other_enterprise.id}) == 3
    assert orgdb.query(EnterpriseForumPost).count() == 3
    membership = orgdb.query(EnterpriseMember).filter_by(enterprise_id=10, user_id=2).one()
    membership.status = "removed"
    orgdb.commit()
    with pytest.raises(ForbiddenError):
        content.create_post(orgdb, 10, 2, "标题", "正文", request_key="key")


def test_reply_and_announcement_idempotency_check_payload_and_current_role(orgdb):
    post = content.create_post(orgdb, 10, 2, "帖子", "正文")
    other_post = content.create_post(orgdb, 10, 2, "另一个帖子", "正文")
    first = content.create_reply(orgdb, 10, 2, post.id, "回复", request_key="key")
    assert content.create_reply(orgdb, 10, 2, post.id, "回复", request_key="key").id == first.id
    with pytest.raises(ConflictError):
        content.create_reply(orgdb, 10, 2, other_post.id, "回复", request_key="key")
    assert orgdb.query(EnterpriseForumReply).count() == 1
    announcement = content.save_announcement(orgdb, 10, 1, "公告", "正文", request_key="key")
    assert content.save_announcement(orgdb, 10, 1, "公告", "正文", request_key="key").id == announcement.id
    with pytest.raises(ConflictError):
        content.save_announcement(orgdb, 10, 1, "公告", "不同正文", request_key="key")
    assert orgdb.query(EnterpriseAnnouncement).count() == 1
    membership = orgdb.query(EnterpriseMember).filter_by(enterprise_id=10, user_id=1).one()
    membership.role = "member"
    orgdb.commit()
    with pytest.raises(ForbiddenError):
        content.save_announcement(orgdb, 10, 1, "公告", "正文", request_key="key")


def test_deleted_retry_cannot_recreate_or_return_deleted_record(orgdb):
    first = content.create_post(orgdb, 10, 2, "标题", "正文", request_key="key")
    content.remove_post(orgdb, 10, 2, first.id)
    with pytest.raises(ConflictError):
        content.create_post(orgdb, 10, 2, "标题", "正文", request_key="key")
    assert orgdb.query(EnterpriseForumPost).count() == 1
