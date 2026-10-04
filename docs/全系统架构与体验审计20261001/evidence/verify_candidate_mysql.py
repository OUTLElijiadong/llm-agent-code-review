"""仅在独立 cr_testdb 的临时库验证候选迁移、锁等待与查询计划。"""
from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

import pymysql
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from pymysql.constants import CLIENT
from sqlalchemy.orm import sessionmaker


ROOT = Path("/candidate")
SCHEMA = os.environ["DB_NAME"]
assert re.fullmatch(r"prism_validation_[0-9a-f]{12}", SCHEMA)
assert os.environ["DB_HOST"] == "127.0.0.1"
CONNECTION = dict(host="127.0.0.1", port=3306, user="root", password=os.environ["DB_PASSWORD"], charset="utf8mb4")
RESULT = {"scope": "isolated_cr_testdb", "schema": SCHEMA}


def run_sql_script(connection, path):
    source = path.read_text(encoding="utf-8")
    source = re.sub(r"CREATE DATABASE IF NOT EXISTS code_review\b.*?;", "", source, flags=re.S | re.I)
    source = re.sub(r"^\s*USE code_review\s*;", "", source, flags=re.M | re.I)
    assert not re.search(r"\b(?:CREATE DATABASE|USE)\s+", source, flags=re.I)
    with connection.cursor() as cursor:
        cursor.execute(source)
        while cursor.nextset():
            pass
    connection.commit()


def migrations():
    config = Config(str(ROOT / "backend/alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "backend/alembic"))
    command.upgrade(config, "059_response_transcript_ledger")
    from app.core.config import settings

    engine = sa.create_engine(settings.db_url)
    with engine.begin() as connection:
        connection.execute(sa.text(
            "INSERT INTO agent_response_transcript_message "
            "(user_id,surface,session_key,position,message_json) "
            "VALUES (90001,'user','isolated-migration',:position,:body)"
        ), [{"position": i, "body": json.dumps({"role": "user", "content": f"第{i}条原文🧩"}, ensure_ascii=False)}
            for i in range(503)])
    command.upgrade(config, "head")
    with engine.connect() as connection:
        rows = connection.execute(sa.text(
            "SELECT message_json,message_sha256 FROM agent_response_transcript_message ORDER BY position"
        )).all()
        assert len(rows) == 503
        assert all(digest == hashlib.sha256(body.encode()).hexdigest() for body, digest in rows)
        assert connection.execute(sa.text("SELECT version_num FROM alembic_version")).scalar_one() == "061_overview_time_indexes"
        nullable = connection.execute(sa.text(
            "SELECT IS_NULLABLE FROM information_schema.COLUMNS WHERE TABLE_SCHEMA=:schema "
            "AND TABLE_NAME='agent_response_transcript_message' AND COLUMN_NAME='message_sha256'"
        ), {"schema": SCHEMA}).scalar_one()
        assert nullable == "NO"
    RESULT["migration"] = {"chain": "init.sql -> seed.sql -> 001..061", "digests": 503, "digest_not_null": True}
    return engine


def lock_regression(engine):
    from app.core.exceptions import ConflictError
    from app.models.code_file import CodeFile
    from app.models.code_version import CodeVersion
    from app.models.project import Project
    from app.models.user import User
    from app.services import code_file_service

    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    rounds = []
    for round_no in range(3):
        with factory() as seed:
            user = User(username=f"isolated-lock-{round_no}", password="x", role="admin", status=1)
            seed.add(user)
            seed.flush()
            project = Project(user_id=user.id, project_name=f"isolated-lock-{round_no}", status="active", language="python")
            seed.add(project)
            seed.flush()
            source = CodeFile(project_id=project.id, file_name="app.py", file_path="app.py", language="python",
                              content="value = 1\n", size_bytes=10, raw_size=10, line_count=1, version_no=1, is_binary=0, status="active")
            seed.add(source)
            seed.flush()
            seed.add(CodeVersion(file_id=source.id, version_no=1, content=source.content, operator_id=user.id, create_time=datetime.utcnow()))
            seed.commit()
            user_id, project_id, file_id = user.id, project.id, source.id

        started, finished = threading.Event(), threading.Event()
        outcome, failures = [], []

        def edit_after_stale_read():
            try:
                with factory() as stale:
                    actor = stale.get(User, user_id)
                    assert stale.get(CodeFile, file_id).version_no == 1
                    started.set()
                    try:
                        code_file_service.update_content(stale, actor, file_id, "value = 99\n", expected_version=1)
                    except ConflictError as exc:
                        outcome.append(exc.code)
                        stale.rollback()
                    else:
                        raise AssertionError("stale write unexpectedly accepted")
            except BaseException as exc:
                failures.append(exc)
            finally:
                finished.set()

        with factory() as writer:
            writer.query(Project).filter_by(id=project_id).with_for_update().one()
            current = writer.query(CodeFile).filter_by(id=file_id).with_for_update().one()
            current.content, current.version_no = "value = 2\n", 2
            writer.add(CodeVersion(file_id=file_id, version_no=2, content=current.content, operator_id=user_id, create_time=datetime.utcnow()))
            writer.flush()
            thread = threading.Thread(target=edit_after_stale_read, daemon=True)
            thread.start()
            assert started.wait(10), "stale read did not start"
            # 数据库自身的锁等待表证明第二连接已进入 FOR UPDATE 等待。
            wait_rows = 0
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                with engine.connect() as monitor:
                    wait_rows = monitor.execute(sa.text(
                        "SELECT COUNT(*) FROM performance_schema.data_lock_waits w "
                        "JOIN performance_schema.data_locks l ON l.ENGINE_LOCK_ID=w.REQUESTING_ENGINE_LOCK_ID "
                        "WHERE l.OBJECT_SCHEMA=:schema"
                    ), {"schema": SCHEMA}).scalar_one()
                if wait_rows:
                    break
                time.sleep(.05)
            assert wait_rows > 0 and not finished.is_set(), "no actual MySQL lock wait observed"
            writer.commit()
            thread.join(10)
            assert not thread.is_alive()
        if failures:
            raise failures[0]
        assert outcome == [40904]
        with factory() as verify:
            current = verify.get(CodeFile, file_id)
            history = verify.query(CodeVersion).filter_by(file_id=file_id).order_by(CodeVersion.version_no).all()
            assert current.content == "value = 2\n" and current.version_no == 2
            assert [item.version_no for item in history] == [1, 2]
            assert code_file_service.get_file_meta(verify, verify.get(User, user_id), file_id)["sha256_hash"] == hashlib.sha256(current.content.encode()).hexdigest()
            assert code_file_service.update_content(verify, verify.get(User, user_id), file_id, "value = 3\n", expected_version=2) == 3
            code_file_service.rename_file(verify, verify.get(User, user_id), file_id, "renamed.py")
            code_file_service.delete_file(verify, verify.get(User, user_id), file_id)
            assert verify.get(CodeFile, file_id).status == "deleted"
        rounds.append({"round": round_no + 1, "lock_wait_rows": wait_rows, "stale_conflict": 40904, "next_save_version": 3})
    RESULT["mysql_lock_regression"] = rounds


def plans(engine):
    now = datetime.utcnow().replace(microsecond=0)
    with engine.begin() as connection:
        connection.execute(sa.text(
            "INSERT INTO tool_call_log (agent_code,tool_code,action,resource,status,risk_level,decision,duration_ms,create_time,update_time) "
            "VALUES (:agent,'isolated-read','read','','success','low','allow',0,:created,:created)"
        ), [{"agent": f"agent_{i % 40:02d}", "created": now - timedelta(days=i % 14)} for i in range(20_000)])
        connection.execute(sa.text(
            "INSERT INTO ai_call_log (agent_label,model_name,status,create_time,prompt_tokens,completion_tokens,total_tokens) "
            "VALUES (:agent,'isolated-model','success',:created,100,25,125)"
        ), [{"agent": f"agent_{i % 40:02d}", "created": now - timedelta(days=i % 14)} for i in range(20_000)])
        for table in ("tool_call_log", "ai_call_log"):
            connection.exec_driver_sql(f"ANALYZE TABLE {table}").all()
        start = now.replace(hour=0, minute=0, second=0)
        bounds = {"start": start, "end": start + timedelta(days=1)}
        tool_plan = connection.execute(sa.text(
            "EXPLAIN SELECT agent_code,COUNT(id) FROM tool_call_log "
            "WHERE create_time>=:start AND create_time<:end GROUP BY agent_code"
        ), bounds).mappings().all()
        ai_plan = connection.execute(sa.text(
            "EXPLAIN SELECT agent_label,model_name,COUNT(id), "
            "COALESCE(SUM(CASE WHEN COALESCE(total_tokens,0)>=COALESCE(prompt_tokens,0)+COALESCE(completion_tokens,0) "
            "THEN COALESCE(total_tokens,0) ELSE COALESCE(prompt_tokens,0)+COALESCE(completion_tokens,0) END),0), "
            "COALESCE(SUM(CASE WHEN total_tokens IS NULL AND (prompt_tokens IS NULL OR completion_tokens IS NULL) "
            "THEN 1 ELSE 0 END),0) FROM ai_call_log "
            "WHERE create_time>=:start AND create_time<:end AND (agent_label IS NOT NULL OR model_name IS NOT NULL) "
            "GROUP BY agent_label,model_name"
        ), bounds).mappings().all()
        assert tool_plan[0]["key"] == "ix_tool_call_time_agent"
        # MySQL may prefer its existing time index for the non-covering token aggregate.
        assert ai_plan[0]["key"] in {"ix_ai_call_log_time_agent_model", "ix_ai_call_log_create_time", "idx_create_time"}
        RESULT["query_plans"] = {"rows_per_table": 20_000, "tool": [dict(row) for row in tool_plan], "ai": [dict(row) for row in ai_plan]}


admin = pymysql.connect(**CONNECTION, autocommit=True)
engine = None
created_by_this_run = False
try:
    with admin.cursor() as cursor:
        cursor.execute(f"CREATE DATABASE `{SCHEMA}` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci")
    created_by_this_run = True
    connection = pymysql.connect(**CONNECTION, database=SCHEMA, client_flag=CLIENT.MULTI_STATEMENTS)
    try:
        run_sql_script(connection, ROOT / "deploy/mysql/init.sql")
        run_sql_script(connection, ROOT / "deploy/mysql/seed.sql")
    finally:
        connection.close()
    engine = migrations()
    lock_regression(engine)
    plans(engine)
finally:
    if engine:
        engine.dispose()
    if created_by_this_run:
        with admin.cursor() as cursor:
            cursor.execute(f"DROP DATABASE `{SCHEMA}`")
            cursor.execute("SELECT COUNT(*) FROM information_schema.SCHEMATA WHERE SCHEMA_NAME=%s", (SCHEMA,))
            assert cursor.fetchone()[0] == 0
    admin.close()
    RESULT["temporary_schema_removed"] = created_by_this_run
print(json.dumps(RESULT, ensure_ascii=False, indent=2, default=str))
