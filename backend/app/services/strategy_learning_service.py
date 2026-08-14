"""小菱执行策略记忆：真实成功固化，失败策略抑制与跨会话复用。"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from typing import Any, Mapping, Optional, Sequence

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.agent_capability import SandboxEnvironment, SandboxWorker
from app.models.agent_governance import AgentMemory

MEMORY_TYPE = "execution_strategy"
SHARED_AGENT_CODE = "xiaoling_shared"
MAX_CONTEXT_ITEMS = 5
MAX_CONTEXT_CHARS = 5000

_SENSITIVE_KEYS = {
    "api_key", "apikey", "authorization", "cookie", "credential", "password", "private_key",
    "secret", "ssh_key", "token", "access_token", "refresh_token",
}
_DYNAMIC_KEYS = {
    "call_id", "environment_id", "id", "message_id", "project_id", "request_id", "run_id",
    "source_revision_id", "task_id", "timestamp", "trace_id", "user_id",
}
_PATH_KEYS = {"file", "file_path", "path", "source_path", "storage_ref"}
_STABLE_VALUE_KEYS = {
    "agent_code", "capability", "db_type", "language", "mode", "operation", "purpose",
    "runtime", "test_mode", "tool", "worker_code",
}
_ASYNC_TOOLS = {
    "deploy_project_sandbox", "run_full_project_validation", "run_project_tests", "start_review",
}
_LEARNABLE_SYNC_SUCCESS_TOOLS = {
    "admin_execute_capability", "admin_execute_operation", "control_roundtable_discussion",
    "create_project", "delete_project", "import_remote_project", "invoke_published_agent",
    "save_knowledge_note", "send_message", "update_project", "user_execute_capability",
}


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _clean_text(value: Any, *, limit: int = 500) -> str:
    text = str(value or "")
    text = re.sub(r"(?i)(bearer\s+)[^\s,;]+", r"\1[REDACTED]", text)
    text = re.sub(r"(?i)(api[_-]?key|token|password|secret)\s*[:=]\s*[^\s,;]+", r"\1=[REDACTED]", text)
    text = re.sub(r"(?<![\w])/(?:Users|home|tmp|var|opt)/[^\s,;]+", "[PATH]", text)
    return text[:limit]


def _normalize_arguments(value: Any, *, key: str = "") -> Any:
    lowered = key.lower()
    if lowered in _SENSITIVE_KEYS or any(token in lowered for token in ("password", "secret", "token", "key")):
        return "<redacted>"
    if lowered in _DYNAMIC_KEYS or lowered.endswith("_id"):
        return f"<{lowered or 'id'}>"
    if lowered in _PATH_KEYS or lowered.endswith("_path"):
        return "<path>"
    if isinstance(value, Mapping):
        return {
            str(item_key): _normalize_arguments(item_value, key=str(item_key))
            for item_key, item_value in sorted(value.items(), key=lambda item: str(item[0]))
        }
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_normalize_arguments(item, key=key) for item in list(value)[:20]]
    if isinstance(value, bool) or value is None or isinstance(value, (int, float)):
        return value
    if lowered in _STABLE_VALUE_KEYS:
        return _clean_text(value, limit=120)
    return f"<{type(value).__name__}>"


def strategy_fingerprint(
    *,
    agent_code: str,
    tool_name: str,
    arguments: Mapping[str, Any],
    failure_kind: str = "",
) -> str:
    """生成不含动态标识和秘密的稳定策略指纹。"""
    payload = {
        "agent_code": agent_code,
        "tool_name": tool_name,
        "arguments": _normalize_arguments(arguments),
        "failure_kind": failure_kind,
    }
    encoded = json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def classify_failure(error: str) -> str:
    value = str(error or "").lower()
    if "worker" in value and any(
        word in value for word in ("没有", "不可用", "不健康", "并发上限", "unavailable", "deleted")
    ):
        return "no_healthy_worker"
    if any(word in value for word in ("权限", "forbidden", "permission", "403")):
        return "permission_denied"
    if any(word in value for word in ("超时", "timeout", "timed out")):
        return "timeout"
    if any(word in value for word in ("参数", "校验", "invalid", "validation")):
        return "invalid_arguments"
    if any(word in value for word in ("上游", "upstream", "http 400", "http 500", "http 502", "http 503")):
        return "upstream_error"
    return "execution_failed"


def _guidance(outcome: str, failure_kind: str) -> str:
    if outcome == "success":
        return "已验证成功：优先复用相同工具与稳定参数形态，但仍需重新校验当前权限、源码版本和运行环境。"
    if failure_kind == "no_healthy_worker":
        return (
            "该 worker 选择已失败，不得原样重试；先检查可用运行时，再改用健康 worker_code；"
            "没有可用 worker 时明确报告基础设施阻塞。"
        )
    if failure_kind == "permission_denied":
        return "该权限路径已失败，不得原样重试或绕过权限；改为只读核查、请求授权或选择当前身份允许的能力。"
    if failure_kind == "invalid_arguments":
        return "该参数组合已失败，不得原样重试；重新读取能力契约并调整参数后再执行。"
    return "该方案已失败，必须改变方案：先读取最新状态和错误证据，再调整工具、参数或拆分步骤，禁止原参数原样重试。"


def _strategy_key(owner_user_id: int, fingerprint: str) -> str:
    return hashlib.sha256(f"user:{owner_user_id}:{SHARED_AGENT_CODE}:{fingerprint}".encode("utf-8")).hexdigest()


def record_tool_outcome(
    db: Session,
    *,
    owner_user_id: int,
    project_id: Optional[int],
    agent_code: str,
    tool_name: str,
    arguments: Mapping[str, Any],
    outcome: str,
    summary: str,
    evidence_ref: str,
    failure_kind: str = "",
    verified_async: bool = False,
) -> Optional[AgentMemory]:
    """按独立证据幂等累计工具策略；异步成功必须由真实终态消费者写入。"""
    if outcome not in {"success", "failure"}:
        return None
    if outcome == "success" and tool_name == "user_execute_capability":
        capability = str(arguments.get("capability") or "")
        if capability.endswith((".get", ".list", ".detail", ".status")) or capability.startswith("list_"):
            return None
    if outcome == "success" and not (
        verified_async or tool_name in _LEARNABLE_SYNC_SUCCESS_TOOLS
    ):
        return None
    resolved_failure_kind = failure_kind or (classify_failure(summary) if outcome == "failure" else "")
    fingerprint = strategy_fingerprint(
        agent_code=agent_code,
        tool_name=tool_name,
        arguments=arguments,
        failure_kind=resolved_failure_kind,
    )
    key = _strategy_key(int(owner_user_id), fingerprint)
    row = db.query(AgentMemory).filter(AgentMemory.strategy_key == key).first()
    if row is None:
        savepoint = db.begin_nested()
        row = AgentMemory(
            agent_code=SHARED_AGENT_CODE,
            memory_type=MEMORY_TYPE,
            title=(f"{tool_name} {'成功策略' if outcome == 'success' else '失败抑制'}")[:200],
            content="{}",
            weight=1.0,
            status="active",
            source_ref=_clean_text(evidence_ref, limit=160) or None,
            owner_user_id=int(owner_user_id),
            project_id=int(project_id) if project_id else None,
            share_scope="user",
            fingerprint=fingerprint,
            strategy_key=key,
            outcome=outcome,
            failure_kind=resolved_failure_kind or None,
            success_count=0,
            failure_count=0,
            confidence=0.0,
            evidence_json="{}",
        )
        db.add(row)
        try:
            db.flush()
            savepoint.commit()
        except IntegrityError:
            savepoint.rollback()
            row = db.query(AgentMemory).filter(AgentMemory.strategy_key == key).one()

    evidence = json.loads(row.evidence_json or "{}")
    refs = [str(item) for item in evidence.get("refs", []) if item]
    safe_ref = _clean_text(evidence_ref, limit=160)
    if safe_ref in refs:
        return row
    refs.append(safe_ref)
    # 顶层来源指向最近一次真实证据，历史来源仍保留在 evidence_json.refs。
    row.source_ref = safe_ref or row.source_ref
    if outcome == "success":
        row.success_count = int(row.success_count or 0) + 1
        row.confidence = min(0.95, 0.70 + 0.05 * (row.success_count - 1))
    else:
        row.failure_count = int(row.failure_count or 0) + 1
        row.confidence = min(0.95, 0.65 + 0.05 * (row.failure_count - 1))
    normalized_arguments = _normalize_arguments(arguments)
    payload = {
        "version": 1,
        "source_agent": agent_code,
        "tool_name": tool_name,
        "arguments": normalized_arguments,
        "outcome": outcome,
        "failure_kind": resolved_failure_kind,
        "summary": _clean_text(summary),
        "guidance": _guidance(outcome, resolved_failure_kind),
    }
    row.content = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    row.outcome = outcome
    row.failure_kind = resolved_failure_kind or None
    row.project_id = int(project_id) if project_id else row.project_id
    row.weight = row.confidence
    row.last_seen_at = _utcnow()
    row.evidence_json = json.dumps({"refs": refs[-50:]}, ensure_ascii=False)
    db.flush()
    return row


def observe_sandbox_outcome(
    db: Session,
    environment: SandboxEnvironment,
    conclusion: Optional[Mapping[str, Any]],
) -> Optional[AgentMemory]:
    """仅在沙箱形成布尔终态结论后学习，排队/运行中一律不奖励。"""
    if not isinstance(conclusion, Mapping) or not isinstance(conclusion.get("passed"), bool):
        return None
    config = json.loads(environment.agent_config_json or "{}")
    worker = db.get(SandboxWorker, environment.worker_id) if environment.worker_id else None
    arguments = {
        "project_id": environment.project_id,
        "purpose": environment.purpose,
        "language": environment.language,
        "test_mode": environment.test_mode,
        "runtime": environment.runtime,
        "worker_code": worker.code if worker else "",
        "source_revision_id": config.get("source_revision_id"),
        "db_type": config.get("db_type"),
    }
    passed = bool(conclusion["passed"])
    summary = str(conclusion.get("summary") or environment.error or "沙箱执行未通过")
    if environment.purpose == "deploy":
        tool_name = "deploy_project_sandbox"
    elif environment.test_mode == "combined":
        tool_name = "run_full_project_validation"
    else:
        tool_name = "run_project_tests"
    return record_tool_outcome(
        db,
        owner_user_id=int(environment.owner_id),
        project_id=int(environment.project_id),
        agent_code=str(environment.agent_code),
        tool_name=tool_name,
        arguments=arguments,
        outcome="success" if passed else "failure",
        failure_kind="" if passed else classify_failure(f"{summary} {environment.error or ''}"),
        summary=summary,
        evidence_ref=f"sandbox:{environment.public_id}",
        verified_async=True,
    )


def build_strategy_context(
    db: Session,
    *,
    owner_user_id: int,
    surface: str,
    messages: Sequence[Mapping[str, Any]],
) -> str:
    """生成账户隔离、长度有界的跨会话策略提示。"""
    del messages  # 当前按最近真实证据排序；保留参数以便后续做语义重排。
    rows = (
        db.query(AgentMemory)
        .filter(
            AgentMemory.memory_type == MEMORY_TYPE,
            AgentMemory.status == "active",
            AgentMemory.owner_user_id == int(owner_user_id),
            AgentMemory.share_scope == "user",
        )
        .order_by(AgentMemory.last_seen_at.desc(), AgentMemory.confidence.desc(), AgentMemory.id.desc())
        .limit(MAX_CONTEXT_ITEMS)
        .all()
    )
    if not rows:
        return ""
    lines = [
        "\n\n【小菱已验证策略记忆】",
        "以下内容来自当前账户的真实工具/沙箱终态，所有子 Agent 可复用；仍须遵守当前权限和实时状态。",
    ]
    for row in rows:
        try:
            payload = json.loads(row.content or "{}")
        except json.JSONDecodeError:
            continue
        tool_name = _clean_text(payload.get("tool_name"), limit=120)
        guidance = _clean_text(payload.get("guidance"), limit=600)
        if row.outcome == "success":
            lines.append(f"- 成功[{tool_name}]：{guidance}（真实成功 {int(row.success_count or 0)} 次）")
        else:
            failure_kind = _clean_text(row.failure_kind or "execution_failed", limit=80)
            lines.append(
                f"- 失败[{tool_name}/{failure_kind}]：{guidance}（真实失败 {int(row.failure_count or 0)} 次）"
            )
    lines.append("执行规则：命中失败策略时不得使用相同工具与相同稳定参数原样重试，必须说明并采用不同路径。")
    return "\n".join(lines)[:MAX_CONTEXT_CHARS]
