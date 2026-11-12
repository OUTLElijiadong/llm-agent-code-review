"""MySQL RR: enterprise-chat authority must be current at result publication."""

from concurrent.futures import ThreadPoolExecutor
from threading import Event
from uuid import uuid4

import pytest
import test_fresh_database_chain as chain
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import sessionmaker

from app.core.exceptions import ForbiddenError
from app.models.enterprise_assistant_runtime import EnterpriseAssistantRuntimeState
from app.models.enterprise_chat import EnterpriseAssistantRun, EnterpriseChatMessage, EnterpriseConversation
from app.models.enterprise_org import Enterprise, EnterpriseGroup, EnterpriseGroupMember, EnterpriseMember
from app.models.user import User
from app.services import enterprise_assistant_runtime_service as runtime
from app.services import enterprise_assistant_service, enterprise_chat_service, enterprise_org_service

fresh_chain = chain.fresh_chain
fresh_chain_url = chain.fresh_chain_url


def _seed(engine, *, prior_authority_before_source=False):
    sessions = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    key = uuid4().hex[:12]
    with sessions.begin() as db:
        owner, actor, author = [
            User(username=f"chat-{label}-{key}", password="x", role="user", status=1)
            for label in ("owner", "actor", "author")
        ]
        db.add_all([owner, actor, author])
        db.flush()
        company = Enterprise(
            name=f"chat-{key}", owner_id=owner.id, seat_limit=3, paid_order_id=f"chat-{key}", status="active"
        )
        db.add(company)
        db.flush()
        group = EnterpriseGroup(enterprise_id=company.id, name="group", status="active")
        db.add(group)
        db.flush()
        db.add_all(
            [
                EnterpriseMember(enterprise_id=company.id, user_id=owner.id, role="owner", status="active"),
                EnterpriseMember(enterprise_id=company.id, user_id=actor.id, role="member", status="active"),
                EnterpriseMember(enterprise_id=company.id, user_id=author.id, role="admin", status="active"),
                EnterpriseGroupMember(
                    enterprise_id=company.id, group_id=group.id, user_id=actor.id, role="member", status="active"
                ),
                EnterpriseGroupMember(
                    enterprise_id=company.id, group_id=group.id, user_id=author.id, role="leader", status="active"
                ),
            ]
        )
        db.flush()
        room = EnterpriseConversation(
            enterprise_id=company.id,
            kind="group",
            conversation_key=f"group:{group.id}",
            group_id=group.id,
            created_by=owner.id,
        )
        db.add(room)
        db.flush()
        source = EnterpriseChatMessage(
            enterprise_id=company.id,
            conversation_id=room.id,
            sender_id=actor.id,
            sender_type="human",
            client_message_id="source",
            content="@小菱 回答",
            sender_authority_rank=1,
        )
        earlier = EnterpriseChatMessage(
            enterprise_id=company.id,
            conversation_id=room.id,
            sender_id=author.id,
            sender_type="human",
            client_message_id="prior",
            content="@小菱 原管理员指令" if prior_authority_before_source else "原管理员指令",
            sender_authority_rank=3 if prior_authority_before_source else None,
        )
        if prior_authority_before_source:
            db.add(earlier)
            db.flush()
            db.add(source)
            snapshot_messages = [earlier, source]
        else:
            db.add_all([source, earlier])
            snapshot_messages = [source, earlier]
        db.flush()
        snapshot = runtime._role_snapshot(
            db, enterprise_id=company.id, room=room, messages=snapshot_messages, initiator_id=actor.id
        )
        run = EnterpriseAssistantRun(
            public_id=f"chat-{key}",
            request_key=f"chat-{key}",
            enterprise_id=company.id,
            conversation_id=room.id,
            group_id=group.id,
            initiator_user_id=actor.id,
            source_message_id=source.id,
            status="running",
            attempt_count=1,
            model_name="test",
            prompt_sha256="a" * 64,
        )
        db.add(run)
        db.flush()
        checkpoint = runtime.RunCheckpoint(
            run_id=run.public_id,
            model="test",
            transcript=[],
            tools=[],
            status="completed",
            output_text="不得发布的旧答案",
        )
        db.add(
            EnterpriseAssistantRuntimeState(
                assistant_run_id=run.id,
                enterprise_id=company.id,
                conversation_id=room.id,
                group_id=group.id,
                initiator_user_id=actor.id,
                source_message_id=source.id,
                status="completed",
                checkpoint_json=runtime._json(checkpoint.to_dict()),
                permission_snapshot_json=runtime._json(snapshot),
                permission_snapshot_sha256=runtime._digest(snapshot),
                lease_generation=1,
                billing_invocation_count=1,
                version=1,
            )
        )
        return sessions, company.id, group.id, owner.id, author.id, room.id, run.public_id


def _change(db, kind, enterprise_id, group_id, owner_id, author_id):
    if kind == "group":
        enterprise_org_service.remove_group_member(db, enterprise_id, group_id, owner_id, author_id)
    elif kind == "member":
        enterprise_org_service.remove_enterprise_member(db, enterprise_id, owner_id, author_id)
    elif kind == "role":
        enterprise_org_service.set_enterprise_role(db, enterprise_id, owner_id, author_id, "member")
    else:
        db.query(User).filter_by(id=author_id).one().status = 0
        db.flush()


def test_mysql_directive_snapshot_is_required_and_message_is_immutable(fresh_chain):
    sessions, enterprise_id, _group_id, _owner_id, author_id, room_id, _run_id = _seed(fresh_chain)
    with sessions.begin() as db:
        directive = enterprise_chat_service.send_message(
            db, enterprise_id, author_id, room_id, "immutable-directive", "@小菱 请暂停并核对",
        )
        directive_id = int(directive.id)
        assert directive.sender_authority_rank == 3
    with sessions() as db:
        directive = db.get(EnterpriseChatMessage, directive_id)
        params = {
            "id": directive_id,
            "enterprise_id": enterprise_id,
            "conversation_id": room_id,
            "sender_id": author_id,
            "client_message_id": directive.client_message_id,
            "content": directive.content,
            "rank": directive.sender_authority_rank,
        }

    invalid_writes = [
        (
            "INSERT INTO enterprise_chat_message "
            "(enterprise_id, conversation_id, sender_id, sender_type, client_message_id, content, "
            "create_time, update_time) VALUES (:enterprise_id, :conversation_id, :sender_id, 'human', "
            "'missing-rank', '@小菱 缺少发送时快照', UTC_TIMESTAMP(), UTC_TIMESTAMP())",
            params,
        ),
        (
            "UPDATE enterprise_chat_message SET content='普通消息' WHERE id=:id",
            params,
        ),
        (
            "UPDATE enterprise_chat_message SET sender_authority_rank=2 WHERE id=:id",
            params,
        ),
        (
            "DELETE FROM enterprise_chat_message WHERE id=:id",
            params,
        ),
        (
            "REPLACE INTO enterprise_chat_message "
            "(id, enterprise_id, conversation_id, sender_id, sender_type, client_message_id, content, "
            "sender_authority_rank, create_time, update_time) VALUES (:id, :enterprise_id, :conversation_id, "
            ":sender_id, 'human', :client_message_id, '@小菱 覆盖消息', :rank, UTC_TIMESTAMP(), UTC_TIMESTAMP())",
            params,
        ),
    ]
    for statement, values in invalid_writes:
        with pytest.raises(DBAPIError):
            with fresh_chain.begin() as connection:
                connection.execute(text(statement), values)

    with sessions() as db:
        persisted = db.get(EnterpriseChatMessage, directive_id)
        assert persisted.content == "@小菱 请暂停并核对"
        assert persisted.sender_authority_rank == 3


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("content", "@小菱 请暂停 abc"),
        ("client_message_id", "IMMUTABLE-DIRECTIVE"),
    ],
)
def test_mysql_directive_identity_uses_byte_comparison(fresh_chain, field, value):
    sessions, enterprise_id, _group_id, _owner_id, author_id, room_id, _run_id = _seed(fresh_chain)
    with sessions.begin() as db:
        directive = enterprise_chat_service.send_message(
            db, enterprise_id, author_id, room_id, "immutable-directive", "@小菱 请暂停 ABC",
        )
        directive_id = int(directive.id)

    with pytest.raises(DBAPIError):
        with fresh_chain.begin() as connection:
            connection.execute(
                text(f"UPDATE enterprise_chat_message SET {field}=:value WHERE id=:id"),
                {"id": directive_id, "value": value},
            )


@pytest.mark.parametrize("kind", ["group", "member", "role", "disabled"])
def test_revoked_message_author_cannot_publish_from_old_rr_snapshot(fresh_chain, monkeypatch, kind):
    sessions, enterprise_id, group_id, owner_id, author_id, room_id, public_id = _seed(fresh_chain)
    monkeypatch.setattr(enterprise_assistant_service, "_require_subscription", lambda *_a, **_k: None)
    monkeypatch.setattr(
        "app.services.billing_checkout_service.enterprise_subscription_status", lambda *_a, **_k: "active"
    )
    monkeypatch.setattr(
        "app.services.billing_checkout_service.enterprise_subscription_refund_pending", lambda *_a, **_k: False
    )
    paused, proceed = Event(), Event()

    def reservations(db, _run):
        # Match the ordinary reservation SELECT which starts the RR snapshot.
        db.query(EnterpriseMember).filter_by(enterprise_id=enterprise_id).count()
        paused.set()
        assert proceed.wait(15)
        return [type("Reservation", (), {"status": "settled"})()]

    monkeypatch.setattr(runtime, "_reservations_for_run", reservations)

    def publish():
        try:
            runtime._finalize_runtime(
                sessions, enterprise_id=enterprise_id, conversation_id=room_id, run_public_id=public_id
            )
            return "published"
        except ForbiddenError:
            return "suppressed"

    with ThreadPoolExecutor(max_workers=1) as pool:
        result = pool.submit(publish)
        assert paused.wait(15)
        try:
            with sessions.begin() as db:
                _change(db, kind, enterprise_id, group_id, owner_id, author_id)
        finally:
            proceed.set()
        assert result.result(timeout=20) == "suppressed"
    with sessions() as db:
        run = db.query(EnterpriseAssistantRun).filter_by(public_id=public_id).one()
        assert run.status == "suppressed"
        assert run.response_text is None


@pytest.mark.parametrize("kind", ["group", "member", "role", "disabled"])
def test_publication_wins_before_later_authority_change(fresh_chain, monkeypatch, kind):
    from time import monotonic, sleep

    from sqlalchemy import text

    sessions, enterprise_id, group_id, owner_id, author_id, room_id, public_id = _seed(fresh_chain)
    monkeypatch.setattr(enterprise_assistant_service, "_require_subscription", lambda *_a, **_k: None)
    monkeypatch.setattr(
        "app.services.billing_checkout_service.enterprise_subscription_status", lambda *_a, **_k: "active"
    )
    monkeypatch.setattr(
        "app.services.billing_checkout_service.enterprise_subscription_refund_pending", lambda *_a, **_k: False
    )
    monkeypatch.setattr(
        runtime, "_reservations_for_run", lambda *_a: [type("Reservation", (), {"status": "settled"})()]
    )
    locked, proceed, mutating = Event(), Event(), Event()
    connection_id = []
    real_ensure_message = runtime._ensure_message

    def hold_publication(*args, **kwargs):
        locked.set()
        assert proceed.wait(15)
        return real_ensure_message(*args, **kwargs)

    monkeypatch.setattr(runtime, "_ensure_message", hold_publication)

    def publish():
        return runtime._finalize_runtime(
            sessions, enterprise_id=enterprise_id, conversation_id=room_id, run_public_id=public_id
        )["status"]

    def revoke():
        with sessions.begin() as db:
            connection_id.append(db.execute(text("SELECT CONNECTION_ID()")).scalar_one())
            mutating.set()
            _change(db, kind, enterprise_id, group_id, owner_id, author_id)
        return "changed"

    with ThreadPoolExecutor(max_workers=2) as pool:
        published = pool.submit(publish)
        assert locked.wait(15)
        changed = pool.submit(revoke)
        try:
            assert mutating.wait(15)
            deadline = monotonic() + 10
            waiting = False
            while monotonic() < deadline:
                with sessions() as observer:
                    waiting = bool(
                        observer.execute(
                            text(
                                "SELECT COUNT(*) FROM performance_schema.data_lock_waits w "
                                "JOIN performance_schema.threads t ON t.THREAD_ID=w.REQUESTING_THREAD_ID "
                                "WHERE t.PROCESSLIST_ID=:id"
                            ),
                            {"id": connection_id[0]},
                        ).scalar()
                    )
                if waiting:
                    break
                sleep(0.05)
            if changed.done():
                changed.result()
            assert waiting, "未观察到权限变更事务等待发布事务持有的数据库锁"
        finally:
            proceed.set()
        assert published.result(timeout=20) == "completed"
        assert changed.result(timeout=20) == "changed"


@pytest.mark.parametrize("kind", ["group", "member", "role", "disabled"])
def test_revoked_message_author_blocks_stale_tool_executor(fresh_chain, monkeypatch, kind):
    sessions, enterprise_id, group_id, owner_id, author_id, room_id, public_id = _seed(fresh_chain)
    monkeypatch.setattr(
        "app.services.billing_checkout_service.enterprise_subscription_status", lambda *_a, **_k: "active"
    )
    monkeypatch.setattr(
        "app.services.billing_checkout_service.enterprise_subscription_refund_pending", lambda *_a, **_k: False
    )
    with sessions.begin() as db:
        run = db.query(EnterpriseAssistantRun).filter_by(public_id=public_id).one()
        state = db.query(EnterpriseAssistantRuntimeState).filter_by(assistant_run_id=run.id).one()
        state.status = runtime.RUNNING
        initiator_id = int(run.initiator_user_id)
    with sessions() as stale:
        # Existing RR transaction established before the author loses authority.
        stale.query(EnterpriseMember).filter_by(enterprise_id=enterprise_id).count()
        with sessions.begin() as db:
            _change(db, kind, enterprise_id, group_id, owner_id, author_id)
        executor = runtime.EnterpriseAssistantToolExecutor(
            stale,
            enterprise_id=enterprise_id,
            conversation_id=room_id,
            run_public_id=public_id,
            lease_generation=1,
            actor_user_id=initiator_id,
        )
        with pytest.raises(ForbiddenError, match="会话成员角色已变化"):
            executor._current()
