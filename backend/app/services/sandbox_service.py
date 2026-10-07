"""代码测试与持续部署沙箱编排。

后端只处理权限、不可变源码快照、worker 选择和审计。它不挂载 Docker
Socket，也不接受用户命令、镜像、宿主路径、挂载或环境变量。
"""

# ruff: noqa: E501

from __future__ import annotations

import ast
import base64
import binascii
import hashlib
import hmac
import html
import io
import ipaddress
import itertools
import json
import re
import stat
import threading
import time
import urllib.parse
import uuid
import zipfile
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

import httpx
import jwt
from sqlalchemy.orm import Session, object_session, sessionmaker

from app.agents.event_bus import emit_event
from app.agents.events import AgentEventType
from app.agents.syntax_repair_agent import SyntaxRepairAgent, collect_php_lint_errors
from app.ai.language_detector import detect_language
from app.core.config import settings
from app.core.database import SessionLocal
from app.core.exceptions import (
    AppError,
    AuthError,
    ConflictError,
    ForbiddenError,
    NotFoundError,
    PermissionError,
    ServiceUnavailableError,
    ValidationError,
)
from app.core.observability import observe_event
from app.core.permission_codes import PermissionCode
from app.models.agent_capability import (
    SandboxArtifact,
    SandboxEnvironment,
    SandboxEvent,
    SandboxWorker,
)
from app.models.agent_governance import AgentAlert, ApprovalItem
from app.models.project import Project
from app.models.user import User
from app.services import (
    audit_service,
    decompilation_service,
    project_source_revision_service,
    project_source_service,
    rbac_service,
    strategy_learning_service,
)
from app.services.agent_model_service import configure_subagent
from app.services.ai_usage_context import ATTRIBUTION_FIELDS, current_attribution, model_attribution, usage_context
from app.services.project_member_service import (
    get_visible_project_ids,
    require_project_access,
    require_project_execution,
)
from app.utils.api_resolver import decrypt_api_key_with_metadata, encrypt_api_key
from app.utils.archive_extractor import read_archive_members
from app.utils.public_http import pin_public_http_url

LANGUAGES = ("python", "node", "java", "go", "php")
MODES = ("whitebox", "blackbox", "combined", "deploy")
ACTIVE_STATES = ("queued", "recovering", "dispatching", "running", "finalizing", "ready", "stopping")
TERMINAL_STATES = ("succeeded", "failed", "blocked", "stopped", "expired")
PREVIEW_COOKIE_NAME = "prism_sandbox_preview"
PREVIEW_SESSION_SECONDS = 300
_IMAGE_REFS = {
    "python": "prism-sandbox-python:3.11",
    "node": "prism-sandbox-node:20",
    "java": "prism-sandbox-java:17",
    "go": "prism-sandbox-go:1.23",
    "php": "prism-sandbox-php:8.3",
}
_PROJECT_LANGUAGE_TO_RUNTIME = {
    "python": "python",
    "py": "python",
    "javascript": "node",
    "js": "node",
    "typescript": "node",
    "ts": "node",
    "node": "node",
    "nodejs": "node",
    "node.js": "node",
    "vue": "node",
    "svelte": "node",
    "java": "java",
    "go": "go",
    "golang": "go",
    "php": "php",
}
_PROJECT_LANGUAGE_COMPACT_ALIASES = sorted(
    {re.sub(r"[^a-z0-9]+", "", alias): runtime for alias, runtime in _PROJECT_LANGUAGE_TO_RUNTIME.items()}.items(),
    key=lambda item: len(item[0]),
    reverse=True,
)
_PROFILE_POLICIES = {
    "python": {"memory_mb": 512, "cpus": 1.0, "pids": 128, "timeout_seconds": 120, "workspace_mb": 256},
    "node": {"memory_mb": 768, "cpus": 1.0, "pids": 256, "timeout_seconds": 180, "workspace_mb": 512},
    "java": {"memory_mb": 1024, "cpus": 1.0, "pids": 256, "timeout_seconds": 300, "workspace_mb": 768},
    "go": {"memory_mb": 768, "cpus": 1.0, "pids": 256, "timeout_seconds": 180, "workspace_mb": 512},
    "php": {"memory_mb": 512, "cpus": 1.0, "pids": 128, "timeout_seconds": 120, "workspace_mb": 256},
}
_RESOURCE_POLICY_COMMON = {
    "output_bytes": 2_097_152,
    "network": "none",
    "read_only": True,
    "cap_drop": ["ALL"],
    "no_new_privileges": True,
}


_AGENT_TEST_CACHE: dict[tuple[str, str, str], tuple[float, list[dict[str, str]]]] = {}
_AGENT_TEST_CACHE_LOCK = threading.Lock()


def _utcnow() -> datetime:
    # 数据库历史字段是无时区 DateTime，统一写入 UTC naive 值。
    return datetime.utcnow()


def _naive_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _loads(value: str | None, fallback: Any) -> Any:
    try:
        parsed = json.loads(value or "")
    except (TypeError, ValueError, json.JSONDecodeError):
        return fallback
    return parsed


def _is_environment_worker_request_id(environment: SandboxEnvironment, request_id: str) -> bool:
    pattern = rf"{re.escape(environment.public_id)}(?:-r[1-9][0-9]*|-verify-(?:whitebox|blackbox|combined))?"
    return re.fullmatch(pattern, request_id) is not None


def _register_worker_request(environment: SandboxEnvironment, request_id: str) -> None:
    """Persist every concrete Worker request before it can be submitted."""
    environment.agent_config_json = _worker_request_config_json(environment, request_id)


def _worker_request_config_json(environment: SandboxEnvironment, request_id: str) -> str:
    """Build the worker-request ledger without mutating the ORM object."""
    if not _is_environment_worker_request_id(environment, request_id):
        raise RuntimeError("Sandbox Worker request_id 不属于当前环境")
    config = _loads(environment.agent_config_json, {})
    if not isinstance(config, dict):
        config = {}
    raw_ids = config.get("worker_request_ids")
    request_ids = (
        [
            str(item)
            for item in raw_ids
            if isinstance(item, str) and _is_environment_worker_request_id(environment, item)
        ]
        if isinstance(raw_ids, list)
        else []
    )
    if request_id not in request_ids:
        request_ids.append(request_id)
    # 受控流程最多两轮修复加部署验证；上限防止异常状态无限增长。
    config["worker_request_ids"] = request_ids[-16:]
    config["active_worker_request_id"] = request_id
    return _json(config)


_WORKER_EXECUTE_FIELDS = frozenset(
    {
        "request_id",
        "purpose",
        "language",
        "test_mode",
        "db_type",
        "source_sha256",
        "ttl_seconds",
        "image_digest",
    }
)


def _worker_execute_payload(request_envelope: dict[str, Any], source_archive_base64: str) -> dict[str, Any]:
    """Project backend provenance onto the strict, versioned Worker execute contract.

    Revision and repair metadata stay in the durable backend execution ledger. The
    Worker only receives fields it uses to validate and run the immutable archive.
    """
    payload = {key: request_envelope[key] for key in _WORKER_EXECUTE_FIELDS if key in request_envelope}
    payload["source_archive_base64"] = source_archive_base64
    required = _WORKER_EXECUTE_FIELDS | {"source_archive_base64"}
    missing = sorted(required.difference(payload))
    if missing:
        raise RuntimeError(f"Sandbox Worker execute 请求缺少字段: {', '.join(missing)}")
    return payload


def _validate_worker_execution_receipt(
    response: dict[str, Any],
    *,
    request_id: str,
    source_sha256: str,
) -> dict[str, str]:
    """拒绝与提交请求不匹配或没有完整归档回执的 Worker 响应。"""
    returned_request_id = str(response.get("request_id") or "")
    returned_sha256 = str(response.get("source_sha256") or "").lower()
    if returned_request_id != request_id:
        raise RuntimeError("Sandbox Worker 回执 request_id 与当前执行轮次不一致")
    if not returned_sha256 or not hmac.compare_digest(returned_sha256, source_sha256):
        raise RuntimeError("Sandbox Worker 回执源码 SHA-256 与当前执行快照不一致")
    return {"request_id": returned_request_id, "source_sha256": returned_sha256}


def _worker_execution_passed(
    *,
    purpose: str,
    state: str,
    target_status: str,
    conclusion: dict[str, Any],
) -> bool:
    """Only a terminal test result with an explicit zero exit code is a test pass.

    Deploy workers are intentionally long-lived; their running state means the preview
    process is serving, while test workers must reach a terminal result.
    """
    if purpose == "deploy" and state == "running" and target_status == "ready":
        return True
    if target_status != "succeeded":
        return False
    exit_code = conclusion.get("exit_code")
    return (
        state in {"completed", "succeeded"}
        and type(exit_code) is int
        and exit_code == 0
    )


def _worker_status_is_terminal(purpose: str, state: str) -> bool:
    """Test jobs must terminate; only deploy previews treat a live process as ready."""
    if purpose == "deploy" and state == "running":
        return True
    return state in {"completed", "succeeded", "failed", "blocked", "stopped", "expired"}


def _remote_blackbox_passed(execution: dict[str, Any]) -> bool:
    """Only a final 2xx response is a successful functional black-box check."""
    return execution.get("status") == "passed" and execution.get("route_passed") is True


def _execution_result_passed(
    *,
    purpose: str,
    state: str,
    target_status: str,
    conclusion: dict[str, Any],
    test_mode: str,
    evidence: dict[str, Any],
    agent_tests_result: dict[str, Any] | None = None,
    agent_test_generation: dict[str, Any] | None = None,
    remote_target_url: str | None = None,
) -> bool:
    """Combine every required execution receipt before publishing a passing result."""
    if not _worker_execution_passed(
        purpose=purpose,
        state=state,
        target_status=target_status,
        conclusion=conclusion,
    ):
        return False
    if agent_tests_result is not None and not _agent_tests_succeeded(agent_tests_result):
        return False
    if agent_test_generation and agent_test_generation.get("status") == "generated" and agent_tests_result is None:
        return False
    if purpose == "test" and test_mode in {"blackbox", "combined"}:
        blackbox = evidence.get("blackbox_execution")
        if not isinstance(blackbox, dict) and remote_target_url:
            blackbox = evidence.get("remote_blackbox_execution")
        if (
            not isinstance(blackbox, dict)
            or blackbox.get("status") != "passed"
            or blackbox.get("route_passed") is not True
        ):
            return False
    if remote_target_url:
        remote_execution = evidence.get("remote_blackbox_execution")
        if not isinstance(remote_execution, dict) or not _remote_blackbox_passed(remote_execution):
            return False
    return True


def _source_provenance(environment: SandboxEnvironment, worker_receipt: dict[str, str] | None = None) -> dict[str, Any]:
    config = _loads(environment.agent_config_json, {})
    request = _loads(getattr(environment, "worker_request_json", None), {})
    return {
        "source_revision_id": config.get("source_revision_id"),
        "source_revision_no": config.get("source_revision_no"),
        "source_revision_sha256": config.get("source_revision_sha256"),
        "source_revision_parent_sha256": config.get("source_revision_parent_sha256"),
        "original_source_sha256": config.get("original_source_sha256"),
        "execution_source_sha256": getattr(environment, "execution_source_sha256", None),
        "execution_round": int(getattr(environment, "execution_round", 0) or 0),
        "worker_request_id": request.get("request_id") if isinstance(request, dict) else None,
        "worker_receipt": worker_receipt,
        "syntax_repair_revisions": config.get("syntax_repair_revisions", []),
    }


def _append_repair_revision_to_config(
    config_json: str | None,
    *,
    revision: dict[str, Any],
    repair_round: int,
    worker_request_id: str,
) -> str:
    config = _loads(config_json, {})
    if not isinstance(config, dict):
        config = {}
    revisions = config.get("syntax_repair_revisions")
    if not isinstance(revisions, list):
        revisions = []
    revisions.append({**revision, "repair_round": repair_round, "worker_request_id": worker_request_id})
    config["syntax_repair_revisions"] = revisions[-8:]
    return _json(config)


def _registered_worker_request_ids(environment: SandboxEnvironment) -> list[str]:
    """Return current-first concrete request IDs, always including the parent tombstone."""
    config = _loads(environment.agent_config_json, {})
    if not isinstance(config, dict):
        config = {}
    candidates: list[str] = []
    active = config.get("active_worker_request_id")
    if isinstance(active, str):
        candidates.append(active)
    raw_ids = config.get("worker_request_ids")
    if isinstance(raw_ids, list):
        candidates.extend(reversed([item for item in raw_ids if isinstance(item, str)]))
    candidates.append(environment.public_id)
    result: list[str] = []
    for request_id in candidates:
        if request_id in result or not _is_environment_worker_request_id(environment, request_id):
            continue
        result.append(request_id)
    return result


def _normalize_agent_team_context(value: Any) -> dict[str, Any] | None:
    """校验内部团队租约上下文；公开 API schema 不接受这些字段。"""

    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValidationError("Agent 团队运行上下文格式无效", code=40001)
    try:
        team_id = int(value.get("team_id"))
        task_id = int(value.get("task_id"))
        attempt = int(value.get("attempt") or 0)
    except (TypeError, ValueError) as exc:
        raise ValidationError("Agent 团队运行上下文标识无效", code=40001) from exc
    lease_token = str(value.get("lease_token") or "")
    if team_id <= 0 or task_id <= 0 or attempt <= 0 or not lease_token or len(lease_token) > 80:
        raise ValidationError("Agent 团队运行上下文不完整", code=40001)
    normalized: dict[str, Any] = {
        "team_id": team_id,
        "task_id": task_id,
        "attempt": attempt,
        "lease_token": lease_token,
    }
    raw_strategy = value.get("execution_strategy")
    if isinstance(raw_strategy, dict):
        changes = raw_strategy.get("changes")
        try:
            strategy_version = max(1, min(int(raw_strategy.get("version") or 1), 100))
            strategy_attempt = max(1, min(int(raw_strategy.get("attempt") or attempt), 100))
        except (TypeError, ValueError) as exc:
            raise ValidationError("Agent 团队执行策略格式无效", code=40001) from exc
        normalized["execution_strategy"] = {
            "version": strategy_version,
            "attempt": strategy_attempt,
            "mode": str(raw_strategy.get("mode") or "")[:120],
            "instruction": str(raw_strategy.get("instruction") or "")[:2000],
            "previous_error": str(raw_strategy.get("previous_error") or "")[:2000],
            "previous_mode": str(raw_strategy.get("previous_mode") or "")[:120],
            "automatic": bool(raw_strategy.get("automatic", False)),
            "changes": [str(item)[:120] for item in changes[:16]] if isinstance(changes, list) else [],
        }
    return normalized


def _artifact_log_text(conclusion: dict[str, Any]) -> str:
    evidence = conclusion.get("evidence") if isinstance(conclusion.get("evidence"), dict) else {}
    worker_result = evidence.get("worker_result") if isinstance(evidence.get("worker_result"), dict) else {}
    logs = worker_result.get("logs") if isinstance(worker_result.get("logs"), dict) else {}
    text = logs.get("text")
    return str(text or "")[:2_097_152]


def _artifact_documents(
    environment: SandboxEnvironment,
    conclusion: dict[str, Any],
) -> list[tuple[str, str, str, bytes]]:
    passed = bool(conclusion.get("passed"))
    summary = str(conclusion.get("summary") or ("测试通过" if passed else "测试未通过"))
    log_text = _artifact_log_text(conclusion)
    result_json = json.dumps(conclusion, ensure_ascii=False, sort_keys=True, indent=2, default=str).encode("utf-8")
    escaped_summary = html.escape(summary)
    escaped_log = html.escape(log_text[-20_000:])
    escaped_id = html.escape(environment.public_id)
    escaped_source = html.escape(environment.source_sha256)
    escaped_runtime = html.escape(environment.runtime)
    status_class = "ok" if passed else "failed"
    html_report = (
        "<!doctype html>\n"
        '<html lang="zh-CN"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">\n'
        f"<title>Prism Sandbox {escaped_id}</title><style>\n"
        "body{margin:0;background:#f6f7f9;color:#17202a;font:14px/1.6 system-ui,sans-serif}"
        "main{max-width:960px;margin:32px auto;padding:0 20px}h1{font-size:24px}"
        "section{margin:16px 0;padding:18px;background:#fff;border:1px solid #dfe3e8;border-radius:6px}"
        "dl{display:grid;grid-template-columns:160px 1fr;gap:8px}dt{color:#667085}"
        "dd{margin:0;word-break:break-all}pre{overflow:auto;padding:14px;background:#111827;color:#f9fafb;"
        "white-space:pre-wrap}.ok{color:#08783e}.failed{color:#b42318}\n"
        "</style></head><body><main><h1>Prism 沙箱执行报告</h1><section>"
        f'<h2 class="{status_class}">{escaped_summary}</h2><dl>'
        f"<dt>任务</dt><dd>{escaped_id}</dd><dt>Agent</dt><dd>{html.escape(environment.agent_code)}</dd>"
        f"<dt>运行时</dt><dd>{escaped_runtime}</dd><dt>源码 SHA-256</dt><dd>{escaped_source}</dd>"
        "</dl></section><section><h2>执行日志</h2>"
        f"<pre>{escaped_log or '无日志输出'}</pre></section></main></body></html>"
    ).encode("utf-8")
    failure = (
        ""
        if passed
        else (f'<failure message="{escaped_summary}">{html.escape(log_text[-4_000:] or summary)}</failure>')
    )
    agent_test_details: bytes | None = None
    at_result = conclusion.get("agent_tests") if isinstance(conclusion.get("agent_tests"), dict) else None
    details = (
        at_result.get("details") if isinstance(at_result, dict) and isinstance(at_result.get("details"), dict) else {}
    )
    if details:
        parts = []
        for file_name, output in details.items():
            parts.append(f"===== agent 测试用例: {file_name} =====\n{output}\n")
        agent_test_details = "\n".join(parts).encode("utf-8", errors="replace")
    junit = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        f'<testsuite name="PrismSandbox" tests="1" failures="{0 if passed else 1}">'
        f'<testcase classname="{html.escape(environment.agent_code)}" name="{escaped_id}">{failure}'
        f"<system-out>{html.escape(log_text[-64_000:])}</system-out></testcase></testsuite>\n"
    ).encode("utf-8")
    sarif_result = (
        []
        if passed
        else [
            {
                "ruleId": "sandbox.execution.failed",
                "level": "error",
                "message": {"text": summary},
            }
        ]
    )
    sarif = json.dumps(
        {
            "$schema": "https://json.schemastore.org/sarif-2.1.0.json",
            "version": "2.1.0",
            "runs": [
                {
                    "tool": {"driver": {"name": "Prism Sandbox", "version": "1.0"}},
                    "results": sarif_result,
                    "properties": {
                        "environment_id": environment.public_id,
                        "source_sha256": environment.source_sha256,
                        "runtime": environment.runtime,
                    },
                }
            ],
        },
        ensure_ascii=False,
        sort_keys=True,
        indent=2,
    ).encode("utf-8")
    documents = [
        ("result", "sandbox-result.json", "application/json", result_json),
        ("log", "sandbox.log", "text/plain; charset=utf-8", log_text.encode("utf-8")),
        ("junit", "sandbox-junit.xml", "application/xml", junit),
        ("sarif", "sandbox-results.sarif", "application/sarif+json", sarif),
        ("html", "sandbox-report.html", "text/html; charset=utf-8", html_report),
    ]
    if agent_test_details is not None:
        documents.append(
            (
                "agent_test_details",
                f"agent-test-details-{environment.public_id}.txt",
                "text/plain; charset=utf-8",
                agent_test_details,
            )
        )  # noqa: E501
    return documents


def _persist_artifacts(
    db: Session,
    environment: SandboxEnvironment,
    conclusion: dict[str, Any],
    execution_token: str | None = None,
) -> list[SandboxArtifact]:
    if execution_token is not None and not _execution_lease_valid(db, environment.id, execution_token):
        raise RuntimeError("沙箱执行租约已失效")
    db.query(SandboxArtifact).filter(SandboxArtifact.environment_id == environment.id).delete(
        synchronize_session=False,
    )
    rows: list[SandboxArtifact] = []
    for artifact_type, file_name, mime_type, content in _artifact_documents(environment, conclusion):
        digest = hashlib.sha256(content).hexdigest()
        row = SandboxArtifact(
            environment_id=environment.id,
            artifact_type=artifact_type,
            file_name=file_name,
            mime_type=mime_type,
            byte_size=len(content),
            sha256=digest,
            # 确定性引用，避免插入后再 UPDATE 触发 sandbox_artifact 竞态。
            storage_ref=f"database://sandbox-artifact/{environment.public_id}-{artifact_type}",
            content_base64=base64.b64encode(content).decode("ascii"),
        )
        db.add(row)
        rows.append(row)
    db.flush()
    return rows


def _execution_lease_valid(db: Session, environment_id: int, execution_token: str) -> bool:
    """验证执行器仍持有当前环境租约，阻断旧线程覆盖恢复结果。"""
    return (
        db.query(SandboxEnvironment.id)
        .filter(
            SandboxEnvironment.id == environment_id,
            SandboxEnvironment.execution_token == execution_token,
        )
        .first()
        is not None
    )


def _require_execution_lease(db: Session, environment_id: int, execution_token: str | None) -> None:
    """Stop stale workers before they append events or persist a new snapshot."""
    if execution_token is not None and not _execution_lease_valid(db, environment_id, execution_token):
        raise RuntimeError("沙箱执行租约已失效")
    _require_execution_authorization(db, environment_id)


def _require_sandbox_execution(db: Session, actor: User, project_id: int) -> None:
    """Execution intersects existing global permissions and the current project role."""
    if actor.status != 1:
        raise ForbiddenError("当前账号不能执行沙箱任务", code=40300)
    require_project_execution(db, project_id, actor)
    for permission in (PermissionCode.PROJECT_VIEW, PermissionCode.FILE_VIEW):
        if not rbac_service.check_permission(db, actor.id, permission):
            raise PermissionError(
                f"无操作权限: 需要 {permission}",
                detail={"required_permission": permission},
            )


def _require_execution_authorization(db: Session, environment_id: int) -> None:
    """Recheck queued/running work in an independent authorization transaction.

    Lease identity remains separate so revoked actors can still be cleaned up.
    """
    bind = db.get_bind()
    factory = sessionmaker(bind=getattr(bind, "engine", bind), expire_on_commit=False)
    with factory() as auth_db:
        scope = (
            auth_db.query(SandboxEnvironment.project_id, SandboxEnvironment.owner_id)
            .filter(
                SandboxEnvironment.id == environment_id,
            )
            .one_or_none()
        )
        actor = auth_db.get(User, scope.owner_id) if scope is not None else None
        if actor is None:
            raise ForbiddenError("沙箱执行账号或环境已失效", code=40300)
        _require_sandbox_execution(auth_db, actor, int(scope.project_id))


def _require_current_actor_execution(db: Session, actor_id: int, project_id: int) -> None:
    bind = db.get_bind()
    factory = sessionmaker(bind=getattr(bind, "engine", bind), expire_on_commit=False)
    with factory() as auth_db:
        actor = auth_db.get(User, actor_id)
        if actor is None:
            raise ForbiddenError("沙箱执行账号已失效", code=40300)
        _require_sandbox_execution(auth_db, actor, project_id)


def _can_preview_environment(db: Session, actor: User | None, environment: SandboxEnvironment) -> bool:
    if actor is None:
        return False
    try:
        _require_sandbox_execution(db, actor, environment.project_id)
    except AppError:
        return False
    return True


def _can_stop_environment(db: Session, actor: User | None, environment: SandboxEnvironment) -> bool:
    if actor is None or not _can_manage(db, actor, environment):
        return False
    try:
        require_project_access(db, environment.project_id, actor, need_write=False)
    except AppError:
        return False
    return True


def _execution_model_guard(db: Session, environment: SandboxEnvironment):
    token = str(environment.execution_token or "")
    return lambda: _require_execution_lease(db, environment.id, token)


def _commit_execution(db: Session, environment_id: int, execution_token: str | None) -> None:
    """Validate the lease under a row lock and commit the current transaction."""
    if execution_token is not None:
        row = (
            db.query(SandboxEnvironment.execution_token)
            .filter(SandboxEnvironment.id == environment_id)
            .with_for_update()
            .first()
        )
        if row is None or str(row[0] or "") != execution_token:
            db.rollback()
            raise RuntimeError("沙箱执行租约已失效")
    db.commit()


def _persist_worker_execution_snapshot(
    db: Session,
    environment: SandboxEnvironment,
    *,
    execution_bytes: bytes,
    source_sha256: str,
    repair_round: int,
    request_envelope: dict[str, Any],
    request_config_json: str,
    execution_token: str | None,
) -> bool:
    """Persist the exact Worker input only while this executor owns the lease."""
    updated = (
        db.query(SandboxEnvironment)
        .filter(
            SandboxEnvironment.id == environment.id,
            *([SandboxEnvironment.execution_token == execution_token] if execution_token is not None else []),
        )
        .update(
            {
                "execution_archive_blob": execution_bytes,
                "execution_source_sha256": source_sha256,
                "execution_round": repair_round,
                "worker_request_json": _json(request_envelope),
                "agent_config_json": request_config_json,
                **({"started_at": environment.started_at} if environment.started_at is not None else {}),
            },
            synchronize_session=False,
        )
    )
    if not updated:
        db.rollback()
        db.expire_all()
        return False
    return True


def _enter_finalizing(
    db: Session,
    environment: SandboxEnvironment,
    *,
    result: dict[str, Any],
    target_status: str,
    execution_token: str | None,
) -> bool:
    """Move to finalizing atomically so a stale executor cannot steal a recovered run."""
    values: dict[str, Any] = {
        "status": "finalizing",
        "executor_ref": str(result.get("executor_ref") or result.get("request_id") or "")[:160] or None,
        "runtime": str(result.get("runtime") or environment.runtime)[:50],
        "image_ref": str(result.get("image_ref") or environment.image_ref)[:300],
        "image_digest": str(result.get("image_digest") or "")[:100] or None,
        "started_at": environment.started_at or _utcnow(),
    }
    if isinstance(result.get("resource_policy"), dict):
        values["resource_policy_json"] = _json(result["resource_policy"])
    if environment.purpose == "deploy" and target_status == "ready":
        values["preview_path"] = f"/api/sandboxes/{environment.public_id}/preview/"
    updated = (
        db.query(SandboxEnvironment)
        .filter(
            SandboxEnvironment.id == environment.id,
            SandboxEnvironment.status.notin_({"stopping", "stopped", "expired", "finalizing"}),
            *([SandboxEnvironment.execution_token == execution_token] if execution_token is not None else []),
        )
        .update(values, synchronize_session=False)
    )
    if not updated:
        db.rollback()
        db.expire_all()
        return False
    return True


def _persist_browser_artifact(
    db: Session,
    environment: SandboxEnvironment,
    *,
    artifact_type: str,
    file_name: str,
    mime_type: str,
    content: bytes,
    execution_token: str | None = None,
) -> SandboxArtifact:
    """追加浏览器证据；不得删除同一环境已有的白盒/黑盒制品。"""
    _require_execution_lease(db, environment.id, execution_token)

    row = SandboxArtifact(
        environment_id=environment.id,
        artifact_type=artifact_type,
        file_name=file_name,
        mime_type=mime_type,
        byte_size=len(content),
        sha256=hashlib.sha256(content).hexdigest(),
        storage_ref=f"database://sandbox-artifact/{environment.public_id}-{artifact_type}",
        content_base64=base64.b64encode(content).decode("ascii"),
    )
    db.add(row)
    db.flush()
    return row


def _run_auto_smoke_test(db: Session, environment: SandboxEnvironment) -> dict[str, Any]:
    """部署就绪后通过 Worker 预览通道做一次带外 HTTP 根路径冒烟测试。

    经 worker 预览通道从环境外部发起 GET,与人工预览访问同一路径,
    不调用 Agent、不触碰容器内源码,也不影响部署保活。该证据只说明根路径可达；
    任何失败只记录、不阻断预览部署，也不能替代业务用例和授权黑盒测试。
    """
    started = datetime.now(timezone.utc)
    worker = db.get(SandboxWorker, environment.worker_id) if environment.worker_id else None
    if worker is None:
        return {"available": False, "reason": "worker 不可用"}
    _require_execution_lease(db, environment.id, str(environment.execution_token or ""))
    try:
        status_code, _headers, content = _proxy_worker_preview(
            worker,
            environment.public_id,
            "/",
            "",
            "GET",
            {"Accept": "text/html,application/json,*/*"},
            b"",
        )
    except Exception as exc:  # noqa: BLE001
        return {"available": False, "reason": f"自动测试探测失败: {str(exc)[:300]}"}
    body = content[:4096]
    elapsed_ms = int((datetime.now(timezone.utc) - started).total_seconds() * 1000)
    passed = 200 <= status_code < 400
    artifact = _persist_browser_artifact(
        db,
        environment,
        artifact_type="auto_smoke_evidence",
        file_name=f"auto-smoke-{environment.public_id}.txt",
        mime_type="text/plain",
        content=body if body else b"(empty body)",
    )
    return {
        "available": True,
        "scope": "http_root_smoke",
        "method": "GET",
        "path": "/",
        "status_code": status_code,
        "latency_ms": elapsed_ms,
        "body_bytes": len(content),
        "body_preview": body.decode("utf-8", errors="replace")[:500],
        "passed": passed,
        "artifact_id": artifact.id,
    }


# deploy 后自动核验所用的内嵌 runner:作为 `_prism_verify.sh` 随源码注入,
# 用 deploy 镜像自带的解释器运行,不依赖项目镜像 runner.sh 的 test 分支(deploy 镜像通常不含)。
# 白盒执行可用的静态/编译/既有单测；黑盒仅探测源码候选路由的 HTTP 可达性，不生成或执行 AI 动态断言。
# 冒烟结果不得作为业务路径或授权渗透已经覆盖的证据。
_DEPLOY_VERIFY_RUNNER = r"""#!/bin/bash
set -u
# runner.sh 已把源码(含本脚本)拷到 /workspace 并 cd 进去,这里就地运行。
MODE="${1:-combined}"
LANG_="${PRISM_LANGUAGE:-python}"
PORT="${PRISM_PREVIEW_PORT:-8080}"
cd "${PRISM_WORKSPACE:-/workspace}" 2>/dev/null || true

# ── v3.5 多Agent测试: Recon 事实采集(零LLM,结构化facts供沙箱外Agent推理) ──
collect_facts() {
  if command -v python3 >/dev/null 2>&1; then
python3 - <<'PYEOF_INNER' 2>/dev/null || true
import json, os, re
facts = {"entrypoints": [], "test_files": {"found": 0, "framework": ""},
         "endpoints": [], "param_hints": [], "hardcoded_secrets": []}
entry_names = {"main.py", "app.py", "manage.py", "wsgi.py", "asgi.py", "index.js",
               "server.js", "app.js", "main.go", "go.mod", "pom.xml", "index.php"}
test_re = re.compile(r"(^test_.*\.py$|.*_test\.py$|.*\.test\.js$|.*_test\.go$|Test\.java$)")
route_re = re.compile(
    r"(?:route|get|post|put|delete|patch)\s*\(\s*['\"]([^'\"]+)['\"]|"
    r"@(?:app|bp|router)\.(?:route|get|post|put|delete|patch)\s*\(\s*['\"]([^'\"]+)['\"]|"
    r"path\s*\(\s*['\"]([^'\"]+)['\"]", re.I)
secret_re = re.compile(r"(api[_-]?key|secret|token|password|passwd)\s*[:=]\s*['\"]([^'\"]{6,})['\"]", re.I)
param_names = {"file", "path", "filename", "download", "url", "callback", "id", "userid",
               "orderid", "template", "export", "redirect", "next", "upload"}
endpoints, secrets, params, tests = [], [], set(), 0
framework = ""
for root, dirs, files in os.walk("."):
    dirs[:] = [d for d in dirs if d not in {".git", "node_modules", "__pycache__", "venv", ".venv", "vendor"}]
    for fn in files:
        p = os.path.join(root, fn)
        if fn in entry_names:
            facts["entrypoints"].append(p.lstrip("./"))
        if test_re.search(fn):
            tests += 1
            if fn.endswith(".py"): framework = framework or "pytest"
            if fn.endswith(".js"): framework = framework or "jest"
        if not fn.endswith((".py", ".js", ".ts", ".go", ".java", ".php")):
            continue
        try:
            with open(p, "r", errors="ignore") as fh:
                src = fh.read(200000)
        except OSError:
            continue
        for m in route_re.finditer(src):
            ep = m.group(1) or m.group(2) or m.group(3)
            if ep and ep.startswith("/") and len(endpoints) < 60:
                endpoints.append({"path": ep, "file": p.lstrip("./")})
        for m in secret_re.finditer(src):
            if len(secrets) < 20:
                secrets.append({"file": p.lstrip("./"), "kind": m.group(1)})
        for name in param_names:
            if re.search(r"[?&\"'\s]" + name + r"['\"=:\\s]", src, re.I):
                params.add(name)
facts["test_files"] = {"found": tests, "framework": framework}
facts["endpoints"] = endpoints
facts["hardcoded_secrets"] = secrets
facts["param_hints"] = sorted(params)
with open("/tmp/prism_facts.json", "w") as out:
    json.dump(facts, out, ensure_ascii=False)
print("facts: entries=%d endpoints=%d secrets=%d tests=%d" % (
    len(facts["entrypoints"]), len(endpoints), len(secrets), tests))
PYEOF_INNER
  elif command -v php >/dev/null 2>&1; then
    cat > /tmp/_facts.php <<'PHPF'
<?php
try {
$facts = array("entrypoints"=>array(), "test_files"=>array("found"=>0,"framework"=>""), "endpoints"=>array(), "param_hints"=>array(), "hardcoded_secrets"=>array());
$entry_names = array("main.py","app.py","manage.py","wsgi.py","asgi.py","index.js","server.js","app.js","main.go","go.mod","pom.xml","index.php","index.html");
$route_re = "/(?:route|get|post|put|delete|patch)\s*\(\s*['"]([^'"]+)['"]|@(?:app|bp|router)\.(?:route|get|post|put|delete|patch)\s*\(\s*['"]([^'"]+)['"]|path\s*\(\s*['"]([^'"]+)['"]/i";
$secret_re = "/(api[_-]?key|secret|token|password|passwd)\s*[:=]\s*['"]([^'"]{6,})['"]/i";
$param_names = array("file","path","filename","download","url","callback","id","userid","orderid","template","export","redirect","next","upload");
$tests = 0; $framework = ""; $endpoints = array(); $secrets = array(); $params = array();
$scanned = 0;
$it = new RecursiveIteratorIterator(new RecursiveDirectoryIterator("."));
foreach ($it as $f) {
  if ($f->isDir()) continue;
  if ($scanned++ > 3000) break;
  $name = $f->getFilename(); $rel = substr($f->getPathname(), 2);
  if (in_array($name, $entry_names)) $facts["entrypoints"][] = $rel;
  if (preg_match("/^(test_.*\.py$|.*_test\.py$|.*\.test\.js$|.*_test\.go$|Test\.java$)/", $name)) { $tests++; if (substr($name,-3)===".py") $framework = $framework ?: "pytest"; if (substr($name,-3)===".js") $framework = $framework ?: "jest"; }
  $ext = strtolower(pathinfo($name, PATHINFO_EXTENSION));
  if (!in_array($ext, array("py","js","ts","go","java","php"))) continue;
  $src = @file_get_contents($f->getPathname());
  if ($src === false) continue;
  $src = substr($src, 0, 200000);
  if (preg_match_all($route_re, $src, $mm)) {
    foreach (array_merge($mm[1], $mm[2], $mm[3]) as $ep) {
      if ($ep && $ep[0] === "/" && count($endpoints) < 60) $endpoints[] = array("path"=>$ep, "file"=>$rel);
    }
  }
  if (preg_match_all($secret_re, $src, $sm)) {
    foreach ($sm[1] as $k) { if (count($secrets) < 20) $secrets[] = array("file"=>$rel, "kind"=>$k); }
  }
  foreach ($param_names as $pn) {
    if (preg_match("/[?&'"\s]" . preg_quote($pn, "/") . "['"=:\s]/i", $src)) $params[$pn] = 1;
  }
}
$facts["test_files"] = array("found"=>$tests, "framework"=>$framework);
$facts["endpoints"] = $endpoints;
$facts["hardcoded_secrets"] = $secrets;
$facts["param_hints"] = array_keys($params);
echo "PRISM_FACTS_BEGIN\n" . json_encode($facts, JSON_INVALID_UTF8_SUBSTITUTE | JSON_PARTIAL_OUTPUT_ON_ERROR) . "\nPRISM_FACTS_END\n";
} catch (Throwable $e) { echo "PRISM_FACTS_BEGIN\n{\"error\":\"" . addslashes($e->getMessage()) . "\"}\nPRISM_FACTS_END\n"; }
PHPF
    php /tmp/_facts.php 2>/dev/null || true
  fi
}

emit_facts() {  # 把 facts 打到日志,后端经 docker logs 回收
  if [ -f /tmp/prism_facts.json ]; then
    echo "PRISM_FACTS_BEGIN"
    cat /tmp/prism_facts.json
    echo ""
    echo "PRISM_FACTS_END"
  fi
}

run_whitebox() {
  case "$LANG_" in
    python)
      python -m compileall -q . || return 1
      if find . -type f \( -name 'test_*.py' -o -name '*_test.py' \) -print -quit | grep -q .; then
        if python -c 'import pytest' >/dev/null 2>&1; then python -m pytest -q --disable-warnings --maxfail=50 || return 1
        else python -m unittest discover -v || return 1; fi
      fi
      ;;
    node)
      find . -type f -name '*.js' -not -path './node_modules/*' -exec node --check '{}' ';' || return 1
      ;;
    java)
      find . -type f -name '*.java' -print > /tmp/javasrc 2>/dev/null
      [ -s /tmp/javasrc ] && { mkdir -p .prism-classes; javac -d .prism-classes @/tmp/javasrc || return 1; }
      ;;
    go)
      command -v go >/dev/null 2>&1 || { echo "go vet: Go toolchain unavailable"; return 1; }
      go vet ./... >/dev/null 2>&1 || { echo "go vet: static analysis failed"; return 1; }
      ;;
    php)
      # 逐文件起进程在大项目上必超时(3400+ 文件 × 进程开销 > profile 上限);
      # php -l 对致命解析错误退出码恒为 0,必须靠输出捕获。分批:外层 sh -c 提供
      # 参数基址(_ 为 $0,文件从 $1 起),每批 50 个文件一次进程。
      # 判定:仅 Fatal/Parse error 算失败(PHP8 对老库大量 Deprecated/Warning 是
      # 提示级,`php -l` 对其退出码为 0,不该判白盒失败);Deprecated/Warning 仍输出供审查。
      find . -type f -name '*.php' -print0 | xargs -0 -n 50 -r php -l 2>&1 \
        | grep -v 'No syntax errors detected' > /tmp/.lint_all || true
      grep -E 'Fatal error|Parse error|Errors parsing' /tmp/.lint_all > /tmp/.lint_fatal || true
      if [ -s /tmp/.lint_fatal ]; then
        cat /tmp/.lint_fatal
        return 1
      fi
      cat /tmp/.lint_all
      ;;
  esac
  return 0
}

# 与 deploy/sandbox/runner.sh 的 php_document_root 保持一致:入口在顶层子目录时仅下探唯一候选。
php_doc_root() {
  # 优先:当前目录直接有入口
  if [ -f ./index.php ] || [ -f ./index.html ]; then
    printf '%s
' .
    return 0
  fi
  # 其次:public 子目录有入口(且当前目录无入口)
  if [ -d public ] && { [ -f public/index.php ] || [ -f public/index.html ]; }; then
    printf '%s
' public
    return 0
  fi
  # 嵌套包(zip 多套一层目录)递归下探唯一候选
  nested_root=""
  nested_count=0
  for directory in */; do
    [ -d "$directory" ] || continue
    case "$directory" in .*|prism-tmp/*|prism-home/*|prism-cache/*) continue ;; esac
    nested_candidate=""
    if [ -f "${directory}index.php" ] || [ -f "${directory}index.html" ]; then
      nested_candidate="${directory%/}"
    elif [ -f "${directory}public/index.php" ] || [ -f "${directory}public/index.html" ]; then
      nested_candidate="${directory%/}/public"
    fi
    [ -n "$nested_candidate" ] || continue
    nested_root="$nested_candidate"
    nested_count=$((nested_count + 1))
  done
  if [ "$nested_count" -eq 1 ]; then
    printf '%s
' "$nested_root"
    return 0
  fi
  printf '%s
' .
}

start_app() {
  : > /tmp/prism-app.log
  APP_PID=""
  # 将应用及 npm/shell 后代放入独立作业进程组，超时/退出时可统一回收。
  set -m
  is_asgi_application() {
    [ -f "$1" ] || return 1
    python -c 'import ast,sys
tree=ast.parse(open(sys.argv[1], encoding="utf-8").read())
raise SystemExit(0 if any(
  isinstance(node, ast.Call)
  and (getattr(node.func, "id", "") if isinstance(node.func, ast.Name) else getattr(node.func, "attr", "")) in {"FastAPI", "Starlette"}
  for node in ast.walk(tree)
) else 1)' "$1"
  }
  case "$LANG_" in
    python)
      if [ -f manage.py ] && python -c 'import django' >/dev/null 2>&1; then
        python manage.py runserver "127.0.0.1:$PORT" --noreload >/tmp/prism-app.log 2>&1 & APP_PID=$!
      elif [ -f asgi.py ] && python -c 'import uvicorn' >/dev/null 2>&1; then
        python -m uvicorn asgi:application --host 127.0.0.1 --port "$PORT" >/tmp/prism-app.log 2>&1 & APP_PID=$!
      elif [ -f main.py ] && python -c 'import uvicorn' >/dev/null 2>&1 && is_asgi_application main.py; then
        python -m uvicorn main:app --host 127.0.0.1 --port "$PORT" >/tmp/prism-app.log 2>&1 & APP_PID=$!
      elif [ -f app.py ] && python -c 'import uvicorn' >/dev/null 2>&1 && is_asgi_application app.py; then
        python -m uvicorn app:app --host 127.0.0.1 --port "$PORT" >/tmp/prism-app.log 2>&1 & APP_PID=$!
      elif [ -f wsgi.py ]; then
        python - "$PORT" <<'PYWSGI' >/tmp/prism-app.log 2>&1 &
import sys
from wsgiref.simple_server import make_server
namespace = {}
exec(compile(open("wsgi.py", encoding="utf-8").read(), "wsgi.py", "exec"), namespace)
application = namespace.get("application") or namespace.get("app")
if not callable(application):
    raise SystemExit("wsgi.py must define callable application or app")
make_server("127.0.0.1", int(sys.argv[1]), application).serve_forever()
PYWSGI
        APP_PID=$!
      elif [ -f app.py ] && python -c 'import flask' >/dev/null 2>&1; then
        python -m flask --app app run --host 127.0.0.1 --port "$PORT" >/tmp/prism-app.log 2>&1 & APP_PID=$!
      elif [ -f main.py ]; then
        python main.py >/tmp/prism-app.log 2>&1 & APP_PID=$!
      elif [ -f app.py ]; then
        python app.py >/tmp/prism-app.log 2>&1 & APP_PID=$!
      else set +m; return 1; fi
      ;;
    node)
      node_entry=""
      if [ -f package.json ]; then
        if node -e 'process.exit(require("./package.json").scripts?.start ? 0 : 1)' 2>/dev/null; then
          npm start >/tmp/prism-app.log 2>&1 & APP_PID=$!
          set +m
          return 0
        fi
        node_entry="$(node -e 'try { process.stdout.write(require("./package.json").main || "") } catch (_) {}' 2>/dev/null || true)"
      fi
      for entry in "$node_entry" server.js server.mjs index.js index.mjs app.js main.js; do
        [ -n "$entry" ] || continue
        case "$entry" in /*|../*|*/../*) continue ;; esac
        if [ -f "$entry" ]; then
          node "$entry" >/tmp/prism-app.log 2>&1 & APP_PID=$!
          break
        fi
      done
      if [ -z "$APP_PID" ]; then set +m; return 1; fi
      ;;
    java)   JAR=$(find . -type f -name '*.jar' -not -name '*-sources.jar' -print -quit); if [ -z "$JAR" ]; then set +m; return 1; fi; java -Dserver.address=127.0.0.1 -Dserver.port="$PORT" -jar "$JAR" >/tmp/prism-app.log 2>&1 & APP_PID=$! ;;
    go)     go run . >/tmp/prism-app.log 2>&1 & APP_PID=$! ;;
    php)    ROOT=$(php_doc_root); php -S "127.0.0.1:$PORT" -t "$ROOT" >/tmp/prism-app.log 2>&1 & APP_PID=$! ;;
  esac
  set +m
  [ -n "$APP_PID" ]
}

stop_app() {
  [ -n "${APP_PID:-}" ] || return 0
  kill -TERM "-$APP_PID" >/dev/null 2>&1 || true
  sleep 1
  kill -KILL "-$APP_PID" >/dev/null 2>&1 || true
  wait "$APP_PID" >/dev/null 2>&1 || true
  APP_PID=""
}

http_probe() {
  url_path="$1"
  if command -v python >/dev/null 2>&1; then
    python -c "import urllib.request,sys
try:
  r=urllib.request.urlopen('http://127.0.0.1:$PORT'+sys.argv[1],timeout=3); print(r.status)
except Exception as e:
  print(getattr(e,'code',0) or 0)" "$url_path" 2>/dev/null || echo 0
  else
    # 无 python(PHP 沙箱):用 bash /dev/tcp 探测,与 run_blackbox 探活一致
    bash -c 'port="$1"; path="$2"; exec 3<>"/dev/tcp/127.0.0.1/$port"; printf "GET %s HTTP/1.0\r\nHost: localhost\r\nConnection: close\r\n\r\n" "$path" >&3; IFS= read -r line <&3; case "$line" in HTTP/*\ [1-5][0-9][0-9]\ *) code="${line#HTTP/* }"; printf "%s" "${code%% *}" ;; *) printf "0" ;; esac' prism-probe "$PORT" "$url_path" 2>/dev/null || echo 0
  fi
}

discover_probe_routes() {
  route_prefix='(route|get|post|put|delete|patch|path|HandleFunc|Path|GetMapping|PostMapping|PutMapping|DeleteMapping|PatchMapping|RequestMapping)[[:space:]]*\([[:space:]]*'
  double_pattern="${route_prefix}\"[^\"]+\""
  single_pattern="${route_prefix}'[^']+'"
  {
    find . -type f \( -name '*.py' -o -name '*.js' -o -name '*.mjs' -o -name '*.ts' -o -name '*.go' -o -name '*.java' -o -name '*.php' \) \
      -not -path './.git/*' -not -path './node_modules/*' -not -path './vendor/*' -not -path './_agent_tests/*' -print0 \
      | xargs -0 grep -hiEo "$double_pattern" 2>/dev/null \
      | sed -nE "s/.*\([[:space:]]*['\"]([^'\"]+)['\"].*/\1/p" || true
    find . -type f \( -name '*.py' -o -name '*.js' -o -name '*.mjs' -o -name '*.ts' -o -name '*.go' -o -name '*.java' -o -name '*.php' \) \
      -not -path './.git/*' -not -path './node_modules/*' -not -path './vendor/*' -not -path './_agent_tests/*' -print0 \
      | xargs -0 grep -hiEo "$single_pattern" 2>/dev/null \
      | sed -nE "s/.*\([[:space:]]*['\"]([^'\"]+)['\"].*/\1/p" || true
    printf '%s\n' / /health /healthz /api/health /api/healthz /api /openapi.json /docs /login
  } | awk '{if (substr($0, 1, 1) != "/") $0 = "/" $0; if (!seen[$0]++) print $0}' | head -n 80
}

run_blackbox() {
  start_app || { echo "blackbox: 无法启动应用"; return 1; }
  trap 'stop_app; exit 130' INT
  trap 'stop_app; exit 143' TERM
  trap 'stop_app' EXIT
  sleep 1
  i=0; READY=0
  while [ $i -lt 30 ]; do
    S=$(http_probe "/")
    case "$S" in 1*|2*|3*|4*|5*) READY=1; break;; esac  # 存活探测只证明端口有响应，不代表测试通过
    kill -0 "$APP_PID" 2>/dev/null || break
    i=$((i+1)); sleep 1
  done
  if [ "$READY" != "1" ]; then echo "blackbox: 应用未在回环端口就绪"; return 1; fi
  blackbox_route=""
  blackbox_status="0"
  route_passed=false
  failure_route="/"
  failure_status="0"
  while IFS= read -r p; do
    [ -n "$p" ] || continue
    printf '%s\n' "$p" | grep -Eq '^/[A-Za-z0-9._~!$&+,;=@%/?-]+$' || continue
    probe_status="$(http_probe "$p")"
    printf 'blackbox probe %s -> %s\n' "$p" "$probe_status"
    case "$probe_status" in
      2*) blackbox_route="$p"; blackbox_status="$probe_status"; route_passed=true; break ;;
      3*) if [ "$failure_status" -eq 0 ]; then failure_route="$p"; failure_status="$probe_status"; fi ;;
      5*) failure_route="$p"; failure_status="$probe_status" ;;
      4*) if [ "$failure_status" -eq 0 ]; then failure_route="$p"; failure_status="$probe_status"; fi ;;
    esac
  done <<EOFROUTES
$(discover_probe_routes)
EOFROUTES
  if [ -z "$blackbox_route" ]; then
    echo 'blackbox: no discovered application route returned HTTP 2xx'
    blackbox_route="$failure_route"
    blackbox_status="$failure_status"
  fi
  stop_app
  trap - EXIT INT TERM
  if [ "$route_passed" = true ]; then
    printf 'PRISM_BLACKBOX_DONE {"executed":true,"passed":true,"basis":"route_smoke","route_passed":true,"route":"%s","status_code":%s}\n' "$blackbox_route" "$blackbox_status"
  else
    printf 'PRISM_BLACKBOX_DONE {"executed":true,"passed":false,"basis":"route_smoke","route_passed":false,"route":"%s","status_code":%s}\n' "$failure_route" "$failure_status"
    return 1
  fi
  echo "blackbox: route $blackbox_route returned HTTP $blackbox_status"
  return 0
}

case "$MODE" in
  whitebox)
    collect_facts
    run_whitebox; WB=$?
    emit_facts
    [ $WB -eq 0 ] && { echo "PRISM_VERIFY whitebox ok"; exit 0; } || { echo "PRISM_VERIFY whitebox fail"; exit 1; }
    ;;
  blackbox)
    collect_facts
    run_blackbox; BB=$?
    emit_facts
    [ $BB -eq 0 ] && { echo "PRISM_VERIFY blackbox ok"; exit 0; } || { echo "PRISM_VERIFY blackbox fail"; exit 1; }
    ;;
  combined)
    collect_facts
    run_whitebox; WHITEBOX_OK=$?
    run_blackbox; BB=$?
    emit_facts
    [ $WHITEBOX_OK -eq 0 ] && [ $BB -eq 0 ] && { echo "PRISM_VERIFY combined ok"; exit 0; } || { echo "PRISM_VERIFY combined fail"; exit 1; }
    ;;
  *) echo "unknown mode"; exit 64 ;;
esac
"""


def _run_deploy_auto_tests(
    db: Session,
    environment: SandboxEnvironment,
    worker: SandboxWorker,
    source_archive_base64: str,
    modes: tuple = ("whitebox", "blackbox"),
) -> list[dict[str, Any]]:
    """部署就绪后自动执行白盒检查与黑盒路由冒烟。

    复用同一 worker 与不可变源码快照,注入内嵌 `_prism_verify.sh` 作为 deploy 专用
    runner,每次起一次性测试容器跑完即回收,与常驻 deploy 预览互不影响。
    黑盒部分只探测候选路由可达性，不等同于 AI 动态业务断言；失败只记录。
    """
    language = environment.language
    results: list[dict[str, Any]] = []
    zip_bytes = base64.b64decode(source_archive_base64)
    buf = io.BytesIO(zip_bytes)
    with zipfile.ZipFile(buf, "a", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("_prism_verify.sh", _DEPLOY_VERIFY_RUNNER)
    augmented = base64.b64encode(buf.getvalue()).decode("ascii")
    sha = hashlib.sha256(buf.getvalue()).hexdigest()
    ttl = max(120, int((environment.expires_at - _utcnow()).total_seconds()))
    for mode in modes:
        _require_execution_lease(db, environment.id, str(environment.execution_token or ""))
        request_id = f"{environment.public_id}-verify-{mode}"
        _register_worker_request(environment, request_id)
        db.commit()
        db.refresh(environment)
        if environment.status in {"stopping", "stopped", "expired"}:
            try:
                _stop_registered_worker_requests(worker, environment)
                if environment.status == "stopping":
                    environment.status = "stopped"
                    environment.stopped_at = _utcnow()
            except Exception as exc:  # noqa: BLE001 - cancellation remains nonterminal until cleanup succeeds
                environment.status = "stopping"
                environment.error = f"停止部署验证 worker 失败：{str(exc)[:1000]}"
            db.commit()
            results.append({"mode": mode, "passed": False, "status": environment.status})
            return results
        try:
            _require_execution_lease(db, environment.id, str(environment.execution_token or ""))
            response = _call_worker(
                worker,
                "POST",
                "/execute",
                {
                    "request_id": request_id,
                    "purpose": "test",
                    "language": language,
                    "test_mode": mode,
                    "source_archive_base64": augmented,
                    "source_sha256": sha,
                    "ttl_seconds": ttl,
                    "image_digest": environment.image_digest or "",
                },
            )
            result = response.get("result") if isinstance(response.get("result"), dict) else response
            _validate_worker_execution_receipt(
                result,
                request_id=request_id,
                source_sha256=sha,
            )
            last_seq = 0
            deadline = time.monotonic() + 300
            while not _worker_status_is_terminal("test", str(result.get("status") or "")):
                if time.monotonic() >= deadline:
                    raise RuntimeError("自动测试轮询超时")
                time.sleep(1)
                _require_execution_lease(db, environment.id, str(environment.execution_token or ""))
                status_response = _call_worker(
                    worker, "POST", "/status", {"request_id": request_id, "after_sequence": last_seq}
                )
                result = (
                    status_response.get("result")
                    if isinstance(status_response.get("result"), dict)
                    else status_response
                )
                _validate_worker_execution_receipt(
                    result,
                    request_id=request_id,
                    source_sha256=sha,
                )
                last_seq = int(result.get("last_sequence") or last_seq)
            conclusion = result.get("result") if isinstance(result.get("result"), dict) else result
            exit_code = conclusion.get("exit_code") if isinstance(conclusion, dict) else None
            logs = conclusion.get("logs") if isinstance(conclusion, dict) else {}
            log_text = str((logs or {}).get("text") or "")
            passed = (
                str(result.get("status")) in {"succeeded", "completed"}
                and type(exit_code) is int
                and exit_code == 0
            )
            results.append({"mode": mode, "passed": passed, "exit_code": exit_code, "log": log_text[-1500:]})
            # 提取 Recon 结构化事实(PRISM_FACTS_BEGIN/END 包裹),供多Agent审查使用
            facts = _extract_prism_facts(log_text)
            if facts:
                results[-1]["facts"] = facts
                _persist_browser_artifact(
                    db,
                    environment,
                    artifact_type="recon_facts",
                    file_name=f"recon-facts-{mode}-{environment.public_id}.json",
                    mime_type="application/json",
                    content=json.dumps(facts, ensure_ascii=False).encode("utf-8"),
                )
            _append_event(
                db,
                environment,
                "complete" if passed else "progress",
                f"auto_{mode}",
                (
                    f"部署后自动白盒测试{'通过' if passed else '未通过'}"
                    if mode == "whitebox"
                    else f"部署后自动黑盒测试{'通过' if passed else '未通过'}"
                ),
                {"mode": mode, "passed": passed, "exit_code": exit_code,
                 "scope": "route_smoke" if mode == "blackbox" else "static_and_existing_tests"},
            )
            _persist_browser_artifact(
                db,
                environment,
                artifact_type=f"auto_{mode}_log",
                file_name=f"auto-{mode}-{environment.public_id}.log",
                mime_type="text/plain",
                content=log_text.encode("utf-8", errors="replace")[:65536] or b"(no log)",
            )
            db.commit()
        except Exception as exc:  # noqa: BLE001 - only continue after the test request is reclaimed
            results.append({"mode": mode, "passed": False, "error": str(exc)[:300]})
            try:
                _stop_worker_requests(worker, [request_id])
            except Exception as cleanup_exc:  # noqa: BLE001 - fail closed while a test may still run
                environment.status = "stopping"
                environment.error = f"部署验证异常且 Worker 回收待重试：{str(cleanup_exc)[:1000]}"
                _append_event(db, environment, "failed", f"auto_{mode}", environment.error[:420])
                db.commit()
                return results
            _append_event(
                db,
                environment,
                "progress",
                f"auto_{mode}",
                f"部署后自动{mode}测试异常，已确认回收: {str(exc)[:120]}",
            )
            db.commit()
            if isinstance(exc, AppError):
                raise
    return results


def _extract_prism_facts(log_text: str) -> dict[str, Any] | None:
    """从容器日志提取 PRISM_FACTS_BEGIN/END 包裹的 Recon 结构化事实。

    docker log 会给每行加时间戳前缀(2026-...Z ),因此按行解析:
    BEGIN 行之后的 JSON 行(去时间戳)到 END 行为止。
    """
    lines = (log_text or "").splitlines()
    begin = next((i for i, line in enumerate(lines) if "PRISM_FACTS_BEGIN" in line), None)
    if begin is None:
        return None
    end = next((i for i, line in enumerate(lines) if "PRISM_FACTS_END" in line and i > begin), None)
    if end is None:
        return None
    payload_lines: list[str] = []
    for line in lines[begin + 1 : end]:
        cleaned = re.sub(r"^\S+Z\s*", "", line)  # 去掉 docker 时间戳前缀
        if cleaned.strip():
            payload_lines.append(cleaned)
    if not payload_lines:
        return None
    try:
        return json.loads("".join(payload_lines))
    except (ValueError, TypeError):
        return None


def _source_summary_for_agent_tests(source_archive_base64: str, language: str) -> dict[str, Any]:
    """从源码 zip 提取可逐片压缩的完整文本证据与文件清单。"""
    from app.agents.source_context import MAX_SOURCE_CHUNKS

    def incomplete(reason: str) -> dict[str, Any]:
        return {
            "language": language,
            "files": [],
            "entries": [],
            "source_chunks": [],
            "coverage_complete": False,
            "coverage_error": reason,
        }

    try:
        raw = base64.b64decode(source_archive_base64, validate=True)
    except (binascii.Error, ValueError):
        return incomplete("源码归档 Base64 无效")
    file_names: list[str] = []
    manifest: list[dict[str, Any]] = []
    chunks: list[dict[str, Any]] = []
    snippets: dict[str, str] = {}
    coverage_error = ""
    source_text_file_count = 0
    source_binary_file_count = 0
    source_text_bytes = 0
    binary_suffixes = (
        ".png",
        ".jpg",
        ".jpeg",
        ".gif",
        ".webp",
        ".ico",
        ".pdf",
        ".zip",
        ".jar",
        ".class",
        ".pyc",
        ".so",
        ".dylib",
        ".exe",
        ".woff",
        ".woff2",
        ".ttf",
        ".otf",
        ".mp3",
        ".mp4",
        ".sqlite",
        ".db",
    )
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as zf:
            for info in zf.infolist():
                name = info.filename
                if name.startswith("_prism") or name.startswith("__MACOSX") or name.endswith("/"):
                    continue
                file_names.append(name)
                manifest.append({"path": name, "size": info.file_size, "crc": info.CRC})
            file_name_set = set(file_names)
            if len(file_name_set) != len(file_names):
                coverage_error = "源码 ZIP 存在重复路径，无法确定唯一文件内容"
            # 清单也分片：后续压缩模型会看到每个路径，不再假设前 300 项代表全局。
            manifest_parts: list[list[dict[str, Any]]] = []
            current_part: list[dict[str, Any]] = []
            for item in manifest:
                candidate = [*current_part, item]
                if current_part and len(json.dumps(candidate, ensure_ascii=False)) > 5_000:
                    manifest_parts.append(current_part)
                    current_part = [item]
                else:
                    current_part = candidate
            if current_part:
                manifest_parts.append(current_part)
            for part_number, part in enumerate(manifest_parts, start=1):
                text = json.dumps(part, ensure_ascii=False, separators=(",", ":"))
                if len(text) > 5_000:
                    coverage_error = "源码文件名过长，无法在清单分片中完整展示"
                    break
                digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
                source_id = f"manifest-{part_number:03d}-{digest[:12]}"
                chunks.append(
                    {
                        "source_id": source_id,
                        "path": "<archive-manifest>",
                        "text": text,
                        "sha256": digest,
                    }
                )
            if len(chunks) > MAX_SOURCE_CHUNKS:
                coverage_error = f"文件清单超过资源保护上限 {MAX_SOURCE_CHUNKS} 个完整源码分片"
            packed_files: list[dict[str, str]] = []

            def flush_packed_files() -> None:
                if not packed_files:
                    return
                # Keep file bodies verbatim inside the source text. JSON-encoding
                # the whole bundle escapes quotes and backslashes, so an exact
                # source quote from the model can never match the text checked by
                # source_context. Paths are metadata; code remains lossless.
                packed_source_files = [dict(item) for item in packed_files]
                text = "\n".join(
                    f"[FILE PATH JSON] {json.dumps(item['path'], ensure_ascii=False)}\n"
                    f"[SOURCE TEXT]\n{item['text']}"
                    for item in packed_source_files
                )
                digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
                chunks.append(
                    {
                        "source_id": f"files-{len(chunks) + 1:03d}-{digest[:12]}",
                        "path": "<multiple-files>",
                        "files": packed_source_files,
                        "text": text,
                        "sha256": digest,
                    }
                )
                packed_files.clear()

            for info in zf.infolist():
                if coverage_error:
                    break
                name = info.filename
                if name not in file_name_set:
                    continue
                if name.lower().endswith(binary_suffixes):
                    source_binary_file_count += 1
                    continue  # 非代码资产的路径/大小/CRC 已完整进入清单。
                if info.file_size > 400_000:
                    coverage_error = f"源码文件 {name} 超过单文件 400000 字节上限"
                    break
                content = zf.read(name)
                if b"\x00" in content:
                    source_binary_file_count += 1
                    continue  # 二进制资产只参与完整清单，不作为代码输入。
                try:
                    decoded = content.decode("utf-8")
                except UnicodeDecodeError:
                    coverage_error = f"源码文件 {name} 无法按 UTF-8 完整解码"
                    break
                if not decoded:
                    continue
                source_text_file_count += 1
                source_text_bytes += len(content)
                if len(snippets) < 12:
                    snippets[name] = decoded[:8_000]
                if len(decoded) <= 3_000:
                    record = {"path": name, "text": decoded}
                    if packed_files and len(json.dumps([*packed_files, record], ensure_ascii=False)) > 5_000:
                        flush_packed_files()
                    if len(json.dumps([record], ensure_ascii=False)) <= 5_000:
                        packed_files.append(record)
                        if len(chunks) > MAX_SOURCE_CHUNKS:
                            coverage_error = f"源码超过资源保护上限 {MAX_SOURCE_CHUNKS} 个完整源码分片"
                            break
                        continue
                flush_packed_files()
                for offset in range(0, len(decoded), 5_000):
                    text = decoded[offset : offset + 5_000]
                    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
                    source_id = f"file-{len(chunks) + 1:03d}-{digest[:12]}"
                    chunks.append(
                        {
                            "source_id": source_id,
                            "path": name,
                            "offset": offset,
                            "text": text,
                            "sha256": digest,
                        }
                    )
                    if len(chunks) > MAX_SOURCE_CHUNKS:
                        coverage_error = f"源码超过资源保护上限 {MAX_SOURCE_CHUNKS} 个完整源码分片"
                        break
                if coverage_error:
                    break
            if not coverage_error:
                flush_packed_files()
    except (zipfile.BadZipFile, OSError):
        return incomplete("源码 ZIP 归档损坏")
    if not chunks:
        coverage_error = coverage_error or "源码归档没有可压缩的清单或文本"
    return {
        "language": language,
        "files": file_names,
        "entries": file_names,
        "snippets": snippets,
        "source_archive_sha256": hashlib.sha256(raw).hexdigest(),
        "source_manifest_sha256": hashlib.sha256(
            json.dumps(manifest, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest(),
        "source_file_count": len(file_names),
        "source_text_file_count": source_text_file_count,
        "source_binary_file_count": source_binary_file_count,
        "source_text_bytes": source_text_bytes,
        "source_chunk_count": len(chunks),
        "source_chunks": chunks,
        "coverage_complete": not coverage_error,
        "coverage_error": coverage_error or None,
    }


def _inject_agent_test_files(source_archive_base64: str, files: list[dict[str, str]]) -> str:
    """把 agent 生成的测试文件注入源码 zip 的 _agent_tests/ 目录,返回新 zip。"""
    raw = base64.b64decode(source_archive_base64)
    buf = io.BytesIO(raw)
    with zipfile.ZipFile(buf, "a", zipfile.ZIP_DEFLATED) as zf:
        for item in files:
            path = str(item.get("path") or "").strip()
            content = str(item.get("content") or "")
            if not path or not content:
                continue
            zf.writestr(f"_agent_tests/{path}", content)
    return base64.b64encode(buf.getvalue()).decode("ascii")


def _agent_test_paths(source_archive_base64: str) -> set[str]:
    """从已持久化的执行归档恢复 Agent 测试文件清单。"""
    try:
        with zipfile.ZipFile(io.BytesIO(base64.b64decode(source_archive_base64))) as archive:
            return {
                name.removeprefix("_agent_tests/")
                for name in archive.namelist()
                if name.startswith("_agent_tests/") and not name.endswith("/")
            }
    except (ValueError, zipfile.BadZipFile):
        return set()


def _generated_test_contract_issues(files: list[dict[str, str]], language: str) -> list[str]:
    """Reject generated tests that can fail before exercising the project."""

    issues: list[str] = []
    for item in files:
        path = str(item.get("path") or "")
        content = str(item.get("content") or "")
        tree: ast.AST | None = None
        if language == "python":
            try:
                tree = ast.parse(content, filename=path or "<generated-test>")
            except SyntaxError as exc:
                issues.append(f"{path or '未命名文件'} Python 语法无效: 第 {exc.lineno or 0} 行")
                tree = None

        def dotted_name(node: ast.AST) -> str:
            parts: list[str] = []
            current: ast.AST = node
            while isinstance(current, ast.Attribute):
                parts.append(current.attr)
                current = current.value
            if isinstance(current, ast.Name):
                parts.append(current.id)
            return ".".join(reversed(parts))

        def call_name(node: ast.Call) -> str:
            return dotted_name(node.func)

        module_scope = tree
        parents: dict[ast.AST, ast.AST] = {}
        if tree is not None:
            for parent in ast.walk(tree):
                for child in ast.iter_child_nodes(parent):
                    parents[child] = parent

        def enclosing_scope(node: ast.AST) -> ast.AST | None:
            current: ast.AST | None = node
            while current is not None:
                if isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                    return current
                current = parents.get(current)
            return module_scope

        assignment_bindings: dict[tuple[ast.AST, str], list[tuple[int, int, ast.AST]]] = {}
        local_names: dict[ast.AST, set[str]] = {}
        parameter_sources: dict[tuple[ast.AST, str], list[ast.AST]] = {}
        function_calls: dict[ast.AST, list[ast.Call]] = {}
        trusted_encoder_calls: set[str] = set()
        trusted_request_calls: set[str] = set()
        trusted_urlopen_calls: set[str] = set()
        trusted_port_calls: set[str] = set()
        trusted_environ_names: set[str] = set()
        trusted_os_module_names: set[str] = set()
        trusted_operator_module_names: set[str] = set()
        trusted_builtin_module_names: set[str] = set()
        trusted_importlib_module_names: set[str] = set()
        trusted_sys_module_names: set[str] = set()
        trusted_functools_module_names: set[str] = set()
        trusted_import_functions: set[str] = {"__import__"}
        trusted_import_module_functions: set[str] = set()
        trusted_partial_calls: set[str] = set()
        trusted_partialmethod_calls: set[str] = set()
        trusted_dict_getitem_calls: set[str] = {"dict.__getitem__"}
        trusted_dict_update_calls: set[str] = {"dict.update"}
        trusted_getattr_calls: set[str] = {"getattr"}
        trusted_setitem_calls: set[str] = {"dict.__setitem__"}
        trusted_delitem_calls: set[str] = {"dict.__delitem__", "dict.pop"}
        trusted_methodcaller_calls: set[str] = set()
        dynamic_execution_names = {"exec", "eval", "compile"}

        def bind_name(name: str, statement: ast.AST, value: ast.AST) -> None:
            scope = enclosing_scope(statement)
            if scope is None:
                return
            local_names.setdefault(scope, set()).add(name)
            assignment_bindings.setdefault((scope, name), []).append(
                (int(getattr(statement, "lineno", 0) or 0), int(getattr(statement, "col_offset", 0) or 0), value)
            )

        if tree is not None:
            for import_node in (node for node in ast.walk(tree) if isinstance(node, (ast.Import, ast.ImportFrom))):
                if isinstance(import_node, ast.Import):
                    for alias in import_node.names:
                        bound = alias.asname or alias.name.split(".", 1)[0]
                        if alias.name == "urllib.parse":
                            prefix = bound if alias.asname else "urllib.parse"
                            trusted_encoder_calls.update(
                                {f"{prefix}.urlencode", f"{prefix}.quote", f"{prefix}.quote_plus"}
                            )
                        elif alias.name == "urllib.request":
                            prefix = bound if alias.asname else "urllib.request"
                            trusted_request_calls.add(f"{prefix}.Request")
                            trusted_urlopen_calls.add(f"{prefix}.urlopen")
                        elif alias.name == "urllib":
                            prefix = bound
                            trusted_encoder_calls.update(
                                {
                                    f"{prefix}.parse.urlencode",
                                    f"{prefix}.parse.quote",
                                    f"{prefix}.parse.quote_plus",
                                }
                            )
                            trusted_request_calls.add(f"{prefix}.request.Request")
                            trusted_urlopen_calls.add(f"{prefix}.request.urlopen")
                        elif alias.name == "os":
                            prefix = bound
                            trusted_os_module_names.add(prefix)
                            trusted_port_calls.update({f"{prefix}.getenv", f"{prefix}.environ.get"})
                            trusted_environ_names.update({f"{prefix}.environ", f"{prefix}.environb"})
                        elif alias.name == "builtins":
                            trusted_builtin_module_names.add(bound)
                            trusted_import_functions.add(f"{bound}.__import__")
                            trusted_getattr_calls.add(f"{bound}.getattr")
                        elif alias.name == "importlib":
                            trusted_importlib_module_names.add(bound)
                            trusted_import_module_functions.add(f"{bound}.import_module")
                        elif alias.name == "sys":
                            trusted_sys_module_names.add(bound)
                        elif alias.name == "functools":
                            trusted_functools_module_names.add(bound)
                            trusted_partial_calls.add(f"{bound}.partial")
                            trusted_partialmethod_calls.add(f"{bound}.partialmethod")
                        elif alias.name in {"operator", "_operator"}:
                            trusted_operator_module_names.add(bound)
                            trusted_setitem_calls.add(f"{bound}.setitem")
                            trusted_delitem_calls.add(f"{bound}.delitem")
                            trusted_methodcaller_calls.add(f"{bound}.methodcaller")
                elif import_node.module == "urllib.parse":
                    for alias in import_node.names:
                        if alias.name in {"urlencode", "quote", "quote_plus"}:
                            trusted_encoder_calls.add(alias.asname or alias.name)
                elif import_node.module == "urllib.request":
                    for alias in import_node.names:
                        bound = alias.asname or alias.name
                        if alias.name == "Request":
                            trusted_request_calls.add(bound)
                        elif alias.name == "urlopen":
                            trusted_urlopen_calls.add(bound)
                elif import_node.module == "urllib":
                    for alias in import_node.names:
                        if alias.name == "parse":
                            prefix = alias.asname or alias.name
                            trusted_encoder_calls.update(
                                {f"{prefix}.urlencode", f"{prefix}.quote", f"{prefix}.quote_plus"}
                            )
                        elif alias.name == "request":
                            prefix = alias.asname or alias.name
                            trusted_request_calls.add(f"{prefix}.Request")
                            trusted_urlopen_calls.add(f"{prefix}.urlopen")
                elif import_node.module == "os":
                    for alias in import_node.names:
                        bound = alias.asname or alias.name
                        if alias.name == "getenv":
                            trusted_port_calls.add(bound)
                        elif alias.name == "environ":
                            trusted_environ_names.add(bound)
                            trusted_port_calls.add(f"{bound}.get")
                        elif alias.name == "environb":
                            trusted_environ_names.add(bound)
                            trusted_port_calls.add(f"{bound}.get")
                elif import_node.module == "operator":
                    for alias in import_node.names:
                        if alias.name == "setitem":
                            trusted_setitem_calls.add(alias.asname or alias.name)
                        elif alias.name == "delitem":
                            trusted_delitem_calls.add(alias.asname or alias.name)
                        elif alias.name == "methodcaller":
                            trusted_methodcaller_calls.add(alias.asname or alias.name)
                elif import_node.module == "_operator":
                    for alias in import_node.names:
                        if alias.name == "methodcaller":
                            trusted_methodcaller_calls.add(alias.asname or alias.name)
                elif import_node.module == "builtins":
                    for alias in import_node.names:
                        if alias.name == "__import__":
                            trusted_import_functions.add(alias.asname or alias.name)
                        elif alias.name == "getattr":
                            trusted_getattr_calls.add(alias.asname or alias.name)
                        elif alias.name in dynamic_execution_names:
                            dynamic_execution_names.add(alias.asname or alias.name)
                elif import_node.module == "importlib":
                    for alias in import_node.names:
                        if alias.name == "import_module":
                            trusted_import_module_functions.add(alias.asname or alias.name)
                elif import_node.module == "functools":
                    for alias in import_node.names:
                        if alias.name == "partial":
                            trusted_partial_calls.add(alias.asname or alias.name)
                        elif alias.name == "partialmethod":
                            trusted_partialmethod_calls.add(alias.asname or alias.name)
            static_dynamic_name_cache: dict[ast.AST, str | None] = {}

            def static_dynamic_name(node: ast.AST) -> str | None:
                if node in static_dynamic_name_cache:
                    return static_dynamic_name_cache[node]
                result: str | None = None
                if isinstance(node, ast.Constant) and isinstance(node.value, str):
                    result = node.value
                elif isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
                    left = static_dynamic_name(node.left)
                    right = static_dynamic_name(node.right)
                    if left is not None and right is not None and len(left) + len(right) <= 64:
                        result = f"{left}{right}"
                elif isinstance(node, ast.JoinedStr):
                    parts = [static_dynamic_name(value) for value in node.values]
                    if all(value is not None for value in parts):
                        joined = "".join(value or "" for value in parts)
                        if len(joined) <= 64:
                            result = joined
                static_dynamic_name_cache[node] = result
                return result

            def builtin_namespace_reference(node: ast.AST, seen: set[str] | None = None) -> bool:
                visited = set(seen or ())
                if isinstance(node, ast.Name):
                    if node.id == "__builtins__" or node.id in trusted_builtin_module_names:
                        return True
                    if node.id in visited:
                        return False
                    visited.add(node.id)
                    for assignment in ast.walk(tree):
                        targets = (
                            assignment.targets
                            if isinstance(assignment, ast.Assign)
                            else [assignment.target]
                            if isinstance(assignment, ast.AnnAssign)
                            else []
                        )
                        value = getattr(assignment, "value", None)
                        if value is not None and any(
                            isinstance(target, ast.Name) and target.id == node.id for target in targets
                        ) and builtin_namespace_reference(value, visited):
                            return True
                    return False
                if isinstance(node, ast.Attribute) and node.attr == "__dict__":
                    return builtin_namespace_reference(node.value, visited)
                if isinstance(node, ast.Subscript):
                    return builtin_namespace_reference(node.value, visited)
                if isinstance(node, ast.Call):
                    name = call_name(node)
                    if name == "vars" and node.args:
                        return builtin_namespace_reference(node.args[0], visited)
                    if name in trusted_import_functions | trusted_import_module_functions and node.args:
                        return static_dynamic_name(node.args[0]) == "builtins"
                return False

            dynamic_execution_used = any(
                isinstance(node, ast.Name) and node.id in dynamic_execution_names
                or isinstance(node, ast.Attribute) and node.attr in dynamic_execution_names
                or isinstance(node, ast.Subscript)
                and builtin_namespace_reference(node.value)
                and (
                    not isinstance(node.slice, ast.Constant)
                    or static_dynamic_name(node.slice) in dynamic_execution_names
                )
                or isinstance(node, ast.Call)
                and call_name(node) in trusted_getattr_calls
                and len(node.args) >= 2
                and builtin_namespace_reference(node.args[0])
                and (
                    not isinstance(node.args[1], ast.Constant)
                    or static_dynamic_name(node.args[1]) in dynamic_execution_names
                )
                for node in ast.walk(tree)
            )
            dynamic_name_literal = any(
                static_dynamic_name(node) in dynamic_execution_names
                for node in ast.walk(tree)
            )
            dynamic_reflection_used = any(
                isinstance(node, ast.Attribute)
                and node.attr == "__dict__"
                and builtin_namespace_reference(node.value)
                or isinstance(node, ast.Call)
                and call_name(node) in {"vars", "globals", "locals"}
                or isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "get"
                and builtin_namespace_reference(node.func.value)
                for node in ast.walk(tree)
            )
            dynamic_execution_used = dynamic_execution_used or dynamic_name_literal and dynamic_reflection_used
            if dynamic_execution_used:
                issues.append(
                    f"{path or '未命名文件'} Python 动态测试不得使用 exec/eval/compile 或其反射别名，"
                    "以保证端口来源和请求目标可静态核验"
                )
            for node in ast.walk(tree):
                if isinstance(node, ast.Assign):
                    for target in node.targets:
                        if isinstance(target, ast.Name):
                            bind_name(target.id, node, node.value)
                elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.value is not None:
                    bind_name(node.target.id, node, node.value)
            for loop in (node for node in ast.walk(tree) if isinstance(node, ast.For)):
                if isinstance(loop.target, ast.Name):
                    bind_name(loop.target.id, loop, loop.iter)
            function_defs = {
                node.name: node for node in ast.walk(tree) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            }

            def expanded_keyword_entries(
                value: ast.AST,
                call: ast.Call,
                seen: set[tuple[int, str]] | None = None,
            ) -> list[tuple[ast.AST | None, ast.AST]]:
                """Resolve statically assigned dicts passed through ``**kwargs``."""
                visited = set(seen or ())
                if isinstance(value, ast.Dict):
                    entries: list[tuple[ast.AST | None, ast.AST]] = []
                    for key, item in zip(value.keys, value.values):
                        if key is None:
                            entries.extend(expanded_keyword_entries(item, call, visited))
                        else:
                            entries.append((key, item))
                    return entries
                if isinstance(value, ast.Name):
                    scope = enclosing_scope(call)
                    if scope is not None:
                        token = (id(scope), value.id)
                        if token not in visited:
                            position = (
                                int(getattr(call, "lineno", 1 << 30) or (1 << 30)),
                                int(getattr(call, "col_offset", 1 << 30) or (1 << 30)),
                            )
                            bindings = [
                                assignment_value
                                for line, column, assignment_value in assignment_bindings.get((scope, value.id), [])
                                if (line, column) < position
                            ]
                            if not bindings and scope is not module_scope and value.id not in local_names.get(scope, set()):
                                bindings = [
                                    assignment_value
                                    for line, column, assignment_value in assignment_bindings.get(
                                        (module_scope, value.id), []
                                    )
                                    if (line, column) < position
                                ]
                            if bindings:
                                return expanded_keyword_entries(bindings[-1], call, visited | {token})
                return [(None, value)]

            for function in function_defs.values():
                outer_scope = enclosing_scope(parents.get(function, tree))
                if outer_scope is not None:
                    local_names.setdefault(outer_scope, set()).add(function.name)
                    assignment_bindings.setdefault((outer_scope, function.name), []).append(
                        (
                            int(getattr(function, "lineno", 0) or 0),
                            int(getattr(function, "col_offset", 0) or 0),
                            function,
                        )
                    )
                positional = [*function.args.posonlyargs, *function.args.args]
                keyword_only = list(function.args.kwonlyargs)
                parameter_names = [argument.arg for argument in [*positional, *keyword_only]]
                if function.args.vararg is not None:
                    parameter_names.append(function.args.vararg.arg)
                if function.args.kwarg is not None:
                    parameter_names.append(function.args.kwarg.arg)
                local_names.setdefault(function, set()).update(parameter_names)

                calls = [
                    node
                    for node in ast.walk(tree)
                    if isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Name)
                    and node.func.id == function.name
                    and enclosing_scope(node) is not function
                ]
                function_calls[function] = calls
                for call in calls:
                    for argument, value in zip(positional, call.args):
                        parameter_sources.setdefault((function, argument.arg), []).append(value)
                    for keyword in call.keywords:
                        if keyword.arg in parameter_names:
                            parameter_sources.setdefault((function, keyword.arg), []).append(keyword.value)
                    if function.args.vararg is not None and len(call.args) > len(positional):
                        parameter_sources.setdefault((function, function.args.vararg.arg), []).append(
                            ast.Tuple(elts=list(call.args[len(positional) :]), ctx=ast.Load())
                        )
                    if function.args.kwarg is not None:
                        unpacked_keywords = [
                            entry
                            for keyword in call.keywords
                            if keyword.arg is None
                            for entry in expanded_keyword_entries(keyword.value, call)
                        ]
                        keyword_values = {
                            keyword.arg: keyword.value
                            for keyword in call.keywords
                            if keyword.arg is not None and keyword.arg not in parameter_names
                        }
                        if keyword_values or unpacked_keywords:
                            parameter_sources.setdefault((function, function.args.kwarg.arg), []).append(
                                ast.Dict(
                                    keys=[
                                        *[ast.Constant(value=name) for name in keyword_values],
                                        *[key for key, _item in unpacked_keywords],
                                    ],
                                    values=[*keyword_values.values(), *[item for _key, item in unpacked_keywords]],
                                )
                            )

            # Lambda aliases and functools.partialmethod do not appear as ordinary
            # named-function calls. Add their concrete call-site argument sources
            # so the security contract can follow environment and callback values
            # across those wrappers as well.
            lambda_aliases: dict[ast.Lambda, set[str]] = {}
            for node in ast.walk(tree):
                if isinstance(node, ast.Assign) and isinstance(node.value, ast.Lambda):
                    for target in node.targets:
                        if isinstance(target, ast.Name):
                            lambda_aliases.setdefault(node.value, set()).add(target.id)
                elif isinstance(node, ast.AnnAssign) and isinstance(node.value, ast.Lambda) and isinstance(node.target, ast.Name):
                    lambda_aliases.setdefault(node.value, set()).add(node.target.id)
            for lambda_node, aliases in lambda_aliases.items():
                lambda_arguments = [*lambda_node.args.posonlyargs, *lambda_node.args.args]
                calls = [
                    node
                    for node in ast.walk(tree)
                    if isinstance(node, ast.Call)
                    and (
                        node.func is lambda_node
                        or isinstance(node.func, ast.Name) and node.func.id in aliases
                    )
                ]
                for call in calls:
                    for argument, value in zip(lambda_arguments, call.args):
                        parameter_sources.setdefault((lambda_node, argument.arg), []).append(value)
                    for keyword in call.keywords:
                        if keyword.arg is not None and keyword.arg in {argument.arg for argument in lambda_arguments}:
                            parameter_sources.setdefault((lambda_node, keyword.arg), []).append(keyword.value)

            for lambda_node, aliases in lambda_aliases.items():
                for alias in aliases:
                    function_defs[alias] = lambda_node

            for class_node in (node for node in ast.walk(tree) if isinstance(node, ast.ClassDef)):
                methods = {
                    node.name: node
                    for node in class_node.body
                    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                }
                partial_methods: dict[str, tuple[ast.FunctionDef | ast.AsyncFunctionDef, list[ast.AST]]] = {}
                for statement in class_node.body:
                    if not isinstance(statement, (ast.Assign, ast.AnnAssign)):
                        continue
                    value = statement.value
                    if not isinstance(value, ast.Call) or call_name(value) not in trusted_partialmethod_calls:
                        continue
                    if not value.args or not isinstance(value.args[0], ast.Name):
                        continue
                    underlying = methods.get(value.args[0].id)
                    if underlying is None:
                        continue
                    targets = statement.targets if isinstance(statement, ast.Assign) else [statement.target]
                    for target in targets:
                        if isinstance(target, ast.Name):
                            partial_methods[target.id] = (underlying, list(value.args[1:]))
                for call in (node for node in ast.walk(tree) if isinstance(node, ast.Call)):
                    if not isinstance(call.func, ast.Attribute) or not isinstance(call.func.value, ast.Call):
                        continue
                    constructor = call.func.value
                    if not isinstance(constructor.func, ast.Name) or constructor.func.id != class_node.name:
                        continue
                    partial_method = partial_methods.get(call.func.attr)
                    if partial_method is None:
                        continue
                    underlying, bound_arguments = partial_method
                    positional = [*underlying.args.posonlyargs, *underlying.args.args]
                    callable_arguments = positional[1:] if positional else []
                    values = [*bound_arguments, *call.args]
                    for argument, value in zip(callable_arguments, values):
                        parameter_sources.setdefault((underlying, argument.arg), []).append(value)
                    for keyword in call.keywords:
                        if keyword.arg in {argument.arg for argument in callable_arguments}:
                            parameter_sources.setdefault((underlying, keyword.arg), []).append(keyword.value)
            for bindings in assignment_bindings.values():
                bindings.sort(key=lambda binding: (binding[0], binding[1]))

        def target_path(target: ast.AST) -> str:
            if isinstance(target, ast.Subscript):
                return dotted_name(target.value)
            return dotted_name(target)

        environ_aliases = set(trusted_environ_names)
        if tree is not None:
            # Track simple references to os.environ so writes through a local alias
            # cannot make a hard-coded port look like a trusted runtime value.
            alias_dependents: dict[str, set[str]] = {}

            def bind_alias_target(target: ast.AST, value: ast.AST) -> None:
                if isinstance(target, ast.Name):
                    source = dotted_name(value)
                    if source:
                        alias_dependents.setdefault(source, set()).add(target.id)
                elif isinstance(target, (ast.Tuple, ast.List)) and isinstance(value, (ast.Tuple, ast.List)):
                    for target_item, value_item in zip(target.elts, value.elts):
                        bind_alias_target(target_item, value_item)

            for node in ast.walk(tree):
                if isinstance(node, ast.Assign):
                    targets = node.targets
                    for target in targets:
                        bind_alias_target(target, node.value)
                elif isinstance(node, (ast.AnnAssign, ast.NamedExpr)) and node.value is not None:
                    bind_alias_target(node.target, node.value)

            def expand_alias_names(names: set[str]) -> None:
                pending = list(names)
                while pending:
                    source = pending.pop()
                    for alias in alias_dependents.get(source, set()) - names:
                        names.add(alias)
                        pending.append(alias)

            expand_alias_names(trusted_builtin_module_names)
            expand_alias_names(trusted_importlib_module_names)
            expand_alias_names(trusted_sys_module_names)
            expand_alias_names(trusted_functools_module_names)
            expand_alias_names(trusted_partial_calls)
            expand_alias_names(trusted_partialmethod_calls)
            expand_alias_names(trusted_dict_getitem_calls)
            expand_alias_names(trusted_dict_update_calls)
            expand_alias_names(trusted_setitem_calls)
            expand_alias_names(trusted_delitem_calls)
            expand_alias_names(trusted_import_functions)
            for module_name in trusted_builtin_module_names:
                trusted_import_functions.add(f"{module_name}.__import__")
                trusted_getattr_calls.add(f"{module_name}.getattr")
            for module_name in trusted_importlib_module_names:
                trusted_import_module_functions.add(f"{module_name}.import_module")
            for module_name in trusted_functools_module_names:
                trusted_partial_calls.add(f"{module_name}.partial")
                trusted_partialmethod_calls.add(f"{module_name}.partialmethod")
            expand_alias_names(trusted_import_functions)
            expand_alias_names(trusted_import_module_functions)
            expand_alias_names(trusted_partial_calls)
            expand_alias_names(trusted_partialmethod_calls)
            expand_alias_names(trusted_getattr_calls)
            # Preserve the operator module's trusted mutator paths across module
            # object aliases (operator_alias = operator), including alias chains.
            pending_operator_modules = list(trusted_operator_module_names)
            while pending_operator_modules:
                source = pending_operator_modules.pop()
                for alias in alias_dependents.get(source, set()) - trusted_operator_module_names:
                    trusted_operator_module_names.add(alias)
                    trusted_setitem_calls.add(f"{alias}.setitem")
                    trusted_delitem_calls.add(f"{alias}.delitem")
                    trusted_methodcaller_calls.add(f"{alias}.methodcaller")
                    pending_operator_modules.append(alias)
            expand_alias_names(trusted_setitem_calls)
            expand_alias_names(trusted_delitem_calls)
            expand_alias_names(trusted_dict_update_calls)
            # Track function-object aliases of operator.methodcaller as well as
            # module/import aliases. Otherwise an indirect mutator can rewrite
            # PRISM_PREVIEW_PORT without poisoning the trusted environment source.
            pending_methodcaller_aliases = list(trusted_methodcaller_calls)
            while pending_methodcaller_aliases:
                source = pending_methodcaller_aliases.pop()
                for alias in alias_dependents.get(source, set()) - trusted_methodcaller_calls:
                    trusted_methodcaller_calls.add(alias)
                    pending_methodcaller_aliases.append(alias)
            # Track module aliases too (``os_alias = os``), otherwise writes through
            # an ordinary assignment would evade the trusted-getenv/environ checks.
            pending_modules = list(trusted_os_module_names)
            while pending_modules:
                source = pending_modules.pop()
                for alias in alias_dependents.get(source, set()) - trusted_os_module_names:
                    trusted_os_module_names.add(alias)
                    trusted_port_calls.update({f"{alias}.getenv", f"{alias}.environ.get"})
                    trusted_environ_names.update({f"{alias}.environ", f"{alias}.environb"})
                    pending_modules.append(alias)

            environ_aliases.update(trusted_environ_names)
            pending_aliases = list(environ_aliases)
            while pending_aliases:
                source = pending_aliases.pop()
                aliases = {
                    alias
                    for value_path, dependents in alias_dependents.items()
                    if value_path == source or value_path.startswith(f"{source}.")
                    for alias in dependents
                }
                for alias in aliases - environ_aliases:
                    environ_aliases.add(alias)
                    pending_aliases.append(alias)

        os_module_dict_names = {f"{module_name}.__dict__" for module_name in trusted_os_module_names}
        os_module_dict_aliases = set(os_module_dict_names)
        if tree is not None:
            pending_dict_aliases = list(os_module_dict_aliases)
            while pending_dict_aliases:
                source = pending_dict_aliases.pop()
                for alias in alias_dependents.get(source, set()) - os_module_dict_aliases:
                    os_module_dict_aliases.add(alias)
                    pending_dict_aliases.append(alias)

        def constant_string(node: ast.AST) -> str | None:
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                return node.value
            if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
                left = constant_string(node.left)
                right = constant_string(node.right)
                return f"{left}{right}" if left is not None and right is not None else None
            return None

        def constant_mapping_key(node: ast.AST) -> str | None:
            if isinstance(node, ast.Constant):
                if isinstance(node.value, str):
                    return node.value
                if isinstance(node.value, bytes):
                    try:
                        return node.value.decode("utf-8")
                    except UnicodeDecodeError:
                        return None
            return None

        def mapping_update_may_change_port(node: ast.AST | None) -> bool:
            if not isinstance(node, ast.Dict):
                return True
            return any(
                key is None or constant_mapping_key(key) in {None, "PRISM_PREVIEW_PORT"}
                for key in node.keys
            )

        def module_dict_reference(node: ast.AST) -> bool:
            if dotted_name(node) in os_module_dict_aliases:
                return True
            if isinstance(node, ast.Call) and call_name(node) == "vars" and node.args:
                return dotted_name(node.args[0]) in trusted_os_module_names
            if (
                isinstance(node, ast.Call)
                and call_name(node) == "getattr"
                and len(node.args) >= 2
                and dotted_name(node.args[0]) in trusted_os_module_names
                and isinstance(node.args[1], ast.Constant)
                and node.args[1].value == "__dict__"
            ):
                return True
            return False

        def module_dict_mutation(target: ast.AST, key: ast.AST | None = None) -> None:
            """Poison trusted os getters/environment when code writes via module __dict__."""
            if not isinstance(target, ast.Subscript):
                return
            if not module_dict_reference(target.value):
                return
            key_node = key if key is not None else target.slice
            key_value = key_node.value if isinstance(key_node, ast.Constant) else None
            affected = trusted_os_module_names
            if key_value == "getenv":
                mutated_port_sources.update(f"{module_name}.getenv" for module_name in affected)
            elif key_value == "environ":
                mutated_port_sources.update(f"{module_name}.environ" for module_name in affected)
            else:
                # Unknown/dynamic keys are conservatively treated as mutating either
                # trusted source; this prevents reflection from bypassing the guard.
                mutated_port_sources.update(
                    name for module_name in affected for name in (f"{module_name}.getenv", f"{module_name}.environ")
                )

        def is_environ_reference(name: str) -> bool:
            return any(name == alias or name.startswith(f"{alias}.") for alias in environ_aliases)

        def environ_mapping_reference(node: ast.AST, seen_parameters: set[tuple[int, str]] | None = None) -> bool:
            visited = set(seen_parameters or ())
            if is_environ_reference(dotted_name(node)):
                return True
            if isinstance(node, ast.Starred):
                return environ_mapping_reference(node.value, visited)
            if isinstance(node, ast.Name):
                scope = enclosing_scope(node)
                if scope is not None:
                    token = (id(scope), node.id)
                    if token not in visited and any(
                        environ_mapping_reference(value, visited | {token})
                        for value in parameter_sources.get((scope, node.id), [])
                    ):
                        return True
            if isinstance(node, ast.Subscript) and isinstance(node.value, ast.Name):
                scope = enclosing_scope(node.value)
                if scope is not None:
                    token = (id(scope), node.value.id)
                    if token not in visited:
                        key = constant_mapping_key(node.slice)
                        index = node.slice.value if isinstance(node.slice, ast.Constant) else None
                        for source in parameter_sources.get((scope, node.value.id), []):
                            selected: list[ast.AST] = []
                            if isinstance(source, (ast.Tuple, ast.List)) and isinstance(index, int):
                                if -len(source.elts) <= index < len(source.elts):
                                    selected = [source.elts[index]]
                            elif isinstance(source, ast.Dict):
                                selected = [
                                    value
                                    for source_key, value in zip(source.keys, source.values)
                                    if source_key is None or key is None or constant_mapping_key(source_key) == key
                                ]
                            if any(
                                environ_mapping_reference(value, visited | {token})
                                for value in selected
                            ):
                                return True
            if isinstance(node, (ast.Dict, ast.List, ast.Tuple, ast.Set)):
                values = node.values if isinstance(node, ast.Dict) else node.elts
                return any(
                    value is not None and environ_mapping_reference(value, visited)
                    for value in values
                )
            if isinstance(node, ast.Subscript) and module_dict_reference(node.value):
                return constant_string(node.slice) in {None, "environ"}
            if isinstance(node, ast.Call):
                if (
                    call_name(node) == "getattr"
                    and len(node.args) >= 2
                    and dotted_name(node.args[0]) in trusted_os_module_names
                    and constant_string(node.args[1]) in {None, "environ"}
                ):
                    return True
                if (
                    isinstance(node.func, ast.Attribute)
                    and node.func.attr == "get"
                    and module_dict_reference(node.func.value)
                    and node.args
                    and constant_string(node.args[0]) in {None, "environ"}
                ):
                    return True
            return False

        def assignment_name_pairs(target: ast.AST, value: ast.AST) -> list[tuple[ast.Name, ast.AST]]:
            if isinstance(target, ast.Name):
                return [(target, value)]
            if isinstance(target, (ast.Tuple, ast.List)) and isinstance(value, (ast.Tuple, ast.List)):
                return [
                    pair
                    for target_item, value_item in zip(target.elts, value.elts)
                    for pair in assignment_name_pairs(target_item, value_item)
                ]
            return []

        if tree is not None:
            environment_alias_assignments = [
                pair
                for node in ast.walk(tree)
                if isinstance(node, ast.Assign)
                for target in node.targets
                for pair in assignment_name_pairs(target, node.value)
            ]
            environment_alias_assignments.extend(
                pair
                for node in ast.walk(tree)
                if isinstance(node, ast.AnnAssign) and node.value is not None
                for pair in assignment_name_pairs(node.target, node.value)
            )
            changed = True
            while changed:
                changed = False
                for target, value in environment_alias_assignments:
                    value_is_alias = isinstance(value, ast.Name) and value.id in environ_aliases
                    if target.id not in environ_aliases and (value_is_alias or environ_mapping_reference(value)):
                        environ_aliases.add(target.id)
                        changed = True
            trusted_environ_names.update(environ_aliases)
            trusted_port_calls.update(f"{alias}.get" for alias in environ_aliases)

        def contains_environ_reference(node: ast.AST) -> bool:
            visited_parameters: set[tuple[int, str]] = set()

            def contains_reference(value: ast.AST) -> bool:
                if environ_mapping_reference(value):
                    return True
                if isinstance(value, ast.Name):
                    scope = enclosing_scope(value)
                    if scope is not None:
                        token = (id(scope), value.id)
                        if token not in visited_parameters:
                            visited_parameters.add(token)
                            if any(
                                contains_reference(source)
                                for source in parameter_sources.get((scope, value.id), [])
                            ):
                                return True
                return any(contains_reference(child) for child in ast.iter_child_nodes(value))

            return contains_reference(node)

        def contains_os_module_reference(node: ast.AST) -> bool:
            return any(
                dotted_name(child) in trusted_os_module_names
                for child in ast.walk(node)
                if isinstance(child, (ast.Name, ast.Attribute))
            )

        def poison_os_mapping_trust() -> None:
            mutated_port_sources.update(
                name
                for module_name in trusted_os_module_names
                for name in (f"{module_name}.getenv", f"{module_name}.environ")
            )
            mutated_port_sources.update(trusted_port_calls)

        def poison_environ_trust(key: ast.AST | str | None = None) -> None:
            key_value = constant_mapping_key(key) if isinstance(key, ast.AST) else key
            if key_value is not None and key_value != "PRISM_PREVIEW_PORT":
                return
            mutated_port_sources.update(environ_aliases)
            # getenv and imported getenv aliases read this same environment mapping.
            mutated_port_sources.update(trusted_port_calls)

        methodcaller_mutator_names: set[str] = set()
        mutation_methods = {
            "__setitem__",
            "__delitem__",
            "__ior__",
            "update",
            "clear",
            "pop",
            "popitem",
            "setdefault",
        }

        mutated_port_sources: set[str] = set()

        def builtins_module_reference(node: ast.AST) -> bool:
            return isinstance(node, ast.Name) and (
                node.id == "__builtins__" or node.id in trusted_builtin_module_names
            )

        def function_return_values(
            function: ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda,
        ) -> list[ast.AST]:
            if isinstance(function, ast.Lambda):
                return [function.body]
            return [
                node.value
                for node in ast.walk(function)
                if isinstance(node, ast.Return)
                and node.value is not None
                and enclosing_scope(node) is function
            ]

        MAX_STATIC_STRING_VALUES = 256
        MAX_STATIC_STRING_PRODUCTS = 4096
        MAX_STATIC_STRING_LENGTH = 16_384
        static_string_resolution_overflow = False

        def bounded_static_strings(values: Iterable[str]) -> set[str]:
            nonlocal static_string_resolution_overflow
            result: set[str] = set()
            for examined, value in enumerate(values, start=1):
                if examined > MAX_STATIC_STRING_PRODUCTS:
                    static_string_resolution_overflow = True
                    return set()
                if len(value) > MAX_STATIC_STRING_LENGTH:
                    static_string_resolution_overflow = True
                    return set()
                result.add(value)
                if len(result) > MAX_STATIC_STRING_VALUES:
                    static_string_resolution_overflow = True
                    return set()
            return result

        def static_product_within_limit(groups: list[set[str]]) -> bool:
            nonlocal static_string_resolution_overflow
            combinations = 1
            for group in groups:
                combinations *= len(group)
                if combinations > MAX_STATIC_STRING_PRODUCTS:
                    static_string_resolution_overflow = True
                    return False
            return True

        def resolved_string_values(node: ast.AST, seen: set[str] | None = None) -> set[str]:
            visited = set(seen or ())
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                return bounded_static_strings((node.value,))
            if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
                left = resolved_string_values(node.left, visited)
                right = resolved_string_values(node.right, visited)
                if not static_product_within_limit([left, right]):
                    return set()
                return bounded_static_strings(f"{left_value}{right_value}" for left_value in left for right_value in right)
            if isinstance(node, ast.Name):
                scope = enclosing_scope(node)
                if scope is None:
                    return set()
                token = f"{id(scope)}:{node.id}"
                if token in visited:
                    return set()
                position = (
                    int(getattr(node, "lineno", 1 << 30) or (1 << 30)),
                    int(getattr(node, "col_offset", 1 << 30) or (1 << 30)),
                )
                bindings = assignment_bindings.get((scope, node.id), [])
                prior = [value for line, column, value in bindings if (line, column) < position]
                values = [prior[-1]] if prior else parameter_sources.get((scope, node.id), [])
                if not values and scope is not module_scope:
                    module_bindings = assignment_bindings.get((module_scope, node.id), [])
                    module_prior = [value for line, column, value in module_bindings if (line, column) < position]
                    values = [module_prior[-1]] if module_prior else []
                return bounded_static_strings(
                    value
                    for source in values
                    for value in resolved_string_values(source, visited | {token})
                )
            if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
                return bounded_static_strings(
                    value
                    for item in node.elts
                    for value in resolved_string_values(item, visited)
                )
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                function = function_defs.get(node.func.id)
                token = f"call:{node.func.id}"
                if function is not None and token not in visited:
                    return bounded_static_strings(
                        value
                        for result in function_return_values(function)
                        for value in resolved_string_values(result, visited | {token})
                    )
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "join":
                separator_values = resolved_string_values(node.func.value, visited)
                sequence = node.args[0] if node.args else None
                if isinstance(sequence, (ast.List, ast.Tuple, ast.Set)):
                    elements = [resolved_string_values(item, visited) for item in sequence.elts]
                    groups = [separator_values, *elements]
                    if separator_values and all(elements) and static_product_within_limit(groups):
                        return bounded_static_strings(
                            separator.join(values)
                            for separator in separator_values
                            for values in itertools.product(*elements)
                        )
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "format":
                templates = resolved_string_values(node.func.value, visited)
                arguments = [resolved_string_values(value, visited) for value in node.args]
                groups = [templates, *arguments]
                if templates and all(arguments) and static_product_within_limit(groups):
                    rendered: list[str] = []
                    for template in templates:
                        for values in itertools.product(*arguments):
                            try:
                                rendered.append(template.format(*values))
                            except (IndexError, KeyError, ValueError):
                                continue
                    return bounded_static_strings(rendered)
            return set()

        def operator_import_call(node: ast.AST) -> bool:
            if not isinstance(node, ast.Call) or not node.args:
                return False
            module_names = resolved_string_values(node.args[0])
            if not module_names or not module_names <= {"operator", "_operator"}:
                return False
            function = node.func
            if dotted_name(function) in trusted_import_functions:
                return True
            if (
                isinstance(function, ast.Attribute)
                and function.attr == "__import__"
                and builtins_module_reference(function.value)
            ):
                return True
            if (
                isinstance(function, ast.Subscript)
                and builtins_module_reference(function.value)
                and constant_string(function.slice) == "__import__"
            ):
                return True
            if dotted_name(function) in trusted_import_module_functions:
                return True
            return False

        def sys_modules_reference(node: ast.AST) -> bool:
            return (
                isinstance(node, ast.Attribute)
                and node.attr == "modules"
                and isinstance(node.value, ast.Name)
                and node.value.id in trusted_sys_module_names
            )

        def operator_module_reference(node: ast.AST, seen_functions: set[str] | None = None) -> bool:
            seen = seen_functions or set()
            if isinstance(node, ast.Name) and node.id in trusted_operator_module_names:
                return True
            if isinstance(node, ast.Name):
                scope = enclosing_scope(node)
                if scope is not None:
                    token = f"{id(scope)}:{node.id}"
                    if token not in seen:
                        return any(
                            operator_module_reference(value, {*seen, token})
                            for value in parameter_sources.get((scope, node.id), [])
                        )
            if operator_import_call(node):
                return True
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id not in seen:
                function = function_defs.get(node.func.id)
                if function is not None:
                    next_seen = {*seen, node.func.id}
                    if any(
                        operator_module_reference(value, next_seen)
                        for value in function_return_values(function)
                    ):
                        return True
            if isinstance(node, ast.Subscript) and sys_modules_reference(node.value):
                return constant_string(node.slice) in {None, "operator", "_operator"}
            return (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "get"
                and sys_modules_reference(node.func.value)
                and bool(node.args)
                and constant_string(node.args[0]) in {None, "operator", "_operator"}
            )

        assignment_pairs: list[tuple[ast.Name, ast.AST]] = []
        if tree is not None:
            for node in ast.walk(tree):
                if isinstance(node, ast.Assign):
                    assignment_pairs.extend(
                        pair for target in node.targets for pair in assignment_name_pairs(target, node.value)
                    )
                elif isinstance(node, ast.AnnAssign) and node.value is not None:
                    assignment_pairs.extend(assignment_name_pairs(node.target, node.value))

        mutator_names = mutation_methods | {"setitem", "delitem"}
        mutator_aliases: dict[str, tuple[str, bool, tuple[ast.AST, ...]]] = {}

        def resolved_callable_expression(node: ast.AST, seen: set[str] | None = None) -> ast.AST:
            visited = set(seen or ())
            if not isinstance(node, ast.Name) or node.id in visited:
                return node
            visited.add(node.id)
            scope = enclosing_scope(node)
            if scope is None:
                return node
            position = (
                int(getattr(node, "lineno", 1 << 30) or (1 << 30)),
                int(getattr(node, "col_offset", 1 << 30) or (1 << 30)),
            )
            bindings = [
                value
                for line, column, value in assignment_bindings.get((scope, node.id), [])
                if (line, column) < position
            ]
            if not bindings and scope is not module_scope and node.id not in local_names.get(scope, set()):
                bindings = [
                    value
                    for line, column, value in assignment_bindings.get((module_scope, node.id), [])
                    if (line, column) < position
                ]
            if bindings:
                return resolved_callable_expression(bindings[-1], visited)
            parameter_values = parameter_sources.get((scope, node.id), [])
            if len(parameter_values) == 1:
                return resolved_callable_expression(parameter_values[0], visited)
            return node

        def mutator_callable_spec(
            node: ast.AST, seen: set[str] | None = None
        ) -> tuple[str, bool, tuple[ast.AST, ...]] | None:
            visited = set(seen or ())
            if isinstance(node, ast.Name):
                if node.id in mutator_aliases:
                    return mutator_aliases[node.id]
                if node.id not in visited:
                    visited.add(node.id)
                    scope = enclosing_scope(node)
                    if scope is not None:
                        specs = [
                            spec
                            for value in parameter_sources.get((scope, node.id), [])
                            if (spec := mutator_callable_spec(value, visited)) is not None
                        ]
                        if specs:
                            return specs[0] if all(spec == specs[0] for spec in specs) else ("", False, ())
                return None
            if isinstance(node, ast.Attribute):
                return (node.attr, environ_mapping_reference(node.value), ()) if node.attr in mutator_names else None
            if isinstance(node, ast.Subscript):
                method = constant_string(node.slice)
                return (method, False, ()) if method in mutator_names else None
            if isinstance(node, ast.Call):
                getter = resolved_callable_expression(node.func)
                if isinstance(getter, ast.Name) and getter.id in trusted_getattr_calls | {"getattr", "object.__getattribute__"} and len(node.args) >= 2:
                    method = constant_string(node.args[1])
                    if method in mutator_names:
                        return (method, environ_mapping_reference(node.args[0]), ())
                if dotted_name(getter) == "object.__getattribute__" and len(node.args) >= 2:
                    method = constant_string(node.args[1])
                    if method in mutator_names:
                        return (method, environ_mapping_reference(node.args[0]), ())
                if (
                    isinstance(getter, ast.Attribute)
                    and getter.attr == "__getattribute__"
                    and node.args
                ):
                    method = constant_string(node.args[0])
                    if method in mutator_names:
                        return (method, environ_mapping_reference(getter.value), ())
                if call_name(node) in trusted_partial_calls and node.args:
                    spec = mutator_callable_spec(node.args[0], visited)
                    if spec is not None:
                        method, bound, prior = spec
                        return method, bound, (*prior, *node.args[1:])
                if isinstance(node.func, ast.Name) and node.func.id not in visited:
                    function = function_defs.get(node.func.id)
                    if function is not None:
                        next_visited = {*visited, node.func.id}
                        specs = [
                            spec
                            for value in function_return_values(function)
                            if (spec := mutator_callable_spec(value, next_visited)) is not None
                        ]
                        if specs:
                            return specs[0] if all(spec == specs[0] for spec in specs) else ("", False, ())
                name = call_name(node)
                if name in trusted_setitem_calls:
                    return "setitem", False, ()
                if name in trusted_delitem_calls:
                    return "delitem", False, ()
                if name in trusted_dict_update_calls:
                    return "update", False, ()
            return None

        def mutator_call_may_change_port(
            method: str, bound: bool, bound_arguments: tuple[ast.AST, ...], call: ast.Call
        ) -> bool:
            arguments = [*bound_arguments, *call.args]
            if method in {"__setitem__", "__delitem__", "setitem", "delitem", "pop", "setdefault"}:
                key_index = 0 if bound else 1
                target_index = None if bound else 0
                if target_index is not None and (
                    target_index >= len(arguments) or not environ_mapping_reference(arguments[target_index])
                ):
                    return False
                if key_index < len(arguments):
                    return constant_mapping_key(arguments[key_index]) in {None, "PRISM_PREVIEW_PORT"}
                return True
            if method == "update":
                target_index = None if bound else 0
                mapping_index = 0 if bound else 1
                if target_index is not None and (
                    target_index >= len(arguments) or not environ_mapping_reference(arguments[target_index])
                ):
                    return False
                return mapping_index >= len(arguments) or mapping_update_may_change_port(arguments[mapping_index])
            if method in {"clear", "popitem", "__ior__"}:
                target_index = None if bound else 0
                return target_index is None or (
                    target_index < len(arguments) and environ_mapping_reference(arguments[target_index])
                )
            return False

        if tree is not None:
            changed = True
            while changed:
                changed = False
                for target, value in assignment_pairs:
                    spec = mutator_callable_spec(value)
                    if target.id not in mutator_aliases and spec is not None:
                        mutator_aliases[target.id] = spec
                        changed = True

            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                if isinstance(node.func, ast.Name) and node.func.id in mutator_aliases:
                    method, bound, bound_arguments = mutator_aliases[node.func.id]
                    if mutator_call_may_change_port(method, bound, bound_arguments, node):
                        poison_environ_trust()
                elif any(mutator_callable_spec(argument) is not None for argument in node.args) and contains_environ_reference(node):
                    # Passing a mutator through a helper can hide its eventual target;
                    # keep the runner port untrusted rather than guessing the callee.
                    poison_environ_trust()

            # Resolve aliases created from importlib, __import__, and sys.modules.
            # Without this fixed point, an assigned module object looks unrelated to
            # operator even though its source is statically known.
            changed = True
            while changed:
                changed = False
                for target, value in assignment_pairs:
                    if target.id not in trusted_operator_module_names and operator_module_reference(value):
                        trusted_operator_module_names.add(target.id)
                        trusted_setitem_calls.add(f"{target.id}.setitem")
                        trusted_delitem_calls.add(f"{target.id}.delitem")
                        trusted_methodcaller_calls.add(f"{target.id}.methodcaller")
                        changed = True

        def operator_dict_reference(node: ast.AST) -> bool:
            if isinstance(node, ast.Attribute) and node.attr == "__dict__":
                return operator_module_reference(node.value)
            if isinstance(node, ast.Call) and call_name(node) == "vars" and node.args:
                return operator_module_reference(node.args[0])
            return False

        operator_dict_getter_names: set[str] = set()

        def trusted_getattr_call(node: ast.Call) -> bool:
            return dotted_name(node.func) in trusted_getattr_calls

        def operator_dict_getter_reference(node: ast.AST) -> bool:
            if isinstance(node, ast.Name):
                return node.id in operator_dict_getter_names
            if isinstance(node, ast.Attribute) and dotted_name(node) in trusted_dict_getitem_calls:
                return True
            if isinstance(node, ast.Attribute) and node.attr == "get":
                return operator_dict_reference(node.value)
            if (
                isinstance(node, ast.Call)
                and trusted_getattr_call(node)
                and len(node.args) >= 2
                and operator_dict_reference(node.args[0])
                and constant_string(node.args[1]) == "get"
            ):
                return True
            return False

        if tree is not None:
            changed = True
            while changed:
                changed = False
                for target, value in assignment_pairs:
                    if target.id not in operator_dict_getter_names and operator_dict_getter_reference(value):
                        operator_dict_getter_names.add(target.id)
                        changed = True

        def is_partial_call(node: ast.AST) -> bool:
            return isinstance(node, ast.Call) and call_name(node) in trusted_partial_calls

        methodcaller_factory_names = set(trusted_methodcaller_calls)

        def methodcaller_factory_reference(node: ast.AST, seen_functions: set[str] | None = None) -> bool:
            seen = seen_functions or set()
            if isinstance(node, ast.Name) and node.id in methodcaller_factory_names:
                return True
            if isinstance(node, ast.Attribute) and node.attr == "methodcaller":
                return operator_module_reference(node.value)
            if isinstance(node, ast.Call):
                if (
                    trusted_getattr_call(node)
                    and len(node.args) >= 2
                    and (operator_module_reference(node.args[0]) or operator_dict_reference(node.args[0]))
                    and (constant_string(node.args[1]) in {None, "methodcaller"})
                ):
                    return True
                if (
                    operator_dict_getter_reference(node.func)
                    and bool(node.args)
                    and constant_string(node.args[0]) in {None, "methodcaller"}
                ):
                    return True
                if (
                    call_name(node) == "dict.__getitem__"
                    and len(node.args) >= 2
                    and operator_dict_reference(node.args[0])
                    and constant_string(node.args[1]) in {None, "methodcaller"}
                ):
                    return True
                if isinstance(node.func, ast.Name) and node.func.id not in seen:
                    function = function_defs.get(node.func.id)
                    if function is not None:
                        next_seen = {*seen, node.func.id}
                        return any(
                            methodcaller_factory_reference(value, next_seen)
                            for value in function_return_values(function)
                        )
            if isinstance(node, ast.Subscript) and operator_dict_reference(node.value):
                key = constant_string(node.slice)
                return key in {None, "methodcaller"}
            return False

        methodcaller_mutator_specs: dict[str, tuple[str | None, ast.AST | None]] = {}

        def methodcaller_mutator_spec(
            node: ast.AST, seen_functions: set[str] | None = None
        ) -> tuple[str | None, ast.AST | None] | None:
            seen = seen_functions or set()
            if isinstance(node, ast.Name):
                known = methodcaller_mutator_specs.get(node.id)
                if known is not None:
                    return known
                scope = enclosing_scope(node)
                token = f"{id(scope)}:{node.id}"
                if scope is not None and token not in seen:
                    specs = [
                        spec
                        for value in parameter_sources.get((scope, node.id), [])
                        if (spec := methodcaller_mutator_spec(value, {*seen, token})) is not None
                    ]
                    if specs:
                        return specs[0] if all(spec == specs[0] for spec in specs) else (None, None)
                return None
            if isinstance(node, ast.Call):
                if is_partial_call(node) and node.args:
                    callable_expression = node.args[0]
                    if methodcaller_factory_reference(callable_expression):
                        method = constant_string(node.args[1]) if len(node.args) > 1 else None
                        if method is not None and method not in mutation_methods:
                            return None
                        key_index = (
                            2
                            if method in {"__setitem__", "__delitem__", "pop", "setdefault", "update", "__ior__"}
                            else None
                        )
                        return method, node.args[key_index] if key_index is not None and len(node.args) > key_index else None
                    return methodcaller_mutator_spec(callable_expression, seen)
                if isinstance(node.func, ast.Name) and node.func.id in methodcaller_mutator_specs:
                    return methodcaller_mutator_specs[node.func.id]
                if methodcaller_factory_reference(node.func):
                    method = constant_string(node.args[0]) if node.args else None
                    if method is not None and method not in mutation_methods:
                        return None
                    key_index = (
                        1
                        if method in {"__setitem__", "__delitem__", "pop", "setdefault", "update", "__ior__"}
                        else None
                    )
                    return method, node.args[key_index] if key_index is not None and len(node.args) > key_index else None
                if isinstance(node.func, ast.Name) and node.func.id not in seen:
                    function = function_defs.get(node.func.id)
                    if function is not None:
                        next_seen = {*seen, node.func.id}
                        specs = [
                            spec
                            for value in function_return_values(function)
                            if (spec := methodcaller_mutator_spec(value, next_seen)) is not None
                        ]
                        if specs:
                            return specs[0] if all(spec == specs[0] for spec in specs) else (None, None)
            return None

        def methodcaller_mutator_expression(node: ast.AST, seen_functions: set[str] | None = None) -> bool:
            return methodcaller_mutator_spec(node, seen_functions) is not None

        def poison_methodcaller_target(method: str | None, key_or_mapping: ast.AST | None) -> None:
            if method in {"__setitem__", "__delitem__", "pop", "setdefault"}:
                poison_environ_trust(key_or_mapping)
            elif method in {"update", "__ior__"}:
                if mapping_update_may_change_port(key_or_mapping):
                    poison_environ_trust()
            else:
                poison_environ_trust()

        if tree is not None:
            # Do not rely on recognizing the callable's origin: generated code can
            # recover methodcaller through descriptors, containers, or wrappers.
            # A statically recoverable mutator configured to alter the runner's
            # dynamic port invalidates that port source even when the eventual
            # invocation is indirect.
            dynamic_port_mutators = {
                method
                for method in mutation_methods
                if method not in {"clear", "popitem"}
            }
            has_environment_reference = contains_environ_reference(tree)
            for candidate in ast.walk(tree):
                if not isinstance(candidate, ast.Call):
                    continue
                prior_overflow = static_string_resolution_overflow
                static_string_resolution_overflow = False
                argument_strings = {
                    value
                    for argument in [*candidate.args, *(keyword.value for keyword in candidate.keywords)]
                    for child in ast.walk(argument)
                    for value in resolved_string_values(child)
                }
                candidate_overflow = static_string_resolution_overflow
                static_string_resolution_overflow = prior_overflow or candidate_overflow
                if argument_strings & dynamic_port_mutators and "PRISM_PREVIEW_PORT" in argument_strings:
                    poison_environ_trust("PRISM_PREVIEW_PORT")
                elif has_environment_reference and argument_strings & {"clear", "popitem"}:
                    poison_environ_trust()
                candidate_callable = resolved_callable_expression(candidate.func)
                candidate_name = dotted_name(candidate_callable)
                overflow_sensitive_call = (
                    candidate_name in trusted_import_functions
                    | trusted_import_module_functions
                    | trusted_getattr_calls
                    | trusted_partial_calls
                    | trusted_methodcaller_calls
                    | {"object.__getattribute__"}
                    or (
                        isinstance(candidate_callable, ast.Attribute)
                        and candidate_callable.attr == "__getattribute__"
                    )
                )
                if candidate_overflow and has_environment_reference and overflow_sensitive_call:
                    # Bound combinatorial work for reflection/import/mutator
                    # resolution, while leaving unrelated application strings
                    # (such as encoded query parameters) out of this trust gate.
                    poison_environ_trust()

        if tree is not None:
            changed = True
            while changed:
                changed = False
                for target, value in assignment_pairs:
                    if target.id not in methodcaller_factory_names and methodcaller_factory_reference(value):
                        methodcaller_factory_names.add(target.id)
                        changed = True
            changed = True
            while changed:
                changed = False
                for target, value in assignment_pairs:
                    spec = methodcaller_mutator_spec(value)
                    if target.id not in methodcaller_mutator_names and spec is not None:
                        methodcaller_mutator_names.add(target.id)
                        methodcaller_mutator_specs[target.id] = spec
                        changed = True

        if tree is not None:
            for node in ast.walk(tree):
                targets: list[ast.AST] = []
                if isinstance(node, ast.Assign):
                    targets = list(node.targets)
                elif isinstance(node, (ast.AnnAssign, ast.AugAssign, ast.NamedExpr)):
                    targets = [node.target]
                elif isinstance(node, ast.Delete):
                    targets = list(node.targets)
                for target in targets:
                    binding_value = (
                        node.value
                        if isinstance(node, (ast.Assign, ast.AnnAssign, ast.NamedExpr))
                        else None
                    )
                    alias_initialization = (
                        isinstance(target, ast.Name)
                        and target.id in environ_aliases
                        and binding_value is not None
                        and environ_mapping_reference(binding_value)
                    )
                    if isinstance(target, (ast.Tuple, ast.List)):
                        target_paths = [target_path(child) for child in target.elts]
                    else:
                        target_paths = [target_path(target)]
                    for target_name in target_paths:
                        is_mapping_item = isinstance(target, ast.Subscript) and (
                            is_environ_reference(target_name) or environ_mapping_reference(target.value)
                        )
                        if (
                            (target_name in trusted_environ_names or target_name in trusted_port_calls)
                            and not alias_initialization
                            and not is_mapping_item
                        ):
                            mutated_port_sources.add(target_name)
                        if (is_environ_reference(target_name) or is_mapping_item) and not alias_initialization:
                            poison_environ_trust(target.slice if isinstance(target, ast.Subscript) else None)
                    for target in (target.elts if isinstance(target, (ast.Tuple, ast.List)) else [target]):
                        module_dict_mutation(target)

                if not isinstance(node, ast.Call):
                    continue
                if call_name(node) in trusted_delitem_calls and node.args:
                    if contains_environ_reference(node.args[0]):
                        poison_environ_trust(node.args[1] if len(node.args) > 1 else None)
                methodcaller_spec = methodcaller_mutator_spec(node.func)
                bound_partial_environment = is_partial_call(node.func) and any(
                    contains_environ_reference(argument) for argument in node.func.args[1:]
                )
                if methodcaller_spec is not None and (bound_partial_environment or any(
                    contains_environ_reference(argument)
                    for argument in [*node.args, *(keyword.value for keyword in node.keywords)]
                )):
                    poison_methodcaller_target(*methodcaller_spec)
                if is_partial_call(node) and len(node.args) > 1:
                    partial_spec = methodcaller_mutator_spec(node)
                    if partial_spec is not None and any(contains_environ_reference(argument) for argument in node.args[1:]):
                        poison_methodcaller_target(*partial_spec)
                if isinstance(node.func, ast.Attribute):
                    if environ_mapping_reference(node.func.value) and node.func.attr in {
                        "clear",
                        "pop",
                        "popitem",
                        "setdefault",
                        "update",
                        "__delitem__",
                        "__setitem__",
                    }:
                        if node.func.attr in {"__setitem__", "__delitem__", "pop", "setdefault"}:
                            poison_environ_trust(node.args[0] if node.args else None)
                        elif node.func.attr in {"update", "__ior__"}:
                            if mapping_update_may_change_port(node.args[0] if node.args else None):
                                poison_environ_trust()
                        else:
                            poison_environ_trust()
                    if module_dict_reference(node.func.value) and node.func.attr in {
                        "clear",
                        "pop",
                        "popitem",
                        "setdefault",
                        "update",
                        "__delitem__",
                        "__setitem__",
                    }:
                        # Mutation methods on the os module namespace may replace
                        # getenv/environ; without proving the changed keys, fail closed.
                        mutated_port_sources.update(
                            name
                            for module_name in trusted_os_module_names
                            for name in (f"{module_name}.getenv", f"{module_name}.environ")
                        )
                    if node.func.attr in {
                        "setitem",
                        "__setitem__",
                        "__delitem__",
                        "__setattr__",
                        "__delattr__",
                        "update",
                        "clear",
                        "pop",
                        "popitem",
                        "setdefault",
                    }:
                        if contains_environ_reference(node.func.value) or any(
                            contains_environ_reference(argument) for argument in node.args
                        ):
                            poison_environ_trust()
                        if (
                            module_dict_reference(node.func.value)
                            or any(module_dict_reference(argument) for argument in node.args)
                            or (
                                node.func.attr in {"__setattr__", "__delattr__"}
                                and any(contains_os_module_reference(argument) for argument in node.args)
                            )
                        ):
                            poison_os_mapping_trust()
                dict_setitem = call_name(node) in trusted_setitem_calls
                dict_delitem = call_name(node) in trusted_delitem_calls
                dict_update = call_name(node) in trusted_dict_update_calls
                if (dict_setitem or dict_delitem or dict_update) and node.args:
                    target = node.args[0]
                    if environ_mapping_reference(target):
                        if dict_setitem or dict_delitem:
                            poison_environ_trust(node.args[1] if len(node.args) > 1 else None)
                        elif mapping_update_may_change_port(node.args[1] if len(node.args) > 1 else None):
                            poison_environ_trust()
                    if module_dict_reference(target):
                        key = node.args[1] if len(node.args) > 1 else None
                        if dict_setitem or dict_delitem:
                            module_dict_mutation(ast.Subscript(value=target, slice=key or ast.Constant(None), ctx=ast.Store()))
                        else:
                            # dict.update accepts an arbitrary mapping/kwargs.
                            poison_os_mapping_trust()
                if call_name(node) == "setitem" and len(node.args) >= 3:
                    target_name = dotted_name(node.args[0])
                    if environ_mapping_reference(node.args[0]):
                        poison_environ_trust(node.args[1])
                    if module_dict_reference(node.args[0]):
                        module_dict_mutation(
                            ast.Subscript(value=node.args[0], slice=node.args[1], ctx=ast.Store())
                        )
                if call_name(node) in trusted_setitem_calls and len(node.args) >= 3:
                    target = node.args[0]
                    if environ_mapping_reference(target):
                        poison_environ_trust(node.args[1])
                    if module_dict_reference(target):
                        module_dict_mutation(ast.Subscript(value=target, slice=node.args[1], ctx=ast.Store()))
                if (
                    call_name(node) in {"setattr", "delattr", "getattr", "vars", "object.__getattribute__"}
                    and node.args
                    and dotted_name(node.args[0]) in trusted_os_module_names
                ):
                    # Reflection over os can replace, hide, or recover trusted
                    # attributes; fail closed instead of trying to evaluate it.
                    poison_os_mapping_trust()

        # Add simple local aliases after scanning their defining assignments, so the
        # alias initialization itself is not mistaken for a mutation.
        trusted_environ_names.update(environ_aliases)
        trusted_port_calls.update(f"{alias}.get" for alias in environ_aliases)

        def preceding_bindings(scope: ast.AST, name: str, position: tuple[int, int]) -> list[ast.AST]:
            return [
                value for line, column, value in assignment_bindings.get((scope, name), []) if (line, column) < position
            ]

        def name_values(name: str, reference: ast.AST) -> list[ast.AST]:
            scope = enclosing_scope(reference)
            if scope is None:
                return []
            reference_position = (
                int(getattr(reference, "lineno", 1 << 30) or (1 << 30)),
                int(getattr(reference, "col_offset", 1 << 30) or (1 << 30)),
            )
            local = preceding_bindings(scope, name, reference_position)
            if local:
                return [local[-1]]
            parameter_values = parameter_sources.get((scope, name), [])
            if parameter_values:
                return parameter_values
            if scope is not module_scope and name in local_names.get(scope, set()):
                return []
            if module_scope is None:
                return []
            if scope is module_scope:
                module_values = preceding_bindings(module_scope, name, reference_position)
                return [module_values[-1]] if module_values else []
            call_sites = function_calls.get(scope, [])
            visible_values: list[ast.AST] = []
            for call in call_sites:
                call_position = (
                    int(getattr(call, "lineno", 1 << 30) or (1 << 30)),
                    int(getattr(call, "col_offset", 1 << 30) or (1 << 30)),
                )
                module_values = preceding_bindings(module_scope, name, call_position)
                if module_values and all(value is not module_values[-1] for value in visible_values):
                    visible_values.append(module_values[-1])
            if visible_values:
                return visible_values
            module_values = preceding_bindings(module_scope, name, (1 << 30, 1 << 30))
            return [module_values[-1]] if module_values else []

        def trusted_reference(name: str, node: ast.AST, trusted_names: set[str]) -> bool:
            if name not in trusted_names:
                return False
            if any(name == mutated or name.startswith(f"{mutated}.") for mutated in mutated_port_sources):
                return False
            root = name.split(".", 1)[0]
            scope = enclosing_scope(node)
            position = (
                int(getattr(node, "lineno", 1 << 30) or (1 << 30)),
                int(getattr(node, "col_offset", 1 << 30) or (1 << 30)),
            )

            trusted_alias_roots = trusted_os_module_names | environ_aliases

            def alias_is_trusted(
                alias: str,
                alias_scope: ast.AST | None,
                alias_position: tuple[int, int],
                seen: set[str] | None = None,
            ) -> bool:
                seen = set(seen or ())
                if alias in seen or alias not in trusted_alias_roots:
                    return False
                seen.add(alias)
                bindings = preceding_bindings(alias_scope, alias, alias_position) if alias_scope is not None else []
                if bindings:
                    value = bindings[-1]
                    if isinstance(value, ast.Name):
                        return alias_is_trusted(value.id, alias_scope, alias_position, seen)
                    if isinstance(value, ast.Attribute):
                        value_path = dotted_name(value)
                        if value_path in trusted_alias_roots:
                            return alias_is_trusted(
                                value_path.split(".", 1)[0], alias_scope, alias_position, seen
                            )
                    return False
                if alias_scope is not None and alias_scope is not module_scope and alias in local_names.get(alias_scope, set()):
                    return False
                if alias_scope is not None and alias_scope is not module_scope:
                    for call in function_calls.get(alias_scope, []):
                        call_position = (
                            int(getattr(call, "lineno", 1 << 30) or (1 << 30)),
                            int(getattr(call, "col_offset", 1 << 30) or (1 << 30)),
                        )
                        module_bindings = preceding_bindings(module_scope, alias, call_position) if module_scope else []
                        if module_bindings:
                            value = module_bindings[-1]
                            if not isinstance(value, ast.Name) or not alias_is_trusted(
                                value.id, module_scope, call_position, seen
                            ):
                                return False
                return True

            if root in trusted_alias_roots:
                if not alias_is_trusted(root, scope, position):
                    return False
            else:
                if scope is not None and scope is not module_scope and root in local_names.get(scope, set()):
                    return False
                if scope is not None and preceding_bindings(scope, root, position):
                    return False
                if module_scope is not None and scope is not module_scope:
                    call_sites = function_calls.get(scope, [])
                    if any(
                        preceding_bindings(
                            module_scope,
                            root,
                            (
                                int(getattr(call, "lineno", 1 << 30) or (1 << 30)),
                                int(getattr(call, "col_offset", 1 << 30) or (1 << 30)),
                            ),
                        )
                        for call in call_sites
                    ):
                        return False
                elif module_scope is not None and preceding_bindings(module_scope, root, position):
                    return False
            return True

        def trusted_port_alias_call(node: ast.Call, seen: set[str] | None = None) -> bool:
            if not isinstance(node.func, ast.Name):
                return False
            visited = set(seen or ())
            alias = node.func.id
            if alias in visited:
                return False
            visited.add(alias)
            sources = name_values(alias, node.func)
            if not sources:
                return False

            def source_is_trusted(source: ast.AST) -> bool:
                if isinstance(source, ast.Attribute):
                    return trusted_reference(dotted_name(source), source, trusted_port_calls)
                if isinstance(source, ast.Name):
                    return trusted_port_alias_call(
                        ast.Call(func=source, args=[], keywords=[]), visited
                    )
                if isinstance(source, ast.Call):
                    if (
                        call_name(source) in trusted_getattr_calls | {"object.__getattribute__"}
                        and len(source.args) >= 2
                        and constant_string(source.args[1]) == "get"
                        and trusted_environ_mapping(source.args[0])
                    ):
                        return True
                    return (
                        isinstance(source.func, ast.Attribute)
                        and source.func.attr == "__getattribute__"
                        and bool(source.args)
                        and constant_string(source.args[0]) == "get"
                        and trusted_environ_mapping(source.func.value)
                    )
                return False

            return all(source_is_trusted(source) for source in sources)

        def trusted_call(node: ast.Call, trusted_names: set[str]) -> bool:
            return trusted_reference(call_name(node), node, trusted_names) or (
                trusted_names is trusted_port_calls and trusted_port_alias_call(node)
            )

        def trusted_environ_mapping(node: ast.AST, seen: set[tuple[int, str]] | None = None) -> bool:
            visited = set(seen or ())
            reference_name = dotted_name(node)
            if is_environ_reference(reference_name):
                return trusted_reference(reference_name, node, trusted_environ_names)
            if isinstance(node, ast.Starred):
                return trusted_environ_mapping(node.value, visited)
            if isinstance(node, ast.Name):
                scope = enclosing_scope(node)
                if scope is not None:
                    token = (id(scope), node.id)
                    if token not in visited:
                        sources = parameter_sources.get((scope, node.id), [])
                        if sources:
                            return all(trusted_environ_mapping(source, visited | {token}) for source in sources)
            if isinstance(node, ast.Subscript) and isinstance(node.value, ast.Name):
                scope = enclosing_scope(node.value)
                if scope is not None:
                    token = (id(scope), node.value.id)
                    if token not in visited:
                        key = constant_mapping_key(node.slice)
                        index = node.slice.value if isinstance(node.slice, ast.Constant) else None
                        selected: list[ast.AST] = []
                        for source in parameter_sources.get((scope, node.value.id), []):
                            if isinstance(source, (ast.Tuple, ast.List)) and isinstance(index, int):
                                if -len(source.elts) <= index < len(source.elts):
                                    selected.append(source.elts[index])
                            elif isinstance(source, ast.Dict):
                                selected.extend(
                                    value
                                    for source_key, value in zip(source.keys, source.values)
                                    if source_key is None or key is None or constant_mapping_key(source_key) == key
                                )
                        if selected:
                            return all(
                                trusted_environ_mapping(value, visited | {token})
                                for value in selected
                            )
            if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
                return bool(node.elts) and all(trusted_environ_mapping(value, visited) for value in node.elts)
            if isinstance(node, ast.Dict):
                return bool(node.values) and all(
                    trusted_environ_mapping(value, visited) for value in node.values
                )
            if isinstance(node, ast.Subscript) and module_dict_reference(node.value):
                key = constant_string(node.slice)
                if key in {None, "environ"}:
                    return any(
                        trusted_reference(f"{module_name}.environ", node, trusted_environ_names)
                        for module_name in trusted_os_module_names
                    )
            if isinstance(node, ast.Call) and node.args:
                if call_name(node) == "getattr" and dotted_name(node.args[0]) in trusted_os_module_names:
                    if constant_string(node.args[1]) in {None, "environ"}:
                        return any(
                            trusted_reference(f"{module_name}.environ", node, trusted_environ_names)
                            for module_name in trusted_os_module_names
                        )
                if (
                    isinstance(node.func, ast.Attribute)
                    and node.func.attr == "get"
                    and module_dict_reference(node.func.value)
                    and constant_string(node.args[0]) in {None, "environ"}
                ):
                    return any(
                        trusted_reference(f"{module_name}.environ", node, trusted_environ_names)
                        for module_name in trusted_os_module_names
                    )
            return False

        def constant_numeric_guess(node: ast.AST, seen: set[str] | None = None) -> bool:
            seen = set(seen or ())
            if isinstance(node, ast.Name):
                token = f"{id(enclosing_scope(node))}:{node.id}"
                values = name_values(node.id, node)
                if values and token not in seen:
                    return all(constant_numeric_guess(value, seen | {token}) for value in values)
                return False
            if isinstance(node, ast.Constant):
                if isinstance(node.value, (int, float)) and not isinstance(node.value, bool):
                    return True
                return isinstance(node.value, str) and bool(re.fullmatch(r"\s*\d+(?:\.\d+)?\s*", node.value))
            if isinstance(node, ast.UnaryOp):
                return constant_numeric_guess(node.operand, seen)
            if isinstance(node, ast.BinOp):
                return constant_numeric_guess(node.left, seen) and constant_numeric_guess(node.right, seen)
            if isinstance(node, ast.FormattedValue):
                return constant_numeric_guess(node.value, seen)
            if isinstance(node, ast.JoinedStr):
                return any(constant_numeric_guess(value, seen) for value in node.values)
            if isinstance(node, ast.Call) and call_name(node) in {"str", "int", "float"} and node.args:
                return constant_numeric_guess(node.args[0], seen)
            return False

        if tree is not None:
            for call in (node for node in ast.walk(tree) if isinstance(node, ast.Call)):
                for index, argument in enumerate(call.args[:-1]):
                    if (
                        isinstance(argument, ast.Constant)
                        and isinstance(argument.value, str)
                        and argument.value.casefold() == "content-length"
                        and constant_numeric_guess(call.args[index + 1])
                    ):
                        issues.append(
                            f"{path or '未命名文件'} 第 {getattr(call, 'lineno', 0)} 行 Content-Length "
                            "使用了硬编码数字,必须由预期正文或实际响应计算"
                        )
                        break
                keyword_values = {keyword.arg: keyword.value for keyword in call.keywords if keyword.arg}
                header_keyword = next(
                    (
                        keyword_values[name]
                        for name in ("name", "header", "header_name", "key")
                        if name in keyword_values
                        and isinstance(keyword_values[name], ast.Constant)
                        and isinstance(keyword_values[name].value, str)
                        and keyword_values[name].value.casefold() == "content-length"
                    ),
                    None,
                )
                if header_keyword is not None and any(
                    name in keyword_values and constant_numeric_guess(keyword_values[name])
                    for name in ("value", "header_value", "expected", "expected_value")
                ):
                    issues.append(
                        f"{path or '未命名文件'} 第 {getattr(call, 'lineno', 0)} 行 Content-Length "
                        "使用了硬编码数字,必须由预期正文或实际响应计算"
                    )
            for dictionary in (node for node in ast.walk(tree) if isinstance(node, ast.Dict)):
                for key, value in zip(dictionary.keys, dictionary.values):
                    if (
                        isinstance(key, ast.Constant)
                        and isinstance(key.value, str)
                        and key.value.casefold() == "content-length"
                        and constant_numeric_guess(value)
                    ):
                        issues.append(
                            f"{path or '未命名文件'} 第 {getattr(dictionary, 'lineno', 0)} 行 Content-Length "
                            "使用了硬编码数字,必须由预期正文或实际响应计算"
                        )
                        break
        for line_number, line in enumerate(content.splitlines(), start=1):
            if "content-length" not in line.casefold():
                continue
            if re.search(
                r"(?:['\"]\d+['\"].*content-length|content-length.*(?:['\"]\d+['\"]|str\s*\(\s*\d+\s*\)|f['\"][^'\"]*\{\s*\d+\s*\}))",
                line,
                re.I,
            ):
                issues.append(
                    f"{path or '未命名文件'} 第 {line_number} 行 Content-Length 使用了硬编码数字,"
                    "必须由预期正文或实际响应计算"
                )
                break

        if language != "python" or path != "blackbox.py" or tree is None:
            continue
        if "urllib" not in content:
            issues.append(f"{path} 必须使用 urllib 发起真实的 127.0.0.1 回环请求")
            continue

        def suspicious_literal(value: str) -> bool:
            folded = value.casefold()
            return bool(re.search(r"\s", value)) or any(
                token in folded for token in ("' or ", '" or ', " union ", "<script", "../", "%0d", "%0a")
            )

        def is_zero_slice(node: ast.Subscript) -> bool:
            return (
                isinstance(node.slice, ast.Slice)
                and node.slice.lower is None
                and isinstance(node.slice.upper, ast.Constant)
                and node.slice.upper.value == 0
            )

        def constant_string_value(node: ast.AST, seen: set[str] | None = None) -> str | None:
            seen = set(seen or ())
            if isinstance(node, ast.Name):
                token = f"{id(enclosing_scope(node))}:{node.id}"
                values = name_values(node.id, node)
                if values and token not in seen:
                    constants = [constant_string_value(value, seen | {token}) for value in values]
                    if constants and all(value is not None and value == constants[0] for value in constants):
                        return constants[0]
                return None
            if isinstance(node, ast.Constant):
                if isinstance(node.value, str):
                    return node.value
                if isinstance(node.value, (int, float)) and not isinstance(node.value, bool):
                    return str(node.value)
                return None
            if isinstance(node, ast.FormattedValue):
                return constant_string_value(node.value, seen)
            if isinstance(node, ast.JoinedStr):
                values = [constant_string_value(value, seen) for value in node.values]
                return "".join(values) if all(value is not None for value in values) else None
            if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
                left = constant_string_value(node.left, seen)
                right = constant_string_value(node.right, seen)
                return left + right if left is not None and right is not None else None
            if not isinstance(node, ast.Call):
                return None
            name = call_name(node)
            if name in {"str", "int", "float"} and node.args:
                return constant_string_value(node.args[0], seen)
            if isinstance(node.func, ast.Attribute) and node.func.attr == "join" and node.args:
                separator = constant_string_value(node.func.value, seen)
                sequence_node = node.args[0]
                if isinstance(sequence_node, ast.Name):
                    bound_values = name_values(sequence_node.id, sequence_node)
                    sequence_node = bound_values[0] if len(bound_values) == 1 else sequence_node
                if separator is None or not isinstance(sequence_node, (ast.List, ast.Tuple, ast.Set)):
                    return None
                values = [constant_string_value(value, seen) for value in sequence_node.elts]
                return separator.join(values) if all(value is not None for value in values) else None
            if isinstance(node.func, ast.Attribute) and node.func.attr == "format":
                template = constant_string_value(node.func.value, seen)
                values = [constant_string_value(value, seen) for value in node.args]
                named_values = {
                    keyword.arg: constant_string_value(keyword.value, seen)
                    for keyword in node.keywords
                    if keyword.arg is not None
                }
                if (
                    template is not None
                    and all(value is not None for value in values)
                    and all(value is not None for value in named_values.values())
                ):
                    try:
                        return template.format(*values, **named_values)
                    except (IndexError, KeyError, ValueError):
                        return None
            return None

        # (kind, text): literal/raw retain text; encoded/port/unknown are provenance markers.
        def expression_segments(node: ast.AST, seen: set[str] | None = None) -> list[tuple[str, str]]:
            seen = set(seen or ())
            constant_value = constant_string_value(node, seen)
            if constant_value is not None:
                return [("raw" if suspicious_literal(constant_value) else "literal", constant_value)]
            if isinstance(node, ast.Name):
                token = f"{id(enclosing_scope(node))}:{node.id}"
                values = name_values(node.id, node)
                if values and token not in seen:
                    return [segment for value in values for segment in expression_segments(value, seen | {token})]
                return [("unknown", node.id)]
            if isinstance(node, ast.Constant):
                return []
            if isinstance(node, ast.FormattedValue):
                return expression_segments(node.value, seen)
            if isinstance(node, ast.JoinedStr):
                return [segment for value in node.values for segment in expression_segments(value, seen)]
            if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
                return expression_segments(node.left, seen) + expression_segments(node.right, seen)
            if isinstance(node, ast.Subscript):
                if is_zero_slice(node):
                    return []
                key = node.slice
                if (
                    isinstance(key, ast.Constant)
                    and key.value == "PRISM_PREVIEW_PORT"
                    and trusted_reference(dotted_name(node.value), node.value, trusted_environ_names)
                ):
                    return [("port", "")]
                if (
                    isinstance(key, ast.Constant)
                    and key.value == "PRISM_PREVIEW_PORT"
                    and trusted_environ_mapping(node.value)
                ):
                    return [("port", "")]
                segments = expression_segments(node.value, seen)
                return segments or [("unknown", "subscript")]
            if isinstance(node, ast.Attribute):
                return [("unknown", node.attr)]
            if isinstance(node, ast.Call):
                name = call_name(node)
                if (
                    isinstance(node.func, ast.Call)
                    and call_name(node.func) in trusted_getattr_calls | {"object.__getattribute__"}
                    and len(node.func.args) >= 2
                    and constant_string(node.func.args[1]) == "get"
                    and node.args
                    and "PRISM_PREVIEW_PORT" in resolved_string_values(node.args[0])
                    and trusted_environ_mapping(node.func.args[0])
                ):
                    return [("port", "")]
                if (
                    isinstance(node.func, ast.Call)
                    and isinstance(node.func.func, ast.Attribute)
                    and node.func.func.attr == "__getattribute__"
                    and node.func.args
                    and constant_string(node.func.args[0]) == "get"
                    and node.args
                    and "PRISM_PREVIEW_PORT" in resolved_string_values(node.args[0])
                    and trusted_environ_mapping(node.func.func.value)
                ):
                    return [("port", "")]
                if trusted_call(node, trusted_port_calls) and node.args:
                    first = node.args[0]
                    if isinstance(first, ast.Constant) and first.value == "PRISM_PREVIEW_PORT":
                        return [("port", "")]
                if (
                    isinstance(node.func, ast.Attribute)
                    and node.func.attr == "get"
                    and node.args
                    and "PRISM_PREVIEW_PORT" in resolved_string_values(node.args[0])
                    and trusted_environ_mapping(node.func.value)
                ):
                    return [("port", "")]
                if trusted_call(node, trusted_encoder_calls):
                    return [("encoded", "")]
                if name in {"str", "int"} and node.args:
                    return expression_segments(node.args[0], seen)
                if trusted_call(node, trusted_request_calls):
                    request_url = (
                        node.args[0]
                        if node.args
                        else next(
                            (keyword.value for keyword in node.keywords if keyword.arg == "url"),
                            None,
                        )
                    )
                    return expression_segments(request_url, seen) if request_url is not None else [("unknown", "url")]
                if isinstance(node.func, ast.Name):
                    function = function_defs.get(node.func.id)
                    token = f"call:{node.func.id}"
                    if function is not None and token not in seen:
                        returned = [
                            segment
                            for value in function_return_values(function)
                            for segment in expression_segments(value, seen | {token})
                        ]
                        if returned:
                            return returned
                if isinstance(node.func, ast.Attribute) and node.func.attr in {"join", "format", "format_map"}:
                    values = [node.func.value, *node.args, *(keyword.value for keyword in node.keywords)]
                else:
                    values = [*node.args, *(keyword.value for keyword in node.keywords)]
                segments = [segment for value in values for segment in expression_segments(value, seen)]
                return [*segments, ("unknown", name or "call")]
            if isinstance(node, ast.Dict):
                return [
                    segment
                    for value in [*node.keys, *node.values]
                    if value is not None
                    for segment in expression_segments(value, seen)
                ]
            if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
                return [segment for value in node.elts for segment in expression_segments(value, seen)]
            if isinstance(node, ast.DictComp):
                values = [node.key, node.value, *(generator.iter for generator in node.generators)]
                return [
                    *(segment for value in values for segment in expression_segments(value, seen)),
                    ("unknown", "DictComp"),
                ]
            if isinstance(node, (ast.GeneratorExp, ast.ListComp, ast.SetComp)):
                values = [node.elt, *(generator.iter for generator in node.generators)]
                return [
                    *(segment for value in values for segment in expression_segments(value, seen)),
                    ("unknown", type(node).__name__),
                ]
            return [("unknown", type(node).__name__)]

        request_calls = [
            node for node in ast.walk(tree) if isinstance(node, ast.Call) and trusted_call(node, trusted_urlopen_calls)
        ]
        valid_loopback_request = False
        raw_payload_request = False
        unknown_payload_request = False
        fixed_port_request = False
        for request_call in request_calls:
            request_url = (
                request_call.args[0]
                if request_call.args
                else next(
                    (keyword.value for keyword in request_call.keywords if keyword.arg == "url"),
                    None,
                )
            )
            if request_url is None:
                unknown_payload_request = True
                continue
            segments = expression_segments(request_url)
            rendered = "".join(
                (
                    value
                    if kind in {"literal", "raw"}
                    else "__PORT__"
                    if kind == "port"
                    else "__ENCODED__"
                    if kind == "encoded"
                    else "__UNKNOWN__"
                )
                for kind, value in segments
            )
            has_raw_payload = any(kind == "raw" for kind, _value in segments)
            has_unknown_payload = any(kind == "unknown" for kind, _value in segments)
            has_dynamic_authority = bool(re.search(r"http://127\.0\.0\.1:__PORT__(?:[/?#]|$)", rendered, re.I))
            has_fixed_authority = bool(re.search(r"http://127\.0\.0\.1:\d+(?:[/?#]|$)", rendered, re.I))
            raw_payload_request = raw_payload_request or has_raw_payload
            unknown_payload_request = unknown_payload_request or has_unknown_payload
            fixed_port_request = fixed_port_request or has_fixed_authority
            valid_loopback_request = valid_loopback_request or (
                has_dynamic_authority and not has_raw_payload and not has_unknown_payload and not has_fixed_authority
            )
        if raw_payload_request:
            issues.append(f"{path} 将未编码的探测 payload 直接拼入 urllib URL,必须先做 URL 编码")
        if unknown_payload_request:
            issues.append(f"{path} 将无法追踪的动态值传入 urllib URL,必须先做 URL 编码")
        if fixed_port_request:
            issues.append(f"{path} 回环请求端口不得写死,必须在 URL authority 中实际使用 PRISM_PREVIEW_PORT")
        if not valid_loopback_request:
            issues.append(f"{path} 必须将安全 URL 实际传入使用 PRISM_PREVIEW_PORT 且动态值已编码的 127.0.0.1 回环请求")
    return list(dict.fromkeys(issues))


def _agent_test_cache_key(source_archive_base64: str, language: str, test_mode: str) -> tuple[str, str, str]:
    """按实际传入源码字节计算缓存键，避免环境上的旧 source_sha256 字段被补丁污染后误命中。"""
    source_sha256 = hashlib.sha256(base64.b64decode(source_archive_base64, validate=True)).hexdigest()
    return (source_sha256, language, test_mode)


def _get_cached_agent_test_files(key: tuple[str, str, str]) -> list[dict[str, str]] | None:
    now = time.monotonic()
    with _AGENT_TEST_CACHE_LOCK:
        entry = _AGENT_TEST_CACHE.get(key)
        if entry is None:
            return None
        expires_at, files = entry
        if expires_at <= now:
            _AGENT_TEST_CACHE.pop(key, None)
            return None
        return [dict(item) for item in files]


def _store_agent_test_files(key: tuple[str, str, str], files: list[dict[str, str]]) -> list[dict[str, str]]:
    ttl = int(getattr(settings, "sandbox_agent_test_cache_seconds", 3600) or 3600)
    cached_files = [dict(item) for item in files]
    with _AGENT_TEST_CACHE_LOCK:
        _AGENT_TEST_CACHE[key] = (time.monotonic() + ttl, cached_files)
    return [dict(item) for item in cached_files]


def _generate_agent_test_cases(
    db: Session,
    environment: "SandboxEnvironment",
    source_archive_base64: str,
    language: str,
    test_mode: str,
    *,
    deadline: float | None = None,
) -> list[dict[str, str]] | None:
    """测试执行前调用 LLM 生成白盒/黑盒自包含断言测试文件。

    失败静默(只记事件不阻断):LLM 未配置/生成失败/校验不通过都不影响原测试链。
    返回注入用的文件列表;未生成返回 None。
    """
    try:
        cache_key = _agent_test_cache_key(source_archive_base64, language, test_mode)
        cached_files = _get_cached_agent_test_files(cache_key)
        if cached_files is not None:
            _append_event(
                db,
                environment,
                "progress",
                "agent_tests",
                f"命中进程内测试用例缓存,复用 {len(cached_files)} 个动态用例",
                {"cache_ttl_seconds": settings.sandbox_agent_test_cache_seconds},
            )
            db.commit()
            return cached_files
        from app.agents.base import AgentContext
        from app.agents.test_case_generator_agent import TestCaseGeneratorAgent

        agent = configure_subagent(db, TestCaseGeneratorAgent(), environment.owner_id)
        if not agent._api_key:
            _append_event(db, environment, "progress", "agent_tests", "LLM 未配置,跳过快照 agent 测试用例生成")
            db.commit()
            return None
        summary = _source_summary_for_agent_tests(source_archive_base64, language)
        if not summary.get("coverage_complete"):
            _append_event(
                db,
                environment,
                "progress",
                "agent_tests",
                f"动态测试未执行：源码上下文不完整 ({str(summary.get('coverage_error') or '未知原因')[:140]})",
                {"source_file_count": summary.get("source_file_count")},
            )
            db.commit()
            return None
        environment_config = _loads(getattr(environment, "agent_config_json", None) or "{}", {}) or {}
        environment_config = environment_config if isinstance(environment_config, dict) else {}
        db_type = str(environment_config.get("db_type") or "none")
        team_config = environment_config.get("agent_team")
        team_config = team_config if isinstance(team_config, dict) else {}
        execution_strategy = team_config.get("execution_strategy")
        if isinstance(execution_strategy, dict) and execution_strategy:
            summary["previous_execution_feedback"] = execution_strategy
        ctx = AgentContext(
            user_id=environment.owner_id,
            project_id=environment.project_id,
            extra={"trace_id": environment.public_id, "before_model_call": _execution_model_guard(db, environment)},
        )
        configured_deadline = time.monotonic() + int(
            getattr(settings, "sandbox_agent_test_generation_seconds", 300) or 300
        )
        generation_deadline = min(deadline, configured_deadline) if deadline is not None else configured_deadline
        failure_labels = {
            "source_context": "源码上下文未通过核验",
            "input_budget": "输入超出预算",
            "output_truncated": "模型输出被截断",
            "generation_timeout": "用例生成超时",
            "model_call_failed": "模型调用失败",
            "grounding_rejected": "用例引用未证实的源码符号",
            "schema_invalid": "用例输出结构无效",
            "configuration_invalid": "测试配置不受支持",
            "contract_rejected": "用例未通过执行前契约校验",
            "empty_generation": "模型未生成用例",
            "generation_failed": "用例生成未完成",
        }
        failure_stages: list[str] = []
        from app.agents.source_context import SourceContextError, compact_source_context

        try:
            summary["_compacted_source_context"] = compact_source_context(
                agent,
                summary,
                ctx=ctx,
                deadline=generation_deadline,
                max_chars=40_000,
            )
        except SourceContextError as exc:
            _append_event(
                db,
                environment,
                "progress",
                "agent_tests",
                f"动态测试未执行：源码上下文压缩未能完整核验 ({str(exc)[:140]})",
                {"source_file_count": summary.get("source_file_count"), "source_chunk_count": summary.get("source_chunk_count")},
            )
            db.commit()
            return None
        compacted = summary["_compacted_source_context"]
        _append_event(
            db,
            environment,
            "progress",
            "source_compaction",
            "源码上下文已压缩并完成来源覆盖核验",
            {
                "source_chunk_count": len(compacted.get("covered_source_ids") or []),
                "protected_fact_count": len(compacted.get("protected_facts") or []),
                "compression_calls": (compacted.get("compression") or {}).get("model_calls"),
            },
        )
        db.commit()
        _append_event(
            db,
            environment,
            "progress",
            "agent_tests",
            "正在生成与源码锚定的动态测试用例…",
        )
        db.commit()
        for generation_round in range(1, 4):
            if time.monotonic() >= generation_deadline:
                _append_event(
                    db,
                    environment,
                    "progress",
                    "agent_tests",
                    "测试用例生成超过时间预算，已跳过动态白盒用例（不阻断黑盒与静态验证）",
                    {"generation_timeout_seconds": settings.sandbox_agent_test_generation_seconds},
                )
                db.commit()
                return None
            result = agent.generate(
                language=language,
                test_mode=test_mode,
                source_summary=dict(summary),
                db_type=db_type,
                ctx=ctx,
                deadline=generation_deadline,
            )
            ctx.extra["before_model_call"]()
            files = result.get("files") if isinstance(result, dict) else None
            if not files:
                result_error = result.get("error") if isinstance(result, dict) else "生成结果不是对象"
                failure_kind = result.get("failure_kind") if isinstance(result, dict) else None
                if failure_kind not in failure_labels:
                    failure_kind = "empty_generation" if not result_error else "generation_failed"
                failure_stages.append(failure_kind)
                feedback = str(result_error or "生成结果为空")[:2000]
                summary["previous_generation_feedback"] = feedback
                _append_event(
                    db,
                    environment,
                    "progress",
                    "agent_tests",
                    f"第 {generation_round} 轮动态用例未形成可注入文件（{failure_labels[failure_kind]}）,已反馈重试: {feedback[:120]}",
                    {
                        "generation_round": generation_round,
                        "failure_kind": failure_kind,
                        "issues": [feedback],
                    },
                )
                db.commit()
                continue
            issues = _generated_test_contract_issues(files, language)
            if not issues:
                _append_event(
                    db,
                    environment,
                    "progress",
                    "agent_tests",
                    f"agent 已生成 {len(files)} 个动态测试用例,注入沙箱执行",
                    {
                        "count": len(files),
                        "files": [f.get("path") for f in files],
                        "generation_round": generation_round,
                        "source_archive_sha256": summary.get("source_archive_sha256"),
                        "source_manifest_sha256": summary.get("source_manifest_sha256"),
                        "source_chunk_count": len(summary.get("source_chunks") or []),
                    },
                )
                db.commit()
                return _store_agent_test_files(cache_key, files)
            failure_stages.append("contract_rejected")
            feedback = "；".join(issues)[:2000]
            summary["previous_generation_feedback"] = feedback
            _append_event(
                db,
                environment,
                "progress",
                "agent_tests",
                f"第 {generation_round} 轮动态用例未通过执行前契约校验,已反馈重新生成",
                {"generation_round": generation_round, "issues": issues},
            )
            db.commit()
        _append_event(
            db,
            environment,
            "progress",
            "agent_tests",
            "动态用例连续 3 轮后仍未形成可执行文件（最后原因："
            + failure_labels[failure_stages[-1] if failure_stages else "generation_failed"]
            + "）,本轮回退到确定性测试",
            {
                "failure_kinds": failure_stages,
                "issues": [str(summary.get("previous_generation_feedback") or "生成失败")],
            },
        )
        db.commit()
        return None
    except (ForbiddenError, NotFoundError):
        raise
    except Exception as exc:  # noqa: BLE001 - 生成失败不阻断原测试链
        _append_event(db, environment, "progress", "agent_tests", f"agent 测试用例生成异常: {str(exc)[:120]}")
        db.commit()
        return None


def _generate_deployment_patch(
    db: Session,
    environment: "SandboxEnvironment",
    source_archive_base64: str,
    language: str,
    *,
    deadline: float | None = None,
) -> dict[str, str] | None:
    """完整部署核验:LLM 判断入口/依赖是否完整,生成受控补全启动脚本。

    失败静默(只记事件不阻断):LLM 未配置/生成失败都回退 runner 内置部署逻辑。
    返回 {"launch_script": str, "notes": str} 或 None。
    """
    try:
        from app.agents.base import AgentContext
        from app.agents.deployment_coordinator_agent import DeploymentCoordinatorAgent

        agent = configure_subagent(db, DeploymentCoordinatorAgent(), environment.owner_id)
        if not agent._api_key:
            return None
        summary = _source_summary_for_agent_tests(source_archive_base64, language)
        ctx = AgentContext(
            user_id=environment.owner_id,
            project_id=environment.project_id,
            extra={"trace_id": environment.public_id, "before_model_call": _execution_model_guard(db, environment)},
        )
        db_type = str(
            (_loads(getattr(environment, "agent_config_json", None) or "{}", {}) or {}).get("db_type") or "none"
        )
        result = agent.plan(
            language=language,
            test_mode=str(getattr(environment, "test_mode", "") or "combined"),
            source_summary=summary,
            db_type=db_type,
            ctx=ctx,
            deadline=deadline,
        )
        ctx.extra["before_model_call"]()
        if not isinstance(result, dict) or result.get("error"):
            _append_event(
                db,
                environment,
                "progress",
                "deploy_verify",
                f"部署核验未形成完整计划: {str(result.get('error') if isinstance(result, dict) else '结果格式无效')[:160]}",
            )
            db.commit()
            return None
        launch_script = str(result.get("launch_script") or "").strip() if isinstance(result, dict) else ""
        notes = str(result.get("notes") or "").strip() if isinstance(result, dict) else ""
        if not launch_script:
            if notes:
                _append_event(db, environment, "progress", "deploy_verify", f"部署核验: 入口完整, {notes[:120]}")
                db.commit()
            return None
        _append_event(
            db,
            environment,
            "progress",
            "deploy_verify",
            "部署核验: 生成补全启动脚本 _prism_launch.sh" + (f"({notes[:100]})" if notes else ""),
            {
                "source_archive_sha256": summary.get("source_archive_sha256"),
                "source_manifest_sha256": summary.get("source_manifest_sha256"),
                "source_chunk_count": len(summary.get("source_chunks") or []),
            },
        )
        db.commit()
        return {"launch_script": launch_script, "notes": notes}
    except (ForbiddenError, NotFoundError):
        raise
    except Exception as exc:  # noqa: BLE001 - 补全失败不阻断原测试链
        _append_event(db, environment, "progress", "deploy_verify", f"部署核验异常: {str(exc)[:120]}")
        db.commit()
        return None


def _inject_deployment_patch(source_archive_base64: str, launch_script: str) -> str:
    """把部署补全启动脚本注入源码 zip 的 _prism_launch.sh。"""
    raw = base64.b64decode(source_archive_base64)
    buf = io.BytesIO(raw)
    with zipfile.ZipFile(buf, "a", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("_prism_launch.sh", launch_script)
    return base64.b64encode(buf.getvalue()).decode("ascii")


_SANDBOX_SECRET_ASSIGNMENT_RE = re.compile(
    r"(?P<prefix>(?<![A-Za-z0-9_])['\"]?"
    r"(?:password|passwd|secret|token|api[_-]?key|authorization|cookie|private[_-]?key)"
    r"['\"]?\s*[:=]\s*)"
    r"(?P<value>\"(?:\\.|[^\"\\])*\"|'(?:\\.|[^'\\])*'|"
    r"Bearer\s+[^\s,;\]}&]+|[^\s,;\]}&]+)",
    re.IGNORECASE,
)
_SANDBOX_BEARER_RE = re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]+", re.IGNORECASE)
_SANDBOX_API_KEY_RE = re.compile(r"\bsk-[A-Za-z0-9_-]{8,}\b", re.IGNORECASE)
_SANDBOX_PRIVATE_KEY_RE = re.compile(
    r"-----BEGIN [^-\r\n]*PRIVATE KEY-----[\s\S]*?-----END [^-\r\n]*PRIVATE KEY-----",
    re.IGNORECASE,
)


def _redact_sandbox_output(value: str) -> str:
    """动态测试输出在写入事件、结果与制品前统一脱敏。"""

    redacted = _SANDBOX_PRIVATE_KEY_RE.sub("[REDACTED PRIVATE KEY]", str(value or ""))
    redacted = _SANDBOX_SECRET_ASSIGNMENT_RE.sub(
        lambda match: f"{match.group('prefix')}[REDACTED]",
        redacted,
    )
    redacted = _SANDBOX_BEARER_RE.sub("Bearer [REDACTED]", redacted)
    redacted = _SANDBOX_API_KEY_RE.sub("[REDACTED API KEY]", redacted)
    for secret in sorted(
        (item for item in {settings.sandbox_executor_token} if len(item) >= 8),
        key=len,
        reverse=True,
    ):
        redacted = redacted.replace(secret, "[REDACTED SECRET VALUE]")
    return redacted[-2000:]


def _valid_agent_test_tuple(status: str, phase: str, failure_kind: str, exit_code: int) -> bool:
    if status == "pass":
        return phase == "execute" and not failure_kind and exit_code == 0
    if status != "fail" or exit_code == 0:
        return False
    if failure_kind == "infrastructure_error":
        return phase in {"compile", "execute"} and exit_code in {126, 127}
    if phase == "compile":
        return failure_kind == "compile_error"
    if phase == "execute":
        return failure_kind == "execution_failure"
    if phase == "protocol":
        return failure_kind == "protocol_error" and exit_code == 65
    return False


def _extract_agent_tests_result(log_text: str) -> dict[str, Any] | None:
    """聚合白盒和应用就绪后黑盒的动态测试结果。"""
    pattern = r"PRISM_AGENT_TESTS_BEGIN\s*(\{.*?\})\s*PRISM_AGENT_TESTS_END"
    records: list[dict[str, Any]] = []
    for match in re.finditer(pattern, log_text, re.S):
        try:
            payload = json.loads(match.group(1))
        except (ValueError, TypeError):
            continue
        if isinstance(payload, dict):
            records.append(payload)
    if not records:
        return None

    # runner 会分别输出白盒和应用就绪后的黑盒标记；每个注入文件只能出现一次。
    # 重复文件可能是用户源码伪造标记，必须失败关闭，不能让后一个结果覆盖前一个。
    files: dict[str, str] = {}
    file_results: dict[str, dict[str, Any]] = {}
    protocol_versions: set[int] = set()
    protocol_issues: dict[str, str] = {}
    seen_files: set[str] = set()
    for record in records:
        raw_protocol_version = record.get("protocol_version", 1)
        record_protocol_version = raw_protocol_version if type(raw_protocol_version) is int else 0
        protocol_versions.add(record_protocol_version)
        record_files = record.get("files")
        if not isinstance(record_files, dict):
            continue
        raw_file_results = record.get("file_results")
        raw_file_results = (
            raw_file_results if record_protocol_version == 2 and isinstance(raw_file_results, dict) else {}
        )
        for file_name, status in record_files.items():
            normalized = str(status or "").strip().lower()
            if normalized in {"pass", "fail"}:
                normalized_name = str(file_name)
                if normalized_name in seen_files:
                    files[normalized_name] = "fail"
                    file_results.pop(normalized_name, None)
                    protocol_issues[normalized_name] = "runner 对同一用例返回了重复结果"
                    continue
                seen_files.add(normalized_name)
                files[normalized_name] = normalized
                file_results.pop(normalized_name, None)
                if record_protocol_version not in {1, 2}:
                    protocol_issues[normalized_name] = "runner 返回了不支持的协议版本"
                    continue
                raw_file_result = raw_file_results.get(file_name)
                if not isinstance(raw_file_result, dict):
                    continue
                structured_status = str(raw_file_result.get("status") or "").strip().lower()
                phase = str(raw_file_result.get("phase") or "").strip().lower()
                failure_kind = str(raw_file_result.get("failure_kind") or "").strip().lower()
                if structured_status != normalized or phase not in {"compile", "execute", "protocol"}:
                    protocol_issues[normalized_name] = "runner v2 文件状态或阶段不合法"
                    continue
                raw_exit_code = raw_file_result.get("exit_code")
                if type(raw_exit_code) is not int:
                    protocol_issues[normalized_name] = "runner v2 退出码类型无效"
                    continue
                exit_code = raw_exit_code
                if not -255 <= exit_code <= 255:
                    protocol_issues[normalized_name] = "runner v2 退出码超出协议范围"
                    continue
                if raw_file_result.get("output_encoding") != "base64" or not isinstance(
                    raw_file_result.get("output_base64"), str
                ):
                    protocol_issues[normalized_name] = "runner v2 动态测试输出编码声明无效"
                    continue
                output = ""
                encoded = raw_file_result["output_base64"]
                if len(encoded) > 12_000:
                    protocol_issues[normalized_name] = "runner v2 动态测试输出超出协议上限"
                    continue
                try:
                    output = _redact_sandbox_output(
                        base64.b64decode(encoded, validate=True).decode("utf-8", errors="replace")
                    )
                except (binascii.Error, ValueError, TypeError):
                    protocol_issues[normalized_name] = "runner v2 动态测试输出编码无效"
                    continue
                if not _valid_agent_test_tuple(structured_status, phase, failure_kind, exit_code):
                    protocol_issues[normalized_name] = "runner v2 结构化结果组合不合法"
                    files[normalized_name] = "fail"
                    file_results[normalized_name] = {
                        "status": "fail",
                        "phase": "protocol",
                        "failure_kind": "protocol_error",
                        "exit_code": 65,
                    }
                    continue
                normalized_result = {
                    "status": structured_status,
                    "phase": phase,
                    "failure_kind": failure_kind,
                    "exit_code": exit_code,
                }
                if output:
                    normalized_result["output"] = output
                file_results[normalized_name] = normalized_result

    if len(protocol_versions) != 1:
        for file_name in files:
            protocol_issues[file_name] = "runner 在同一次执行中返回了混合协议版本"
    protocol_version = next(iter(protocol_versions)) if len(protocol_versions) == 1 else 0
    for file_name, issue in protocol_issues.items():
        files[file_name] = "fail"
        file_results[file_name] = {
            "status": "fail",
            "phase": "protocol",
            "failure_kind": "protocol_error",
            "exit_code": 65,
        }
    if files:
        passed_count = sum(1 for status in files.values() if status == "pass")
        failed_count = sum(1 for status in files.values() if status == "fail")
        generated = len(files)
    else:
        passed_count = sum(int(record.get("passed_count") or record.get("passed") or 0) for record in records)
        failed_count = sum(int(record.get("failed") or 0) for record in records)
        generated = passed_count + failed_count
    result: dict[str, Any] = {
        "generated": generated,
        "passed": passed_count,
        "failed": failed_count,
        "passed_count": passed_count,
        "files": files,
        "protocol_version": protocol_version,
    }
    if file_results:
        result["file_results"] = file_results
    if failed_count:
        failure_details = _extract_agent_test_failures(log_text)
        details: dict[str, str] = {}
        for file_name, status in files.items():
            if status != "fail":
                continue
            structured_output = str((file_results.get(file_name) or {}).get("output") or "").strip()
            if structured_output:
                details[file_name] = structured_output
            elif file_name in protocol_issues:
                details[file_name] = protocol_issues[file_name]
            elif file_name in failure_details:
                details[file_name] = _redact_sandbox_output(failure_details[file_name])
        if details:
            result["details"] = details
    return result


def _extract_decompilation_result(log_text: str) -> dict[str, Any] | None:
    """读取受信 runner 的单一反编译结果标记，重复标记失败关闭。"""
    records: list[dict[str, Any]] = []
    for match in re.finditer(r"PRISM_DECOMPILATION_JSON\s+(\{.*?\})", log_text, re.S):
        try:
            payload = json.loads(match.group(1))
        except (TypeError, ValueError):
            continue
        if isinstance(payload, dict):
            records.append(payload)
    if len(records) != 1:
        return None
    result = records[0]
    status = str(result.get("status") or "").strip().lower()
    if status not in {"skipped", "succeeded", "failed"}:
        return None
    try:
        candidate_count = int(result.get("candidate_count") or 0)
        output_file_count = int(result.get("output_file_count") or 0)
    except (TypeError, ValueError):
        return None
    if candidate_count < 0 or output_file_count < 0:
        return None
    raw_exit_code = result.get("exit_code", 0)
    if type(raw_exit_code) is not int or not -255 <= raw_exit_code <= 255:
        return None
    input_sha256 = str(result.get("input_sha256") or "")[:64]
    output_sha256 = str(result.get("output_sha256") or "")[:64]
    if input_sha256 and not re.fullmatch(r"[0-9a-f]{64}", input_sha256):
        return None
    if output_sha256 and not re.fullmatch(r"[0-9a-f]{64}", output_sha256):
        return None
    raw_input_artifact_sha256s = result.get("input_artifact_sha256s")
    input_artifact_sha256s: list[str] = []
    if raw_input_artifact_sha256s is not None:
        if not isinstance(raw_input_artifact_sha256s, list):
            return None
        input_artifact_sha256s = [str(item) for item in raw_input_artifact_sha256s]
        if any(not re.fullmatch(r"[0-9a-f]{64}", item) for item in input_artifact_sha256s):
            return None
    if status == "succeeded" and (not input_sha256 or len(input_artifact_sha256s) != candidate_count):
        return None
    try:
        output_size_bytes = int(result.get("output_size_bytes") or 0)
    except (TypeError, ValueError):
        return None
    if output_size_bytes < 0:
        return None
    raw_artifacts = result.get("artifact_refs")
    artifact_refs = []
    if raw_artifacts is not None:
        if not isinstance(raw_artifacts, list):
            return None
        artifact_refs = [str(item)[:120] for item in raw_artifacts if str(item).strip()]
    return {
        "status": status,
        "tool": str(result.get("tool") or "none")[:40],
        "tool_version": str(result.get("tool_version") or "")[:40],
        "candidate_count": candidate_count,
        "output_file_count": output_file_count,
        "input_sha256": input_sha256,
        "input_artifact_sha256s": input_artifact_sha256s,
        "output_sha256": output_sha256,
        "output_size_bytes": output_size_bytes,
        "exit_code": raw_exit_code,
        "log_ref": str(result.get("log_ref") or "")[:120],
        "artifact_refs": artifact_refs,
        "reason": str(result.get("reason") or "")[:300],
    }


def _extract_blackbox_result(log_text: str) -> dict[str, Any] | None:
    """Read the runner's isolated smoke-test receipt; duplicate or malformed receipts are invalid."""
    records: list[dict[str, Any]] = []
    for match in re.finditer(r"PRISM_BLACKBOX_DONE\s*(\{[^\n]*\})", str(log_text or "")):
        try:
            value = json.loads(match.group(1))
        except (TypeError, ValueError):
            continue
        if isinstance(value, dict):
            records.append(value)
    if len(records) != 1:
        return None
    result = records[0]
    route = str(result.get("route") or "")
    status_code = result.get("status_code")
    route_passed = result.get("route_passed")
    agent_assertions_passed = result.get("agent_assertions_passed")
    basis = str(result.get("basis") or "route_and_agent_assertions")
    failure_kind = result.get("failure_kind")
    failure_reason = result.get("failure_reason")
    route_status_passed = type(status_code) is int and 200 <= status_code < 300
    startup_failure = failure_kind == "application_startup"
    if (
        result.get("executed") is not True
        or type(result.get("passed")) is not bool
        or type(route_passed) is not bool
        or (agent_assertions_passed is not None and type(agent_assertions_passed) is not bool)
        or basis not in {"route_smoke", "route_and_agent_assertions"}
        or (basis == "route_and_agent_assertions" and agent_assertions_passed is None)
        or not re.fullmatch(r"/[A-Za-z0-9._~!$&+,;=@%/?-]*", route)
        or type(status_code) is not int
        or not 0 <= status_code <= 599
        or route_passed != route_status_passed
        or (result["passed"] and not route_passed)
        or (result["passed"] and agent_assertions_passed is False)
        or (basis == "route_smoke" and agent_assertions_passed is not None)
        or (failure_kind is not None and not startup_failure)
        or (
            startup_failure
            and (
                result["passed"]
                or route_passed
                or status_code != 0
                or agent_assertions_passed is not None
                or failure_reason not in {"application_exited_before_ready", "application_readiness_timeout"}
            )
        )
        or (failure_reason is not None and not startup_failure)
    ):
        return None
    failure_kind = None
    if not result["passed"]:
        failure_kind = (
            "application_startup"
            if startup_failure
            else (
                "dynamic_assertion"
                if route_passed and agent_assertions_passed is False
                else "route_or_dynamic_assertion"
            )
        )
    if result["passed"]:
        status = "passed"
    elif (
        type(status_code) is int
        and (300 <= status_code < 400 or status_code in {401, 403})
        and agent_assertions_passed is not False
    ):
        status = "partial"
    else:
        status = "failed"
    receipt = {
        "status": status,
        "route_passed": route_passed,
        "route": route,
        "status_code": status_code,
        "basis": basis,
        "agent_assertions_passed": agent_assertions_passed,
        "failure_kind": failure_kind,
    }
    if startup_failure:
        receipt["failure_reason"] = failure_reason
    return receipt


def _remote_blackbox_execution(remote_probe: dict[str, Any]) -> dict[str, Any]:
    """A remote redirect or 4xx proves reachability, not a successful final route check."""
    status_code = remote_probe.get("status_code")
    if type(status_code) is not int or not 100 <= status_code <= 599:
        raise RuntimeError("远程黑盒探测没有返回有效 HTTP 状态码")
    route_passed = 200 <= status_code < 300
    status = "passed" if route_passed else ("partial" if status_code < 500 else "failed")
    target = str(remote_probe.get("target_origin") or "")
    try:
        route = urllib.parse.urlsplit(target).path or "/"
    except ValueError:
        route = "/"
    if not re.fullmatch(r"/[A-Za-z0-9._~!$&+,;=@%/?-]*", route):
        route = "/"
    return {
        "status": status,
        "route_passed": route_passed,
        "route": route,
        "status_code": status_code,
        "basis": "authorized_remote_http_probe",
    }


def _sandbox_verification_coverage(conclusion: dict[str, Any]) -> dict[str, Any]:
    """Summarize independently recorded deterministic and AI-generated verification scope."""
    evidence = conclusion.get("evidence") if isinstance(conclusion.get("evidence"), dict) else {}
    deterministic = evidence.get("deterministic_test_execution")
    deterministic = deterministic if isinstance(deterministic, dict) else {}
    generation = evidence.get("agent_test_generation")
    generation = generation if isinstance(generation, dict) else {}
    dynamic = evidence.get("agent_tests")
    dynamic = dynamic if isinstance(dynamic, dict) else {}
    mode = str(deterministic.get("requested_mode") or generation.get("mode") or "")
    passed = bool(conclusion.get("passed"))
    dynamic_state = str(generation.get("status") or "unknown")
    if dynamic_state == "generated":
        if dynamic:
            dynamic_state = "passed" if _agent_tests_succeeded(dynamic) else "failed"
        else:
            dynamic_state = "receipt_missing"
    elif dynamic_state == "skipped":
        dynamic_state = "skipped"
    else:
        dynamic_state = "unknown"
    blackbox = evidence.get("blackbox_execution")
    blackbox = blackbox if isinstance(blackbox, dict) else {}
    blackbox_state = str(blackbox.get("status") or "not_required")
    if mode in {"blackbox", "combined"} and blackbox_state == "not_required":
        blackbox_state = "unknown"
    remote = evidence.get("remote_blackbox")
    remote = remote if isinstance(remote, dict) else {}
    remote_status_code = remote.get("status_code")
    if type(remote_status_code) is int and 200 <= remote_status_code < 300:
        remote_state = "passed"
    elif type(remote_status_code) is int and 300 <= remote_status_code < 500:
        remote_state = "partial"
    elif type(remote_status_code) is int and 500 <= remote_status_code <= 599:
        remote_state = "failed"
    else:
        remote_state = "unknown" if remote else "not_required"
    verification_status = "complete"
    explicit_failure = (
        deterministic.get("status") == "failed"
        or dynamic_state in {"failed", "receipt_missing"}
        or blackbox_state == "failed"
        or remote_state == "failed"
    )
    if explicit_failure:
        verification_status = "failed"
    elif remote_state == "partial" or blackbox_state == "partial":
        # 重定向或客户端响应只能证明入口有响应，不能证明最终业务路由已通过验证。
        verification_status = "partial"
    elif not passed:
        verification_status = "failed"
    elif (
        deterministic.get("status") != "passed"
        or dynamic_state in {"skipped", "unknown"}
        or (mode in {"blackbox", "combined"} and blackbox_state != "passed")
        or remote_state in {"partial", "unknown"}
    ):
        verification_status = "partial"
    elif mode in {"blackbox", "combined"} and blackbox_state != "passed":
        verification_status = "failed" if blackbox_state == "failed" else "partial"
    return {
        "verification_status": verification_status,
        "requested_mode": mode or None,
        "deterministic_status": str(deterministic.get("status") or "unknown"),
        "ai_dynamic_status": dynamic_state,
        "blackbox_status": blackbox_state,
        "remote_blackbox_status": remote_state,
        "reason": (
            "AI 动态测试未执行，结果只覆盖确定性 runner 已执行范围"
            if verification_status == "partial" and dynamic_state == "skipped"
            else "黑盒目标返回 3xx/4xx；入口有响应，但最终业务路由未完成验证"
            if verification_status == "partial" and (remote_state == "partial" or blackbox_state == "partial")
            else None
        ),
    }


def _agent_tests_succeeded(result: dict[str, Any] | None) -> bool:
    """没有动态用例时沿用基础测试；生成后必须全部通过。"""
    if not isinstance(result, dict):
        return True
    generated = int(result.get("generated") or 0)
    if generated <= 0:
        return True
    if result.get("missing") or result.get("unexpected"):
        return False
    passed_count = int(result.get("passed_count") or result.get("passed") or 0)
    failed_count = int(result.get("failed") or 0)
    return failed_count == 0 and passed_count == generated


def _reconcile_blackbox_agent_assertions(
    result: dict[str, Any] | None,
    *,
    generation: dict[str, Any] | None,
    expected_files: set[str],
    agent_tests_result: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """只根据后端注入契约和逐文件可信回执认定 AI 黑盒断言状态。"""
    if not isinstance(result, dict):
        return None
    reconciled = dict(result)
    generation = generation if isinstance(generation, dict) else {}
    generated_blackbox_files = {name for name in expected_files if name.rsplit("/", 1)[-1].startswith("blackbox.")}
    assertions: bool | None = None
    startup_failure = result.get("failure_kind") == "application_startup"
    blackbox_contract_invalid = (
        not startup_failure
        and generation.get("status") == "generated"
        and generation.get("mode") in {"blackbox", "combined"}
        and len(generated_blackbox_files) != 1
    )
    if not startup_failure and result.get("agent_assertions_passed") is False:
        # A valid runner receipt can prove an assertion failed even when the
        # generation record is stale or says "skipped". Never erase that failure.
        assertions = False
    elif (
        not startup_failure
        and not blackbox_contract_invalid
        and generation.get("status") == "generated"
        and len(generated_blackbox_files) == 1
    ):
        blackbox_file = next(iter(generated_blackbox_files))
        files = agent_tests_result.get("files") if isinstance(agent_tests_result, dict) else None
        assertions = isinstance(files, dict) and files.get(blackbox_file) == "pass"

    status_code = result.get("status_code")
    route_passed = type(status_code) is int and 200 <= status_code < 300
    if startup_failure:
        status = "failed"
        failure_kind = "application_startup"
    elif blackbox_contract_invalid:
        # A generated blackbox run without exactly one injected blackbox file
        # is a broken test contract, not a successful route-only verification.
        status = "failed"
        failure_kind = "agent_assertion_contract"
    elif assertions is False:
        status = "failed"
        failure_kind = "dynamic_assertion" if route_passed else "route_or_dynamic_assertion"
    elif route_passed:
        status = "passed"
        failure_kind = None
    elif type(status_code) is int and (300 <= status_code < 400 or status_code in {401, 403}):
        status = "partial"
        failure_kind = None
    else:
        status = "failed"
        failure_kind = "route_or_dynamic_assertion"

    reconciled.update(
        {
            "status": status,
            "passed": status == "passed",
            "basis": "route_and_agent_assertions" if assertions is not None else "route_smoke",
            "agent_assertions_passed": assertions,
            "failure_kind": failure_kind,
        }
    )
    if not startup_failure:
        reconciled.pop("failure_reason", None)
    return reconciled


def _reconcile_agent_tests_result(
    expected_files: set[str],
    result: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """已注入用例必须逐个返回可信状态，缺失或越界均失败关闭。"""
    if not expected_files:
        # 没有后端注入契约时，不信任用户源码输出的同名标记。
        return None
    reconciled = dict(result or {})
    raw_files = reconciled.get("files") if isinstance(reconciled.get("files"), dict) else {}
    files = {
        str(file_name): str(status or "").strip().lower()
        for file_name, status in raw_files.items()
        if str(status or "").strip().lower() in {"pass", "fail"}
    }
    actual_files = set(files)
    missing = sorted(expected_files - actual_files)
    unexpected = sorted(actual_files - expected_files)
    details = dict(reconciled.get("details") or {}) if isinstance(reconciled.get("details"), dict) else {}
    raw_file_results = reconciled.get("file_results") if isinstance(reconciled.get("file_results"), dict) else {}
    raw_protocol_version = reconciled.get("protocol_version")
    protocol_version = raw_protocol_version if type(raw_protocol_version) is int else 0
    file_results: dict[str, dict[str, Any]] = {}
    for file_name, status in files.items():
        if protocol_version != 2:
            files[file_name] = "fail"
            details[file_name] = "runner 未返回精确的动态测试协议 v2"
            file_results[file_name] = {
                "status": "fail",
                "phase": "protocol",
                "failure_kind": "protocol_error",
                "exit_code": 65,
            }
            continue
        raw_file_result = raw_file_results.get(file_name)
        if not isinstance(raw_file_result, dict):
            if protocol_version == 2:
                files[file_name] = "fail"
                details[file_name] = "runner v2 未返回该用例的结构化文件结果"
                file_results[file_name] = {
                    "status": "fail",
                    "phase": "protocol",
                    "failure_kind": "protocol_error",
                    "exit_code": 65,
                }
            continue
        structured_status = str(raw_file_result.get("status") or "").strip().lower()
        if structured_status != status:
            files[file_name] = "fail"
            details[file_name] = "runner 文件状态与结构化结果不一致"
            file_results[file_name] = {
                "status": "fail",
                "phase": "protocol",
                "failure_kind": "protocol_error",
                "exit_code": 65,
            }
            continue
        file_results[file_name] = dict(raw_file_result)
    for file_name in missing:
        files[file_name] = "fail"
        details[file_name] = "runner 未返回该已注入用例的可信结果标记"
        file_results[file_name] = {
            "status": "fail",
            "phase": "protocol",
            "failure_kind": "protocol_error",
            "exit_code": 65,
        }
    for file_name in unexpected:
        files[file_name] = "fail"
        details[file_name] = "runner 返回了不在注入契约中的用例标记"
        file_results[file_name] = {
            "status": "fail",
            "phase": "protocol",
            "failure_kind": "protocol_error",
            "exit_code": 65,
        }
    passed_count = sum(1 for status in files.values() if status == "pass")
    failed_count = sum(1 for status in files.values() if status == "fail")
    reconciled.update(
        {
            "generated": len(files),
            "passed": passed_count,
            "passed_count": passed_count,
            "failed": failed_count,
            "files": files,
            "file_results": file_results,
        }
    )
    if missing:
        reconciled["missing"] = missing
    if unexpected:
        reconciled["unexpected"] = unexpected
    if details:
        reconciled["details"] = details
    return reconciled


def _extract_agent_test_failures(log_text: str) -> dict[str, str]:
    """从容器日志提取每个失败 agent 用例的执行输出(供存档审查)。"""
    details: dict[str, str] = {}
    boundary = r"(?=\n(?:\S+Z\s*)?agent test failed:|(?:\S+Z\s*)?PRISM_AGENT_TESTS_BEGIN|(?:\S+Z\s*)?PRISM_VERIFY|(?:\S+Z\s*)?PRISM_AGENT_TESTS_END|$)"  # noqa: E501
    for match in re.finditer(r"agent test failed: ([^\s]+)\s*\n(.*?)" + boundary, log_text, re.S):
        file_name = match.group(1).split("/")[-1]
        output = match.group(2).strip()[-2000:]
        if file_name and output:
            details[file_name] = output
    return details


def _fact_gate_report(report_md: str, conclusion: dict[str, Any]) -> str:
    """以确定性执行事实覆盖模型可能写错的总体结论。"""
    passed = bool(conclusion.get("passed"))
    coverage = _sandbox_verification_coverage(conclusion)
    agent_tests = conclusion.get("agent_tests") if isinstance(conclusion.get("agent_tests"), dict) else {}
    generated = int(agent_tests.get("generated") or 0)
    passed_count = int(agent_tests.get("passed_count") or agent_tests.get("passed") or 0)
    failed_count = int(agent_tests.get("failed") or 0)
    if coverage["verification_status"] == "complete":
        gate = "通过"
        gate_label = "系统验证门禁"
    elif coverage["verification_status"] == "partial":
        gate = "部分通过（验证范围不完整）"
        gate_label = "系统验证门禁"
    else:
        gate = "未通过"
        gate_label = "系统验证门禁"
    facts = [f"**{gate_label}：{gate}。**", f"确定性执行结论：{str(conclusion.get('summary') or ('通过' if passed else '未通过'))}。"]
    facts.append(
        f"覆盖状态：确定性检查={coverage['deterministic_status']}，"
        f"AI 动态用例={coverage['ai_dynamic_status']}，黑盒路由={coverage['blackbox_status']}。"
    )
    if coverage.get("reason"):
        facts.append(f"范围说明：{coverage['reason']}。")
    if generated:
        facts.append(f"动态用例 {generated} 个，通过 {passed_count} 个，失败 {failed_count} 个。")
    facts.append("后续分析若与本段结构化事实冲突，以本段为准。")
    canonical = "## 总体结论\n\n" + " ".join(facts)
    remainder = re.sub(
        r"(?ms)^## 总体结论\s*.*?(?=^## |\Z)",
        "",
        str(report_md or ""),
    ).lstrip()
    evidence = conclusion.get("evidence") if isinstance(conclusion.get("evidence"), dict) else {}
    decompilation = evidence.get("decompilation") if isinstance(evidence.get("decompilation"), dict) else None
    if decompilation:
        status = str(decompilation.get("status") or "unknown")
        details = [f"状态：`{status}`", f"工具：`{str(decompilation.get('tool') or 'none')}`"]
        if decompilation.get("tool_version"):
            details.append(f"版本：`{decompilation['tool_version']}`")
        if decompilation.get("input_sha256"):
            details.append(f"输入清单 SHA-256：`{decompilation['input_sha256']}`")
        if decompilation.get("input_artifact_sha256s"):
            artifacts = "、".join(f"`{digest}`" for digest in decompilation["input_artifact_sha256s"])
            details.append(f"原始制品 SHA-256：{artifacts}")
        if decompilation.get("output_sha256"):
            details.append(f"派生源码 SHA-256：`{decompilation['output_sha256']}`")
        if "output_file_count" in decompilation:
            details.append(f"派生源码文件：`{decompilation['output_file_count']}`")
        if "output_size_bytes" in decompilation:
            details.append(f"派生源码字节数：`{decompilation['output_size_bytes']}`")
        if "exit_code" in decompilation:
            details.append(f"退出码：`{decompilation['exit_code']}`")
        if decompilation.get("log_ref"):
            details.append(f"日志制品：`{decompilation['log_ref']}`")
        if decompilation.get("artifact_refs"):
            details.append(f"证据制品：`{', '.join(decompilation['artifact_refs'])}`")
        canonical += "\n\n## 反编译证据\n\n" + "；".join(details) + "。"
    return canonical + ("\n\n" + remainder if remainder else "")


def _build_deterministic_test_report(conclusion: dict[str, Any]) -> str:
    """模型不可用时生成可导出的确定性报告，禁止把缺少模型伪装成成功。"""
    passed = bool(conclusion.get("passed"))
    agent_tests = conclusion.get("agent_tests") if isinstance(conclusion.get("agent_tests"), dict) else {}
    generated = int(agent_tests.get("generated") or 0)
    passed_count = int(agent_tests.get("passed_count") or agent_tests.get("passed") or 0)
    failed_count = int(agent_tests.get("failed") or 0)
    coverage = _sandbox_verification_coverage(conclusion)
    report = (
        "## 总体结论\n\n"
        f"系统验证门禁：{coverage['verification_status']}。\n\n"
        "## 执行摘要\n\n"
        f"Worker 结论：{str(conclusion.get('summary') or ('测试通过' if passed else '测试未通过'))}。\n"
        f"动态用例：生成 {generated} 个，通过 {passed_count} 个，失败 {failed_count} 个。\n"
        f"确定性执行：{coverage['deterministic_status']}；AI 动态用例：{coverage['ai_dynamic_status']}；"
        f"黑盒路由：{coverage['blackbox_status']}。\n\n"
        "## 问题清单\n\n"
        + ("- 反编译或白盒执行未通过，详见执行日志与结构化证据。\n" if not passed else "- 未发现确定性执行失败。\n")
    )
    return _fact_gate_report(report, conclusion)


def _run_test_review_report(
    db: Session,
    environment: SandboxEnvironment,
    conclusion: dict[str, Any],
) -> dict[str, Any] | None:
    """黑白盒测试终态后,调用多Agent审查编排产出中文报告并落制品。

    失败静默(只记事件不阻断):LLM 未配置/超时/任一角色异常都不影响测试结论。
    返回写入 result_json 的摘要 dict,未执行或失败返回 None。
    """
    try:
        from app.agents.base import AgentContext
        from app.agents.test_review_reporter_agent import TestReviewReporterAgent

        agent = configure_subagent(db, TestReviewReporterAgent(), environment.owner_id)
        data: dict[str, Any] = {}
        roles: dict[str, Any] = {}
        report_md = _build_deterministic_test_report(conclusion)
        if not agent._api_key:
            _append_event(db, environment, "progress", "multi_agent_review", "LLM 未配置，已使用确定性测试报告兜底")
        else:
            ctx = AgentContext(
                user_id=environment.owner_id,
                project_id=environment.project_id,
                extra={"trace_id": environment.public_id, "before_model_call": _execution_model_guard(db, environment)},
            )
            result = agent.review(db, environment=environment, conclusion=conclusion, ctx=ctx)
            ctx.extra["before_model_call"]()
            if result.success:
                data = result.data if isinstance(result.data, dict) else {}
                candidate = _fact_gate_report(str(data.get("report_md") or ""), conclusion)
                if candidate.strip():
                    report_md = candidate
                roles = data.get("roles") if isinstance(data.get("roles"), dict) else {}
            else:
                _append_event(
                    db,
                    environment,
                    "progress",
                    "multi_agent_review",
                    f"多 Agent 测试审查未生成，已使用确定性报告兜底: {str(result.error)[:120]}",
                )
        artifact = _persist_browser_artifact(
            db,
            environment,
            artifact_type="review_report",
            file_name=f"sandbox-review-report-{environment.public_id}.md",
            mime_type="text/markdown",
            content=report_md.encode("utf-8", errors="replace"),
        )
        summary = {
            "agent_code": "test_reviewer",
            "artifact_id": artifact.id,
            "roles_executed": int(data.get("roles_executed") or 0),
            "roles": {k: {"ok": bool(v.get("ok"))} for k, v in roles.items() if isinstance(v, dict)},
            "report_bytes": len(report_md.encode("utf-8")),
        }
        published = _publish_sandbox_report(db, environment, conclusion, report_md)
        if published:
            summary["report_task_id"] = published.get("report_task_id")
            summary["issues"] = published.get("issues", 0)
        _append_event(
            db,
            environment,
            "complete",
            "multi_agent_review",
            f"多 Agent 测试审查报告已生成(角色 {summary['roles_executed']}/4)",
            summary,
        )
        db.commit()
        return summary
    except (ForbiddenError, NotFoundError):
        raise
    except Exception as exc:  # noqa: BLE001 - 审查增强失败不阻断测试结论
        _append_event(db, environment, "progress", "multi_agent_review", f"多 Agent 测试审查异常: {str(exc)[:120]}")
        db.commit()
        return None


def _publish_sandbox_report(
    db: Session,
    environment: "SandboxEnvironment",
    conclusion: dict[str, Any],
    report_md: str,
) -> dict[str, Any]:
    """把沙箱多 Agent 审查报告正式入库"审查报告"中心(ReviewTask+ReviewReport)。

    幂等:按 public_id 写入 task_name 查重,重复执行只更新不重复建。
    失败静默,不阻断测试结论。
    """
    try:
        from datetime import datetime
        from datetime import timezone as _tz

        from app.models.review_report import ReviewReport
        from app.models.review_task import ReviewTask

        owner_id = int(getattr(environment, "owner_id", 0) or 0)
        project_id = int(getattr(environment, "project_id", 0) or 0)
        public_id = str(getattr(environment, "public_id", "") or "")
        passed = bool(conclusion.get("passed"))
        task_name = f"沙箱黑白盒测试 · {public_id}"
        from app.services.sandbox_report_summary import summarize_sandbox_report

        report_issue_summary = summarize_sandbox_report(report_md)
        verification = _sandbox_verification_coverage(conclusion)
        verification_status = verification["verification_status"]
        # 旧列不可为空；未知与报告来源另存结构化摘要，详情不把该占位零当真实结论。
        issue_count = report_issue_summary["total"] or 0
        severity_counts = report_issue_summary["severity_counts"]
        task = (
            db.query(ReviewTask)
            .filter(ReviewTask.task_name == task_name, ReviewTask.review_type == "sandbox_test")
            .first()
        )
        now = datetime.now(_tz.utc).replace(tzinfo=None)
        started_at = getattr(environment, "started_at", None) or now
        stopped_at = getattr(environment, "stopped_at", None) or now
        if getattr(started_at, "tzinfo", None) is not None:
            started_at = started_at.replace(tzinfo=None)
        if getattr(stopped_at, "tzinfo", None) is not None:
            stopped_at = stopped_at.replace(tzinfo=None)
        duration_ms = max(0, int((stopped_at - started_at).total_seconds() * 1000))
        # 评分由确定性用例通过率决定，禁止把“runner 正常退出”误当成“测试全部通过”。
        agent_tests = conclusion.get("agent_tests") if isinstance(conclusion.get("agent_tests"), dict) else {}
        generated = int(agent_tests.get("generated") or 0)
        passed_count = int(agent_tests.get("passed_count") or agent_tests.get("passed") or 0)
        if passed:
            report_score = 100
        elif generated:
            report_score = round(60 * passed_count / generated)
        else:
            report_score = 0
        if task is None:
            task = ReviewTask(
                user_id=owner_id,
                **model_attribution(environment),
                project_id=project_id,
                task_name=task_name,
                review_type="sandbox_test",
                status="success" if passed else "failed",
                total_files=1,
                processed_files=1,
                total_issues=issue_count,
                severe_issues=severity_counts["严重"],
                high_issues=severity_counts["高"],
                medium_issues=severity_counts["中"],
                low_issues=severity_counts["低"],
                score=report_score,
                summary=report_md,
                start_time=started_at,
                end_time=stopped_at,
                duration_ms=duration_ms,
                coverage={
                    "stage": "complete" if verification_status == "complete" else verification_status,
                    **verification,
                },
            )
            db.add(task)
        else:
            task.project_id = project_id
            task.status = "success" if passed else "failed"
            task.total_issues = issue_count
            task.severe_issues = severity_counts["严重"]
            task.high_issues = severity_counts["高"]
            task.medium_issues = severity_counts["中"]
            task.low_issues = severity_counts["低"]
            task.score = report_score
            task.summary = report_md
            task.start_time = started_at
            task.end_time = stopped_at
            task.duration_ms = duration_ms
            task.coverage = {
                "stage": "complete" if verification_status == "complete" else verification_status,
                **verification,
            }
            origin = model_attribution(environment)
            for field in ATTRIBUTION_FIELDS:
                setattr(task, field, origin.get(field))
        db.flush()
        report_row = db.query(ReviewReport).filter(ReviewReport.task_id == task.id).first()
        if report_row is None:
            report_row = ReviewReport(
                task_id=task.id,
                user_id=owner_id,
                content_json={
                    "source": "sandbox_test",
                    "public_id": public_id,
                    "report_md": report_md,
                    "report_issue_summary": report_issue_summary,
                    "evidence": conclusion.get("evidence", {}),
                },
                summary=report_md[:2000],
                score=report_score,
                create_time=now,
            )
            db.add(report_row)
        else:
            report_row.content_json = {
                "source": "sandbox_test",
                "public_id": public_id,
                "report_md": report_md,
                "report_issue_summary": report_issue_summary,
                "evidence": conclusion.get("evidence", {}),
            }
            report_row.summary = report_md[:2000]
            report_row.score = report_score
        db.commit()
        return {"report_task_id": task.id, "issues": issue_count}
    except Exception:  # noqa: BLE001 - 入库失败不阻断测试结论
        db.rollback()
        return {}


def artifact_to_dict(row: SandboxArtifact, *, download_allowed: bool = True) -> dict[str, Any]:
    return {
        "id": row.id,
        "artifact_type": row.artifact_type,
        "file_name": row.file_name,
        "mime_type": row.mime_type,
        "byte_size": row.byte_size,
        "sha256": row.sha256,
        "download_allowed": download_allowed,
    }


def _profile_policy(language: str) -> dict[str, Any]:
    policy = dict(_PROFILE_POLICIES.get(language, {}))
    policy.update(_RESOURCE_POLICY_COMMON)
    return policy


def _normalize_source_archive_for_worker(source_archive: bytes, filename: str) -> tuple[bytes, str]:
    """将 worker 的源码快照统一为 ZIP,兼容 GitHub 常见 tar.gz/tgz 输入。

    worker/executor 的协议只接收无文件名的 base64,因此不能把归档后缀交给
    worker 自己判断。先用统一安全读取器校验路径、链接和解压倍率,再重建为
    普通 ZIP;原始归档的反编译计划和摘要仍由调用方单独保留。
    """
    lower = (filename or "").lower()
    if lower.endswith((".zip", ".apk", ".aab")):
        return source_archive, filename
    members, _ = read_archive_members(
        source_archive,
        filename,
        filter_sensitive=False,
        strict_paths=True,
    )
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for member in members:
            archive.writestr(member.path, member.content)
    return output.getvalue(), "source-normalized.zip"


def _project_deployment_language(project_language: str | None) -> str | None:
    """将项目主语言映射到固定沙箱 profile。

    部署环境只能运行一个受控 profile，因此以项目已保存的主语言为
    权威来源；前端、MCP 或 Agent 传入的过期语言不得覆盖它。
    """

    normalized = str(project_language or "").strip().lower().replace("_", "").replace("-", "")
    exact = _PROJECT_LANGUAGE_TO_RUNTIME.get(normalized)
    if exact:
        return exact
    compact = re.sub(r"[^a-z0-9]+", "", normalized)
    for alias, runtime in _PROJECT_LANGUAGE_COMPACT_ALIASES:
        if compact == alias or (compact.startswith(alias) and compact[len(alias) :].isdigit()):
            return runtime
    return None


def _dominant_archive_language(source_archive_base64: str) -> str | None:
    """从待测源码压缩包推断主语言(按可审计源码文件数)。

    仅读取文件名清单(不解压到磁盘、不执行内容),安全且开销可忽略。
    隔离源码项目建档语言常与真实源码不符,白盒/黑盒测试据此纠正运行时。
    """
    try:
        raw = base64.b64decode(source_archive_base64)
        counts: dict[str, int] = {}
        with zipfile.ZipFile(io.BytesIO(raw)) as bundle:
            for info in bundle.infolist():
                if info.is_dir():
                    continue
                language = detect_language(info.filename)
                if language in {"plaintext", "binary"}:
                    continue
                counts[language] = counts.get(language, 0) + 1
        if not counts:
            return None
        dominant = max(counts.items(), key=lambda item: item[1])[0]
        return dominant if dominant in LANGUAGES else None
    except Exception:
        return None


def _worker_token(worker: SandboxWorker) -> str:
    if not worker.encrypted_token:
        return settings.sandbox_executor_token if worker.transport == "unix" else ""
    decryption = decrypt_api_key_with_metadata(worker.encrypted_token)
    if not decryption:
        raise RuntimeError("Sandbox worker 令牌无法解密")
    if decryption.needs_rotation:
        worker.encrypted_token = encrypt_api_key(decryption.plaintext)
        session = object_session(worker)
        if session is not None:
            session.flush()
    return decryption.plaintext


def _assert_worker_endpoint(transport: str, endpoint: str) -> str:
    value = (endpoint or "").strip()
    if transport == "unix":
        # 生产 socket 位于持久 StateDirectory:/run 是 private propagation,
        # tmpfs RuntimeDirectory 重建会让已运行容器内的 socket 引用失效,
        # 因此生产 socket 不放在 /run。
        production_path = value.startswith(("/run/prism-sandbox/", "/var/lib/prism-sandbox/"))
        local_path = settings.sandbox_mode == "local_development" and value.startswith("/tmp/prism-sandbox/")
        if not (production_path or local_path) or not value.endswith(".sock"):
            raise ValidationError("Unix worker 路径不在允许目录内", code=40001)
        return value
    target = pin_public_http_url(value, require_https=True)
    if target.original_url.rstrip("/") != value.rstrip("/"):
        raise ValidationError("Worker 地址格式无效", code=40001)
    return value.rstrip("/")


def upsert_worker(
    db: Session,
    actor: User,
    payload: dict[str, Any],
    worker_id: int = 0,
) -> SandboxWorker:
    row = db.get(SandboxWorker, worker_id) if worker_id else None
    if not row:
        row = db.query(SandboxWorker).filter(SandboxWorker.code == payload["code"]).first()
    if not row:
        row = SandboxWorker(code=payload["code"], name=payload["name"])
        db.add(row)
    languages = list(dict.fromkeys(payload["supported_languages"]))
    modes = list(dict.fromkeys(payload["supported_modes"]))
    if not languages or any(item not in LANGUAGES for item in languages):
        raise ValidationError("Worker 语言配置无效", code=40001)
    if not modes or any(item not in MODES for item in modes):
        raise ValidationError("Worker 模式配置无效", code=40001)
    row.code = payload["code"]
    row.name = payload["name"]
    row.worker_type = payload["worker_type"]
    row.transport = payload["transport"]
    row.endpoint = _assert_worker_endpoint(row.transport, payload["endpoint"])
    if payload.get("token"):
        row.encrypted_token = encrypt_api_key(payload["token"])
    row.supported_languages_json = _json(languages)
    row.supported_modes_json = _json(modes)
    row.runtime = payload.get("runtime") or "runsc"
    row.max_concurrency = int(payload.get("max_concurrency") or 1)
    row.priority = int(payload.get("priority") or 50)
    row.enabled = int(bool(payload.get("enabled")))
    row.status = "unknown" if row.enabled else "disabled"
    audit_service.log(
        db,
        actor,
        "sandbox_worker_upsert",
        target_type="sandbox_worker",
        target_id=row.code,
        detail=f"type={row.worker_type}; transport={row.transport}; runtime={row.runtime}; enabled={row.enabled}",
        commit=False,
    )
    db.commit()
    db.refresh(row)
    return row


def worker_to_dict(row: SandboxWorker) -> dict[str, Any]:
    return {
        "id": row.id,
        "code": row.code,
        "name": row.name,
        "worker_type": row.worker_type,
        "transport": row.transport,
        "endpoint": row.endpoint,
        "supported_languages": _loads(row.supported_languages_json, []),
        "supported_modes": _loads(row.supported_modes_json, []),
        "runtime": row.runtime,
        "max_concurrency": row.max_concurrency,
        "priority": row.priority,
        "status": row.status,
        "enabled": bool(row.enabled),
        "last_seen_at": row.last_seen_at,
        "last_error": row.last_error,
        "fingerprint": _loads(row.fingerprint_json, {}),
    }


def list_workers(db: Session) -> list[dict[str, Any]]:
    rows = db.query(SandboxWorker).order_by(SandboxWorker.priority, SandboxWorker.id).all()
    return [worker_to_dict(row) for row in rows]


def _worker_http_error(response: httpx.Response, path: str) -> RuntimeError:
    """Return a bounded, redacted worker error suitable for persisted task diagnostics."""
    detail = ""
    try:
        body = response.json()
    except (ValueError, UnicodeDecodeError):
        body = None
    if isinstance(body, dict):
        for key in ("error", "detail", "message", "code"):
            value = body.get(key)
            if isinstance(value, str) and value.strip():
                detail = value.strip()
                break
    if detail:
        try:
            from app.services.agent_responses_service import redact_agent_output_text

            detail = redact_agent_output_text(detail)
        except Exception:  # noqa: BLE001 - diagnostics must not mask the HTTP failure
            detail = re.sub(r"(?i)(bearer\s+)[A-Za-z0-9._~+/-]{8,}", r"\1[REDACTED]", detail)
        detail = " ".join(detail.split())[:240]
    suffix = f": {detail}" if detail else ""
    return RuntimeError(f"Sandbox worker {path} 返回 HTTP {response.status_code}{suffix}")


def _call_worker(
    worker: SandboxWorker,
    method: str,
    path: str,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    token = _worker_token(worker)
    headers = {"Authorization": f"Bearer {token}"}
    timeout = httpx.Timeout(630.0, connect=10.0)
    if worker.transport == "unix":
        transport = httpx.HTTPTransport(uds=worker.endpoint)
        with httpx.Client(transport=transport, base_url="http://sandbox", timeout=timeout, trust_env=False) as client:
            response = client.request(method, path, headers=headers, json=payload)
    else:
        target = pin_public_http_url(f"{worker.endpoint.rstrip('/')}{path}", require_https=True)
        with httpx.Client(timeout=timeout, trust_env=False, follow_redirects=False) as client:
            response = client.request(
                method,
                target.request_url,
                headers={**headers, "Host": target.host_header},
                json=payload,
                extensions=target.request_extensions,
            )
    if response.is_error:
        raise _worker_http_error(response, path)
    body = response.json()
    if not isinstance(body, dict):
        raise RuntimeError("Sandbox worker 响应不是对象")
    return body


def _stop_worker_requests(
    worker: SandboxWorker,
    request_ids: Iterable[str],
) -> dict[str, dict[str, Any]]:
    """Stop concrete requests and require a terminal acknowledgement for all."""
    states: dict[str, dict[str, Any]] = {}
    errors: list[str] = []
    exceptions: list[Exception] = []
    for request_id in dict.fromkeys(request_ids):
        try:
            response = _call_worker(worker, "POST", "/stop", {"request_id": request_id})
            state = response.get("result") if isinstance(response.get("result"), dict) else response
            status = str(state.get("status") or "")
            response_id = state.get("request_id")
            if response_id != request_id:
                errors.append(f"{request_id}=request_id_mismatch")
                continue
            if status not in TERMINAL_STATES:
                errors.append(f"{request_id}={status or 'unknown'}")
                continue
            result = state.get("result")
            if (
                not isinstance(result, dict)
                or result.get("cleanup_confirmed") is not True
                or result.get("cleanup_error")
            ):
                errors.append(f"{request_id}=cleanup_unconfirmed")
                continue
            states[request_id] = state
        except Exception as exc:  # noqa: BLE001 - continue reclaiming sibling requests
            errors.append(f"{request_id}={str(exc)[:180]}")
            exceptions.append(exc)
    if errors:
        if len(errors) == 1 and len(exceptions) == 1:
            raise exceptions[0]
        raise RuntimeError("Sandbox worker 未确认全部资源已终止：" + "; ".join(errors))
    return states


def _stop_registered_worker_requests(
    worker: SandboxWorker,
    environment: SandboxEnvironment,
) -> dict[str, dict[str, Any]]:
    """Stop every request registered for an environment, including its parent tombstone."""
    return _stop_worker_requests(worker, _registered_worker_request_ids(environment))


def _proxy_worker_preview(
    worker: SandboxWorker,
    public_id: str,
    path: str,
    query: str,
    method: str,
    request_headers: dict[str, str],
    body: bytes,
) -> tuple[int, dict[str, str], bytes]:
    if method not in {"GET", "HEAD", "POST"}:
        raise ValidationError("预览只支持 GET、HEAD 和 POST", code=40500)
    if len(path) > 2048 or len(query) > 2048 or "\r" in path + query or "\n" in path + query:
        raise ValidationError("预览路径或查询参数过长", code=40001)
    if len(body) > 1_048_576:
        raise ValidationError("预览请求体超过 1 MiB", code=40001)
    quoted_path = urllib.parse.quote("/" + path.lstrip("/"), safe="/!$&'()*+,-./:;=@_~")
    worker_path = f"/preview/{public_id}{quoted_path}" + (f"?{query}" if query else "")
    token = _worker_token(worker)
    headers = {"Authorization": f"Bearer {token}"}
    for name in ("Accept", "Accept-Language", "Content-Type"):
        value = str(request_headers.get(name) or "")
        if value:
            if len(value) > 512 or "\r" in value or "\n" in value:
                raise ValidationError("预览请求头不合法", code=40001)
            headers[name] = value
    timeout = httpx.Timeout(30.0, connect=10.0)
    if worker.transport == "unix":
        transport = httpx.HTTPTransport(uds=worker.endpoint)
        with httpx.Client(transport=transport, base_url="http://sandbox", timeout=timeout, trust_env=False) as client:
            response = client.request(method, worker_path, headers=headers, content=body if method == "POST" else None)
    else:
        target = pin_public_http_url(f"{worker.endpoint.rstrip('/')}{worker_path}", require_https=True)
        with httpx.Client(timeout=timeout, trust_env=False, follow_redirects=False) as client:
            response = client.request(
                method,
                target.request_url,
                headers={**headers, "Host": target.host_header},
                content=body if method == "POST" else None,
                extensions=target.request_extensions,
            )
    if len(response.content) > 2_097_152:
        raise RuntimeError("预览响应超过 2 MiB")
    safe_headers: dict[str, str] = {}
    safe_response_headers = (
        "cache-control",
        "content-disposition",
        "content-language",
        "content-type",
        "etag",
        "last-modified",
        "location",
    )
    for name in safe_response_headers:
        value = response.headers.get(name)
        if value and "\r" not in value and "\n" not in value:
            safe_headers[name] = value[:2048]
    return response.status_code, safe_headers, response.content


def create_preview_session(db: Session, actor: User, public_id: str) -> dict[str, Any]:
    environment = _get_visible(db, actor, public_id)
    _require_sandbox_execution(db, actor, environment.project_id)
    # 隔离归档允许部署,但只允许通过受 JWT 保护的 backend→worker 预览代理访问。
    # 它不会获得 host network、宿主端口映射或任何无保护的服务器执行路径。
    if environment.purpose != "deploy" or environment.status != "ready":
        raise ValidationError("持续部署沙箱尚未处于可预览状态", code=40901)
    if environment.expires_at <= _utcnow():
        raise ValidationError("持续部署沙箱已到期", code=40901)
    worker = db.get(SandboxWorker, environment.worker_id) if environment.worker_id else None
    if not worker:
        raise ValidationError("持续部署 worker 不可用", code=50301)
    now = datetime.now(timezone.utc)
    token = jwt.encode(
        {
            "sub": str(actor.id),
            "ver": int(actor.token_version or 0),
            "typ": "sandbox_preview",
            "sbx": environment.public_id,
            "iat": int(now.timestamp()),
            "exp": int((now + timedelta(seconds=PREVIEW_SESSION_SECONDS)).timestamp()),
        },
        settings.jwt_secret,
        algorithm=settings.jwt_algorithm,
    )
    audit_service.log(
        db,
        actor,
        "sandbox_preview_session",
        target_type="sandbox_environment",
        target_id=environment.public_id,
        commit=False,
    )
    db.commit()
    return {
        "token": token,
        "path": environment.preview_path or f"/api/sandboxes/{environment.public_id}/preview/",
        "max_age": PREVIEW_SESSION_SECONDS,
    }


def authenticate_preview_session(db: Session, public_id: str, token: str) -> tuple[SandboxEnvironment, SandboxWorker]:
    try:
        payload = jwt.decode(
            token,
            settings.jwt_secret,
            algorithms=[settings.jwt_algorithm],
            options={"require": ["exp", "sub", "typ", "sbx", "ver"]},
        )
        if payload.get("typ") != "sandbox_preview" or payload.get("sbx") != public_id:
            raise ValueError("preview token scope mismatch")
        user_id = int(payload["sub"])
        token_version = int(payload["ver"])
    except Exception as exc:
        raise AuthError("预览会话无效或已过期", code=40101) from exc
    actor = db.query(User).populate_existing().filter(User.id == user_id).one_or_none()
    if not actor or actor.status != 1 or int(actor.token_version or 0) != token_version:
        raise AuthError("预览会话已失效", code=40102)
    environment = _get_visible(db, actor, public_id)
    _require_sandbox_execution(db, actor, environment.project_id)
    # 隔离归档允许部署,但只允许通过受 JWT 保护的 backend→worker 预览代理访问。
    # 它不会获得 host network、宿主端口映射或任何无保护的服务器执行路径。
    if environment.purpose != "deploy" or environment.status != "ready" or environment.expires_at <= _utcnow():
        raise ValidationError("持续部署沙箱当前不可预览", code=40901)
    worker = db.get(SandboxWorker, environment.worker_id) if environment.worker_id else None
    if not worker:
        raise ValidationError("持续部署 worker 不可用", code=50301)
    return environment, worker


def proxy_preview(
    db: Session,
    public_id: str,
    token: str,
    path: str,
    query: str,
    method: str,
    request_headers: dict[str, str],
    body: bytes,
) -> tuple[int, dict[str, str], bytes]:
    _environment, worker = authenticate_preview_session(db, public_id, token)
    response = _proxy_worker_preview(worker, public_id, path, query, method, request_headers, body)
    db.commit()
    return response


def check_worker(db: Session, worker_id: int) -> dict[str, Any]:
    row = db.get(SandboxWorker, worker_id)
    if not row:
        raise NotFoundError("Sandbox worker 不存在", code=40400)
    try:
        health = _call_worker(row, "GET", "/health")
        health_result = health.get("result") if isinstance(health.get("result"), dict) else health
        runtime = str(health_result.get("runtime") or "")
        required_languages = set(_loads(row.supported_languages_json, []))
        required_modes = set(_loads(row.supported_modes_json, []))
        reported_languages = set(health_result.get("supported_languages") or [])
        reported_modes = set(health_result.get("supported_test_modes") or [])
        reported_purposes = set(health_result.get("supported_purposes") or [])
        profiles = health_result.get("profiles") if isinstance(health_result.get("profiles"), dict) else {}
        profiles_ready = all(
            isinstance(profiles.get(language), dict)
            and profiles[language].get("ready") is True
            and bool(profiles[language].get("profile_fingerprint"))
            for language in required_languages
        )
        contract_ok = (
            health_result.get("protocol_version") == "1.0"
            and required_languages.issubset(reported_languages)
            and required_modes.difference({"deploy"}).issubset(reported_modes)
            and ("deploy" not in required_modes or "deploy" in reported_purposes)
            and profiles_ready
        )
        healthy = bool(health.get("ok", health_result.get("ready"))) and runtime == row.runtime and contract_ok
        row.status = "healthy" if healthy else "blocked"
        row.last_error = (
            None
            if healthy
            else (
                f"worker 合约或 runtime 不匹配: expected_runtime={row.runtime}; "
                f"actual_runtime={runtime or 'unknown'}; contract_ok={contract_ok}"
            )
        )
        row.last_seen_at = _utcnow()
        row.fingerprint_json = _json(health_result)
    except Exception as exc:
        row.status = "unhealthy"
        row.last_error = str(exc)[:1000]
        row.last_seen_at = _utcnow()
    db.commit()
    return worker_to_dict(row)


def _browser_fingerprint_ready(row: SandboxWorker) -> bool:
    fingerprint = _loads(row.fingerprint_json, {})
    browser = fingerprint.get("browser_blackbox") if isinstance(fingerprint, dict) else None
    if not isinstance(browser, dict) or browser.get("ready") is not True:
        return False
    digest = str(browser.get("image_digest") or "")
    policy = browser.get("resource_policy")
    return bool(
        row.enabled
        and row.status == "healthy"
        and row.runtime == settings.sandbox_required_runtime == "runsc"
        and len(digest) == 71
        and digest.startswith("sha256:")
        and isinstance(policy, dict)
        and policy.get("network") == "private_browser_to_fixed_target_proxy"
        and policy.get("target") == "single_https_origin_and_pinned_public_ip"
        and str(browser.get("egress_policy_fingerprint") or "")
    )


def browser_worker_ready(db: Session) -> bool:
    rows = (
        db.query(SandboxWorker)
        .filter(
            SandboxWorker.enabled == 1,
            SandboxWorker.status == "healthy",
        )
        .all()
    )
    return any(_browser_fingerprint_ready(row) for row in rows)


def _select_browser_worker(db: Session) -> SandboxWorker:
    rows = (
        db.query(SandboxWorker)
        .filter(
            SandboxWorker.enabled == 1,
            SandboxWorker.status == "healthy",
        )
        .order_by(SandboxWorker.priority, SandboxWorker.id)
        .all()
    )
    for row in rows:
        if _browser_fingerprint_ready(row):
            return row
    raise ValidationError("没有通过 Playwright 镜像、runsc 与出口策略自检的 worker", code=50301)


def _normalize_browser_target(value: str) -> tuple[str, Any]:
    target = pin_public_http_url(value, require_https=True)
    parsed = urllib.parse.urlsplit(target.original_url)
    hostname = (parsed.hostname or "").rstrip(".").encode("idna").decode("ascii").lower()
    port = parsed.port or 443
    netloc = hostname if port == 443 else f"{hostname}:{port}"
    normalized = urllib.parse.urlunsplit(("https", netloc, parsed.path or "/", parsed.query, ""))
    return normalized, target


def run_browser_blackbox(
    db: Session,
    actor: User,
    public_id: str,
    target_url: str,
) -> dict[str, Any]:
    environment = _get_visible(db, actor, public_id)
    _require_sandbox_execution(db, actor, environment.project_id)
    if not _can_manage(db, actor, environment):
        raise ForbiddenError("只有沙箱创建者或唯一超级管理员可执行浏览器黑盒测试", code=40300)
    if environment.purpose != "test" or environment.test_mode not in {"blackbox", "combined"}:
        raise ValidationError("浏览器黑盒工具只能用于黑盒或组合测试沙箱", code=40901)
    if environment.status not in {"succeeded", "failed"}:
        raise ValidationError("沙箱尚未进入可验证的测试终态", code=40901)
    if environment.expires_at <= _utcnow():
        raise ValidationError("沙箱已到期，不能执行浏览器黑盒测试", code=40901)
    if not environment.remote_target_url or environment.remote_target_authorized_at is None:
        raise ForbiddenError("沙箱没有已保存的远程目标授权", code=40340)

    expected_url, expected_target = _normalize_browser_target(environment.remote_target_url)
    requested_url, _requested_target = _normalize_browser_target(target_url)
    if requested_url != expected_url:
        raise ForbiddenError("浏览器目标必须与沙箱已授权的远程目标完全一致", code=40340)
    try:
        pinned_ip = ipaddress.ip_address(expected_target.ip_address)
    except ValueError as exc:
        raise ValidationError("授权目标没有可固定的公网 IP", code=40001) from exc
    if pinned_ip.version != 4 or not pinned_ip.is_global:
        raise ValidationError("当前 Playwright worker 只接受固定公网 IPv4 目标", code=40001)

    worker = _select_browser_worker(db)
    request_id = f"bbx-{uuid.uuid4().hex[:24]}"
    _require_current_actor_execution(db, actor.id, environment.project_id)
    response = _call_worker(
        worker,
        "POST",
        "/browser-blackbox",
        {
            "request_id": request_id,
            "target_url": expected_url,
            "target_ip": str(pinned_ip),
        },
    )
    result = response.get("result") if isinstance(response.get("result"), dict) else response
    _require_current_actor_execution(db, actor.id, environment.project_id)
    if not isinstance(result, dict) or result.get("protocol_version") != "1.0":
        raise RuntimeError("Playwright worker 返回的证据协议无效")

    # Worker 调用可能跨越一次关闭或到期；锁定最新环境状态后再写制品，
    # 避免迟到的截图/结论归属到已经停止的沙箱。
    environment = (
        db.query(SandboxEnvironment)
        .populate_existing()
        .filter(
            SandboxEnvironment.id == environment.id,
            SandboxEnvironment.status.in_(("succeeded", "failed")),
            SandboxEnvironment.expires_at > _utcnow(),
        )
        .with_for_update()
        .first()
    )
    if environment is None:
        db.rollback()
        raise ConflictError("沙箱已关闭或状态已变化，浏览器结果已丢弃", code=40903)

    evidence = dict(result)
    screenshot_encoded = str(evidence.pop("screenshot_base64", "") or "")
    artifacts: list[SandboxArtifact] = []
    if screenshot_encoded:
        try:
            screenshot = base64.b64decode(screenshot_encoded, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise RuntimeError("Playwright 截图证据 Base64 无效") from exc
        if not screenshot or len(screenshot) > 2 * 1024 * 1024:
            raise RuntimeError("Playwright 截图证据大小不合法")
        artifacts.append(
            _persist_browser_artifact(
                db,
                environment,
                artifact_type="browser_screenshot",
                file_name=f"browser-{request_id}.jpg",
                mime_type="image/jpeg",
                content=screenshot,
            )
        )
    evidence_bytes = json.dumps(evidence, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8")
    artifacts.append(
        _persist_browser_artifact(
            db,
            environment,
            artifact_type="browser_evidence",
            file_name=f"browser-{request_id}.json",
            mime_type="application/json",
            content=evidence_bytes,
        )
    )

    conclusion = _loads(environment.result_json, {})
    if not isinstance(conclusion, dict):
        conclusion = {}
    runs = conclusion.get("browser_blackbox_runs")
    if not isinstance(runs, list):
        runs = []
    conclusion["browser_blackbox_runs"] = [*runs[-19:], evidence]
    environment.result_json = _json(conclusion)
    _append_event(
        db,
        environment,
        "complete" if evidence.get("passed") else "failed",
        "browser_blackbox",
        "Playwright 浏览器黑盒测试完成",
        {
            "request_id": request_id,
            "passed": bool(evidence.get("passed")),
            "status_code": int(evidence.get("status_code") or 0),
            "artifact_ids": [row.id for row in artifacts],
        },
    )
    audit_service.log(
        db,
        actor,
        "sandbox_browser_blackbox",
        target_type="sandbox_environment",
        target_id=environment.public_id,
        detail=f"request_id={request_id}; passed={bool(evidence.get('passed'))}; worker={worker.code}",
        commit=False,
    )
    db.commit()
    return {
        **evidence,
        "artifacts": [artifact_to_dict(row) for row in artifacts],
    }


def seed_production_worker(db: Session, actor: User) -> SandboxWorker:
    row = db.query(SandboxWorker).filter(SandboxWorker.code == "production-fallback").first()
    if not row:
        row = SandboxWorker(
            code="production-fallback",
            name="生产受限兜底节点",
            priority=900,
        )
        db.add(row)
    row.worker_type = "production_fallback"
    row.transport = "unix"
    row.endpoint = _assert_worker_endpoint("unix", settings.sandbox_executor_socket)
    row.supported_languages_json = _json(LANGUAGES)
    row.supported_modes_json = _json(MODES)
    row.runtime = settings.sandbox_required_runtime
    row.max_concurrency = settings.sandbox_max_concurrency
    row.status = "unknown" if settings.sandbox_enabled else "disabled"
    row.enabled = int(settings.sandbox_enabled)
    db.commit()
    audit_service.log(db, actor, "sandbox_worker_seed", target_type="sandbox_worker", target_id=row.code)
    return row


def _select_worker(db: Session, *, language: str, mode: str, worker_code: str = "") -> SandboxWorker:
    query = db.query(SandboxWorker).filter(SandboxWorker.enabled == 1, SandboxWorker.status == "healthy")
    if worker_code:
        query = query.filter(SandboxWorker.code == worker_code)
    candidates = query.order_by(SandboxWorker.priority, SandboxWorker.id).all()
    for row in candidates:
        local_runc_allowed = (
            settings.sandbox_mode == "local_development"
            and settings.sandbox_allow_runc
            and row.worker_type == "local"
            and row.runtime == "runc"
        )
        if row.runtime != settings.sandbox_required_runtime and not local_runc_allowed:
            continue
        languages = _loads(row.supported_languages_json, [])
        modes = _loads(row.supported_modes_json, [])
        running = (
            db.query(SandboxEnvironment)
            .filter(
                SandboxEnvironment.worker_id == row.id,
                SandboxEnvironment.status.in_(ACTIVE_STATES),
            )
            .count()
        )
        if language in languages and mode in modes and running < row.max_concurrency:
            return row
    if worker_code:
        raise ValidationError("指定 worker 不健康、不支持当前任务或已达并发上限", code=40901)
    raise ValidationError("没有可用的隔离 worker，任务未运行", code=50301)


def _append_event(
    db: Session,
    environment: SandboxEnvironment,
    event_type: str,
    stage: str,
    message: str,
    payload: dict[str, Any] | None = None,
) -> SandboxEvent:
    event = SandboxEvent(
        environment_id=environment.id,
        event_type=event_type,
        stage=stage,
        message=message[:500],
        payload_json=_json(payload or {}),
        create_time=_utcnow(),
    )
    db.add(event)
    db.flush()
    return event


def _emit(
    environment: SandboxEnvironment,
    type_: AgentEventType,
    message: str,
    payload: dict[str, Any] | None = None,
) -> None:
    emit_event(
        type_=type_,
        agent=environment.agent_code,
        trace_id=environment.public_id,
        message=message,
        payload={"sandbox_id": environment.public_id, **(payload or {})},
        user_id=environment.owner_id,
    )


def _probe_remote_target(url: str) -> dict[str, Any]:
    if not settings.sandbox_remote_targets_enabled:
        raise ValidationError("远程目标黑盒测试未启用", code=40300)
    target = pin_public_http_url(url)
    started = datetime.utcnow()
    sampled_bytes = 0
    with (
        httpx.Client(
            timeout=httpx.Timeout(float(settings.sandbox_remote_timeout), connect=10.0),
            follow_redirects=False,
            trust_env=False,
        ) as client,
        client.stream(
            "GET",
            target.request_url,
            headers={"Host": target.host_header, "User-Agent": "Prism-Blackbox-Agent/1.0"},
            extensions=target.request_extensions,
        ) as response,
    ):
        status_code = response.status_code
        headers = {key.lower(): value for key, value in response.headers.items()}
        for chunk in response.iter_bytes():
            sampled_bytes += min(len(chunk), 65_536 - sampled_bytes)
            if sampled_bytes >= 65_536:
                break
    elapsed_ms = int((datetime.utcnow() - started).total_seconds() * 1000)
    expected_headers = (
        "content-security-policy",
        "strict-transport-security",
        "x-content-type-options",
        "referrer-policy",
    )
    return {
        "kind": "remote_http_blackbox",
        "target_origin": target.original_url,
        "resolved_ip": target.ip_address,
        "status_code": status_code,
        "latency_ms": elapsed_ms,
        "redirect_location": headers.get("location", ""),
        "content_type": headers.get("content-type", ""),
        "security_headers": {key: headers.get(key, "") for key in expected_headers},
        "missing_security_headers": [key for key in expected_headers if not headers.get(key)],
        "body_bytes_sampled": sampled_bytes,
    }


def issue_remote_target_authorization(
    db: Session,
    actor: User,
    *,
    project_id: int,
    remote_target_url: str,
    test_mode: str,
    confirmed: bool,
) -> dict[str, Any]:
    """记录用户对精确外部目标和模式的短时确认，返回只能消费一次的凭证。"""
    if not confirmed:
        raise ForbiddenError("必须明确确认本次远程目标测试授权", code=40340)
    if test_mode not in {"blackbox", "combined"}:
        raise ValidationError("远程目标授权只支持黑盒或组合测试", code=40001)
    _require_sandbox_execution(db, actor, int(project_id))
    target = pin_public_http_url(str(remote_target_url).strip(), require_https=True)
    target_url = str(target.original_url)
    expires_at = _utcnow() + timedelta(minutes=5)
    secret = uuid.uuid4().hex + uuid.uuid4().hex
    request = {
        "owner_user_id": int(actor.id),
        "project_id": int(project_id),
        "remote_target_url": target_url,
        "test_mode": test_mode,
        "token_sha256": hashlib.sha256(secret.encode("ascii")).hexdigest(),
        "expires_at": expires_at.isoformat(),
        "consumed_by": None,
    }
    approval = ApprovalItem(
        title="确认远程黑盒测试目标",
        agent_code="test_verifier",
        action="sandbox.remote_target.test",
        resource=f"project:{int(project_id)}",
        risk_level="medium",
        status="approved",
        decision="allow",
        decision_reason="用户在沙箱工作台逐目标确认本次测试",
        request_json=_json(request),
        decided_by=int(actor.id),
        decided_at=_utcnow(),
    )
    db.add(approval)
    db.flush()
    audit_service.log(
        db,
        actor,
        "sandbox_remote_target_authorized",
        target_type="approval",
        target_id=str(approval.id),
        detail=f"project={int(project_id)}; mode={test_mode}; target={target_url}",
        commit=False,
    )
    db.commit()
    return {"approval_token": f"{approval.id}.{secret}", "expires_at": expires_at}


def _consume_remote_target_authorization(
    db: Session,
    actor: User,
    *,
    approval_token: str,
    project_id: int,
    remote_target_url: str,
    test_mode: str,
    sandbox_public_id: str,
) -> int:
    try:
        raw_id, secret = str(approval_token).split(".", 1)
        approval_id = int(raw_id)
    except (TypeError, ValueError):
        raise ForbiddenError("远程目标授权凭证无效或已过期", code=40340) from None
    approval = db.query(ApprovalItem).filter(ApprovalItem.id == approval_id).with_for_update().first()
    if approval is None or approval.action != "sandbox.remote_target.test":
        raise ForbiddenError("远程目标授权凭证无效或已过期", code=40340)
    request = _loads(approval.request_json, {})
    target = pin_public_http_url(remote_target_url, require_https=True)
    supplied_digest = hashlib.sha256(secret.encode("ascii", errors="ignore")).hexdigest()
    try:
        expires_at = datetime.fromisoformat(str(request.get("expires_at") or ""))
    except ValueError:
        expires_at = datetime.min
    valid = (
        approval.status == "approved"
        and approval.decision == "allow"
        and approval.decided_by == int(actor.id)
        and int(request.get("owner_user_id") or 0) == int(actor.id)
        and int(request.get("project_id") or 0) == int(project_id)
        and request.get("remote_target_url") == str(target.original_url)
        and request.get("test_mode") == test_mode
        and not request.get("consumed_by")
        and expires_at >= _utcnow()
        and hmac.compare_digest(str(request.get("token_sha256") or ""), supplied_digest)
    )
    if not valid:
        raise ForbiddenError("远程目标授权与账号、项目、目标、模式不匹配，或已过期/使用", code=40340)
    request["consumed_by"] = sandbox_public_id
    request["consumed_at"] = _utcnow().isoformat()
    approval.request_json = _json(request)
    db.flush()
    return int(approval.id)


def _require_remote_target_authorization(payload: dict[str, Any], *, server_approval_required: bool) -> None:
    if server_approval_required:
        if not payload.get("remote_target_approval_token"):
            raise ForbiddenError("远程目标必须先完成服务端逐目标确认", code=40340)
    elif not payload.get("remote_target_authorized"):
        raise ForbiddenError("必须显式确认已获得该远程目标本次测试授权", code=40340)


def create_environment(
    db: Session,
    actor: User,
    payload: dict[str, Any],
    *,
    require_remote_target_approval: bool = False,
) -> SandboxEnvironment:
    """创建沙箱环境，并在所有失败路径释放项目行锁。"""
    try:
        return _create_environment_locked(
            db,
            actor,
            payload,
            require_remote_target_approval=require_remote_target_approval,
        )
    except Exception:
        db.rollback()
        raise


def _create_environment_locked(
    db: Session,
    actor: User,
    payload: dict[str, Any],
    *,
    require_remote_target_approval: bool = False,
) -> SandboxEnvironment:
    project_id = int(payload["project_id"])
    purpose = payload["purpose"]
    agent_team_context = _normalize_agent_team_context(payload.get("agent_team"))
    require_project_access(db, project_id, actor, need_write=purpose == "deploy")
    _require_sandbox_execution(db, actor, project_id)
    maintenance_file = Path(settings.sandbox_maintenance_file)
    try:
        maintenance_state = maintenance_file.lstat()
    except FileNotFoundError:
        maintenance_state = None
    except OSError as exc:
        raise ServiceUnavailableError(
            "沙箱维护状态无法确认，任务已暂停提交，请稍后重试",
            code=50301,
        ) from exc
    if maintenance_state is not None:
        state_kind = "维护标记" if stat.S_ISREG(maintenance_state.st_mode) else "异常维护标记"
        raise ServiceUnavailableError(
            f"沙箱正在维护（{state_kind}已启用），任务已暂停提交，请稍后重试",
            code=50301,
        )
    project: Project | None = None
    # deploy/test 都锁项目行，防止同项目并发创建多个沙箱造成 artifact 更新竞争。
    if purpose in {"deploy", "test"}:
        project = (
            db.query(Project).filter(Project.id == project_id, Project.status != "deleted").with_for_update().first()
        )
        if project is None:
            raise NotFoundError("项目不存在", code=40400)
    if purpose == "test":
        active_test = (
            db.query(SandboxEnvironment.id)
            .filter(
                SandboxEnvironment.project_id == project_id,
                SandboxEnvironment.purpose == "test",
                SandboxEnvironment.status.in_({"queued", "dispatching", "running", "finalizing"}),
            )
            .first()
        )
        if active_test is not None:
            raise ConflictError(
                "该项目已有进行中的测试沙箱，请等待其完成或关闭后再发起",
                code=40903,
            )
    # 隔离归档的 source snapshot 可交给固定 profile 的 runsc 容器运行;绝不在宿主机执行。
    # 预览始终经受 JWT 保护的 backend 代理访问,而非暴露容器端口。
    requested_language = str(payload["language"] or "").strip().lower()
    if requested_language not in LANGUAGES:
        raise ValidationError("沙箱语言或模式不受支持", code=40001)
    project_language = _project_deployment_language(project.language if project else None)
    if purpose == "deploy" and not project_language:
        raise ValidationError(
            "项目主语言无法映射到受控部署运行时，请先更新项目语言",
            code=40001,
        )
    language = project_language or requested_language
    mode = "deploy" if purpose == "deploy" else payload.get("test_mode", "whitebox")
    if language not in LANGUAGES or mode not in MODES:
        raise ValidationError("沙箱语言或模式不受支持", code=40001)
    if purpose == "test" and mode == "deploy":
        raise ValidationError("测试任务不能使用 deploy 模式", code=40001)
    remote_url = str(payload.get("remote_target_url") or "").strip()
    if remote_url:
        if purpose != "test" or mode not in {"blackbox", "combined"}:
            raise ValidationError("远程目标只能用于黑盒或组合测试", code=40001)
        _require_remote_target_authorization(payload, server_approval_required=require_remote_target_approval)
        pin_public_http_url(remote_url, require_https=True)

    remote_only = bool(remote_url and mode == "blackbox")
    worker_mode = "whitebox" if remote_url and mode == "combined" else mode
    # 先做参数与源码/副本校验(worker 繁忙时不掩盖真实参数错误),最后再选 worker
    db_type = str(payload.get("db_type") or "none").strip().lower()
    if db_type not in {"none", "sqlite", "mysql"}:
        raise ValidationError("沙箱数据库类型不受支持", code=40001)
    source_revision_id = payload.get("source_revision_id")
    source_revision_snapshot: dict[str, Any] | None = None
    if source_revision_id:
        source_revision_snapshot = project_source_revision_service.get_revision_archive_snapshot(
            db,
            actor,
            int(source_revision_id),
            project_id,
        )
        archive = source_revision_snapshot["archive"]
    else:
        archive, archive_filename = project_source_service.build_source_archive(db, actor, project_id)
    if source_revision_id:
        archive_filename = "source-revision.zip"
    try:
        decompilation_plan = decompilation_service.plan_decompilation_archive(archive, archive_filename)
    except decompilation_service.DecompilationError as exc:
        # 归档格式/安全约束属于用户输入问题，不能冒泡成通用 500。
        raise ValidationError(str(exc), code=40001) from exc
    original_source_sha256 = hashlib.sha256(archive).hexdigest()
    if worker_mode in {"whitebox", "blackbox", "combined", "deploy"}:
        try:
            archive, worker_archive_filename = _normalize_source_archive_for_worker(archive, archive_filename)
        except ValidationError as exc:
            raise ValidationError(str(exc), code=40001) from exc
    else:
        worker_archive_filename = archive_filename
    source_sha256 = hashlib.sha256(archive).hexdigest()
    worker = (
        None
        if remote_only
        else _select_worker(
            db,
            language=language,
            mode=worker_mode,
            worker_code=payload.get("worker_code", ""),
        )
    )
    requested_ttl = int(payload.get("ttl_hours") or settings.sandbox_default_ttl_hours)
    ttl_hours = max(1, min(requested_ttl, settings.sandbox_max_ttl_hours))
    public_id = f"sbx_{uuid.uuid4().hex[:24]}"
    remote_target_approval_id = None
    if remote_url and require_remote_target_approval:
        remote_target_approval_id = _consume_remote_target_authorization(
            db,
            actor,
            approval_token=str(payload.get("remote_target_approval_token") or ""),
            project_id=project_id,
            remote_target_url=remote_url,
            test_mode=mode,
            sandbox_public_id=public_id,
        )
    agent_code = "sandbox_deployer" if purpose == "deploy" else "test_verifier"
    execution_token = uuid.uuid4().hex
    environment = SandboxEnvironment(
        public_id=public_id,
        **current_attribution(int(actor.id)),
        project_id=project_id,
        owner_id=actor.id,
        worker_id=worker.id if worker else None,
        agent_code=agent_code,
        purpose=purpose,
        language=language,
        test_mode=mode,
        status="queued",
        runtime=worker.runtime if worker else "remote_http",
        image_ref=_IMAGE_REFS[language] if worker else "remote-http-probe:v1",
        source_sha256=source_sha256,
        source_archive_blob=archive,
        execution_token=execution_token,
        resource_policy_json=_json(
            _profile_policy(language)
            if worker
            else {
                "network": "authorized_remote_target_only",
                "timeout_seconds": settings.sandbox_remote_timeout,
                "response_sample_bytes": 65_536,
            }
        ),
        agent_config_json=_json(
            {
                "worker_mode": worker_mode,
                "remote_only": remote_only,
                "ttl_hours": ttl_hours,
                "requested_language": requested_language,
                "resolved_language": language,
                "language_source": "project" if project_language else "request",
                "db_type": db_type,
                "source_revision_id": int(source_revision_id) if source_revision_id else None,
                "remote_target_approval_id": remote_target_approval_id,
                "source_revision_no": source_revision_snapshot["revision_no"] if source_revision_snapshot else None,
                "source_revision_sha256": source_revision_snapshot["source_sha256"]
                if source_revision_snapshot
                else None,
                "source_revision_parent_sha256": source_revision_snapshot["parent_sha256"]
                if source_revision_snapshot
                else None,
                "syntax_repair_revisions": [],
                "source_archive_filename": worker_archive_filename,
                "original_source_sha256": original_source_sha256,
                "decompilation": decompilation_plan,
                "agent_team": (
                    {
                        "team_id": agent_team_context["team_id"],
                        "task_id": agent_team_context["task_id"],
                        "attempt": agent_team_context["attempt"],
                        "execution_strategy": agent_team_context.get("execution_strategy", {}),
                        "lease_fingerprint": hashlib.sha256(
                            agent_team_context["lease_token"].encode("utf-8")
                        ).hexdigest(),
                    }
                    if agent_team_context
                    else None
                ),
            }
        ),
        remote_target_url=remote_url or None,
        remote_target_authorized_at=_utcnow()
        if remote_url and (remote_target_approval_id or payload.get("remote_target_authorized"))
        else None,
        expires_at=_utcnow() + timedelta(hours=ttl_hours),
    )
    db.add(environment)
    db.flush()
    if agent_team_context:
        from app.services import agent_team_service

        agent_team_service.attach_task_runtime_resource(
            db,
            owner_user_id=int(actor.id),
            team_id=agent_team_context["team_id"],
            task_id=agent_team_context["task_id"],
            lease_token=agent_team_context["lease_token"],
            resource_type="sandbox_environment",
            resource_id=public_id,
            metadata={"attempt": agent_team_context["attempt"], "purpose": purpose},
        )
    _append_event(
        db,
        environment,
        "dispatch",
        "authorization",
        f"{agent_code} 已校验项目权限和测试边界",
        {
            "remote_target_approval_id": remote_target_approval_id,
            "remote_target_authorized": bool(remote_url and remote_target_approval_id),
        },
    )
    if language != requested_language:
        _append_event(
            db,
            environment,
            "dispatch",
            "language",
            f"已按项目主语言将运行时从 {requested_language} 调整为 {language}",
            {"requested_language": requested_language, "resolved_language": language},
        )
    _append_event(db, environment, "dispatch", "snapshot", f"已生成不可变源码快照 {source_sha256[:12]}")
    worker_label = worker.code if worker else "remote-http-probe"
    worker_runtime = worker.runtime if worker else "remote_http"
    _append_event(
        db,
        environment,
        "dispatch",
        "worker",
        f"已调用 worker {worker_label}",
        {"worker_code": worker_label, "runtime": worker_runtime},
    )
    audit_service.log(
        db,
        actor,
        "sandbox_create",
        target_type="sandbox_environment",
        target_id=public_id,
        detail=(
            f"project={project_id}; purpose={purpose}; mode={mode}; worker={worker_label}; "
            f"requested_language={requested_language}; resolved_language={language}; source={source_sha256}"
        ),
        commit=False,
    )
    db.commit()
    _emit(environment, AgentEventType.DISPATCH, f"{agent_code} 已调用 {worker_label}", {"stage": "worker"})
    thread = threading.Thread(
        target=_execute_environment,
        args=(environment.id, None, execution_token),
        name=f"sandbox-{public_id}",
        daemon=True,
    )
    thread.start()
    return environment


def resume_interrupted_environments() -> int:
    """在启动阶段 CAS 领取并重启上个进程遗留的沙箱测试。"""
    db = SessionLocal()
    dispatched: list[tuple[int, str, str]] = []
    try:
        rows = (
            db.query(SandboxEnvironment.id, SandboxEnvironment.status, SandboxEnvironment.execution_token)
            .filter(SandboxEnvironment.status.in_(("queued", "dispatching", "running", "finalizing")))
            .order_by(SandboxEnvironment.id.asc())
            .all()
        )
        for environment_id, previous_status, previous_token in rows:
            next_token = uuid.uuid4().hex
            claimed = (
                db.query(SandboxEnvironment)
                .filter(
                    SandboxEnvironment.id == environment_id,
                    SandboxEnvironment.status == previous_status,
                    SandboxEnvironment.execution_token == str(previous_token or ""),
                )
                .update(
                    {"status": "recovering", "execution_token": next_token},
                    synchronize_session=False,
                )
            )
            if claimed != 1:
                db.rollback()
                continue
            environment = db.get(SandboxEnvironment, environment_id)
            if environment is None or environment.source_archive_blob is None:
                if environment is not None:
                    environment.status = "failed"
                    environment.error = "旧沙箱缺少持久化源码快照，无法在重启后安全恢复"
                    environment.stopped_at = _utcnow()
                db.commit()
                continue
            raw = bytes(environment.source_archive_blob)
            if not hmac.compare_digest(hashlib.sha256(raw).hexdigest(), environment.source_sha256):
                environment.status = "failed"
                environment.error = "沙箱持久化源码快照完整性校验失败"
                environment.stopped_at = _utcnow()
                db.commit()
                continue
            _append_event(db, environment, "progress", "recovery", "服务重启后已从不可变源码快照恢复执行")
            db.commit()
            dispatched.append((int(environment_id), next_token, environment.public_id))
    finally:
        db.close()

    for environment_id, execution_token, public_id in dispatched:
        threading.Thread(
            target=_execute_environment,
            args=(environment_id, None, execution_token),
            name=f"sandbox-recovery-{public_id}",
            daemon=True,
        ).start()
    return len(dispatched)


def _select_repair_targets(lint_errors: list[dict[str, Any]], max_files: int) -> list[dict[str, Any]]:
    """按优先级选择本轮修复的文件:入口链 > api/inc 公共库 > 其余,同文件合并错误。"""
    by_file: dict[str, list[dict[str, Any]]] = {}
    for err in lint_errors:
        f = str(err.get("file") or "").strip()
        if f:
            by_file.setdefault(f, []).append(err)

    def priority(path: str) -> int:
        p = path.lower()
        if p == "index.php" or p.startswith("index/"):
            return 0
        if p.startswith("api/") or p.startswith("inc/") or p.startswith("classes/"):
            return 1
        return 2

    ordered = sorted(by_file.items(), key=lambda kv: (priority(kv[0]), kv[0]))
    out: list[dict[str, Any]] = []
    for path, errs in ordered[:max_files]:
        out.append({"file": path, "errors": errs})
    return out


def _syntax_repair_round(
    db: Session,
    environment: Any,
    source_archive_base64: str,
    lint_errors: list[dict[str, Any]],
    *,
    parent_source_sha256: str | None = None,
) -> dict[str, Any] | None:
    """对白盒 php -l 报错文件调用修复 Agent,返回写回修复后的新 zip。

    失败静默(只记事件不阻断):LLM 未配置/生成失败都跳过本轮修复。
    """
    try:
        from app.agents.base import AgentContext

        max_files = int(getattr(settings, "sandbox_repair_max_files", 8) or 8)
        targets = _select_repair_targets(lint_errors, max_files)
        if not targets:
            return None
        raw = base64.b64decode(source_archive_base64)
        files: dict[str, str] = {}
        errors_payload: list[dict[str, Any]] = []
        with zipfile.ZipFile(io.BytesIO(raw)) as zf:
            names = set(zf.namelist())
            for target in targets:
                path = target["file"]
                if path in names:
                    try:
                        files[path] = zf.read(path).decode("utf-8", errors="replace")
                    except Exception:
                        continue
                errors_payload.extend(target["errors"])
        if not files:
            return None
        agent = configure_subagent(db, SyntaxRepairAgent(), environment.owner_id)
        if not agent._api_key:
            _append_event(db, environment, "progress", "syntax_repair", "LLM 未配置,跳过后端语法修复")
            db.commit()
            return None
        ctx = AgentContext(
            user_id=environment.owner_id,
            project_id=environment.project_id,
            extra={"trace_id": environment.public_id, "before_model_call": _execution_model_guard(db, environment)},
        )
        result = agent.repair(
            language=environment.language,
            errors=errors_payload,
            files=files,
            ctx=ctx,
        )
        ctx.extra["before_model_call"]()
        repaired = result.get("files") if isinstance(result.get("files"), dict) else {}
        if not repaired:
            _append_event(
                db,
                environment,
                "progress",
                "syntax_repair",
                f"语法修复未生成: {str(result.get('error') or '空结果')[:120]}",
            )
            db.commit()
            return None
        # 写回 zip:重建 zip 并替换同名成员(不能用 append,否则产生重复条目导致 executor 解压失败)
        buf = io.BytesIO()
        with zipfile.ZipFile(io.BytesIO(raw), "r") as zin, zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zout:
            for info in zin.infolist():
                data = zin.read(info.filename)
                if info.filename in repaired:
                    data = repaired[info.filename].encode("utf-8")
                zout.writestr(info, data)
        new_source = base64.b64encode(buf.getvalue()).decode("ascii")
        # 修复后的源码作为不可变项目副本持久化;执行快照须可回指到该副本。
        try:
            saved = project_source_revision_service.save_revision(
                db,
                project_id=environment.project_id,
                owner_id=environment.owner_id,
                repaired_source_base64=new_source,
                repaired_files=sorted(repaired),
                parent_sha256=parent_source_sha256 or environment.source_sha256,
                repair_notes=f"沙箱语法修复第{len(lint_errors)}处错误,修复{len(repaired)}文件",
            )
            if saved is None:
                raise RuntimeError("源码副本保存服务未返回可核验版本")
            repaired_bytes = base64.b64decode(new_source, validate=True)
            revision_archive = project_source_revision_service._strip_internal_members(repaired_bytes)
            revision_sha256 = hashlib.sha256(revision_archive).hexdigest()
            if not hmac.compare_digest(revision_sha256, str(saved.source_sha256 or "")) or not hmac.compare_digest(
                hashlib.sha256(bytes(saved.archive_blob)).hexdigest(), revision_sha256
            ):
                raise RuntimeError("修复副本与待重跑源码内容不一致")
            execution_sha256 = hashlib.sha256(repaired_bytes).hexdigest()
            revision_metadata = {
                "revision_id": int(saved.id),
                "revision_no": int(saved.revision_no),
                "parent_source_sha256": parent_source_sha256 or environment.source_sha256,
                "revision_source_sha256": revision_sha256,
                "execution_source_sha256": execution_sha256,
                "repaired_files": sorted(repaired),
            }
            saved_note = f", 已保存为项目源码副本 rev#{saved.revision_no} ({revision_sha256[:12]})"
        except Exception as exc:  # noqa: BLE001 - 无法固化并校验的修复版本不得重跑
            db.rollback()
            environment = db.get(SandboxEnvironment, environment.id) or environment
            _append_event(
                db,
                environment,
                "failed",
                "syntax_repair",
                f"修复副本未能通过完整性校验，已阻止重跑: {str(exc)[:100]}",
                {"repaired_files": sorted(repaired), "round_errors": len(lint_errors)},
            )
            db.commit()
            return None
        _append_event(
            db,
            environment,
            "progress",
            "syntax_repair",
            f"后端语法修复 Agent 已修复 {len(repaired)} 个文件({', '.join(sorted(repaired)[:8])}){saved_note}",
            {
                "repaired_files": sorted(repaired),
                "round_errors": len(lint_errors),
                "repair_revision": revision_metadata,
            },
        )
        db.commit()
        return {
            "source": new_source,
            "files": sorted(repaired),
            "repair_revision": revision_metadata,
        }
    except (ForbiddenError, NotFoundError):
        raise
    except Exception as exc:  # noqa: BLE001 - 修复失败不阻断原测试链
        _append_event(db, environment, "progress", "syntax_repair", f"语法修复异常: {str(exc)[:120]}")
        db.commit()
        return None


def heartbeat_and_recover_sandboxes(db: Session) -> dict[str, int]:
    """长任务心跳与卡死回收：只有 Worker 确认终止后才将任务置为终态。"""
    active = db.query(SandboxEnvironment).filter(SandboxEnvironment.status.in_(ACTIVE_STATES)).all()
    # Previous watchdog releases could write ``failed`` before Worker confirmed
    # cleanup. Retry only those legacy rows that explicitly retained the failed
    # cleanup receipt; unrelated failed jobs remain terminal and untouched.
    legacy_cleanup_pending = (
        db.query(SandboxEnvironment)
        .filter(
            SandboxEnvironment.status == "failed",
            SandboxEnvironment.result_json.like("%cleanup_error%"),
        )
        .all()
    )
    environments_by_id = {environment.id: environment for environment in (*active, *legacy_cleanup_pending)}
    active = list(environments_by_id.values())
    heartbeat_count = 0
    recovered_count = 0
    cleanup_pending_count = 0
    now = _utcnow()
    for environment in active:
        last_event = (
            db.query(SandboxEvent.create_time)
            .filter(SandboxEvent.environment_id == environment.id)
            .order_by(SandboxEvent.id.desc())
            .first()
        )
        last_at = last_event[0] if last_event else environment.create_time
        elapsed = max(0, int((now - last_at).total_seconds())) if last_at else 0
        if elapsed >= int(settings.sandbox_heartbeat_seconds):
            _append_event(
                db,
                environment,
                "heartbeat",
                "executor",
                f"沙箱仍在运行：status={environment.status}，距上次事件 {elapsed}s",
                {"status": environment.status, "elapsed_seconds": elapsed},
            )
            heartbeat_count += 1
            observe_event("sandbox_heartbeat", labels={"status": environment.status})
        started = environment.started_at or environment.create_time
        if started is None or (now - started).total_seconds() < int(settings.sandbox_stuck_after_seconds):
            continue

        project = db.get(Project, environment.project_id)
        project_name = project.project_name if project is not None else None
        project_label = (
            f"{project_name}#{environment.project_id}" if project_name else f"project#{environment.project_id}"
        )
        cleanup_error = None
        try:
            worker = db.get(SandboxWorker, environment.worker_id) if environment.worker_id else None
            if worker is None:
                raise RuntimeError("沙箱关联的 Worker 不存在，无法确认资源终止")
            _stop_registered_worker_requests(worker, environment)
        except Exception as exc:  # noqa: BLE001 - preserve retryable state and continue other sandboxes
            cleanup_error = " ".join(str(exc).split())[:300] or type(exc).__name__

        elapsed_seconds = int((now - started).total_seconds())
        if cleanup_error:
            environment.status = "stopping"
            environment.error = "沙箱心跳超时，Worker 未确认资源终止，等待回收重试"
            environment.stopped_at = None
            title = f"沙箱 {environment.public_id} 资源回收待重试（项目 {project_label}）"
            reason = "Worker 未确认资源终止；任务保留 stopping 并由后续 watchdog 周期重试"
            _append_event(
                db,
                environment,
                "progress",
                "cleanup",
                "Worker 未确认沙箱资源终止，保留 stopping 并等待重试",
                {"cleanup_error": cleanup_error, "elapsed_seconds": elapsed_seconds},
            )
            cleanup_pending_count += 1
        else:
            environment.status = "failed"
            environment.error = "沙箱心跳超时，Worker 已确认资源终止"
            environment.stopped_at = now
            prior_result = _loads(environment.result_json, {})
            if isinstance(prior_result, dict) and prior_result.get("cleanup_error"):
                prior_result.pop("cleanup_error", None)
                prior_result["cleanup_confirmed"] = True
                environment.result_json = _json(prior_result)
            title = f"沙箱 {environment.public_id} 心跳超时，Worker 已确认回收（项目 {project_label}）"
            reason = "沙箱心跳超时，Worker 已确认全部请求资源终止"
            _append_event(db, environment, "failed", "watchdog", reason)
            recovered_count += 1
            observe_event("sandbox_stuck_recovered", labels={"status": environment.status})

        alert = (
            db.query(AgentAlert)
            .filter_by(alert_type="sandbox_stuck", fingerprint=environment.public_id, status="open")
            .order_by(AgentAlert.id.desc())
            .first()
        )
        alert_detail = {
            "public_id": environment.public_id,
            "project_id": environment.project_id,
            "project_name": project_name,
            "reason": reason,
            "elapsed_seconds": elapsed_seconds,
            "cleanup_confirmed": cleanup_error is None,
            "cleanup_error": cleanup_error,
        }
        if alert is None:
            alert = AgentAlert(
                alert_type="sandbox_stuck",
                category="sandbox_stuck",
                source="sandbox_watchdog",
                severity="high",
                user_id=environment.owner_id,
                fingerprint=environment.public_id,
            )
            db.add(alert)
        alert.title = title[:200]
        alert.detail_json = _json(alert_detail)
    db.commit()
    return {
        "heartbeat": heartbeat_count,
        "recovered": recovered_count,
        "cleanup_pending": cleanup_pending_count,
    }


def _execute_environment(
    environment_id: int,
    source_archive_base64: str | None = None,
    execution_token: str | None = None,
) -> None:
    db = SessionLocal()
    usage_scope = ExitStack()
    try:
        environment = db.get(SandboxEnvironment, environment_id)
        if not environment or environment.status not in {"queued", "recovering"}:
            return
        if execution_token is not None and str(environment.execution_token or "") != execution_token:
            return
        _require_execution_lease(db, environment_id, execution_token)
        usage_scope.enter_context(usage_context(int(environment.owner_id), model_attribution(environment), db=db))
        if source_archive_base64 is None:
            if environment.source_archive_blob is None:
                raise RuntimeError("沙箱缺少持久化源码快照")
            source_bytes = bytes(environment.source_archive_blob)
            if not hmac.compare_digest(hashlib.sha256(source_bytes).hexdigest(), environment.source_sha256):
                raise RuntimeError("沙箱持久化源码快照完整性校验失败")
            source_archive_base64 = base64.b64encode(source_bytes).decode("ascii")
        worker = db.get(SandboxWorker, environment.worker_id) if environment.worker_id else None
        config = _loads(environment.agent_config_json, {})
        if not worker and not config.get("remote_only"):
            raise RuntimeError("Sandbox worker 已被删除")
        environment.status = "dispatching"
        _append_event(db, environment, "progress", "executor", "独立执行器已接收固定测试配置")
        _commit_execution(db, environment_id, execution_token)
        _emit(environment, AgentEventType.PROGRESS, "独立执行器已接收任务", {"stage": "executor"})
        worker_mode = config.get("worker_mode", environment.test_mode)
        # 隔离源码项目建档语言可能与真实源码不符(上传时手动选择)。白盒/黑盒测试
        # 以源码内容推断的主语言为准纠正受控运行时,避免 PHP 项目被按 Python 跑而
        # 找不到部署入口。deploy 目的仍由 create_environment 以项目主语言严格裁决。
        source_language = _dominant_archive_language(source_archive_base64)
        if source_language and source_language != environment.language:
            if environment.purpose == "test" and source_language in LANGUAGES:
                _append_event(
                    db,
                    environment,
                    "dispatch",
                    "language",
                    f"已按源码内容将测试运行时从 {environment.language} 纠正为 {source_language}",
                    {"declared_language": environment.language, "resolved_language": source_language},
                )
                environment.language = source_language
                _commit_execution(db, environment_id, execution_token)
        # deploy 前自动白盒:测试容器跑完即回收、不占槽,避免常驻 deploy 把单槽 worker 占成 429。
        pre_whitebox: dict[str, Any] | None = None
        if worker and environment.purpose == "deploy" and environment.agent_code == "sandbox_deployer":
            pre_whitebox = _run_deploy_auto_tests(db, environment, worker, source_archive_base64, modes=("whitebox",))[
                0
            ]
            db.refresh(environment)
            if environment.status in {"stopping", "stopped", "expired"}:
                return
        effective_source = source_archive_base64
        effective_sha = environment.source_sha256
        repair_round = 0
        worker_receipt: dict[str, str] | None = None
        expected_agent_tests: set[str] = set()
        agent_test_generation: dict[str, Any] | None = None
        if environment.purpose == "test" and config.get("remote_only"):
            agent_test_generation = {
                "status": "skipped",
                "mode": worker_mode,
                "count": 0,
                "reason": "远程只读 HTTP 探测不注入 AI 动态测试用例",
            }
        if getattr(environment, "execution_archive_blob", None) is not None:
            execution_bytes = bytes(environment.execution_archive_blob)
            execution_sha = hashlib.sha256(execution_bytes).hexdigest()
            if not getattr(environment, "execution_source_sha256", None) or not hmac.compare_digest(
                execution_sha,
                environment.execution_source_sha256,
            ):
                raise RuntimeError("沙箱 Worker 执行快照完整性校验失败")
            effective_source = base64.b64encode(execution_bytes).decode("ascii")
            effective_sha = execution_sha
            repair_round = int(getattr(environment, "execution_round", 0) or 0)
            expected_agent_tests = _agent_test_paths(effective_source)
            if expected_agent_tests:
                agent_test_generation = {
                    "status": "generated",
                    "mode": worker_mode,
                    "count": len(expected_agent_tests),
                    "source": "persisted_execution_snapshot",
                }
        elif worker and environment.purpose == "test":
            # 压缩、部署规划与动态用例共享一个时间预算，禁止各阶段分别重置超时。
            agent_context_deadline = time.monotonic() + int(
                getattr(settings, "sandbox_agent_test_generation_seconds", 300) or 300
            )
            # 1) 完整部署核验:LLM 判断入口/依赖,生成补全启动脚本 _prism_launch.sh
            deploy_patch = _generate_deployment_patch(
                db,
                environment,
                source_archive_base64,
                environment.language,
                deadline=agent_context_deadline,
            )
            if deploy_patch and deploy_patch.get("launch_script"):
                effective_source = _inject_deployment_patch(effective_source, deploy_patch["launch_script"])
            # 2) agent 动态生成白盒/黑盒测试用例,注入 _agent_tests/ 后由沙箱 runner 确定性执行
            agent_test_files = _generate_agent_test_cases(
                db,
                environment,
                effective_source,
                environment.language,
                worker_mode,
                deadline=agent_context_deadline,
            )
            if agent_test_files:
                expected_agent_tests = {
                    str(item.get("path") or "").strip()
                    for item in agent_test_files
                    if str(item.get("path") or "").strip()
                }
                effective_source = _inject_agent_test_files(effective_source, agent_test_files)
                agent_test_generation = {
                    "status": "generated",
                    "mode": worker_mode,
                    "count": len(expected_agent_tests),
                    "source": "current_execution",
                }
            else:
                # 该结果说明确定性 runner 没有收到 AI 动态补充用例。
                # 不能把动态测试缺失伪装成通过；基础 runner 的结果仍独立保留。
                agent_test_generation = {
                    "status": "skipped",
                    "mode": worker_mode,
                    "count": 0,
                    "reason": "未生成可执行的 AI 动态测试用例；结果只覆盖确定性 runner 实际执行的检查。详情见 agent_tests/source_compaction 事件。",
                }
            if effective_source != source_archive_base64:
                effective_sha = hashlib.sha256(base64.b64decode(effective_source)).hexdigest()
        if worker:
            max_repair_rounds = int(getattr(settings, "sandbox_max_repair_rounds", 2) or 2)
            while True:
                # 尽量在提交前观察本地停止状态；最终竞态仍由 Worker
                # 的持久化 stop tombstone 协议封闭。
                db.refresh(environment)
                if environment.status in {"stopping", "stopped", "expired"}:
                    return
                _require_execution_lease(db, environment_id, execution_token)
                worker_request_id = (
                    environment.public_id if repair_round == 0 else f"{environment.public_id}-r{repair_round}"
                )
                last_sequence = 0

                def persist_worker_events(state: dict[str, Any]) -> None:
                    nonlocal last_sequence
                    _require_execution_lease(db, environment_id, execution_token)
                    worker_events = state.get("events") if isinstance(state.get("events"), list) else []
                    for item in worker_events:
                        if not isinstance(item, dict):
                            continue
                        _append_event(
                            db,
                            environment,
                            str(item.get("event_type") or "progress"),
                            str(item.get("stage") or "executor"),
                            str(item.get("message") or "worker 进度"),
                            item.get("payload") if isinstance(item.get("payload"), dict) else {},
                        )
                    last_sequence = int(state.get("last_sequence") or last_sequence)

                if environment.started_at is None:
                    environment.started_at = _utcnow()
                    _commit_execution(db, environment_id, execution_token)
                sandbox_db_type = str(
                    (_loads(getattr(environment, "agent_config_json", None) or "{}", {}) or {}).get("db_type") or "none"
                )
                execution_bytes = base64.b64decode(effective_source)
                request_config_json = _worker_request_config_json(environment, worker_request_id)
                saved_request = {
                    "request_id": worker_request_id,
                    "purpose": environment.purpose,
                    "language": environment.language,
                    "test_mode": worker_mode if worker_mode in {"whitebox", "blackbox", "combined"} else "whitebox",
                    "db_type": sandbox_db_type,
                    "source_sha256": effective_sha,
                    "source_revision_id": config.get("source_revision_id"),
                    "source_revision_sha256": config.get("source_revision_sha256"),
                    "repair_round": repair_round,
                    "ttl_seconds": max(60, int((environment.expires_at - _utcnow()).total_seconds())),
                    "image_digest": environment.image_digest or "",
                }
                persisted_request = _loads(getattr(environment, "worker_request_json", None), {})
                if (
                    isinstance(persisted_request, dict)
                    and persisted_request.get("request_id") == worker_request_id
                    and persisted_request.get("source_sha256") == effective_sha
                ):
                    saved_request = persisted_request
                if not _persist_worker_execution_snapshot(
                    db,
                    environment,
                    execution_bytes=execution_bytes,
                    source_sha256=effective_sha,
                    repair_round=repair_round,
                    request_envelope=saved_request,
                    request_config_json=request_config_json,
                    execution_token=execution_token,
                ):
                    return
                _commit_execution(db, environment_id, execution_token)
                environment = db.get(SandboxEnvironment, environment_id)
                if environment is None:
                    return
                if environment.status in {"stopping", "stopped", "expired"}:
                    try:
                        _stop_registered_worker_requests(worker, environment)
                        if environment.status == "stopping":
                            environment.status = "stopped"
                            environment.stopped_at = _utcnow()
                    except Exception as exc:  # noqa: BLE001 - keep cleanup visible and retryable
                        environment.status = "stopping"
                        environment.error = f"停止 worker 失败：{str(exc)[:1000]}"
                    _commit_execution(db, environment_id, execution_token)
                    return
                execute_payload = _worker_execute_payload(saved_request, effective_source)
                _require_execution_lease(db, environment_id, execution_token)
                execute_response = _call_worker(
                    worker,
                    "POST",
                    "/execute",
                    execute_payload,
                )
                _require_execution_lease(db, environment_id, execution_token)
                result = (
                    execute_response.get("result")
                    if isinstance(execute_response.get("result"), dict)
                    else execute_response
                )
                worker_receipt = _validate_worker_execution_receipt(
                    result,
                    request_id=worker_request_id,
                    source_sha256=effective_sha,
                )
                last_sequence = 0
                configured_policy = _loads(environment.resource_policy_json, {})
                deadline = time.monotonic() + int(configured_policy.get("timeout_seconds") or 600) + 180
                persist_worker_events(result)
                _commit_execution(db, environment_id, execution_token)
                while not _worker_status_is_terminal(
                    environment.purpose,
                    str(result.get("status") or ""),
                ):
                    if time.monotonic() >= deadline:
                        raise RuntimeError("Sandbox worker 状态轮询超时")
                    time.sleep(1)
                    _require_execution_lease(db, environment_id, execution_token)
                    status_response = _call_worker(
                        worker,
                        "POST",
                        "/status",
                        {
                            "request_id": worker_request_id,
                            "after_sequence": last_sequence,
                        },
                    )
                    _require_execution_lease(db, environment_id, execution_token)
                    result = (
                        status_response.get("result")
                        if isinstance(status_response.get("result"), dict)
                        else status_response
                    )
                    worker_receipt = _validate_worker_execution_receipt(
                        result,
                        request_id=worker_request_id,
                        source_sha256=effective_sha,
                    )
                    persist_worker_events(result)
                    _commit_execution(db, environment_id, execution_token)
                environment = db.get(SandboxEnvironment, environment_id)
                _require_execution_lease(db, environment_id, execution_token)
                if environment.status in {"stopping", "stopped", "expired"}:
                    if environment.status == "stopping":
                        try:
                            _stop_registered_worker_requests(worker, environment)
                            environment.status = "stopped"
                            environment.stopped_at = _utcnow()
                        except Exception as exc:  # noqa: BLE001 - keep cleanup visible and retryable
                            environment.error = f"停止 worker 失败：{str(exc)[:1000]}"
                        _commit_execution(db, environment_id, execution_token)
                    return
                # ── 后端语法修复:白盒 php -l 报错时,LLM 修复文件后重跑(最多 max_repair_rounds 轮) ──
                if environment.purpose == "test" and environment.language == "php" and repair_round < max_repair_rounds:
                    worker_concl = result.get("result") if isinstance(result.get("result"), dict) else result
                    wlogs = worker_concl.get("logs") if isinstance(worker_concl, dict) else None
                    log_text = str((wlogs or {}).get("text") or "")
                    lint_errors = collect_php_lint_errors(log_text)
                    if lint_errors:
                        repaired = _syntax_repair_round(
                            db,
                            environment,
                            effective_source,
                            lint_errors,
                            parent_source_sha256=effective_sha,
                        )
                        if repaired:
                            effective_source = repaired["source"]
                            effective_sha = hashlib.sha256(base64.b64decode(effective_source)).hexdigest()
                            revision = repaired.get("repair_revision")
                            if not isinstance(revision, dict) or not hmac.compare_digest(
                                str(revision.get("execution_source_sha256") or ""),
                                effective_sha,
                            ):
                                raise RuntimeError("修复副本执行哈希与下一轮 Worker 输入不一致")
                            repair_round += 1
                            next_request_id = f"{environment.public_id}-r{repair_round}"
                            next_config_json = _append_repair_revision_to_config(
                                _worker_request_config_json(environment, next_request_id),
                                revision=revision,
                                repair_round=repair_round,
                                worker_request_id=next_request_id,
                            )
                            next_request = {
                                "request_id": next_request_id,
                                "purpose": environment.purpose,
                                "language": environment.language,
                                "test_mode": (
                                    worker_mode if worker_mode in {"whitebox", "blackbox", "combined"} else "whitebox"
                                ),
                                "db_type": sandbox_db_type,
                                "source_sha256": effective_sha,
                                "source_revision_id": revision.get("revision_id"),
                                "source_revision_sha256": revision.get("revision_source_sha256"),
                                "repair_revision_id": revision.get("revision_id"),
                                "repair_round": repair_round,
                                "ttl_seconds": max(60, int((environment.expires_at - _utcnow()).total_seconds())),
                                "image_digest": environment.image_digest or "",
                            }
                            if not _persist_worker_execution_snapshot(
                                db,
                                environment,
                                execution_bytes=base64.b64decode(effective_source),
                                source_sha256=effective_sha,
                                repair_round=repair_round,
                                request_envelope=next_request,
                                request_config_json=next_config_json,
                                execution_token=execution_token,
                            ):
                                return
                            _commit_execution(db, environment_id, execution_token)
                            continue
                break
        else:
            result = {"request_id": environment.public_id, "status": "succeeded", "result": {"exit_code": 0}}
        state = str(result.get("status") or "failed")
        _require_execution_lease(db, environment_id, execution_token)
        target_status = {
            "completed": "succeeded",
            "succeeded": "succeeded",
            "running": "ready",
            "blocked": "blocked",
            "stopped": "stopped",
        }.get(state, "failed")
        # Worker 的确定性结果已返回，但制品和多 Agent 报告尚未完成。
        # 先用租约条件更新进入 finalizing，旧 Worker 不能用普通 ORM commit 抢占新租约。
        if not _enter_finalizing(
            db,
            environment,
            result=result,
            target_status=target_status,
            execution_token=execution_token,
        ):
            return
        _commit_execution(db, environment_id, execution_token)
        environment = db.get(SandboxEnvironment, environment_id)
        if environment is None:
            return
        auto_smoke: dict[str, Any] | None = None
        auto_test_chain: list[dict[str, Any]] = []
        if (
            environment.purpose == "deploy"
            and target_status == "ready"
            and environment.agent_code == "sandbox_deployer"
        ):
            # 预览冒烟 = 黑盒(从环境外部对运行中的服务发真实 HTTP,单槽下无法另起黑盒容器)。
            _require_execution_lease(db, environment_id, execution_token)
            auto_smoke = _run_auto_smoke_test(db, environment)
            _append_event(
                db,
                environment,
                "complete" if auto_smoke.get("passed") else "progress",
                "auto_smoke",
                (
                    "部署后自动 Agent 冒烟测试完成"
                    if auto_smoke.get("passed")
                    else "部署后自动 Agent 冒烟测试未通过或不可用"
                ),
                auto_smoke,
            )
            _emit(
                environment,
                AgentEventType.COMPLETE if auto_smoke.get("passed") else AgentEventType.PROGRESS,
                "部署后自动 Agent 冒烟测试完成",
                {"stage": "auto_smoke", "passed": bool(auto_smoke.get("passed"))},
            )
            _commit_execution(db, environment_id, execution_token)
        if environment.purpose == "deploy":
            if pre_whitebox:
                auto_test_chain.append(pre_whitebox)
            if auto_smoke is not None and auto_smoke.get("available"):
                auto_test_chain.append(
                    {
                        "mode": "service_smoke",
                        "passed": bool(auto_smoke.get("passed")),
                        "status_code": auto_smoke.get("status_code"),
                        "latency_ms": auto_smoke.get("latency_ms"),
                        "via": "preview_smoke",
                        "scope": "http_root_smoke",
                    }
                )
        _require_execution_lease(db, environment_id, execution_token)
        worker_conclusion = result.get("result") if isinstance(result.get("result"), dict) else result
        evidence: dict[str, Any] = {"worker_result": worker_conclusion}
        if agent_test_generation is not None:
            evidence["agent_test_generation"] = agent_test_generation
        if environment.purpose == "test":
            evidence["deterministic_test_execution"] = {
                "requested_mode": str(environment.test_mode or ""),
                "worker_mode": str(worker_mode or ""),
                "status": "passed" if _worker_execution_passed(
                    purpose=environment.purpose,
                    state=state,
                    target_status=target_status,
                    conclusion=worker_conclusion,
                ) else "failed",
                "exit_code": worker_conclusion.get("exit_code"),
            }
        agent_tests_result: dict[str, Any] | None = None
        worker_logs = (
            worker_conclusion.get("logs")
            if isinstance(worker_conclusion, dict) and isinstance(worker_conclusion.get("logs"), dict)
            else None
        )
        expected_decompilation = config.get("decompilation") if isinstance(config.get("decompilation"), dict) else {}
        expected_decompilation_status = str(expected_decompilation.get("status") or "skipped")
        if expected_decompilation_status == "unsupported":
            target_status = "failed"
            evidence["decompilation"] = {
                **expected_decompilation,
                "status": "unsupported",
                "reason": str(expected_decompilation.get("reason") or "输入类型不受支持"),
            }
            _append_event(
                db,
                environment,
                "failed",
                "decompilation",
                "反编译输入类型不受支持，已失败关闭",
                evidence["decompilation"],
            )
            _commit_execution(db, environment_id, execution_token)
        if expected_decompilation_status == "planned" and not (
            worker_logs and str(worker_logs.get("text") or "").strip()
        ):
            raise RuntimeError("反编译 runner 未返回执行日志")
        if worker_logs and str(worker_logs.get("text") or ""):
            log_text_for_facts = str(worker_logs["text"])
            recon_facts = _extract_prism_facts(log_text_for_facts)
            if recon_facts:
                evidence["recon_facts"] = recon_facts
                _persist_browser_artifact(
                    db,
                    environment,
                    artifact_type="recon_facts",
                    file_name=f"recon-facts-{environment.public_id}.json",
                    mime_type="application/json",
                    content=json.dumps(recon_facts, ensure_ascii=False).encode("utf-8"),
                    execution_token=execution_token,
                )
            agent_tests_result = _extract_agent_tests_result(log_text_for_facts)
            decompilation_result = _extract_decompilation_result(log_text_for_facts)
            if expected_decompilation_status == "planned":
                if decompilation_result is None:
                    raise RuntimeError("反编译 runner 未返回唯一可信结果")
                if decompilation_result.get("status") != "succeeded":
                    target_status = "failed"
                evidence["decompilation"] = decompilation_result
                _append_event(
                    db,
                    environment,
                    "complete" if decompilation_result.get("status") == "succeeded" else "failed",
                    "decompilation",
                    "Android 制品反编译完成"
                    if decompilation_result.get("status") == "succeeded"
                    else "Android 制品反编译失败",
                    decompilation_result,
                )
                _commit_execution(db, environment_id, execution_token)
            elif expected_decompilation_status != "unsupported" and decompilation_result is not None:
                evidence["decompilation"] = decompilation_result
        agent_tests_result = _reconcile_agent_tests_result(expected_agent_tests, agent_tests_result)
        if agent_tests_result is not None:
            evidence["agent_tests"] = agent_tests_result
            generated = int(agent_tests_result.get("generated") or 0)
            agent_ok = _agent_tests_succeeded(agent_tests_result)
            if generated == 0:
                message = "未注入 agent 动态测试用例,沿用常规测试结果"
                event_type = "progress"
            else:
                message = (
                    f"agent 动态测试{'通过' if agent_ok else '未通过'}"
                    f"(生成 {generated} 个,通过 {int(agent_tests_result.get('passed_count') or 0)} 个)"
                )
                event_type = "complete" if agent_ok else "failed"
            _append_event(db, environment, event_type, "agent_tests", message, agent_tests_result)
            _commit_execution(db, environment_id, execution_token)
        if (
            environment.purpose == "test"
            and worker is not None
            and str(environment.test_mode or "") in {"blackbox", "combined"}
        ):
            blackbox_result = _extract_blackbox_result(str(worker_logs.get("text") or "") if worker_logs else "")
            blackbox_result = _reconcile_blackbox_agent_assertions(
                blackbox_result,
                generation=agent_test_generation,
                expected_files=expected_agent_tests,
                agent_tests_result=agent_tests_result,
            )
            if blackbox_result is None:
                evidence["blackbox_execution"] = {
                    "status": "receipt_missing",
                    "reason": "固定 runner 未返回唯一有效的黑盒路由回执",
                }
                target_status = "failed"
                _append_event(
                    db, environment, "failed", "blackbox",
                    "黑盒执行回执缺失或无效，已失败关闭",
                    evidence["blackbox_execution"],
                )
            else:
                evidence["blackbox_execution"] = blackbox_result
                if blackbox_result["status"] != "passed":
                    target_status = "failed"
                    _append_event(
                        db, environment, "failed", "blackbox",
                        "黑盒路由探测或动态断言未通过",
                        blackbox_result,
                    )
        if environment.remote_target_url:
            _append_event(db, environment, "progress", "remote_blackbox", "已在授权边界内调用远程 HTTP(S) 黑盒探测")
            _commit_execution(db, environment_id, execution_token)
            _require_execution_lease(db, environment_id, execution_token)
            evidence["remote_blackbox"] = _probe_remote_target(environment.remote_target_url)
            remote_execution = _remote_blackbox_execution(evidence["remote_blackbox"])
            evidence["remote_blackbox_execution"] = remote_execution
            if config.get("remote_only") or str(environment.test_mode or "") == "combined":
                evidence["blackbox_execution"] = remote_execution
            if not _remote_blackbox_passed(remote_execution):
                target_status = "failed"
        passed = _execution_result_passed(
            purpose=environment.purpose,
            state=state,
            target_status=target_status,
            conclusion=worker_conclusion,
            test_mode=str(environment.test_mode or ""),
            evidence=evidence,
            agent_tests_result=agent_tests_result,
            agent_test_generation=agent_test_generation,
            remote_target_url=environment.remote_target_url,
        )
        if agent_test_generation and agent_test_generation.get("status") == "generated" and agent_tests_result is None:
            evidence["agent_tests_receipt"] = {
                "status": "missing",
                "reason": "已注入 AI 动态测试，但 runner 没有返回可信执行结果",
            }
        if environment.purpose == "test":
            evidence["verification_coverage"] = _sandbox_verification_coverage({
                "passed": passed,
                "evidence": evidence,
            })
        final_status = "failed" if environment.purpose == "test" and not passed else target_status
        if environment.purpose == "deploy":
            summary = "部署就绪" if passed else "部署失败"
            if auto_test_chain:
                wb = next((r for r in auto_test_chain if r.get("mode") == "whitebox"), None)
                smoke = next((r for r in auto_test_chain if r.get("mode") == "service_smoke"), None)
                summary += (
                    f"；自动核验 白盒{'✓' if wb and wb.get('passed') else '✗'}"
                    f"/服务冒烟{'✓' if smoke and smoke.get('passed') else '✗'}"
                )
            if auto_smoke and auto_smoke.get("available"):
                summary += f"；根路径 HTTP 冒烟{'✓' if auto_smoke.get('passed') else '✗'}"
        else:
            mode_label = {
                "whitebox": "白盒",
                "blackbox": "黑盒",
                "combined": "组合",
            }.get(str(environment.test_mode or ""), "沙箱")
            remote_execution = evidence.get("remote_blackbox_execution")
            if isinstance(remote_execution, dict) and remote_execution.get("status") == "partial":
                summary = (
                    f"{mode_label}验证未完成：远程目标返回 HTTP {remote_execution.get('status_code')}；"
                    "目标有响应，但未通过功能路径验证"
                )
            elif passed and agent_test_generation and agent_test_generation.get("status") == "skipped":
                summary = f"确定性{mode_label}测试通过；AI动态补充未执行"
            elif passed and agent_tests_result is not None:
                summary = f"{mode_label}测试与AI动态用例通过"
            else:
                summary = f"{mode_label}测试通过" if passed else f"{mode_label}测试未通过"
        conclusion = {
            "passed": passed,
            "summary": summary,
            "evidence": evidence,
            "agent_code": environment.agent_code,
            "source_provenance": _source_provenance(environment, worker_receipt),
        }
        if agent_tests_result is not None:
            conclusion["agent_tests"] = agent_tests_result
            if isinstance(agent_tests_result.get("details"), dict) and agent_tests_result["details"]:
                evidence["agent_test_details"] = agent_tests_result["details"]
        if auto_test_chain:
            conclusion["auto_test_chain"] = auto_test_chain
            evidence["auto_test_chain"] = auto_test_chain
        if auto_smoke is not None:
            conclusion["auto_smoke_test"] = auto_smoke
            evidence["auto_smoke_test"] = auto_smoke
        environment.result_json = _json(conclusion)
        artifacts = _persist_artifacts(db, environment, conclusion, execution_token)
        # 先发布确定性结果并释放 SandboxEnvironment 行锁；此时仍为
        # finalizing，调度器不会把尚未完成多 Agent 报告的结果误判为终态。
        _commit_execution(db, environment_id, execution_token)
        # 黑白盒链路结束后,由多Agent审查编排产出中文报告(失败只记录,不阻断)
        _require_execution_lease(db, environment_id, execution_token)
        review_report = _run_test_review_report(db, environment, conclusion)
        _require_execution_lease(db, environment_id, execution_token)
        if review_report is not None:
            conclusion["multi_agent_review"] = review_report
        final_result_json = _json(conclusion)
        if not _complete_finalizing_transition(
            db,
            environment,
            final_status=final_status,
            result_json=final_result_json,
            execution_token=execution_token,
        ):
            # 报告生成期间已被另一会话停止或到期回收，保留真实终态。
            return
        _append_event(
            db,
            environment,
            "complete" if passed else "failed",
            "conclusion",
            conclusion["summary"],
            {"passed": passed, "artifact_count": len(artifacts), "multi_agent_review": bool(review_report)},
        )
        _commit_execution(db, environment_id, execution_token)
        try:
            strategy_learning_service.observe_sandbox_outcome(db, environment, conclusion)
            _commit_execution(db, environment_id, execution_token)
        except Exception:  # noqa: BLE001 - 策略固化失败不得篡改已持久化的沙箱结论
            db.rollback()
        _emit(
            environment,
            AgentEventType.COMPLETE if passed else AgentEventType.FAILED,
            conclusion["summary"],
            {"stage": "conclusion", "passed": passed},
        )
    except Exception as exc:
        db.rollback()
        environment = db.get(SandboxEnvironment, environment_id)
        if environment:
            if execution_token is not None and not _execution_lease_valid(db, environment_id, execution_token):
                return
            if environment.status in {"stopped", "expired"}:
                return
            failure = str(exc)[:4000]
            cancellation_requested = environment.status == "stopping"
            worker = db.get(SandboxWorker, environment.worker_id) if environment.worker_id else None
            cleanup_confirmed = environment.worker_id is None
            cleanup_error = ""
            if worker:
                environment.status = "stopping"
                environment.error = failure
                _append_event(db, environment, "progress", "cleanup", "执行异常，正在回收已提交的 Worker 请求")
                _commit_execution(db, environment_id, execution_token)
                try:
                    _stop_registered_worker_requests(worker, environment)
                    cleanup_confirmed = True
                except Exception as cleanup_exc:  # noqa: BLE001 - remain nonterminal until retry succeeds
                    cleanup_error = str(cleanup_exc)[:1000]
            elif environment.worker_id is not None:
                cleanup_error = f"Sandbox Worker 配置不存在：worker_id={environment.worker_id}"
            conclusion = {
                "passed": False,
                "summary": "沙箱执行失败",
                "evidence": {"error": failure[:1000]},
                "agent_code": environment.agent_code,
            }
            environment.result_json = _json(conclusion)
            if cleanup_confirmed:
                environment.status = "stopped" if cancellation_requested else "failed"
                environment.error = failure
                environment.stopped_at = _utcnow()
                _append_event(
                    db,
                    environment,
                    "complete" if cancellation_requested else "failed",
                    "stop" if cancellation_requested else "executor",
                    "沙箱已关闭" if cancellation_requested else f"沙箱执行失败：{failure[:420]}",
                )
            else:
                environment.status = "stopping"
                environment.error = f"{failure}; Worker 回收待重试：{cleanup_error}"[:4000]
                conclusion["summary"] = "沙箱执行失败，Worker 资源回收待重试"
                conclusion["evidence"]["cleanup_error"] = cleanup_error
                environment.result_json = _json(conclusion)
                _append_event(db, environment, "failed", "cleanup", "Worker 未确认全部资源终止，保留 stopping 状态")
            db.commit()
            if cleanup_confirmed and not cancellation_requested:
                try:
                    strategy_learning_service.observe_sandbox_outcome(db, environment, conclusion)
                    db.commit()
                except Exception:  # noqa: BLE001 - 学习链路独立降级
                    db.rollback()
            _emit(
                environment,
                AgentEventType.FAILED if cleanup_confirmed else AgentEventType.PROGRESS,
                conclusion["summary"],
                {"stage": "executor" if cleanup_confirmed else "cleanup", "error": failure[:500]},
            )
    finally:
        usage_scope.close()
        db.close()


def _complete_finalizing_transition(
    db: Session,
    environment: SandboxEnvironment,
    *,
    final_status: str,
    result_json: str,
    execution_token: str | None = None,
) -> bool:
    """Atomically publish the terminal/ready state without reviving a stopped sandbox."""

    values: dict[str, Any] = {
        "status": final_status,
        "result_json": result_json,
    }
    stopped_at = environment.stopped_at
    if final_status in TERMINAL_STATES:
        stopped_at = stopped_at or _utcnow()
        values["stopped_at"] = stopped_at
    updated = (
        db.query(SandboxEnvironment)
        .filter(
            SandboxEnvironment.id == environment.id,
            SandboxEnvironment.status == "finalizing",
            *([SandboxEnvironment.execution_token == execution_token] if execution_token is not None else []),
        )
        .update(values, synchronize_session=False)
    )
    if not updated:
        db.rollback()
        db.expire_all()
        return False
    environment.status = final_status
    environment.result_json = result_json
    environment.stopped_at = stopped_at
    return True


def _can_manage(db: Session, actor: User, environment: SandboxEnvironment) -> bool:
    if environment.owner_id == actor.id:
        return True
    return rbac_service.is_super_admin_user(db, actor.id)


def _get_visible(db: Session, actor: User, public_id: str) -> SandboxEnvironment:
    row = db.query(SandboxEnvironment).filter(SandboxEnvironment.public_id == public_id).first()
    if not row:
        raise NotFoundError("沙箱不存在", code=40400)
    try:
        require_project_access(db, row.project_id, actor, need_write=False)
    except Exception as exc:
        raise NotFoundError("沙箱不存在", code=40400) from exc
    return row


def _can_access_report_artifact(db: Session, actor: User | None, environment: SandboxEnvironment) -> bool:
    if actor is None:
        return False
    in_report_scope = environment.owner_id == actor.id or rbac_service.is_admin_user(db, actor.id)
    return in_report_scope and rbac_service.check_permission(db, actor.id, PermissionCode.REPORT_VIEW)


def environment_to_dict(
    db: Session,
    row: SandboxEnvironment,
    actor: User | None = None,
) -> dict[str, Any]:
    can_preview = _can_preview_environment(db, actor, row)
    worker = db.get(SandboxWorker, row.worker_id) if row.worker_id else None
    events = db.query(SandboxEvent).filter(SandboxEvent.environment_id == row.id).order_by(SandboxEvent.id).all()
    artifacts = (
        db.query(SandboxArtifact).filter(SandboxArtifact.environment_id == row.id).order_by(SandboxArtifact.id).all()
    )
    env_config = _loads(row.agent_config_json, {})
    return {
        "public_id": row.public_id,
        "project_id": row.project_id,
        "owner_id": row.owner_id,
        "can_execute": can_preview and actor is not None and _can_manage(db, actor, row),
        "can_preview": can_preview,
        "can_stop": _can_stop_environment(db, actor, row),
        "worker_code": worker.code if worker else None,
        "agent_code": row.agent_code,
        "purpose": row.purpose,
        "language": row.language,
        "test_mode": row.test_mode,
        "status": row.status,
        "runtime": row.runtime,
        "source_sha256": row.source_sha256,
        "source_revision_id": env_config.get("source_revision_id"),
        "source_revision_no": env_config.get("source_revision_no"),
        "source_revision_sha256": env_config.get("source_revision_sha256"),
        "execution_source_sha256": row.execution_source_sha256,
        "execution_round": row.execution_round,
        "worker_request_id": _loads(row.worker_request_json, {}).get("request_id"),
        "syntax_repair_revisions": env_config.get("syntax_repair_revisions", []),
        "preview_path": row.preview_path,
        "remote_target_url": row.remote_target_url,
        "remote_target_approval_id": env_config.get("remote_target_approval_id"),
        "expires_at": row.expires_at,
        "started_at": row.started_at,
        "stopped_at": row.stopped_at,
        "result": _loads(row.result_json, {}),
        "error": row.error,
        "events": [
            {
                "id": item.id,
                "event_type": item.event_type,
                "stage": item.stage,
                "message": item.message,
                "payload": _loads(item.payload_json, {}),
                "create_time": item.create_time,
            }
            for item in events
        ],
        # 不向项目成员暴露报告制品的名称、摘要或下载入口；普通测试证据仍按
        # 项目可见范围返回，并由下载路由继续校验项目成员权限。
        "artifacts": [
            artifact_to_dict(item)
            for item in artifacts
            if item.artifact_type != "review_report" or _can_access_report_artifact(db, actor, row)
        ],
    }


def list_environments(db: Session, actor: User, limit: int = 50) -> list[dict[str, Any]]:
    query = db.query(SandboxEnvironment)
    if not rbac_service.is_super_admin_user(db, actor.id):
        project_ids, _scope = get_visible_project_ids(db, actor)
        if not project_ids:
            return []
        query = query.filter(SandboxEnvironment.project_id.in_(project_ids))
    rows = query.order_by(SandboxEnvironment.id.desc()).limit(max(1, min(limit, 100))).all()
    return [environment_to_dict(db, row, actor) for row in rows]


def get_environment(db: Session, actor: User, public_id: str) -> dict[str, Any]:
    return environment_to_dict(db, _get_visible(db, actor, public_id), actor)


def get_artifact_download(
    db: Session,
    actor: User,
    public_id: str,
    artifact_id: int,
) -> tuple[bytes, str, str]:
    environment = _get_visible(db, actor, public_id)
    row = (
        db.query(SandboxArtifact)
        .filter(
            SandboxArtifact.id == artifact_id,
            SandboxArtifact.environment_id == environment.id,
        )
        .first()
    )
    if not row:
        raise NotFoundError("沙箱制品不存在", code=40400)
    if row.artifact_type == "review_report":
        if environment.owner_id != actor.id and not rbac_service.is_admin_user(db, actor.id):
            raise NotFoundError("沙箱制品不存在", code=40400)
        if not rbac_service.check_permission(db, actor.id, PermissionCode.REPORT_VIEW):
            raise PermissionError(
                f"无操作权限: 需要 {PermissionCode.REPORT_VIEW}",
                detail={"required_permission": PermissionCode.REPORT_VIEW},
            )
    try:
        content = base64.b64decode(row.content_base64, validate=True)
    except (binascii.Error, ValueError, TypeError) as exc:
        raise RuntimeError("沙箱制品内容损坏") from exc
    if len(content) != row.byte_size or not hmac.compare_digest(hashlib.sha256(content).hexdigest(), row.sha256):
        raise RuntimeError("沙箱制品完整性校验失败")
    return content, row.file_name, row.mime_type


def stop_environment(db: Session, actor: User, public_id: str) -> dict[str, Any]:
    row = _get_visible(db, actor, public_id)
    if not _can_manage(db, actor, row):
        raise ForbiddenError("只有创建者或超级管理员可关闭沙箱", code=40300)
    if row.status in TERMINAL_STATES:
        return environment_to_dict(db, row, actor)
    worker = db.get(SandboxWorker, row.worker_id) if row.worker_id is not None else None
    row.status = "stopping"
    _append_event(db, row, "dispatch", "stop", f"{row.agent_code} 已调用关闭工具")
    db.commit()
    if row.worker_id is not None and worker is None:
        error = f"Sandbox Worker 配置不存在：worker_id={row.worker_id}"
        row.error = error
        _append_event(db, row, "failed", "stop", "关联 Worker 配置不存在，保留 stopping 状态等待恢复")
        db.commit()
        raise RuntimeError(error)
    if worker:
        try:
            _stop_registered_worker_requests(worker, row)
        except httpx.HTTPStatusError as exc:
            # 404 也不能视为成功：新协议必须返回已持久化的停止墓碑。
            row.error = f"关闭 worker 失败：{str(exc)[:1000]}"
            _append_event(db, row, "failed", "stop", "关闭 worker 未返回持久化终止回执，保留 stopping 状态等待重试")
            db.commit()
            raise
        except Exception as exc:
            row.error = f"关闭 worker 失败：{str(exc)[:1000]}"
            _append_event(db, row, "failed", "stop", "关闭 worker 失败，保留 stopping 状态等待重试")
            db.commit()
            raise
    row.status = "stopped"
    row.stopped_at = _utcnow()
    _append_event(db, row, "complete", "stop", "沙箱已关闭")
    audit_service.log(
        db,
        actor,
        "sandbox_stop",
        target_type="sandbox_environment",
        target_id=row.public_id,
        commit=False,
    )
    db.commit()
    return environment_to_dict(db, row, actor)


def extend_environment(db: Session, actor: User, public_id: str, hours: int) -> dict[str, Any]:
    row = _get_visible(db, actor, public_id)
    _require_sandbox_execution(db, actor, row.project_id)
    if not _can_manage(db, actor, row):
        raise ForbiddenError("只有创建者或超级管理员可续期", code=40300)
    if row.status not in ACTIVE_STATES:
        raise ValidationError("只能续期正在运行的沙箱", code=40901)
    created_at = _naive_utc(row.create_time) if row.create_time else _utcnow()
    maximum = created_at + timedelta(hours=settings.sandbox_max_ttl_hours)
    new_expiry = min(row.expires_at + timedelta(hours=hours), maximum)
    if new_expiry <= row.expires_at:
        raise ValidationError("沙箱已达到最大保留时间，不能继续续期", code=40901)
    worker = db.get(SandboxWorker, row.worker_id)
    if worker and row.executor_ref:
        request_id = _registered_worker_request_ids(row)[0]
        _call_worker(
            worker,
            "POST",
            "/extend",
            {
                "request_id": request_id,
                "extend_seconds": max(60, int((new_expiry - row.expires_at).total_seconds())),
            },
        )
    row.expires_at = new_expiry
    _append_event(db, row, "complete", "extend", f"已续期至 {new_expiry.isoformat()}Z")
    audit_service.log(
        db,
        actor,
        "sandbox_extend",
        target_type="sandbox_environment",
        target_id=row.public_id,
        detail=f"hours={hours}",
        commit=False,
    )
    db.commit()
    return environment_to_dict(db, row, actor)


def expire_due_environments() -> int:
    db = SessionLocal()
    count = 0
    try:
        rows = (
            db.query(SandboxEnvironment)
            .filter(
                SandboxEnvironment.status.in_(ACTIVE_STATES),
                SandboxEnvironment.expires_at <= _utcnow(),
            )
            .all()
        )
        for row in rows:
            worker = db.get(SandboxWorker, row.worker_id)
            if row.worker_id is not None and worker is None:
                row.status = "stopping"
                row.error = f"Sandbox Worker 配置不存在：worker_id={row.worker_id}"
                _append_event(db, row, "failed", "expiry", "关联 Worker 配置不存在，保留 stopping 状态等待恢复")
                continue
            try:
                if worker:
                    _stop_registered_worker_requests(worker, row)
            except Exception as exc:
                row.status = "stopping"
                row.error = f"到期回收 worker 失败：{str(exc)[:1000]}"
                _append_event(db, row, "failed", "expiry", "到期回收失败，将在下一周期重试")
                continue
            row.status = "expired"
            row.stopped_at = _utcnow()
            _append_event(db, row, "complete", "expiry", "沙箱到期已回收")
            count += 1
        db.commit()
        return count
    finally:
        db.close()


def visible_environment_ids(db: Session, actor: User, rows: Iterable[SandboxEnvironment]) -> list[int]:
    """为后续产物下载提供单一的项目级可见性判断入口。"""
    visible: list[int] = []
    for row in rows:
        try:
            require_project_access(db, row.project_id, actor, need_write=False)
        except Exception:
            continue
        visible.append(row.id)
    return visible
