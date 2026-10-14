"""合成历史检查点的状态查询内存对照；不接触生产数据或请求。"""

import gc
import json
import tracemalloc

from sqlalchemy import create_engine, event, tuple_
from sqlalchemy.orm import sessionmaker

from app.models import load_all_models
from app.models.agent_response_run import AgentResponseRun
from app.services.agent_mesh_service import _latest_run_states


def legacy_states(db):
    rows = db.query(AgentResponseRun).filter(
        AgentResponseRun.user_id == 7,
        tuple_(AgentResponseRun.surface, AgentResponseRun.session_key).in_([("user", "session-synthetic")]),
    ).order_by(AgentResponseRun.id.desc()).all()
    states = {}
    for row in rows:
        states.setdefault((row.surface, row.session_key), (row.run_id, row.status))
    return states


def measure(query, db):
    statements = []

    def capture(_connection, _cursor, statement, _parameters, _context, _executemany):
        statements.append(statement)

    gc.collect()
    tracemalloc.start()
    event.listen(db.get_bind(), "before_cursor_execute", capture)
    try:
        states = query(db)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        event.remove(db.get_bind(), "before_cursor_execute", capture)
        tracemalloc.stop()
    return {
        "python_traced_peak_bytes": peak,
        "state_count": len(states),
        "select_count": len(statements),
        "select_contains_checkpoint": any("checkpoint_json" in sql.lower() for sql in statements),
        "select_sql": statements,
    }


def main():
    load_all_models()
    engine = create_engine("sqlite:///:memory:")
    AgentResponseRun.__table__.create(engine)
    db = sessionmaker(bind=engine)()
    checkpoint = '{"synthetic_payload":"' + "x" * (1024 * 1024) + '"}'
    for index in range(36):
        db.add(AgentResponseRun(
            run_id=f"synthetic-{index}", user_id=7, surface="user", session_key="session-synthetic",
            status="completed", checkpoint_json=checkpoint,
        ))
    db.commit()
    db.expunge_all()
    try:
        report = {
            "sample_type": "local_synthetic_sqlite_no_production_content",
            "history_rows": 36,
            "checkpoint_bytes_per_row": len(checkpoint.encode()),
            "metric_scope": "tracemalloc Python heap during query; excludes SQLite C allocations and process RSS",
            "legacy": measure(legacy_states, db),
            "projected": measure(lambda session: _latest_run_states(
                session, user_id=7, conversation_refs=[("user", "session-synthetic")],
            ), db),
        }
        print(json.dumps(report, ensure_ascii=False, indent=2))
    finally:
        db.close()
        engine.dispose()


if __name__ == "__main__":
    main()
