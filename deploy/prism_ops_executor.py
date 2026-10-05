"""Root-side allowlisted operations executor exposed only through a Unix socket."""

from __future__ import annotations

import hashlib
import hmac
import importlib.util
import ipaddress
import json
import os
import re
import socket
import socketserver
import stat
import struct
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from typing import Any

DEPLOY_DIR = Path(__file__).resolve().parent
BACKUP_DIR = (DEPLOY_DIR.parent / "backups").resolve()
SOCKET_PATH = Path(os.environ.get("OPS_EXECUTOR_SOCKET", "/run/prism-ops/agent.sock"))
NGINX_LOG_LINE_LIMIT = 30_000
AUDIT_LOG = Path(os.environ.get("OPS_EXECUTOR_AUDIT_LOG", "/var/log/prism-ops/executions.jsonl"))
LEDGER_DIR = Path(os.environ.get("OPS_EXECUTOR_LEDGER_DIR", "/var/lib/prism-ops/execution-ledger"))
LEDGER_LOCK = threading.RLock()
MUTATION_LOCK = threading.Lock()
SERVICES = {"backend", "frontend", "mysql", "redis", "clamav"}
ALLOWED_PEER_UID = 10001
ALLOWED_PEER_GID = 991
SOCKET_DIRECTORY_UID = 0
SOCKET_DIRECTORY_GID = 991
SOCKET_DIRECTORY_MODE = 0o710
READABLE_TEXT_ROOTS = (Path("/var/log"),)
LISTABLE_DIRECTORY_ROOTS = (Path("/var/log"),)
JOURNAL_UNIT_ALLOWLIST = frozenset({
    "sshd.service", "docker.service", "containerd.service", "systemd-journald.service",
    "prism-ops-executor.service", "prism-sandbox-executor.service", "prism-security-block.service",
    "prism-ops-check.service", "prism-backup.service", "prism-verify-backup.service",
    "prism-cert-renew.service",
})
READ_ONLY_ACTIONS = {
    "status", "certificate_status", "host_inventory", "list_directory", "read_text_file",
    "journal_query", "ssh_login_events", "flytrap_attack_events", "nginx_attack_events",
    "backup_audit", "db_threat_signals", "db_health", "ip_attribution",
    "security_block_status", "security_block_candidates",
    "security_ip_trace", "security_surface_audit", "security_traffic_summary",
    "security_decoy_status",
}
MAX_TEXT_BYTES = 256 * 1024
MAX_DIRECTORY_ENTRIES = 500
# 与 prism_security_block 同模块处理的只读溯源动作。
SECURITY_MODULE_ACTIONS = frozenset({"security_ip_trace", "security_surface_audit", "security_traffic_summary",
                                        "security_decoy_status", "security_decoy_apply"})
UNIT_NAME = re.compile(r"^[A-Za-z0-9_.@:-]{1,128}$")
REQUEST_ID = re.compile(r"^[A-Za-z0-9_.:-]{8,128}$")
DENIED_PATH_ROOTS = tuple(Path(value) for value in ("/proc", "/sys", "/dev", "/run"))
SENSITIVE_EXACT_PATHS = {Path("/etc/shadow"), Path("/etc/gshadow")}
SENSITIVE_NAMES = {".env", "authorized_keys", "credentials", "credentials.json"}
SENSITIVE_SUFFIXES = {".pem", ".key", ".p12", ".pfx", ".jks"}
CONFIG_RULES = {
    "LOG_LEVEL": re.compile(r"^(DEBUG|INFO|WARNING|ERROR)$"),
    "REVIEW_MAX_CONCURRENCY": re.compile(r"^[1-8]$"),
    "BACKEND_MEM_LIMIT": re.compile(r"^[0-9]{2,5}[mMgG]$"),
    "FRONTEND_MEM_LIMIT": re.compile(r"^[0-9]{2,5}[mMgG]$"),
    "MYSQL_MEM_LIMIT": re.compile(r"^[0-9]{2,5}[mMgG]$"),
    "REDIS_MEM_LIMIT": re.compile(r"^[0-9]{2,5}[mMgG]$"),
    "CLAMAV_MEM_LIMIT": re.compile(r"^[0-9]{2,5}[mMgG]$"),
    "OPS_DISK_MAX_PERCENT": re.compile(r"^(?:[1-9]|[1-9][0-9]|100)$"),
    "OPS_MEMORY_MAX_PERCENT": re.compile(r"^(?:[1-9]|[1-9][0-9]|100)$"),
}
ACTION_PARAM_KEYS = {
    "status": set(), "certificate_status": set(), "backup_database": set(), "verify_backup": {"file"},
    "restart_service": {"service"}, "nginx_reload": set(), "renew_certificate": set(),
    "database_maintenance": set(), "update_config": {"key", "value"},
    "rollback_application": {"target"}, "restore_database": {"file"}, "cleanup": set(),
    "host_inventory": set(), "list_directory": {"path", "limit"},
    "read_text_file": {"path", "max_bytes"}, "journal_query": {"unit", "since", "lines"},
    "ssh_login_events": {"since_hours", "limit", "focus"},
    "flytrap_attack_events": {"since_hours", "limit"},
    "nginx_attack_events": {"since_hours", "limit", "failure_threshold"},
    "backup_audit": set(),
    "db_threat_signals": {"since_hours", "limit"},
    "db_health": set(),
    "ip_attribution": {"ip"},
    "security_block_status": set(),
    "security_block_configure": {
        "enabled", "ai_anomaly_enabled", "duration_seconds", "window_seconds",
        "ssh_threshold", "web_threshold", "allowlist_cidrs", "protected_ip",
        # 与 backend/app/services/ops_service.ACTION_PARAM_KEYS 必须逐字一致；
        # 两处白名单由不同进程各自加载，漏改一处就会出现"后端通过、执行器拒绝"。
        "auto_escalate",
    },
    "security_block_reconcile": set(),
    "security_block_release": {"ip", "reason"},
    "security_block_candidates": set(),
    "security_block_apply_anomalies": {"decisions"},
    "security_ip_trace": {"ip"},
    "security_surface_audit": set(),
    "security_traffic_summary": {"since_hours"},
    "security_decoy_status": set(),
    "security_decoy_apply": {"reason"},
}


def run(
    args: list[str],
    *,
    timeout: int = 900,
    allow_failure: bool = False,
    stdout_tail_limit: int | None = 100_000,
) -> dict[str, Any]:
    completed = subprocess.run(
        args,
        cwd=DEPLOY_DIR,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
        env={**os.environ, "DEPLOY_ENV_FILE": ".env"},
    )
    stdout = _redact_text(
        completed.stdout if stdout_tail_limit is None else completed.stdout[-stdout_tail_limit:]
    )
    stderr = _redact_text(completed.stderr[-20_000:])
    if completed.returncode != 0 and not allow_failure:
        raise RuntimeError(f"命令失败 exit={completed.returncode}: {stderr or stdout}"[:4000])
    return {"exit_code": completed.returncode, "stdout": stdout, "stderr": stderr}


def execute(action: str, params: dict[str, Any], request_id: str = "") -> dict[str, Any]:
    allowed_params = ACTION_PARAM_KEYS.get(action)
    if allowed_params is None:
        raise ValueError("动作不在白名单")
    extra_params = set(params) - allowed_params
    if extra_params:
        raise ValueError(f"动作 {action} 包含未允许参数: {sorted(extra_params)}")
    if action.startswith("security_block_") or action in SECURITY_MODULE_ACTIONS:
        # 同目录的 root 专用确定性防御模块；不允许请求决定导入文件路径。
        spec = importlib.util.spec_from_file_location("prism_security_block", DEPLOY_DIR / "prism_security_block.py")
        if spec is None or spec.loader is None:
            raise RuntimeError("安全防御模块不可用")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module.execute(action, params)
    if action == "status":
        result = run([str(DEPLOY_DIR / "ops-check.sh")], allow_failure=True)
        try:
            checks = json.loads(result["stdout"])
        except json.JSONDecodeError as exc:
            raise RuntimeError("ops-check 未返回有效 JSON") from exc
        health_status = str(checks.get("status") or "error")
        return {
            "checks": checks,
            "command_exit": result["exit_code"],
            "health_status": health_status,
            "can_continue": bool(checks.get("can_continue")),
            "summary": str(checks.get("summary") or ""),
        }
    if action == "certificate_status":
        domain = _read_env("APP_DOMAIN")
        cert = _configured_certbot_conf_dir() / "live" / domain / "fullchain.pem"
        details = run(["openssl", "x509", "-enddate", "-subject", "-noout", "-in", str(cert)], timeout=30)
        validity = run(
            ["openssl", "x509", "-checkend", str(30 * 24 * 60 * 60), "-noout", "-in", str(cert)],
            timeout=30,
            allow_failure=True,
        )
        return {
            "certificate": details,
            "valid_for_30_days": validity["exit_code"] == 0,
            "check_exit_code": validity["exit_code"],
        }
    if action == "host_inventory":
        return _host_inventory()
    if action == "list_directory":
        return _list_directory(params)
    if action == "read_text_file":
        return _read_text_file(params)
    if action == "journal_query":
        return _journal_query(params)
    if action == "backup_database":
        return run([str(DEPLOY_DIR / "backup.sh"), "--reason", "ai_ops"])
    if action == "verify_backup":
        args = [str(DEPLOY_DIR / "verify-backup.sh")]
        if params.get("file"):
            args.append(str(_backup_file(str(params["file"]))))
        return run(args)
    if action == "restart_service":
        service = str(params.get("service") or "")
        if service not in SERVICES:
            raise ValueError("service 不在白名单")
        result = run(["docker", "compose", "--env-file", ".env", "restart", service], timeout=180)
        _wait_service(service)
        return result
    if action == "nginx_reload":
        check = run(["docker", "exec", "cr_frontend", "nginx", "-t"], timeout=30)
        reload_result = run(["docker", "exec", "cr_frontend", "nginx", "-s", "reload"], timeout=30)
        return {"check": check, "reload": reload_result}
    if action == "renew_certificate":
        return run([str(DEPLOY_DIR / "renew-cert.sh")], timeout=600)
    if action == "database_maintenance":
        return run([
            "docker", "exec", "cr_mysql", "sh", "-ec",
            (
                'MYSQL_PWD="$MYSQL_ROOT_PASSWORD" exec mysqlcheck '
                '--protocol=TCP -h 127.0.0.1 -uroot --analyze "$MYSQL_DATABASE"'
            ),
        ], timeout=900)
    if action == "update_config":
        key = str(params.get("key") or "")
        value = str(params.get("value") or "")
        rule = CONFIG_RULES.get(key)
        if rule is None or not rule.fullmatch(value):
            raise ValueError("配置键或值不在白名单")
        _update_env(key, value)
        return {"updated": key, "restart_required": True}
    if action == "rollback_application":
        target = str(params.get("target") or "all")
        if target not in {"all", "backend", "frontend"}:
            raise ValueError("回滚目标不合法")
        return run([str(DEPLOY_DIR / "rollback.sh"), target, "--confirm", "ROLLBACK_APPLICATION"], timeout=900)
    if action == "restore_database":
        backup = _backup_file(str(params.get("file") or ""))
        return run([str(DEPLOY_DIR / "restore.sh"), str(backup), "--confirm", "RESTORE_PRODUCTION"], timeout=1800)
    if action == "cleanup":
        return run([str(DEPLOY_DIR / "cleanup.sh"), "--apply"], timeout=900)
    if action == "ssh_login_events":
        return _ssh_login_events(params)
    if action == "flytrap_attack_events":
        return _flytrap_attack_events(params)
    if action == "nginx_attack_events":
        return _nginx_attack_events(params)
    if action == "backup_audit":
        return _backup_audit()
    if action == "db_threat_signals":
        return _db_threat_signals(params)
    if action == "db_health":
        return _db_health()
    if action == "ip_attribution":
        return _ip_attribution(params)
    raise ValueError("动作不在白名单")


def _host_inventory() -> dict[str, Any]:
    commands = {
        "host": ["hostnamectl"],
        "uptime": ["uptime"],
        "memory": ["free", "-h"],
        "filesystems": ["df", "-hT"],
        "failed_units": ["systemctl", "--failed", "--no-pager", "--plain"],
        "running_services": [
            "systemctl", "list-units", "--type=service", "--state=running", "--no-pager", "--plain",
        ],
        "timers": ["systemctl", "list-timers", "--all", "--no-pager", "--plain"],
        "containers": [
            "docker", "ps", "--no-trunc", "--format",
            "{{json .}}",
        ],
        "listeners": ["ss", "-lntup"],
    }
    return {name: run(args, timeout=60, allow_failure=True) for name, args in commands.items()}


def _absolute_path(raw: Any) -> Path:
    value = str(raw or "")
    if not value or "\x00" in value:
        raise ValueError("path 不能为空")
    path = Path(value)
    if not path.is_absolute():
        raise ValueError("path 必须是绝对路径")
    if ".." in path.parts:
        raise ValueError("path 不能包含上级目录")
    resolved = Path(os.path.normpath(value))
    for root in DENIED_PATH_ROOTS:
        if resolved == root or root in resolved.parents:
            raise ValueError("path 位于禁止访问的虚拟文件系统")
    _reject_symlink_components(resolved)
    return resolved


def _under_allowed_roots(path: Path, roots: tuple[Path, ...]) -> bool:
    return any(path == root or root in path.parents for root in roots)


def _reject_symlink_components(path: Path) -> None:
    current = Path(path.anchor)
    for component in path.parts[1:]:
        current /= component
        if current.is_symlink():
            raise ValueError("path 不能经过符号链接")


def _is_sensitive_path(path: Path) -> bool:
    lowered = path.name.lower()
    if path in SENSITIVE_EXACT_PATHS or lowered in SENSITIVE_NAMES:
        return True
    if path.suffix.lower() in SENSITIVE_SUFFIXES:
        return True
    if any(part.lower() == ".ssh" for part in path.parts):
        return True
    if lowered.startswith("id_") and not lowered.endswith(".pub"):
        return True
    return lowered.startswith("ssh_host_") and not lowered.endswith(".pub")


def _safe_text_path(raw: Any, *, must_exist: bool) -> Path:
    path = _absolute_path(raw)
    if not _under_allowed_roots(path, READABLE_TEXT_ROOTS):
        raise ValueError("文本文件路径超出允许范围")
    if _is_sensitive_path(path):
        raise ValueError("拒绝访问凭据或私钥路径")
    if must_exist and not path.exists():
        raise ValueError("文件不存在")
    if path.exists():
        mode = path.lstat().st_mode
        if stat.S_ISLNK(mode) or not stat.S_ISREG(mode):
            raise ValueError("只允许普通文本文件，不能使用符号链接")
    return path


def _list_directory(params: dict[str, Any]) -> dict[str, Any]:
    path = _absolute_path(params.get("path"))
    allowed_roots = (*LISTABLE_DIRECTORY_ROOTS, _configured_backup_dir())
    if not _under_allowed_roots(path, allowed_roots):
        raise ValueError("目录路径超出允许范围")
    if not path.is_dir() or path.is_symlink():
        raise ValueError("目标不是可读取的普通目录")
    requested_limit = int(params.get("limit") or 200)
    if requested_limit < 1 or requested_limit > MAX_DIRECTORY_ENTRIES:
        raise ValueError(f"limit 必须在 1 到 {MAX_DIRECTORY_ENTRIES} 之间")
    entries: list[dict[str, Any]] = []
    all_entries = sorted(path.iterdir(), key=lambda item: item.name)
    for item in all_entries[:requested_limit]:
        info = item.lstat()
        entries.append({
            "name": item.name,
            "type": "symlink" if item.is_symlink() else "directory" if item.is_dir() else "file",
            "size": info.st_size,
            "mode": format(stat.S_IMODE(info.st_mode), "04o"),
            "modified_at": datetime.fromtimestamp(info.st_mtime, tz=timezone.utc).isoformat(),
            "sensitive": _is_sensitive_path(item),
        })
    return {"path": str(path), "entries": entries, "truncated": len(all_entries) > len(entries)}


def _read_text_file(params: dict[str, Any]) -> dict[str, Any]:
    path = _safe_text_path(params.get("path"), must_exist=True)
    requested_limit = int(params.get("max_bytes") or 64 * 1024)
    if requested_limit < 1 or requested_limit > MAX_TEXT_BYTES:
        raise ValueError(f"max_bytes 必须在 1 到 {MAX_TEXT_BYTES} 之间")
    size = path.stat().st_size
    raw = path.read_bytes()[:requested_limit]
    try:
        content = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("文件不是 UTF-8 文本") from exc
    return {
        "path": str(path),
        "size": size,
        "sha256": _sha256_file(path),
        "mode": format(stat.S_IMODE(path.stat().st_mode), "04o"),
        "truncated": size > len(raw),
        "content": _redact_text(content),
    }


def _journal_query(params: dict[str, Any]) -> dict[str, Any]:
    unit = str(params.get("unit") or "")
    if not UNIT_NAME.fullmatch(unit):
        raise ValueError("unit 名称不合法")
    if unit not in JOURNAL_UNIT_ALLOWLIST:
        raise ValueError("unit 不在只读查询白名单")
    since = str(params.get("since") or "1 hour ago")
    if not re.fullmatch(r"[A-Za-z0-9 :+_.-]{1,64}", since):
        raise ValueError("since 格式不合法")
    lines = int(params.get("lines") or 100)
    if lines < 1 or lines > 500:
        raise ValueError("lines 必须在 1 到 500 之间")
    return run([
        "journalctl", "-u", unit, "--since", since, "--no-pager", "--output=short-iso", "-n", str(lines),
    ], timeout=60, allow_failure=True)


MAX_SINCE_HOURS = 720
MAX_EVENT_LIMIT = 5000
SSH_FOCUS_VALUES = {"all", "accepted", "failed"}
FLYTRAP_HEALTH_WINDOW_MINUTES = 10
FLYTRAP_AGENT_ERROR_MARKERS = (
    "Heartbeat 调用失败",
    "Reporter 持久队列发送失败",
    "ConfigSync FetchConfig 失败",
    "Agent token 续签失败",
)
FLYTRAP_SYNC_ERROR_MARKERS = ("同步周期异常",)
FLYTRAP_SYNC_SUCCESS_MARKERS = (
    "登录成功",
    "增量事件:",
    "节点同步:",
    "告警同步:",
    "IOC 同步:",
    "攻击者同步:",
    "小时聚合重算:",
)


def _since_hours_arg(params: dict[str, Any], default: int = 24) -> int:
    try:
        since_hours = int(params.get("since_hours")) if params.get("since_hours") is not None else default
    except (TypeError, ValueError) as exc:
        raise ValueError("since_hours 必须是整数") from exc
    if since_hours < 1 or since_hours > MAX_SINCE_HOURS:
        raise ValueError(f"since_hours 必须在 1 到 {MAX_SINCE_HOURS} 之间")
    return since_hours


def _event_limit_arg(params: dict[str, Any], default: int = 1000) -> int:
    try:
        limit = int(params.get("limit")) if params.get("limit") is not None else default
    except (TypeError, ValueError) as exc:
        raise ValueError("limit 必须是整数") from exc
    if limit < 1 or limit > MAX_EVENT_LIMIT:
        raise ValueError(f"limit 必须在 1 到 {MAX_EVENT_LIMIT} 之间")
    return limit


def _http_failure_threshold_arg(params: dict[str, Any], default: int = 20) -> int:
    try:
        threshold = int(params.get("failure_threshold")) if params.get("failure_threshold") is not None else default
    except (TypeError, ValueError) as exc:
        raise ValueError("failure_threshold 必须是整数") from exc
    if threshold < 1 or threshold > MAX_EVENT_LIMIT:
        raise ValueError(f"failure_threshold 必须在 1 到 {MAX_EVENT_LIMIT} 之间")
    return threshold


def parse_ssh_log(lines: list[str]) -> dict[str, Any]:
    accepted_pattern = re.compile(
        r"Accepted (publickey|password|keyboard-interactive) for (\S+) from "
        r"([0-9a-fA-F:.]+) port \d+ ssh2(?::?\s*(.*))?$"
    )
    failed_pattern = re.compile(
        r"Failed password for (?:invalid user )?(\S+) from ([0-9a-fA-F:.]+) port \d+"
    )
    invalid_pattern = re.compile(r"Invalid user (\S+) from ([0-9a-fA-F:.]+) port \d+")
    accepted: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []
    for raw in lines:
        line = str(raw).rstrip("\n")
        match = accepted_pattern.search(line)
        if match:
            accepted.append(
                {
                    "method": match.group(1),
                    "user": match.group(2),
                    "ip": match.group(3),
                    "detail": match.group(4) or "",
                }
            )
            continue
        match = failed_pattern.search(line)
        if match:
            failed.append({"user": match.group(1), "ip": match.group(2), "detail": "failed_password"})
            continue
        match = invalid_pattern.search(line)
        if match:
            failed.append({"user": match.group(1), "ip": match.group(2), "detail": "invalid_user"})
    return {"accepted": accepted, "failed": failed}


def parse_flytrap_log(lines: list[str]) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for raw in lines:
        line = str(raw).strip()
        if not line.startswith("{"):
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict):
            continue
        remote = str(payload.get("remote") or "")
        if remote.startswith("[") and "]" in remote:
            ip = remote[1:remote.index("]")]
        elif remote.count(":") == 1:
            ip = remote.rsplit(":", 1)[0]
        else:
            ip = remote
        try:
            ipaddress.ip_address(ip)
        except ValueError:
            # Agent 的心跳、上报重试和配置同步日志同样是 JSON；没有
            # 可验证远端 IP 的运维日志不能伪装成攻击事件。
            continue
        events.append({
            "time": payload.get("time") or "",
            "username": payload.get("username") or "",
            "ip": ip,
            "message": str(payload.get("message") or "")[:500],
        })
    return events


def _latest_flytrap_sync_signal(lines: list[str]) -> str:
    """按日志顺序返回最近一次同步结果（ok/error/unknown）。"""
    signal = "unknown"
    for raw in lines:
        line = str(raw)
        if any(marker in line for marker in FLYTRAP_SYNC_ERROR_MARKERS):
            signal = "error"
        elif any(marker in line for marker in FLYTRAP_SYNC_SUCCESS_MARKERS):
            signal = "ok"
    return signal


def summarize_flytrap_health(
    *,
    agent_active: bool,
    sync_active: bool,
    agent_lines: list[str],
    sync_lines: list[str],
    source_errors: list[str] | None = None,
) -> dict[str, Any]:
    """汇总 FlyTrap 本地采集和上游同步健康，不把上游故障当作阻断项。"""
    issues: list[dict[str, str]] = []
    agent_upstream_error = any(
        marker in line
        for line in agent_lines
        for marker in FLYTRAP_AGENT_ERROR_MARKERS
    )
    sync_signal = _latest_flytrap_sync_signal(sync_lines)

    if not agent_active:
        issues.append({"code": "flytrap_agent_inactive", "message": "FlyTrap 本地蜜罐进程未运行"})
    elif agent_upstream_error:
        issues.append({
            "code": "flytrap_agent_upstream_error",
            "message": "FlyTrap 心跳或事件上报在最近窗口内失败，本地持久队列继续保留事件",
        })
    if not sync_active:
        issues.append({"code": "flytrap_sync_inactive", "message": "FlyTrap 数据同步进程未运行"})
    elif sync_signal == "error":
        issues.append({"code": "flytrap_sync_error", "message": "FlyTrap 最近一次上游同步失败"})
    elif sync_signal == "unknown":
        issues.append({
            "code": "flytrap_sync_stale",
            "message": f"最近 {FLYTRAP_HEALTH_WINDOW_MINUTES} 分钟未发现 FlyTrap 同步成功记录",
        })
    for source in source_errors or []:
        issues.append({
            "code": "flytrap_health_source_error",
            "message": f"无法读取 FlyTrap {source} 健康信号",
        })

    degraded = bool(issues)
    return {
        "ok": not degraded,
        "status": "degraded" if degraded else "ok",
        "can_continue": True,
        "requires_human": degraded,
        "window_minutes": FLYTRAP_HEALTH_WINDOW_MINUTES,
        "agent": {"active": agent_active, "upstream_error": agent_upstream_error},
        "sync": {"active": sync_active, "latest_signal": sync_signal},
        "issues": issues,
    }


def parse_nginx_log(lines: list[str]) -> list[dict[str, Any]]:
    access_pattern = re.compile(r'^([^\s]+) - - \[[^\]]+\] "([^"]*)" (\d{3})')
    events: list[dict[str, Any]] = []
    for raw in lines:
        match = access_pattern.match(str(raw).strip())
        if not match:
            continue
        ip, request, status = match.groups()
        method = request.split(" ", 1)[0] if request else ""
        if method == "CONNECT":
            detail = "proxy_connect"
        elif method.startswith("\\x") or not method.isalpha():
            detail = "tls_gibberish"
        elif status in {"400", "403", "444"}:
            detail = f"http_{status}"
        else:
            continue
        events.append({"ip": ip, "method": method, "path": request[:200], "status": status, "detail": detail})
    return events


def _aggregate(events: list[dict[str, Any]], key: str) -> list[dict[str, Any]]:
    counts: dict[str, int] = {}
    for event in events:
        value = str(event.get(key) or "unknown")
        counts[value] = counts.get(value, 0) + 1
    return [
        {"value": value, "count": count}
        for value, count in sorted(counts.items(), key=lambda item: item[1], reverse=True)
    ][:30]


def _ssh_login_events(params: dict[str, Any]) -> dict[str, Any]:
    since_hours = _since_hours_arg(params)
    limit = _event_limit_arg(params, default=1000)
    focus = str(params.get("focus") or "all")
    if focus not in SSH_FOCUS_VALUES:
        raise ValueError("focus 必须是 all/accepted/failed")
    result = run(
        [
            "journalctl",
            "-u",
            "sshd",
            "--since",
            f"{since_hours} hours ago",
            "--no-pager",
            "--output=short-iso",
            "-n",
            "20000",
        ],
        timeout=90,
        allow_failure=True,
    )
    parsed = parse_ssh_log(result["stdout"].splitlines())
    accepted, failed = parsed["accepted"], parsed["failed"]
    selected = ([*accepted] if focus in {"all", "accepted"} else []) + ([*failed] if focus in {"all", "failed"} else [])
    return {
        "ok": result["exit_code"] == 0,
        "since_hours": since_hours,
        "focus": focus,
        "accepted_total": len(accepted),
        "failed_total": len(failed),
        "accepted_by_ip": _aggregate(accepted, "ip"),
        "accepted_by_detail": _aggregate(accepted, "detail"),
        "failed_by_ip": _aggregate(failed, "ip"),
        "recent": selected[-min(limit, 200):],
        "source_exit_code": result["exit_code"],
        "source_error": result["stderr"][:400] if result["exit_code"] else "",
        "stdout_capped": len(result["stdout"]) >= 100_000,
    }


def _flytrap_attack_events(params: dict[str, Any]) -> dict[str, Any]:
    since_hours, limit = _since_hours_arg(params), _event_limit_arg(params, default=1000)
    if os.environ.get("PRISM_FLYTRAP_ENABLED", "false").strip().lower() not in {"1", "true", "yes", "on"}:
        return {
            "ok": True,
            "enabled": False,
            "status": "retired",
            "degraded": False,
            "can_continue": True,
            "reason": "FlyTrap 集成已退役",
            "human_actions": [],
            "since_hours": since_hours,
            "total": 0,
            "by_ip": [],
            "by_username": [],
            "recent": [],
        }
    result = run(
        [
            "journalctl",
            "-u",
            "flytrap-agent",
            "--since",
            f"{since_hours} hours ago",
            "--no-pager",
            "--output=cat",
            "-n",
            "30000",
        ],
        timeout=90,
        allow_failure=True,
    )
    events = parse_flytrap_log(result["stdout"].splitlines())
    agent_status = run(["systemctl", "is-active", "flytrap-agent.service"], timeout=20, allow_failure=True)
    sync_status = run(["systemctl", "is-active", "flytrap-sync.service"], timeout=20, allow_failure=True)
    recent_since = f"{FLYTRAP_HEALTH_WINDOW_MINUTES} minutes ago"
    agent_health_log = run([
        "journalctl", "-u", "flytrap-agent.service", "--since", recent_since,
        "--no-pager", "--output=cat", "-n", "5000",
    ], timeout=60, allow_failure=True)
    sync_health_log = run([
        "journalctl", "-u", "flytrap-sync.service", "--since", recent_since,
        "--no-pager", "--output=cat", "-n", "2000",
    ], timeout=60, allow_failure=True)
    health_source_errors = []
    if agent_health_log["exit_code"] != 0:
        health_source_errors.append("agent 日志")
    if sync_health_log["exit_code"] != 0:
        health_source_errors.append("sync 日志")
    health = summarize_flytrap_health(
        agent_active=agent_status["exit_code"] == 0 and agent_status["stdout"].strip() == "active",
        sync_active=sync_status["exit_code"] == 0 and sync_status["stdout"].strip() == "active",
        agent_lines=agent_health_log["stdout"].splitlines(),
        sync_lines=sync_health_log["stdout"].splitlines(),
        source_errors=health_source_errors,
    )
    degraded = health["status"] == "degraded"
    human_actions = []
    if degraded:
        human_actions.append({
            "code": "flytrap_upstream_recovery",
            "label": "检查 FlyTrap 上游连通性",
            "message": "核对上游服务、防火墙和网络路由；本地蜜罐与持久队列可继续工作，恢复后确认同步成功日志。",
            "requires_human": True,
        })
    return {
        "ok": result["exit_code"] == 0,
        "degraded": degraded,
        "can_continue": result["exit_code"] == 0,
        "reason": "；".join(item["message"] for item in health["issues"]),
        "health": health,
        "human_actions": human_actions,
        "since_hours": since_hours,
        "total": len(events),
        "by_ip": _aggregate(events, "ip"),
        "by_username": _aggregate(events, "username"),
        "recent": events[-min(limit, 300):],
        "source_exit_code": result["exit_code"],
        "source_error": result["stderr"][:400] if result["exit_code"] else "",
        "stdout_capped": len(result["stdout"]) >= 100_000,
    }


def _nginx_attack_events(params: dict[str, Any]) -> dict[str, Any]:
    since_hours, limit = _since_hours_arg(params), _event_limit_arg(params, default=1000)
    failure_threshold = _http_failure_threshold_arg(params)
    result = run(
        ["docker", "logs", "--since", f"{since_hours}h", "-n", str(NGINX_LOG_LINE_LIMIT), "cr_frontend"],
        timeout=90,
        allow_failure=True,
        stdout_tail_limit=None,
    )
    log_lines = result["stdout"].splitlines()
    events = parse_nginx_log(log_lines)
    failures_by_ip: dict[str, dict[str, Any]] = {}
    for event in events:
        status = str(event.get("status") or "")
        if event.get("detail") not in {"http_400", "http_403", "http_444"}:
            continue
        ip = str(event.get("ip") or "")
        if not ip:
            continue
        item = failures_by_ip.setdefault(ip, {"ip": ip, "failure_count": 0, "status_counts": {}})
        item["failure_count"] += 1
        item["status_counts"][status] = item["status_counts"].get(status, 0) + 1
    http_failures_by_ip = sorted(
        (item for item in failures_by_ip.values() if item["failure_count"] >= failure_threshold),
        key=lambda item: (-item["failure_count"], item["ip"]),
    )
    return {
        "ok": result["exit_code"] == 0,
        "since_hours": since_hours,
        "total": len(events),
        "by_ip": _aggregate(events, "ip"),
        "by_detail": _aggregate(events, "detail"),
        "http_failure_threshold": failure_threshold,
        "http_failures_by_ip": http_failures_by_ip,
        "source_line_limit": NGINX_LOG_LINE_LIMIT,
        "source_truncated": len(log_lines) >= NGINX_LOG_LINE_LIMIT,
        "recent": events[-min(limit, 300):],
        "source_exit_code": result["exit_code"],
        "source_error": result["stderr"][:400] if result["exit_code"] else "",
        # 此采集显式禁用了 stdout 尾部裁剪；日志源行数上限由
        # source_truncated 单独、准确地表示。
        "stdout_capped": False,
    }


def _backup_audit() -> dict[str, Any]:
    backup_dir = _configured_backup_dir()
    if not backup_dir.is_dir():
        return {"ok": False, "error": "备份目录不存在", "dir": str(backup_dir)}
    now = datetime.now(timezone.utc)
    rows: list[dict[str, Any]] = []
    total_gzip_bytes = 0
    for path in backup_dir.glob("*.sql.gz"):
        try:
            if not path.is_file() or path.is_symlink():
                continue
            modified = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
            size = path.stat().st_size
            total_gzip_bytes += size
            rows.append(
                {
                    "name": path.name,
                    "size": size,
                    "age_hours": round((now - modified).total_seconds() / 3600, 2),
                    "has_sha256": path.with_name(path.name + ".sha256").is_file(),
                    "has_meta": path.with_name(path.name + ".meta").is_file(),
                    "modified_at": modified.isoformat(),
                }
            )
        except OSError:
            continue
    rows.sort(key=lambda item: item["modified_at"], reverse=True)
    other_bytes = 0
    other_count = 0
    try:
        children = list(backup_dir.iterdir())
    except OSError:
        children = []
    for path in children:
        if path.name.endswith((".sql.gz", ".sha256", ".meta")):
            continue
        try:
            if path.is_file():
                other_bytes += path.stat().st_size
                other_count += 1
            elif path.is_dir() and not path.is_symlink():
                other_bytes += sum(
                    item.stat().st_size
                    for item in path.rglob("*")
                    if item.is_file() and not item.is_symlink()
                )
                other_count += 1
        except OSError:
            continue
    return {
        "ok": True,
        "dir": str(backup_dir),
        "sql_gz_count": len(rows),
        "sql_gz_bytes": total_gzip_bytes,
        "other_entries_count": other_count,
        "other_bytes": other_bytes,
        "older_than_14_days": sum(1 for row in rows if row["age_hours"] > 24 * 14),
        "newest": rows[0] if rows else None,
        "oldest": rows[-1] if rows else None,
        "recent": rows[:100],
    }


DB_SQL_MAX_LEN = 160
_DB_DESTRUCTIVE_RE = re.compile(
    r"\b(drop\s+table|drop\s+database|truncate\s+table|delete\s+from|"
    r"alter\s+table|rename\s+table|grant\b|revoke\b|"
    r"create\s+user|drop\s+user|set\s+password)\b"
)
_DB_DUMP_RE = re.compile(r"\b(select\s+.+\s+into\s+(out|dump)file|load_file\s*\()\b")
_DB_ERROR_RE = re.compile(r"\b(access\s+denied|error\s+\d{3,5}|you have an error)\b")
_DB_SQL_REDACT_RE = re.compile(r"('[^']*'|\"[^\"]*\"|\b\d{4,}\b)")


def _normalize_db_sql(text: Any) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()[:DB_SQL_MAX_LEN]


def _redact_db_sql(text: str) -> str:
    return _DB_SQL_REDACT_RE.sub("?", text)


def parse_db_general_log(rows: list[dict[str, Any]]) -> dict[str, Any]:
    categories: dict[str, list[dict[str, Any]]] = {"destructive": [], "dump_exfil": [], "error": []}
    for row in rows:
        if not isinstance(row, dict):
            continue
        raw = _normalize_db_sql(row.get("argument"))
        if not raw:
            continue
        sample = {
            "user_host": str(row.get("user_host") or "")[:128],
            "sql": _redact_db_sql(raw),
            "event_time": str(row.get("event_time") or ""),
        }
        lowered = raw.lower()
        if _DB_DUMP_RE.search(lowered):
            categories["dump_exfil"].append(sample)
        elif _DB_DESTRUCTIVE_RE.search(lowered):
            categories["destructive"].append(sample)
        elif _DB_ERROR_RE.search(lowered):
            categories["error"].append(sample)

    def aggregate(items: list[dict[str, Any]], key: str) -> list[dict[str, Any]]:
        counts: dict[str, int] = {}
        for item in items:
            value = str(item.get(key) or "")
            counts[value] = counts.get(value, 0) + 1
        return [
            {"value": value, "count": count}
            for value, count in sorted(counts.items(), key=lambda pair: pair[1], reverse=True)
        ][:20]

    return {
        "destructive_total": len(categories["destructive"]),
        "dump_exfil_total": len(categories["dump_exfil"]),
        "error_total": len(categories["error"]),
        "destructive_by_user": aggregate(categories["destructive"], "user_host"),
        "dump_exfil_by_user": aggregate(categories["dump_exfil"], "user_host"),
        "error_by_user": aggregate(categories["error"], "user_host"),
        "samples": {name: values[-20:] for name, values in categories.items()},
    }


def _db_threat_signals(params: dict[str, Any]) -> dict[str, Any]:
    since_hours = _since_hours_arg(params)
    limit = _event_limit_arg(params, default=4000)
    sql = (
        "SELECT user_host, argument, event_time FROM mysql.general_log "
        f"WHERE event_time >= DATE_SUB(NOW(), INTERVAL {since_hours} HOUR) "
        f"AND command_type IN ('Query','Execute') ORDER BY event_time DESC LIMIT {limit}"
    )
    result = run([
        "docker", "exec", "cr_mysql", "sh", "-ec",
        (
            'MYSQL_PWD="$MYSQL_ROOT_PASSWORD" exec mysql --protocol=TCP -h 127.0.0.1 '
            '-uroot --batch --skip-column-names --raw -e "$1"'
        ),
        "sh", sql,
    ], timeout=90, allow_failure=True)
    if result["exit_code"] != 0:
        return {
            "ok": False,
            "since_hours": since_hours,
            "reason": "general_log 未开启或暂时不可读",
            "source_exit_code": result["exit_code"],
            "source_error": result["stderr"][:400],
        }
    rows: list[dict[str, Any]] = []
    for line in result["stdout"].splitlines():
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        rows.append({
            "user_host": parts[0],
            "argument": "\t".join(parts[1:-1]),
            "event_time": parts[-1],
        })
    parsed = parse_db_general_log(rows)
    parsed.update(
        {
            "ok": True,
            "since_hours": since_hours,
            "sampled_rows": len(rows),
            "stdout_capped": len(result["stdout"]) >= 100_000,
        }
    )
    return parsed


_DB_RESTART_RE = re.compile(r"/usr/sbin/mysqld: ready for connections", re.IGNORECASE)
_DB_RECOVERY_RE = re.compile(
    r"(InnoDB: (Starting crash recovery|Doing recovery|Database was not shutdown normally)|"
    r"Starting crash recovery|crash recovery|forcing InnoDB Recovery)",
    re.IGNORECASE,
)


def parse_db_error_log(lines: list[str]) -> dict[str, Any]:
    restarts: list[str] = []
    recovery: list[str] = []
    for line in lines:
        text = str(line or "")
        if _DB_RESTART_RE.search(text):
            restarts.append(text.strip()[:200])
        if _DB_RECOVERY_RE.search(text):
            recovery.append(text.strip()[:200])
    return {
        "restart_count": len(restarts),
        "recovery_detected": bool(recovery),
        "restart_lines": restarts[-10:],
        "recovery_lines": recovery[-10:],
    }


def _db_health() -> dict[str, Any]:
    restart_count = run(
        ["docker", "inspect", "cr_mysql", "--format", "{{.RestartCount}}"],
        timeout=30,
        allow_failure=True,
    )
    memory = run(
        ["docker", "stats", "cr_mysql", "--no-stream", "--format", "{{.MemUsage}}"],
        timeout=30,
        allow_failure=True,
    )
    logs = run(["docker", "logs", "cr_mysql", "--since", "24h"], timeout=60, allow_failure=True)
    combined = (logs.get("stdout") or "") + "\n" + (logs.get("stderr") or "")
    parsed = parse_db_error_log(combined.splitlines())
    failures = [item for item in (restart_count, memory, logs) if item.get("exit_code") != 0]
    parsed.update({
        "ok": not failures,
        "container_restart_count": restart_count.get("stdout", "").strip(),
        "mem_usage": memory.get("stdout", "").strip(),
        "source_errors": [str(item.get("stderr") or item.get("stdout") or "采集失败")[:300] for item in failures],
    })
    return parsed


def _threat_intel_base() -> str:
    try:
        value = _read_env("THREAT_INTEL_BASE_URL")
    except RuntimeError:
        return "http://ip-api.com/json"
    if not re.fullmatch(r"https?://[A-Za-z0-9.-]+(/[A-Za-z0-9._~/-]*)*", value):
        raise ValueError("THREAT_INTEL_BASE_URL 不合法")
    return value.rstrip("/")


def _ip_attribution(params: dict[str, Any]) -> dict[str, Any]:
    import ipaddress

    ip = str(params.get("ip") or "")
    try:
        ipaddress.ip_address(ip)
    except ValueError as exc:
        raise ValueError("ip 不是合法地址") from exc
    url = f"{_threat_intel_base()}/{ip}?fields=status,message,country,regionName,city,isp,org,as,query"
    result = run(["curl", "-fsS", "--max-time", "15", url], timeout=20, allow_failure=True)
    try:
        data = json.loads(result["stdout"])
    except json.JSONDecodeError:
        data = {"error": "无法解析情报响应"}
    return {
        "ok": result["exit_code"] == 0 and isinstance(data, dict) and data.get("status") != "fail",
        "ip": ip,
        "attribution": data,
        "source_exit_code": result["exit_code"],
        "source_error": result["stderr"][:400] if result["exit_code"] else "",
    }



def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _redact_text(value: str) -> str:
    redacted = re.sub(
        r"(?i)(password|passwd|token|secret|api[_-]?key|authorization)(\s*[:=]\s*)([^\s,;]+)",
        r"\1\2[REDACTED]",
        value,
    )
    return re.sub(r"(?i)(Bearer\s+)[A-Za-z0-9._~+/-]{8,}", r"\1[REDACTED]", redacted)


def _audit_params(action: str, params: dict[str, Any]) -> dict[str, Any]:
    sensitive = re.compile(
        r"(?i)^(?:content|public[_-]?key|private[_-]?key|password|passwd|token|secret|api[_-]?key|authorization)$"
    )

    def sanitize(value: Any, key: str = "") -> Any:
        if sensitive.fullmatch(key):
            return "[REDACTED]"
        if isinstance(value, dict):
            return {str(child_key): sanitize(child, str(child_key)) for child_key, child in value.items()}
        if isinstance(value, list):
            return [sanitize(child) for child in value]
        if isinstance(value, str):
            return _redact_text(value)
        return value

    return sanitize(params)


def _write_host_audit(
    *, request_id: str, action: str, params: dict[str, Any], status_value: str,
    duration_ms: int, error: str = "",
) -> None:
    try:
        AUDIT_LOG.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(AUDIT_LOG.parent, 0o700)
        record = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "request_id": request_id,
            "action": action,
            "params": _audit_params(action, params),
            "status": status_value,
            "duration_ms": duration_ms,
            "error": _redact_text(error)[:1000],
        }
        descriptor = os.open(AUDIT_LOG, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.write(descriptor, (json.dumps(record, ensure_ascii=False, default=str) + "\n").encode("utf-8"))
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except Exception as exc:  # noqa: BLE001 - 审计失败必须让 systemd 日志可见
        sys.stderr.write(f"prism-ops audit failure: {_redact_text(str(exc))[:500]}\n")


def _request_digest(action: str, params: dict[str, Any]) -> str:
    raw = json.dumps({"action": action, "params": params}, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _ledger_path(request_id: str) -> Path:
    if not REQUEST_ID.fullmatch(request_id):
        raise ValueError("request_id 不合法")
    return LEDGER_DIR / f"{request_id}.json"


def _read_ledger(request_id: str) -> dict[str, Any] | None:
    path = _ledger_path(request_id)
    if not path.exists():
        return None
    if path.is_symlink() or not path.is_file():
        raise RuntimeError("执行幂等账本路径不安全")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError("执行幂等账本损坏，已禁止重试") from exc
    if not isinstance(payload, dict):
        raise TypeError("执行幂等账本格式错误，已禁止重试")
    return payload


def _write_ledger(request_id: str, payload: dict[str, Any]) -> None:
    LEDGER_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(LEDGER_DIR, 0o700)
    path = _ledger_path(request_id)
    descriptor, temp_name = tempfile.mkstemp(prefix=f".{request_id}.", dir=str(LEDGER_DIR))
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True, default=str)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp_name, 0o600)
        os.replace(temp_name, path)
        directory_fd = os.open(LEDGER_DIR, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def _backup_file(name: str) -> Path:
    if not re.fullmatch(r"code_review_[A-Za-z0-9_.-]+\.sql\.gz", name):
        raise ValueError("备份文件名不合法")
    backup_dir = _configured_backup_dir()
    path = (backup_dir / name).resolve()
    if path.parent != backup_dir or not path.is_file():
        raise ValueError("备份文件不存在")
    return path


def _configured_backup_dir() -> Path:
    """读取跨版本持久备份目录；未配置时沿用原默认路径。"""
    raw = os.environ.get("BACKUP_DIR", "").strip()
    if not raw:
        try:
            raw = _read_env("BACKUP_DIR").strip()
        except (OSError, RuntimeError):
            raw = ""
    if not raw:
        return BACKUP_DIR
    path = Path(raw)
    if not path.is_absolute():
        path = DEPLOY_DIR / path
    return path.resolve()


def _read_env(key: str) -> str:
    pattern = re.compile(rf"^\s*{re.escape(key)}\s*=\s*(.*?)\s*$")
    value: str | None = None
    for line in (DEPLOY_DIR / ".env").read_text(encoding="utf-8").splitlines():
        match = pattern.match(line)
        if match:
            value = match.group(1)
    if not value:
        raise RuntimeError(f"缺少配置 {key}")
    if value[0] in {"\"", "'"}:
        if len(value) < 2 or value[-1] != value[0]:
            raise ValueError(f"配置 {key} 的引号格式不匹配")
        value = value[1:-1]
    return value


def _configured_certbot_conf_dir() -> Path:
    """读取与 Compose bind mount 相同的证书目录，不向 root 进程注入 dotenv。"""
    configured = os.environ.get("CERTBOT_CONF_DIR")
    if configured is None:
        try:
            configured = _read_env("CERTBOT_CONF_DIR")
        except RuntimeError:
            configured = ""
    configured = configured.strip()
    if len(configured) >= 2 and configured[0] == configured[-1] and configured[0] in {"'", '"'}:
        configured = configured[1:-1]
    conf_dir = Path(configured or "./certbot/conf")
    if not conf_dir.is_absolute():
        # Compose resolves relative bind-mount sources from the compose/deploy directory.
        conf_dir = DEPLOY_DIR / conf_dir
    return conf_dir.resolve()


def _update_env(key: str, value: str) -> None:
    path = DEPLOY_DIR / ".env"
    lines = path.read_text(encoding="utf-8").splitlines()
    updated = False
    output: list[str] = []
    for line in lines:
        if line.startswith(f"{key}="):
            output.append(f"{key}={value}")
            updated = True
        else:
            output.append(line)
    if not updated:
        output.append(f"{key}={value}")
    temp = path.with_suffix(".env.tmp")
    temp.write_text("\n".join(output) + "\n", encoding="utf-8")
    os.chmod(temp, 0o600)
    os.replace(temp, path)


def _wait_service(service: str) -> None:
    deadline = time.monotonic() + 150
    while time.monotonic() < deadline:
        result = run(["docker", "compose", "--env-file", ".env", "ps", "-q", service], timeout=20)
        container_id = result["stdout"].strip()
        if container_id:
            state = run([
                "docker", "inspect", "--format",
                "{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}",
                container_id,
            ], timeout=20)["stdout"].strip()
            if state in {"healthy", "running"}:
                return
        time.sleep(2)
    raise RuntimeError(f"服务 {service} 重启后未恢复健康")


def _execute_with_concurrency_policy(
    action: str,
    params: dict[str, Any],
    request_id: str,
) -> dict[str, Any]:
    """读巡检可并发，有副作用的宿主机动作必须串行。"""
    if action in READ_ONLY_ACTIONS:
        return execute(action, params, request_id=request_id)
    with MUTATION_LOCK:
        return execute(action, params, request_id=request_id)


def _authorized_peer(connection: Any) -> bool:
    """只接受 Linux Unix-socket peer credentials 中的固定后端身份；不回退共享令牌。"""
    peer_option = getattr(socket, "SO_PEERCRED", None)
    if peer_option is None:
        return False
    try:
        raw = connection.getsockopt(socket.SOL_SOCKET, peer_option, struct.calcsize("3i"))
        pid, uid, gid = struct.unpack("3i", raw)
    except (AttributeError, OSError, struct.error, TypeError):
        return False
    return pid > 0 and uid == ALLOWED_PEER_UID and gid == ALLOWED_PEER_GID


def _prepare_socket_directory() -> None:
    parent = SOCKET_PATH.parent
    if parent.is_symlink():
        raise RuntimeError("运维 socket 目录不能是符号链接")
    parent.mkdir(parents=True, exist_ok=True, mode=SOCKET_DIRECTORY_MODE)
    metadata = parent.lstat()
    if not stat.S_ISDIR(metadata.st_mode):
        raise RuntimeError("运维 socket 父路径不是目录")
    if metadata.st_uid != SOCKET_DIRECTORY_UID or metadata.st_gid != SOCKET_DIRECTORY_GID:
        raise RuntimeError("运维 socket 父目录属主必须为 root:prism-ops")
    os.chmod(parent, SOCKET_DIRECTORY_MODE)
    if parent.lstat().st_mode & 0o7777 != SOCKET_DIRECTORY_MODE:
        raise RuntimeError("运维 socket 父目录权限设置失败")


def _remove_stale_socket() -> None:
    if SOCKET_PATH.is_symlink():
        raise RuntimeError("拒绝覆盖符号链接运维 socket")
    if not SOCKET_PATH.exists():
        return
    if not stat.S_ISSOCK(SOCKET_PATH.lstat().st_mode):
        raise RuntimeError("拒绝覆盖非 socket 运维路径")
    SOCKET_PATH.unlink()


class Handler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:
        try:
            if not _authorized_peer(self.connection):
                self._json(403, {"ok": False, "error": "unix socket peer 不在允许范围"})
                return
            if self.path != "/execute":
                self._json(404, {"ok": False, "error": "not found"})
                return
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > 64 * 1024:
                raise ValueError("请求体大小不合法")
            body = json.loads(self.rfile.read(length))
            action = str(body.get("action") or "")
            params = body.get("params") if isinstance(body.get("params"), dict) else {}
            request_id = str(body.get("request_id") or "")
            if not REQUEST_ID.fullmatch(request_id):
                raise ValueError("request_id 不合法")
            digest = _request_digest(action, params)
            with LEDGER_LOCK:
                recorded = _read_ledger(request_id)
                if recorded is not None:
                    if not hmac.compare_digest(str(recorded.get("request_digest") or ""), digest):
                        self._json(409, {"ok": False, "error": "request_id 已绑定其他运维请求"})
                        return
                    status_value = str(recorded.get("status") or "")
                    if status_value == "success":
                        response = dict(recorded.get("response") or {})
                        response["duplicate"] = True
                        self._json(200, response)
                        return
                    if status_value == "failed":
                        self._json(
                            400,
                            {"ok": False, "error": str(recorded.get("error") or "运维动作已失败"), "duplicate": True},
                        )
                        return
                    recovery = "请核对宿主机审计与实际状态后，使用新的 request_id 再决定是否执行"
                    self._json(409, {
                        "ok": False,
                        "error": "动作已开始但结果未确认，已禁止自动重试",
                        "duplicate": True,
                        "retryable": False,
                        "next_action": recovery,
                    })
                    return
                _write_ledger(
                    request_id,
                    {
                        "request_id": request_id,
                        "request_digest": digest,
                        "action": action,
                        "params": _audit_params(action, params),
                        "status": "running",
                        "started_at": datetime.now(timezone.utc).isoformat(),
                    },
                )
            started = time.monotonic()
            try:
                result = _execute_with_concurrency_policy(action, params, request_id)
                semantic_ok = not (
                    action == "status"
                    and (result.get("health_status") == "error" or not result.get("can_continue"))
                )
                response = {
                    "ok": semantic_ok,
                    "action": action,
                    "result": result,
                    "duplicate": False,
                }
                if not semantic_ok:
                    response.update({
                        "error": result.get("summary") or "生产关键检查未通过",
                        "retryable": False,
                        "next_action": "请先按巡检动作建议完成人工处置，再重新检查",
                    })
                _write_ledger(
                    request_id,
                    {
                        "request_id": request_id,
                        "request_digest": digest,
                        "action": action,
                        "params": _audit_params(action, params),
                        "status": "success",
                        "finished_at": datetime.now(timezone.utc).isoformat(),
                        "response": response,
                    },
                )
                _write_host_audit(
                    request_id=request_id,
                    action=action,
                    params=params,
                    status_value="success",
                    duration_ms=int((time.monotonic() - started) * 1000),
                )
            except Exception as exc:  # noqa: BLE001 - 宿主机失败必须进入幂等账本和执行审计
                error = _redact_text(str(exc))[:4000]
                _write_ledger(
                    request_id,
                    {
                        "request_id": request_id,
                        "request_digest": digest,
                        "action": action,
                        "params": _audit_params(action, params),
                        "status": "failed",
                        "finished_at": datetime.now(timezone.utc).isoformat(),
                        "error": error,
                    },
                )
                _write_host_audit(
                    request_id=request_id,
                    action=action,
                    params=params,
                    status_value="failed",
                    duration_ms=int((time.monotonic() - started) * 1000),
                    error=error,
                )
                self._json(400, {"ok": False, "error": error, "duplicate": False})
                return
            self._json(200, response)
        except Exception as exc:  # noqa: BLE001 - 返回脱敏错误摘要
            self._json(400, {"ok": False, "error": _redact_text(str(exc))[:4000]})

    def log_message(self, fmt: str, *args: object) -> None:
        sys.stderr.write("prism-ops: " + (fmt % args) + "\n")

    def _json(self, status: int, payload: dict[str, Any]) -> None:
        raw = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


class UnixHTTPServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    allow_reuse_address = True
    daemon_threads = True


def main() -> None:
    _prepare_socket_directory()
    _remove_stale_socket()
    server = UnixHTTPServer(str(SOCKET_PATH), Handler)
    metadata = SOCKET_PATH.lstat()
    if not stat.S_ISSOCK(metadata.st_mode):
        server.server_close()
        raise RuntimeError("运维 socket 路径不是 socket")
    if metadata.st_uid != SOCKET_DIRECTORY_UID or metadata.st_gid != SOCKET_DIRECTORY_GID:
        server.server_close()
        SOCKET_PATH.unlink()
        raise RuntimeError("运维 socket 属主必须为 root:prism-ops")
    os.chmod(SOCKET_PATH, 0o660)
    try:
        server.serve_forever()
    finally:
        server.server_close()
        if SOCKET_PATH.exists() and stat.S_ISSOCK(SOCKET_PATH.lstat().st_mode):
            SOCKET_PATH.unlink()


if __name__ == "__main__":
    main()
