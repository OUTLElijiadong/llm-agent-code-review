from datetime import datetime, timezone

from app.models.enterprise_chat import EnterpriseAssistantRun, EnterpriseChatMessage
from app.services import enterprise_assistant_runtime_service as runtime


def test_published_answer_closes_messages_received_after_its_placeholder(db):
    before_answer = datetime(2026, 11, 16, 10, 0, tzinfo=timezone.utc)
    during_answer = datetime(2026, 11, 16, 10, 2, tzinfo=timezone.utc)
    answer_published = datetime(2026, 11, 16, 10, 5, tzinfo=timezone.utc)
    next_source_time = datetime(2026, 11, 16, 10, 6, tzinfo=timezone.utc)

    source = EnterpriseChatMessage(
        enterprise_id=1,
        conversation_id=1,
        sender_id=10,
        sender_type="human",
        client_message_id="source",
        content="@小菱 请回答",
        create_time=before_answer,
        update_time=before_answer,
    )
    db.add(source)
    db.flush()

    run = EnterpriseAssistantRun(
        public_id="discussion-turn-regression",
        request_key="assistant:discussion-turn-regression",
        enterprise_id=1,
        conversation_id=1,
        initiator_user_id=10,
        source_message_id=int(source.id),
        status="completed",
        attempt_count=1,
        prompt_sha256="a" * 64,
        response_text="回答已发布",
    )
    db.add(run)
    db.flush()

    placeholder = EnterpriseChatMessage(
        enterprise_id=1,
        conversation_id=1,
        sender_id=None,
        sender_type="assistant",
        assistant_run_id=int(run.id),
        client_message_id=run.public_id,
        content="小菱已收到请求，正在处理。",
        create_time=before_answer,
        update_time=answer_published,
    )
    during = EnterpriseChatMessage(
        enterprise_id=1,
        conversation_id=1,
        sender_id=20,
        sender_type="human",
        client_message_id="during-answer",
        content="请补充另一项要求",
        create_time=during_answer,
        update_time=during_answer,
    )
    next_source = EnterpriseChatMessage(
        enterprise_id=1,
        conversation_id=1,
        sender_id=10,
        sender_type="human",
        client_message_id="next-source",
        content="@小菱 开始新一轮",
        create_time=next_source_time,
        update_time=next_source_time,
    )
    db.add_all([placeholder, during, next_source])
    db.flush()
    placeholder.discussion_closed_through_message_id = int(during.id)
    db.flush()

    messages = runtime._current_discussion_messages(
        db,
        enterprise_id=1,
        conversation_id=1,
        source_message_id=int(next_source.id),
    )

    assert [int(message.id) for message in messages] == [int(next_source.id)]


def test_legacy_answer_fallback_keeps_same_second_messages_unclosed(db):
    earlier = datetime(2026, 11, 16, 10, 59, tzinfo=timezone.utc)
    same_second = datetime(2026, 11, 16, 11, 0, tzinfo=timezone.utc)
    later = datetime(2026, 11, 16, 11, 1, tzinfo=timezone.utc)
    original_source = EnterpriseChatMessage(
        enterprise_id=2,
        conversation_id=2,
        sender_id=10,
        sender_type="human",
        client_message_id="legacy-original-source",
        content="@小菱 原请求",
        create_time=earlier,
        update_time=earlier,
    )
    db.add(original_source)
    db.flush()
    run = EnterpriseAssistantRun(
        public_id="discussion-turn-legacy",
        request_key="assistant:discussion-turn-legacy",
        enterprise_id=2,
        conversation_id=2,
        initiator_user_id=10,
        source_message_id=int(original_source.id),
        status="completed",
        attempt_count=1,
        prompt_sha256="b" * 64,
        response_text="legacy answer",
    )
    db.add(run)
    db.flush()
    placeholder = EnterpriseChatMessage(
        enterprise_id=2,
        conversation_id=2,
        sender_id=None,
        sender_type="assistant",
        assistant_run_id=int(run.id),
        client_message_id=run.public_id,
        content="legacy answer",
        create_time=same_second,
        update_time=same_second,
    )
    ambiguous = EnterpriseChatMessage(
        enterprise_id=2,
        conversation_id=2,
        sender_id=20,
        sender_type="human",
        client_message_id="legacy-same-second",
        content="same-second directive",
        create_time=same_second,
        update_time=same_second,
    )
    source = EnterpriseChatMessage(
        enterprise_id=2,
        conversation_id=2,
        sender_id=10,
        sender_type="human",
        client_message_id="legacy-source",
        content="@小菱 新请求",
        create_time=later,
        update_time=later,
    )
    db.add_all([placeholder, ambiguous, source])
    db.flush()

    messages = runtime._current_discussion_messages(
        db,
        enterprise_id=2,
        conversation_id=2,
        source_message_id=int(source.id),
    )

    assert [int(message.id) for message in messages] == [int(ambiguous.id), int(source.id)]
