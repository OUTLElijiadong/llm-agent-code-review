"""全新建库链回归：空 MySQL 依次执行 init.sql → seed.sql → alembic upgrade head。

新环境由 docker-compose 的 docker-entrypoint-initdb.d 先导入冻结基线
(deploy/mysql/init.sql、seed.sql)，再由发布脚本执行 Alembic。ORM 声明了某一列而基线和
迁移都没有创建它时，靠 ORM create_all 建库的 SQLite 测试永远发现不了，只有走真实建库链
才会在首个查询处报 1054 Unknown column(review_rule.language/severity 即此类缺陷)。

本模块只在显式给出一次性空 MySQL 时运行：

    PRISM_FRESH_CHAIN_MYSQL_URL='mysql+pymysql://root:<密码>@127.0.0.1:<端口>/' \\
        python -m pytest -q --no-cov tests/test_fresh_database_chain.py

目标必须是回环地址上没有 code_review 表的服务器，否则直接失败而不是改动已有数据；
测试结束会删除自己创建的 code_review 库。CI 设置 PRISM_FRESH_CHAIN_REQUIRED=1，
使缺少 MySQL 时失败而不是静默跳过。
"""

from __future__ import annotations

import os
import secrets
import subprocess
import sys
import textwrap
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

import pymysql
import pytest
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory
from pymysql.constants import CLIENT
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import URL, make_url
from sqlalchemy.orm import Session

BACKEND = Path(__file__).resolve().parents[1]
ROOT = BACKEND.parent
INIT_SQL = ROOT / "deploy" / "mysql" / "init.sql"
SEED_SQL = ROOT / "deploy" / "mysql" / "seed.sql"
RULES_V2_SQL = BACKEND / "seed_rules_v2.sql"

DATABASE = "code_review"  # init.sql 写死的库名
LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}

# alembic 在独立进程里运行，且不读取开发机 .env，避免本机配置影响建库链结果。
_ALEMBIC_BOOTSTRAP = textwrap.dedent(
    """
    from pydantic_settings import BaseSettings

    original_init = BaseSettings.__init__

    def init_without_dotenv(instance, *args, **kwargs):
        kwargs["_env_file"] = None
        original_init(instance, *args, **kwargs)

    BaseSettings.__init__ = init_without_dotenv

    from alembic.config import main

    main(argv=["-c", "alembic.ini", "upgrade", "head"], prog="alembic")
    """
)


def _connect(url: URL, *, database: str | None = None, autocommit: bool = True):
    return pymysql.connect(
        host=url.host,
        port=url.port or 3306,
        user=url.username,
        password=url.password or "",
        database=database,
        charset="utf8mb4",
        client_flag=CLIENT.MULTI_STATEMENTS,
        autocommit=autocommit,
    )


def _execute_sql_file(connection, path: Path) -> None:
    """交给服务端解析整份脚本，与 mysql 客户端导入的语句边界一致。"""
    with connection.cursor() as cursor:
        cursor.execute(path.read_text(encoding="utf-8"))
        while cursor.nextset():
            pass


def _alembic_upgrade_head(url: URL) -> None:
    environment = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": os.environ.get("HOME", ""),
        "DB_HOST": url.host,
        "DB_PORT": str(url.port or 3306),
        "DB_USER": url.username,
        "DB_PASSWORD": url.password or "",
        "DB_NAME": DATABASE,
        # 022 迁移只在新库上用它初始化 admin 密码；一次性随机值，库随测试销毁。
        "INITIAL_ADMIN_PASSWORD": secrets.token_urlsafe(18),
    }
    result = subprocess.run(
        [sys.executable, "-I", "-X", "utf8", "-c", _ALEMBIC_BOOTSTRAP],
        cwd=BACKEND,
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=600,
        check=False,
    )
    if result.returncode != 0:
        tail = "\n".join((result.stdout + result.stderr).splitlines()[-40:])
        pytest.fail(f"alembic upgrade head 在全新库上失败(退出码 {result.returncode}):\n{tail}")


@pytest.fixture(scope="module")
def fresh_chain_url() -> URL:
    raw = os.environ.get("PRISM_FRESH_CHAIN_MYSQL_URL", "")
    if not raw:
        if os.environ.get("PRISM_FRESH_CHAIN_REQUIRED") == "1":
            pytest.fail("PRISM_FRESH_CHAIN_REQUIRED=1，但没有提供 PRISM_FRESH_CHAIN_MYSQL_URL")
        pytest.skip("需要显式提供一次性空 MySQL：PRISM_FRESH_CHAIN_MYSQL_URL")
    url = make_url(raw)
    if url.host not in LOOPBACK_HOSTS:
        pytest.fail(f"拒绝在非回环地址 {url.host} 上执行建库链测试")
    return url


@pytest.fixture(scope="module")
def fresh_chain(fresh_chain_url: URL):
    """在空服务器上真实走完 init.sql → seed.sql → alembic upgrade head，返回连新库的引擎。"""
    admin = _connect(fresh_chain_url)
    try:
        with admin.cursor() as cursor:
            cursor.execute(
                "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema = %s", (DATABASE,)
            )
            existing = cursor.fetchone()[0]
        if existing:
            pytest.fail(
                f"目标 MySQL 的 {DATABASE} 已有 {existing} 张表；本测试只能在一次性空服务器上运行，已拒绝执行"
            )
        try:
            _execute_sql_file(admin, INIT_SQL)
            _execute_sql_file(admin, SEED_SQL)
            _alembic_upgrade_head(fresh_chain_url)
            engine = create_engine(
                fresh_chain_url.set(drivername="mysql+pymysql", database=DATABASE),
                connect_args={"client_flag": CLIENT.MULTI_STATEMENTS | CLIENT.FOUND_ROWS},
            )
            try:
                yield engine
            finally:
                engine.dispose()
        finally:
            with admin.cursor() as cursor:
                cursor.execute(f"DROP DATABASE IF EXISTS {DATABASE}")
    finally:
        admin.close()


def _script_heads() -> list[str]:
    config = Config(str(BACKEND / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND / "alembic"))
    return ScriptDirectory.from_config(config).get_heads()


def test_chain_ends_at_the_single_alembic_head(fresh_chain) -> None:
    with fresh_chain.connect() as connection:
        applied = connection.execute(text("SELECT version_num FROM alembic_version")).scalars().all()

    assert applied == _script_heads()


def test_review_rule_has_language_and_severity_columns(fresh_chain) -> None:
    columns = {column["name"]: column for column in inspect(fresh_chain).get_columns("review_rule")}

    assert {"language", "severity"} <= set(columns), (
        "全新建库链缺少 review_rule 的 language/severity 列：" f"{sorted(columns)}"
    )
    language, severity = columns["language"], columns["severity"]
    assert (language["type"].length, language["nullable"]) == (30, True)
    assert (severity["type"].length, severity["nullable"]) == (10, True)
    assert str(language["default"]).strip("'\"") == "*"
    assert str(severity["default"]).strip("'\"") == "中"
    assert language["comment"] == "适用编程语言,*=通用"
    assert severity["comment"] == "严重|高|中|低"


def test_orm_tables_and_columns_are_all_created_by_the_chain(fresh_chain) -> None:
    from app.core.database import Base
    from app.models import load_all_models

    load_all_models()
    with fresh_chain.connect() as connection:
        differences = compare_metadata(MigrationContext.configure(connection), Base.metadata)

    missing = []
    for difference in differences:
        if not isinstance(difference, tuple):
            continue
        if difference[0] == "add_table":
            missing.append(f"表 {difference[1].name}")
        elif difference[0] == "add_column":
            missing.append(f"列 {difference[2]}.{difference[3].name}")
    assert not missing, "ORM 已声明但全新建库链没有创建：" + ", ".join(sorted(missing))


def test_baseline_seed_rules_are_backfilled_with_column_defaults(fresh_chain) -> None:
    expected = sum(1 for line in SEED_SQL.read_text(encoding="utf-8").splitlines() if line.startswith("(NULL,"))
    with fresh_chain.connect() as connection:
        rows = connection.execute(
            text("SELECT language, severity FROM review_rule WHERE is_builtin = 1")
        ).all()

    assert expected > 0
    assert len(rows) == expected
    # language 为 NULL 的规则会被按语言过滤整体漏掉，所以基线种子必须拿到默认值。
    assert {(row.language, row.severity) for row in rows} == {("*", "中")}


def test_rule_service_language_filter_runs_on_the_fresh_database(fresh_chain) -> None:
    from app.services import rule_service

    with Session(fresh_chain) as session:
        unfiltered = rule_service.get_enabled_rules(session, user_id=1)
        python_rules = rule_service.get_enabled_rules(session, user_id=1, language="python")

    assert unfiltered
    assert {rule.rule_code for rule in python_rules} == {rule.rule_code for rule in unfiltered}


def test_065_on_mysql_keeps_hand_patched_columns_and_adds_only_the_missing_one(fresh_chain_url: URL) -> None:
    """老生产库当年手工补过列(可能只补了一列，定义也不同)：065 只补缺的，已有的原样保留。"""
    probe = "prism_065_hand_patched_probe"
    spec = spec_from_file_location(
        "migration_065_for_mysql_probe", BACKEND / "alembic" / "versions" / "065_review_rule_lang_severity.py"
    )
    migration = module_from_spec(spec)
    spec.loader.exec_module(migration)

    admin = _connect(fresh_chain_url)
    engine = None
    try:
        with admin.cursor() as cursor:
            cursor.execute(f"DROP DATABASE IF EXISTS {probe}")
            cursor.execute(f"CREATE DATABASE {probe} DEFAULT CHARACTER SET utf8mb4")
            cursor.execute(
                f"CREATE TABLE {probe}.review_rule (id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY, "
                "rule_code VARCHAR(50) NOT NULL, language VARCHAR(50) NOT NULL DEFAULT 'any')"
            )
            cursor.execute(f"INSERT INTO {probe}.review_rule (rule_code, language) VALUES ('r1', 'go')")
        engine = create_engine(fresh_chain_url.set(drivername="mysql+pymysql", database=probe))
        for _ in range(2):  # 第二轮验证重复执行是空操作
            with engine.begin() as connection:
                migration.op = Operations(MigrationContext.configure(connection))
                migration.upgrade()

        columns = {column["name"]: column for column in inspect(engine).get_columns("review_rule")}
        assert (columns["language"]["type"].length, columns["language"]["nullable"]) == (50, False)
        assert str(columns["language"]["default"]).strip("'\"") == "any"
        assert (columns["severity"]["type"].length, columns["severity"]["nullable"]) == (10, True)
        assert str(columns["severity"]["default"]).strip("'\"") == "中"
        with engine.connect() as connection:
            assert tuple(connection.execute(text("SELECT language, severity FROM review_rule")).one()) == ("go", "中")
    finally:
        if engine is not None:
            engine.dispose()
        with admin.cursor() as cursor:
            cursor.execute(f"DROP DATABASE IF EXISTS {probe}")
        admin.close()


def test_complete_builtin_rule_set_loads_and_filters_by_language(fresh_chain) -> None:
    """seed_rules_v2.sql 同时写入 language/severity；事务内验证后回滚，不影响其他用例。"""
    from app.services import rule_service

    expected = sum(1 for line in RULES_V2_SQL.read_text(encoding="utf-8").splitlines() if line.startswith("(NULL,"))
    with fresh_chain.connect() as connection:
        transaction = connection.begin()
        try:
            cursor = connection.connection.cursor()
            cursor.execute(RULES_V2_SQL.read_text(encoding="utf-8"))
            while cursor.nextset():
                pass
            session = Session(bind=connection)
            builtin_count = connection.execute(
                text("SELECT COUNT(*) FROM review_rule WHERE is_builtin = 1")
            ).scalar_one()
            severities = set(
                connection.execute(text("SELECT DISTINCT severity FROM review_rule WHERE is_builtin = 1")).scalars()
            )
            python_rules = rule_service.get_enabled_rules(session, user_id=1, language="python")
            go_rules = rule_service.get_enabled_rules(session, user_id=1, language="go")
        finally:
            transaction.rollback()

    assert builtin_count == expected
    assert severities <= {"严重", "高", "中", "低"}
    assert {rule.language for rule in python_rules} == {"*", "python"}
    assert {rule.language for rule in go_rules} == {"*", "go"}
    assert "go_nil_map" in {rule.rule_code for rule in go_rules}
    assert "go_nil_map" not in {rule.rule_code for rule in python_rules}
