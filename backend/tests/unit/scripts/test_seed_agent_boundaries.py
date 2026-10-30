"""seed_agent_boundaries.py 必须能在全新库上跑通，且不能把运行时真实调用的工具锁死。

脚本是独立进程：没有 FastAPI 启动，运行时 Agent 注册表为空；BOUNDARIES 又必须与
contracts.CONTRACTS 逐项对应，否则 main() 在清单校验处直接抛 RuntimeError。边界是
default-deny，所以每个受管 Agent 在网关里真实调用的工具码都必须写进它的边界，否则
这个脚本一旦在生产执行，运维/页面执行路径会被"工具不在职责边界内"整体拒绝。
"""

from __future__ import annotations

import ast
import importlib.util
import json
import os
import sqlite3
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

BACKEND = Path(__file__).resolve().parents[3]
SCRIPT = BACKEND / "scripts" / "seed_agent_boundaries.py"

# 冷进程驱动：不读开发机 .env；先建表（等同全新库已完成 init.sql + alembic），再原样运行脚本。
_COLD_DRIVER = textwrap.dedent(
    """
    import runpy
    import sys

    from pydantic_settings import BaseSettings

    original_init = BaseSettings.__init__

    def init_without_dotenv(instance, *args, **kwargs):
        kwargs["_env_file"] = None
        original_init(instance, *args, **kwargs)

    BaseSettings.__init__ = init_without_dotenv

    backend, script = sys.argv[1], sys.argv[2]
    sys.path.insert(0, backend)

    from app.core.database import Base, engine
    from app.models import load_all_models

    load_all_models()
    Base.metadata.create_all(engine)
    runpy.run_path(script, run_name="__main__")
    """
)


@pytest.fixture(scope="module")
def seed():
    spec = importlib.util.spec_from_file_location("seed_agent_boundaries_under_test", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _gateway_call_sites() -> dict[tuple[str, str], list[str]]:
    """扫描 app/ 里所有字面量 agent_code/tool_code 的 tool_gateway.authorize/execute 调用。"""
    sites: dict[tuple[str, str], list[str]] = {}
    for path in sorted((BACKEND / "app").rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        if "tool_gateway" not in source:
            continue
        for node in ast.walk(ast.parse(source)):
            if not (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in {"authorize", "execute"}
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "tool_gateway"
            ):
                continue
            keywords = {keyword.arg: keyword.value for keyword in node.keywords}
            agent, tool = keywords.get("agent_code"), keywords.get("tool_code")
            if isinstance(agent, ast.Constant) and isinstance(tool, ast.Constant):
                where = f"{path.relative_to(BACKEND).as_posix()}:{node.lineno}"
                sites.setdefault((agent.value, tool.value), []).append(where)
    return sites


def test_boundary_catalog_matches_every_managed_contract(seed) -> None:
    from app.agents.contracts import CONTRACTS, PROTECTED_AGENT_CODES

    expected = set(CONTRACTS) - set(PROTECTED_AGENT_CODES)

    assert set(seed.BOUNDARIES) == expected, (
        f"missing={sorted(expected - set(seed.BOUNDARIES))} unexpected={sorted(set(seed.BOUNDARIES) - expected)}"
    )


def test_no_boundary_locks_out_a_tool_the_runtime_really_calls(seed) -> None:
    sites = _gateway_call_sites()
    # 扫描器自检：已知的两条真实调用链必须被扫到，否则本用例会空转通过。
    assert ("operations", "admin_execute_operation") in sites
    assert ("site_operator", "operate_current_page") in sites

    locked_out = []
    for (agent, tool), where in sorted(sites.items()):
        boundary = seed.BOUNDARIES.get(agent)
        if boundary is None:  # 受保护 Agent 的边界由 agent_governance_service 另行维护
            continue
        permitted = set(boundary["allowed_tools"]) | set(boundary["approval_tools"])
        if tool not in permitted or tool in boundary["blocked_tools"]:
            locked_out.append(f"{agent} -> {tool} (调用点 {', '.join(where)})")

    assert not locked_out, "边界会拒绝运行时真实调用的工具：" + "; ".join(locked_out)


def test_supervisor_boundary_is_read_only(seed) -> None:
    boundary = seed.BOUNDARIES["supervisor"]

    assert boundary["approval_tools"] == []
    assert not {"workflow_dispatch", "release_publisher", "knowledge_store"} & set(boundary["allowed_tools"])


def _run_seed(workdir: Path, db_name: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-I", "-X", "utf8", "-c", _COLD_DRIVER, str(BACKEND), str(SCRIPT)],
        cwd=workdir,
        env={
            "PATH": os.environ.get("PATH", ""),
            "HOME": os.environ.get("HOME", ""),
            "DB_HOST": "sqlite",
            "DB_NAME": db_name,
        },
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=300,
        check=False,
    )


def _counts(database: Path) -> dict[str, int]:
    connection = sqlite3.connect(database)
    try:
        return {
            table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("agent_profile", "agent_tool_permission", "agent_skill_binding", "audit_log")
        }
    finally:
        connection.close()


def _decide(session: Session, agent: str, tool: str, action: str, resource: str, risk: str) -> tuple[str, str]:
    from app.services import tool_gateway

    decision = tool_gateway.authorize(
        session, agent_code=agent, tool_code=tool, action=action, resource=resource, declared_risk=risk
    )
    return decision.decision, decision.risk_level


# 与运行时真实调用点一致：agent_responses_service(operations)、site_agent_service(site_operator)。
_RUNTIME_CALLS = (
    ("operations", "admin_execute_operation", "operations.status", "production", "low"),
    ("operations", "admin_execute_operation", "operations.restart_service", "production", "medium"),
    ("operations", "admin_execute_operation", "operations.restore_database", "production", "critical"),
    ("site_operator", "operate_current_page", "browser.inspect", "current_account", "low"),
    ("site_operator", "operate_current_page", "browser.click", "current_account", "high"),
)


def test_cold_process_seed_completes_is_idempotent_and_keeps_runtime_tools_usable(seed, tmp_path: Path) -> None:
    from app.agents.contracts import CONTRACTS, PROTECTED_AGENT_CODES
    from app.core.database import Base
    from app.models import load_all_models

    database = tmp_path / "boundary_seed.db"
    load_all_models()
    engine = create_engine(f"sqlite:///{database}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        before = {call: _decide(session, *call) for call in _RUNTIME_CALLS}

    first = _run_seed(tmp_path, "boundary_seed")
    assert first.returncode == 0, "脚本在全新库上失败：\n" + "\n".join(first.stderr.splitlines()[-12:])
    summary = json.loads(first.stdout.strip().splitlines()[-1])
    managed = set(CONTRACTS) - set(PROTECTED_AGENT_CODES)
    assert summary["profiles"] == len(CONTRACTS)
    assert summary["contracts_applied"] == summary["boundary_configured"] == len(managed)
    after_first = _counts(database)

    second = _run_seed(tmp_path, "boundary_seed")
    assert second.returncode == 0, "\n".join(second.stderr.splitlines()[-12:])
    assert _counts(database) == after_first, "重复执行不应新增画像、权限、技能绑定或审计记录"

    connection = sqlite3.connect(database)
    try:
        stored = {
            code: json.loads(config)["governance_boundary"]
            for code, config in connection.execute("SELECT code, config_json FROM agent_profile")
            if code in managed
        }
    finally:
        connection.close()
    assert stored == {code: seed.BOUNDARIES[code] for code in managed}

    with Session(engine) as session:
        # 边界生效后，运行时真实调用的决策必须与未配置边界时完全一致。
        after = {call: _decide(session, *call) for call in _RUNTIME_CALLS}
        assert after == before
        # 反向对照：边界确实是 default-deny，上面的"一致"不是因为边界根本没加载。
        for agent in ("operations", "site_operator"):
            assert _decide(session, agent, "shell", "shell.read", "workspace", "low")[0] == "deny"
            assert _decide(session, agent, "unlisted_tool", "x.read", "workspace", "low")[0] == "deny"
    engine.dispose()
