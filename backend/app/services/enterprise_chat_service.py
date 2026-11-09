"""企业会话与消息：每次读取、写入都检查当前企业及会话成员资格。

该服务仅提供员工消息存储，不调用模型，也不将企业会话映射到个人小菱会话。
"""
from sqlalchemy import or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.exceptions import ConflictError, ForbiddenError, NotFoundError, ValidationError
from app.models.enterprise_chat import EnterpriseChatMessage, EnterpriseConversation
from app.models.enterprise_org import Enterprise, EnterpriseGroup, EnterpriseGroupMember, EnterpriseMember
from app.models.user import User
from app.services.enterprise_org_service import require_enterprise_member, require_group_member


def _lock_enterprise(db: Session, enterprise_id: int) -> Enterprise:
    row = db.query(Enterprise).filter_by(id=enterprise_id).populate_existing().with_for_update().first()
    if not row or row.status not in {"active", "suspended"}:
        raise NotFoundError("企业不存在")
    return row


def _require_locked_member(db: Session, enterprise_id: int, user_id: int, *,
                           expected_token_version: int | None = None) -> EnterpriseMember:
    # 锁定读读取最新已提交值，而不沿用 MySQL REPEATABLE READ 的旧认证快照。
    row = db.query(EnterpriseMember).filter_by(
        enterprise_id=enterprise_id, user_id=user_id, status="active"
    ).populate_existing().with_for_update().first()
    user = db.query(User).filter_by(id=user_id, status=1).populate_existing().with_for_update().first()
    if row is None or user is None:
        raise ForbiddenError("没有该企业的访问权限")
    if expected_token_version is not None and (
        type(expected_token_version) is not int or int(user.token_version or 0) != expected_token_version
    ):
        raise ForbiddenError("登录状态已变化，请重新登录")
    return row


def _require_locked_group(db: Session, enterprise_id: int, group_id: int, user_id: int) -> None:
    group = db.query(EnterpriseGroup).filter_by(
        id=group_id, enterprise_id=enterprise_id, status="active"
    ).populate_existing().with_for_update().first()
    member = db.query(EnterpriseGroupMember).filter_by(
        enterprise_id=enterprise_id, group_id=group_id, user_id=user_id, status="active"
    ).populate_existing().with_for_update().first()
    if group is None or member is None:
        raise ForbiddenError("没有该小组的访问权限")


def _check_access(
    db: Session,
    enterprise_id: int,
    user_id: int,
    conversation: EnterpriseConversation,
    *,
    require_billing_access: bool = True,
) -> None:
    # 禁止全局管理员豁免；即便知道会话 ID 也必须仍是该企业的有效成员。
    require_enterprise_member(
        db, enterprise_id, user_id, require_billing_access=require_billing_access,
    )
    if conversation.enterprise_id != enterprise_id:
        raise NotFoundError("会话不存在")
    if conversation.kind == "group":
        if conversation.group_id is None:
            raise ForbiddenError("会话成员关系无效")
        require_group_member(
            db,
            enterprise_id,
            int(conversation.group_id),
            user_id,
            allow_admin=False,
            require_billing_access=require_billing_access,
        )
    elif conversation.kind == "direct":
        if user_id not in (conversation.participant_low_id, conversation.participant_high_id):
            raise ForbiddenError("无权访问此私聊")
    elif conversation.kind != "company":
        raise ForbiddenError("会话类型无效")


def get_conversation(
    db: Session,
    enterprise_id: int,
    user_id: int,
    conversation_id: int,
    *,
    require_billing_access: bool = True,
    lock: bool = False,
    shared_lock: bool = False,
) -> EnterpriseConversation:
    # 查询时就限定组织，避免凭全局自增 ID 访问跨租户资源。
    require_enterprise_member(
        db, enterprise_id, user_id, require_billing_access=require_billing_access,
    )
    query = db.query(EnterpriseConversation).filter_by(id=conversation_id, enterprise_id=enterprise_id)
    row = (query.populate_existing().with_for_update(read=shared_lock) if lock else query).first()
    if row is None:
        raise NotFoundError("会话不存在")
    _check_access(db, enterprise_id, user_id, row, require_billing_access=require_billing_access)
    return row


def open_conversation(
    db: Session, enterprise_id: int, user_id: int, kind: str, *, group_id: int | None = None,
    other_user_id: int | None = None,
) -> EnterpriseConversation:
    # 与成员变更使用相同的企业行锁，避免撤权和新建会话交错。
    _lock_enterprise(db, enterprise_id)
    _require_locked_member(db, enterprise_id, user_id)
    require_enterprise_member(db, enterprise_id, user_id)
    if kind == "company" and group_id is None and other_user_id is None:
        key, low, high = "company", None, None
    elif kind == "group" and group_id is not None and other_user_id is None:
        _require_locked_group(db, enterprise_id, group_id, user_id)
        require_group_member(db, enterprise_id, group_id, user_id, allow_admin=False)
        key, low, high = f"group:{group_id}", None, None
    elif kind == "direct" and group_id is None and other_user_id is not None and other_user_id != user_id:
        _require_locked_member(db, enterprise_id, other_user_id)
        require_enterprise_member(db, enterprise_id, other_user_id)
        low, high = sorted((user_id, other_user_id))
        key = f"direct:{low}:{high}"
    else:
        raise ValidationError("会话类型与参与者参数不匹配")
    row = db.query(EnterpriseConversation).filter_by(enterprise_id=enterprise_id, conversation_key=key).first()
    if row is None:
        try:
            with db.begin_nested():
                row = EnterpriseConversation(
                    enterprise_id=enterprise_id, kind=kind, conversation_key=key, group_id=group_id,
                    participant_low_id=low, participant_high_id=high, created_by=user_id,
                )
                db.add(row)
                db.flush()
        except IntegrityError:
            # 并发创建同一群或相同两人的私聊，只保留一个会话。
            row = db.query(EnterpriseConversation).filter_by(enterprise_id=enterprise_id, conversation_key=key).one()
    _check_access(db, enterprise_id, user_id, row)
    db.commit()
    return row


def list_conversations(db: Session, enterprise_id: int, user_id: int) -> list[EnterpriseConversation]:
    require_enterprise_member(db, enterprise_id, user_id)
    candidates = db.query(EnterpriseConversation).filter(
        EnterpriseConversation.enterprise_id == enterprise_id,
        or_(EnterpriseConversation.kind != "direct", EnterpriseConversation.participant_low_id == user_id,
            EnterpriseConversation.participant_high_id == user_id),
    ).order_by(EnterpriseConversation.id.asc()).all()
    visible = []
    for row in candidates:
        try:
            _check_access(db, enterprise_id, user_id, row)
        except (ForbiddenError, NotFoundError):
            continue
        visible.append(row)
    return visible


def list_messages(db: Session, enterprise_id: int, user_id: int, conversation_id: int, *, after_id: int | None = None,
                  before_id: int | None = None, limit: int = 50) -> list[EnterpriseChatMessage]:
    get_conversation(db, enterprise_id, user_id, conversation_id)
    if before_id is not None and after_id is not None:
        raise ValidationError("不能同时使用前后消息游标")
    query = db.query(EnterpriseChatMessage).filter(
        EnterpriseChatMessage.enterprise_id == enterprise_id,
        EnterpriseChatMessage.conversation_id == conversation_id,
    )
    if after_id is not None:
        return query.filter(EnterpriseChatMessage.id > after_id).order_by(
            EnterpriseChatMessage.id.asc()
        ).limit(limit).all()
    if before_id is not None:
        query = query.filter(EnterpriseChatMessage.id < before_id)
    # 先倒序取最新一页，再按时间顺序返回，避免旧消息过多时看不到最新内容。
    return list(reversed(query.order_by(EnterpriseChatMessage.id.desc()).limit(limit).all()))


def send_message(
    db: Session,
    enterprise_id: int,
    user_id: int,
    conversation_id: int,
    client_message_id: str,
    content: str,
    *,
    expected_token_version: int | None = None,
) -> EnterpriseChatMessage:
    # 组织服务在成员或小组变更时也锁企业行；发消息与撤权串行化。
    company = _lock_enterprise(db, enterprise_id)
    _require_locked_member(db, enterprise_id, user_id, expected_token_version=expected_token_version)
    room = get_conversation(db, enterprise_id, user_id, conversation_id)
    if room.kind == "group":
        _require_locked_group(db, enterprise_id, int(room.group_id), user_id)
    if not content.strip() or len(content) > 10000 or not client_message_id.strip() or len(client_message_id) > 80:
        raise ValidationError("消息内容或客户端消息编号无效")
    row = db.query(EnterpriseChatMessage).filter_by(
        conversation_id=conversation_id, sender_id=user_id, client_message_id=client_message_id,
    ).first()
    is_new = row is None
    if row is None:
        try:
            with db.begin_nested():
                row = EnterpriseChatMessage(
                    enterprise_id=enterprise_id, conversation_id=conversation_id, sender_id=user_id,
                    client_message_id=client_message_id, content=content,
                )
                db.add(row)
                db.flush()
        except IntegrityError:
            row = db.query(EnterpriseChatMessage).filter_by(
                conversation_id=conversation_id, sender_id=user_id, client_message_id=client_message_id,
            ).one()
            is_new = False
    if row.enterprise_id != enterprise_id or row.content != content:
        raise ConflictError("客户端消息编号已用于其他内容")
    if is_new and company.proactive_enabled:
        from app.services.enterprise_assistant_proactive_service import record_message_event

        record_message_event(db, company, room, row)
    db.commit()
    return row
