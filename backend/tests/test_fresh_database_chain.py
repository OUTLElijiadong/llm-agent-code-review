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
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from threading import Barrier

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
from sqlalchemy.orm import Session, sessionmaker

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
    import sys

    main(argv=["-c", "alembic.ini", "upgrade", sys.argv[1]], prog="alembic")
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


def _alembic_upgrade(url: URL, revision: str) -> None:
    environment = {
        **os.environ,
        "DB_HOST": url.host,
        "DB_PORT": str(url.port or 3306),
        "DB_USER": url.username,
        "DB_PASSWORD": url.password or "",
        "DB_NAME": DATABASE,
        # 022 迁移只在新库上用它初始化 admin 密码；一次性随机值，库随测试销毁。
        "INITIAL_ADMIN_PASSWORD": secrets.token_urlsafe(18),
    }
    result = subprocess.run(
        [sys.executable, "-I", "-X", "utf8", "-c", _ALEMBIC_BOOTSTRAP, revision],
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
        pytest.fail(f"alembic upgrade {revision} 在全新库上失败(退出码 {result.returncode}):\n{tail}")


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
            _alembic_upgrade(fresh_chain_url, "075_payment_checkout_state")
            probe_engine = create_engine(
                fresh_chain_url.set(drivername="mysql+pymysql", database=DATABASE),
                connect_args={"client_flag": CLIENT.MULTI_STATEMENTS | CLIENT.FOUND_ROWS},
            )
            try:
                with probe_engine.begin() as connection:
                    reviewer_id = connection.execute(
                        text("SELECT id FROM role WHERE code = 'reviewer' AND status = 'active' LIMIT 1")
                    ).scalar_one()
                    user_id = connection.execute(
                        text("INSERT INTO user (username, password, role, status) "
                             "VALUES ('legacy-reviewer-migration-probe', 'x', 'reviewer', 1)")
                    ).lastrowid
                    connection.execute(
                        text("INSERT INTO user_role (user_id, role_id) VALUES (:user_id, :role_id)"),
                        {"user_id": user_id, "role_id": reviewer_id},
                    )
            finally:
                probe_engine.dispose()
            _alembic_upgrade(fresh_chain_url, "head")
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


@pytest.fixture(scope="module")
def verified_model_catalog(fresh_chain):
    """Append a synthetic approved catalog for integration tests that make model calls.

    Migration 105 deliberately leaves historical CNY facts unknown.  Keep that
    original row intact and create a separate, explicitly test-only directory;
    this never bootstraps or changes a production catalog.
    """
    from app.models.billing_ledger import BillingRateCatalog
    from app.services import billing_rate_catalog_service as catalog

    sessions = sessionmaker(bind=fresh_chain, autoflush=False, expire_on_commit=False)
    now = datetime.now(timezone.utc).replace(microsecond=0)
    decision_at = now - timedelta(hours=2)
    checked_at = decision_at - timedelta(minutes=1)
    with sessions.begin() as db:
        historical = db.query(BillingRateCatalog).order_by(BillingRateCatalog.id).first()
        assert historical.model_cny_source_url is None
        assert historical.model_cny_source_checked_at is None
        with pytest.raises(catalog.RateCatalogError, match="来源未完整核验"):
            catalog.rate_snapshot(db, "deepseek-flash")
        draft = catalog.create_draft(
            db, actor_id=9_099_001, effective_at=now - timedelta(hours=1),
            source_url="https://api-docs.deepseek.com/quick_start/pricing/",
            source_checked_at=checked_at,
            model_cny_source_url="https://api-docs.deepseek.com/zh-cn/quick_start/pricing/",
            model_cny_source_checked_at=checked_at,
            fx_source_url="https://www.boc.cn/sourcedb/whpj/index.html",
            fx_source_checked_at=checked_at, fx_cny_per_usd_scaled=67_168,
            change_note="隔离 MySQL 的合成模型费率，不代表真实核价。",
            items=catalog.baseline_items(), now=decision_at,
        )
        catalog.approve_draft(db, catalog_id=draft.id, actor_id=9_099_002, now=decision_at)
        return draft.version_key


def _script_heads() -> list[str]:
    config = Config(str(BACKEND / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND / "alembic"))
    return ScriptDirectory.from_config(config).get_heads()


def test_chain_ends_at_the_single_alembic_head(fresh_chain) -> None:
    with fresh_chain.connect() as connection:
        applied = connection.execute(text("SELECT version_num FROM alembic_version")).scalars().all()

    assert applied == _script_heads()


def test_mysql_security_artifact_schema_is_tenant_scoped_and_immutable(fresh_chain) -> None:
    """Migration 096 must install tenant FKs and append-only report guards on MySQL."""
    from sqlalchemy.exc import DBAPIError

    table_name = "enterprise_security_artifact"
    expected_constraints = {
        "fk_ent_sec_artifact_team_scope": {
            ("team_id", "id"),
            ("enterprise_id", "enterprise_id"),
            ("conversation_id", "conversation_id"),
        },
        "fk_ent_sec_artifact_task_scope": {("task_id", "id"), ("team_id", "team_id")},
        "fk_ent_sec_artifact_project_scope": {
            ("enterprise_id", "enterprise_id"),
            ("project_id", "project_id"),
        },
    }
    with fresh_chain.connect() as connection:
        fk_rows = connection.execute(
            text(
                "SELECT CONSTRAINT_NAME, COLUMN_NAME, REFERENCED_COLUMN_NAME "
                "FROM information_schema.KEY_COLUMN_USAGE "
                "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = :table_name "
                "AND REFERENCED_TABLE_NAME IS NOT NULL"
            ),
            {"table_name": table_name},
        ).mappings().all()
        actual_constraints: dict[str, set[tuple[str, str]]] = {}
        for row in fk_rows:
            actual_constraints.setdefault(str(row["CONSTRAINT_NAME"]), set()).add(
                (str(row["COLUMN_NAME"]), str(row["REFERENCED_COLUMN_NAME"]))
            )
        assert all(actual_constraints.get(name) == columns for name, columns in expected_constraints.items())

        triggers = connection.execute(
            text(
                "SELECT ACTION_STATEMENT FROM information_schema.TRIGGERS "
                "WHERE TRIGGER_SCHEMA = DATABASE() AND EVENT_OBJECT_TABLE = :table_name"
            ),
            {"table_name": table_name},
        ).scalars().all()
        assert len(triggers) == 2
        assert all("enterprise security artifacts are immutable" in str(item) for item in triggers)

        # Disable only FK checks on this isolated test connection so one row can
        # exercise the two database triggers without constructing a large tenant
        # graph. The FK declarations above are inspected independently.
        connection.exec_driver_sql("SET FOREIGN_KEY_CHECKS = 0")
        connection.execute(
            text(
                "INSERT INTO enterprise_security_artifact "
                "(id, team_id, task_id, enterprise_id, project_id, conversation_id, attempt_no, "
                "lease_generation, audit_run_id, source_sha256, report_sha256, coverage_status, "
                "termination_reason, summary_json, report_json) "
                "VALUES (990000000000001, 990000000000002, 990000000000003, 990000000000004, "
                "990000000000005, 990000000000006, 1, 1, 'mysql-096-trigger-probe', :hash, :hash, "
                "'complete', '', '{}', '{}')"
            ),
            {"hash": "a" * 64},
        )
        connection.commit()
        connection.exec_driver_sql("SET FOREIGN_KEY_CHECKS = 1")
        connection.commit()

        for statement in (
            "UPDATE enterprise_security_artifact SET summary_json = '{\"changed\":true}' "
            "WHERE id = 990000000000001",
            "DELETE FROM enterprise_security_artifact WHERE id = 990000000000001",
        ):
            with pytest.raises(DBAPIError, match="immutable"):
                connection.exec_driver_sql(statement)
            connection.rollback()


def test_mysql_rr_permission_revocation_uses_current_grant_at_report_commit(fresh_chain) -> None:
    """A pre-existing RR snapshot must not authorize a report commit after revoke."""
    from threading import Event
    from uuid import uuid4

    from sqlalchemy.orm import sessionmaker

    from app.models.rbac import Permission, Role, RolePermission, UserRole
    from app.models.user import User
    from app.services import rbac_service

    sessions = sessionmaker(bind=fresh_chain, expire_on_commit=False, autoflush=False)
    permission_code = f"test:rr_fence_{uuid4().hex[:20]}"
    with sessions.begin() as db:
        role = db.query(Role).filter_by(code="user", status="active").one()
        user = User(username=f"rr-permission-{uuid4().hex[:16]}", password="x", role="user", status=1)
        permission = Permission(code=permission_code, name="RR 权限撤销", module="test")
        db.add_all([user, permission])
        db.flush()
        user_id, role_id, permission_id = int(user.id), int(role.id), int(permission.id)
        db.add_all([
            UserRole(user_id=user_id, role_id=role_id),
            RolePermission(role_id=role_id, permission_id=permission_id),
        ])

    snapshot_ready = Event()
    revoke_committed = Event()

    def report_commit_check() -> bool:
        with sessions() as db:
            db.begin()
            prior_grant = db.query(RolePermission).filter_by(
                role_id=role_id, permission_id=permission_id,
            ).first()
            assert prior_grant is not None
            snapshot_ready.set()
            if not revoke_committed.wait(timeout=10):
                raise AssertionError("权限撤销事务未完成")
            allowed = rbac_service.check_permission_grant(db, user_id, permission_code)
            db.commit()
            return allowed

    def revoke_permission() -> None:
        if not snapshot_ready.wait(timeout=10):
            raise AssertionError("报告事务未建立旧 RR 快照")
        with sessions.begin() as db:
            grant = db.query(RolePermission).filter_by(
                role_id=role_id, permission_id=permission_id,
            ).one()
            db.delete(grant)
        revoke_committed.set()

    with ThreadPoolExecutor(max_workers=2) as pool:
        report_future = pool.submit(report_commit_check)
        revoke_future = pool.submit(revoke_permission)
        revoke_future.result(timeout=15)
        assert report_future.result(timeout=15) is False


def _seed_mysql_security_scan_task(fresh_chain, monkeypatch):
    """Create one real, project-scoped Sentinel task for MySQL transaction races."""
    import hashlib
    from datetime import datetime, timedelta, timezone
    from uuid import uuid4

    from sqlalchemy.orm import sessionmaker

    from app.models.enterprise_agent_team import (
        EnterpriseAgentTeam,
        EnterpriseAgentTeamMember,
        EnterpriseAgentTeamTask,
    )
    from app.models.enterprise_assistant_runtime import EnterpriseAssistantRuntimeState
    from app.models.enterprise_chat import EnterpriseAssistantRun, EnterpriseChatMessage, EnterpriseConversation
    from app.models.enterprise_org import (
        Enterprise,
        EnterpriseGroup,
        EnterpriseGroupMember,
        EnterpriseMember,
    )
    from app.models.enterprise_project import EnterpriseProject
    from app.models.project import Project
    from app.models.rbac import Permission, Role, RolePermission, UserRole
    from app.models.user import User
    from app.services import enterprise_agent_team_runtime_service as runtime

    # The parent assistant role snapshot is orthogonal to the task worker's live
    # project and security:scan gates. Keep the latter real in these races.
    from app.services import (
        enterprise_agent_team_service,
        enterprise_assistant_runtime_service,
        enterprise_assistant_service,
    )

    monkeypatch.setattr(enterprise_assistant_runtime_service, "_live_role_snapshot", lambda *_a, **_k: {})
    monkeypatch.setattr(enterprise_assistant_service, "_require_subscription", lambda *_a, **_k: None)
    monkeypatch.setattr(enterprise_assistant_service, "_require_active_limit", lambda *_a, **_k: None)

    suffix = uuid4().hex[:12]
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    plan = {
        "allowed_models": ["deepseek-flash"],
        "max_calls_per_task": 3,
        "max_input_tokens_per_call": 200,
        "max_output_tokens_per_call": 200,
        "max_team_credits_milli": 10_000,
        "risk_level": "low",
        "requires_approval": False,
        "project_id": None,
    }
    permission_snapshot = {"messages": []}
    _contract, capabilities = enterprise_agent_team_service._contract_snapshot("security_sentinel")
    sessions = sessionmaker(bind=fresh_chain, expire_on_commit=False, autoflush=False)

    with sessions.begin() as db:
        owner = User(username=f"sentinel-owner-{suffix}", password="x", role="user", status=1)
        initiator = User(username=f"sentinel-user-{suffix}", password="x", role="reviewer", status=1)
        db.add_all([owner, initiator])
        db.flush()
        enterprise = Enterprise(
            name=f"哨兵并发企业-{suffix}", owner_id=int(owner.id), seat_limit=5,
            status="active", billing_suspended=False, paid_order_id=f"sentinel-order-{suffix}",
        )
        db.add(enterprise)
        db.flush()
        group = EnterpriseGroup(enterprise_id=int(enterprise.id), name=f"哨兵小组-{suffix}", status="active")
        project = Project(user_id=int(initiator.id), project_name=f"哨兵项目-{suffix}", status="active")
        db.add_all([group, project])
        db.flush()

        conversation = EnterpriseConversation(
            enterprise_id=int(enterprise.id), kind="company", conversation_key="company",
            group_id=None, created_by=int(owner.id),
        )
        db.add(conversation)
        db.flush()
        message = EnterpriseChatMessage(
            enterprise_id=int(enterprise.id), conversation_id=int(conversation.id),
            sender_id=int(initiator.id), sender_type="human", client_message_id=f"source-{suffix}",
            content="@小菱 执行安全扫描",
        )
        db.add(message)
        db.flush()
        run = EnterpriseAssistantRun(
            public_id=f"sentinel-run-{suffix}", request_key=f"sentinel-request-{suffix}",
            enterprise_id=int(enterprise.id), conversation_id=int(conversation.id), group_id=None,
            initiator_user_id=int(initiator.id), source_message_id=int(message.id), status="running",
            attempt_count=1, prompt_sha256=hashlib.sha256(suffix.encode()).hexdigest(),
        )
        db.add(run)
        db.flush()
        runtime_state = EnterpriseAssistantRuntimeState(
            assistant_run_id=int(run.id), enterprise_id=int(enterprise.id),
            conversation_id=int(conversation.id), group_id=None, initiator_user_id=int(initiator.id),
            source_message_id=int(message.id), status="running", checkpoint_json="{}",
            permission_snapshot_json=runtime._canonical_json(permission_snapshot),
            permission_snapshot_sha256=runtime._digest(permission_snapshot), stop_requested=False,
            lease_generation=1, lease_expires_at=now + timedelta(hours=1),
            billing_invocation_count=1, version=1,
        )
        db.add(runtime_state)
        db.add_all([
            EnterpriseMember(enterprise_id=int(enterprise.id), user_id=int(owner.id), role="owner", status="active"),
            EnterpriseMember(
                enterprise_id=int(enterprise.id), user_id=int(initiator.id), role="member", status="active",
            ),
            EnterpriseGroupMember(
                enterprise_id=int(enterprise.id), group_id=int(group.id), user_id=int(initiator.id),
                role="leader", status="active",
            ),
            EnterpriseProject(
                enterprise_id=int(enterprise.id), project_id=int(project.id), group_id=int(group.id), status="active",
            ),
        ])
        db.flush()

        reviewer = db.query(Role).filter_by(code="reviewer").one()
        scan_permission = db.query(Permission).filter_by(code="security:scan").first()
        if scan_permission is None:
            scan_permission = Permission(code="security:scan", name="安全扫描", module="security")
            db.add(scan_permission)
            db.flush()
        if db.query(UserRole).filter_by(user_id=int(initiator.id), role_id=int(reviewer.id)).first() is None:
            db.add(UserRole(user_id=int(initiator.id), role_id=int(reviewer.id)))
        grant = db.query(RolePermission).filter_by(
            role_id=int(reviewer.id), permission_id=int(scan_permission.id),
        ).first()
        grant_preexisting = grant is not None
        if grant is None:
            grant = RolePermission(role_id=int(reviewer.id), permission_id=int(scan_permission.id))
            db.add(grant)
            db.flush()

        plan["project_id"] = int(project.id)
        team = EnterpriseAgentTeam(
            assistant_run_id=int(run.id), enterprise_id=int(enterprise.id), conversation_id=int(conversation.id),
            project_id=int(project.id), group_id=None, initiator_user_id=int(initiator.id),
            source_message_id=int(message.id), source_tool_call_id=f"call-{suffix}",
            title="项目安全扫描", objective="只读扫描企业绑定项目", status="queued",
            payer_owner_type="enterprise", payer_owner_id=int(enterprise.id), trace_id=f"trace-{suffix}",
            request_key_prefix=f"request-{suffix}", parent_lease_generation=1, lease_generation=0,
            plan_sha256=runtime._digest(plan), plan_snapshot_json=runtime._canonical_json(plan),
            permission_snapshot_sha256=runtime._digest(permission_snapshot),
            permission_snapshot_json=runtime._canonical_json(permission_snapshot), result_json="{}", error_json="{}",
        )
        db.add(team)
        db.flush()
        member = EnterpriseAgentTeamMember(
            team_id=int(team.id), member_key="sentinel", display_name="安全哨兵",
            address="agent:security_sentinel", kind="registered", role="worker",
            snapshot_sha256=runtime._digest(capabilities), capabilities_json=runtime._canonical_json(capabilities),
            status="created",
        )
        db.add(member)
        db.flush()
        task = EnterpriseAgentTeamTask(
            team_id=int(team.id), member_id=int(member.id), task_key="scan", title="项目扫描",
            instructions="按计划只读扫描", dependency_keys_json="[]", input_json="{}", status="queued",
            priority=0, attempt_count=0, max_attempts=3, lease_generation=0, result_json="{}",
            artifacts_json="[]", errors_json="[]",
        )
        db.add(task)
        db.flush()
        enterprise_id, owner_id, initiator_id = int(enterprise.id), int(owner.id), int(initiator.id)
        task_id, team_id, grant_id = int(task.id), int(team.id), int(grant.id)
        reviewer_role_id, scan_permission_id = int(reviewer.id), int(scan_permission.id)

    claim = runtime.claim_task(
        sessions, enterprise_id=enterprise_id, task_id=task_id, now=now, lease_seconds=10,
    )
    assert claim is not None and claim["status"] == "running"
    return {
        "sessions": sessions,
        "enterprise_id": enterprise_id,
        "owner_id": owner_id,
        "initiator_id": initiator_id,
        "task_id": task_id,
        "team_id": team_id,
        "grant_id": grant_id,
        "reviewer_role_id": reviewer_role_id,
        "scan_permission_id": scan_permission_id,
        "grant_preexisting": grant_preexisting,
        "now": now,
        "claim": claim,
        "permission_code": "security:scan",
    }


def _mysql_static_security_report(suffix: str) -> dict[str, object]:
    import hashlib

    return {
        "audit_run_id": f"mysql_scan_{suffix}",
        "source_sha256": hashlib.sha256(suffix.encode()).hexdigest(),
        "coverage_status": "complete",
        "termination_reason": "",
        "summary": {"summary": "MySQL 静态扫描报告", "finding_count": 0},
        "report": {"findings": [], "compliance": {"provider_request_count": 0}, "complete": True},
    }


@pytest.mark.parametrize("winner", ["cancel", "reclaim"])
def test_mysql_security_artifact_commit_is_fenced_by_cancel_and_reclaimed_lease(
    fresh_chain, monkeypatch, winner: str,
) -> None:
    """An old RR worker cannot publish after cancellation or a new lease generation."""
    from concurrent.futures import ThreadPoolExecutor
    from datetime import timedelta
    from threading import Event, current_thread

    from app.core.exceptions import ConflictError, ForbiddenError
    from app.models.enterprise_agent_team import EnterpriseAgentTeamTask
    from app.models.enterprise_security_artifact import EnterpriseSecurityArtifact
    from app.services import enterprise_agent_team_runtime_service as runtime
    from app.services import enterprise_org_service

    seeded = _seed_mysql_security_scan_task(fresh_chain, monkeypatch)
    sessions = seeded["sessions"]
    worker_entered = Event()
    winner_committed = Event()
    real_company = enterprise_org_service._company

    def pause_stale_worker_before_lock(db, enterprise_id, *, lock=False):
        if current_thread().name == "sentinel-late-report":
            worker_entered.set()
            if not winner_committed.wait(timeout=15):
                raise AssertionError("取消或新租约事务没有完成")
        return real_company(db, enterprise_id, lock=lock)

    monkeypatch.setattr(enterprise_org_service, "_company", pause_stale_worker_before_lock)

    def write_late_report() -> str:
        current_thread().name = "sentinel-late-report"
        try:
            runtime.complete_task(
                sessions,
                enterprise_id=seeded["enterprise_id"],
                task_id=seeded["task_id"],
                lease_token=seeded["claim"]["lease_token"],
                lease_generation=seeded["claim"]["lease_generation"],
                result={"status": "completed"},
                security_artifact=_mysql_static_security_report("late" + winner),
                now=seeded["now"] + timedelta(seconds=1),
            )
        except (ConflictError, ForbiddenError) as exc:
            return type(exc).__name__
        return "committed"

    def supersede_worker() -> str:
        assert worker_entered.wait(timeout=15), "迟到 worker 未建立 MySQL 一致性快照"
        if winner == "cancel":
            result = runtime.cancel_team(
                sessions,
                enterprise_id=seeded["enterprise_id"],
                team_id=seeded["team_id"],
                actor_user_id=seeded["initiator_id"],
                reason="MySQL Sentinel 取消竞态",
            )
            winner_committed.set()
            return str(result["status"])

        # The claimed task is now past its short lease, while the assistant parent
        # remains live. A fresh claimant receives a new attempt and fence generation.
        expired_at = seeded["claim"]["lease_expires_at"]
        from datetime import datetime

        new_claim = runtime.claim_task(
            sessions,
            enterprise_id=seeded["enterprise_id"],
            task_id=seeded["task_id"],
            now=datetime.fromisoformat(expired_at) + timedelta(seconds=1),
            lease_seconds=30,
        )
        assert new_claim is not None and new_claim["status"] == "running"
        assert new_claim["lease_generation"] > seeded["claim"]["lease_generation"]
        winner_committed.set()
        return "reclaimed"

    with ThreadPoolExecutor(max_workers=2) as pool:
        late = pool.submit(write_late_report)
        winner_future = pool.submit(supersede_worker)
        assert winner_future.result(timeout=30) in {"cancelled", "reclaimed"}
        assert late.result(timeout=30) in {"ConflictError", "ForbiddenError"}

    with sessions() as db:
        task = db.get(EnterpriseAgentTeamTask, seeded["task_id"])
        assert db.query(EnterpriseSecurityArtifact).filter_by(task_id=seeded["task_id"]).count() == 0
        assert task.result_json == "{}"
        if winner == "cancel":
            assert task.status == "cancelled"
        else:
            assert task.status == "running"
            assert task.attempt_count == 2
            assert task.lease_generation == 2


def test_mysql_security_artifact_commit_observes_revoked_scan_grant_after_rr_snapshot(
    fresh_chain, monkeypatch,
) -> None:
    """The complete report transaction must reject a grant revoked after its RR snapshot."""
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event, current_thread

    from app.core.exceptions import ForbiddenError
    from app.models.enterprise_agent_team import EnterpriseAgentTeamTask
    from app.models.enterprise_security_artifact import EnterpriseSecurityArtifact
    from app.models.rbac import RolePermission
    from app.services import enterprise_agent_team_runtime_service as runtime
    from app.services import rbac_service

    seeded = _seed_mysql_security_scan_task(fresh_chain, monkeypatch)
    sessions = seeded["sessions"]
    stale_snapshot_ready = Event()
    revoke_committed = Event()
    real_check = rbac_service.check_permission_grant

    def establish_old_snapshot_then_check_current_grant(db, user_id, permission_code):
        if current_thread().name == "sentinel-report-commit":
            visible_before_revoke = db.query(RolePermission).filter_by(id=seeded["grant_id"]).first()
            assert visible_before_revoke is not None
            stale_snapshot_ready.set()
            if not revoke_committed.wait(timeout=15):
                raise AssertionError("security:scan 撤权事务没有完成")
        return real_check(db, user_id, permission_code)

    monkeypatch.setattr(rbac_service, "check_permission_grant", establish_old_snapshot_then_check_current_grant)

    def commit_report() -> str:
        current_thread().name = "sentinel-report-commit"
        try:
            runtime.complete_task(
                sessions,
                enterprise_id=seeded["enterprise_id"],
                task_id=seeded["task_id"],
                lease_token=seeded["claim"]["lease_token"],
                lease_generation=seeded["claim"]["lease_generation"],
                result={"status": "completed"},
                security_artifact=_mysql_static_security_report("revoked"),
                now=seeded["now"],
            )
        except ForbiddenError:
            return "revoked"
        return "committed"

    def revoke_grant() -> None:
        assert stale_snapshot_ready.wait(timeout=15), "报告事务未建立可见的旧 RBAC 快照"
        with sessions.begin() as db:
            grant = db.get(RolePermission, seeded["grant_id"])
            assert grant is not None
            db.delete(grant)
        revoke_committed.set()

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            report_future = pool.submit(commit_report)
            revoke_future = pool.submit(revoke_grant)
            revoke_future.result(timeout=30)
            assert report_future.result(timeout=30) == "revoked"
    finally:
        # The migration test database is shared by this module's cases. Restore a
        # seeded global role grant so this race test cannot alter later assertions.
        if seeded["grant_preexisting"]:
            with sessions.begin() as db:
                if db.query(RolePermission).filter_by(
                    role_id=seeded["reviewer_role_id"],
                    permission_id=seeded["scan_permission_id"],
                ).first() is None:
                    db.add(RolePermission(
                        role_id=seeded["reviewer_role_id"],
                        permission_id=seeded["scan_permission_id"],
                    ))

    with sessions() as db:
        task = db.get(EnterpriseAgentTeamTask, seeded["task_id"])
        assert task.status == "running"
        assert task.result_json == "{}"
        assert db.query(EnterpriseSecurityArtifact).filter_by(task_id=seeded["task_id"]).count() == 0


def test_mysql_security_artifact_commit_serializes_before_later_scan_grant_revoke(
    fresh_chain, monkeypatch,
) -> None:
    """A report that locks its live grant first commits before a concurrent revoke."""
    import time
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event, current_thread

    from app.models.enterprise_agent_team import EnterpriseAgentTeamTask
    from app.models.enterprise_security_artifact import EnterpriseSecurityArtifact
    from app.models.rbac import RolePermission
    from app.services import enterprise_agent_team_runtime_service as runtime
    from app.services import rbac_service

    seeded = _seed_mysql_security_scan_task(fresh_chain, monkeypatch)
    sessions = seeded["sessions"]
    report_holds_grant = Event()
    release_report = Event()
    revoke_attempting = Event()
    revoke_connection_id: list[int] = []
    real_check = rbac_service.check_permission_grant

    def hold_live_grant_until_report_commit(db, user_id, permission_code):
        allowed = real_check(db, user_id, permission_code)
        if current_thread().name == "sentinel-report-lock-holder":
            assert allowed is True
            report_holds_grant.set()
            if not release_report.wait(timeout=15):
                raise AssertionError("报告事务未获准完成")
        return allowed

    monkeypatch.setattr(rbac_service, "check_permission_grant", hold_live_grant_until_report_commit)

    def commit_report() -> str:
        current_thread().name = "sentinel-report-lock-holder"
        result = runtime.complete_task(
            sessions,
            enterprise_id=seeded["enterprise_id"],
            task_id=seeded["task_id"],
            lease_token=seeded["claim"]["lease_token"],
            lease_generation=seeded["claim"]["lease_generation"],
            result={"status": "completed"},
            security_artifact=_mysql_static_security_report("locked"),
            now=seeded["now"],
        )
        return str(result["status"])

    def revoke_after_report_lock() -> None:
        current_thread().name = "sentinel-grant-revoker"
        assert report_holds_grant.wait(timeout=15), "报告事务未先锁定 security:scan 授权"
        with sessions() as db:
            with db.begin():
                revoke_connection_id.append(int(db.execute(text("SELECT CONNECTION_ID()")).scalar_one()))
                grant = db.get(RolePermission, seeded["grant_id"])
                assert grant is not None
                db.delete(grant)
                revoke_attempting.set()
                db.flush()

    def revoke_is_waiting_for_mysql_lock() -> bool:
        if not revoke_connection_id:
            return False
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            with fresh_chain.connect() as connection:
                waiting = connection.execute(
                    text(
                        "SELECT COUNT(*) FROM information_schema.INNODB_TRX "
                        "WHERE trx_mysql_thread_id = :connection_id AND trx_state = 'LOCK WAIT'"
                    ),
                    {"connection_id": revoke_connection_id[0]},
                ).scalar_one()
            if int(waiting or 0) > 0:
                return True
            time.sleep(0.02)
        return False

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            report_future = pool.submit(commit_report)
            revoke_future = pool.submit(revoke_after_report_lock)
            assert revoke_attempting.wait(timeout=15), "撤权事务未执行删除语句"
            assert revoke_is_waiting_for_mysql_lock() is True, "MySQL 未观察到撤权等待报告授权行锁"
            release_report.set()
            assert report_future.result(timeout=30) == "completed"
            revoke_future.result(timeout=30)
    finally:
        release_report.set()
        if seeded["grant_preexisting"]:
            with sessions.begin() as db:
                if db.query(RolePermission).filter_by(
                    role_id=seeded["reviewer_role_id"],
                    permission_id=seeded["scan_permission_id"],
                ).first() is None:
                    db.add(RolePermission(
                        role_id=seeded["reviewer_role_id"],
                        permission_id=seeded["scan_permission_id"],
                    ))

    with sessions() as db:
        task = db.get(EnterpriseAgentTeamTask, seeded["task_id"])
        artifacts = db.query(EnterpriseSecurityArtifact).filter_by(task_id=seeded["task_id"]).all()
        assert task.status == "completed"
        assert len(artifacts) == 1


def test_mysql_enterprise_team_project_scope_is_nullable_and_tenant_bound(fresh_chain) -> None:
    """Migration 097 must bind a team's optional project to the same tenant."""
    with fresh_chain.connect() as connection:
        project_column = connection.execute(
            text(
                "SELECT IS_NULLABLE, DATA_TYPE FROM information_schema.COLUMNS "
                "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'enterprise_agent_team' "
                "AND COLUMN_NAME = 'project_id'"
            )
        ).mappings().one()
        assert project_column["IS_NULLABLE"] == "YES"
        assert project_column["DATA_TYPE"] == "bigint"

        fk_rows = connection.execute(
            text(
                "SELECT CONSTRAINT_NAME, COLUMN_NAME, REFERENCED_COLUMN_NAME "
                "FROM information_schema.KEY_COLUMN_USAGE "
                "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'enterprise_agent_team' "
                "AND CONSTRAINT_NAME = 'fk_ent_agent_team_project_scope' "
                "AND REFERENCED_TABLE_NAME IS NOT NULL"
            )
        ).mappings().all()
        assert {
            (str(row["COLUMN_NAME"]), str(row["REFERENCED_COLUMN_NAME"]))
            for row in fk_rows
        } == {("enterprise_id", "enterprise_id"), ("project_id", "project_id")}

        project_index = connection.execute(
            text(
                "SELECT GROUP_CONCAT(COLUMN_NAME ORDER BY SEQ_IN_INDEX) "
                "FROM information_schema.STATISTICS "
                "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'enterprise_agent_team' "
                "AND INDEX_NAME = 'ix_ent_agent_team_project'"
            )
        ).scalar_one()
        assert project_index == "enterprise_id,project_id,id"


def test_model_reservation_reconciliation_allows_only_one_concurrent_approval(
    fresh_chain, verified_model_catalog,
) -> None:
    from app.models.billing_ledger import BillingCreditBatch, BillingReservation, BillingReservationReconciliationEvent
    from app.services import billing_reservation_reconciliation_service as reconciliation
    from app.services.billing_ledger_service import (
        BillingProviderIdentity,
        get_or_create_wallet,
        grant,
        mark_unknown,
        reserve,
    )

    sessions = sessionmaker(bind=fresh_chain, autoflush=False, expire_on_commit=False)
    request_key = f"mysql-reconcile-race-{secrets.token_urlsafe(12)}"
    provider = BillingProviderIdentity(
        identity_version="deepseek-official-v1", provider_name="deepseek",
        endpoint_host="api.deepseek.com", credential_source="system",
    )
    with sessions.begin() as db:
        wallet = get_or_create_wallet(db, "user", 9_001_001)
        grant(db, wallet_id=wallet.id, grant_key=f"grant:{request_key}", kind="purchase", amount_milli=100_000)
        reservation = reserve(
            db, request_key=request_key, model_name="deepseek-flash", user_id=9_001_001,
            max_milli=80_000, personal_wallet_id=wallet.id, provider_identity=provider,
        )
        mark_unknown(db, request_key, server_tool_search_calls=0, server_tool_fetch_calls=0)
        proposal = reconciliation.propose_reconciliation(
            db, reservation_id=reservation.id, actor_id=9_001_010, resolution="settle",
            provider_request_id=f"provider-{secrets.token_urlsafe(8)}", input_tokens=10_000,
            output_tokens=5_000, evidence_reference=f"mysql-case-{request_key}",
            evidence_sha256="c" * 64, note="本地 MySQL 锁序并发测试",
        )
        reservation_id, proposal_id, wallet_id = reservation.id, proposal.id, wallet.id

    start = Barrier(3)

    def approve(actor_id: int) -> str:
        with sessions() as db:
            start.wait(timeout=10)
            try:
                reconciliation.decide_reconciliation(
                    db, reservation_id=reservation_id, actor_id=actor_id,
                    decision="approve", note=f"第二人复核 #{actor_id}",
                )
                db.commit()
                return "approved"
            except reconciliation.ReconciliationBlocked:
                db.commit()
                return "blocked"
            except reconciliation.ReconciliationError:
                db.rollback()
                return "rejected"

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(approve, 9_001_011)
        second = pool.submit(approve, 9_001_012)
        start.wait(timeout=10)
        outcomes = [first.result(timeout=30), second.result(timeout=30)]

    with sessions() as db:
        row = db.query(BillingReservation).filter_by(id=reservation_id).one()
        events = reconciliation.list_reconciliation_events(db, reservation_id=reservation_id)
        batch = db.query(BillingCreditBatch).filter_by(wallet_id=wallet_id).one()
        assert row.status == "settled" and row.actual_milli == 30_000
        assert outcomes.count("approved") == 1
        assert outcomes.count("blocked") == 1
        assert [event.decision for event in events].count("proposed") == 1
        assert [event.decision for event in events].count("approved") == 1
        assert [event.decision for event in events].count("blocked") == 1
        assert events[0].id == proposal_id
        assert (batch.remaining_milli, batch.reserved_milli) == (70_000, 0)
        assert db.query(BillingReservationReconciliationEvent).filter_by(
            reservation_id=reservation_id, decision="approved",
        ).count() == 1


def test_mysql_team_asset_c4_release_allows_claim_and_fences_late_child_worker(
    fresh_chain, monkeypatch, verified_model_catalog,
) -> None:
    """团队派生 child 经 MySQL 双人未发送释放后可显式恢复，旧 worker 不能写入。"""
    import hashlib
    import json
    from datetime import datetime, timedelta, timezone
    from threading import Barrier
    from types import SimpleNamespace

    from sqlalchemy.orm import sessionmaker

    from app.core.exceptions import ConflictError, PaymentRequiredError
    from app.models.billing_ledger import (
        BillingReservation,
        BillingReservationReconciliationEvent,
    )
    from app.models.enterprise_asset import (
        EnterpriseAssetDraft,
        EnterpriseAssetDraftVersion,
        EnterpriseAssetPublication,
        EnterpriseAssistantAssetInvocation,
    )
    from app.models.enterprise_assistant_runtime import EnterpriseAssistantRuntimeState
    from app.models.enterprise_chat import EnterpriseAssistantRun
    from app.services import billing_reservation_reconciliation_service as reconciliation
    from app.services import enterprise_assistant_runtime_service as runtime
    from app.services.billing_ledger_service import (
        BillingProviderIdentity,
        get_or_create_wallet,
        grant,
        mark_unknown,
        quote_reservation_milli,
        reserve,
        settle,
    )
    from app.services.deepseek_responses_runtime import RunCheckpoint

    suffix = secrets.token_urlsafe(10)
    enterprise_id = 9_030_001
    conversation_id = 9_030_002
    initiator_id = 9_030_003
    run_public_id = f"ear-mysql-team-c4-{suffix}"
    parent_call_id = f"team-parent-{suffix}"
    invocation_public_id = f"asset-invocation-{suffix}"
    parent_request_key = f"enterprise-assistant:{run_public_id}:1:parent"
    child_root = f"enterprise-asset:{invocation_public_id}"
    child_request_key = f"{child_root}:generation:1:1"
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    provider = BillingProviderIdentity(
        identity_version="deepseek-official-v1",
        provider_name="deepseek",
        endpoint_host="api.deepseek.com",
        credential_source="system",
    )
    task = "按验收边界整理差异"
    steps = [{"publication_id": 1, "task": task}]
    parent_call = {
        "type": "function_call",
        "call_id": parent_call_id,
        "name": "run_approved_enterprise_asset_team",
        "arguments": json.dumps({"objective": "检查恢复边界", "steps": steps}),
    }
    child_call_id = "team_" + hashlib.sha256(
        f"{run_public_id}:{parent_call_id}:1".encode("utf-8")
    ).hexdigest()[:48]
    checkpoint = RunCheckpoint(
        run_id=run_public_id,
        model="deepseek-flash",
        transcript=[{"role": "user", "content": "检查恢复"}, parent_call],
        tools=[],
        status="failed",
        last_response={"status": "completed", "output": [parent_call]},
    )
    sessions = sessionmaker(bind=fresh_chain, autoflush=False, expire_on_commit=False)

    with sessions.begin() as db:
        wallet = get_or_create_wallet(db, "enterprise", enterprise_id)
        grant(
            db,
            wallet_id=wallet.id,
            grant_key=f"mysql-team-c4-grant-{suffix}",
            kind="purchase",
            amount_milli=50_000,
        )
        draft = EnterpriseAssetDraft(
            enterprise_id=enterprise_id,
            creator_id=initiator_id,
            kind="agent",
            title="MySQL C4 用例",
            definition_json=json.dumps({"schema_version": 1, "prompt": "返回可验证文本"}),
            current_version=1,
            request_key=f"mysql-team-c4-draft-{suffix}",
            request_digest=hashlib.sha256(suffix.encode()).hexdigest(),
            status="active",
        )
        db.add(draft)
        db.flush()
        version = EnterpriseAssetDraftVersion(
            draft_id=draft.id,
            version_number=1,
            title=draft.title,
            definition_json=draft.definition_json,
            request_key=f"mysql-team-c4-version-{suffix}",
            request_digest=hashlib.sha256((suffix + "v").encode()).hexdigest(),
        )
        db.add(version)
        db.flush()
        publication = EnterpriseAssetPublication(
            enterprise_id=enterprise_id,
            draft_id=draft.id,
            version_id=version.id,
            version_number=1,
            requested_by=initiator_id,
            kind="agent",
            scope="enterprise",
            status="approved",
            title=draft.title,
            definition_json=draft.definition_json,
            content_sha256=hashlib.sha256(b"mysql-team-c4-publication").hexdigest(),
            request_key=f"mysql-team-c4-publication-{suffix}",
            request_digest=hashlib.sha256((suffix + "p").encode()).hexdigest(),
        )
        db.add(publication)
        db.flush()
        parent_call["arguments"] = json.dumps(
            {"objective": "检查恢复边界", "steps": [{"publication_id": publication.id, "task": task}]}
        )
        checkpoint.transcript[-1] = parent_call
        checkpoint.last_response["output"] = [parent_call]
        run = EnterpriseAssistantRun(
            public_id=run_public_id,
            request_key=f"mysql-team-c4-run-{suffix}",
            enterprise_id=enterprise_id,
            conversation_id=conversation_id,
            initiator_user_id=initiator_id,
            source_message_id=9_030_004,
            status="awaiting_reconciliation",
            attempt_count=1,
            model_name="deepseek-flash",
            prompt_sha256=hashlib.sha256(suffix.encode()).hexdigest(),
        )
        db.add(run)
        db.flush()
        state = EnterpriseAssistantRuntimeState(
            assistant_run_id=run.id,
            enterprise_id=enterprise_id,
            conversation_id=conversation_id,
            initiator_user_id=initiator_id,
            source_message_id=run.source_message_id,
            status="awaiting_reconciliation",
            checkpoint_json=runtime._json(checkpoint.to_dict()),
            permission_snapshot_json="{}",
            permission_snapshot_sha256=hashlib.sha256(b"{}").hexdigest(),
            lease_generation=1,
            lease_expires_at=None,
            billing_invocation_count=1,
            version=1,
            error_code="provider_attempt_pending_reconciliation",
        )
        db.add(state)
        db.flush()
        reserve(
            db,
            request_key=parent_request_key,
            model_name="deepseek-flash",
            user_id=initiator_id,
            enterprise_wallet_id=wallet.id,
            enterprise_id=enterprise_id,
            max_milli=10_000,
            provider_identity=provider,
        )
        settle(
            db,
            request_key=parent_request_key,
            actual_milli=quote_reservation_milli(
                db,
                request_key=parent_request_key,
                input_tokens=10,
                output_tokens=10,
            ),
            input_tokens=10,
            output_tokens=10,
            server_tool_search_calls=0,
            server_tool_fetch_calls=0,
        )
        child_reservation = reserve(
            db,
            request_key=child_request_key,
            model_name="deepseek-flash",
            user_id=initiator_id,
            enterprise_wallet_id=wallet.id,
            enterprise_id=enterprise_id,
            max_milli=10_000,
            provider_identity=provider,
        )
        mark_unknown(db, child_request_key)
        proposal = reconciliation.propose_reconciliation(
            db,
            reservation_id=child_reservation.id,
            actor_id=9_030_010,
            resolution="release_not_sent",
            evidence_reference=f"mysql-team-c4-{suffix}",
            evidence_sha256=hashlib.sha256((suffix + "evidence").encode()).hexdigest(),
            note="MySQL C4 双人审批竞争回归",
            provider_request_id=None,
            input_tokens=None,
            output_tokens=None,
        )
        invocation = EnterpriseAssistantAssetInvocation(
            enterprise_id=enterprise_id,
            conversation_id=conversation_id,
            assistant_run_id=run.id,
            initiator_user_id=initiator_id,
            tool_call_id=child_call_id,
            invocation_id=invocation_public_id,
            publication_id=publication.id,
            publication_version_id=version.id,
            publication_sha256=publication.content_sha256,
            input_sha256=hashlib.sha256(task.encode()).hexdigest(),
            request_key_prefix=child_root,
            status="running",
            execution_token="mysql-c4-old-execution-token",
            lease_expires_at=now - timedelta(minutes=1),
        )
        db.add(invocation)
        db.flush()
        run_id = int(run.id)
        state_id = int(state.id)
        invocation_id = int(invocation.id)
        reservation_id = int(child_reservation.id)
        proposal_id = int(proposal.id)

    start = Barrier(3)

    def approve(actor_id: int) -> str:
        with sessions() as db:
            start.wait(timeout=10)
            try:
                reconciliation.decide_reconciliation(
                    db,
                    reservation_id=reservation_id,
                    actor_id=actor_id,
                    decision="approve",
                    note=f"独立复核 #{actor_id}",
                )
                db.commit()
                return "approved"
            except reconciliation.ReconciliationBlocked:
                db.commit()
                return "blocked"
            except reconciliation.ReconciliationError:
                db.rollback()
                return "rejected"

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(approve, 9_030_011)
        second = pool.submit(approve, 9_030_012)
        start.wait(timeout=10)
        outcomes = [first.result(timeout=30), second.result(timeout=30)]

    with sessions() as db:
        payload = runtime._status_payload(
            db,
            db.get(EnterpriseAssistantRun, run_id),
            db.get(EnterpriseAssistantRuntimeState, state_id),
        )
        child_reservation = db.get(BillingReservation, reservation_id)
        events = reconciliation.list_reconciliation_events(db, reservation_id=reservation_id)
        assert payload["status"] == "retryable"
        assert payload["recoverable"] is True
        assert child_reservation.status == "released"
        assert outcomes.count("approved") == 1
        assert outcomes.count("blocked") == 1
        assert sum(event.decision == "approved" for event in events) == 1
        assert sum(event.decision == "blocked" for event in events) == 1
        assert any(event.id == proposal_id and event.decision == "proposed" for event in events)

    monkeypatch.setattr(runtime, "_require_enterprise_active", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        runtime.enterprise_chat_service,
        "get_conversation",
        lambda *_args, **_kwargs: SimpleNamespace(kind="company"),
    )
    monkeypatch.setattr(runtime, "_live_role_snapshot", lambda *_args, **_kwargs: {})
    claim_start = Barrier(3)

    def claim_new_generation() -> str:
        with sessions.begin() as db:
            claim_start.wait(timeout=10)
            runtime._claim_new_invocation(
                db,
                enterprise_id=enterprise_id,
                conversation_id=conversation_id,
                run_public_id=run_public_id,
                allow_statuses=runtime._RETRYABLE | {"queued"},
                actor_user_id=initiator_id,
            )
        return "claimed"

    def late_worker_finish() -> str:
        claim_start.wait(timeout=10)
        try:
            runtime._finish_asset_invocation(
                sessions,
                invocation_id=invocation_id,
                execution_token="mysql-c4-old-execution-token",
                lease_generation=1,
                status="completed",
                output_text="迟到的旧 worker 结果",
                error_code=None,
            )
        except (ConflictError, PaymentRequiredError) as exc:
            return type(exc).__name__
        return "committed"

    with ThreadPoolExecutor(max_workers=2) as pool:
        claim_future = pool.submit(claim_new_generation)
        finish_future = pool.submit(late_worker_finish)
        claim_start.wait(timeout=10)
        claim_result = claim_future.result(timeout=30)
        finish_result = finish_future.result(timeout=30)
    assert claim_result == "claimed"
    assert finish_result in {"ConflictError", "PaymentRequiredError"}

    try:
        runtime._finish_asset_invocation(
            sessions,
            invocation_id=invocation_id,
            execution_token="mysql-c4-old-execution-token",
            lease_generation=1,
            status="completed",
            output_text="迟到的旧 worker 结果",
            error_code=None,
        )
        raise AssertionError("旧 generation worker 必须被父代际 fence 明确拒绝")
    except ConflictError:
        pass

    runtime.EnterpriseAssistantToolExecutor._mark_asset_invocation(
        sessions,
        invocation_id,
        "unknown",
        "late_old_worker",
        execution_token="mysql-c4-old-execution-token",
        lease_generation=1,
    )
    with sessions() as db:
        state = db.get(EnterpriseAssistantRuntimeState, state_id)
        invocation = db.get(EnterpriseAssistantAssetInvocation, invocation_id)
        assert state.lease_generation == 2
        assert invocation.status == "running"
        assert invocation.output_text is None
        assert invocation.execution_token == "mysql-c4-old-execution-token"
        assert db.query(BillingReservationReconciliationEvent).filter_by(
            reservation_id=reservation_id,
            decision="approved",
        ).count() == 1


def test_enterprise_team_calls_allow_parallelism_while_revocation_cancel_and_lifecycle_changes(
    fresh_chain,
    monkeypatch,
    verified_model_catalog,
) -> None:
    """一条企业 Agent 团队模型请求等待上游时，不应锁住同企业的其他团队请求。"""
    from datetime import datetime, timedelta, timezone
    from threading import Event, Thread, current_thread
    from uuid import uuid4

    from sqlalchemy import event
    from sqlalchemy.orm import sessionmaker

    from app.models.billing_ledger import (
        BillingLimit,
        BillingOrder,
        BillingPaymentEvent,
        BillingRefund,
        BillingSubscription,
        BillingSubscriptionPeriod,
    )
    from app.models.enterprise_agent_team import (
        EnterpriseAgentTeam,
        EnterpriseAgentTeamMember,
        EnterpriseAgentTeamTask,
    )
    from app.models.enterprise_assistant_runtime import EnterpriseAssistantRuntimeState
    from app.models.enterprise_chat import (
        EnterpriseAssistantRun,
        EnterpriseChatMessage,
        EnterpriseConversation,
    )
    from app.models.enterprise_org import Enterprise, EnterpriseGroup, EnterpriseGroupMember, EnterpriseMember
    from app.models.user import User
    from app.services import billing_ledger_service as ledger
    from app.services import enterprise_agent_team_runtime_service as team_runtime
    from app.services import (
        enterprise_assistant_runtime_service,
        enterprise_assistant_service,
        enterprise_chat_service,
        enterprise_org_service,
    )

    suffix = uuid4().hex[:12]
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    sessions = sessionmaker(bind=fresh_chain, expire_on_commit=False, autoflush=False)
    plan = {
        "allowed_models": ["deepseek-flash"],
        "max_calls_per_task": 10,
        "max_input_tokens_per_call": 200,
        "max_output_tokens_per_call": 200,
        "max_team_credits_milli": 10_000,
        "risk_level": "low",
        "requires_approval": False,
    }
    permission = {"messages": []}
    capability = {"execution_mode": "model_only", "tools": [], "external_effects": False}

    real_live_role_snapshot = enterprise_assistant_runtime_service._live_role_snapshot
    monkeypatch.setattr(enterprise_assistant_service, "_require_active_limit", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(enterprise_assistant_runtime_service, "_live_role_snapshot", lambda *_args, **_kwargs: {})

    with sessions.begin() as db:
        owner = User(username=f"team-lock-owner-{suffix}", password="x", role="user", status=1)
        first_user = User(username=f"team-lock-first-{suffix}", password="x", role="user", status=1)
        second_user = User(username=f"team-lock-second-{suffix}", password="x", role="user", status=1)
        third_user = User(username=f"team-lock-third-{suffix}", password="x", role="user", status=1)
        db.add_all([owner, first_user, second_user, third_user])
        db.flush()

        company = Enterprise(
            name=f"并发测试企业-{suffix}",
            owner_id=int(owner.id),
            seat_limit=4,
            status="active",
            billing_suspended=False,
            paid_order_id=f"team-lock-order-{suffix}",
        )
        db.add(company)
        db.flush()
        enterprise_id = int(company.id)
        owner_id = int(owner.id)
        order_no = str(company.paid_order_id)
        db.add_all(
            [
                BillingOrder(
                    order_no=order_no,
                    user_id=owner_id,
                    enterprise_id=enterprise_id,
                    product="enterprise_create",
                    amount_fen=100,
                    quantity=4,
                    channel="alipay",
                    status="paid",
                    payment_status="paid",
                    created_at=now,
                    paid_at=now,
                ),
                BillingPaymentEvent(
                    channel="alipay",
                    trade_id=f"team-lock-trade-{suffix}",
                    order_no=order_no,
                    amount_fen=100,
                    verified_at=now,
                ),
            ]
        )
        subscription = BillingSubscription(
            owner_type="enterprise",
            owner_id=enterprise_id,
            product="enterprise",
            anchor_at=now,
            last_end_at=now + timedelta(days=30),
            next_offset=1,
        )
        db.add(subscription)
        db.flush()
        period = BillingSubscriptionPeriod(
            subscription_id=int(subscription.id),
            order_no=order_no,
            starts_at=now - timedelta(minutes=1),
            ends_at=now + timedelta(days=30),
            credit_milli=1_000_000,
            seats=4,
            granted_at=now,
        )
        refund = BillingRefund(
            order_no=order_no,
            refund_no=f"team-lock-refund-{suffix}",
            amount_fen=100,
            reason="企业团队模型并发退款回归",
            status="requested",
            requested_by=owner_id,
            created_at=now,
            updated_at=now,
        )
        db.add_all([period, refund])
        db.flush()
        subscription_period_id = int(period.id)
        refund_id = int(refund.id)
        first_user_id, second_user_id, third_user_id = (
            int(first_user.id),
            int(second_user.id),
            int(third_user.id),
        )
        db.add_all(
            [
                EnterpriseMember(enterprise_id=enterprise_id, user_id=int(owner.id), role="owner", status="active"),
                EnterpriseMember(enterprise_id=enterprise_id, user_id=first_user_id, role="member", status="active"),
                EnterpriseMember(enterprise_id=enterprise_id, user_id=second_user_id, role="member", status="active"),
                EnterpriseMember(enterprise_id=enterprise_id, user_id=third_user_id, role="member", status="active"),
            ]
        )
        group = EnterpriseGroup(enterprise_id=enterprise_id, name="并发小组", status="active")
        db.add(group)
        db.flush()
        group_id = int(group.id)
        db.add(
            BillingLimit(
                limit_key=f"team-lock-limit-{suffix}",
                enterprise_id=enterprise_id,
                category="normal",
                group_id=group_id,
                period_start=now - timedelta(days=1),
                period_end=now + timedelta(days=30),
                limit_milli=1_000_000,
            )
        )
        db.add_all(
            [
                EnterpriseGroupMember(
                    enterprise_id=enterprise_id,
                    group_id=group_id,
                    user_id=first_user_id,
                    role="member",
                    status="active",
                ),
                EnterpriseGroupMember(
                    enterprise_id=enterprise_id,
                    group_id=group_id,
                    user_id=second_user_id,
                    role="member",
                    status="active",
                ),
                EnterpriseGroupMember(
                    enterprise_id=enterprise_id,
                    group_id=group_id,
                    user_id=third_user_id,
                    role="member",
                    status="active",
                ),
            ]
        )
        db.flush()
        room = EnterpriseConversation(
            enterprise_id=enterprise_id,
            kind="group",
            conversation_key=f"group:{group_id}",
            group_id=group_id,
            created_by=int(owner.id),
        )
        db.add(room)
        db.flush()
        conversation_id = int(room.id)

        task_ids: list[int] = []
        team_ids: list[int] = []
        for index, initiator_id in enumerate((first_user_id, second_user_id, third_user_id), start=1):
            source_id = 900_000_000 + index
            run = EnterpriseAssistantRun(
                public_id=f"team-lock-run-{suffix}-{index}",
                request_key=f"team-lock-request-{suffix}-{index}",
                enterprise_id=enterprise_id,
                conversation_id=conversation_id,
                group_id=group_id,
                initiator_user_id=initiator_id,
                source_message_id=source_id,
                status="completed",
                attempt_count=1,
                prompt_sha256="p" * 64,
            )
            db.add(run)
            db.flush()
            db.add(
                EnterpriseChatMessage(
                    id=source_id,
                    enterprise_id=enterprise_id,
                    conversation_id=conversation_id,
                    sender_id=initiator_id,
                    sender_type="human",
                    client_message_id=f"team-lock-message-{suffix}-{index}",
                    content="@小菱 执行只读任务",
                )
            )
            db.add(
                EnterpriseAssistantRuntimeState(
                    assistant_run_id=int(run.id),
                    enterprise_id=enterprise_id,
                    conversation_id=conversation_id,
                    group_id=group_id,
                    initiator_user_id=initiator_id,
                    source_message_id=source_id,
                    status="completed",
                    checkpoint_json="{}",
                    permission_snapshot_json=team_runtime._canonical_json(permission),
                    permission_snapshot_sha256=team_runtime._digest(permission),
                    stop_requested=False,
                    lease_generation=1,
                    lease_expires_at=None,
                    billing_invocation_count=0,
                    version=1,
                )
            )
            db.flush()
            team = EnterpriseAgentTeam(
                assistant_run_id=int(run.id),
                enterprise_id=enterprise_id,
                conversation_id=conversation_id,
                group_id=group_id,
                initiator_user_id=initiator_id,
                source_message_id=source_id,
                source_tool_call_id=f"team-lock-call-{suffix}-{index}",
                title=f"并发任务{index}",
                objective="模型并发锁回归",
                status="queued",
                payer_owner_type="enterprise",
                payer_owner_id=enterprise_id,
                trace_id=f"team-lock-trace-{suffix}-{index}",
                request_key_prefix=f"team-lock-key-{suffix}-{index}",
                parent_lease_generation=1,
                lease_generation=0,
                plan_sha256=team_runtime._digest(plan),
                plan_snapshot_json=team_runtime._canonical_json(plan),
                permission_snapshot_sha256=team_runtime._digest(permission),
                permission_snapshot_json=team_runtime._canonical_json(permission),
                result_json="{}",
                error_json="{}",
            )
            db.add(team)
            db.flush()
            team_ids.append(int(team.id))
            member = EnterpriseAgentTeamMember(
                team_id=int(team.id),
                member_key="worker",
                display_name="工作 Agent",
                address="agent:worker",
                kind="registered",
                role="worker",
                snapshot_sha256=team_runtime._digest(capability),
                capabilities_json=team_runtime._canonical_json(capability),
                status="created",
            )
            db.add(member)
            db.flush()
            task = EnterpriseAgentTeamTask(
                team_id=int(team.id),
                member_id=int(member.id),
                task_key="work",
                title="执行任务",
                instructions="只执行无工具模型任务",
                dependency_keys_json="[]",
                input_json="{}",
                status="queued",
                priority=0,
                attempt_count=0,
                max_attempts=3,
                lease_generation=0,
                result_json="{}",
                artifacts_json="[]",
                errors_json="[]",
            )
            db.add(task)
            db.flush()
            task_ids.append(int(task.id))

        wallet = ledger.get_or_create_wallet(db, "enterprise", enterprise_id)
        ledger.grant(
            db,
            wallet_id=int(wallet.id),
            grant_key=f"team-lock-credit-{suffix}",
            kind="purchase",
            amount_milli=20_000,
        )
    claims = [
        team_runtime.claim_task(
            sessions,
            enterprise_id=enterprise_id,
            task_id=task_id,
            now=now,
        )
        for task_id in task_ids
    ]
    assert all(claim is not None for claim in claims)

    provider = ledger.BillingProviderIdentity(
        identity_version="deepseek-official-v1",
        provider_name="deepseek",
        endpoint_host="api.deepseek.com",
        credential_source="system",
    )
    first_send_entered = Event()
    release_first_send = Event()
    second_send_entered = Event()
    revocation_waiting_on_member = Event()
    revocation_finished = Event()
    cancel_send_entered = Event()
    release_cancel_send = Event()
    independent_call_entered = Event()
    cancel_run_lock_sent = Event()
    cancel_finished = Event()
    outcomes: dict[str, object] = {}

    real_active_member_record = enterprise_org_service._active_member_record

    def _observe_revocation_lock(db, requested_enterprise_id, user_id, *, lock=False):
        if lock and int(user_id) == first_user_id and current_thread().name == "team-member-removal":
            revocation_waiting_on_member.set()
        return real_active_member_record(
            db,
            requested_enterprise_id,
            user_id,
            lock=lock,
        )

    monkeypatch.setattr(enterprise_org_service, "_active_member_record", _observe_revocation_lock)

    def _call(
        index: int,
        task_id: int,
        claim: dict[str, object],
        *,
        call_index: int = 1,
        started: Event | None = None,
        release: Event | None = None,
    ) -> None:
        try:

            def send():
                if started is not None:
                    started.set()
                if release is not None and not release.wait(timeout=10):
                    raise TimeoutError("测试未释放模拟模型请求")
                return f"result-{index}-{call_index}", {"prompt_tokens": 20, "completion_tokens": 10}

            outcomes[f"{index}:{call_index}"] = team_runtime.metered_team_call(
                session_factory=sessions,
                enterprise_id=enterprise_id,
                task_id=task_id,
                lease_token=str(claim["lease_token"]),
                lease_generation=int(claim["lease_generation"]),
                call_index=call_index,
                model_name="deepseek-flash",
                max_input_tokens=200,
                max_output_tokens=200,
                provider_identity=provider,
                send=send,
                now=now,
            )
        except Exception as exc:  # pragma: no cover - asserted through outcomes
            outcomes[f"{index}:{call_index}_error"] = exc

    def _remove_member() -> None:
        try:
            with sessions.begin() as db:
                enterprise_org_service.remove_group_member(
                    db,
                    enterprise_id,
                    group_id,
                    owner_id,
                    first_user_id,
                )
            outcomes["revocation"] = "removed"
        except Exception as exc:  # pragma: no cover - asserted through outcomes
            outcomes["revocation_error"] = exc
        finally:
            revocation_finished.set()

    def _cancel_team() -> None:
        try:
            outcomes["cancel"] = team_runtime.cancel_team(
                sessions,
                enterprise_id=enterprise_id,
                team_id=team_ids[2],
                actor_user_id=third_user_id,
                reason="MySQL 并发取消回归",
                now=now,
            )
        except Exception as exc:  # pragma: no cover - asserted through outcomes
            outcomes["cancel_error"] = exc
        finally:
            cancel_finished.set()

    first = Thread(
        target=_call,
        args=(1, task_ids[0], claims[0]),
        kwargs={"started": first_send_entered, "release": release_first_send},
        daemon=True,
    )
    second = Thread(
        target=_call,
        args=(2, task_ids[1], claims[1]),
        kwargs={"started": second_send_entered},
        daemon=True,
    )
    revoker = Thread(target=_remove_member, name="team-member-removal", daemon=True)
    try:
        first.start()
        assert first_send_entered.wait(timeout=10), "第一条模拟模型请求未进入 provider"
        revoker.start()
        assert revocation_waiting_on_member.wait(timeout=2), "成员撤权被整企业行锁挡住，未到达目标成员锁"
        second.start()
        assert second_send_entered.wait(timeout=2), "第一条上游请求或成员撤权占用了整个企业的排它锁"
        assert not revocation_finished.wait(timeout=0.1), "成员撤权越过了仍在执行的用户请求"
    finally:
        release_first_send.set()
        first.join(timeout=12)
        if second.ident is not None:
            second.join(timeout=12)
        if revoker.ident is not None:
            revoker.join(timeout=12)

    assert not first.is_alive() and not second.is_alive() and not revoker.is_alive(), "并发请求/撤权测试线程未退出"
    assert "1:1_error" not in outcomes and "2:1_error" not in outcomes, outcomes
    assert "revocation_error" not in outcomes, outcomes
    assert outcomes == {"1:1": "result-1-1", "2:1": "result-2-1", "revocation": "removed"}
    with sessions() as db:
        assert (
            db.query(EnterpriseMember)
            .filter_by(
                enterprise_id=enterprise_id,
                user_id=first_user_id,
            )
            .one()
            .status
            == "active"
        )
        assert (
            db.query(EnterpriseGroupMember)
            .filter_by(
                enterprise_id=enterprise_id,
                group_id=group_id,
                user_id=first_user_id,
            )
            .one()
            .status
            == "removed"
        )

    def _observe_cancel_run_lock(connection, _cursor, statement, _parameters, _context, _executemany):
        if (
            current_thread().name == "team-cancel"
            and "enterprise_assistant_run" in statement.lower()
            and "for update" in statement.lower()
        ):
            cancel_run_lock_sent.set()

    event.listen(fresh_chain, "before_cursor_execute", _observe_cancel_run_lock)
    cancel_call = Thread(
        target=_call,
        args=(3, task_ids[2], claims[2]),
        kwargs={"started": cancel_send_entered, "release": release_cancel_send},
        daemon=True,
    )
    cancel = Thread(target=_cancel_team, name="team-cancel", daemon=True)
    independent_call = Thread(
        target=_call,
        args=(2, task_ids[1], claims[1]),
        kwargs={"call_index": 2, "started": independent_call_entered},
        daemon=True,
    )
    try:
        cancel_call.start()
        assert cancel_send_entered.wait(timeout=10), "待取消任务的模拟模型请求未进入 provider"
        cancel.start()
        assert cancel_run_lock_sent.wait(timeout=2), "取消服务没有到达父运行锁"
        assert not cancel_finished.wait(timeout=0.1), "团队取消越过了仍在执行的 provider 请求"
        independent_call.start()
        assert independent_call_entered.wait(timeout=2), "团队取消等待时阻塞了同组另一成员的模型调用"
        assert not cancel_finished.wait(timeout=0.1), "独立请求期间取消提前绕过了活动 provider 锁"
    finally:
        release_cancel_send.set()
        cancel_call.join(timeout=12)
        if cancel.ident is not None:
            cancel.join(timeout=12)
        if independent_call.ident is not None:
            independent_call.join(timeout=12)
        event.remove(fresh_chain, "before_cursor_execute", _observe_cancel_run_lock)

    assert all(
        not thread.is_alive() for thread in (cancel_call, cancel, independent_call)
    ), "取消竞态测试线程未退出"
    assert "3:1_error" not in outcomes and "2:2_error" not in outcomes, outcomes
    assert "cancel_error" not in outcomes, outcomes
    assert outcomes["3:1"] == "result-3-1" and outcomes["2:2"] == "result-2-2", outcomes
    assert outcomes["cancel"]["status"] == "cancelled"
    with sessions() as db:
        assert db.get(EnterpriseAgentTeam, team_ids[2]).status == "cancelled"
        assert db.get(EnterpriseAgentTeamTask, task_ids[2]).status == "cancelled"
        reservations = db.query(ledger.BillingReservation).filter_by(enterprise_id=enterprise_id).all()
        assert len(reservations) == 4 and all(row.status == "settled" for row in reservations)

    def _set_company_status(status: str) -> None:
        with sessions.begin() as db:
            db.query(Enterprise).filter_by(id=enterprise_id).update(
                {Enterprise.status: status},
                synchronize_session=False,
            )

    suspension_send_entered = Event()
    release_suspension_send = Event()
    suspension_finished = Event()
    suspension_outcome: dict[str, object] = {}

    def _suspend_company() -> None:
        try:
            _set_company_status("suspended")
            suspension_outcome["status"] = "suspended"
        except Exception as exc:  # pragma: no cover - asserted through outcomes
            suspension_outcome["error"] = exc
        finally:
            suspension_finished.set()

    active_before_suspension = Thread(
        target=_call,
        args=(2, task_ids[1], claims[1]),
        kwargs={"call_index": 3, "started": suspension_send_entered, "release": release_suspension_send},
        daemon=True,
    )
    suspension = Thread(target=_suspend_company, name="team-company-suspension", daemon=True)
    try:
        active_before_suspension.start()
        assert suspension_send_entered.wait(timeout=10), "企业暂停前的模型请求未进入 provider"
        suspension.start()
        assert suspension_finished.wait(timeout=2), "企业暂停被 provider 等待期间的锁阻塞"
        assert suspension_outcome == {"status": "suspended"}, suspension_outcome
    finally:
        release_suspension_send.set()
        active_before_suspension.join(timeout=12)
        if suspension.ident is not None:
            suspension.join(timeout=12)

    assert not active_before_suspension.is_alive() and not suspension.is_alive(), "企业暂停并发线程未退出"
    assert "2:3_error" not in outcomes and outcomes["2:3"] == "result-2-3", outcomes

    denied_while_suspended_entered = Event()
    denied_while_suspended = Thread(
        target=_call,
        args=(2, task_ids[1], claims[1]),
        kwargs={"call_index": 4, "started": denied_while_suspended_entered},
        daemon=True,
    )
    denied_while_suspended.start()
    denied_while_suspended.join(timeout=12)
    from app.core.exceptions import ConflictError, ForbiddenError

    assert not denied_while_suspended.is_alive(), "企业暂停后的请求检查线程未退出"
    assert isinstance(outcomes.get("2:4_error"), ForbiddenError), outcomes
    assert not denied_while_suspended_entered.is_set(), "企业已暂停仍发送了新的模型请求"
    outcomes.pop("2:4_error")

    _set_company_status("active")

    def _set_company_billing_suspended(value: bool) -> None:
        with sessions.begin() as db:
            db.query(Enterprise).filter_by(id=enterprise_id).update(
                {Enterprise.billing_suspended: value},
                synchronize_session=False,
            )

    _set_company_billing_suspended(True)
    denied_during_billing_suspension_entered = Event()
    denied_during_billing_suspension = Thread(
        target=_call,
        args=(2, task_ids[1], claims[1]),
        kwargs={"call_index": 4, "started": denied_during_billing_suspension_entered},
        daemon=True,
    )
    denied_during_billing_suspension.start()
    denied_during_billing_suspension.join(timeout=12)
    assert not denied_during_billing_suspension.is_alive(), "账单冻结后的请求检查线程未退出"
    assert isinstance(outcomes.get("2:4_error"), ForbiddenError), outcomes
    assert not denied_during_billing_suspension_entered.is_set(), "企业账单冻结时仍发送了新的模型请求"
    outcomes.pop("2:4_error")
    _set_company_billing_suspended(False)

    refund_send_entered = Event()
    release_refund_send = Event()
    refund_update_finished = Event()
    refund_update_outcome: dict[str, object] = {}

    def _mark_refund_processing() -> None:
        try:
            with sessions.begin() as db:
                db.query(BillingRefund).filter_by(id=refund_id).update(
                    {BillingRefund.status: "processing"},
                    synchronize_session=False,
                )
            refund_update_outcome["status"] = "processing"
        except Exception as exc:  # pragma: no cover - asserted through outcomes
            refund_update_outcome["error"] = exc
        finally:
            refund_update_finished.set()

    active_before_refund = Thread(
        target=_call,
        args=(2, task_ids[1], claims[1]),
        kwargs={"call_index": 4, "started": refund_send_entered, "release": release_refund_send},
        daemon=True,
    )
    mark_refund_processing = Thread(target=_mark_refund_processing, name="team-refund-processing", daemon=True)
    try:
        active_before_refund.start()
        assert refund_send_entered.wait(timeout=10), "退款前的企业订阅模型请求未进入 provider"
        mark_refund_processing.start()
        assert refund_update_finished.wait(timeout=2), "退款状态更新被活动 provider 请求锁住"
        assert refund_update_outcome == {"status": "processing"}, refund_update_outcome
    finally:
        release_refund_send.set()
        active_before_refund.join(timeout=12)
        if mark_refund_processing.ident is not None:
            mark_refund_processing.join(timeout=12)

    assert not active_before_refund.is_alive() and not mark_refund_processing.is_alive(), "退款并发线程未退出"
    assert "2:4_error" not in outcomes and outcomes["2:4"] == "result-2-4", outcomes

    denied_during_refund_entered = Event()
    denied_during_refund = Thread(
        target=_call,
        args=(2, task_ids[1], claims[1]),
        kwargs={"call_index": 5, "started": denied_during_refund_entered},
        daemon=True,
    )
    denied_during_refund.start()
    denied_during_refund.join(timeout=12)
    assert not denied_during_refund.is_alive(), "退款处理中请求检查线程未退出"
    assert isinstance(outcomes.get("2:5_error"), ForbiddenError), outcomes
    assert not denied_during_refund_entered.is_set(), "订阅退款处理中仍发送了新的模型请求"
    with sessions() as db:
        reservations = db.query(ledger.BillingReservation).filter_by(enterprise_id=enterprise_id).all()
        assert len(reservations) == 6 and all(row.status == "settled" for row in reservations)
    outcomes.pop("2:5_error")

    with sessions.begin() as db:
        db.query(BillingRefund).filter_by(id=refund_id).update(
            {BillingRefund.status: "failed"},
            synchronize_session=False,
        )
    recovered_call_entered = Event()
    recovered_call = Thread(
        target=_call,
        args=(2, task_ids[1], claims[1]),
        kwargs={"call_index": 5, "started": recovered_call_entered},
        daemon=True,
    )
    recovered_call.start()
    recovered_call.join(timeout=12)
    assert not recovered_call.is_alive(), "退款失败恢复后的模型请求未退出"
    assert "2:5_error" not in outcomes and outcomes["2:5"] == "result-2-5", outcomes
    assert recovered_call_entered.is_set(), "退款失败并恢复有效订阅后未重新发送模型请求"

    with sessions.begin() as db:
        db.query(BillingSubscriptionPeriod).filter_by(id=subscription_period_id).update(
            {BillingSubscriptionPeriod.ends_at: now - timedelta(days=8)},
            synchronize_session=False,
        )
    denied_after_expiration_entered = Event()
    denied_after_expiration = Thread(
        target=_call,
        args=(2, task_ids[1], claims[1]),
        kwargs={"call_index": 6, "started": denied_after_expiration_entered},
        daemon=True,
    )
    denied_after_expiration.start()
    denied_after_expiration.join(timeout=12)
    assert not denied_after_expiration.is_alive(), "订阅过期后的模型请求检查线程未退出"
    assert isinstance(outcomes.get("2:6_error"), ForbiddenError), outcomes
    assert not denied_after_expiration_entered.is_set(), "订阅超过宽限期仍发送了新的模型请求"
    with sessions() as db:
        reservations = db.query(ledger.BillingReservation).filter_by(enterprise_id=enterprise_id).all()
        assert len(reservations) == 7 and all(row.status == "settled" for row in reservations)
    with sessions.begin() as db:
        db.query(BillingSubscriptionPeriod).filter_by(id=subscription_period_id).update(
            {BillingSubscriptionPeriod.ends_at: now + timedelta(days=30)},
            synchronize_session=False,
        )

    from app.services import user_service

    real_lock_user = user_service._lock_user
    account_disable_lock_attempted = Event()

    def _observe_account_disable_lock(db, user_id):
        if int(user_id) == second_user_id and current_thread().name == "team-account-disable":
            account_disable_lock_attempted.set()
        return real_lock_user(db, user_id)

    monkeypatch.setattr(user_service, "_lock_user", _observe_account_disable_lock)
    account_send_entered = Event()
    release_account_send = Event()
    account_disable_finished = Event()
    account_disable_outcome: dict[str, object] = {}

    def _disable_account() -> None:
        try:
            with sessions() as db:
                user_service.toggle_status(db, second_user_id, 0)
            account_disable_outcome["status"] = "disabled"
        except Exception as exc:  # pragma: no cover - asserted through outcomes
            account_disable_outcome["error"] = exc
        finally:
            account_disable_finished.set()

    active_before_disable = Thread(
        target=_call,
        args=(2, task_ids[1], claims[1]),
        kwargs={"call_index": 7, "started": account_send_entered, "release": release_account_send},
        daemon=True,
    )
    disable_account = Thread(target=_disable_account, name="team-account-disable", daemon=True)
    try:
        active_before_disable.start()
        assert account_send_entered.wait(timeout=10), "账号禁用前的模型请求未进入 provider"
        disable_account.start()
        assert account_disable_lock_attempted.wait(timeout=2), "账号禁用没有进入用户行锁"
        # Keep the provider pinned briefly after the status service begins its
        # current-user lock so the database race is exercised before release.
        account_disable_finished.wait(timeout=0.1)
    finally:
        release_account_send.set()
        active_before_disable.join(timeout=12)
        if disable_account.ident is not None:
            disable_account.join(timeout=12)

    assert not active_before_disable.is_alive() and not disable_account.is_alive(), "账号禁用并发线程未退出"
    assert "2:7_error" not in outcomes, repr(outcomes.get("2:7_error"))
    assert outcomes["2:7"] == "result-2-7", outcomes
    assert account_disable_outcome == {"status": "disabled"}, account_disable_outcome
    assert account_disable_finished.is_set()

    denied_after_disable_entered = Event()
    denied_after_disable = Thread(
        target=_call,
        args=(2, task_ids[1], claims[1]),
        kwargs={"call_index": 8, "started": denied_after_disable_entered},
        daemon=True,
    )
    denied_after_disable.start()
    denied_after_disable.join(timeout=12)
    assert not denied_after_disable.is_alive(), "账号禁用后的请求检查线程未退出"
    assert isinstance(outcomes.get("2:8_error"), ForbiddenError), outcomes
    assert not denied_after_disable_entered.is_set(), "账号禁用后仍发送了新的模型请求"

    with sessions.begin() as db:
        db.query(User).filter_by(id=second_user_id).update(
            {User.status: 1},
            synchronize_session=False,
        )
        db.query(EnterpriseAgentTeamTask).filter_by(id=task_ids[1]).update(
            {EnterpriseAgentTeamTask.lease_expires_at: now - timedelta(seconds=1)},
            synchronize_session=False,
        )
    recovered_lease = team_runtime.claim_task(
        sessions,
        enterprise_id=enterprise_id,
        task_id=task_ids[1],
        now=now + timedelta(seconds=1),
    )
    assert recovered_lease == {
        "status": "awaiting_reconciliation",
        "team_id": team_ids[1],
        "task_id": task_ids[1],
    }, recovered_lease
    assert team_runtime.claim_task(
        sessions,
        enterprise_id=enterprise_id,
        task_id=task_ids[1],
        now=now + timedelta(seconds=2),
    ) is None, "有已结算 reservation 的过期任务不能自动重复 claim"
    stale_provider_entered = Event()
    stale_worker = Thread(
        target=_call,
        args=(2, task_ids[1], claims[1]),
        kwargs={"call_index": 9, "started": stale_provider_entered},
        daemon=True,
    )
    stale_worker.start()
    stale_worker.join(timeout=12)
    assert not stale_worker.is_alive(), "旧租约 worker fencing 线程未退出"
    assert isinstance(outcomes.get("2:9_error"), ConflictError), outcomes
    assert not stale_provider_entered.is_set(), "已进入人工对账的过期任务仍重新发送 provider 请求"
    with sessions() as db:
        reservations = db.query(ledger.BillingReservation).filter_by(enterprise_id=enterprise_id).all()
        assert len(reservations) == 8 and all(row.status == "settled" for row in reservations)
        assert db.get(EnterpriseAgentTeamTask, task_ids[1]).status == "awaiting_reconciliation"
        assert db.get(EnterpriseAgentTeam, team_ids[1]).status == "awaiting_reconciliation"

    monkeypatch.setattr(enterprise_assistant_runtime_service, "_live_role_snapshot", real_live_role_snapshot)
    authority_run_public_id = f"team-lock-authority-{suffix}"
    with sessions.begin() as db:
        db.query(EnterpriseGroupMember).filter_by(
            enterprise_id=enterprise_id,
            group_id=group_id,
            user_id=third_user_id,
        ).update(
            {EnterpriseGroupMember.role: "leader"},
            synchronize_session=False,
        )
        room = db.get(EnterpriseConversation, conversation_id)
        source = EnterpriseChatMessage(
            enterprise_id=enterprise_id,
            conversation_id=conversation_id,
            sender_id=second_user_id,
            sender_type="human",
            client_message_id=f"team-lock-authority-source-{suffix}",
            content="@小菱 请处理旧任务",
        )
        db.add(source)
        db.flush()
        assistant_run = EnterpriseAssistantRun(
            public_id=authority_run_public_id,
            request_key=f"assistant:{authority_run_public_id}",
            enterprise_id=enterprise_id,
            conversation_id=conversation_id,
            group_id=group_id,
            initiator_user_id=second_user_id,
            source_message_id=int(source.id),
            status="completed",
            attempt_count=1,
            model_name="deepseek-flash",
            prompt_sha256="f" * 64,
        )
        db.add(assistant_run)
        db.flush()
        snapshot = enterprise_assistant_runtime_service._role_snapshot(
            db,
            enterprise_id=enterprise_id,
            room=room,
            messages=[source],
            initiator_id=second_user_id,
        )
        checkpoint = enterprise_assistant_runtime_service.RunCheckpoint(
            run_id=authority_run_public_id,
            model="deepseek-flash",
            transcript=[],
            tools=[],
            status="completed",
            output_text="这条答案必须被更高权限新指令压制",
        )
        state = EnterpriseAssistantRuntimeState(
            assistant_run_id=int(assistant_run.id),
            enterprise_id=enterprise_id,
            conversation_id=conversation_id,
            group_id=group_id,
            initiator_user_id=second_user_id,
            source_message_id=int(source.id),
            status="completed",
            checkpoint_json=enterprise_assistant_runtime_service._json(checkpoint.to_dict()),
            permission_snapshot_json=enterprise_assistant_runtime_service._json(snapshot),
            permission_snapshot_sha256=enterprise_assistant_runtime_service._digest(snapshot),
            stop_requested=False,
            lease_generation=1,
            billing_invocation_count=1,
            version=1,
        )
        placeholder = EnterpriseChatMessage(
            enterprise_id=enterprise_id,
            conversation_id=conversation_id,
            sender_id=None,
            sender_type="assistant",
            assistant_run_id=int(assistant_run.id),
            client_message_id=authority_run_public_id,
            content="小菱已收到请求，正在处理。",
        )
        reservation = ledger.BillingReservation(
            request_key=f"enterprise-assistant:{authority_run_public_id}:1:1",
            model_name="deepseek-flash",
            requested_milli=1_000,
            actual_milli=10,
            input_tokens=20,
            output_tokens=10,
            enterprise_id=enterprise_id,
            group_id=group_id,
            user_id=second_user_id,
            usage_kind="member",
            status="settled",
        )
        db.add_all([state, placeholder, reservation])

    finalizer_waiting_for_company = Event()
    release_finalizer_company = Event()
    finalizer_finished = Event()
    directive_finished = Event()
    finalization_outcome: dict[str, object] = {}
    directive_outcome: dict[str, object] = {}
    real_company_lookup = enterprise_org_service._company

    def _pause_finalizer_before_company_lock(db, requested_enterprise_id, *, lock=False):
        if lock and current_thread().name == "assistant-answer-finalizer":
            finalizer_waiting_for_company.set()
            if not release_finalizer_company.wait(timeout=10):
                raise TimeoutError("测试未释放企业小菱最终发布事务")
        return real_company_lookup(db, requested_enterprise_id, lock=lock)

    def _finalize_old_answer() -> None:
        try:
            finalization_outcome["result"] = enterprise_assistant_runtime_service._finalize_runtime(
                sessions,
                enterprise_id=enterprise_id,
                conversation_id=conversation_id,
                run_public_id=authority_run_public_id,
            )
        except Exception as exc:  # pragma: no cover - asserted through outcomes
            finalization_outcome["error"] = exc
        finally:
            finalizer_finished.set()

    def _send_higher_authority_directive() -> None:
        try:
            with sessions() as db:
                enterprise_chat_service.send_message(
                    db,
                    enterprise_id,
                    third_user_id,
                    conversation_id,
                    f"team-lock-authority-directive-{suffix}",
                    "@小菱 请先暂停旧任务并处理我这条新指令",
                )
            directive_outcome["status"] = "committed"
        except Exception as exc:  # pragma: no cover - asserted through outcomes
            directive_outcome["error"] = exc
        finally:
            directive_finished.set()

    monkeypatch.setattr(enterprise_org_service, "_company", _pause_finalizer_before_company_lock)
    finalizer = Thread(target=_finalize_old_answer, name="assistant-answer-finalizer", daemon=True)
    directive_writer = Thread(target=_send_higher_authority_directive, name="authority-directive-writer", daemon=True)
    try:
        finalizer.start()
        assert finalizer_waiting_for_company.wait(timeout=10), "最终答案事务未到达企业发布锁"
        directive_writer.start()
        assert directive_finished.wait(timeout=5), "更高权限新指令未在最终发布事务前提交"
        assert directive_outcome == {"status": "committed"}, directive_outcome
    finally:
        release_finalizer_company.set()
        finalizer.join(timeout=12)
        if directive_writer.ident is not None:
            directive_writer.join(timeout=12)

    assert not finalizer.is_alive() and not directive_writer.is_alive(), "企业小菱发布竞态线程未退出"
    from app.core.exceptions import ForbiddenError

    assert isinstance(finalization_outcome.get("error"), ForbiddenError), finalization_outcome
    with sessions() as db:
        saved_run = db.query(EnterpriseAssistantRun).filter_by(public_id=authority_run_public_id).one()
        saved_state = db.query(EnterpriseAssistantRuntimeState).filter_by(
            assistant_run_id=int(saved_run.id),
        ).one()
        saved_message = db.query(EnterpriseChatMessage).filter_by(
            assistant_run_id=int(saved_run.id),
        ).one()
        settled = db.query(ledger.BillingReservation).filter_by(
            request_key=f"enterprise-assistant:{authority_run_public_id}:1:1",
        ).one()
        assert saved_run.status == "suppressed" and saved_run.error_code == "authority_directive_superseded"
        assert saved_run.response_text is None
        assert saved_state.status == "suppressed" and saved_state.error_code == "authority_directive_superseded"
        assert "新的同级或更高权限" in saved_message.content
        assert "这条答案必须被更高权限新指令压制" not in saved_message.content
        assert settled.status == "settled" and settled.actual_milli == 10


def test_billing_reservation_settles_with_its_mysql_rate_snapshot_after_price_change(
    fresh_chain, monkeypatch, verified_model_catalog,
) -> None:
    from fractions import Fraction
    from uuid import uuid4

    from sqlalchemy.orm import sessionmaker

    from app.models.billing_ledger import BillingCreditBatch
    from app.models.user import User
    from app.services import billing_ledger_service as ledger

    suffix = uuid4().hex[:16]
    sessions = sessionmaker(bind=fresh_chain, expire_on_commit=False, autoflush=False)
    with sessions.begin() as db:
        user = User(username=f"rate-snapshot-{suffix}", password="x", role="user", status=1)
        db.add(user)
        db.flush()
        wallet = ledger.get_or_create_wallet(db, "user", int(user.id))
        ledger.grant(db, wallet_id=int(wallet.id), grant_key=f"rate-snapshot:{suffix}",
                     kind="purchase", amount_milli=100_000)
        requested = ledger.quote_milli("deepseek-flash", 10_000, 2_000)
        provider_identity = ledger.resolve_billing_provider_identity(
            provider_name="deepseek", base_url="https://api.deepseek.com/v1",
            credential_source="system", model_name="deepseek-flash",
        )
        ledger.reserve(
            db, request_key=f"rate-snapshot:{suffix}", model_name="deepseek-flash",
            user_id=int(user.id), max_milli=requested, personal_wallet_id=int(wallet.id),
            provider_identity=provider_identity,
        )

    monkeypatch.setitem(ledger._RATES, "deepseek-flash", (Fraction(2), Fraction(8)))
    with sessions.begin() as db:
        with pytest.raises(ledger.BillingError, match="输入和输出 Token 用量"):
            ledger.settle(db, request_key=f"rate-snapshot:{suffix}", actual_milli=10)
        still_reserved = db.query(ledger.BillingReservation).filter_by(
            request_key=f"rate-snapshot:{suffix}",
        ).one()
        assert still_reserved.status == "reserved" and still_reserved.actual_milli is None
        actual = ledger.quote_reservation_milli(
            db, request_key=f"rate-snapshot:{suffix}", input_tokens=10_000, output_tokens=2_000,
        )
        settled = ledger.settle(
            db, request_key=f"rate-snapshot:{suffix}", actual_milli=actual,
            input_tokens=10_000, output_tokens=2_000,
        )
        batch = db.query(BillingCreditBatch).filter_by(grant_key=f"rate-snapshot:{suffix}").one()
        assert settled.status == "settled"
        assert settled.actual_milli == 18_000
        assert settled.pricing_version == verified_model_catalog
        assert (settled.input_rate_numerator, settled.input_rate_denominator,
                settled.output_rate_numerator, settled.output_rate_denominator) == (1, 1, 4, 1)
        assert (batch.remaining_milli, batch.reserved_milli) == (82_000, 0)


def test_legacy_reviewer_is_backfilled_as_audited_permanent_entitlement(fresh_chain) -> None:
    with fresh_chain.connect() as connection:
        rows = connection.execute(text("""
            SELECT u.id, u.role, p.status, p.granted_by, e.action, e.reason
            FROM `user` AS u
            JOIN billing_permanent_reviewer AS p ON p.user_id = u.id
            JOIN billing_permanent_reviewer_event AS e ON e.user_id = u.id
            WHERE u.username = 'legacy-reviewer-migration-probe'
        """)).all()

    assert len(rows) == 1
    reviewer_id, role, status, actor_id, action, reason = rows[0]
    assert (role, status, action) == ("reviewer", "active", "grandfather")
    assert reviewer_id > 0 and actor_id > 0 and "076" in reason


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


def test_alembic_chain_creates_every_orm_table_and_column(fresh_chain) -> None:
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


def test_mysql_rate_catalog_keeps_provider_and_fx_sources_separate_and_immutable(fresh_chain) -> None:
    from sqlalchemy.exc import DBAPIError

    with fresh_chain.connect() as connection:
        row = connection.execute(text(
            "SELECT id, source_url, source_checked_at, fx_source_url, fx_source_checked_at "
            "FROM billing_rate_catalog ORDER BY id LIMIT 1"
        )).one()
        assert row[1] == "https://api-docs.deepseek.com/quick_start/pricing/"
        assert row[3] == "https://www.boc.cn/sourcedb/whpj/index.html"
        assert row[2] is not None and row[4] is not None

        for statement in (
            text("UPDATE billing_rate_catalog SET fx_source_url='https://example.com/' WHERE id=:id"),
            text("UPDATE billing_rate_catalog SET fx_source_checked_at='2026-01-01 00:00:00' WHERE id=:id"),
        ):
            with pytest.raises(DBAPIError, match="snapshot is immutable"):
                connection.execute(statement, {"id": row[0]})
            connection.rollback()


def test_mysql_credit_checkout_and_new_rate_draft_follow_the_active_catalog(fresh_chain) -> None:
    from uuid import uuid4

    from app.models.billing_ledger import (
        BillingOrder,
        BillingRateCatalog,
        BillingRateCatalogEvent,
        BillingRateCatalogItem,
    )
    from app.models.user import User
    from app.services import billing_checkout_service, billing_rate_catalog_service

    sessions = sessionmaker(bind=fresh_chain, expire_on_commit=False, autoflush=False)
    now = datetime.now(timezone.utc)
    at = now.replace(tzinfo=None)
    # Keep this approved test-only catalog inside an uncommitted outer
    # transaction; the immutable row must not affect later tests in the chain.
    with sessions() as db, db.begin_nested():
        buyer = User(username=f"mysql-rate-checkout-{uuid4().hex[:16]}", password="x", role="user", status=1)
        db.add(buyer)
        db.flush()
        old_order = billing_checkout_service.create_order(
            db, user_id=int(buyer.id), product="personal_credits", quantity=2, channel="alipay",
        )
        assert old_order.amount_fen == 800

        unit_price = 501
        catalog = BillingRateCatalog(
            version_key="c" * 64, status="approved", effective_at=at - timedelta(seconds=1),
            approved_effective_at=at - timedelta(seconds=1),
            source_url="https://api-docs.deepseek.com/quick_start/pricing/",
            source_checked_at=at, fx_source_url="https://www.boc.cn/sourcedb/whpj/index.html",
            model_cny_source_url="https://api-docs.deepseek.com/zh-cn/quick_start/pricing/",
            model_cny_source_checked_at=at,
            fx_source_checked_at=at, source_sha256="d" * 64,
            fx_cny_per_usd_scaled=67168, fx_scale=10000,
            credit_pack_price_fen=unit_price, credit_pack_credits=1000,
            change_note="MySQL 测试中的新积分商品版本。", created_by=None, created_at=at,
            approved_by=None, approved_at=at,
        )
        db.add(catalog)
        db.flush()
        for item in billing_rate_catalog_service.baseline_items():
            db.add(BillingRateCatalogItem(catalog_id=catalog.id, **item))
        db.add(BillingRateCatalogEvent(
            catalog_id=catalog.id, actor_id=None, action="approved",
            note="隔离 MySQL 测试价格目录。", created_at=at,
        ))
        db.flush()

        assert billing_rate_catalog_service.active_catalog(db, now=now).id == catalog.id
        new_order = billing_checkout_service.create_order(
            db, user_id=int(buyer.id), product="personal_credits", quantity=2, channel="alipay",
        )
        assert new_order.amount_fen == unit_price * 2
        assert db.query(BillingOrder).filter_by(order_no=old_order.order_no).one().amount_fen == 800

        draft = billing_rate_catalog_service.create_draft(
            db, actor_id=int(buyer.id), effective_at=now + timedelta(hours=2),
            source_url="https://api-docs.deepseek.com/quick_start/pricing/",
            source_checked_at=now, fx_source_url="https://www.boc.cn/sourcedb/whpj/index.html",
            model_cny_source_url="https://api-docs.deepseek.com/zh-cn/quick_start/pricing/",
            model_cny_source_checked_at=now,
            fx_source_checked_at=now, fx_cny_per_usd_scaled=67168,
            change_note="验证模型费率草稿继承当前积分收入分母。",
            items=billing_rate_catalog_service.baseline_items(), now=now,
        )
        assert (draft.credit_pack_price_fen, draft.credit_pack_credits) == (unit_price, 1000)


def test_server_tool_usage_columns_are_nullable_and_paired(fresh_chain) -> None:
    inspector = inspect(fresh_chain)
    columns = {column["name"]: column for column in inspector.get_columns("billing_reservation")}
    assert columns["server_tool_search_calls"]["nullable"]
    assert columns["server_tool_fetch_calls"]["nullable"]
    checks = {item["name"] for item in inspector.get_check_constraints("billing_reservation")}
    assert {
        "ck_billing_reservation_server_tool_usage_pair",
        "ck_billing_reservation_rate_snapshot_pair",
        "ck_billing_reservation_billable_kind_snapshot",
        "ck_billing_reservation_tool_success_count",
    } <= checks


def test_mysql_persists_search_fetch_reservation_with_tool_price_snapshot(fresh_chain) -> None:
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    from uuid import uuid4

    from app.models.billing_ledger import BillingCreditBatch, BillingLimit, BillingReservationReconciliationEvent
    from app.models.user import User
    from app.services import billing_ledger_service as ledger
    from app.services import billing_rate_catalog_service as catalog
    from app.services import billing_reservation_reconciliation_service as reconciliation

    sessions = sessionmaker(bind=fresh_chain, expire_on_commit=False, autoflush=False)
    # MySQL DATETIME columns have second precision; use the same precision for
    # the simulated effective time so rounding cannot select the older catalog.
    now = datetime.now(timezone.utc).replace(microsecond=0)
    checked_at = now - timedelta(minutes=10)
    suffix = uuid4().hex[:16]
    enterprise_id = 9_100_001
    group_id = 9_100_002
    request_key = f"mysql-tool:{suffix}"
    with sessions.begin() as db:
        user = User(username=f"mysql-tool-reservation-{suffix}", password="x", role="user", status=1)
        db.add(user)
        db.flush()
        draft = catalog.create_draft(
            db, actor_id=int(user.id), effective_at=now + timedelta(hours=2),
            source_url="https://api-docs.deepseek.com/quick_start/pricing/",
            source_checked_at=checked_at,
            model_cny_source_url="https://api-docs.deepseek.com/zh-cn/quick_start/pricing/",
            model_cny_source_checked_at=checked_at,
            fx_source_url="https://www.boc.cn/sourcedb/whpj/index.html",
            fx_source_checked_at=checked_at, fx_cny_per_usd_scaled=67_168,
            change_note="MySQL 工具 reservation 迁移约束验证。",
            items=catalog.baseline_items(), now=now,
            tool_cny_source_url="https://api-docs.deepseek.com/zh-cn/guides/search/",
            tool_cny_source_checked_at=checked_at,
            tool_usd_source_url="https://api-docs.deepseek.com/quick_start/pricing/",
            tool_usd_source_checked_at=checked_at,
            tool_search_cost_cny_micros_per_success=5_038,
            tool_search_price_milli_per_success=1_800,
            tool_fetch_cost_cny_micros_per_success=5_038,
            tool_fetch_price_milli_per_success=1_800,
        )
        catalog.approve_draft(db, catalog_id=int(draft.id), actor_id=int(user.id) + 1, now=now)
        wallet = ledger.get_or_create_wallet(db, "enterprise", enterprise_id)
        ledger.grant(db, wallet_id=int(wallet.id), grant_key=f"mysql-tool:{suffix}",
                     kind="purchase", amount_milli=5_000)
        db.add(BillingLimit(
            limit_key=f"mysql-tool-limit:{suffix}", enterprise_id=enterprise_id,
            group_id=group_id, user_id=int(user.id),
            period_start=now.replace(tzinfo=None) - timedelta(days=1),
            period_end=now.replace(tzinfo=None) + timedelta(days=30),
            limit_milli=5_000, used_milli=0, reserved_milli=0,
        ))
        db.flush()
        provider = ledger.resolve_billing_provider_identity(
            provider_name="deepseek", base_url="https://api.deepseek.com/v1",
            credential_source="system", model_name="deepseek-flash",
        )
        wallet_id = int(wallet.id)
        user_id = int(user.id)

    # Two requests with the same key race on MySQL RR. Exactly one may reserve;
    # the waiter must observe the first committed row and refuse another send.
    start = Barrier(2)

    def duplicate_reservation():
        with sessions() as db:
            try:
                start.wait(timeout=10)
                reservation = ledger.reserve_tool(
                    db, request_key=request_key, tool_name="search", user_id=user_id,
                    enterprise_id=enterprise_id, enterprise_wallet_id=wallet_id, group_id=group_id,
                    provider_identity=provider, now=draft.effective_at, clock=lambda: draft.effective_at,
                )
                db.commit()
                return ("created_reservation", int(reservation.id))
            except ledger.IdempotencyConflict:
                db.rollback()
                return ("blocked_duplicate", None)

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(lambda _index: duplicate_reservation(), range(2)))
    assert sorted(item[0] for item in outcomes) == ["blocked_duplicate", "created_reservation"]
    reservation_id = next(item[1] for item in outcomes if item[1] is not None)
    with sessions() as db:
        reservation = db.query(ledger.BillingReservation).filter_by(id=reservation_id).one()
        assert reservation.billable_kind == "search"
        assert reservation.pricing_version
        assert reservation.tool_cost_cny_micros_per_success == 5_038
        assert reservation.tool_price_milli_per_success == 1_800
        assert reservation.input_rate_numerator is None
        assert reservation.provider_attempt_started_at is None

    # The one-way attempt marker is committed in its own transaction before a
    # caller is allowed to perform network I/O. Unknown results keep both holds.
    with sessions.begin() as db:
        marked = ledger.mark_tool_attempt_started(db, request_key, now=draft.effective_at)
        assert marked.provider_attempt_started_at is not None
    with sessions.begin() as db:
        ledger.mark_unknown(db, request_key)
    evidence = {
        "evidence_reference": f"mysql-provider-statement:{suffix}",
        "evidence_sha256": "9" * 64,
        "note": "隔离 MySQL 并发账本验证",
    }
    with sessions.begin() as db:
        proposal = reconciliation.propose_reconciliation(
            db, reservation_id=reservation_id, actor_id=9_100_010, resolution="settle",
            provider_request_id=f"provider-search:{suffix}", input_tokens=None, output_tokens=None,
            tool_successful_calls=1, **evidence,
        )
        proposal_id = int(proposal.id)

    approve_start = Barrier(2)

    def concurrent_approval(actor_id: int):
        with sessions() as db:
            try:
                approve_start.wait(timeout=10)
                reconciliation.decide_reconciliation(
                    db, reservation_id=reservation_id, actor_id=actor_id,
                    decision="approve", note="并发审批锁序验证",
                )
                db.commit()
                return "approved"
            except reconciliation.ReconciliationError:
                # Business-block audit events must survive the rejected decision.
                db.commit()
                return "blocked"

    with ThreadPoolExecutor(max_workers=2) as pool:
        decisions = list(pool.map(concurrent_approval, (9_100_011, 9_100_012)))
    assert sorted(decisions) == ["approved", "blocked"]

    with sessions() as db:
        current = db.query(ledger.BillingReservation).filter_by(id=reservation_id).one()
        batch = db.query(BillingCreditBatch).filter_by(wallet_id=wallet_id).one()
        limit = db.query(BillingLimit).filter_by(limit_key=f"mysql-tool-limit:{suffix}").one()
        events = db.query(BillingReservationReconciliationEvent).filter_by(
            reservation_id=reservation_id,
        ).order_by(BillingReservationReconciliationEvent.id).all()
        assert (current.status, current.tool_successful_calls, current.actual_milli) == (
            "settled", 1, 1_800,
        )
        assert (batch.remaining_milli, batch.reserved_milli) == (3_200, 0)
        assert (limit.used_milli, limit.reserved_milli) == (1_800, 0)
        assert [event.decision for event in events].count("approved") == 1
        assert [event.decision for event in events].count("blocked") == 1
        assert any(event.id == proposal_id and event.decision == "proposed" for event in events)


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


def test_migration_065_on_mysql_keeps_hand_patched_columns_and_adds_only_the_missing_one(fresh_chain_url: URL) -> None:
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


@pytest.mark.parametrize("approval_gets_company_lock_first", [False, True])
def test_global_publication_approval_serializes_with_member_removal(
    fresh_chain, monkeypatch, approval_gets_company_lock_first: bool,
) -> None:
    """在 MySQL REPEATABLE READ 下验证审批与撤员的两种串行顺序。"""
    from threading import Event, Thread, current_thread
    from uuid import uuid4

    from sqlalchemy.orm import sessionmaker

    from app.core.exceptions import ForbiddenError
    from app.models.enterprise_asset import EnterpriseAssetPublication
    from app.models.enterprise_org import Enterprise, EnterpriseMember
    from app.models.user import User
    from app.services import enterprise_asset_service as asset_service
    from app.services.enterprise_org_service import remove_enterprise_member

    suffix = uuid4().hex[:12]
    sessions = sessionmaker(bind=fresh_chain, expire_on_commit=False)
    with sessions() as db:
        owner = User(username=f"race-owner-{suffix}", password="x", role="user", status=1)
        requester = User(username=f"race-requester-{suffix}", password="x", role="user", status=1)
        db.add_all([owner, requester])
        platform_admin = db.query(User).filter_by(username="admin", role="super_admin", status=1).one()
        db.flush()
        enterprise = Enterprise(name=f"并发审批企业-{suffix}", owner_id=owner.id, seat_limit=2,
                                status="active", paid_order_id=f"race-order-{suffix}")
        db.add(enterprise)
        db.flush()
        enterprise_id, owner_id, requester_id, platform_admin_id = (
            enterprise.id, owner.id, requester.id, platform_admin.id,
        )
        db.add_all([
            EnterpriseMember(enterprise_id=enterprise_id, user_id=owner_id, role="owner", status="active"),
            EnterpriseMember(enterprise_id=enterprise_id, user_id=requester_id, role="member", status="active"),
        ])
        db.commit()

    with sessions() as db:
        draft = asset_service.create_draft(
            db, enterprise_id, requester_id, kind="agent", title="并发审批快照",
            definition={"prompt": "固定快照"}, request_key=f"race-create-{suffix}",
        )
        publication = asset_service.request_publication(
            db, enterprise_id, requester_id, draft.id, scope="global", expected_version=1,
            request_key=f"race-publication-{suffix}",
        )
        publication_id = publication.id

    monkeypatch.setattr(
        asset_service.rbac_service, "is_super_admin_user",
        lambda _db, user_id: user_id == platform_admin_id,
    )
    real_company = asset_service._company
    company_lock_reached = Event()
    allow_approval = Event()
    removal_completed = Event()
    approval_result: list[object] = []

    def gated_company(db, target_enterprise_id, *, lock=False):
        if current_thread().name == "publication-approver":
            if approval_gets_company_lock_first:
                company = real_company(db, target_enterprise_id, lock=lock)
                company_lock_reached.set()
                if not allow_approval.wait(timeout=15):
                    raise TimeoutError("等待并发撤员测试释放审批超时")
                return company
            company_lock_reached.set()
            if not allow_approval.wait(timeout=15):
                raise TimeoutError("等待并发撤员测试释放审批超时")
        return real_company(db, target_enterprise_id, lock=lock)

    monkeypatch.setattr(asset_service, "_company", gated_company)

    def approve():
        try:
            with sessions() as db:
                db.connection().exec_driver_sql(
                    "SET SESSION TRANSACTION ISOLATION LEVEL REPEATABLE READ"
                )
                result = asset_service.decide_global_publication(
                    db, publication_id, platform_admin_id, approve=True, reason="并发权限复核",
                )
                approval_result.append(("approved", result.status))
        except Exception as exc:  # 保存线程异常，由主线程断言具体结果
            approval_result.append(exc)

    def remove_member():
        try:
            with sessions() as db:
                db.connection().exec_driver_sql(
                    "SET SESSION TRANSACTION ISOLATION LEVEL REPEATABLE READ"
                )
                remove_enterprise_member(db, enterprise_id, owner_id, requester_id)
                db.commit()
                removal_completed.set()
        except Exception as exc:
            approval_result.append(exc)
            removal_completed.set()

    approver = Thread(target=approve, name="publication-approver", daemon=True)
    approver.start()
    assert company_lock_reached.wait(timeout=15)

    remover = Thread(target=remove_member, name="enterprise-member-remover", daemon=True)
    remover.start()
    if approval_gets_company_lock_first:
        assert not removal_completed.wait(timeout=0.25), "撤员应等待审批释放企业行锁"
        allow_approval.set()
        approver.join(timeout=15)
        remover.join(timeout=15)
        assert not approver.is_alive() and not remover.is_alive(), "审批与撤员线程未正常结束"
        assert ("approved", "approved") in approval_result
    else:
        assert removal_completed.wait(timeout=15), "先行撤员未能提交"
        allow_approval.set()
        approver.join(timeout=15)
        remover.join(timeout=15)
        assert not approver.is_alive() and not remover.is_alive(), "审批与撤员线程未正常结束"
        assert any(isinstance(result, ForbiddenError) for result in approval_result)

    with sessions() as db:
        publication = db.get(EnterpriseAssetPublication, publication_id)
        requester = db.query(EnterpriseMember).filter_by(
            enterprise_id=enterprise_id, user_id=requester_id,
        ).one()
        if approval_gets_company_lock_first:
            assert publication.status == "approved"
            assert requester.status == "removed"
        else:
            assert publication.status == "pending"
            assert requester.status == "removed"


@pytest.mark.parametrize("authorization_locks_first", [False, True])
def test_runtime_publication_authorization_serializes_with_revoke(
    fresh_chain, monkeypatch, authorization_locks_first: bool,
) -> None:
    """在 MySQL REPEATABLE READ 下验证运行授权与资产撤销的两种锁序。"""
    from threading import Event, Thread, current_thread
    from uuid import uuid4

    from sqlalchemy.orm import sessionmaker

    from app.core.exceptions import NotFoundError
    from app.models.enterprise_asset import EnterpriseAssetPublication
    from app.models.enterprise_org import Enterprise, EnterpriseMember
    from app.models.user import User
    from app.services import enterprise_asset_service as asset_service

    suffix = uuid4().hex[:12]
    sessions = sessionmaker(bind=fresh_chain, expire_on_commit=False, autoflush=False)
    with sessions() as db:
        owner = User(username=f"asset-runtime-owner-{suffix}", password="x", role="user", status=1)
        member = User(username=f"asset-runtime-member-{suffix}", password="x", role="user", status=1)
        db.add_all([owner, member])
        db.flush()
        enterprise = Enterprise(
            name=f"资产运行撤销企业-{suffix}", owner_id=owner.id, seat_limit=2,
            status="active", paid_order_id=f"asset-runtime-order-{suffix}",
        )
        db.add(enterprise)
        db.flush()
        enterprise_id, owner_id, member_id = enterprise.id, owner.id, member.id
        db.add_all([
            EnterpriseMember(enterprise_id=enterprise_id, user_id=owner_id, role="owner", status="active"),
            EnterpriseMember(enterprise_id=enterprise_id, user_id=member_id, role="member", status="active"),
        ])
        db.commit()

    with sessions() as db:
        draft = asset_service.create_draft(
            db,
            enterprise_id,
            member_id,
            kind="agent",
            title="并发撤销运行资产",
            definition={"schema_version": 1, "runtime": "prompt", "system_prompt": "只输出核验文本。"},
            request_key=f"asset-runtime-draft-{suffix}",
        )
        publication = asset_service.request_publication(
            db,
            enterprise_id,
            member_id,
            draft.id,
            scope="enterprise",
            expected_version=1,
            request_key=f"asset-runtime-publication-{suffix}",
        )
        publication = asset_service.decide_enterprise_publication(
            db, enterprise_id, publication.id, owner_id, approve=True, reason="运行时撤销并发验收",
        )
        publication_id = publication.id

    authorization_started = Event()
    authorization_company_attempted = Event()
    allow_authorization_commit = Event()
    revoker_started = Event()
    revocation_member_locked = Event()
    allow_revocation_commit = Event()
    revocation_completed = Event()
    authorization_result: list[object] = []
    revocation_result: list[object] = []
    original_company = asset_service._company
    original_member_lock = asset_service._lock_member

    def observe_authorization_company(db, target_enterprise_id, *, lock=False):
        if current_thread().name == "asset-runtime-resolver":
            authorization_company_attempted.set()
        return original_company(db, target_enterprise_id, lock=lock)

    def gate_revocation_member_lock(db, target_enterprise_id, user_id):
        original_member_lock(db, target_enterprise_id, user_id)
        if current_thread().name == "asset-publication-revoker":
            revocation_member_locked.set()
            if not allow_revocation_commit.wait(timeout=15):
                raise TimeoutError("等待运行时资产撤销测试释放成员锁超时")

    monkeypatch.setattr(asset_service, "_company", observe_authorization_company)
    monkeypatch.setattr(asset_service, "_lock_member", gate_revocation_member_lock)

    def authorize_runtime():
        try:
            with sessions() as db:
                db.connection().exec_driver_sql(
                    "SET SESSION TRANSACTION ISOLATION LEVEL REPEATABLE READ"
                )
                resolved = asset_service.resolve_runtime_publication(
                    db, publication_id, user_id=member_id, enterprise_id=enterprise_id,
                )
                authorization_started.set()
                if not allow_authorization_commit.wait(timeout=15):
                    raise TimeoutError("等待运行时资产授权测试提交超时")
                authorization_result.append(resolved)
                db.commit()
        except Exception as exc:  # 在线程边界保存异常，由主线程断言
            authorization_result.append(exc)
            authorization_started.set()

    def revoke_publication():
        revoker_started.set()
        try:
            with sessions() as db:
                db.connection().exec_driver_sql(
                    "SET SESSION TRANSACTION ISOLATION LEVEL REPEATABLE READ"
                )
                row = asset_service.revoke_enterprise_publication(
                    db, enterprise_id, publication_id, member_id, reason="并发撤销运行资产",
                )
                db.commit()
                revocation_result.append(row.status)
        except Exception as exc:  # 在线程边界保存异常，由主线程断言
            revocation_result.append(exc)
        finally:
            revocation_completed.set()

    authorizer = None
    revoker = None
    try:
        if authorization_locks_first:
            authorizer = Thread(target=authorize_runtime, name="asset-runtime-resolver", daemon=True)
            authorizer.start()
            assert authorization_started.wait(timeout=15), "运行时授权未锁定已发布资产"
            revoker = Thread(target=revoke_publication, name="asset-publication-revoker", daemon=True)
            revoker.start()
            assert revoker_started.wait(timeout=15), "撤销线程未启动"
            assert not revocation_completed.wait(timeout=0.25), "撤销应等待已锁定的运行授权事务"
            allow_authorization_commit.set()
            authorizer.join(timeout=15)
            assert not authorizer.is_alive(), "运行授权线程未正常结束"
            assert revocation_member_locked.wait(timeout=15), "撤销事务未在授权提交后取得企业成员锁"
            assert not revocation_completed.is_set(), "撤销应保持等待，直到测试释放成员锁"
            allow_revocation_commit.set()
            revoker.join(timeout=15)
            assert not revoker.is_alive(), "撤销线程未正常结束"
            assert isinstance(authorization_result[0], dict)
            assert authorization_result[0]["publication_id"] == publication_id
            assert revocation_result == ["revoked"]
        else:
            revoker = Thread(target=revoke_publication, name="asset-publication-revoker", daemon=True)
            revoker.start()
            assert revocation_member_locked.wait(timeout=15), "撤销事务未取得企业成员锁"
            authorizer = Thread(target=authorize_runtime, name="asset-runtime-resolver", daemon=True)
            authorizer.start()
            assert authorization_company_attempted.wait(timeout=15), "运行时授权未尝试读取企业权限"
            assert not authorization_started.wait(timeout=0.25), "运行授权应等待已锁定的撤销事务"
            allow_revocation_commit.set()
            revoker.join(timeout=15)
            authorizer.join(timeout=15)
            assert not authorizer.is_alive() and not revoker.is_alive(), "撤销与授权线程未正常结束"
            assert revocation_result == ["revoked"]
            assert isinstance(authorization_result[0], NotFoundError)
    finally:
        allow_authorization_commit.set()
        allow_revocation_commit.set()
        for worker in (authorizer, revoker):
            if worker and worker.is_alive():
                worker.join(timeout=15)

    with sessions() as db:
        publication = db.get(EnterpriseAssetPublication, publication_id)
        assert publication.status == "revoked"


def test_asset_invocation_mysql_constraints_are_present(fresh_chain) -> None:
    table = "enterprise_assistant_asset_invocation"
    unique_names = {item["name"] for item in inspect(fresh_chain).get_unique_constraints(table)}
    foreign_key_names = {item["name"] for item in inspect(fresh_chain).get_foreign_keys(table)}
    index_names = {item["name"] for item in inspect(fresh_chain).get_indexes(table)}

    assert {
        "uq_ent_asset_call_invocation",
        "uq_ent_asset_invocation_public_id",
        "uq_ent_asset_invocation_request",
    } <= unique_names
    assert {
        "fk_ent_asset_invocation_run",
        "fk_ent_asset_invocation_publication",
    } <= foreign_key_names
    assert "ix_ent_asset_invocation_scope" in index_names


@pytest.mark.parametrize("ingestion_kind", ["agent", "personal"])
@pytest.mark.parametrize("binding_locks_first", [False, True])
def test_agent_project_ingestion_serializes_with_enterprise_binding(
    fresh_chain, monkeypatch, ingestion_kind: str, binding_locks_first: bool,
) -> None:
    """MySQL RR 下验证个人/Agent 知识入库与企业绑定的真实行锁顺序。"""
    from threading import Event, Thread
    from time import monotonic, sleep
    from uuid import uuid4

    from sqlalchemy.orm import sessionmaker

    from app.core.exceptions import ForbiddenError
    from app.models.agent_governance import AgentKnowledgeChunk, AgentKnowledgeDoc, AgentKnowledgeSource
    from app.models.code_file import CodeFile
    from app.models.knowledge_chunk import KnowledgeChunk
    from app.models.knowledge_doc import KnowledgeDoc
    from app.models.project import Project
    from app.models.user import User
    from app.services import agent_knowledge_service as agent_knowledge
    from app.services import enterprise_org_service as org
    from app.services import knowledge_service
    from app.services.enterprise_project_service import bind_owned_project

    suffix = uuid4().hex[:12]
    sessions = sessionmaker(bind=fresh_chain, expire_on_commit=False)
    with sessions() as db:
        owner = User(username=f"knowledge-owner-{suffix}", password="x", role="user", status=1)
        db.add(owner)
        db.flush()
        enterprise = org.create_enterprise_from_paid_order(
            db, owner_id=owner.id, name=f"知识锁测试企业-{suffix}", seat_limit=2,
            paid_order_id=f"knowledge-lock-order-{suffix}",
        )
        group = org.create_group(db, enterprise.id, owner.id, f"测试组-{suffix}")
        project = Project(user_id=owner.id, project_name=f"知识锁测试项目-{suffix}", status="active")
        db.add(project)
        db.flush()
        code_file = CodeFile(
            project_id=project.id,
            file_name="secret.py",
            file_path="secret.py",
            language="python",
            size_bytes=20,
            content="enterprise source that must stay isolated",
            status="active",
        )
        source = AgentKnowledgeSource(
            agent_code="manager",
            source_type="project",
            source_uri=str(project.id),
            whitelist=1,
            enabled=1,
            config_json=f'{{"project_id": {project.id}}}',
        )
        db.add_all([code_file, source])
        db.flush()
        personal_doc = KnowledgeDoc(
            user_id=owner.id,
            source_type="code",
            source_ref=f"file:{code_file.id}",
            title="personal copy before binding",
            char_count=40,
            chunk_count=1,
            status="active",
        )
        db.add(personal_doc)
        db.flush()
        db.add(KnowledgeChunk(
            doc_id=personal_doc.id,
            user_id=owner.id,
            seq=0,
            content="personal copy that must be retired",
            embedding="[0.2, 0.8]",
            embed_model="fixture:mysql-lock",
        ))
        db.commit()
        enterprise_id, group_id, owner_id = int(enterprise.id), int(group.id), int(owner.id)
        project_id, source_id, file_id = int(project.id), int(source.id), int(code_file.id)

    embedding_started = Event()
    release_embedding = Event()
    binding_cleanup_entered = Event()
    release_binding = Event()
    binding_started = Event()
    binding_done = Event()
    ingestion_started = Event()
    ingestion_done = Event()
    binder_connection_id: list[int] = []
    ingestion_connection_id: list[int] = []
    embedding_calls: list[list[str]] = []
    outcomes: list[object] = []

    def fake_embed(_db, texts, **_kwargs):
        embedding_calls.append(list(texts))
        embedding_started.set()
        if not release_embedding.wait(timeout=15):
            raise TimeoutError("等待测试释放入库嵌入超时")
        return ([[0.2, 0.8] for _ in texts], "fixture:mysql-lock")

    monkeypatch.setattr(agent_knowledge.embedding_service, "embed_texts", fake_embed)
    real_retire = agent_knowledge.retire_bound_project_sources

    def gated_retire(db, target_project_id, **kwargs):
        if binding_locks_first and int(target_project_id) == project_id:
            binding_cleanup_entered.set()
            if not release_binding.wait(timeout=15):
                raise TimeoutError("等待测试释放项目绑定清理超时")
        return real_retire(db, target_project_id, **kwargs)

    monkeypatch.setattr(agent_knowledge, "retire_bound_project_sources", gated_retire)

    def bind_project():
        binding_started.set()
        try:
            with sessions() as db:
                db.connection().exec_driver_sql(
                    "SET SESSION TRANSACTION ISOLATION LEVEL REPEATABLE READ"
                )
                binder_connection_id.append(
                    int(db.connection().exec_driver_sql("SELECT CONNECTION_ID()").scalar())
                )
                bind_owned_project(
                    db, enterprise_id, project_id, group_id=group_id, actor_id=owner_id,
                )
        except Exception as exc:
            outcomes.append(("binding", exc))
        finally:
            binding_done.set()

    def ingest_project():
        ingestion_started.set()
        try:
            with sessions() as db:
                db.connection().exec_driver_sql(
                    "SET SESSION TRANSACTION ISOLATION LEVEL REPEATABLE READ"
                )
                ingestion_connection_id.append(
                    int(db.connection().exec_driver_sql("SELECT CONNECTION_ID()").scalar())
                )
                if ingestion_kind == "agent":
                    doc = agent_knowledge.add_document(
                        db,
                        agent_code="manager",
                        title="project code",
                        content="confidential company code",
                        source_type="project",
                        source_ref=f"code_file:{file_id}",
                        source_id=source_id,
                        user_id=owner_id,
                    )
                else:
                    doc = knowledge_service.add_document(
                        db,
                        owner_id,
                        "personal project code",
                        "confidential personal project code",
                        source_type="code",
                        source_ref=f"file:{file_id}",
                    )
                outcomes.append(("ingestion", doc.id))
        except Exception as exc:
            outcomes.append(("ingestion", exc))
        finally:
            ingestion_done.set()

    def wait_for_mysql_lock_wait(connection_id: int, timeout: float = 5) -> bool:
        deadline = monotonic() + timeout
        while monotonic() < deadline:
            with fresh_chain.connect() as observer:
                waiting = observer.exec_driver_sql(
                    """SELECT COUNT(*)
                       FROM performance_schema.data_lock_waits waits
                       JOIN performance_schema.threads threads
                         ON threads.THREAD_ID = waits.REQUESTING_THREAD_ID
                       WHERE threads.PROCESSLIST_ID = %s""",
                    (connection_id,),
                ).scalar()
            if waiting:
                return True
            sleep(0.025)
        return False

    if binding_locks_first:
        binder = Thread(target=bind_project, name="project-binder", daemon=True)
        binder.start()
        assert binding_cleanup_entered.wait(timeout=15), "绑定事务未取得项目锁并进入清理"
        ingester = Thread(target=ingest_project, name="project-ingester", daemon=True)
        ingester.start()
        assert ingestion_started.wait(timeout=15)
        deadline = monotonic() + 5
        while not ingestion_connection_id and monotonic() < deadline:
            sleep(0.01)
        assert ingestion_connection_id, "入库线程未暴露 MySQL 连接"
        assert wait_for_mysql_lock_wait(ingestion_connection_id[0]), "入库未在绑定持有项目锁时进入行锁等待"
        assert not embedding_started.is_set(), "绑定持有项目锁时不应开始远端嵌入"
        release_binding.set()
        assert binding_done.wait(timeout=15)
        # If a regression allows ingestion through after binding, let its fake
        # embedding finish so the failure is reported as an outcome, not a hang.
        release_embedding.set()
        assert ingestion_done.wait(timeout=15)
    else:
        ingester = Thread(target=ingest_project, name="project-ingester", daemon=True)
        ingester.start()
        assert embedding_started.wait(timeout=15), "入库未在持有项目锁时开始嵌入"
        binder = Thread(target=bind_project, name="project-binder", daemon=True)
        binder.start()
        assert binding_started.wait(timeout=15)
        deadline = monotonic() + 5
        while not binder_connection_id and monotonic() < deadline:
            sleep(0.01)
        assert binder_connection_id, "绑定线程未暴露 MySQL 连接"
        assert wait_for_mysql_lock_wait(binder_connection_id[0]), "绑定未在入库持有项目锁时进入行锁等待"
        assert not binding_done.is_set(), f"锁等待期间绑定事务却已结束；并发结果={outcomes!r}"
        release_embedding.set()
        assert ingestion_done.wait(timeout=15)
        assert binding_done.wait(timeout=15)

    binder.join(timeout=5)
    ingester.join(timeout=5)
    assert not binder.is_alive() and not ingester.is_alive(), "项目绑定/知识入库线程未结束"
    assert not [item for label, item in outcomes if label == "binding"], outcomes

    with sessions() as db:
        docs = db.query(AgentKnowledgeDoc).filter_by(source_id=source_id).all()
        source = db.get(AgentKnowledgeSource, source_id)
        retired_personal_doc = db.get(KnowledgeDoc, personal_doc.id)
        assert retired_personal_doc is not None and retired_personal_doc.status == "deleted"
        assert db.query(KnowledgeChunk).filter_by(doc_id=personal_doc.id).count() == 0
        # Direct inserts model legacy rows that predate the new write guard. They
        # must remain hidden even if cleanup was missed or imported old data exists.
        stale_agent_docs = [
            AgentKnowledgeDoc(
                agent_code="manager",
                source_type="project",
                source_ref=ref,
                title=f"legacy bound source {index}",
                risk_level="low",
                confidence=1.0,
                status="active",
                char_count=12,
                chunk_count=0,
            )
            for index, ref in enumerate((f"project:{project_id}", f"code_file:{file_id}"))
        ]
        stale_personal_doc = KnowledgeDoc(
            user_id=owner_id,
            source_type="code",
            source_ref=f"file:{file_id}",
            title="legacy active personal source",
            char_count=20,
            chunk_count=0,
            status="active",
        )
        db.add_all([*stale_agent_docs, stale_personal_doc])
        db.flush()
        assert not db.query(KnowledgeDoc.id).filter(
            KnowledgeDoc.id == stale_personal_doc.id,
            knowledge_service.personal_source_access_clause(),
        ).first()
        assert agent_knowledge.list_docs(db, user_id=owner_id) == []
        if binding_locks_first:
            assert not any(label == "ingestion" and isinstance(item, int) for label, item in outcomes)
            assert any(label == "ingestion" and isinstance(item, ForbiddenError) for label, item in outcomes)
            assert embedding_calls == []
            assert docs == []
        else:
            assert any(label == "ingestion" and isinstance(item, int) for label, item in outcomes)
            assert len(embedding_calls) == 1
            if ingestion_kind == "agent":
                assert len(docs) == 1 and docs[0].status == "deleted"
                assert db.query(AgentKnowledgeChunk).filter_by(doc_id=docs[0].id).count() == 0
            else:
                personal_written = db.query(KnowledgeDoc).filter_by(
                    user_id=owner_id, source_ref=f"file:{file_id}", title="personal project code",
                ).one()
                assert personal_written.status == "deleted"
                assert db.query(KnowledgeChunk).filter_by(doc_id=personal_written.id).count() == 0
        assert source.enabled == 0 and source.whitelist == 0


@pytest.mark.parametrize("access_method", ["list", "get", "search"])
@pytest.mark.parametrize("mutation", ["member_removal", "attachment_deletion"])
def test_enterprise_attachment_reads_see_member_removal_after_mysql_rr_snapshot(
    fresh_chain, access_method: str, mutation: str,
) -> None:
    """RR snapshots must not hide a committed membership revocation or attachment deletion."""
    from uuid import uuid4

    from sqlalchemy.orm import sessionmaker

    from app.core.exceptions import ForbiddenError
    from app.models.enterprise_knowledge import EnterpriseKnowledgeAttachment, EnterpriseKnowledgeAttachmentChunk
    from app.models.enterprise_org import Enterprise, EnterpriseMember
    from app.models.user import User
    from app.services import enterprise_knowledge_attachment_service as attachment_service
    from app.services import enterprise_org_service as org

    suffix = uuid4().hex[:12]
    sessions = sessionmaker(bind=fresh_chain, expire_on_commit=False)
    with sessions.begin() as db:
        owner = User(username=f"attachment-owner-{suffix}", password="x", role="user", status=1)
        member = User(username=f"attachment-member-{suffix}", password="x", role="user", status=1)
        db.add_all([owner, member])
        db.flush()
        enterprise = Enterprise(
            name=f"附件撤權測試-{suffix}", owner_id=owner.id, seat_limit=3,
            status="active", paid_order_id=f"attachment-removal-{suffix}",
        )
        db.add(enterprise)
        db.flush()
        db.add_all([
            EnterpriseMember(enterprise_id=enterprise.id, user_id=owner.id, role="owner", status="active"),
            EnterpriseMember(enterprise_id=enterprise.id, user_id=member.id, role="member", status="active"),
        ])
        attachment = EnterpriseKnowledgeAttachment(
            enterprise_id=enterprise.id, author_id=owner.id, updated_by=owner.id,
            client_request_id=f"request-{suffix}", create_hash="a" * 64, file_name="保密.txt",
            file_ext=".txt", mime="text/plain", file_size=6, source_sha256="b" * 64,
            chunk_count=1, original_data=b"secret", extracted_text="secret", version=1, status="active",
        )
        db.add(attachment)
        db.flush()
        db.add(EnterpriseKnowledgeAttachmentChunk(
            enterprise_id=enterprise.id, attachment_id=attachment.id, seq=0,
            content="secret confidential", embedding="[1.0,0.0]", embed_model="local:hash-v1:2",
        ))
        enterprise_id, owner_id, member_id, attachment_id = (
            int(enterprise.id), int(owner.id), int(member.id), int(attachment.id),
        )

    with sessions() as reader:
        reader.connection().exec_driver_sql("SET SESSION TRANSACTION ISOLATION LEVEL REPEATABLE READ")
        # Match the authenticated HTTP route: loading User creates the transaction's
        # consistent-read snapshot before service authorization is checked.
        assert reader.query(User.id).filter_by(id=member_id).scalar() == member_id
        with sessions.begin() as changer:
            if mutation == "member_removal":
                org.remove_enterprise_member(changer, enterprise_id, owner_id, member_id)
            else:
                attachment_service.delete_attachment(
                    changer, enterprise_id, owner_id, attachment_id, expected_version=1,
                )
        if mutation == "member_removal":
            with pytest.raises(ForbiddenError):
                if access_method == "list":
                    attachment_service.list_attachments(reader, enterprise_id, member_id)
                elif access_method == "get":
                    attachment_service.get_attachment(reader, enterprise_id, member_id, attachment_id)
                else:
                    attachment_service.search_attachments(reader, enterprise_id, member_id, "secret")
        elif access_method == "list":
            assert attachment_service.list_attachments(reader, enterprise_id, member_id) == []
        elif access_method == "get":
            from app.core.exceptions import NotFoundError

            with pytest.raises(NotFoundError):
                attachment_service.get_attachment(reader, enterprise_id, member_id, attachment_id)
        else:
            assert attachment_service.search_attachments(reader, enterprise_id, member_id, "secret") == []


@pytest.mark.parametrize("access_method", ["list", "get", "search", "member", "chat", "knowledge"])
@pytest.mark.parametrize("mutation", ["refund_processing", "member_removal", "admin_demotion_and_refund"])
def test_enterprise_read_and_mutation_gates_use_fresh_refund_snapshot_after_mysql_rr_snapshot(
    fresh_chain, access_method: str, mutation: str,
) -> None:
    """Current billing and membership state must override an older request snapshot."""
    from datetime import datetime, timedelta, timezone
    from uuid import uuid4

    from sqlalchemy.orm import sessionmaker

    from app.core.exceptions import ForbiddenError
    from app.models.billing_ledger import (
        BillingOrder,
        BillingPaymentEvent,
        BillingRefund,
        BillingSubscription,
        BillingSubscriptionPeriod,
    )
    from app.models.enterprise_org import Enterprise, EnterpriseMember
    from app.models.user import User
    from app.services import enterprise_chat_service as chat_service
    from app.services import enterprise_knowledge_attachment_service as attachment_service
    from app.services import enterprise_knowledge_service as knowledge_service
    from app.services import enterprise_org_service as org_service

    suffix = uuid4().hex[:12]
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    sessions = sessionmaker(bind=fresh_chain, expire_on_commit=False)
    with sessions.begin() as db:
        owner = User(username=f"refund-owner-{suffix}", password="x", role="user", status=1)
        member = User(username=f"refund-member-{suffix}", password="x", role="user", status=1)
        db.add_all([owner, member])
        db.flush()
        order_no = f"attachment-refund-{suffix}"
        enterprise = Enterprise(
            name=f"附件退款快照-{suffix}", owner_id=owner.id, seat_limit=3,
            status="active", paid_order_id=order_no,
        )
        db.add(enterprise)
        db.flush()
        db.add_all([
            EnterpriseMember(enterprise_id=enterprise.id, user_id=owner.id, role="owner", status="active"),
            EnterpriseMember(
                enterprise_id=enterprise.id, user_id=member.id,
                role="admin" if mutation == "admin_demotion_and_refund" else "member", status="active",
            ),
        ])
        db.add(BillingOrder(
            order_no=order_no, user_id=owner.id, enterprise_id=enterprise.id, product="enterprise_create",
            amount_fen=100, quantity=3, channel="alipay", status="paid", payment_status="paid",
            paid_at=now, created_at=now,
        ))
        db.add(BillingPaymentEvent(
            channel="alipay", trade_id=f"trade-{suffix}", order_no=order_no,
            amount_fen=100, verified_at=now,
        ))
        db.flush()
        subscription = BillingSubscription(
            owner_type="enterprise", owner_id=enterprise.id, product="enterprise",
            anchor_at=now, last_end_at=now + timedelta(days=30), next_offset=1,
        )
        db.add(subscription)
        db.flush()
        db.add(BillingSubscriptionPeriod(
            subscription_id=subscription.id, order_no=order_no, starts_at=now - timedelta(minutes=1),
            ends_at=now + timedelta(days=30), credit_milli=1_000_000, seats=3,
        ))
        refund = BillingRefund(
            order_no=order_no, refund_no=f"refund-{suffix}", amount_fen=100,
            reason="退款处理中快照测试", status="requested", requested_by=owner.id,
            created_at=now, updated_at=now,
        )
        db.add(refund)
        db.flush()
        enterprise_id, owner_id, member_id, refund_id = (
            int(enterprise.id), int(owner.id), int(member.id), int(refund.id),
        )

    with sessions() as reader:
        reader.connection().exec_driver_sql("SET SESSION TRANSACTION ISOLATION LEVEL REPEATABLE READ")
        # Match authenticated HTTP execution: the User lookup establishes a consistent-read snapshot.
        assert reader.query(User.id).filter_by(id=member_id).scalar() == member_id
        with sessions.begin() as changer:
            if mutation == "refund_processing":
                changer.query(BillingRefund).filter_by(id=refund_id).one().status = "processing"
            elif mutation == "member_removal":
                org_service.remove_enterprise_member(changer, enterprise_id, owner_id, member_id)
            else:
                changer.query(EnterpriseMember).filter_by(
                    enterprise_id=enterprise_id, user_id=member_id,
                ).one().role = "member"
                changer.query(BillingRefund).filter_by(id=refund_id).one().status = "processing"

        with pytest.raises(ForbiddenError) as blocked:
            if access_method == "list":
                attachment_service.list_attachments(reader, enterprise_id, member_id)
            elif access_method == "get":
                attachment_service.get_attachment(reader, enterprise_id, member_id, attachment_id=1)
            elif access_method == "search":
                attachment_service.search_attachments(reader, enterprise_id, member_id, "needle")
            elif access_method == "member":
                org_service.require_enterprise_member(reader, enterprise_id, member_id)
            elif access_method == "chat":
                chat_service.list_conversations(reader, enterprise_id, member_id)
            else:
                knowledge_service.list_entries(reader, enterprise_id, member_id)
        if mutation == "refund_processing":
            assert "退款处理中" in str(blocked.value)


def test_enterprise_execution_and_invitation_mutations_use_fresh_refund_state_after_mysql_rr_snapshot(
    fresh_chain,
) -> None:
    """Paid execution and membership writes must observe a committed refund transition."""
    import hashlib
    from datetime import datetime, timedelta, timezone
    from uuid import uuid4

    from sqlalchemy.orm import sessionmaker

    from app.core.exceptions import ConflictError, ForbiddenError
    from app.models.billing_ledger import (
        BillingOrder,
        BillingPaymentEvent,
        BillingRefund,
        BillingSubscription,
        BillingSubscriptionPeriod,
    )
    from app.models.enterprise_org import Enterprise, EnterpriseInvitation, EnterpriseMember
    from app.models.user import User
    from app.services import enterprise_org_service as org_service

    suffix = uuid4().hex[:12]
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    token = f"invite-{suffix}"
    sessions = sessionmaker(bind=fresh_chain, expire_on_commit=False)
    with sessions.begin() as db:
        owner = User(username=f"billing-owner-{suffix}", password="x", role="user", status=1)
        admin = User(username=f"billing-admin-{suffix}", password="x", role="user", status=1)
        db.add_all([owner, admin])
        db.flush()
        order_no = f"billing-gate-refund-{suffix}"
        company = Enterprise(
            name=f"账务门禁快照-{suffix}", owner_id=owner.id, seat_limit=4,
            status="active", paid_order_id=order_no,
        )
        db.add(company)
        db.flush()
        db.add_all([
            EnterpriseMember(enterprise_id=company.id, user_id=owner.id, role="owner", status="active"),
            EnterpriseMember(enterprise_id=company.id, user_id=admin.id, role="admin", status="active"),
            EnterpriseInvitation(
                enterprise_id=company.id,
                token_hash=hashlib.sha256(token.encode()).hexdigest(),
                created_by=owner.id,
                expires_at=now + timedelta(hours=1),
                max_uses=1,
                use_count=0,
            ),
            BillingOrder(
                order_no=order_no, user_id=owner.id, enterprise_id=company.id, product="enterprise_create",
                amount_fen=100, quantity=4, channel="alipay", status="paid", payment_status="paid",
                paid_at=now, created_at=now,
            ),
        ])
        db.add(BillingPaymentEvent(
            channel="alipay", trade_id=f"trade-{suffix}", order_no=order_no,
            amount_fen=100, verified_at=now,
        ))
        db.flush()
        subscription = BillingSubscription(
            owner_type="enterprise", owner_id=company.id, product="enterprise",
            anchor_at=now, last_end_at=now + timedelta(days=30), next_offset=1,
        )
        db.add(subscription)
        db.flush()
        db.add(BillingSubscriptionPeriod(
            subscription_id=subscription.id, order_no=order_no, starts_at=now - timedelta(minutes=1),
            ends_at=now + timedelta(days=30), credit_milli=1_000_000, seats=4,
        ))
        refund = BillingRefund(
            order_no=order_no, refund_no=f"refund-{suffix}", amount_fen=100,
            reason="付费执行和邀请快照测试", status="requested", requested_by=owner.id,
            created_at=now, updated_at=now,
        )
        db.add(refund)
        db.flush()
        enterprise_id, admin_id, refund_id = int(company.id), int(admin.id), int(refund.id)

    with sessions() as reader:
        reader.connection().exec_driver_sql("SET SESSION TRANSACTION ISOLATION LEVEL REPEATABLE READ")
        assert reader.query(User.id).filter_by(id=admin_id).scalar() == admin_id
        with sessions.begin() as changer:
            changer.query(BillingRefund).filter_by(id=refund_id).one().status = "processing"

        with pytest.raises(ForbiddenError, match="计费已暂停"):
            org_service.require_enterprise_execution(reader, enterprise_id)
        with pytest.raises(ConflictError, match="退款处理中"):
            org_service.create_invitation(reader, enterprise_id, admin_id)
        with pytest.raises(ConflictError, match="退款处理中"):
            org_service.accept_invitation(reader, token, admin_id)


def test_enterprise_xiaoling_tool_write_observes_refund_after_mysql_rr_snapshot(fresh_chain, monkeypatch) -> None:
    """A long-lived model run Session must not dispatch write tools after refund processing commits."""
    import asyncio
    import json
    from datetime import datetime, timedelta, timezone
    from uuid import uuid4

    from sqlalchemy.orm import sessionmaker

    from app.models.billing_ledger import (
        BillingOrder,
        BillingPaymentEvent,
        BillingRefund,
        BillingSubscription,
        BillingSubscriptionPeriod,
    )
    from app.models.enterprise_assistant_runtime import (
        EnterpriseAssistantApproval,
        EnterpriseAssistantRuntimeState,
    )
    from app.models.enterprise_chat import EnterpriseAssistantRun, EnterpriseConversation
    from app.models.enterprise_org import Enterprise, EnterpriseMember
    from app.models.user import User
    from app.services import enterprise_assistant_runtime_service as runtime
    from app.services.deepseek_responses_runtime import ToolCall

    suffix = uuid4().hex[:12]
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    sessions = sessionmaker(bind=fresh_chain, expire_on_commit=False, autoflush=False)
    with sessions.begin() as db:
        owner = User(username=f"xiaoling-refund-owner-{suffix}", password="x", role="user", status=1)
        admin = User(username=f"xiaoling-refund-admin-{suffix}", password="x", role="user", status=1)
        db.add_all([owner, admin])
        db.flush()
        order_no = f"xiaoling-refund-{suffix}"
        company = Enterprise(
            name=f"小菱退款快照-{suffix}", owner_id=owner.id, seat_limit=4,
            status="active", paid_order_id=order_no,
        )
        db.add(company)
        db.flush()
        conversation = EnterpriseConversation(
            enterprise_id=company.id, kind="company", conversation_key=f"company:{company.id}",
            group_id=None, created_by=owner.id,
        )
        db.add_all([
            conversation,
            EnterpriseMember(enterprise_id=company.id, user_id=owner.id, role="owner", status="active"),
            EnterpriseMember(enterprise_id=company.id, user_id=admin.id, role="admin", status="active"),
            BillingOrder(
                order_no=order_no, user_id=owner.id, enterprise_id=company.id, product="enterprise_create",
                amount_fen=100, quantity=4, channel="alipay", status="paid", payment_status="paid",
                paid_at=now, created_at=now,
            ),
        ])
        db.add(BillingPaymentEvent(
            channel="alipay", trade_id=f"trade-{suffix}", order_no=order_no,
            amount_fen=100, verified_at=now,
        ))
        db.flush()
        subscription = BillingSubscription(
            owner_type="enterprise", owner_id=company.id, product="enterprise",
            anchor_at=now, last_end_at=now + timedelta(days=30), next_offset=1,
        )
        db.add(subscription)
        db.flush()
        db.add(BillingSubscriptionPeriod(
            subscription_id=subscription.id, order_no=order_no, starts_at=now - timedelta(minutes=1),
            ends_at=now + timedelta(days=30), credit_milli=1_000_000, seats=4,
        ))
        refund = BillingRefund(
            order_no=order_no, refund_no=f"refund-{suffix}", amount_fen=100,
            reason="模型等待期间退款快照测试", status="requested", requested_by=owner.id,
            created_at=now, updated_at=now,
        )
        db.add(refund)
        run = EnterpriseAssistantRun(
            public_id=f"ear-refund-{suffix}", request_key=f"ear-refund-request-{suffix}",
            enterprise_id=company.id, conversation_id=conversation.id, group_id=None,
            initiator_user_id=admin.id, source_message_id=1, status="running", attempt_count=1,
            prompt_sha256="a" * 64,
        )
        db.add(run)
        db.flush()
        state = EnterpriseAssistantRuntimeState(
            assistant_run_id=run.id, enterprise_id=company.id, conversation_id=conversation.id,
            group_id=None, initiator_user_id=admin.id, source_message_id=1,
            status="running", checkpoint_json="{}", permission_snapshot_json="{}",
            permission_snapshot_sha256=runtime._digest({}), stop_requested=False, cancel_reason="",
            lease_generation=4, billing_invocation_count=1, version=1,
        )
        db.add(state)
        db.flush()
        enterprise_id, conversation_id, run_id = int(company.id), int(conversation.id), run.public_id
        admin_id, refund_id = int(admin.id), int(refund.id)

    monkeypatch.setattr(runtime, "_live_role_snapshot", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(runtime.assistant_service, "_require_active_limit", lambda *_args, **_kwargs: None)
    with sessions() as reader:
        reader.connection().exec_driver_sql("SET SESSION TRANSACTION ISOLATION LEVEL REPEATABLE READ")
        assert reader.query(User.id).filter_by(id=admin_id).scalar() == admin_id
        with sessions.begin() as changer:
            changer.query(BillingRefund).filter_by(id=refund_id).one().status = "processing"

        executor = runtime.EnterpriseAssistantToolExecutor(
            reader, enterprise_id=enterprise_id, conversation_id=conversation_id,
            run_public_id=run_id, lease_generation=4, actor_user_id=admin_id,
        )
        result = asyncio.run(executor.execute(ToolCall(
            call_id="announce-after-refund", name="propose_enterprise_announcement",
            arguments={"title": "不应创建", "content": "退款处理中不得新增审批"},
            raw_arguments=json.dumps({"title": "不应创建", "content": "退款处理中不得新增审批"}),
        )))
        assert result.status == "error"
        assert result.failure_kind == "authorization"
        assert "计费已暂停" in result.error
        assert reader.query(EnterpriseAssistantApproval).filter_by(
            run_public_id=run_id, call_id="announce-after-refund",
        ).count() == 0


def test_enterprise_approval_cancel_lock_order_serializes_on_mysql(fresh_chain, monkeypatch) -> None:
    """Real service calls must serialize approval against cancellation on the InnoDB run lock."""
    import asyncio
    import json
    from threading import Event, Thread
    from uuid import uuid4

    from sqlalchemy import event
    from sqlalchemy.orm import Session as OrmSession
    from sqlalchemy.orm import sessionmaker

    from app.core.exceptions import ConflictError
    from app.models.enterprise_assistant_runtime import EnterpriseAssistantApproval, EnterpriseAssistantRuntimeState
    from app.models.enterprise_chat import EnterpriseAssistantRun, EnterpriseConversation
    from app.models.enterprise_org import Enterprise, EnterpriseMember
    from app.models.user import User
    from app.services import enterprise_assistant_runtime_service as runtime
    from app.services.deepseek_responses_runtime import PendingAction, RunCheckpoint, ToolCall

    suffix = uuid4().hex[:12]
    public_id = f"approval-lock-{suffix}"
    call_id = f"call-{suffix}"
    sessions = sessionmaker(bind=fresh_chain, expire_on_commit=False)
    with sessions.begin() as db:
        initiator = User(username=f"approval-initiator-{suffix}", password="x", role="user", status=1)
        approver = User(username=f"approval-admin-{suffix}", password="x", role="user", status=1)
        db.add_all([initiator, approver])
        db.flush()
        enterprise = Enterprise(
            name=f"审批锁序测试-{suffix}", owner_id=approver.id, seat_limit=3,
            status="active", paid_order_id=f"approval-lock-order-{suffix}",
        )
        db.add(enterprise)
        db.flush()
        room = EnterpriseConversation(
            enterprise_id=enterprise.id, kind="company", conversation_key="company",
            created_by=initiator.id,
        )
        db.add_all([
            EnterpriseMember(enterprise_id=enterprise.id, user_id=initiator.id, role="member", status="active"),
            EnterpriseMember(enterprise_id=enterprise.id, user_id=approver.id, role="admin", status="active"),
        ])
        db.add(room)
        db.flush()
        args = {"title": "锁序测试公告", "content": "仅验证审批与取消串行化。"}
        args_json = json.dumps(args, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        run = EnterpriseAssistantRun(
            public_id=public_id, request_key=f"approval-lock-request-{suffix}",
            enterprise_id=enterprise.id, conversation_id=room.id, initiator_user_id=initiator.id,
            source_message_id=900004, status="awaiting_approval", attempt_count=1,
            prompt_sha256="a" * 64,
        )
        db.add(run)
        db.flush()
        approval = EnterpriseAssistantApproval(
            assistant_run_id=run.id, enterprise_id=enterprise.id, conversation_id=room.id,
            initiator_user_id=initiator.id, source_message_id=900004, run_public_id=public_id,
            call_id=call_id, tool_name="propose_enterprise_announcement",
            required_role="admin", status="pending", arguments_json=args_json,
            arguments_sha256=runtime._digest(args),
        )
        db.add(approval)
        db.flush()
        checkpoint = RunCheckpoint(
            run_id=public_id, model="test", transcript=[], tools=[], status=runtime.WAITING_APPROVAL,
            pending=PendingAction(
                kind="approval",
                call=ToolCall(call_id=call_id, name="propose_enterprise_announcement",
                              arguments=args, raw_arguments=args_json),
                approval_id=int(approval.id),
            ),
        )
        db.add(EnterpriseAssistantRuntimeState(
            assistant_run_id=run.id, enterprise_id=enterprise.id, conversation_id=room.id,
            initiator_user_id=initiator.id, source_message_id=900004, status=runtime.WAITING_APPROVAL,
            checkpoint_json=runtime._json(checkpoint.to_dict()), permission_snapshot_json="{}",
            permission_snapshot_sha256="c" * 64,
        ))
        enterprise_id, conversation_id = int(enterprise.id), int(room.id)
        initiator_id, approver_id, approval_id, run_id = (
            int(initiator.id), int(approver.id), int(approval.id), int(run.id),
        )

    cancel_has_run_lock = Event()
    allow_cancel_to_commit = Event()
    decision_run_sql_sent = Event()
    decision_finished = Event()
    outcomes: dict[str, object] = {}

    class TrackingSession(OrmSession):
        pass

    @event.listens_for(TrackingSession, "after_begin")
    def _tag_worker(session, _transaction, connection):
        connection.info["approval_test_worker"] = session.info.get("approval_test_worker")

    @event.listens_for(fresh_chain, "before_cursor_execute")
    def _observe_decision_run_lock(connection, _cursor, statement, _parameters, _context, _executemany):
        if (connection.info.get("approval_test_worker") == "decision"
                and "enterprise_assistant_run" in statement.lower()
                and "for update" in statement.lower()):
            decision_run_sql_sent.set()

    real_run_record = runtime._run_record

    def _hold_cancel_after_real_run_lock(db, *args, lock=False, **kwargs):
        row = real_run_record(db, *args, lock=lock, **kwargs)
        if lock and db.info.get("approval_test_worker") == "cancel":
            cancel_has_run_lock.set()
            if not allow_cancel_to_commit.wait(timeout=10):
                raise TimeoutError("测试未释放取消事务的运行行锁")
        return row

    def _tracked_factory(db):
        worker = db.info["approval_test_worker"]
        return sessionmaker(
            bind=fresh_chain, class_=TrackingSession, expire_on_commit=False, autoflush=False,
            info={"approval_test_worker": worker},
        )

    monkeypatch.setattr(runtime, "_factory", _tracked_factory)
    monkeypatch.setattr(runtime, "_run_record", _hold_cancel_after_real_run_lock)
    monkeypatch.setattr(runtime, "_live_role_snapshot", lambda *_args: {})
    monkeypatch.setattr(runtime.assistant_service, "_require_subscription", lambda *_args: None)

    def _cancel():
        try:
            with sessions() as db:
                db.info["approval_test_worker"] = "cancel"
                outcomes["cancel"] = runtime.cancel_run(
                    db, enterprise_id=enterprise_id, user_id=initiator_id,
                    conversation_id=conversation_id, run_public_id=public_id, reason="竞态测试",
                )
        except Exception as exc:  # pragma: no cover - asserted by the test
            outcomes["cancel_error"] = exc

    def _decide():
        try:
            with sessions() as db:
                db.info["approval_test_worker"] = "decision"
                outcomes["decision"] = asyncio.run(runtime.decide_approval(
                    db, enterprise_id=enterprise_id, approval_id=approval_id,
                    user_id=approver_id, decision="approve",
                ))
        except Exception as exc:
            outcomes["decision_error"] = exc
        finally:
            decision_finished.set()

    cancel_thread = Thread(target=_cancel, daemon=True)
    decision_thread = Thread(target=_decide, daemon=True)
    try:
        cancel_thread.start()
        assert cancel_has_run_lock.wait(timeout=10), "取消服务未取得真实运行行锁"
        decision_thread.start()
        assert decision_run_sql_sent.wait(timeout=10), "审批服务未向 MySQL 发出运行锁查询"
        assert not decision_finished.wait(timeout=0.25), "审批服务未等待已被取消事务持有的运行锁"
        allow_cancel_to_commit.set()
        cancel_thread.join(timeout=12)
        decision_thread.join(timeout=12)
        assert not cancel_thread.is_alive() and not decision_thread.is_alive(), "审批/取消服务调用悬挂"
        assert "cancel_error" not in outcomes, outcomes
        assert outcomes.get("cancel", {}).get("status") == runtime.CANCELLED
        assert isinstance(outcomes.get("decision_error"), ConflictError), outcomes
        with sessions() as db:
            state = db.query(EnterpriseAssistantRuntimeState).filter_by(assistant_run_id=run_id).one()
            approval = db.query(EnterpriseAssistantApproval).filter_by(id=approval_id).one()
        assert state.status == runtime.CANCELLED
        assert approval.status == "cancelled"
        assert approval.decided_by is None
    finally:
        allow_cancel_to_commit.set()
        event.remove(TrackingSession, "after_begin", _tag_worker)
        event.remove(fresh_chain, "before_cursor_execute", _observe_decision_run_lock)


def test_enterprise_invitation_acceptance_uses_fresh_seat_count_after_mysql_rr_wait(
    fresh_chain, monkeypatch,
) -> None:
    """A company row lock alone cannot refresh a stale RR member-count snapshot."""
    from threading import Event, Thread
    from uuid import uuid4

    from sqlalchemy import event
    from sqlalchemy.orm import Session as OrmSession
    from sqlalchemy.orm import sessionmaker

    from app.core.exceptions import ConflictError
    from app.models.enterprise_org import Enterprise, EnterpriseMember
    from app.models.user import User
    from app.services import enterprise_org_service as org

    suffix = uuid4().hex[:12]
    sessions = sessionmaker(bind=fresh_chain, expire_on_commit=False)
    with sessions.begin() as db:
        owner = User(username=f"seat-owner-{suffix}", password="x", role="user", status=1)
        first = User(username=f"seat-first-{suffix}", password="x", role="user", status=1)
        second = User(username=f"seat-second-{suffix}", password="x", role="user", status=1)
        db.add_all([owner, first, second])
        db.flush()
        company = Enterprise(
            name=f"席位并发-{suffix}", owner_id=owner.id, seat_limit=2,
            status="active", paid_order_id=f"seat-race-{suffix}",
        )
        db.add(company)
        db.flush()
        db.add(EnterpriseMember(
            enterprise_id=company.id, user_id=owner.id, role="owner", status="active",
        ))
        first_token = org.create_invitation(db, int(company.id), int(owner.id))
        second_token = org.create_invitation(db, int(company.id), int(owner.id))
        enterprise_id = int(company.id)
        first_id, second_id = int(first.id), int(second.id)

    first_has_company_lock = Event()
    allow_first_to_finish = Event()
    second_company_lock_sent = Event()
    second_finished = Event()
    outcomes: dict[str, object] = {}

    class TrackingSession(OrmSession):
        pass

    worker_sessions = sessionmaker(bind=fresh_chain, class_=TrackingSession, expire_on_commit=False)

    @event.listens_for(TrackingSession, "after_begin")
    def _tag_worker(session, _transaction, connection):
        connection.info["seat_test_worker"] = session.info.get("seat_test_worker")

    @event.listens_for(fresh_chain, "before_cursor_execute")
    def _observe_second_company_lock(connection, _cursor, statement, _parameters, _context, _executemany):
        if (connection.info.get("seat_test_worker") == "second"
                and "enterprise" in statement.lower()
                and "for update" in statement.lower()):
            second_company_lock_sent.set()

    real_company = org._company

    def _hold_first_after_company_lock(db, company_id, *, lock=False):
        row = real_company(db, company_id, lock=lock)
        if lock and db.info.get("seat_test_worker") == "first":
            first_has_company_lock.set()
            if not allow_first_to_finish.wait(timeout=10):
                raise TimeoutError("测试未释放首个邀请事务持有的企业行锁")
        return row

    monkeypatch.setattr(org, "_company", _hold_first_after_company_lock)

    def _accept(name: str, token: str, user_id: int) -> None:
        try:
            with worker_sessions() as db:
                db.info["seat_test_worker"] = name
                with db.begin():
                    outcomes[name] = org.accept_invitation(db, token, user_id)
        except Exception as exc:  # pragma: no cover - asserted by the test
            outcomes[f"{name}_error"] = exc
        finally:
            if name == "second":
                second_finished.set()

    first_thread = Thread(target=_accept, args=("first", first_token, first_id), daemon=True)
    second_thread = Thread(target=_accept, args=("second", second_token, second_id), daemon=True)
    try:
        first_thread.start()
        assert first_has_company_lock.wait(timeout=10), "首个接受邀请服务未持有企业行锁"
        second_thread.start()
        assert second_company_lock_sent.wait(timeout=10), "第二个接受邀请服务未发出企业锁查询"
        assert not second_finished.wait(timeout=0.25), "第二个接受邀请未等待首个企业事务提交"
        allow_first_to_finish.set()
        first_thread.join(timeout=12)
        second_thread.join(timeout=12)
        assert not first_thread.is_alive() and not second_thread.is_alive(), "邀请接受事务悬挂"
        assert "first_error" not in outcomes, outcomes
        assert "first" in outcomes, outcomes
        assert isinstance(outcomes.get("second_error"), ConflictError), outcomes
        assert "席位不足" in str(outcomes["second_error"])
        with sessions() as db:
            active_count = db.query(EnterpriseMember).filter_by(
                enterprise_id=enterprise_id, status="active",
            ).count()
        assert active_count == 2
    finally:
        allow_first_to_finish.set()
        event.remove(TrackingSession, "after_begin", _tag_worker)
        event.remove(fresh_chain, "before_cursor_execute", _observe_second_company_lock)


def test_subscription_period_activation_recovers_after_rollback_and_serializes_mysql_runners(
    fresh_chain, monkeypatch,
) -> None:
    """A lost worker transaction is retryable, while concurrent schedulers grant/apply seats once."""
    from datetime import datetime, timezone
    from threading import Event, Thread
    from time import monotonic, sleep
    from uuid import uuid4

    from sqlalchemy.orm import sessionmaker

    from app.models.billing_ledger import (
        BillingCreditBatch,
        BillingOrder,
        BillingPaymentEvent,
        BillingSubscription,
        BillingSubscriptionPeriod,
        BillingWallet,
    )
    from app.models.enterprise_org import Enterprise, EnterpriseMember
    from app.models.user import User
    from app.services import billing_checkout_service as checkout

    suffix = uuid4().hex[:12]
    observed_at = datetime.now(timezone.utc).replace(tzinfo=None)
    starts_at = observed_at - timedelta(minutes=1)
    ends_at = observed_at + timedelta(days=30)
    sessions = sessionmaker(bind=fresh_chain, expire_on_commit=False)
    with sessions.begin() as db:
        owner = User(username=f"period-owner-{suffix}", password="x", role="user", status=1)
        db.add(owner)
        db.flush()
        company = Enterprise(
            name=f"周期并发-{suffix}", owner_id=owner.id, seat_limit=2,
            status="active", paid_order_id=f"period-paid-{suffix}",
        )
        db.add(company)
        db.flush()
        db.add_all([
            EnterpriseMember(
                enterprise_id=company.id, user_id=owner.id, role="owner", status="active",
            ),
            BillingWallet(owner_type="enterprise", owner_id=company.id),
        ])
        order_no = f"period-paid-{suffix}"
        db.add(BillingSubscription(
            owner_type="enterprise", owner_id=company.id, product="enterprise",
            anchor_at=starts_at, last_end_at=ends_at, next_offset=2,
        ))
        db.flush()
        subscription = db.query(BillingSubscription).filter_by(
            owner_type="enterprise", owner_id=company.id, product="enterprise",
        ).one()
        order = BillingOrder(
            order_no=order_no, user_id=owner.id, enterprise_id=company.id,
            product="enterprise_renew", amount_fen=27600, quantity=4, channel="alipay",
            status="paid", payment_status="paid", paid_at=starts_at,
        )
        db.add(order)
        db.add(BillingPaymentEvent(
            channel="alipay", trade_id=f"period-trade-{suffix}", order_no=order_no,
            amount_fen=27600, verified_at=starts_at,
        ))
        period = BillingSubscriptionPeriod(
            subscription_id=subscription.id, order_no=order_no, starts_at=starts_at,
            ends_at=ends_at, credit_milli=20_000_000, seats=4,
        )
        db.add(period)
        db.flush()
        enterprise_id, period_id = int(company.id), int(period.id)

    # Simulate a worker/process stopping after activation logic returned but before commit.
    with sessions() as db:
        try:
            with db.begin():
                assert checkout.activate_due_period(db, period_id, now=observed_at)
                raise RuntimeError("simulate worker termination before transaction commit")
        except RuntimeError as exc:
            assert "before transaction commit" in str(exc)
    with sessions() as db:
        period = db.get(BillingSubscriptionPeriod, period_id)
        assert period is not None and period.granted_at is None
        assert db.query(BillingCreditBatch).filter_by(grant_key=f"period:{period_id}").count() == 0
        assert db.get(Enterprise, enterprise_id).seat_limit == 2

    first_has_grant = Event()
    allow_first_to_commit = Event()
    second_connection_ready = Event()
    second_finished = Event()
    outcomes: dict[str, object] = {}
    connection_ids: dict[str, int] = {}

    real_grant = checkout.ledger.grant

    def _hold_first_after_grant(db, **kwargs):
        batch = real_grant(db, **kwargs)
        if db.info.get("subscription_period_worker") == "first":
            first_has_grant.set()
            if not allow_first_to_commit.wait(timeout=20):
                raise TimeoutError("测试未释放首个周期激活事务")
        return batch

    monkeypatch.setattr(checkout.ledger, "grant", _hold_first_after_grant)

    def _activate(name: str) -> None:
        try:
            with sessions() as db:
                db.info["subscription_period_worker"] = name
                with db.begin():
                    connection_ids[name] = int(
                        db.connection().exec_driver_sql("SELECT CONNECTION_ID()").scalar_one()
                    )
                    if name == "second":
                        second_connection_ready.set()
                    outcomes[name] = checkout.activate_due_period(db, period_id, now=observed_at)
        except Exception as exc:  # pragma: no cover - asserted by the test
            outcomes[f"{name}_error"] = exc
        finally:
            if name == "second":
                second_finished.set()

    first_thread = Thread(target=_activate, args=("first",), daemon=True)
    second_thread = Thread(target=_activate, args=("second",), daemon=True)
    first_started = False
    second_started = False
    try:
        first_thread.start()
        first_started = True
        assert first_has_grant.wait(timeout=10), "首个周期激活未完成积分写入"
        second_thread.start()
        second_started = True
        assert second_connection_ready.wait(timeout=10), "第二个调度器未建立数据库连接"
        deadline = monotonic() + 8
        waiting = False
        while monotonic() < deadline and not second_finished.is_set():
            with fresh_chain.connect() as observer:
                waiting = bool(observer.exec_driver_sql(
                    """SELECT COUNT(*) FROM performance_schema.data_lock_waits AS waits
                       JOIN performance_schema.threads AS requester
                         ON requester.THREAD_ID = waits.REQUESTING_THREAD_ID
                       JOIN performance_schema.threads AS blocker
                         ON blocker.THREAD_ID = waits.BLOCKING_THREAD_ID
                       JOIN performance_schema.data_locks AS requested
                         ON requested.ENGINE_LOCK_ID = waits.REQUESTING_ENGINE_LOCK_ID
                        AND requested.ENGINE = waits.ENGINE
                       WHERE requester.PROCESSLIST_ID = %s AND blocker.PROCESSLIST_ID = %s
                         AND requested.OBJECT_SCHEMA = 'code_review'
                         AND requested.OBJECT_NAME = 'enterprise'
                         AND requested.INDEX_NAME = 'PRIMARY'
                         AND requested.LOCK_DATA = %s""",
                    (connection_ids["second"], connection_ids["first"], str(enterprise_id)),
                ).scalar_one())
            if waiting:
                break
            sleep(0.02)
        assert waiting, f"第二个调度器未等待首个事务持有的企业行锁: {outcomes}"
        assert not second_finished.is_set(), "第二个调度器没有等待首个周期事务"
        allow_first_to_commit.set()
        first_thread.join(timeout=12)
        second_thread.join(timeout=12)
        assert not first_thread.is_alive() and not second_thread.is_alive(), "周期激活事务悬挂"
        assert outcomes == {"first": True, "second": False}, outcomes
    finally:
        allow_first_to_commit.set()
        if first_started:
            first_thread.join(timeout=12)
        if second_started:
            second_thread.join(timeout=12)

    with sessions() as db:
        period = db.get(BillingSubscriptionPeriod, period_id)
        company = db.get(Enterprise, enterprise_id)
        batches = db.query(BillingCreditBatch).filter_by(grant_key=f"period:{period_id}").all()
        assert period is not None and period.granted_at is not None
        assert company is not None and company.seat_limit == 4
        assert len(batches) == 1 and batches[0].issued_milli == 20_000_000


@pytest.mark.parametrize("channel", ["alipay", "wechat"])
def test_duplicate_payment_callback_serializes_and_grants_mysql_order_once(
    fresh_chain, monkeypatch, channel: str,
) -> None:
    """Concurrent callbacks accepted by a test verifier create one credit grant."""
    from datetime import datetime, timezone
    from threading import Event, Thread
    from uuid import uuid4

    from sqlalchemy import event
    from sqlalchemy.orm import Session as OrmSession
    from sqlalchemy.orm import sessionmaker

    from app.models.billing_ledger import BillingCreditBatch, BillingOrder, BillingPaymentEvent, BillingWallet
    from app.models.user import User
    from app.services import billing_checkout_service as checkout
    from app.services import billing_rate_catalog_service
    from app.services.billing_ledger_service import available_milli

    suffix = uuid4().hex[:12]
    paid_at = datetime.now(timezone.utc).replace(tzinfo=None)
    sessions = sessionmaker(bind=fresh_chain, expire_on_commit=False)
    with sessions.begin() as db:
        buyer = User(username=f"payment-race-{suffix}", password="x", role="user", status=1)
        db.add(buyer)
        db.flush()
        order = checkout.create_order(
            db, user_id=int(buyer.id), product="personal_credits", quantity=2, channel=channel,
        )
        assert order.amount_fen == 2 * billing_rate_catalog_service.active_catalog(db).credit_pack_price_fen
        frozen_amount_fen = int(order.amount_fen)
        user_id, order_no = int(buyer.id), str(order.order_no)

    # Signature verification has dedicated RSA2 API tests; this integration isolates
    # MySQL transaction/idempotency behavior and never contacts a payment provider.
    trade_id = f"mysql-{channel}-replay-{suffix}"
    monkeypatch.setattr(checkout, f"verify_{channel}", lambda **_kwargs: {"trade_id": trade_id})

    first_has_grant = Event()
    allow_first_to_commit = Event()
    second_order_lock_sent = Event()
    second_finished = Event()
    outcomes: dict[str, object] = {}

    class TrackingSession(OrmSession):
        pass

    worker_sessions = sessionmaker(bind=fresh_chain, class_=TrackingSession, expire_on_commit=False)

    @event.listens_for(TrackingSession, "after_begin")
    def _tag_worker(session, _transaction, connection):
        connection.info["payment_callback_worker"] = session.info.get("payment_callback_worker")

    @event.listens_for(fresh_chain, "before_cursor_execute")
    def _observe_second_order_lock(connection, _cursor, statement, _parameters, _context, _executemany):
        normalized = statement.lower()
        if (connection.info.get("payment_callback_worker") == "second"
                and "billing_order" in normalized and "for update" in normalized):
            second_order_lock_sent.set()

    real_grant = checkout.ledger.grant

    def _hold_first_after_grant(db, **kwargs):
        batch = real_grant(db, **kwargs)
        if db.info.get("payment_callback_worker") == "first":
            first_has_grant.set()
            if not allow_first_to_commit.wait(timeout=10):
                raise TimeoutError("测试未释放首个支付回调事务")
        return batch

    monkeypatch.setattr(checkout.ledger, "grant", _hold_first_after_grant)

    def _apply_callback(name: str) -> None:
        try:
            with worker_sessions() as db:
                db.info["payment_callback_worker"] = name
                with db.begin():
                    if channel == "alipay":
                        applied = checkout.apply_alipay(
                            db, order_no=order_no, params={}, public_key_pem=b"test-only",
                            expected_appid="test-app", expected_seller="test-seller", now=paid_at,
                        )
                    else:
                        applied = checkout.apply_wechat(
                            db, order_no=order_no, headers={}, body=b"test-only", public_key_pem=b"test-only",
                            api_v3_key=b"test-only", expected_serial="test-serial",
                            expected_appid="test-app", expected_mchid="test-merchant", now=paid_at,
                        )
                    outcomes[name] = applied.status
        except Exception as exc:  # pragma: no cover - asserted by the test
            outcomes[f"{name}_error"] = exc
        finally:
            if name == "second":
                second_finished.set()

    first_thread = Thread(target=_apply_callback, args=("first",), daemon=True)
    second_thread = Thread(target=_apply_callback, args=("second",), daemon=True)
    first_started = False
    second_started = False
    try:
        first_thread.start()
        first_started = True
        assert first_has_grant.wait(timeout=10), "首个回调未完成积分写入"
        second_thread.start()
        second_started = True
        assert second_order_lock_sent.wait(timeout=10), "重复回调未竞争订单行锁"
        assert not second_finished.wait(timeout=0.25), "重复回调没有等待首个订单事务"
        allow_first_to_commit.set()
        first_thread.join(timeout=12)
        second_thread.join(timeout=12)
        assert not first_thread.is_alive() and not second_thread.is_alive(), "支付回调事务悬挂"
        assert outcomes == {"first": "paid", "second": "paid"}, outcomes
    finally:
        allow_first_to_commit.set()
        try:
            if first_started:
                first_thread.join(timeout=12)
            if second_started:
                second_thread.join(timeout=12)
        finally:
            event.remove(TrackingSession, "after_begin", _tag_worker)
            event.remove(fresh_chain, "before_cursor_execute", _observe_second_order_lock)

    with sessions() as db:
        order = db.query(BillingOrder).filter_by(order_no=order_no).one()
        events = db.query(BillingPaymentEvent).filter_by(order_no=order_no).all()
        batches = db.query(BillingCreditBatch).filter_by(grant_key=f"order:{order_no}").all()
        wallet = db.query(BillingWallet).filter_by(owner_type="user", owner_id=user_id).one()
        assert order.status == order.payment_status == "paid"
        assert order.amount_fen == frozen_amount_fen
        assert len(events) == len(batches) == 1
        assert events[0].amount_fen == frozen_amount_fen
        assert batches[0].issued_milli == 2_000_000
        assert available_milli(db, int(wallet.id), paid_at) == 2_000_000


def test_concurrent_checkout_request_id_returns_the_same_mysql_order(fresh_chain) -> None:
    """A simultaneous browser double-submit with one idempotency key must create one order."""
    from threading import Thread
    from uuid import uuid4

    from sqlalchemy import event
    from sqlalchemy.orm import Session as OrmSession
    from sqlalchemy.orm import sessionmaker

    from app.models.billing_ledger import BillingOrder
    from app.models.user import User
    from app.services import billing_checkout_service as checkout

    suffix = uuid4().hex[:12]
    sessions = sessionmaker(bind=fresh_chain, expire_on_commit=False)
    with sessions.begin() as db:
        buyer = User(username=f"double-checkout-{suffix}", password="x", role="user", status=1)
        db.add(buyer)
        db.flush()
        user_id = int(buyer.id)
    client_request_id = f"same-click-{suffix}"

    with fresh_chain.connect() as connection:
        isolation = connection.execute(text("SELECT @@transaction_isolation")).scalar_one()
    assert isolation.upper() == "REPEATABLE-READ"

    lookup_barrier = Barrier(2)
    synchronized_workers: set[str] = set()
    outcomes: dict[str, object] = {}

    class TrackingSession(OrmSession):
        pass

    worker_sessions = sessionmaker(bind=fresh_chain, class_=TrackingSession, expire_on_commit=False)

    @event.listens_for(TrackingSession, "after_begin")
    def _tag_worker(session, _transaction, connection):
        connection.info["checkout_worker"] = session.info.get("checkout_worker")

    @event.listens_for(fresh_chain, "before_cursor_execute")
    def _synchronize_absent_request_lookups(connection, _cursor, statement, _parameters, _context, _executemany):
        normalized = statement.lower()
        worker = connection.info.get("checkout_worker")
        if (worker and worker not in synchronized_workers and "billing_order" in normalized
                and "client_request_id" in normalized and "for update" in normalized):
            synchronized_workers.add(worker)
            lookup_barrier.wait(timeout=10)

    def _create_order(name: str) -> None:
        try:
            with worker_sessions() as db:
                db.info["checkout_worker"] = name
                with db.begin():
                    order = checkout.create_order(
                        db, user_id=user_id, product="personal_credits", quantity=1, channel="alipay",
                        client_request_id=client_request_id,
                    )
                    outcomes[name] = order.order_no
        except Exception as exc:  # pragma: no cover - asserted by the test
            outcomes[f"{name}_error"] = exc

    threads = [Thread(target=_create_order, args=(name,), daemon=True) for name in ("first", "second")]
    started: list[Thread] = []
    try:
        for thread in threads:
            thread.start()
            started.append(thread)
        for thread in started:
            thread.join(timeout=12)
        assert all(not thread.is_alive() for thread in started), "并发下单事务悬挂"
        assert set(outcomes) == {"first", "second"}, outcomes
        assert outcomes["first"] == outcomes["second"]
    finally:
        if lookup_barrier.broken:
            lookup_barrier.abort()
        for thread in started:
            thread.join(timeout=12)
        event.remove(TrackingSession, "after_begin", _tag_worker)
        event.remove(fresh_chain, "before_cursor_execute", _synchronize_absent_request_lookups)

    with sessions() as db:
        rows = db.query(BillingOrder).filter_by(user_id=user_id, client_request_id=client_request_id).all()
        assert len(rows) == 1
        assert rows[0].order_no == outcomes["first"]
        assert rows[0].request_fingerprint is not None


@pytest.mark.parametrize("winner", ["cancel", "callback"])
def test_simulated_payment_and_local_close_mysql_race_keeps_paid_entitlement(
    fresh_chain, monkeypatch, winner: str,
) -> None:
    """The test verifier's accepted notification wins regardless of local close ordering."""
    from datetime import datetime, timezone
    from threading import Event, Thread
    from uuid import uuid4

    from sqlalchemy import event
    from sqlalchemy.orm import Session as OrmSession
    from sqlalchemy.orm import sessionmaker

    from app.models.billing_ledger import BillingCreditBatch, BillingOrder, BillingPaymentEvent, BillingWallet
    from app.models.user import User
    from app.services import billing_checkout_service as checkout
    from app.services import billing_rate_catalog_service
    from app.services.billing_ledger_service import available_milli

    suffix = uuid4().hex[:12]
    paid_at = datetime.now(timezone.utc).replace(tzinfo=None)
    sessions = sessionmaker(bind=fresh_chain, expire_on_commit=False)
    with sessions.begin() as db:
        buyer = User(username=f"close-payment-race-{winner}-{suffix}", password="x", role="user", status=1)
        db.add(buyer)
        db.flush()
        order = checkout.create_order(
            db, user_id=int(buyer.id), product="personal_credits", quantity=1, channel="alipay",
        )
        assert order.amount_fen == billing_rate_catalog_service.active_catalog(db).credit_pack_price_fen
        frozen_amount_fen = int(order.amount_fen)
        user_id, order_no = int(buyer.id), str(order.order_no)

    trade_id = f"mysql-close-paid-{winner}-{suffix}"
    monkeypatch.setattr(checkout, "verify_alipay", lambda **_kwargs: {"trade_id": trade_id})
    winner_locked_order = Event()
    release_winner = Event()
    loser_lock_query_sent = Event()
    loser_finished = Event()
    outcomes: dict[str, object] = {}

    class TrackingSession(OrmSession):
        pass

    worker_sessions = sessionmaker(bind=fresh_chain, class_=TrackingSession, expire_on_commit=False)

    @event.listens_for(TrackingSession, "after_begin")
    def _tag_worker(session, _transaction, connection):
        connection.info["payment_close_race_role"] = session.info.get("payment_close_race_role")

    @event.listens_for(fresh_chain, "before_cursor_execute")
    def _observe_loser_order_lock(connection, _cursor, statement, _parameters, _context, _executemany):
        normalized = statement.lower()
        if (connection.info.get("payment_close_race_role") == "loser"
                and "billing_order" in normalized and "for update" in normalized):
            loser_lock_query_sent.set()

    if winner == "cancel":
        real_close = checkout.mark_order_closed

        def _hold_cancel_after_lock(db, **kwargs):
            closed = real_close(db, **kwargs)
            if db.info.get("payment_close_race_role") == "winner":
                winner_locked_order.set()
                if not release_winner.wait(timeout=10):
                    raise TimeoutError("测试未释放先行关单事务")
            return closed

        monkeypatch.setattr(checkout, "mark_order_closed", _hold_cancel_after_lock)
    else:
        real_grant = checkout.ledger.grant

        def _hold_payment_after_grant(db, **kwargs):
            batch = real_grant(db, **kwargs)
            if db.info.get("payment_close_race_role") == "winner":
                winner_locked_order.set()
                if not release_winner.wait(timeout=10):
                    raise TimeoutError("测试未释放先行支付事务")
            return batch

        monkeypatch.setattr(checkout.ledger, "grant", _hold_payment_after_grant)

    def _run(role: str, action: str) -> None:
        try:
            with worker_sessions() as db:
                db.info["payment_close_race_role"] = role
                with db.begin():
                    if action == "cancel":
                        result = checkout.mark_order_closed(db, order_no=order_no)
                    else:
                        result = checkout.apply_alipay(
                            db, order_no=order_no, params={}, public_key_pem=b"test-only",
                            expected_appid="test-app", expected_seller="test-seller", now=paid_at,
                        )
                    outcomes[f"{role}_{action}"] = result.status
        except Exception as exc:  # pragma: no cover - asserted by the test
            outcomes[f"{role}_{action}_error"] = exc
        finally:
            if role == "loser":
                loser_finished.set()

    winner_action, loser_action = (winner, "callback" if winner == "cancel" else "cancel")
    winner_thread = Thread(target=_run, args=("winner", winner_action), daemon=True)
    loser_thread = Thread(target=_run, args=("loser", loser_action), daemon=True)
    winner_started = False
    loser_started = False
    try:
        winner_thread.start()
        winner_started = True
        assert winner_locked_order.wait(timeout=10), "先行事务未完成订单状态写入"
        loser_thread.start()
        loser_started = True
        assert loser_lock_query_sent.wait(timeout=10), "后续事务未竞争订单行锁"
        assert not loser_finished.wait(timeout=0.25), "后续事务未等待先行订单事务提交"
        release_winner.set()
        winner_thread.join(timeout=12)
        loser_thread.join(timeout=12)
        assert not winner_thread.is_alive() and not loser_thread.is_alive(), "关单/支付竞态事务悬挂"
        assert set(outcomes) == {f"winner_{winner_action}", f"loser_{loser_action}"}, outcomes
        expected_winner_status = "paid" if winner == "callback" else "cancelled"
        assert outcomes[f"winner_{winner_action}"] == expected_winner_status, outcomes
        assert outcomes[f"loser_{loser_action}"] == "paid", outcomes
    finally:
        release_winner.set()
        try:
            if winner_started:
                winner_thread.join(timeout=12)
            if loser_started:
                loser_thread.join(timeout=12)
        finally:
            event.remove(TrackingSession, "after_begin", _tag_worker)
            event.remove(fresh_chain, "before_cursor_execute", _observe_loser_order_lock)

    with sessions() as db:
        order = db.query(BillingOrder).filter_by(order_no=order_no).one()
        events = db.query(BillingPaymentEvent).filter_by(order_no=order_no).all()
        batches = db.query(BillingCreditBatch).filter_by(grant_key=f"order:{order_no}").all()
        wallet = db.query(BillingWallet).filter_by(owner_type="user", owner_id=user_id).one()
        assert order.status == order.payment_status == "paid"
        assert order.amount_fen == frozen_amount_fen
        assert len(events) == len(batches) == 1
        assert events[0].amount_fen == frozen_amount_fen
        assert batches[0].issued_milli == 1_000_000
        assert available_milli(db, int(wallet.id), paid_at) == 1_000_000


def test_enterprise_group_authorization_sees_revocation_after_mysql_rr_snapshot(fresh_chain) -> None:
    """Group membership reads must not reuse the request's stale RR snapshot."""
    from uuid import uuid4

    from sqlalchemy.orm import sessionmaker

    from app.core.exceptions import ForbiddenError
    from app.models.enterprise_org import (
        Enterprise,
        EnterpriseGroup,
        EnterpriseGroupMember,
        EnterpriseMember,
    )
    from app.models.user import User
    from app.services import enterprise_org_service as org

    suffix = uuid4().hex[:12]
    sessions = sessionmaker(bind=fresh_chain, expire_on_commit=False)
    with sessions.begin() as db:
        owner = User(username=f"group-revoke-owner-{suffix}", password="x", role="user", status=1)
        member = User(username=f"group-revoke-member-{suffix}", password="x", role="user", status=1)
        db.add_all([owner, member])
        db.flush()
        company = Enterprise(
            name=f"小组撤权快照-{suffix}", owner_id=owner.id, seat_limit=3,
            status="active", paid_order_id=f"group-revoke-{suffix}",
        )
        db.add(company)
        db.flush()
        group = EnterpriseGroup(enterprise_id=company.id, name="研发", status="active")
        db.add_all([
            EnterpriseMember(enterprise_id=company.id, user_id=owner.id, role="owner", status="active"),
            EnterpriseMember(enterprise_id=company.id, user_id=member.id, role="member", status="active"),
            group,
        ])
        db.flush()
        db.add(EnterpriseGroupMember(
            enterprise_id=company.id, group_id=group.id, user_id=member.id,
            role="member", status="active",
        ))
        enterprise_id, group_id, owner_id, member_id = (
            int(company.id), int(group.id), int(owner.id), int(member.id),
        )

    with sessions() as reader:
        reader.connection().exec_driver_sql("SET SESSION TRANSACTION ISOLATION LEVEL REPEATABLE READ")
        assert reader.query(User.id).filter_by(id=member_id).scalar() == member_id
        with sessions.begin() as remover:
            org.remove_group_member(remover, enterprise_id, group_id, owner_id, member_id)
        with pytest.raises(ForbiddenError):
            org.require_group_member(reader, enterprise_id, group_id, member_id, allow_admin=False)


def test_enterprise_group_creation_and_assignment_share_mysql_transaction(fresh_chain) -> None:
    """Current-read revocation checks must retain same-transaction group setup."""
    from uuid import uuid4

    from sqlalchemy.orm import sessionmaker

    from app.models.enterprise_org import Enterprise, EnterpriseGroupMember, EnterpriseMember
    from app.models.user import User
    from app.services import enterprise_org_service as org

    suffix = uuid4().hex[:12]
    sessions = sessionmaker(bind=fresh_chain, expire_on_commit=False)
    with sessions.begin() as db:
        owner = User(username=f"group-setup-owner-{suffix}", password="x", role="user", status=1)
        member = User(username=f"group-setup-member-{suffix}", password="x", role="user", status=1)
        db.add_all([owner, member])
        db.flush()
        company = Enterprise(
            name=f"小组事务测试-{suffix}", owner_id=owner.id, seat_limit=3,
            status="active", paid_order_id=f"group-setup-{suffix}",
        )
        db.add(company)
        db.flush()
        db.add_all([
            EnterpriseMember(enterprise_id=company.id, user_id=owner.id, role="owner", status="active"),
            EnterpriseMember(enterprise_id=company.id, user_id=member.id, role="member", status="active"),
        ])
        db.flush()
        group = org.create_group(db, int(company.id), int(owner.id), "本事务创建")
        org.set_group_member(db, int(company.id), int(group.id), int(owner.id), int(member.id), "member")
        assignment = db.query(EnterpriseGroupMember).filter_by(
            enterprise_id=company.id, group_id=group.id, user_id=member.id,
        ).one()
        assert assignment.status == "active"


def test_enterprise_rejoin_does_not_restore_removed_mysql_group_grant(fresh_chain) -> None:
    """Rejoining an enterprise requires a new explicit grant to its old groups."""
    from uuid import uuid4

    from sqlalchemy.orm import sessionmaker

    from app.core.exceptions import ForbiddenError
    from app.models.enterprise_org import (
        Enterprise,
        EnterpriseGroup,
        EnterpriseGroupMember,
        EnterpriseMember,
    )
    from app.models.user import User
    from app.services import enterprise_org_service as org

    suffix = uuid4().hex[:12]
    sessions = sessionmaker(bind=fresh_chain, expire_on_commit=False)
    with sessions.begin() as db:
        owner = User(username=f"group-rejoin-owner-{suffix}", password="x", role="user", status=1)
        member = User(username=f"group-rejoin-member-{suffix}", password="x", role="user", status=1)
        db.add_all([owner, member])
        db.flush()
        company = Enterprise(
            name=f"小组重入测试-{suffix}", owner_id=owner.id, seat_limit=3,
            status="active", paid_order_id=f"group-rejoin-{suffix}",
        )
        db.add(company)
        db.flush()
        group = EnterpriseGroup(enterprise_id=company.id, name="曾属小组", status="active")
        db.add_all([
            EnterpriseMember(enterprise_id=company.id, user_id=owner.id, role="owner", status="active"),
            EnterpriseMember(enterprise_id=company.id, user_id=member.id, role="member", status="active"),
            group,
        ])
        db.flush()
        db.add(EnterpriseGroupMember(
            enterprise_id=company.id, group_id=group.id, user_id=member.id,
            role="member", status="active",
        ))
        invite = org.create_invitation(db, int(company.id), int(owner.id))
        enterprise_id, group_id, owner_id, member_id = (
            int(company.id), int(group.id), int(owner.id), int(member.id),
        )
    with sessions.begin() as db:
        org.remove_enterprise_member(db, enterprise_id, owner_id, member_id)
    with sessions.begin() as db:
        org.accept_invitation(db, invite, member_id)
        grant = db.query(EnterpriseGroupMember).filter_by(
            enterprise_id=enterprise_id, group_id=group_id, user_id=member_id,
        ).one()
        assert grant.status == "removed"
    with sessions() as db:
        with pytest.raises(ForbiddenError):
            org.require_group_member(db, enterprise_id, group_id, member_id, allow_admin=False)


def test_refund_request_and_provider_execution_use_consistent_mysql_lock_order(fresh_chain) -> None:
    """Idempotent refund requests must not deadlock with channel execution."""
    from datetime import datetime, timedelta, timezone
    from threading import Event, Thread
    from uuid import uuid4

    from sqlalchemy import event
    from sqlalchemy.orm import Session as OrmSession
    from sqlalchemy.orm import sessionmaker

    from app.models.billing_ledger import BillingOrder, BillingPaymentEvent, BillingRefund
    from app.models.user import User
    from app.services import billing_checkout_service as checkout
    from app.services import billing_ledger_service as ledger

    suffix = uuid4().hex[:12]
    sessions = sessionmaker(bind=fresh_chain, expire_on_commit=False)
    with sessions.begin() as db:
        buyer = User(username=f"refund-lock-buyer-{suffix}", password="x", role="user", status=1)
        db.add(buyer)
        db.flush()
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        order_no = f"refund-lock-order-{suffix}"
        order = BillingOrder(
            order_no=order_no, user_id=buyer.id, product="personal_credits", amount_fen=400,
            quantity=1, channel="alipay", status="paid", payment_status="paid", paid_at=now,
            created_at=now,
        )
        db.add(order)
        db.add(BillingPaymentEvent(
            channel="alipay", trade_id=f"refund-lock-trade-{suffix}", order_no=order_no,
            amount_fen=400, verified_at=now,
        ))
        db.flush()
        wallet = ledger.get_or_create_wallet(db, "user", int(buyer.id))
        ledger.grant(
            db, wallet_id=int(wallet.id), grant_key=f"order:{order_no}", kind="purchase",
            amount_milli=1_000_000,
        )
        buyer_id = int(buyer.id)

    with sessions.begin() as db:
        refund = checkout.request_refund(
            db, order_no=order_no, requester_id=buyer_id, reason="渠道锁序回归",
        )
        refund_no = str(refund.refund_no)

    first_order_locked = Event()
    release_first = Event()
    second_order_waiting = Event()
    outcomes: dict[str, object] = {}

    class TrackingSession(OrmSession):
        pass

    worker_sessions = sessionmaker(bind=fresh_chain, class_=TrackingSession, expire_on_commit=False)

    @event.listens_for(TrackingSession, "after_begin")
    def _tag_worker(session, _transaction, connection):
        connection.info["refund_lock_test_worker"] = session.info.get("refund_lock_test_worker")

    @event.listens_for(fresh_chain, "after_cursor_execute")
    def _hold_first_order_lock(connection, _cursor, statement, _parameters, _context, _executemany):
        if (connection.info.get("refund_lock_test_worker") == "request"
                and "billing_order" in statement.lower() and "for update" in statement.lower()):
            first_order_locked.set()
            if not release_first.wait(timeout=10):
                raise TimeoutError("测试未释放退款请求持有的订单锁")

    @event.listens_for(fresh_chain, "before_cursor_execute")
    def _observe_execution_order_wait(connection, _cursor, statement, _parameters, _context, _executemany):
        if (connection.info.get("refund_lock_test_worker") == "execution"
                and "billing_order" in statement.lower() and "for update" in statement.lower()):
            second_order_waiting.set()

    def _request_again() -> None:
        try:
            with worker_sessions() as db:
                db.info["refund_lock_test_worker"] = "request"
                with db.begin():
                    outcomes["request"] = checkout.request_refund(
                        db, order_no=order_no, requester_id=buyer_id, reason="渠道锁序回归",
                    )
        except Exception as exc:  # pragma: no cover - asserted by the test
            outcomes["request_error"] = exc

    def _execute() -> None:
        try:
            with worker_sessions() as db:
                db.info["refund_lock_test_worker"] = "execution"
                with db.begin():
                    outcomes["execution"] = checkout.prepare_refund(
                        db, refund_no=refund_no, admin_id=991,
                    )
        except Exception as exc:  # pragma: no cover - asserted by the test
            outcomes["execution_error"] = exc

    request_thread = Thread(target=_request_again, daemon=True)
    execution_thread = Thread(target=_execute, daemon=True)
    try:
        request_thread.start()
        assert first_order_locked.wait(timeout=10), "幂等退款请求未取得订单锁"
        execution_thread.start()
        assert second_order_waiting.wait(timeout=10), "渠道退款执行未发出订单锁查询"
        release_first.set()
        request_thread.join(timeout=12)
        execution_thread.join(timeout=12)
        assert not request_thread.is_alive() and not execution_thread.is_alive(), "退款事务发生锁等待超时"
        assert "request_error" not in outcomes, outcomes
        assert "execution_error" not in outcomes, outcomes
        assert outcomes["request"].id == outcomes["execution"][0].id
        with sessions() as db:
            persisted = db.query(BillingRefund).filter_by(refund_no=refund_no).one()
        assert persisted.status == "processing"

        claim_time = datetime.now(timezone.utc).replace(tzinfo=None)
        with sessions.begin() as db:
            current = db.query(BillingRefund).filter_by(refund_no=refund_no).one()
            current.updated_at = claim_time - timedelta(seconds=16)
            first_token = checkout.claim_refund_reconciliation(db, refund_no=refund_no, now=claim_time)
            assert first_token
        with sessions.begin() as db:
            checkout.update_refund_result(
                db, refund_no=refund_no, result={"state": "PROCESSING"}, now=claim_time + timedelta(seconds=1),
            )
            current = db.query(BillingRefund).filter_by(refund_no=refund_no).one()
            assert current.reconciliation_claim_token is None
            assert current.reconciliation_claimed_until is None
        with sessions() as db:
            with pytest.raises(ledger.BillingError, match="认领已过期"):
                with db.begin():
                    checkout.update_refund_result(
                        db, refund_no=refund_no, result={"state": "SUCCESS"},
                        verified_provider_query=True, reconciliation_token=first_token,
                        now=claim_time + timedelta(seconds=2),
                    )
    finally:
        release_first.set()
        event.remove(TrackingSession, "after_begin", _tag_worker)
        event.remove(fresh_chain, "after_cursor_execute", _hold_first_order_lock)
        event.remove(fresh_chain, "before_cursor_execute", _observe_execution_order_wait)


def test_refund_reconciliation_is_atomically_claimed_before_provider_query(fresh_chain, monkeypatch) -> None:
    """A slow provider query must remain exclusive after the cooldown elapses."""
    from datetime import datetime, timedelta, timezone
    from threading import Event, Lock, Thread
    from uuid import uuid4

    from fastapi import HTTPException
    from sqlalchemy.orm import sessionmaker

    from app.api.v1 import billing
    from app.models.billing_ledger import BillingOrder, BillingPaymentEvent, BillingRefund
    from app.models.user import User
    from app.services import billing_checkout_service as checkout
    from app.services import billing_ledger_service as ledger

    suffix = uuid4().hex[:12]
    sessions = sessionmaker(bind=fresh_chain, expire_on_commit=False)
    with sessions.begin() as db:
        buyer = User(username=f"refund-query-buyer-{suffix}", password="x", role="user", status=1)
        db.add(buyer)
        db.flush()
        admin_id = int(db.query(User).filter_by(username="admin", role="super_admin", status=1).one().id)
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        order_no = f"refund-query-order-{suffix}"
        order = BillingOrder(
            order_no=order_no, user_id=buyer.id, product="personal_credits", amount_fen=400,
            quantity=1, channel="alipay", status="paid", payment_status="paid", paid_at=now,
            created_at=now,
        )
        db.add(order)
        db.add(BillingPaymentEvent(
            channel="alipay", trade_id=f"refund-query-trade-{suffix}", order_no=order_no,
            amount_fen=400, verified_at=now,
        ))
        db.flush()
        wallet = ledger.get_or_create_wallet(db, "user", int(buyer.id))
        ledger.grant(
            db, wallet_id=int(wallet.id), grant_key=f"order:{order_no}", kind="purchase",
            amount_milli=1_000_000,
        )
        buyer_id = int(buyer.id)

    with sessions.begin() as db:
        refund = checkout.request_refund(
            db, order_no=order_no, requester_id=buyer_id, reason="并发渠道查单回归",
        )
        checkout.prepare_refund(db, refund_no=refund.refund_no, admin_id=admin_id)
        refund_no = str(refund.refund_no)
        refund.updated_at -= timedelta(seconds=16)

    monkeypatch.setattr(billing, "_gateway_configured", lambda _channel: True)
    provider_entered = Event()
    release_provider = Event()
    second_done = Event()
    call_lock = Lock()
    calls: list[str] = []
    outcomes: dict[str, object] = {}

    def fake_provider_query(_order, refund_row):
        with call_lock:
            calls.append(str(refund_row.refund_no))
        provider_entered.set()
        if not release_provider.wait(timeout=12):
            raise TimeoutError("测试未释放渠道查单")
        return {"state": "PROCESSING"}

    monkeypatch.setattr(billing, "_provider_refund_query", fake_provider_query)

    def reconcile(name: str) -> None:
        try:
            with sessions() as db:
                admin = db.query(User).filter_by(username="admin", role="super_admin", status=1).one()
                outcomes[name] = billing.reconcile_refund(refund_no, db=db, _admin=admin)
                db.commit()
        except Exception as exc:  # pragma: no cover - asserted by the test
            outcomes[f"{name}_error"] = exc
        finally:
            if name == "second":
                second_done.set()

    first = Thread(target=reconcile, args=("first",), daemon=True)
    second = Thread(target=reconcile, args=("second",), daemon=True)
    try:
        first.start()
        assert provider_entered.wait(timeout=10), "首个查单没有进入渠道查询"
        # Age only the cooldown timestamp while preserving the live claim lease.
        with sessions.begin() as db:
            persisted = db.query(BillingRefund).filter_by(refund_no=refund_no).one()
            persisted.updated_at -= timedelta(seconds=16)
        second.start()
        assert second_done.wait(timeout=3), "第二个查单未被原子认领阻止"
        assert isinstance(outcomes.get("second_error"), HTTPException), outcomes
        assert outcomes["second_error"].status_code == 409
        with call_lock:
            assert len(calls) == 1, calls
        release_provider.set()
        first.join(timeout=12)
        second.join(timeout=12)
        assert not first.is_alive() and not second.is_alive(), "退款查单线程未结束"
        assert "first_error" not in outcomes, outcomes
    finally:
        release_provider.set()
        if first.ident is not None:
            first.join(timeout=12)
        if second.ident is not None:
            second.join(timeout=12)


def test_expired_refund_reconciliation_lease_fences_old_provider_result(fresh_chain) -> None:
    """A late provider response cannot overwrite the result of a replacement claimant."""
    from datetime import datetime, timedelta, timezone
    from uuid import uuid4

    from sqlalchemy.orm import sessionmaker

    from app.models.billing_ledger import BillingOrder, BillingPaymentEvent, BillingRefund
    from app.models.user import User
    from app.services import billing_checkout_service as checkout
    from app.services import billing_ledger_service as ledger

    suffix = uuid4().hex[:12]
    sessions = sessionmaker(bind=fresh_chain, expire_on_commit=False)
    with sessions.begin() as db:
        buyer = User(username=f"refund-lease-buyer-{suffix}", password="x", role="user", status=1)
        db.add(buyer)
        db.flush()
        admin_id = int(db.query(User).filter_by(username="admin", role="super_admin", status=1).one().id)
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        order_no = f"refund-lease-order-{suffix}"
        order = BillingOrder(
            order_no=order_no, user_id=buyer.id, product="personal_credits", amount_fen=400,
            quantity=1, channel="alipay", status="paid", payment_status="paid", paid_at=now,
            created_at=now,
        )
        db.add(order)
        db.add(BillingPaymentEvent(
            channel="alipay", trade_id=f"refund-lease-trade-{suffix}", order_no=order_no,
            amount_fen=400, verified_at=now,
        ))
        db.flush()
        wallet = ledger.get_or_create_wallet(db, "user", int(buyer.id))
        ledger.grant(db, wallet_id=int(wallet.id), grant_key=f"order:{order_no}",
                     kind="purchase", amount_milli=1_000_000)
        buyer_id = int(buyer.id)

    with sessions.begin() as db:
        refund = checkout.request_refund(
            db, order_no=order_no, requester_id=buyer_id, reason="查单租约 fencing 回归",
        )
        checkout.prepare_refund(db, refund_no=refund.refund_no, admin_id=admin_id)
        refund_no = str(refund.refund_no)
        refund.updated_at -= timedelta(seconds=16)

    claim_time = datetime.now(timezone.utc).replace(tzinfo=None)
    with sessions.begin() as db:
        first_token = checkout.claim_refund_reconciliation(db, refund_no=refund_no, now=claim_time)
        assert first_token
    with sessions.begin() as db:
        assert checkout.claim_refund_reconciliation(
            db, refund_no=refund_no, now=claim_time + timedelta(seconds=16),
        ) is None, "冷却窗口结束前不能抢占仍有效的查单租约"
    with sessions() as db:
        with pytest.raises(ledger.BillingError, match="认领已过期"):
            with db.begin():
                checkout.update_refund_result(
                    db, refund_no=refund_no, result={"state": "FAILED"},
                    verified_provider_query=True, reconciliation_token=first_token,
                    now=claim_time + timedelta(seconds=61),
                )
    with sessions.begin() as db:
        replacement_token = checkout.claim_refund_reconciliation(
            db, refund_no=refund_no, now=claim_time + timedelta(seconds=61),
        )
        assert replacement_token and replacement_token != first_token

    with sessions() as db:
        with pytest.raises(ledger.BillingError, match="认领已过期"):
            with db.begin():
                checkout.update_refund_result(
                    db, refund_no=refund_no, result={"state": "FAILED"},
                    verified_provider_query=True, reconciliation_token=first_token,
                    now=claim_time + timedelta(seconds=62),
                )

    with sessions.begin() as db:
        completed = checkout.update_refund_result(
            db, refund_no=refund_no, result={"state": "SUCCESS"},
            verified_provider_query=True, reconciliation_token=replacement_token,
            now=claim_time + timedelta(seconds=63),
        )
        assert completed.status == "succeeded"
        assert completed.reconciliation_claim_token is None
        assert completed.reconciliation_claimed_until is None
    with sessions() as db:
        persisted = db.query(BillingRefund).filter_by(refund_no=refund_no).one()
        assert persisted.provider_status == "SUCCESS" and persisted.status == "succeeded"


@pytest.mark.parametrize("winner,contender", [
    ("recover", "finalize"), ("finalize", "recover"), ("recover", "replay"),
    ("finalize", "replay"), ("replay", "finalize"),
])
def test_mysql_stale_tool_recovery_serializes_with_worker(fresh_chain, monkeypatch, winner, contender):
    """Real InnoDB locks serialize recovery against a late response or replay."""
    import asyncio
    import time
    from threading import Event, get_ident

    from sqlalchemy import event

    from app.models.billing_ledger import (
        BillingCreditBatch,
        BillingReservation,
        BillingReservationPart,
        BillingToolExecutionResult,
    )
    from app.models.enterprise_assistant_runtime import EnterpriseAssistantRuntimeState
    from app.models.enterprise_chat import EnterpriseAssistantRun
    from app.services import billing_reservation_reconciliation_service as reconciliation
    from app.services import billing_search_fetch_service as tool_service
    from app.services import enterprise_assistant_runtime_service as runtime
    from app.services.billing_ledger_service import get_or_create_wallet, grant

    # Avoid reusing the previous parameter's observer session and its cached
    # information_schema transaction snapshot.
    fresh_chain.dispose()
    sessions = sessionmaker(bind=fresh_chain, autoflush=False, expire_on_commit=False)
    suffix = secrets.token_hex(8)
    public_id = f"mysql-lock-{suffix}"
    request_key = f"enterprise-assistant:{public_id}:tool:search:{'a' * 32}"
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    org = int(secrets.randbelow(10**8)) + 500_000_000
    with sessions.begin() as db:
        run = EnterpriseAssistantRun(
            public_id=public_id, request_key=f"assistant:{public_id}", enterprise_id=org, conversation_id=org,
            initiator_user_id=org, source_message_id=org, status="running", attempt_count=1,
            model_name="deepseek-flash", prompt_sha256="b" * 64,
        )
        db.add(run)
        db.flush()
        db.add(EnterpriseAssistantRuntimeState(
            assistant_run_id=run.id, enterprise_id=org, conversation_id=org, initiator_user_id=org,
            source_message_id=org, status="running", checkpoint_json="{}", permission_snapshot_json="{}",
            permission_snapshot_sha256="c" * 64, lease_generation=1,
            lease_expires_at=now - timedelta(minutes=1), billing_invocation_count=1, version=1,
        ))
        wallet = get_or_create_wallet(db, "enterprise", org)
        batch = grant(db, wallet_id=wallet.id, grant_key=f"grant:{public_id}", kind="purchase", amount_milli=10_000)
        batch.reserved_milli = 1_800
        row = BillingReservation(
            request_key=request_key, model_name="deepseek-flash", billable_kind="search", tool_name="search",
            tool_cost_cny_micros_per_success=5_038, tool_price_milli_per_success=1_800,
            provider_attempt_started_at=now - timedelta(minutes=31), provider_identity_version="deepseek-official-v1",
            provider_name="deepseek", provider_endpoint_host="api.deepseek.com", credential_source="system",
            pricing_version="catalog-v1", enterprise_id=org, user_id=org, usage_kind="member",
            enterprise_wallet_id=wallet.id, requested_milli=1_800, usage_state="pending", status="reserved",
        )
        db.add(row)
        db.flush()
        db.add(BillingReservationPart(reservation_id=row.id, wallet_id=wallet.id, batch_id=batch.id,
                                      reserved_milli=1_800, charged_milli=None))
        reservation_id, wallet_id, batch_id = row.id, wallet.id, batch.id

    locked, release, contender_started, lock_statement_started = Event(), Event(), Event(), Event()
    contender_thread = []
    contender_connection = []
    contender_lock_sql = []
    original_recovery_lock = reconciliation._reservation_for_update

    def recovery_lock(db, reservation_id, *, wallet_first):
        if winner == "recover":
            locked.set()
            assert release.wait(30), "恢复事务未释放"
        return original_recovery_lock(db, reservation_id, wallet_first=wallet_first)

    monkeypatch.setattr(reconciliation, "_reservation_for_update", recovery_lock)

    def authorize(db):
        if contender == "replay" and get_ident() in contender_thread:
            # Establish the repeatable-read snapshot before waiting for the
            # finalizer's state lock, just as pricing/config reads do in runtime.
            db.query(BillingReservation.id).filter_by(id=reservation_id).one()
        state = runtime._state_for_run(db, enterprise_id=org, conversation_id=org,
                                       run_public_id=public_id, lock=True)
        if state is None or state.lease_generation != 1 or state.status != "running":
            raise PermissionError("执行代次已失效")
        if winner in {"finalize", "replay"} and get_ident() not in contender_thread:
            locked.set()
            assert release.wait(30), "结算事务未释放"

    def recover():
        with sessions() as db:
            try:
                reconciliation.recover_stale_tool_attempt(
                    db, reservation_id=reservation_id, actor_id=org, actor_name="admin", note="竞争测试", now=now,
                )
                db.commit()
                return "recovered"
            except reconciliation.ReconciliationBlocked:
                db.commit()
                return "blocked"

    def finalize():
        try:
            tool_service._finalize_result(
                sessions, request_key, tool_service.SearchFetchResult(
                    tool_name="search", status="success", billable=True, payload={"results": [{"title": "ok"}]},
                ), tool_service._input_sha256("search", tool_service.validate_arguments("search", {"query": "test"})),
                authorize,
            )
            return "settled"
        except tool_service.SearchFetchOutcomeUnknown:
            return "unknown"

    def replay():
        async def invoke():
            return await tool_service.execute_tool(
                session_factory=sessions, request_key=request_key, tool_name="search", arguments={"query": "test"},
                user_id=org, enterprise_id=org, enterprise_wallet_id=wallet_id, group_id=None,
                provider_name="deepseek", base_url="https://api.deepseek.com", credential_source="system",
                api_key="sk-test", authorize=authorize,
            )
        try:
            result = asyncio.run(invoke())
        except (PermissionError, tool_service.SearchFetchOutcomeUnknown):
            return "blocked"
        assert result.status == "success" and result.billable
        return "replayed"

    def forbid_network(*_args, **_kwargs):
        pytest.fail("重放和恢复竞争不得发送真实供应商请求")

    monkeypatch.setattr(tool_service.httpx, "AsyncClient", forbid_network)
    functions = {"recover": recover, "finalize": finalize, "replay": replay}

    def start_contender():
        contender_thread.append(get_ident())
        contender_started.set()
        return functions[contender]()

    def observe_lock(conn, _cursor, statement, _parameters, _context, _many):
        if contender_thread and get_ident() == contender_thread[0] and "FOR UPDATE" in statement.upper():
            if not contender_connection:
                contender_connection.append(conn.connection.driver_connection.thread_id())
                contender_lock_sql.append(statement)
                lock_statement_started.set()

    event.listen(fresh_chain, "before_cursor_execute", observe_lock)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(functions[winner])
            try:
                assert locked.wait(10), "首个事务没有取得运行锁"
                second = pool.submit(start_contender)
                assert contender_started.wait(10)
                assert lock_statement_started.wait(10), "竞争事务没有发起锁定读取"
                deadline = time.monotonic() + 12
                waiting = False
                while time.monotonic() < deadline:
                    with fresh_chain.connect() as observer:
                        waiting = bool(observer.execute(
                            text("SELECT COUNT(*) FROM performance_schema.data_lock_waits w "
                                 "JOIN performance_schema.threads t ON t.thread_id = w.requesting_thread_id "
                                 "WHERE t.processlist_id = :connection_id"),
                            {"connection_id": contender_connection[0]},
                        ).scalar_one())
                    if waiting:
                        break
                    time.sleep(0.02)
                assert waiting, (f"MySQL 未观察到竞争事务等待运行锁: {contender_connection}, "
                                 f"{contender_lock_sql}, {second.result() if second.done() else None}")
            finally:
                release.set()
            outcomes = (first.result(timeout=20), second.result(timeout=20))
    finally:
        event.remove(fresh_chain, "before_cursor_execute", observe_lock)

    if winner == "recover":
        assert outcomes == ("recovered", "unknown" if contender == "finalize" else "blocked")
    elif winner == "finalize":
        assert outcomes == ("settled", "blocked" if contender == "recover" else "replayed")
    else:
        assert outcomes == ("blocked", "unknown")
    with sessions() as db:
        row = db.get(BillingReservation, reservation_id)
        state = db.query(EnterpriseAssistantRuntimeState).filter_by(enterprise_id=org).one()
        batch = db.get(BillingCreditBatch, batch_id)
        results = db.query(BillingToolExecutionResult).filter_by(request_key=request_key).count()
        if winner in {"recover", "replay"}:
            generation = 2 if winner == "recover" else 1
            assert (row.status, row.actual_milli, state.lease_generation, results) == ("unknown", None, generation, 0)
            assert (batch.remaining_milli, batch.reserved_milli) == (10_000, 1_800)
        else:
            assert (row.status, row.actual_milli, state.lease_generation, results) == ("settled", 1_800, 1, 1)
            assert (batch.remaining_milli, batch.reserved_milli) == (8_200, 0)


@pytest.mark.parametrize("same_key", [False, True])
def test_mysql_tool_reservation_missing_keys_do_not_gap_deadlock(fresh_chain, monkeypatch, same_key):
    """Synchronize both missing-key reads in one unique-index gap before inserting."""
    from sqlalchemy import event

    from app.models.billing_ledger import BillingCreditBatch, BillingReservation, BillingReservationPart
    from app.services import billing_ledger_service as ledger
    from app.services import billing_rate_catalog_service as catalog

    sessions = sessionmaker(bind=fresh_chain, autoflush=False, expire_on_commit=False)
    prefix = f"zz-mysql-tool-gap-{secrets.token_hex(12)}-"
    keys = (prefix + "a", prefix + ("a" if same_key else "b"))
    orgs = (secrets.randbelow(10**8) + 700_000_000, secrets.randbelow(10**8) + 800_000_000)
    with sessions.begin() as db:
        assert db.query(BillingReservation.id).filter(
            BillingReservation.request_key >= prefix,
            BillingReservation.request_key < prefix + "z",
        ).count() == 0
        wallet_ids, batch_ids = [], []
        for org in orgs:
            wallet = ledger.get_or_create_wallet(db, "enterprise", org)
            batch = ledger.grant(db, wallet_id=wallet.id, grant_key=f"grant:{prefix}:{org}",
                                 kind="purchase", amount_milli=10_000)
            wallet_ids.append(wallet.id)
            batch_ids.append(batch.id)

    monkeypatch.setattr(catalog, "tool_rate_snapshot", lambda _db, _name, **_kw: ("test-catalog", 5_038, 1_800))
    barrier = Barrier(2)
    seen = []

    def after_select(_conn, _cursor, statement, parameters, _context, _executemany):
        sql = " ".join(statement.lower().split())
        if sql.startswith("select billing_reservation.id") and "billing_reservation.request_key =" in sql:
            key = parameters.get("request_key_1") if isinstance(parameters, dict) else None
            if key in keys:
                seen.append(key)
                barrier.wait(timeout=10)

    provider = ledger.BillingProviderIdentity(
        identity_version="deepseek-official-v1", provider_name="deepseek",
        endpoint_host="api.deepseek.com", credential_source="system",
    )

    def reserve(index):
        with sessions() as db:
            try:
                ledger.reserve_tool(
                    db, request_key=keys[index], tool_name="search", user_id=orgs[index],
                    enterprise_id=orgs[index], enterprise_wallet_id=wallet_ids[index], group_id=None,
                    provider_identity=provider,
                )
                db.commit()
                return "reserved"
            except ledger.IdempotencyConflict:
                db.rollback()
                return "conflict"

    event.listen(fresh_chain, "after_cursor_execute", after_select)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(reserve, i) for i in range(2)]
            outcomes = [future.result(timeout=20) for future in futures]
    finally:
        event.remove(fresh_chain, "after_cursor_execute", after_select)

    assert sorted(seen) == sorted(keys)
    assert sorted(outcomes) == (["conflict", "reserved"] if same_key else ["reserved", "reserved"])
    with sessions() as db:
        rows = db.query(BillingReservation).filter(BillingReservation.request_key.in_(keys)).all()
        assert len(rows) == (1 if same_key else 2)
        parts = db.query(BillingReservationPart).filter(
            BillingReservationPart.reservation_id.in_([row.id for row in rows]),
        ).all()
        assert len(parts) == len(rows)
        assert all(part.reserved_milli == 1_800 for part in parts)
        batches = [db.get(BillingCreditBatch, batch_id) for batch_id in batch_ids]
        assert sorted(batch.reserved_milli for batch in batches) == ([0, 1_800] if same_key else [1_800, 1_800])


def test_mysql_tool_unsent_replay_and_c4_decision_lock_order(fresh_chain, monkeypatch):
    """An unsent replay and an invalid C4 decision serialize without a lock cycle."""
    import asyncio
    from threading import Event, current_thread

    from sqlalchemy import event

    from app.models.billing_ledger import BillingReservation, BillingReservationPart
    from app.models.enterprise_assistant_runtime import EnterpriseAssistantRuntimeState
    from app.models.enterprise_chat import EnterpriseAssistantRun
    from app.services import billing_ledger_service as ledger
    from app.services import billing_reservation_reconciliation_service as reconciliation
    from app.services import billing_search_fetch_service as tool_service
    from app.services import enterprise_assistant_runtime_service as runtime

    sessions = sessionmaker(bind=fresh_chain, autoflush=False, expire_on_commit=False)
    suffix = secrets.token_hex(8)
    public_id = f"mysql-unsent-{suffix}"
    request_key = f"enterprise-assistant:{public_id}:tool:search:{'a' * 32}"
    org = secrets.randbelow(10**8) + 900_000_000
    with sessions.begin() as db:
        run = EnterpriseAssistantRun(
            public_id=public_id, request_key=f"assistant:{public_id}", enterprise_id=org, conversation_id=org,
            initiator_user_id=org, source_message_id=org, status="running", prompt_sha256="b" * 64,
        )
        db.add(run)
        db.flush()
        db.add(EnterpriseAssistantRuntimeState(
            assistant_run_id=run.id, enterprise_id=org, conversation_id=org, initiator_user_id=org,
            source_message_id=org, status="running", checkpoint_json="{}", permission_snapshot_json="{}",
            permission_snapshot_sha256="c" * 64, lease_generation=1,
        ))
        wallet = ledger.get_or_create_wallet(db, "enterprise", org)
        batch = ledger.grant(db, wallet_id=wallet.id, grant_key=f"grant:{public_id}",
                             kind="purchase", amount_milli=10_000)
        batch.reserved_milli = 1_800
        row = BillingReservation(
            request_key=request_key, model_name="deepseek-search", billable_kind="search", tool_name="search",
            tool_cost_cny_micros_per_success=5_038, tool_price_milli_per_success=1_800,
            provider_identity_version="deepseek-official-v1", provider_name="deepseek",
            provider_endpoint_host="api.deepseek.com", credential_source="system", pricing_version="catalog-v1",
            enterprise_id=org, user_id=org, enterprise_wallet_id=wallet.id, requested_milli=1_800,
            usage_kind="member", usage_state="pending", status="reserved",
        )
        db.add(row)
        db.flush()
        db.add(BillingReservationPart(reservation_id=row.id, wallet_id=wallet.id, batch_id=batch.id,
                                      reserved_milli=1_800, charged_milli=None))
        reservation_id, wallet_id, batch_id = row.id, wallet.id, batch.id

    replay_wallet_start, c4_reservation_start = Event(), Event()
    original_lock_wallets = ledger._lock_wallets

    def coordinated_wallet_lock(db, ids):
        if current_thread().name.startswith("unsent-replay") and not replay_wallet_start.is_set():
            replay_wallet_start.set()
            assert c4_reservation_start.wait(15), "C4 未到达 reservation 锁"
        return original_lock_wallets(db, ids)

    def observe_c4(_conn, _cursor, statement, _parameters, _context, _many):
        if (current_thread().name.startswith("c4-decision") and "FOR UPDATE" in statement.upper()
                and "FROM billing_reservation " in " ".join(statement.split())):
            c4_reservation_start.set()

    monkeypatch.setattr(ledger, "_lock_wallets", coordinated_wallet_lock)
    event.listen(fresh_chain, "before_cursor_execute", observe_c4)

    def forbid_network(*_args, **_kwargs):
        pytest.fail("未发送重放不得发起网络请求")

    monkeypatch.setattr(tool_service.httpx, "AsyncClient", forbid_network)

    def authorize(db):
        runtime._state_for_run(db, enterprise_id=org, conversation_id=org, run_public_id=public_id, lock=True)

    def replay():
        async def invoke():
            await tool_service.execute_tool(
                session_factory=sessions, request_key=request_key, tool_name="search", arguments={"query": "test"},
                user_id=org, enterprise_id=org, enterprise_wallet_id=wallet_id, group_id=None,
                provider_name="deepseek", base_url="https://api.deepseek.com", credential_source="system",
                api_key="sk-test", authorize=authorize,
            )
        try:
            asyncio.run(invoke())
        except tool_service.SearchFetchReplayBlocked:
            return "released"
        pytest.fail("未发送重放不得发起网络请求")

    def decide():
        with sessions() as db:
            try:
                reconciliation.decide_reconciliation(
                    db, reservation_id=reservation_id, actor_id=org, decision="approve", note="锁序测试",
                )
            except reconciliation.ReconciliationBlocked:
                db.commit()
                return "blocked"
        pytest.fail("无提案的 C4 决定不得批准")

    try:
        with ThreadPoolExecutor(max_workers=1, thread_name_prefix="unsent-replay") as replay_pool, \
             ThreadPoolExecutor(max_workers=1, thread_name_prefix="c4-decision") as c4_pool:
            first = replay_pool.submit(replay)
            assert replay_wallet_start.wait(15), "重放未到达钱包锁"
            second = c4_pool.submit(decide)
            assert (first.result(timeout=20), second.result(timeout=20)) == ("released", "blocked")
    finally:
        event.remove(fresh_chain, "before_cursor_execute", observe_c4)
    with sessions() as db:
        assert db.get(BillingReservation, reservation_id).status == "released"
        assert db.get(ledger.BillingCreditBatch, batch_id).reserved_milli == 0


def test_mysql_personal_agent_snapshot_races_and_isolation(fresh_chain):
    """Owner lock, idempotency and version checks hold under MySQL RR snapshots."""
    from app.core.exceptions import ConflictError, NotFoundError
    from app.models.personal_agent import PersonalAgent, PersonalAgentVersion
    from app.models.user import User
    from app.services import personal_agent_service as service

    sessions = sessionmaker(bind=fresh_chain, autoflush=False, expire_on_commit=False)
    suffix = secrets.token_hex(8)
    with sessions.begin() as db:
        owner = User(username=f"mysql-pa-owner-{suffix}", password="x", role="user", status=1)
        other = User(username=f"mysql-pa-other-{suffix}", password="x", role="user", status=1)
        db.add_all([owner, other])
        db.flush()
        owner_id, other_id = owner.id, other.id

    def race(operation):
        start = Barrier(2)

        def call(index):
            with sessions() as db:
                # Authentication typically reads before the service takes its
                # owner lock. Both requests must have a snapshot of the past.
                assert db.query(User.id).filter_by(id=owner_id).one()
                start.wait(timeout=10)
                try:
                    row = operation(db, index)
                    value = ("ok", row.id, row.current_version)
                    db.commit()
                    return value
                except ConflictError:
                    db.rollback()
                    return ("conflict", None, None)

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(call, i) for i in range(2)]
            return [future.result(timeout=20) for future in futures]

    created = race(lambda db, _i: service.create(db, owner_id, "私人助手", "处理资料", "create-1"))
    assert [item[0] for item in created] == ["ok", "ok"]
    assert created[0][1] == created[1][1] and created[0][2] == created[1][2] == 1
    agent_id = created[0][1]
    edited = race(lambda db, _i: service.revise(db, owner_id, agent_id, "新版", "检查资料", 1, "edit-same"))
    assert [item[0] for item in edited] == ["ok", "ok"]
    assert all(item[1:] == (agent_id, 2) for item in edited)
    competing = race(lambda db, i: service.revise(
        db, owner_id, agent_id, f"竞争版本{i}", "检查资料", 2, f"edit-other-{i}",
    ))
    assert sorted(item[0] for item in competing) == ["conflict", "ok"]
    with sessions() as db:
        assert db.get(PersonalAgent, agent_id).current_version == 3
        versions = db.query(PersonalAgentVersion).filter_by(agent_id=agent_id).order_by(
            PersonalAgentVersion.version_number,
        ).all()
        assert [version.version_number for version in versions] == [1, 2, 3]
        assert len({version.request_key for version in versions}) == 3
        assert service.list_agents(db, other_id, offset=0, limit=20) == []
        for operation in (
            lambda: service.get(db, other_id, agent_id),
            lambda: service.versions(db, other_id, agent_id, offset=0, limit=20),
            lambda: service.revise(db, other_id, agent_id, "越权", "越权", 3, "unauthorized"),
            lambda: service.disable(db, other_id, agent_id, 3),
        ):
            with pytest.raises(NotFoundError):
                operation()
            db.rollback()
        assert db.get(PersonalAgent, agent_id).status == "active"
        assert db.query(PersonalAgentVersion).filter_by(agent_id=agent_id).count() == 3


def test_mysql_personal_agent_max_multibyte_prompt_roundtrip(fresh_chain):
    from app.models.user import User
    from app.services import personal_agent_service as service

    sessions = sessionmaker(bind=fresh_chain, autoflush=False, expire_on_commit=False)
    with sessions.begin() as db:
        user = User(username=f"mysql-pa-utf8-{secrets.token_hex(6)}", password="x", role="user", status=1)
        db.add(user)
        db.flush()
        user_id = user.id
    prompt = "文" * 15_000 + "😀" * 15_000
    assert len(prompt) == 30_000 and len(prompt.encode("utf-8")) > 65_535
    with sessions() as db:
        row = service.create(db, user_id, "多语言助手", prompt, "utf8-create")
        agent_id = row.id
    with sessions() as db:
        row = service.get(db, user_id, agent_id)
        assert service.current_version(db, row).prompt == prompt
        service.revise(db, user_id, agent_id, "多语言助手", prompt[::-1], 1, "utf8-revise")
    with sessions() as db:
        row = service.get(db, user_id, agent_id)
        assert service.current_version(db, row).prompt == prompt[::-1]


def test_mysql_model_cny_migration_keeps_old_evidence_null_and_immutable(fresh_chain):
    from sqlalchemy.exc import DBAPIError

    from app.models.billing_ledger import BillingRateCatalog, BillingRateCatalogItem
    from app.services import billing_rate_catalog_service as catalog_service

    sessions = sessionmaker(bind=fresh_chain, autoflush=False, expire_on_commit=False)
    with sessions() as db:
        legacy = db.query(BillingRateCatalog).order_by(BillingRateCatalog.id).first()
        assert legacy is not None
        legacy_id = legacy.id
        item = db.query(BillingRateCatalogItem).filter_by(catalog_id=legacy_id).first()
        assert item is not None
        item_id = item.id
        assert legacy.model_cny_source_url is None and legacy.model_cny_source_checked_at is None
        assert item.provider_input_cny_micros_per_million is None
        assert item.provider_output_cny_micros_per_million is None
        with pytest.raises(catalog_service.RateCatalogError, match="来源未完整核验"):
            catalog_service._require_current_model_facts(db, legacy, at=datetime.now(timezone.utc).replace(tzinfo=None))
    with fresh_chain.connect() as connection:
        tx = connection.begin()
        with pytest.raises(DBAPIError):
            connection.execute(text(
                "UPDATE billing_rate_catalog SET model_cny_source_url = :url, "
                "model_cny_source_checked_at = NOW() WHERE id = :id"
            ), {"url": "https://api-docs.deepseek.com/zh-cn/quick_start/pricing/", "id": legacy_id})
        tx.rollback()
    with fresh_chain.connect() as connection:
        tx = connection.begin()
        with pytest.raises(DBAPIError):
            connection.execute(text(
                "UPDATE billing_rate_catalog_item SET provider_input_cny_micros_per_million = 2000000, "
                "provider_output_cny_micros_per_million = 8000000 WHERE id = :id"
            ), {"id": item_id})
        tx.rollback()


@pytest.mark.parametrize("winner", ["review", "revoke"])
def test_mysql_rate_review_authorization_and_revocation_serialize(fresh_chain, winner):
    """A committed revocation must fence a waiting reviewer, and vice versa."""
    import time
    from threading import Event, get_ident

    from sqlalchemy import event

    from app.core.exceptions import ForbiddenError
    from app.models.billing_ledger import BillingRateCatalog, BillingRateReviewerGrant
    from app.models.rbac import Role, UserRole
    from app.models.user import User
    from app.services import billing_rate_catalog_service as rates
    from app.services import billing_rate_reviewer_service as review

    sessions = sessionmaker(bind=fresh_chain, autoflush=False, expire_on_commit=False)
    now = datetime.now(timezone.utc)
    source_time = now - timedelta(minutes=1 if winner == "review" else 2)
    with sessions.begin() as db:
        admin_role = db.query(Role).filter_by(code="admin", status="active").one()
        user = User(username=f"rate-review-{secrets.token_hex(6)}", password="x", role="admin", status=1)
        db.add(user)
        db.flush()
        db.add(UserRole(user_id=user.id, role_id=admin_role.id))
        db.flush()
        review.set_reviewer(db, actor_id=900, user_id=user.id, active=True, reason="授权测试")
        reviewer_id = user.id
        draft = rates.create_draft(
            db, actor_id=900, effective_at=now + timedelta(hours=3, minutes=0 if winner == "review" else 1),
            source_url="https://api-docs.deepseek.com/quick_start/pricing/", source_checked_at=source_time,
            model_cny_source_url="https://api-docs.deepseek.com/zh-cn/quick_start/pricing/",
            model_cny_source_checked_at=source_time, fx_source_url="https://www.boc.cn/sourcedb/whpj/index.html",
            fx_source_checked_at=source_time, fx_cny_per_usd_scaled=67_168, change_note="锁序验证",
            items=rates.baseline_items(), now=now,
        )
        catalog_id = draft.id

    locked, release, executing = Event(), Event(), Event()
    contender_thread, contender_connection = [], []

    def reviewer():
        with sessions() as db:
            try:
                review.require_reviewer(db, reviewer_id, 0)
                if winner == "review":
                    locked.set()
                    assert release.wait(15)
                rates.approve_draft(db, catalog_id=catalog_id, actor_id=reviewer_id)
                db.commit()
                return "approved"
            except ForbiddenError:
                db.rollback()
                return "denied"

    def revoke():
        with sessions.begin() as db:
            review.set_reviewer(db, actor_id=900, user_id=reviewer_id, active=False, reason="撤销测试")
            if winner == "revoke":
                locked.set()
                assert release.wait(15)
        return "revoked"

    operations = {"review": reviewer, "revoke": revoke}
    contender = "revoke" if winner == "review" else "review"

    def run_contender():
        contender_thread.append(get_ident())
        return operations[contender]()

    def observe(conn, _cursor, statement, _parameters, _context, _many):
        if contender_thread and get_ident() == contender_thread[0] and "FOR UPDATE" in statement.upper():
            if not contender_connection:
                contender_connection.append(conn.connection.driver_connection.thread_id())
                executing.set()

    event.listen(fresh_chain, "before_cursor_execute", observe)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(operations[winner])
            try:
                assert locked.wait(10)
                second = pool.submit(run_contender)
                assert executing.wait(10)
                deadline = time.monotonic() + 8
                waiting = False
                while time.monotonic() < deadline:
                    with fresh_chain.connect() as observer:
                        waiting = bool(observer.execute(text(
                            "SELECT COUNT(*) FROM performance_schema.data_lock_waits w "
                            "JOIN performance_schema.threads t ON t.thread_id=w.requesting_thread_id "
                            "WHERE t.processlist_id=:connection_id"
                        ), {"connection_id": contender_connection[0]}).scalar_one())
                    if waiting:
                        break
                    time.sleep(0.02)
                assert waiting, "MySQL 未观察到授权撤销竞争等待"
            finally:
                release.set()
            outcomes = first.result(timeout=20), second.result(timeout=20)
    finally:
        event.remove(fresh_chain, "before_cursor_execute", observe)
    assert outcomes == (("approved", "revoked") if winner == "review" else ("revoked", "denied"))
    with sessions() as db:
        assert db.get(BillingRateCatalog, catalog_id).status == ("approved" if winner == "review" else "draft")
        assert db.query(BillingRateReviewerGrant).filter_by(user_id=reviewer_id).one().active == 0


@pytest.mark.parametrize("winner", ["review", "revoke"])
def test_mysql_refund_review_and_revocation_serialize(fresh_chain, winner):
    """A stale RR snapshot cannot authorize a review after revocation commits."""
    import time
    from threading import Event, get_ident

    from sqlalchemy import event

    from app.core.exceptions import ForbiddenError
    from app.models.billing_ledger import BillingRefund, BillingRefundReviewerGrant
    from app.models.rbac import Role, UserRole
    from app.models.user import User
    from app.services import billing_checkout_service as checkout
    from app.services import billing_refund_reviewer_service as authority

    sessions = sessionmaker(bind=fresh_chain, autoflush=False, expire_on_commit=False)
    with sessions.begin() as db:
        admin_role = db.query(Role).filter_by(code="admin", status="active").one()
        reviewer = User(username=f"refund-review-{secrets.token_hex(6)}", password="x", role="admin", status=1)
        buyer = User(username=f"refund-buyer-{secrets.token_hex(6)}", password="x", role="user", status=1)
        db.add_all([reviewer, buyer])
        db.flush()
        db.add(UserRole(user_id=reviewer.id, role_id=admin_role.id))
        db.flush()
        authority.set_reviewer(db, actor_id=900, user_id=reviewer.id, active=True, reason="授权测试")
        order = checkout.create_order(db, user_id=buyer.id, product="reviewer_month", quantity=1, channel="alipay")
        checkout._apply_verified(db, order, {"trade_id": f"mysql-review-{secrets.token_hex(6)}"})
        refund = checkout.request_refund(db, order_no=order.order_no, requester_id=buyer.id, reason="退款测试")
        reviewer_id, refund_no = reviewer.id, refund.refund_no

    locked, release, executing = Event(), Event(), Event()
    contender_thread, contender_connection = [], []

    def reviewer():
        with sessions() as db:
            # Establish an ordinary consistent-read snapshot before locking.
            db.query(BillingRefundReviewerGrant.id).filter_by(user_id=reviewer_id).first()
            try:
                authority.require_reviewer(db, reviewer_id, 0)
                if winner == "review":
                    locked.set()
                    assert release.wait(15)
                checkout.review_manual_refund(db, refund_no=refund_no, admin_id=reviewer_id,
                                              decision="approve", reason="复核通过")
                db.commit()
                return "approved"
            except ForbiddenError:
                db.rollback()
                return "denied"

    def revoke():
        with sessions.begin() as db:
            authority.set_reviewer(db, actor_id=900, user_id=reviewer_id, active=False, reason="撤销测试")
            if winner == "revoke":
                locked.set()
                assert release.wait(15)
        return "revoked"

    operations = {"review": reviewer, "revoke": revoke}
    contender = "revoke" if winner == "review" else "review"

    def run_contender():
        contender_thread.append(get_ident())
        return operations[contender]()

    def observe(conn, _cursor, statement, _parameters, _context, _many):
        if contender_thread and get_ident() == contender_thread[0] and "FOR UPDATE" in statement.upper():
            if not contender_connection:
                contender_connection.append(conn.connection.driver_connection.thread_id())
                executing.set()

    event.listen(fresh_chain, "before_cursor_execute", observe)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(operations[winner])
            try:
                assert locked.wait(10)
                second = pool.submit(run_contender)
                assert executing.wait(10)
                deadline = time.monotonic() + 8
                waiting = False
                while time.monotonic() < deadline:
                    with fresh_chain.connect() as observer:
                        waiting = bool(observer.execute(text(
                            "SELECT COUNT(*) FROM performance_schema.data_lock_waits w "
                            "JOIN performance_schema.threads t ON t.thread_id=w.requesting_thread_id "
                            "WHERE t.processlist_id=:connection_id"
                        ), {"connection_id": contender_connection[0]}).scalar_one())
                    if waiting:
                        break
                    time.sleep(0.02)
                assert waiting, "MySQL 未观察到退款审核与撤权竞争等待"
            finally:
                release.set()
            outcomes = first.result(timeout=20), second.result(timeout=20)
    finally:
        event.remove(fresh_chain, "before_cursor_execute", observe)
    assert outcomes == (("approved", "revoked") if winner == "review" else ("revoked", "denied"))
    with sessions() as db:
        assert db.query(BillingRefund).filter_by(refund_no=refund_no).one().status == (
            "approved" if winner == "review" else "manual_review"
        )
        assert db.query(BillingRefundReviewerGrant).filter_by(user_id=reviewer_id).one().active == 0


def test_mysql_rate_cancellation_cannot_commit_after_catalog_was_used(fresh_chain, monkeypatch):
    """A pending cancellation must not invalidate a rate used across its effective boundary."""
    from app.models.billing_ledger import BillingRateCatalog
    from app.services import billing_rate_cancel_service as cancel
    from app.services import billing_rate_catalog_service as rates

    sessions = sessionmaker(bind=fresh_chain, autoflush=False, expire_on_commit=False)
    before = datetime.now(timezone.utc)
    effective = before + timedelta(hours=4)
    source_time = before - timedelta(minutes=3)
    with sessions.begin() as db:
        draft = rates.create_draft(
            db, actor_id=900, effective_at=effective,
            source_url="https://api-docs.deepseek.com/quick_start/pricing/", source_checked_at=source_time,
            model_cny_source_url="https://api-docs.deepseek.com/zh-cn/quick_start/pricing/",
            model_cny_source_checked_at=source_time, fx_source_url="https://www.boc.cn/sourcedb/whpj/index.html",
            fx_source_checked_at=source_time, fx_cny_per_usd_scaled=67_168, change_note="生效并发测试",
            items=rates.baseline_items(), now=before,
        )
        catalog_id = draft.id
        rates.approve_draft(db, catalog_id=catalog_id, actor_id=901, now=before)
    monkeypatch.setattr(cancel, "_now", lambda: (effective - timedelta(seconds=1)).replace(tzinfo=None))
    with sessions.begin() as db:
        request_id = cancel.propose(db, catalog_id=catalog_id, actor_id=900, note="取消申请").id
    from threading import Event

    snapshot_ready, proceed, executing = Event(), Event(), Event()

    def use_catalog():
        with sessions.begin() as db:
            # Establish an RR snapshot before the cancellation is committed.
            assert db.get(BillingRateCatalog, catalog_id).status == "approved"
            snapshot_ready.set()
            assert proceed.wait(5)
            executing.set()
            return rates.active_catalog(db, now=effective + timedelta(seconds=1), lock_for_use=True,
                                        clock=lambda: effective + timedelta(seconds=1)).id

    with sessions() as deciding, ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(use_catalog)
        assert snapshot_ready.wait(5)
        cancel.decide(deciding, request_id=request_id, actor_id=901, decision="approve", note="同意取消")
        proceed.set()
        assert executing.wait(5)
        try:
            # A current read must wait for the pending transition rather than
            # read the earlier committed version of the cancelled catalog.
            from concurrent.futures import TimeoutError

            with pytest.raises(TimeoutError):
                future.result(timeout=1)
        finally:
            deciding.commit()
        used_id = future.result(timeout=10)

    with sessions() as db:
        final_status = db.get(BillingRateCatalog, catalog_id).status
    assert not (used_id == catalog_id and final_status == "canceled"), (
        "目录已被新请求采用，之后却被取消，历史费率发生回溯"
    )


def test_mysql_rate_shared_readers_and_delayed_cancel_expiry(fresh_chain, monkeypatch):
    """Readers share a rate; cancellation waits, then rechecks time after the lock."""
    from threading import Event

    from app.services import billing_rate_cancel_service as cancel
    from app.services import billing_rate_catalog_service as rates

    sessions = sessionmaker(bind=fresh_chain, autoflush=False, expire_on_commit=False)
    before = datetime.now(timezone.utc)
    effective = before + timedelta(hours=5)
    source_time = before - timedelta(minutes=5)
    with sessions.begin() as db:
        draft = rates.create_draft(
            db, actor_id=910, effective_at=effective,
            source_url="https://api-docs.deepseek.com/quick_start/pricing/", source_checked_at=source_time,
            model_cny_source_url="https://api-docs.deepseek.com/zh-cn/quick_start/pricing/",
            model_cny_source_checked_at=source_time, fx_source_url="https://www.boc.cn/sourcedb/whpj/index.html",
            fx_source_checked_at=source_time, fx_cny_per_usd_scaled=67_168, change_note="共享锁测试",
            items=rates.baseline_items(), now=before,
        )
        catalog_id = draft.id
        rates.approve_draft(db, catalog_id=catalog_id, actor_id=911, now=before)
    expired = Event()
    monkeypatch.setattr(cancel, "_now", lambda: (
        effective + timedelta(seconds=1) if expired.is_set() else effective - timedelta(seconds=1)
    ).replace(tzinfo=None))
    with sessions.begin() as db:
        request_id = cancel.propose(db, catalog_id=catalog_id, actor_id=910, note="取消申请").id

    executing = Event()

    def decide():
        with sessions.begin() as db:
            executing.set()
            return cancel.decide(db, request_id=request_id, actor_id=911, decision="approve", note="同意").status

    with sessions() as first, sessions() as second, ThreadPoolExecutor(max_workers=1) as pool:
        assert rates.active_catalog(first, now=effective + timedelta(seconds=1), lock_for_use=True,
                                        clock=lambda: effective + timedelta(seconds=1)).id == catalog_id
        assert rates.active_catalog(second, now=effective + timedelta(seconds=1), lock_for_use=True,
                                        clock=lambda: effective + timedelta(seconds=1)).id == catalog_id
        future = pool.submit(decide)
        assert executing.wait(5)
        from concurrent.futures import TimeoutError

        try:
            with pytest.raises(TimeoutError):
                future.result(timeout=1)
            expired.set()
        finally:
            first.commit()
            second.commit()
        assert future.result(timeout=10) == "expired"


def test_mysql_rate_quote_wait_rechecks_activation_and_source_expiry(fresh_chain):
    """A delayed shared pricing lock cannot authorize using its earlier clock."""
    from threading import Event

    from app.models.billing_ledger import BillingRateCatalog
    from app.services import billing_ledger_service as ledger
    from app.services import billing_rate_catalog_service as rates

    sessions = sessionmaker(bind=fresh_chain, autoflush=False, expire_on_commit=False)
    before = datetime.now(timezone.utc)
    source = before - timedelta(minutes=7)
    first_effective = before + timedelta(hours=6)
    second_effective = before + timedelta(hours=7)
    with sessions.begin() as db:
        ids = []
        for index, effective in enumerate((first_effective, second_effective)):
            checked = source - timedelta(minutes=index)
            draft = rates.create_draft(
                db, actor_id=920, effective_at=effective,
                source_url="https://api-docs.deepseek.com/quick_start/pricing/", source_checked_at=checked,
                model_cny_source_url="https://api-docs.deepseek.com/zh-cn/quick_start/pricing/",
                model_cny_source_checked_at=checked, fx_source_url="https://www.boc.cn/sourcedb/whpj/index.html",
                fx_source_checked_at=checked, fx_cny_per_usd_scaled=67_168, change_note="报价时间验证",
                items=rates.baseline_items(), now=before,
            )
            rates.approve_draft(db, catalog_id=draft.id, actor_id=921, now=before)
            ids.append(draft.id)

    def check_wait(catalog_id, quoted_at, resumed_at, expected):
        waiting, resumed = Event(), Event()

        def quote():
            with sessions.begin() as db:
                waiting.set()
                with pytest.raises(ledger.BillingError, match=expected):
                    ledger.quote_catalog_milli(
                        db, "deepseek-flash", 100, 100, now=quoted_at,
                        clock=lambda: resumed_at if resumed.is_set() else quoted_at,
                    )

        with sessions() as blocker, ThreadPoolExecutor(max_workers=1) as pool:
            blocker.query(BillingRateCatalog).filter_by(id=catalog_id).with_for_update().one()
            future = pool.submit(quote)
            assert waiting.wait(5)
            from concurrent.futures import TimeoutError

            try:
                with pytest.raises(TimeoutError):
                    future.result(timeout=1)
                resumed.set()
            finally:
                blocker.commit()
            future.result(timeout=10)

    check_wait(ids[0], second_effective - timedelta(seconds=1), second_effective + timedelta(seconds=1), "切换")
    expiry = source - timedelta(minutes=1) + timedelta(days=30)
    check_wait(ids[1], expiry - timedelta(seconds=1), expiry + timedelta(seconds=1), "过期")

    # A quote and its hold in the same transaction keep the original shared lock,
    # but a rate switch before reservation must still abort the entire attempt.
    identity = ledger.BillingProviderIdentity(
        identity_version="deepseek-official-v1", provider_name="deepseek",
        endpoint_host="api.deepseek.com", credential_source="system",
    )
    quoted_at = second_effective - timedelta(seconds=1)
    with sessions() as db:
        amount = ledger.quote_catalog_milli(db, "deepseek-flash", 10_000, 10_000,
                                             now=quoted_at, clock=lambda: quoted_at)
        with pytest.raises(ledger.BillingError, match="切换"):
            ledger.reserve(
                db, request_key="quote-activation-boundary", model_name="deepseek-flash", user_id=1,
                personal_wallet_id=1, max_milli=amount, provider_identity=identity,
                now=quoted_at, clock=lambda: second_effective + timedelta(seconds=1),
            )
        db.rollback()


def test_mysql_pricing_shared_read_and_new_rate_approval_do_not_deadlock(fresh_chain):
    """Pricing a current version and approving a later version can both finish."""
    import time
    from threading import Event, get_ident

    from sqlalchemy import event

    from app.services import billing_rate_catalog_service as rates

    sessions = sessionmaker(bind=fresh_chain, autoflush=False, expire_on_commit=False)
    before = datetime.now(timezone.utc)
    with sessions.begin() as db:
        ids = []
        for index in range(2):
            checked = before - timedelta(minutes=12 + index)
            draft = rates.create_draft(
                db, actor_id=930, effective_at=before + timedelta(hours=8 + index),
                source_url="https://api-docs.deepseek.com/quick_start/pricing/", source_checked_at=checked,
                model_cny_source_url="https://api-docs.deepseek.com/zh-cn/quick_start/pricing/",
                model_cny_source_checked_at=checked, fx_source_url="https://www.boc.cn/sourcedb/whpj/index.html",
                fx_source_checked_at=checked, fx_cny_per_usd_scaled=67_168, change_note="审批并发验证",
                items=rates.baseline_items(), now=before,
            )
            ids.append(draft.id)
            if index == 0:
                rates.approve_draft(db, catalog_id=draft.id, actor_id=931, now=before)
    executing = Event()
    approval_thread, connection_id = [], []

    def approve():
        approval_thread.append(get_ident())
        with sessions.begin() as db:
            return rates.approve_draft(db, catalog_id=ids[1], actor_id=931, now=before).id

    def observe(conn, _cursor, statement, _parameters, _context, _many):
        if approval_thread and get_ident() == approval_thread[0] and "FOR UPDATE" in statement.upper():
            if not connection_id:
                connection_id.append(conn.connection.driver_connection.thread_id())
                executing.set()

    pricing_at = before + timedelta(hours=8, seconds=1)
    event.listen(fresh_chain, "before_cursor_execute", observe)
    try:
        with sessions() as pricing, ThreadPoolExecutor(max_workers=1) as pool:
            assert rates.active_catalog(
                pricing, now=pricing_at, lock_for_use=True, clock=lambda: pricing_at,
            ).id == ids[0]
            future = pool.submit(approve)
            try:
                assert executing.wait(5)
                deadline = time.monotonic() + 5
                observed = False
                while time.monotonic() < deadline:
                    if future.done():
                        observed = True
                        break
                    with fresh_chain.connect() as observer:
                        observed = bool(observer.execute(text(
                            "SELECT COUNT(*) FROM performance_schema.data_lock_waits w "
                            "JOIN performance_schema.threads t ON t.thread_id=w.requesting_thread_id "
                            "WHERE t.processlist_id=:connection_id"
                        ), {"connection_id": connection_id[0]}).scalar_one())
                    if observed:
                        break
                    time.sleep(0.02)
                assert observed, "未观察到审批完成或 MySQL 锁等待"
            finally:
                pricing.commit()
            assert future.result(timeout=10) == ids[1]
    finally:
        event.remove(fresh_chain, "before_cursor_execute", observe)


def test_mysql_pricing_crosses_activation_while_approval_runs(fresh_chain):
    """Overlapping approval and pricing must either finish or fail closed without a deadlock."""
    from threading import Event, get_ident

    from sqlalchemy import event

    from app.services import billing_rate_catalog_service as rates

    sessions = sessionmaker(bind=fresh_chain, autoflush=False, expire_on_commit=False)
    before = datetime.now(timezone.utc)
    with sessions.begin() as db:
        ids = []
        for index in range(2):
            checked = before - timedelta(minutes=16 + index)
            draft = rates.create_draft(
                db, actor_id=940, effective_at=before + timedelta(hours=10 + index),
                source_url="https://api-docs.deepseek.com/quick_start/pricing/", source_checked_at=checked,
                model_cny_source_url="https://api-docs.deepseek.com/zh-cn/quick_start/pricing/",
                model_cny_source_checked_at=checked, fx_source_url="https://www.boc.cn/sourcedb/whpj/index.html",
                fx_source_checked_at=checked, fx_cny_per_usd_scaled=67_168, change_note="切换并发验证",
                items=rates.baseline_items(), now=before,
            )
            ids.append(draft.id)
            if index == 0:
                rates.approve_draft(db, catalog_id=draft.id, actor_id=941, now=before)
    first_locked, approval_query = Event(), Event()
    approval_thread = []

    def clock():
        first_locked.set()
        assert approval_query.wait(5)
        return before + timedelta(hours=11, seconds=1)

    def pricing():
        with sessions.begin() as db:
            try:
                rates.active_catalog(db, now=before + timedelta(hours=10, seconds=1),
                                     lock_for_use=True, clock=clock)
                return "unchanged"
            except rates.RateCatalogError:
                return "switched"

    def approve():
        approval_thread.append(get_ident())
        with sessions.begin() as db:
            rates.approve_draft(db, catalog_id=ids[1], actor_id=941, now=before)
            return "approved"

    def observe(_conn, _cursor, statement, _parameters, _context, _many):
        if approval_thread and get_ident() == approval_thread[0] and "FOR UPDATE" in statement.upper():
            approval_query.set()

    event.listen(fresh_chain, "before_cursor_execute", observe)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            price_future = pool.submit(pricing)
            assert first_locked.wait(5)
            approval_future = pool.submit(approve)
            assert price_future.result(timeout=15) in {"unchanged", "switched"}
            assert approval_future.result(timeout=15) == "approved"
    finally:
        event.remove(fresh_chain, "before_cursor_execute", observe)
