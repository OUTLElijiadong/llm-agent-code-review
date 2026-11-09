"""企业小菱的运行提示只注入当前企业命中的附件片段。"""

from types import SimpleNamespace

from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.core.database import Base
from app.models.user import User
from app.services import enterprise_assistant_runtime_service as runtime


def test_runtime_context_includes_only_attachment_search_hits(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine, tables=[User.__table__])
    with Session(engine) as db:
        db.add(User(id=2, username="成员甲", password="x", email="member@example.invalid", role="user", status=1))
        db.commit()
        messages = [SimpleNamespace(id=3, sender_type="human", sender_id=2, content="@小菱 变更需要审批吗？")]
        monkeypatch.setattr(runtime.enterprise_chat_service, "list_messages", lambda *_args, **_kwargs: messages)
        monkeypatch.setattr(runtime.enterprise_knowledge_service, "search_entries", lambda *_args, **_kwargs: [
            SimpleNamespace(title="发布规则", content="生产变更需要审批。"),
        ])
        lookups = []
        monkeypatch.setattr(runtime.enterprise_knowledge_attachment_service, "search_attachments",
                            lambda _db, enterprise_id, user_id, query, **kwargs: (
                                lookups.append((enterprise_id, user_id, query, kwargs)) or [{
                                    "attachment_id": 5, "file_name": "运维手册.pdf",
                                    "version": 1,
                                    "excerpt": "高风险变更需双人复核。",
                                    "search_mode": "local_hash_vector_and_terms",
                                }]
                            ))
        prompt, references = runtime._render_runtime_context(
            db, enterprise_id=10, user_id=2, room=SimpleNamespace(id=20, kind="company", group_id=None),
            source=messages[0], snapshot={"messages": [{
                "message_id": 3, "sender_id": 2, "username": "成员甲", "member_role": "member", "rank": 1,
            }]},
        )
    assert "生产变更需要审批。" in prompt
    assert "运维手册.pdf" in prompt and "高风险变更需双人复核。" in prompt
    assert references == [{"attachment_id": 5, "version": 1}]
    assert lookups == [(10, 2, "变更需要审批吗？", {"limit": 3})]
    engine.dispose()


def test_long_context_retains_authority_rules_and_complete_recent_message(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine, tables=[User.__table__])
    with Session(engine) as db:
        db.add(User(id=2, username="成员甲", password="x", email="member@example.invalid", role="user", status=1))
        db.commit()
        messages = [SimpleNamespace(id=i, sender_type="human", sender_id=2, content="长" * 1800)
                    for i in range(1, 13)]
        messages[-1].content = "@小菱 " + "问题" * 2000
        monkeypatch.setattr(runtime.enterprise_chat_service, "list_messages", lambda *_args, **_kwargs: messages)
        monkeypatch.setattr(runtime.enterprise_knowledge_service, "search_entries", lambda *_args, **_kwargs: [
            SimpleNamespace(title="知识", content="知" * 2200) for _ in range(3)
        ])
        monkeypatch.setattr(runtime.enterprise_knowledge_attachment_service, "search_attachments",
                            lambda *_args, **_kwargs: [])
        snapshot = {"messages": [
            {"message_id": m.id, "sender_id": 2, "username": "成员甲", "member_role": "member", "rank": 1}
            for m in messages
        ]}
        prompt, _ = runtime._render_runtime_context(
            db, enterprise_id=10, user_id=2, room=SimpleNamespace(id=20, kind="company", group_id=None),
            source=messages[-1], snapshot=snapshot,
        )
    assert len(prompt) <= runtime.assistant_service._MAX_CONTEXT_CHARS
    assert "【服务端角色优先级】" in prompt
    assert "企业所有者 > 企业管理员 > 当前小组组长" in prompt
    assert "【当前发起消息】" in prompt
    assert "【消息 12 · 成员甲（当前角色 member，有效优先级 1）】" in prompt
    engine.dispose()
