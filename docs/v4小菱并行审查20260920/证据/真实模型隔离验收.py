"""真实模型团队验收：只写本次独立 SQLite，不启动 FastAPI 生命周期或生产调度器。

在仓库 backend/.venv 中运行本文件。模型地址、名称和密钥按项目现有配置解析，
不打印或复制密钥；输入仅为本文件定义的合成源代码。HTTP 包装器只记录真实
请求的开始/结束、状态、模型名和团队归因，不改写请求或响应。
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sys
import threading
import time
from datetime import datetime, timezone
from urllib.parse import urlparse

ROOT = (Path(os.environ["PRISM_ACCEPTANCE_ROOT"]) if os.environ.get("PRISM_ACCEPTANCE_ROOT")
        else Path(__file__).resolve().parents[3]).resolve()
STAMP = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
OUTPUT = Path(os.environ.get("PRISM_ACCEPTANCE_OUTPUT_DIR", str(Path(__file__).resolve().parent / "真实模型运行")))
RUN = OUTPUT.resolve() / STAMP
RUN.mkdir(parents=True, exist_ok=False)
os.chdir(RUN)
os.environ.update(DB_HOST="sqlite", DB_NAME="isolated_acceptance", APP_ENV="dev")
sys.path.insert(0, str(ROOT / "backend" if (ROOT / "backend").is_dir() else ROOT))

import httpx  # noqa: E402
from loguru import logger  # noqa: E402
from sqlalchemy import event  # noqa: E402

try:
    from app.core.config import settings  # noqa: E402
except Exception:
    # Pydantic 的默认异常可能包含环境配置值，禁止把原始异常显示到终端。
    raise SystemExit("隔离配置初始化失败；配置值不会输出，请核对本地设置") from None
from app.core.database import Base, SessionLocal, engine  # noqa: E402
from app.models import load_all_models  # noqa: E402

assert engine.dialect.name == "sqlite", "拒绝连接非隔离数据库"
logger.remove()
logger.add(str(RUN / "运行日志.log"), level="INFO", backtrace=False, diagnose=False)


@event.listens_for(engine, "connect")
def sqlite_options(connection, _record):
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA busy_timeout=30000")


load_all_models()
Base.metadata.create_all(engine)

from app.agents.orchestrator import Orchestrator  # noqa: E402
from app.ai.multi_agent import get_agent_profiles  # noqa: E402
from app.models.agent_team import AgentTeam, AgentTeamMember, AgentTeamTask  # noqa: E402
from app.models.ai_call_log import AiCallLog  # noqa: E402
from app.models.code_file import CodeFile  # noqa: E402
from app.models.code_version import CodeVersion  # noqa: E402
from app.models.project import Project  # noqa: E402
from app.models.rbac import Permission, Role, RolePermission, UserRole  # noqa: E402
from app.models.review_issue import ReviewIssue  # noqa: E402
from app.models.review_task import ReviewTask  # noqa: E402
from app.models.user import User  # noqa: E402
from app.schemas.agent_team import AgentTeamCreateIn  # noqa: E402
from app.services import agent_team_dispatcher, agent_team_service, report_service  # noqa: E402
from app.services.ai_usage_context import attribution_snapshot  # noqa: E402
from app.utils.api_resolver import resolve_api_config  # noqa: E402

# 仅限制本地本轮执行时长和团队并发，不替换模型或模型响应。
settings.agent_team_enabled = True
settings.agent_team_max_active_children = 3
settings.agent_full_validation_wait_seconds = 480
settings.agent_team_task_lease_seconds = 540
settings.security_semantic_max_requests = min(settings.security_semantic_max_requests, 6)
settings.security_semantic_timeout_seconds = min(settings.security_semantic_timeout_seconds, 420)

SAMPLE = '''from flask import Flask, request
import sqlite3
import subprocess

app = Flask(__name__)

@app.get("/search")
def search():
    user_name = request.args.get("name", "")
    with sqlite3.connect(":memory:") as connection:
        return str(connection.execute("SELECT id FROM users WHERE name = '" + user_name + "'").fetchall())

@app.get("/ping")
def ping():
    host = request.args.get("host", "localhost")
    return subprocess.check_output("ping -c 1 " + host, shell=True).decode()

@app.post("/calculate")
def calculate():
    return str(eval(request.form["expression"]))
'''


def save(name, value):
    (RUN / name).write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


source_root = ROOT / "backend" / "app" if (ROOT / "backend" / "app").is_dir() else ROOT / "app"
save("验收源码指纹.json", {str(path.relative_to(source_root)): hashlib.sha256(path.read_bytes()).hexdigest()
     for path in sorted(source_root.rglob("*.py"))})


http_events = []
http_guard = threading.Lock()
original_send = httpx.Client.send


def observed_send(self, request, *args, **kwargs):
    started = datetime.now(timezone.utc)
    mono = time.monotonic()
    try:
        body = json.loads(request.content)
    except (ValueError, TypeError):
        body = {}
    attribution = attribution_snapshot()
    row = {"started_at": started.isoformat(), "model": body.get("model"),
           "thread": threading.current_thread().name,
           "team_id": attribution.get("agent_team_id"),
           "team_task_id": attribution.get("agent_team_task_id")}
    try:
        response = original_send(self, request, *args, **kwargs)
        row["http_status"] = response.status_code
        return response
    except Exception as exc:
        row["exception_type"] = type(exc).__name__
        raise
    finally:
        row["finished_at"] = datetime.now(timezone.utc).isoformat()
        row["duration_ms"] = round((time.monotonic() - mono) * 1000)
        with http_guard:
            http_events.append(row)
            save("真实HTTP时序.json", http_events)


httpx.Client.send = observed_send
Orchestrator(register=True)
with SessionLocal() as db:
    actor = User(username="isolated_v4_acceptance", password="isolated_no_login", role="user", status=1)
    role = Role(name="普通用户", code="user", status="active", is_builtin=1)
    db.add_all([actor, role])
    db.flush()
    db.add(UserRole(user_id=actor.id, role_id=role.id))
    for code in ("review:start", "review:view", "issue:view", "report:view", "report:export:json", "ai:chat"):
        permission = Permission(code=code, name=code, module=code.split(":")[0], type="api")
        db.add(permission)
        db.flush()
        db.add(RolePermission(role_id=role.id, permission_id=permission.id))
    project = Project(user_id=actor.id, project_name="v4合成漏洞隔离验收", language="python", status="active")
    db.add(project)
    db.flush()
    source = CodeFile(project_id=project.id, file_name="synthetic_routes.py", file_path="synthetic_routes.py",
                      language="python", content=SAMPLE, status="active", version_no=1, is_binary=0,
                      line_count=len(SAMPLE.splitlines()), size_bytes=len(SAMPLE.encode()), raw_size=len(SAMPLE.encode()))
    db.add(source)
    db.flush()
    db.add(CodeVersion(file_id=source.id, version_no=1, content=SAMPLE, create_time=datetime.now(timezone.utc)))
    db.commit()
    cfg = resolve_api_config(db, actor.id)
    if not cfg.api_key or cfg.api_key == "sk-xxxxx":
        save("验收结果.json", {"passed": False, "blocker": "本地配置缺少可用模型密钥，未执行模型请求"})
        raise SystemExit("本地模型密钥不可用；详情见验收结果，不显示密钥")
    save("输入与配置摘要.json", {"input_kind": "synthetic_only", "files": 1,
         "source_sha256": hashlib.sha256(SAMPLE.encode()).hexdigest(), "source_lines": len(SAMPLE.splitlines()),
         "model": cfg.model, "endpoint_host": urlparse(cfg.base_url).hostname, "config_source": cfg.source,
         "db_kind": "isolated_sqlite", "production_data_used": False, "model_response_mocked": False})
    (RUN / "synthetic_routes.py").write_text(SAMPLE, encoding="utf-8")
    payload = AgentTeamCreateIn.model_validate({
        "surface": "user", "session_id": "v4_isolated_" + STAMP,
        "title": "现有Agent真实并行验收", "objective": "对合成样本并行执行正式全量代码审查和独立白盒审计，再核对真实结果",
        "members": [
            {"member_key": "review", "display_name": "正式代码审查", "address": "agent:review_orchestrator"},
            {"member_key": "audit", "display_name": "白盒安全审计", "address": "agent:security_sentinel"},
            {"member_key": "report", "display_name": "证据复核汇总", "address": "agent:reporter", "role": "summarizer"},
        ],
        "tasks": [
            {"task_key": "review", "member_key": "review", "title": "正式全量代码审查", "instructions": "运行现有审查画像，保存真实任务与发现，覆盖失败必须如实报告。",
             "max_attempts": 1, "input": {"operation": "run_review", "project_id": project.id, "review_type": "full"}},
            {"task_key": "audit", "member_key": "audit", "title": "独立白盒安全审计", "instructions": "完整审计所有合成代码，检查注入与命令执行，明确证据和覆盖。",
             "max_attempts": 1, "input": {"project_id": project.id, "scan_mode": "full"}},
            {"task_key": "report", "member_key": "report", "title": "结果证据复核", "instructions": "核对两条依赖终态与证据，失败不得标记为全部完成。",
             "max_attempts": 1, "depends_on": ["review", "audit"], "input": {}},
        ], "max_active_children": 2, "max_attempts": 1,
    })
    created = agent_team_service.create_team(db, actor, payload)
    team_id = created["team_id"] if "team_id" in created else created["id"]
    user_id = actor.id
    save("创建团队.json", created)

print(json.dumps({"event": "started", "run_directory": str(RUN), "team_id": team_id}), flush=True)
begin = time.monotonic()
dispatch = agent_team_dispatcher.dispatch_once(limit=3)
httpx.Client.send = original_send

with SessionLocal() as db:
    actor = db.get(User, user_id)
    team = agent_team_service.get_team(db, actor, team_id)
    save("团队终态.json", team)
    tasks = db.query(AgentTeamTask).filter_by(team_id=team_id).order_by(AgentTeamTask.id).all()
    raw_results = {row.task_key: json.loads(row.result_json or "{}") for row in tasks}
    save("完整团队结果.json", raw_results)
    members = db.query(AgentTeamMember).filter_by(team_id=team_id).all()
    reviews = db.query(ReviewTask).filter_by(agent_team_id=team_id).all()
    logs = db.query(AiCallLog).order_by(AiCallLog.id).all()
    call_rows = [{"id": row.id, "task_id": row.task_id, "team_id": row.agent_team_id,
                  "team_task_id": row.agent_team_task_id, "agent_label": row.agent_label,
                  "file_id": row.file_id, "chunk_index": row.chunk_index,
                  "model_name": row.model_name, "status": row.status, "duration_ms": row.duration_ms,
                  "prompt_tokens": row.prompt_tokens, "completion_tokens": row.completion_tokens,
                  "total_tokens": row.total_tokens, "create_time": row.create_time,
                  "response_present": bool(row.response), "error_message": row.error_message} for row in logs]
    save("真实调用账本.json", call_rows)
    review_rows = []
    for review in reviews:
        issues = db.query(ReviewIssue).filter_by(task_id=review.id).all()
        details = {"task_id": review.id, "status": review.status, "coverage": review.coverage,
                   "review_type": review.review_type, "total_files": review.total_files,
                   "processed_files": review.processed_files, "total_issues": review.total_issues,
                   "persisted_issues": len(issues), "team_id": review.agent_team_id,
                   "team_task_id": review.agent_team_task_id, "error_message": review.error_message,
                   "issues": [{"id": issue.id, "title": issue.title, "severity": issue.severity,
                               "cwe": issue.cwe, "file_id": issue.file_id, "line": issue.line_number,
                               "source": issue.source, "confirmation_count": issue.confirmation_count} for issue in issues]}
        if review.status == "success":
            details["report"] = report_service.get_report_detail(db, actor, review.id)
        review_rows.append(details)
    save("正式审查与报告.json", review_rows)
    by_key = {row.task_key: row for row in tasks}
    task_overlap = False
    if all(by_key[key].started_at and by_key[key].completed_at for key in ("review", "audit")):
        task_overlap = max(by_key[key].started_at for key in ("review", "audit")) < min(
            by_key[key].completed_at for key in ("review", "audit"))
    cross_node_overlap = any(
        a["team_task_id"] != b["team_task_id"] and a["team_task_id"] and b["team_task_id"]
        and max(a["started_at"], b["started_at"]) < min(a["finished_at"], b["finished_at"])
        for index, a in enumerate(http_events) for b in http_events[index + 1:])
    # 画像到实际调用标签使用生产服务自己的映射，不能靠手写期望猜测名称。
    from app.services.review_service import _PROFILE_TO_AGENT_CODE
    expected_profiles = {_PROFILE_TO_AGENT_CODE.get(profile.code, profile.code) for profile in get_agent_profiles("full")}
    review_ids = {review.id for review in reviews}
    observed_profiles = {row.agent_label for row in logs if row.task_id in review_ids and row.status == "success"}
    profile_calls = {row.chunk_index for row in logs if row.task_id in review_ids and row.status == "success"}
    audit_evidence = raw_results.get("audit", {}).get("evidence", [])
    audit_data = next((item["data"] for item in audit_evidence if isinstance(item.get("data"), dict)), {})
    audit_coverage = audit_data.get("compliance", {})
    summary_artifacts = raw_results.get("report", {}).get("artifacts", [])
    summary_data = next((item["data"] for item in summary_artifacts if isinstance(item.get("data"), dict)), {})
    checks = {
        "existing_runtime_members_only": all(row.kind == "runtime" and row.template_id is None for row in members),
        "three_team_nodes_completed": len(tasks) == 3 and all(row.status == "completed" for row in tasks),
        "independent_team_tasks_overlap": task_overlap,
        "actual_http_requests_overlap_across_nodes": bool(cross_node_overlap),
        "one_full_review_success": len(reviews) == 1 and reviews[0].status == "success" and reviews[0].review_type == "full",
        "file_coverage_complete": len(reviews) == 1 and reviews[0].processed_files == reviews[0].total_files == 1
            and (reviews[0].coverage or {}).get("stage") == "complete",
        "issue_counts_match_database": bool(review_rows) and all(row["total_issues"] == row["persisted_issues"] > 0 for row in review_rows),
        "formal_report_readable": bool(review_rows) and all("report" in row for row in review_rows),
        "all_review_profiles_have_real_success_logs": expected_profiles <= observed_profiles,
        "each_review_profile_index_recorded": set(range(len(get_agent_profiles("full")))) <= profile_calls,
        "audit_reports_full_semantic_coverage": audit_coverage.get("semantic_complete") is True,
        "summary_retains_returned_findings": int(summary_data.get("unique_finding_count") or 0) > 0,
        "all_http_attempts_accounted": len(logs) == len(http_events) > 0,
        "all_calls_attributed_to_team_tasks": all(row.agent_team_id == team_id and row.agent_team_task_id in {t.id for t in tasks} for row in logs),
        "all_real_http_succeeded": bool(http_events) and all(row.get("http_status") == 200 for row in http_events),
    }
    result = {"passed": all(checks.values()), "checks": checks, "dispatch": dispatch,
              "elapsed_seconds": round(time.monotonic() - begin, 2), "http_attempts": len(http_events),
              "ai_call_logs": len(logs), "expected_profiles": sorted(expected_profiles),
              "observed_profiles": sorted(observed_profiles), "run_directory": str(RUN),
              "boundary": "合成单文件真实模型隔离集成，不代表生产数据全项目验收；汇总节点依据返回证据确定性核对。"}
    save("验收结果.json", result)
    print(json.dumps(result, ensure_ascii=False), flush=True)
    if not result["passed"]:
        raise SystemExit(1)
