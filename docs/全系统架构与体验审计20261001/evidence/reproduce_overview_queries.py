"""Isolated query-count reproduction; no production DB or model requests.

Run from backend with .venv311/bin/python and PYTHONPATH=.
"""
from datetime import datetime, timezone
import json

from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session

from app.api.v1.admin_overview import _agent_activity
from app.core.database import Base
from app.models.agent_governance import AgentProfile, ToolCallLog
from app.models.ai_call_log import AiCallLog


def sample(size):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine, tables=[AgentProfile.__table__, ToolCallLog.__table__, AiCallLog.__table__])
    with Session(engine) as db:
        for index in range(size):
            code = f"audit_agent_{index}"
            db.add(AgentProfile(code=code, name=code, is_enabled=1))
            db.add(AiCallLog(agent_label=code, model_name="local-stub", total_tokens=10,
                             create_time=datetime.now(timezone.utc)))
        db.commit()
        statements = []
        def collect(conn, cursor, statement, parameters, context, executemany):
            if statement.lstrip().upper().startswith("SELECT"):
                statements.append(statement)
        event.listen(engine, "before_cursor_execute", collect)
        result = _agent_activity(db)
        event.remove(engine, "before_cursor_execute", collect)
        assert len(result) == size
        assert len(statements) == 4 + size
        return {"profiles_with_model_usage": size, "select_count": len(statements)}


if __name__ == "__main__":
    print(json.dumps([sample(size) for size in [1, 10, 40]], indent=2))
