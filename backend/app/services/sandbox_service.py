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
import json
import re
import stat
import threading
import time
import urllib.parse
import uuid
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

import httpx
import jwt
from sqlalchemy.orm import Session, object_session

from app.agents.event_bus import emit_event
from app.agents.events import AgentEventType
from app.agents.syntax_repair_agent import SyntaxRepairAgent, collect_php_lint_errors
from app.ai.language_detector import detect_language
from app.core.config import settings
from app.core.database import SessionLocal
from app.core.exceptions import (
    AuthError,
    ConflictError,
    ForbiddenError,
    NotFoundError,
    ServiceUnavailableError,
    ValidationError,
)
from app.core.observability import observe_event
from app.models.agent_capability import (
    SandboxArtifact,
    SandboxEnvironment,
    SandboxEvent,
    SandboxWorker,
)
from app.models.agent_governance import AgentAlert
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
from app.services.project_member_service import get_visible_project_ids, require_project_access
from app.utils.api_resolver import decrypt_api_key_with_metadata, encrypt_api_key
from app.utils.archive_extractor import read_archive_members
from app.utils.public_http import pin_public_http_url

LANGUAGES = ("python", "node", "java", "go", "php")
MODES = ("whitebox", "blackbox", "combined", "deploy")
ACTIVE_STATES = ("queued", "dispatching", "running", "finalizing", "ready", "stopping")
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
    environment.agent_config_json = _json(config)


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
            # 确定性引用，避免插入后再 UPDATE 触发 sandbox_artifact 竞态。
            storage_ref=f"database://sandbox-artifact/{environment.public_id}-{artifact_type}",
            content_base64=base64.b64encode(content).decode("ascii"),
        )
        db.add(row)
        rows.append(row)
    db.flush()
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
        storage_ref=f"database://sandbox-artifact/{environment.public_id}-{artifact_type}",
        content_base64=base64.b64encode(content).decode("ascii"),
    )
    db.add(row)
    db.flush()
    return row


def _run_auto_smoke_test(db: Session, environment: SandboxEnvironment) -> dict[str, Any]:
    """部署就绪后自动调用 test_verifier Agent 做带外 HTTP 冒烟测试。

    经 worker 预览通道从环境外部发起 GET,与人工预览访问同一路径,
    不触碰容器内源码,也不影响部署保活。任何失败只记录、不阻断部署。
    """
    started = datetime.now(timezone.utc)
    worker = db.get(SandboxWorker, environment.worker_id) if environment.worker_id else None
    if worker is None:
        return {"available": False, "reason": "worker 不可用"}
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
        "agent_code": "test_verifier",
        "method": "GET",
        "path": "/",
        "status_code": status_code,
        "latency_ms": elapsed_ms,
        "body_bytes": len(content),
        "body_preview": body.decode("utf-8", errors="replace")[:500],
        "passed": passed,
        "artifact_id": artifact.id,
    }


# deploy 后自动白盒/黑盒所用的内嵌 runner:作为 `_prism_verify.sh` 随源码注入,
# 用 deploy 镜像自带的解释器运行,不依赖项目镜像 runner.sh 的 test 分支(deploy 镜像通常不含)。
# 白盒做编译/静态检查与单测发现,黑盒在隔离网内起服务并对多个路径探活+首页断言。
_DEPLOY_VERIFY_RUNNER = r"""#!/bin/sh
set -u
# runner.sh 已把源码(含本脚本)拷到 /workspace 并 cd 进去,这里就地运行。
MODE="${1:-combined}"
LANG_="${PRISM_LANGUAGE:-python}"
PORT="${PRISM_PREVIEW_PORT:-8080}"
cd /workspace 2>/dev/null || true

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
      command -v go >/dev/null 2>&1 && { go vet ./... >/dev/null 2>&1 || true; }
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
  case "$LANG_" in
    python)
      if [ -f app.py ] && python -c 'import flask' >/dev/null 2>&1; then python -m flask --app app run --host 127.0.0.1 --port "$PORT" &
      elif [ -f main.py ]; then python main.py &
      elif [ -f app.py ]; then python app.py &
      else return 1; fi
      ;;
    node)   [ -f package.json ] || return 1; npm start --if-present & ;;
    java)   JAR=$(find . -type f -name '*.jar' -not -name '*-sources.jar' -print -quit); [ -n "$JAR" ] || return 1; java -Dserver.address=127.0.0.1 -Dserver.port="$PORT" -jar "$JAR" & ;;
    go)     go run . & ;;
    php)    ROOT=$(php_doc_root); php -S "127.0.0.1:$PORT" -t "$ROOT" & ;;
  esac
  echo $!
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

run_blackbox() {
  APP_PID=$(start_app) || { echo "blackbox: 无法启动应用"; return 1; }
  sleep 1
  i=0; READY=0
  while [ $i -lt 30 ]; do
    S=$(http_probe "/")
    case "$S" in 1*|2*|3*|4*|5*) READY=1; break;; esac  # 任何合法HTTP状态=服务已就绪(5xx多为应用缺DB等自身错误,属运行态证据)
    kill -0 "$APP_PID" 2>/dev/null || break
    i=$((i+1)); sleep 1
  done
  if [ "$READY" != "1" ]; then kill "$APP_PID" 2>/dev/null; echo "blackbox: 应用未在回环端口就绪"; return 1; fi
  # 首页内容断言:首页非空则判通过
  if command -v python >/dev/null 2>&1; then
    BYTES=$(python -c "import urllib.request,urllib.error
try:
  print(len(urllib.request.urlopen('http://127.0.0.1:$PORT/',timeout=3).read()))
except urllib.error.HTTPError as e:
  print(len(e.read()))" 2>/dev/null || echo 0)  # 5xx 响应体也计入(服务已起来)
  else
    cat > /tmp/_bytes.php <<'PHPB'
<?php
echo strlen((string)@file_get_contents("http://127.0.0.1:PORT/"));
PHPB
    sed -i "s/PORT/$PORT/" /tmp/_bytes.php
    BYTES=$(php /tmp/_bytes.php 2>/dev/null || echo 0)
  fi
  # 常见路径探活
  for p in / /index /health /api /login; do
    printf 'blackbox probe %s -> %s\n' "$p" "$(http_probe "$p")"
  done
  kill "$APP_PID" 2>/dev/null
  wait "$APP_PID" 2>/dev/null
  [ "${BYTES:-0}" -gt 0 ] || { echo "blackbox: 首页响应为空"; return 1; }
  echo "blackbox: 首页 $BYTES 字节, 探活完成"
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
    """部署就绪后自动执行 白盒→黑盒 测试链(test_verifier Agent)。

    复用同一 worker 与不可变源码快照,注入内嵌 `_prism_verify.sh` 作为 deploy 专用
    runner,每次起一次性测试容器跑完即回收,与常驻 deploy 预览互不影响。失败只记录。
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
            last_seq = 0
            deadline = time.monotonic() + 300
            while str(result.get("status") or "") not in {"succeeded", "failed", "blocked", "stopped", "expired"}:
                if time.monotonic() >= deadline:
                    raise RuntimeError("自动测试轮询超时")
                time.sleep(1)
                status_response = _call_worker(
                    worker, "POST", "/status", {"request_id": request_id, "after_sequence": last_seq}
                )
                result = (
                    status_response.get("result")
                    if isinstance(status_response.get("result"), dict)
                    else status_response
                )
                last_seq = int(result.get("last_sequence") or last_seq)
            conclusion = result.get("result") if isinstance(result.get("result"), dict) else result
            exit_code = int(conclusion.get("exit_code") or 0) if isinstance(conclusion, dict) else 0
            logs = conclusion.get("logs") if isinstance(conclusion, dict) else {}
            log_text = str((logs or {}).get("text") or "")
            passed = str(result.get("status")) == "succeeded" and exit_code == 0
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
                {"mode": mode, "passed": passed, "exit_code": exit_code},
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
    """从源码 zip 提取文件清单和有界关键源码片段。"""
    try:
        raw = base64.b64decode(source_archive_base64)
    except (binascii.Error, ValueError):
        return {"language": language, "files": [], "entries": []}
    file_names: list[str] = []
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as zf:
            for info in zf.infolist():
                name = info.filename
                if name.startswith("_prism") or name.startswith("__MACOSX") or name.endswith("/"):
                    continue
                if info.file_size > 400_000:
                    continue
                file_names.append(name)
    except (zipfile.BadZipFile, OSError):
        return {"language": language, "files": [], "entries": []}
    # 入口与关键文件优先展示,避免 agent 基于截断清单误判"文件缺失"
    priority = (
        "index.",
        "main.",
        "app.",
        "server.",
        "config.",
        "classes/",
        "src/",
        "lib/",
        "composer.json",
        "package.json",
        "requirements.txt",
        "pom.xml",
        "go.mod",
    )
    priority_hits = [n for n in file_names if any(n.endswith(p) or n.startswith(p) for p in priority)]
    rest = [n for n in file_names if n not in set(priority_hits)]
    entries = (priority_hits + rest)[:180]
    snippets: dict[str, str] = {}
    remaining_bytes = 48_000
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as zf:
            for name in entries[:12]:
                if remaining_bytes <= 0:
                    break
                content = zf.read(name)[: min(8_000, remaining_bytes)]
                if b"\x00" in content:
                    continue
                text = content.decode("utf-8", errors="replace").strip()
                if not text:
                    continue
                snippets[name] = text
                remaining_bytes -= len(content)
    except (KeyError, OSError, zipfile.BadZipFile):
        snippets = {}
    return {
        "language": language,
        "files": file_names[:300],
        "entries": entries,
        "snippets": snippets,
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

        def call_name(node: ast.Call) -> str:
            parts: list[str] = []
            current: ast.AST = node.func
            while isinstance(current, ast.Attribute):
                parts.append(current.attr)
                current = current.value
            if isinstance(current, ast.Name):
                parts.append(current.id)
            return ".".join(reversed(parts))

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
            for bindings in assignment_bindings.values():
                bindings.sort(key=lambda binding: (binding[0], binding[1]))

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

        def trusted_call(node: ast.Call, trusted_names: set[str]) -> bool:
            name = call_name(node)
            if name not in trusted_names:
                return False
            root = name.split(".", 1)[0]
            scope = enclosing_scope(node)
            position = (
                int(getattr(node, "lineno", 1 << 30) or (1 << 30)),
                int(getattr(node, "col_offset", 1 << 30) or (1 << 30)),
            )
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
                    and isinstance(node.value, ast.Attribute)
                    and node.value.attr == "environ"
                ):
                    return [("port", "")]
                segments = expression_segments(node.value, seen)
                return segments or [("unknown", "subscript")]
            if isinstance(node, ast.Attribute):
                return [("unknown", node.attr)]
            if isinstance(node, ast.Call):
                name = call_name(node)
                if name in {"os.getenv", "os.environ.get"} and node.args:
                    first = node.args[0]
                    if isinstance(first, ast.Constant) and first.value == "PRISM_PREVIEW_PORT":
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
                    else "__PORT__" if kind == "port" else "__ENCODED__" if kind == "encoded" else "__UNKNOWN__"
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

        agent = TestCaseGeneratorAgent()
        if not agent._api_key:
            _append_event(db, environment, "progress", "agent_tests", "LLM 未配置,跳过快照 agent 测试用例生成")
            db.commit()
            return None
        summary = _source_summary_for_agent_tests(source_archive_base64, language)
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
            extra={"trace_id": environment.public_id},
        )
        generation_deadline = time.monotonic() + int(
            getattr(settings, "sandbox_agent_test_generation_seconds", 300) or 300
        )
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
            files = result.get("files") if isinstance(result, dict) else None
            if not files:
                result_error = result.get("error") if isinstance(result, dict) else "生成结果不是对象"
                feedback = str(result_error or "生成结果为空")[:2000]
                summary["previous_generation_feedback"] = feedback
                _append_event(
                    db,
                    environment,
                    "progress",
                    "agent_tests",
                    f"第 {generation_round} 轮 agent 测试用例结构无效,已反馈重新生成: {feedback[:120]}",
                    {"generation_round": generation_round, "issues": [feedback]},
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
                    },
                )
                db.commit()
                return _store_agent_test_files(cache_key, files)
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
            "动态用例连续 3 轮未通过执行前契约校验,本轮回退到可确定性黑白盒测试",
            {"issues": [str(summary.get("previous_generation_feedback") or "生成失败")]},
        )
        db.commit()
        return None
    except Exception as exc:  # noqa: BLE001 - 生成失败不阻断原测试链
        _append_event(db, environment, "progress", "agent_tests", f"agent 测试用例生成异常: {str(exc)[:120]}")
        db.commit()
        return None


def _generate_deployment_patch(
    db: Session,
    environment: "SandboxEnvironment",
    source_archive_base64: str,
    language: str,
) -> dict[str, str] | None:
    """完整部署核验:LLM 判断入口/依赖是否完整,生成受控补全启动脚本。

    失败静默(只记事件不阻断):LLM 未配置/生成失败都回退 runner 内置部署逻辑。
    返回 {"launch_script": str, "notes": str} 或 None。
    """
    try:
        from app.agents.base import AgentContext
        from app.agents.deployment_coordinator_agent import DeploymentCoordinatorAgent

        agent = DeploymentCoordinatorAgent()
        if not agent._api_key:
            return None
        summary = _source_summary_for_agent_tests(source_archive_base64, language)
        ctx = AgentContext(
            user_id=environment.owner_id,
            project_id=environment.project_id,
            extra={"trace_id": environment.public_id},
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
        )
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
        )
        db.commit()
        return {"launch_script": launch_script, "notes": notes}
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
            raw_file_results
            if record_protocol_version == 2 and isinstance(raw_file_results, dict)
            else {}
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
    if status == "succeeded" and (
        not input_sha256
        or len(input_artifact_sha256s) != candidate_count
    ):
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
    raw_file_results = (
        reconciled.get("file_results") if isinstance(reconciled.get("file_results"), dict) else {}
    )
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
    agent_tests = conclusion.get("agent_tests") if isinstance(conclusion.get("agent_tests"), dict) else {}
    generated = int(agent_tests.get("generated") or 0)
    passed_count = int(agent_tests.get("passed_count") or agent_tests.get("passed") or 0)
    failed_count = int(agent_tests.get("failed") or 0)
    gate = "通过" if passed else "未通过"
    facts = [f"**系统事实门禁：{gate}。**", f"沙箱结论：{str(conclusion.get('summary') or gate)}。"]
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
            artifacts = "、".join(
                f"`{digest}`" for digest in decompilation["input_artifact_sha256s"]
            )
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
    report = (
        "## 总体结论\n\n"
        f"系统事实门禁：{'通过' if passed else '未通过'}。\n\n"
        "## 执行摘要\n\n"
        f"Worker 结论：{str(conclusion.get('summary') or ('测试通过' if passed else '测试未通过'))}。\n"
        f"动态用例：生成 {generated} 个，通过 {passed_count} 个，失败 {failed_count} 个。\n\n"
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

        agent = TestReviewReporterAgent()
        data: dict[str, Any] = {}
        roles: dict[str, Any] = {}
        report_md = _build_deterministic_test_report(conclusion)
        if not agent._api_key:
            _append_event(db, environment, "progress", "multi_agent_review", "LLM 未配置，已使用确定性测试报告兜底")
        else:
            ctx = AgentContext(
                user_id=environment.owner_id,
                project_id=environment.project_id,
                extra={"trace_id": environment.public_id},
            )
            result = agent.review(db, environment=environment, conclusion=conclusion, ctx=ctx)
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
        # 从审查报告解析问题清单条数(## 问题清单 段内以 - 开头行)
        issue_count = 0
        issue_section = re.search(r"## 问题清单(.*?)(?=\n## |$)", report_md, re.S)
        if issue_section:
            issue_count = len(re.findall(r"^[-*] ", issue_section.group(1), re.M))
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
                project_id=project_id,
                task_name=task_name,
                review_type="sandbox_test",
                status="success" if passed else "failed",
                total_files=1,
                processed_files=1,
                total_issues=issue_count,
                score=report_score,
                summary=report_md,
                start_time=started_at,
                end_time=stopped_at,
                duration_ms=duration_ms,
            )
            db.add(task)
        else:
            task.project_id = project_id
            task.status = "success" if passed else "failed"
            task.total_issues = issue_count
            task.score = report_score
            task.summary = report_md
            task.start_time = started_at
            task.end_time = stopped_at
            task.duration_ms = duration_ms
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
                "evidence": conclusion.get("evidence", {}),
            }
            report_row.summary = report_md[:2000]
            report_row.score = report_score
        db.commit()
        return {"report_task_id": task.id, "issues": issue_count}
    except Exception:  # noqa: BLE001 - 入库失败不阻断测试结论
        db.rollback()
        return {}


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
            if status not in TERMINAL_STATES:
                errors.append(f"{request_id}={status or 'unknown'}")
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
    actor = db.get(User, user_id)
    if not actor or actor.status != 1 or int(actor.token_version or 0) != token_version:
        raise AuthError("预览会话已失效", code=40102)
    environment = _get_visible(db, actor, public_id)
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
    agent_team_context = _normalize_agent_team_context(payload.get("agent_team"))
    require_project_access(db, project_id, actor, need_write=purpose == "deploy")
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
        if not payload.get("remote_target_authorized"):
            raise ForbiddenError("必须显式确认已获得该远程目标本次测试授权", code=40340)
        pin_public_http_url(remote_url, require_https=True)

    remote_only = bool(remote_url and mode == "blackbox")
    worker_mode = "whitebox" if remote_url and mode == "combined" else mode
    # 先做参数与源码/副本校验(worker 繁忙时不掩盖真实参数错误),最后再选 worker
    db_type = str(payload.get("db_type") or "none").strip().lower()
    if db_type not in {"none", "sqlite", "mysql"}:
        raise ValidationError("沙箱数据库类型不受支持", code=40001)
    source_revision_id = payload.get("source_revision_id")
    if source_revision_id:
        archive = project_source_revision_service.get_revision_archive(
            db,
            actor,
            int(source_revision_id),
            project_id,
        )
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
        remote_target_authorized_at=_utcnow() if remote_url else None,
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
        agent = SyntaxRepairAgent()
        if not agent._api_key:
            _append_event(db, environment, "progress", "syntax_repair", "LLM 未配置,跳过后端语法修复")
            db.commit()
            return None
        ctx = AgentContext(
            user_id=environment.owner_id,
            project_id=environment.project_id,
            extra={"trace_id": environment.public_id},
        )
        result = agent.repair(
            language=environment.language,
            errors=errors_payload,
            files=files,
            ctx=ctx,
        )
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
        # 修复后的源码作为项目副本持久化,下次审计可选用
        try:
            saved = project_source_revision_service.save_revision(
                db,
                project_id=environment.project_id,
                owner_id=environment.owner_id,
                repaired_source_base64=new_source,
                repaired_files=sorted(repaired),
                parent_sha256=environment.source_sha256,
                repair_notes=f"沙箱语法修复第{len(lint_errors)}处错误,修复{len(repaired)}文件",
            )
            saved_note = f", 已保存为项目源码副本 rev#{saved.revision_no}" if saved else ""
        except Exception as exc:  # noqa: BLE001 - 副本保存失败不影响修复流程
            saved_note = f", 副本保存失败: {str(exc)[:80]}"
            db.rollback()
        _append_event(
            db,
            environment,
            "progress",
            "syntax_repair",
            f"后端语法修复 Agent 已修复 {len(repaired)} 个文件({', '.join(sorted(repaired)[:8])}){saved_note}",
            {"repaired_files": sorted(repaired), "round_errors": len(lint_errors)},
        )
        db.commit()
        return {"source": new_source, "files": sorted(repaired)}
    except Exception as exc:  # noqa: BLE001 - 修复失败不阻断原测试链
        _append_event(db, environment, "progress", "syntax_repair", f"语法修复异常: {str(exc)[:120]}")
        db.commit()
        return None


def heartbeat_and_recover_sandboxes(db: Session) -> dict[str, int]:
    """长任务心跳与卡死回收：由后台调度器周期调用。

    心跳只追加 sandbox_event，不刷新 environment.update_time，因此真实执行线程
    一旦死亡，update_time 不再前进，watchdog 才能准确判定卡死并回收。
    """
    active = (
        db.query(SandboxEnvironment)
        .filter(SandboxEnvironment.status.in_(ACTIVE_STATES))
        .all()
    )
    heartbeat_count = 0
    recovered_count = 0
    now = _utcnow()
    for environment in active:
        last_event = (
            db.query(SandboxEvent.create_time)
            .filter(SandboxEvent.environment_id == environment.id)
            .order_by(SandboxEvent.id.desc())
            .first()
        )
        last_at = last_event[0] if last_event else environment.create_time
        if last_at is not None:
            elapsed = max(0, int((now - last_at).total_seconds()))
        else:
            elapsed = 0
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
        if started is not None and (now - started).total_seconds() >= int(
            settings.sandbox_stuck_after_seconds
        ):
            try:
                worker = db.get(SandboxWorker, environment.worker_id) if environment.worker_id else None
                if worker is not None:
                    _stop_registered_worker_requests(worker, environment)
            except Exception:  # noqa: BLE001 - 回收失败不阻断其余环境
                pass
            environment.status = "failed"
            environment.error = "沙箱心跳超时，已自动判定卡死并回收"
            environment.stopped_at = now
            stuck_reason = "沙箱心跳超时，已自动判定卡死并回收"
            project = db.get(Project, environment.project_id)
            project_label = (
                f"{project.project_name}#{environment.project_id}"
                if project is not None
                else f"project#{environment.project_id}"
            )
            db.add(
                AgentAlert(
                    alert_type="sandbox_stuck",
                    category="sandbox_stuck",
                    source="sandbox_watchdog",
                    severity="high",
                    title=(
                        f"沙箱 {environment.public_id} 卡死已回收（项目 {project_label}）：{stuck_reason}"
                    )[:200],
                    detail_json=_json(
                        {
                            "public_id": environment.public_id,
                            "project_id": environment.project_id,
                            "project_name": project.project_name if project is not None else None,
                            "reason": stuck_reason,
                            "elapsed_seconds": int((now - started).total_seconds()),
                        }
                    ),
                    user_id=environment.owner_id,
                    fingerprint=environment.public_id,
                )
            )
            _append_event(
                db,
                environment,
                "failed",
                "watchdog",
                "沙箱心跳超时，已自动判定卡死并回收",
            )
            recovered_count += 1
            observe_event("sandbox_stuck_recovered", labels={"status": environment.status})
    db.commit()
    return {"heartbeat": heartbeat_count, "recovered": recovered_count}


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
                db.commit()
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
        expected_agent_tests: set[str] = set()
        if worker and environment.purpose == "test":
            # 1) 完整部署核验:LLM 判断入口/依赖,生成补全启动脚本 _prism_launch.sh
            deploy_patch = _generate_deployment_patch(
                db,
                environment,
                source_archive_base64,
                environment.language,
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
            )
            if agent_test_files:
                expected_agent_tests = {
                    str(item.get("path") or "").strip()
                    for item in agent_test_files
                    if str(item.get("path") or "").strip()
                }
                effective_source = _inject_agent_test_files(effective_source, agent_test_files)
            if effective_source != source_archive_base64:
                effective_sha = hashlib.sha256(base64.b64decode(effective_source)).hexdigest()
                environment.source_sha256 = effective_sha
                db.commit()
        if worker:
            repair_round = 0
            max_repair_rounds = int(getattr(settings, "sandbox_max_repair_rounds", 2) or 2)
            while True:
                # 尽量在提交前观察本地停止状态；最终竞态仍由 Worker
                # 的持久化 stop tombstone 协议封闭。
                db.refresh(environment)
                if environment.status in {"stopping", "stopped", "expired"}:
                    return
                worker_request_id = (
                    environment.public_id if repair_round == 0 else f"{environment.public_id}-r{repair_round}"
                )
                last_sequence = 0

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

                if environment.started_at is None:
                    environment.started_at = _utcnow()
                    db.commit()
                sandbox_db_type = str(
                    (_loads(getattr(environment, "agent_config_json", None) or "{}", {}) or {}).get("db_type") or "none"
                )
                _register_worker_request(environment, worker_request_id)
                db.commit()
                db.refresh(environment)
                if environment.status in {"stopping", "stopped", "expired"}:
                    try:
                        _stop_registered_worker_requests(worker, environment)
                        if environment.status == "stopping":
                            environment.status = "stopped"
                            environment.stopped_at = _utcnow()
                    except Exception as exc:  # noqa: BLE001 - keep cleanup visible and retryable
                        environment.status = "stopping"
                        environment.error = f"停止 worker 失败：{str(exc)[:1000]}"
                    db.commit()
                    return
                execute_response = _call_worker(
                    worker,
                    "POST",
                    "/execute",
                    {
                        "request_id": worker_request_id,
                        "purpose": environment.purpose,
                        "language": environment.language,
                        "test_mode": worker_mode if worker_mode in {"whitebox", "blackbox", "combined"} else "whitebox",
                        "source_archive_base64": effective_source,
                        "source_sha256": effective_sha,
                        "ttl_seconds": max(60, int((environment.expires_at - _utcnow()).total_seconds())),
                        "image_digest": environment.image_digest or "",
                        "db_type": sandbox_db_type,
                    },
                )
                result = (
                    execute_response.get("result")
                    if isinstance(execute_response.get("result"), dict)
                    else execute_response
                )
                last_sequence = 0
                configured_policy = _loads(environment.resource_policy_json, {})
                deadline = time.monotonic() + int(configured_policy.get("timeout_seconds") or 600) + 180
                persist_worker_events(result)
                db.commit()
                while str(result.get("status") or "") not in {
                    "succeeded",
                    "failed",
                    "blocked",
                    "stopped",
                    "expired",
                    "running",
                }:
                    if time.monotonic() >= deadline:
                        raise RuntimeError("Sandbox worker 状态轮询超时")
                    time.sleep(1)
                    status_response = _call_worker(
                        worker,
                        "POST",
                        "/status",
                        {
                            "request_id": worker_request_id,
                            "after_sequence": last_sequence,
                        },
                    )
                    result = (
                        status_response.get("result")
                        if isinstance(status_response.get("result"), dict)
                        else status_response
                    )
                    persist_worker_events(result)
                    db.commit()
                environment = db.get(SandboxEnvironment, environment_id)
                if environment.status in {"stopping", "stopped", "expired"}:
                    if environment.status == "stopping":
                        try:
                            _stop_registered_worker_requests(worker, environment)
                            environment.status = "stopped"
                            environment.stopped_at = _utcnow()
                        except Exception as exc:  # noqa: BLE001 - keep cleanup visible and retryable
                            environment.error = f"停止 worker 失败：{str(exc)[:1000]}"
                        db.commit()
                    return
                # ── 后端语法修复:白盒 php -l 报错时,LLM 修复文件后重跑(最多 max_repair_rounds 轮) ──
                if environment.purpose == "test" and environment.language == "php" and repair_round < max_repair_rounds:
                    worker_concl = result.get("result") if isinstance(result.get("result"), dict) else result
                    wlogs = worker_concl.get("logs") if isinstance(worker_concl, dict) else None
                    log_text = str((wlogs or {}).get("text") or "")
                    lint_errors = collect_php_lint_errors(log_text)
                    if lint_errors:
                        repaired = _syntax_repair_round(db, environment, effective_source, lint_errors)
                        if repaired:
                            effective_source = repaired["source"]
                            effective_sha = hashlib.sha256(base64.b64decode(effective_source)).hexdigest()
                            environment.source_sha256 = effective_sha
                            repair_round += 1
                            db.commit()
                            continue
                break
        else:
            result = {"request_id": environment.public_id, "status": "succeeded", "result": {"exit_code": 0}}
        state = str(result.get("status") or "failed")
        target_status = {
            "completed": "succeeded",
            "succeeded": "succeeded",
            "running": "ready",
            "blocked": "blocked",
            "stopped": "stopped",
        }.get(state, "failed")
        # Worker 的确定性结果已返回，但制品和多 Agent 报告尚未完成。
        # 对外保持可轮询的非终态，防止团队在长报告事务期间提前交接资源。
        environment.status = "finalizing"
        environment.executor_ref = str(result.get("executor_ref") or result.get("request_id") or "")[:160] or None
        environment.runtime = str(result.get("runtime") or environment.runtime)[:50]
        environment.image_ref = str(result.get("image_ref") or environment.image_ref)[:300]
        environment.image_digest = str(result.get("image_digest") or "")[:100] or None
        if isinstance(result.get("resource_policy"), dict):
            environment.resource_policy_json = _json(result["resource_policy"])
        if environment.started_at is None:
            environment.started_at = _utcnow()
        if environment.purpose == "deploy" and target_status == "ready":
            environment.preview_path = f"/api/sandboxes/{environment.public_id}/preview/"
        auto_smoke: dict[str, Any] | None = None
        auto_test_chain: list[dict[str, Any]] = []
        if (
            environment.purpose == "deploy"
            and target_status == "ready"
            and environment.agent_code == "sandbox_deployer"
        ):
            # 预览冒烟 = 黑盒(从环境外部对运行中的服务发真实 HTTP,单槽下无法另起黑盒容器)。
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
            db.commit()
        if environment.purpose == "deploy":
            if pre_whitebox:
                auto_test_chain.append(pre_whitebox)
            if auto_smoke is not None and auto_smoke.get("available"):
                auto_test_chain.append(
                    {
                        "mode": "blackbox",
                        "passed": bool(auto_smoke.get("passed")),
                        "status_code": auto_smoke.get("status_code"),
                        "latency_ms": auto_smoke.get("latency_ms"),
                        "via": "preview_smoke",
                    }
                )
        worker_conclusion = result.get("result") if isinstance(result.get("result"), dict) else result
        evidence: dict[str, Any] = {"worker_result": worker_conclusion}
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
            db.commit()
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
                    "Android 制品反编译完成" if decompilation_result.get("status") == "succeeded" else "Android 制品反编译失败",
                    decompilation_result,
                )
                db.commit()
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
            db.commit()
        if environment.remote_target_url:
            _append_event(db, environment, "progress", "remote_blackbox", "已在授权边界内调用远程 HTTP(S) 黑盒探测")
            db.commit()
            evidence["remote_blackbox"] = _probe_remote_target(environment.remote_target_url)
            if int(evidence["remote_blackbox"]["status_code"]) >= 500:
                target_status = "failed"
        passed = target_status in {"succeeded", "ready"} and int(worker_conclusion.get("exit_code") or 0) == 0
        if agent_tests_result is not None:
            passed = passed and _agent_tests_succeeded(agent_tests_result)
        if environment.remote_target_url:
            passed = passed and int(evidence["remote_blackbox"]["status_code"]) < 500
        final_status = "failed" if environment.purpose == "test" and not passed else target_status
        if environment.purpose == "deploy":
            summary = "部署就绪" if passed else "部署失败"
            if auto_test_chain:
                wb = next((r for r in auto_test_chain if r.get("mode") == "whitebox"), None)
                bb = next((r for r in auto_test_chain if r.get("mode") == "blackbox"), None)
                summary += (
                    f"；自动测试链 白盒{'✓' if wb and wb.get('passed') else '✗'}"
                    f"/黑盒{'✓' if bb and bb.get('passed') else '✗'}"
                )
            if auto_smoke and auto_smoke.get("available"):
                summary += f"；预览冒烟{'✓' if auto_smoke.get('passed') else '✗'}"
        else:
            summary = "测试通过" if passed else "测试未通过"
        conclusion = {
            "passed": passed,
            "summary": summary,
            "evidence": evidence,
            "agent_code": environment.agent_code,
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
        artifacts = _persist_artifacts(db, environment, conclusion)
        # 先发布确定性结果并释放 SandboxEnvironment 行锁；此时仍为
        # finalizing，调度器不会把尚未完成多 Agent 报告的结果误判为终态。
        db.commit()
        # 黑白盒链路结束后,由多Agent审查编排产出中文报告(失败只记录,不阻断)
        review_report = _run_test_review_report(db, environment, conclusion)
        if review_report is not None:
            conclusion["multi_agent_review"] = review_report
        final_result_json = _json(conclusion)
        if not _complete_finalizing_transition(
            db,
            environment,
            final_status=final_status,
            result_json=final_result_json,
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
        db.commit()
        try:
            strategy_learning_service.observe_sandbox_outcome(db, environment, conclusion)
            db.commit()
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
                db.commit()
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
        db.close()


def _complete_finalizing_transition(
    db: Session,
    environment: SandboxEnvironment,
    *,
    final_status: str,
    result_json: str,
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


def environment_to_dict(db: Session, row: SandboxEnvironment) -> dict[str, Any]:
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
        "worker_code": worker.code if worker else None,
        "agent_code": row.agent_code,
        "purpose": row.purpose,
        "language": row.language,
        "test_mode": row.test_mode,
        "status": row.status,
        "runtime": row.runtime,
        "source_sha256": row.source_sha256,
        "source_revision_id": env_config.get("source_revision_id"),
        "preview_path": row.preview_path,
        "remote_target_url": row.remote_target_url,
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
    return environment_to_dict(db, row)


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
