"""验证 061 索引迁移在 SQLite 上的结构和范围查询计划；不连接生产/MySQL。"""

from __future__ import annotations

import importlib.util
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations


ROOT = Path(__file__).resolve().parents[3]
MIGRATION = ROOT / "backend/alembic/versions/061_tool_call_time_agent_idx.py"
SPEC = importlib.util.spec_from_file_location("migration_061", MIGRATION)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)

engine = sa.create_engine("sqlite:///:memory:")
with engine.begin() as connection:
    connection.exec_driver_sql(
        "CREATE TABLE tool_call_log "
        "(id INTEGER PRIMARY KEY, create_time DATETIME NOT NULL, agent_code VARCHAR(80) NOT NULL)"
    )
    connection.exec_driver_sql(
        "CREATE TABLE ai_call_log "
        "(id INTEGER PRIMARY KEY, create_time DATETIME NOT NULL, "
        "agent_label VARCHAR(50), model_name VARCHAR(50) NOT NULL, "
        "prompt_tokens INTEGER, completion_tokens INTEGER, total_tokens INTEGER)"
    )
    operations = Operations(MigrationContext.configure(connection))
    MODULE.op = SimpleNamespace(create_index=operations.create_index)
    MODULE.upgrade()

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    rows = [
        (now - timedelta(days=index % 14), f"agent_{index % 40:02d}")
        for index in range(20_000)
    ]
    connection.exec_driver_sql(
        "INSERT INTO tool_call_log (create_time, agent_code) VALUES (?, ?)", rows,
    )
    connection.exec_driver_sql(
        "INSERT INTO ai_call_log "
        "(create_time, agent_label, model_name, prompt_tokens, completion_tokens, total_tokens) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        [
        (created_at, agent_code, "probe-model", 100, 25, 125)
            if index % 11
            else (created_at, agent_code, "probe-model", None, 25, None)
            for index, (created_at, agent_code) in enumerate(rows)
        ],
    )
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=1)
    plan = connection.exec_driver_sql(
        "EXPLAIN QUERY PLAN SELECT agent_code, COUNT(id) "
        "FROM tool_call_log WHERE create_time >= ? AND create_time < ? "
        "GROUP BY agent_code",
        (start, end),
    ).all()
    plan_text = " | ".join(str(row[-1]) for row in plan)
    ai_plan = connection.exec_driver_sql(
        "EXPLAIN QUERY PLAN SELECT agent_label, model_name, COUNT(id), "
        "COALESCE(SUM(CASE WHEN COALESCE(total_tokens, 0) >= "
        "COALESCE(prompt_tokens, 0) + COALESCE(completion_tokens, 0) "
        "THEN COALESCE(total_tokens, 0) "
        "ELSE COALESCE(prompt_tokens, 0) + COALESCE(completion_tokens, 0) END), 0) "
        ", COALESCE(SUM(CASE WHEN total_tokens IS NULL AND "
        "(prompt_tokens IS NULL OR completion_tokens IS NULL) THEN 1 ELSE 0 END), 0) "
        "FROM ai_call_log WHERE create_time >= ? AND create_time < ? "
        "AND (agent_label IS NOT NULL OR model_name IS NOT NULL) "
        "GROUP BY agent_label, model_name",
        (start, end),
    ).all()
    ai_plan_text = " | ".join(str(row[-1]) for row in ai_plan)
    inspector = sa.inspect(connection)
    tool_indexes = {index["name"] for index in inspector.get_indexes("tool_call_log")}
    ai_indexes = {index["name"] for index in inspector.get_indexes("ai_call_log")}
    assert "ix_tool_call_time_agent" in tool_indexes
    assert "ix_ai_call_log_time_agent_model" in ai_indexes
    assert "ix_tool_call_time_agent" in plan_text
    assert "ix_ai_call_log_time_agent_model" in ai_plan_text
    print(
        "Rows=20000 per log table; activity range plans use both new indexes; "
        f"tool={plan_text}; ai={ai_plan_text}"
    )
