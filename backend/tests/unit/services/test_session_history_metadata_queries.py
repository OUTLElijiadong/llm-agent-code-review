"""恢复有完整账本的UI历史时不搬运无用的大检查点，隔离与摘要校验保留。"""
import hashlib
import json

from sqlalchemy import event

from app.api.v1 import agent_responses as api
from app.models.agent_response_run import AgentResponseRun, AgentResponseTranscriptMessage


def test_ledger_history_page_reads_only_run_metadata_with_large_checkpoints(db):
    transcript = [{"role": "user" if i % 2 == 0 else "assistant", "content": f"完整消息{i}"} for i in range(12)]
    for i, item in enumerate(transcript):
        raw = json.dumps(item, ensure_ascii=False)
        run_id = f"history-metadata-{i}"
        db.add(AgentResponseRun(run_id=run_id, user_id=7, surface="user", session_key="large-history-metadata",
                                status="completed", checkpoint_json=json.dumps({"unneeded": "x" * 1_000_000})))
        db.add(AgentResponseTranscriptMessage(
            user_id=7, surface="user", session_key="large-history-metadata", position=i, run_id=run_id,
            message_json=raw, message_sha256=hashlib.sha256(raw.encode()).hexdigest(),
        ))
    db.add(AgentResponseRun(run_id="foreign-history-metadata", user_id=8, surface="user",
                            session_key="large-history-metadata", status="completed", checkpoint_json="{}"))
    db.commit()
    db.expunge_all()
    statements = []

    def record(_conn, _cursor, statement, _parameters, _context, _executemany):
        statements.append(statement.lower())

    event.listen(db.bind, "before_cursor_execute", record)
    try:
        for _ in range(3):
            latest = api._session_history_page(db, user_id=7, surface="user",
                                               session_id="large-history-metadata", limit=5)
            assert latest["messages"] == transcript[-5:]
            older = api._session_history_page(db, user_id=7, surface="user",
                                              session_id="large-history-metadata", limit=7, before_message=7)
            assert older["messages"] + latest["messages"] == transcript
            foreign = api._session_history_page(db, user_id=9, surface="user",
                                                session_id="large-history-metadata", limit=100)
            assert foreign["messages"] == []
    finally:
        event.remove(db.bind, "before_cursor_execute", record)
    selects = [sql.split("from", 1)[0] for sql in statements
               if sql.lstrip().startswith("select") and "agent_response_run" in sql]
    assert selects
    assert all("checkpoint_json" not in sql for sql in selects)
