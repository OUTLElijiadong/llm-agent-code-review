"""代码测试与持续部署沙箱编排。

后端只处理权限、不可变源码快照、worker 选择和审计。它不挂载 Docker
Socket，也不接受用户命令、镜像、宿主路径、挂载或环境变量。
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import html
import ipaddress
import json
import re
import threading
import time
import urllib.parse
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

import httpx
import jwt
from sqlalchemy.orm import Session, object_session

from app.agents.event_bus import emit_event
from app.agents.events import AgentEventType
from app.core.config import settings
from app.core.database import SessionLocal
from app.core.exceptions import AuthError, ForbiddenError, NotFoundError, ValidationError
from app.models.agent_capability import (
    SandboxArtifact,
    SandboxEnvironment,
    SandboxEvent,
    SandboxWorker,
)
from app.models.project import Project
from app.models.project_source_archive import ProjectSourceArchive
from app.models.user import User
from app.services import audit_service, project_source_service, rbac_service
from app.services.project_member_service import get_visible_project_ids, require_project_access
from app.utils.api_resolver import decrypt_api_key_with_metadata, encrypt_api_key
from app.utils.public_http import pin_public_http_url

LANGUAGES = ("python", "node", "java", "go", "php")
MODES = ("whitebox", "blackbox", "combined", "deploy")
ACTIVE_STATES = ("queued", "dispatching", "running", "ready", "stopping")
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
    {
        re.sub(r"[^a-z0-9]+", "", alias): runtime
        for alias, runtime in _PROJECT_LANGUAGE_TO_RUNTIME.items()
    }.items(),
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
    failure = "" if passed else (
        f'<failure message="{escaped_summary}">{html.escape(log_text[-4_000:] or summary)}</failure>'
    )
    junit = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        f'<testsuite name="PrismSandbox" tests="1" failures="{0 if passed else 1}">'
        f'<testcase classname="{html.escape(environment.agent_code)}" name="{escaped_id}">{failure}'
        f'<system-out>{html.escape(log_text[-64_000:])}</system-out></testcase></testsuite>\n'
    ).encode("utf-8")
    sarif_result = [] if passed else [{
        "ruleId": "sandbox.execution.failed",
        "level": "error",
        "message": {"text": summary},
    }]
    sarif = json.dumps({
        "$schema": "https://json.schemastore.org/sarif-2.1.0.json",
        "version": "2.1.0",
        "runs": [{
            "tool": {"driver": {"name": "Prism Sandbox", "version": "1.0"}},
            "results": sarif_result,
            "properties": {
                "environment_id": environment.public_id,
                "source_sha256": environment.source_sha256,
                "runtime": environment.runtime,
            },
        }],
    }, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8")
    return [
        ("result", "sandbox-result.json", "application/json", result_json),
        ("log", "sandbox.log", "text/plain; charset=utf-8", log_text.encode("utf-8")),
        ("junit", "sandbox-junit.xml", "application/xml", junit),
        ("sarif", "sandbox-results.sarif", "application/sarif+json", sarif),
        ("html", "sandbox-report.html", "text/html; charset=utf-8", html_report),
    ]


def _persist_artifacts(
    db: Session,
    environment: SandboxEnvironment,
    conclusion: dict[str, Any],
) -> list[SandboxArtifact]:
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
            storage_ref="database:pending",
            content_base64=base64.b64encode(content).decode("ascii"),
        )
        db.add(row)
        rows.append(row)
    db.flush()
    for row in rows:
        row.storage_ref = f"database://sandbox-artifact/{row.id}"
    return rows


def _persist_browser_artifact(
    db: Session,
    environment: SandboxEnvironment,
    *,
    artifact_type: str,
    file_name: str,
    mime_type: str,
    content: bytes,
) -> SandboxArtifact:
    """追加浏览器证据；不得删除同一环境已有的白盒/黑盒制品。"""

    row = SandboxArtifact(
        environment_id=environment.id,
        artifact_type=artifact_type,
        file_name=file_name,
        mime_type=mime_type,
        byte_size=len(content),
        sha256=hashlib.sha256(content).hexdigest(),
        storage_ref="database:pending",
        content_base64=base64.b64encode(content).decode("ascii"),
    )
    db.add(row)
    db.flush()
    row.storage_ref = f"database://sandbox-artifact/{row.id}"
    return row


def artifact_to_dict(row: SandboxArtifact) -> dict[str, Any]:
    return {
        "id": row.id,
        "artifact_type": row.artifact_type,
        "file_name": row.file_name,
        "mime_type": row.mime_type,
        "byte_size": row.byte_size,
        "sha256": row.sha256,
    }


def _profile_policy(language: str) -> dict[str, Any]:
    policy = dict(_PROFILE_POLICIES.get(language, {}))
    policy.update(_RESOURCE_POLICY_COMMON)
    return policy


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
        if compact == alias or (compact.startswith(alias) and compact[len(alias):].isdigit()):
            return runtime
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
    response.raise_for_status()
    body = response.json()
    if not isinstance(body, dict):
        raise RuntimeError("Sandbox worker 响应不是对象")
    return body


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
        "cache-control", "content-disposition", "content-language", "content-type",
        "etag", "last-modified", "location",
    )
    for name in safe_response_headers:
        value = response.headers.get(name)
        if value and "\r" not in value and "\n" not in value:
            safe_headers[name] = value[:2048]
    return response.status_code, safe_headers, response.content


def create_preview_session(db: Session, actor: User, public_id: str) -> dict[str, Any]:
    environment = _get_visible(db, actor, public_id)
    if db.query(ProjectSourceArchive.id).filter(
        ProjectSourceArchive.project_id == environment.project_id,
        ProjectSourceArchive.storage_status == "active",
    ).first():
        raise ForbiddenError("项目已进入隔离源码审计，持续部署预览已禁用", code=40341)
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
    actor = db.get(User, user_id)
    if not actor or actor.status != 1 or int(actor.token_version or 0) != token_version:
        raise AuthError("预览会话已失效", code=40102)
    environment = _get_visible(db, actor, public_id)
    if db.query(ProjectSourceArchive.id).filter(
        ProjectSourceArchive.project_id == environment.project_id,
        ProjectSourceArchive.storage_status == "active",
    ).first():
        raise ForbiddenError("项目已进入隔离源码审计，持续部署预览已禁用", code=40341)
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
        row.last_error = None if healthy else (
            f"worker 合约或 runtime 不匹配: expected_runtime={row.runtime}; "
            f"actual_runtime={runtime or 'unknown'}; contract_ok={contract_ok}"
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
    rows = db.query(SandboxWorker).filter(
        SandboxWorker.enabled == 1,
        SandboxWorker.status == "healthy",
    ).all()
    return any(_browser_fingerprint_ready(row) for row in rows)


def _select_browser_worker(db: Session) -> SandboxWorker:
    rows = db.query(SandboxWorker).filter(
        SandboxWorker.enabled == 1,
        SandboxWorker.status == "healthy",
    ).order_by(SandboxWorker.priority, SandboxWorker.id).all()
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
    response = _call_worker(worker, "POST", "/browser-blackbox", {
        "request_id": request_id,
        "target_url": expected_url,
        "target_ip": str(pinned_ip),
    })
    result = response.get("result") if isinstance(response.get("result"), dict) else response
    if not isinstance(result, dict) or result.get("protocol_version") != "1.0":
        raise RuntimeError("Playwright worker 返回的证据协议无效")

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
        artifacts.append(_persist_browser_artifact(
            db,
            environment,
            artifact_type="browser_screenshot",
            file_name=f"browser-{request_id}.jpg",
            mime_type="image/jpeg",
            content=screenshot,
        ))
    evidence_bytes = json.dumps(evidence, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8")
    artifacts.append(_persist_browser_artifact(
        db,
        environment,
        artifact_type="browser_evidence",
        file_name=f"browser-{request_id}.json",
        mime_type="application/json",
        content=evidence_bytes,
    ))

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
        running = db.query(SandboxEnvironment).filter(
            SandboxEnvironment.worker_id == row.id,
            SandboxEnvironment.status.in_(ACTIVE_STATES),
        ).count()
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
    with httpx.Client(
        timeout=httpx.Timeout(float(settings.sandbox_remote_timeout), connect=10.0),
        follow_redirects=False,
        trust_env=False,
    ) as client, client.stream(
            "GET",
            target.request_url,
            headers={"Host": target.host_header, "User-Agent": "Prism-Blackbox-Agent/1.0"},
            extensions=target.request_extensions,
        ) as response:
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


def create_environment(db: Session, actor: User, payload: dict[str, Any]) -> SandboxEnvironment:
    """创建沙箱环境，并在所有失败路径释放项目行锁。"""
    try:
        return _create_environment_locked(db, actor, payload)
    except Exception:
        db.rollback()
        raise


def _create_environment_locked(
    db: Session,
    actor: User,
    payload: dict[str, Any],
) -> SandboxEnvironment:
    project_id = int(payload["project_id"])
    purpose = payload["purpose"]
    require_project_access(db, project_id, actor, need_write=purpose == "deploy")
    project: Project | None = None
    if purpose == "deploy":
        project = (
            db.query(Project)
            .filter(Project.id == project_id, Project.status != "deleted")
            .with_for_update()
            .first()
        )
        if project is None:
            raise NotFoundError("项目不存在", code=40400)
    source_archive = db.query(ProjectSourceArchive).filter(
        ProjectSourceArchive.project_id == project_id,
        ProjectSourceArchive.storage_status == "active",
    ).first()
    if source_archive is not None and purpose == "deploy":
        raise ForbiddenError(
            "隔离源码归档只能审计或测试，不得部署或开放预览",
            code=40341,
        )
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
        if not payload.get("remote_target_authorized"):
            raise ForbiddenError("必须显式确认已获得该远程目标本次测试授权", code=40340)
        pin_public_http_url(remote_url, require_https=True)

    remote_only = bool(remote_url and mode == "blackbox")
    worker_mode = "whitebox" if remote_url and mode == "combined" else mode
    worker = None if remote_only else _select_worker(
        db,
        language=language,
        mode=worker_mode,
        worker_code=payload.get("worker_code", ""),
    )
    archive, _ = project_source_service.build_source_archive(db, actor, project_id)
    source_sha256 = hashlib.sha256(archive).hexdigest()
    requested_ttl = int(payload.get("ttl_hours") or settings.sandbox_default_ttl_hours)
    ttl_hours = max(1, min(requested_ttl, settings.sandbox_max_ttl_hours))
    public_id = f"sbx_{uuid.uuid4().hex[:24]}"
    agent_code = "sandbox_deployer" if purpose == "deploy" else "test_verifier"
    environment = SandboxEnvironment(
        public_id=public_id,
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
        resource_policy_json=_json(
            _profile_policy(language)
            if worker
            else {
                "network": "authorized_remote_target_only",
                "timeout_seconds": settings.sandbox_remote_timeout,
                "response_sample_bytes": 65_536,
            }
        ),
        agent_config_json=_json({
            "worker_mode": worker_mode,
            "remote_only": remote_only,
            "ttl_hours": ttl_hours,
            "requested_language": requested_language,
            "resolved_language": language,
            "language_source": "project" if project_language else "request",
        }),
        remote_target_url=remote_url or None,
        remote_target_authorized_at=_utcnow() if remote_url else None,
        expires_at=_utcnow() + timedelta(hours=ttl_hours),
    )
    db.add(environment)
    db.flush()
    _append_event(db, environment, "dispatch", "authorization", f"{agent_code} 已校验项目权限和测试边界")
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
        args=(environment.id, base64.b64encode(archive).decode("ascii")),
        name=f"sandbox-{public_id}",
        daemon=True,
    )
    thread.start()
    return environment


def _execute_environment(environment_id: int, source_archive_base64: str) -> None:
    db = SessionLocal()
    try:
        environment = db.get(SandboxEnvironment, environment_id)
        if not environment or environment.status != "queued":
            return
        worker = db.get(SandboxWorker, environment.worker_id) if environment.worker_id else None
        config = _loads(environment.agent_config_json, {})
        if not worker and not config.get("remote_only"):
            raise RuntimeError("Sandbox worker 已被删除")
        environment.status = "dispatching"
        _append_event(db, environment, "progress", "executor", "独立执行器已接收固定测试配置")
        db.commit()
        _emit(environment, AgentEventType.PROGRESS, "独立执行器已接收任务", {"stage": "executor"})
        worker_mode = config.get("worker_mode", environment.test_mode)
        if worker:
            execute_response = _call_worker(worker, "POST", "/execute", {
                "request_id": environment.public_id,
                "purpose": environment.purpose,
                "language": environment.language,
                "test_mode": worker_mode if worker_mode in {"whitebox", "blackbox", "combined"} else "whitebox",
                "source_archive_base64": source_archive_base64,
                "source_sha256": environment.source_sha256,
                "ttl_seconds": max(60, int((environment.expires_at - _utcnow()).total_seconds())),
                "image_digest": environment.image_digest or "",
            })
            result = (
                execute_response.get("result")
                if isinstance(execute_response.get("result"), dict)
                else execute_response
            )
        else:
            result = {"request_id": environment.public_id, "status": "succeeded", "result": {"exit_code": 0}}
        last_sequence = 0
        configured_policy = _loads(environment.resource_policy_json, {})
        deadline = time.monotonic() + int(configured_policy.get("timeout_seconds") or 600) + 180
        def persist_worker_events(state: dict[str, Any]) -> None:
            nonlocal last_sequence
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

        persist_worker_events(result)
        db.commit()
        while str(result.get("status") or "") not in {
            "succeeded", "failed", "blocked", "stopped", "expired", "running",
        }:
            if time.monotonic() >= deadline:
                raise RuntimeError("Sandbox worker 状态轮询超时")
            time.sleep(1)
            status_response = _call_worker(worker, "POST", "/status", {
                "request_id": environment.public_id,
                "after_sequence": last_sequence,
            })
            result = (
                status_response.get("result")
                if isinstance(status_response.get("result"), dict)
                else status_response
            )
            persist_worker_events(result)
            db.commit()
        environment = db.get(SandboxEnvironment, environment_id)
        if environment.status in {"stopped", "expired"}:
            return
        state = str(result.get("status") or "failed")
        environment.status = {
            "completed": "succeeded",
            "succeeded": "succeeded",
            "running": "ready",
            "blocked": "blocked",
            "stopped": "stopped",
        }.get(state, "failed")
        environment.executor_ref = str(result.get("executor_ref") or result.get("request_id") or "")[:160] or None
        environment.runtime = str(result.get("runtime") or environment.runtime)[:50]
        environment.image_ref = str(result.get("image_ref") or environment.image_ref)[:300]
        environment.image_digest = str(result.get("image_digest") or "")[:100] or None
        if isinstance(result.get("resource_policy"), dict):
            environment.resource_policy_json = _json(result["resource_policy"])
        environment.started_at = environment.started_at or _utcnow()
        if environment.status in TERMINAL_STATES:
            environment.stopped_at = _utcnow()
        if environment.purpose == "deploy" and environment.status == "ready":
            environment.preview_path = f"/api/sandboxes/{environment.public_id}/preview/"
        worker_conclusion = result.get("result") if isinstance(result.get("result"), dict) else result
        evidence: dict[str, Any] = {"worker_result": worker_conclusion}
        if environment.remote_target_url:
            _append_event(db, environment, "progress", "remote_blackbox", "已在授权边界内调用远程 HTTP(S) 黑盒探测")
            db.commit()
            evidence["remote_blackbox"] = _probe_remote_target(environment.remote_target_url)
            if int(evidence["remote_blackbox"]["status_code"]) >= 500:
                environment.status = "failed"
                environment.stopped_at = _utcnow()
        passed = environment.status in {"succeeded", "ready"} and int(worker_conclusion.get("exit_code") or 0) == 0
        if environment.remote_target_url:
            passed = passed and int(evidence["remote_blackbox"]["status_code"]) < 500
        if environment.purpose == "deploy":
            summary = "部署就绪" if passed else "部署失败"
        else:
            summary = "测试通过" if passed else "测试未通过"
        conclusion = {
            "passed": passed,
            "summary": summary,
            "evidence": evidence,
            "agent_code": environment.agent_code,
        }
        environment.result_json = _json(conclusion)
        artifacts = _persist_artifacts(db, environment, conclusion)
        _append_event(
            db,
            environment,
            "complete" if passed else "failed",
            "conclusion",
            conclusion["summary"],
            {"passed": passed, "artifact_count": len(artifacts)},
        )
        db.commit()
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
            environment.status = "failed"
            environment.error = str(exc)[:4000]
            environment.stopped_at = _utcnow()
            _append_event(db, environment, "failed", "executor", f"沙箱执行失败：{str(exc)[:420]}")
            db.commit()
            _emit(environment, AgentEventType.FAILED, "沙箱执行失败", {"stage": "executor", "error": str(exc)[:500]})
    finally:
        db.close()


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


def environment_to_dict(db: Session, row: SandboxEnvironment) -> dict[str, Any]:
    worker = db.get(SandboxWorker, row.worker_id) if row.worker_id else None
    events = db.query(SandboxEvent).filter(SandboxEvent.environment_id == row.id).order_by(SandboxEvent.id).all()
    artifacts = (
        db.query(SandboxArtifact)
        .filter(SandboxArtifact.environment_id == row.id)
        .order_by(SandboxArtifact.id)
        .all()
    )
    return {
        "public_id": row.public_id,
        "project_id": row.project_id,
        "owner_id": row.owner_id,
        "worker_code": worker.code if worker else None,
        "agent_code": row.agent_code,
        "purpose": row.purpose,
        "language": row.language,
        "test_mode": row.test_mode,
        "status": row.status,
        "runtime": row.runtime,
        "source_sha256": row.source_sha256,
        "preview_path": row.preview_path,
        "remote_target_url": row.remote_target_url,
        "expires_at": row.expires_at,
        "started_at": row.started_at,
        "stopped_at": row.stopped_at,
        "result": _loads(row.result_json, {}),
        "error": row.error,
        "events": [{
            "id": item.id,
            "event_type": item.event_type,
            "stage": item.stage,
            "message": item.message,
            "payload": _loads(item.payload_json, {}),
            "create_time": item.create_time,
        } for item in events],
        "artifacts": [artifact_to_dict(item) for item in artifacts],
    }


def list_environments(db: Session, actor: User, limit: int = 50) -> list[dict[str, Any]]:
    query = db.query(SandboxEnvironment)
    if not rbac_service.is_super_admin_user(db, actor.id):
        project_ids, _scope = get_visible_project_ids(db, actor)
        if not project_ids:
            return []
        query = query.filter(SandboxEnvironment.project_id.in_(project_ids))
    rows = query.order_by(SandboxEnvironment.id.desc()).limit(max(1, min(limit, 100))).all()
    return [environment_to_dict(db, row) for row in rows]


def get_environment(db: Session, actor: User, public_id: str) -> dict[str, Any]:
    return environment_to_dict(db, _get_visible(db, actor, public_id))


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
        return environment_to_dict(db, row)
    worker = db.get(SandboxWorker, row.worker_id)
    row.status = "stopping"
    _append_event(db, row, "dispatch", "stop", f"{row.agent_code} 已调用关闭工具")
    db.commit()
    if worker:
        try:
            _call_worker(worker, "POST", "/stop", {"request_id": row.public_id})
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
    return environment_to_dict(db, row)


def extend_environment(db: Session, actor: User, public_id: str, hours: int) -> dict[str, Any]:
    row = _get_visible(db, actor, public_id)
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
        _call_worker(worker, "POST", "/extend", {
            "request_id": row.public_id,
            "extend_seconds": max(60, int((new_expiry - row.expires_at).total_seconds())),
        })
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
    return environment_to_dict(db, row)


def expire_due_environments() -> int:
    db = SessionLocal()
    count = 0
    try:
        rows = db.query(SandboxEnvironment).filter(
            SandboxEnvironment.status.in_(ACTIVE_STATES),
            SandboxEnvironment.expires_at <= _utcnow(),
        ).all()
        for row in rows:
            worker = db.get(SandboxWorker, row.worker_id)
            try:
                if worker:
                    _call_worker(worker, "POST", "/stop", {"request_id": row.public_id})
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
