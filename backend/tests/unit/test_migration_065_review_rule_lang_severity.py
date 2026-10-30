"""065 迁移补齐冻结基线 init.sql 缺失的 review_rule.language/severity 两列。

基线表结构与种子行取自仓库里真实的 deploy/mysql/init.sql、seed.sql(译成 SQLite)，
这样基线日后若被改动，本测试验证的仍是"从基线出发能否走到 ORM 要求的结构"。
需要真实 MySQL 的整条建库链见 tests/test_fresh_database_chain.py。
"""

from __future__ import annotations

import importlib.util
import io
import os
import re
import sqlite3
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine
from sqlalchemy.dialects import mysql
from sqlalchemy.orm import Session

BACKEND = Path(__file__).resolve().parents[2]
ROOT = BACKEND.parent
MIGRATION_PATH = BACKEND / "alembic" / "versions" / "065_review_rule_lang_severity.py"
INIT_SQL = ROOT / "deploy" / "mysql" / "init.sql"
SEED_SQL = ROOT / "deploy" / "mysql" / "seed.sql"
REVISION = "065_review_rule_lang_severity"

# 独立进程：不读开发机 .env，把基线库标记在 064，再经 Alembic 命令升级到 065。
_STAMP_064_THEN_UPGRADE = textwrap.dedent(
    """
    import sys

    from pydantic_settings import BaseSettings

    original_init = BaseSettings.__init__

    def init_without_dotenv(instance, *args, **kwargs):
        kwargs["_env_file"] = None
        original_init(instance, *args, **kwargs)

    BaseSettings.__init__ = init_without_dotenv

    backend, target = sys.argv[1], sys.argv[2]
    sys.path.insert(0, backend)

    from alembic import command
    from alembic.config import Config

    config = Config(backend + "/alembic.ini")
    config.set_main_option("script_location", backend + "/alembic")
    command.stamp(config, "064_remote_pentest_chain")
    command.upgrade(config, target)
    """
)


def _baseline_review_rule_ddl() -> str:
    """把 init.sql 里 review_rule 的 MySQL 建表语句逐列译成等价的 SQLite DDL。"""
    sql = INIT_SQL.read_text(encoding="utf-8")
    block = re.search(r"CREATE TABLE IF NOT EXISTS review_rule \((.*?)\n\) ENGINE=", sql, re.S)
    assert block, "init.sql 中找不到 review_rule 建表语句"
    columns = []
    for raw in block.group(1).splitlines():
        line = raw.strip().rstrip(",")
        if not line or line.startswith(("PRIMARY KEY", "UNIQUE KEY", "KEY ")):
            continue
        line = re.sub(r"\s+COMMENT\s+'[^']*'", "", line)
        line = line.replace(" ON UPDATE CURRENT_TIMESTAMP", "")
        line = re.sub(r"^(\w+)\s+BIGINT\s+NOT NULL AUTO_INCREMENT", r"\1 INTEGER PRIMARY KEY", line)
        columns.append(line)
    return "CREATE TABLE review_rule (" + ", ".join(columns) + ")"


def _baseline_seed_statements() -> list[str]:
    """seed.sql 中写 review_rule 的语句；字符串里含 ';'，必须按引号感知切分。"""
    statements, buffer = [], ""
    for line in SEED_SQL.read_text(encoding="utf-8").splitlines(keepends=True):
        if not buffer and (not line.strip() or line.lstrip().startswith("--")):
            continue
        buffer += line
        if sqlite3.complete_statement(buffer):
            statements.append(buffer.strip())
            buffer = ""
    return [
        statement.replace("INSERT IGNORE INTO", "INSERT OR IGNORE INTO", 1)
        for statement in statements
        if "INTO review_rule" in statement
    ]


def _migration():
    spec = importlib.util.spec_from_file_location("migration_065_review_rule_lang_severity", MIGRATION_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run(connection, step: str) -> None:
    migration = _migration()
    migration.op = Operations(MigrationContext.configure(connection))
    getattr(migration, step)()


def _table_info(connection) -> dict[str, tuple]:
    """列名 -> (类型, notnull, 默认值)。"""
    rows = connection.exec_driver_sql("PRAGMA table_info(review_rule)").all()
    return {row[1]: (row[2], row[3], row[4]) for row in rows}


@pytest.fixture
def baseline_engine():
    engine = create_engine("sqlite://")
    with engine.begin() as connection:
        connection.exec_driver_sql(_baseline_review_rule_ddl())
        for statement in _baseline_seed_statements():
            connection.exec_driver_sql(statement)
    try:
        yield engine
    finally:
        engine.dispose()


def test_revision_is_linked_to_the_previous_head_and_fits_the_version_column() -> None:
    migration = _migration()

    assert migration.revision == REVISION
    assert migration.down_revision == "064_remote_pentest_chain"
    assert len(migration.revision) <= 32


def test_baseline_really_lacks_the_columns_the_orm_declares(baseline_engine) -> None:
    """前置条件：测试起点必须就是缺列的冻结基线，否则下面的用例什么也证明不了。"""
    from app.models.review_rule import ReviewRule

    with baseline_engine.connect() as connection:
        columns = set(_table_info(connection))
    orm_columns = {column.name for column in ReviewRule.__table__.columns}

    assert orm_columns - columns == {"language", "severity"}


def test_upgrade_restores_columns_backfills_seed_rows_and_unblocks_rule_service(baseline_engine) -> None:
    from app.services import rule_service

    with baseline_engine.begin() as connection:
        seeded = connection.exec_driver_sql("SELECT COUNT(*) FROM review_rule").scalar_one()
        _run(connection, "upgrade")

        info = _table_info(connection)
        assert info["language"] == ("VARCHAR(30)", 0, "'*'")
        assert info["severity"] == ("VARCHAR(10)", 0, "'中'")
        rows = connection.exec_driver_sql("SELECT language, severity FROM review_rule").all()
        assert seeded > 0 and len(rows) == seeded
        # language 为 NULL 的规则会被按语言过滤整体漏掉，所以基线种子行必须被回填。
        assert {tuple(row) for row in rows} == {("*", "中")}

        session = Session(bind=connection)
        python_rules = rule_service.get_enabled_rules(session, user_id=1, language="python")
        assert len(python_rules) == seeded
        assert {rule.severity for rule in python_rules} == {"中"}


def test_upgrade_is_idempotent(baseline_engine) -> None:
    with baseline_engine.begin() as connection:
        _run(connection, "upgrade")
        first = _table_info(connection)
        _run(connection, "upgrade")

        assert _table_info(connection) == first


def test_upgrade_leaves_hand_patched_columns_untouched() -> None:
    """老生产库当年手工补过列：已存在的列(哪怕定义不同)必须原样保留。"""
    engine = create_engine("sqlite://")
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "CREATE TABLE review_rule (id INTEGER PRIMARY KEY, rule_code VARCHAR(50) NOT NULL, "
            "language VARCHAR(50) NOT NULL DEFAULT 'any', severity VARCHAR(20) DEFAULT '高')"
        )
        connection.exec_driver_sql(
            "INSERT INTO review_rule (rule_code, language, severity) VALUES ('r1', 'go', '严重')"
        )
        before = _table_info(connection)

        _run(connection, "upgrade")

        assert _table_info(connection) == before
        assert connection.exec_driver_sql("SELECT language, severity FROM review_rule").one() == ("go", "严重")
    engine.dispose()


def test_downgrade_keeps_columns_that_the_orm_and_production_depend_on(baseline_engine) -> None:
    with baseline_engine.begin() as connection:
        _run(connection, "upgrade")
        connection.exec_driver_sql("UPDATE review_rule SET language = 'python', severity = '严重' WHERE id = 1")
        before = _table_info(connection)

        _run(connection, "downgrade")

        assert _table_info(connection) == before
        assert connection.exec_driver_sql("SELECT language, severity FROM review_rule WHERE id = 1").one() == (
            "python",
            "严重",
        )


def test_mysql_ddl_matches_the_orm_definition(monkeypatch) -> None:
    from app.models.review_rule import ReviewRule

    migration = _migration()
    output = io.StringIO()
    context = MigrationContext.configure(dialect=mysql.dialect(), opts={"as_sql": True, "output_buffer": output})
    monkeypatch.setattr(migration, "op", Operations(context))

    migration.upgrade()

    ddl = " ".join(output.getvalue().split())
    for name, default in (("language", "*"), ("severity", "中")):
        column = ReviewRule.__table__.c[name]
        assert column.nullable is True
        statement = re.search(rf"ALTER TABLE review_rule ADD COLUMN {name} [^;]*;", ddl)
        assert statement, f"没有生成 {name} 的 ADD COLUMN: {ddl}"
        text = statement.group(0)
        assert f"VARCHAR({column.type.length})" in text
        assert "NOT NULL" not in text
        assert f"DEFAULT '{default}'" in text
        assert f"COMMENT '{column.comment}'" in text


def test_alembic_command_path_upgrades_a_064_baseline_to_065(tmp_path: Path) -> None:
    """走 Alembic 命令路径：基线库标记在 064，升到 065 后必须补齐两列并写入版本号。"""
    database = tmp_path / "baseline.db"
    connection = sqlite3.connect(database)
    connection.execute(_baseline_review_rule_ddl())
    for statement in _baseline_seed_statements():
        connection.execute(statement)
    connection.commit()
    connection.close()

    result = subprocess.run(
        [sys.executable, "-I", "-X", "utf8", "-c", _STAMP_064_THEN_UPGRADE, str(BACKEND), REVISION],
        cwd=tmp_path,
        env={
            "PATH": os.environ.get("PATH", ""),
            "HOME": os.environ.get("HOME", ""),
            "DB_HOST": "sqlite",
            "DB_NAME": "baseline",
        },
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, result.stderr[-2000:]

    connection = sqlite3.connect(database)
    try:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(review_rule)")}
        versions = connection.execute("SELECT version_num FROM alembic_version").fetchall()
        rows = connection.execute("SELECT DISTINCT language, severity FROM review_rule").fetchall()
    finally:
        connection.close()

    assert {"language", "severity"} <= columns
    assert versions == [(REVISION,)]
    assert rows == [("*", "中")]
