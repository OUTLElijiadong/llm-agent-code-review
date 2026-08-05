#!/usr/bin/env python3
"""Root-side allowlisted operations executor exposed only through a Unix socket."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import pwd
import re
import shutil
import socketserver
import stat
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from typing import Any

DEPLOY_DIR = Path(__file__).resolve().parent
BACKUP_DIR = (DEPLOY_DIR.parent / "backups").resolve()
SOCKET_PATH = Path(os.environ.get("OPS_EXECUTOR_SOCKET", "/run/prism-ops/agent.sock"))
TOKEN = os.environ.get("OPS_EXECUTOR_TOKEN", "")
AUDIT_LOG = Path(os.environ.get("OPS_EXECUTOR_AUDIT_LOG", "/var/log/prism-ops/executions.jsonl"))
FILE_BACKUP_DIR = Path(os.environ.get("OPS_FILE_BACKUP_DIR", "/var/lib/prism-ops/file-backups"))
LEDGER_DIR = Path(os.environ.get("OPS_EXECUTOR_LEDGER_DIR", "/var/lib/prism-ops/execution-ledger"))
SERVICES = {"backend", "frontend", "mysql", "redis", "clamav"}
MAX_TEXT_BYTES = 256 * 1024
MAX_DIRECTORY_ENTRIES = 500
UNIT_NAME = re.compile(r"^[A-Za-z0-9_.@:-]{1,128}$")
CONTAINER_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
PACKAGE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9+_.:@-]{0,127}$")
USERNAME = re.compile(r"^[a-z_][a-z0-9_-]{0,30}\$?$")
ZONE_NAME = re.compile(r"^[A-Za-z0-9_-]{1,32}$")
REQUEST_ID = re.compile(r"^[A-Za-z0-9_.:-]{8,128}$")
SSH_KEY_TYPES = {
    "ssh-ed25519",
    "ssh-rsa",
    "ecdsa-sha2-nistp256",
    "ecdsa-sha2-nistp384",
    "ecdsa-sha2-nistp521",
}
DENIED_PATH_ROOTS = tuple(Path(value) for value in ("/proc", "/sys", "/dev", "/run"))
SENSITIVE_EXACT_PATHS = {Path("/etc/shadow"), Path("/etc/gshadow")}
SENSITIVE_NAMES = {".env", "authorized_keys", "credentials", "credentials.json"}
SENSITIVE_SUFFIXES = {".pem", ".key", ".p12", ".pfx", ".jks"}
PROTECTED_ACCOUNTS = {"root", "bin", "daemon", "mysql", "redis", "docker", "sshd"}
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
    "systemd_unit_action": {"unit", "operation"},
    "docker_container_action": {"container", "operation"},
    "write_text_file": {"path", "content", "expected_sha256", "mode"},
    "package_action": {"operation", "packages"},
    "firewall_action": {"operation", "target_type", "value", "zone"},
    "account_action": {"operation", "username", "shell", "remove_home"},
    "ssh_authorized_key_action": {"operation", "username", "public_key", "fingerprint"},
    "ssh_login_events": {"since_hours", "limit", "focus"},
    "flytrap_attack_events": {"since_hours", "limit"},
    "nginx_attack_events": {"since_hours", "limit"},
    "backup_audit": set(),
    "db_threat_signals": {"since_hours", "limit"},
    "db_health": set(),
    "ip_attribution": {"ip"},
}


def run(args: list[str], *, timeout: int = 900, allow_failure: bool = False) -> dict[str, Any]:
    completed = subprocess.run(
        args,
        cwd=DEPLOY_DIR,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
        env={**os.environ, "DEPLOY_ENV_FILE": ".env"},
    )
    stdout = _redact_text(completed.stdout[-100_000:])
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
    if action == "status":
        result = run([str(DEPLOY_DIR / "ops-check.sh")], allow_failure=True)
        try:
            checks = json.loads(result["stdout"])
        except json.JSONDecodeError as exc:
            raise RuntimeError("ops-check 未返回有效 JSON") from exc
        return {"checks": checks, "command_exit": result["exit_code"]}
    if action == "certificate_status":
        domain = _read_env("APP_DOMAIN")
        cert = DEPLOY_DIR / "certbot" / "conf" / "live" / domain / "fullchain.pem"
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
            'MYSQL_PWD="$MYSQL_ROOT_PASSWORD" exec mysqlcheck '
            '--protocol=TCP -h 127.0.0.1 -uroot --analyze "$MYSQL_DATABASE"',
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
    if action == "systemd_unit_action":
        return _systemd_unit_action(params)
    if action == "docker_container_action":
        return _docker_container_action(params)
    if action == "write_text_file":
        return _write_text_file(params, request_id=request_id)
    if action == "package_action":
        return _package_action(params)
    if action == "firewall_action":
        return _firewall_action(params)
    if action == "account_action":
        return _account_action(params)
    if action == "ssh_authorized_key_action":
        return _ssh_authorized_key_action(params, request_id=request_id)
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


def _since_hours_arg(params: dict[str, Any], default: int = 24) -> int:
    raw = params.get("since_hours")
    since_hours = int(raw) if raw is not None else default
    if since_hours < 1 or since_hours > MAX_SINCE_HOURS:
        raise ValueError(f"since_hours 必须在 1 到 {MAX_SINCE_HOURS} 之间")
    return since_hours


def _event_limit_arg(params: dict[str, Any], default: int = 1000) -> int:
    raw = params.get("limit")
    limit = int(raw) if raw is not None else default
    if limit < 1 or limit > MAX_EVENT_LIMIT:
        raise ValueError(f"limit 必须在 1 到 {MAX_EVENT_LIMIT} 之间")
    return limit


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
        line = raw.rstrip("\n")
        match = accepted_pattern.search(line)
        if match:
            accepted.append({
                "method": match.group(1),
                "user": match.group(2),
                "ip": match.group(3),
                "detail": match.group(4) or "",
            })
            continue
        match = failed_pattern.search(line)
        if match:
            failed.append({
                "user": match.group(1),
                "ip": match.group(2),
                "detail": "failed_password",
            })
            continue
        match = invalid_pattern.search(line)
        if match:
            failed.append({
                "user": match.group(1),
                "ip": match.group(2),
                "detail": "invalid_user",
            })
    return {"accepted": accepted, "failed": failed}


def parse_flytrap_log(lines: list[str]) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for raw in lines:
        line = raw.strip()
        if not line.startswith("{"):
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict):
            continue
        remote = str(payload.get("remote") or "")
        ip = remote.rsplit(":", 1)[0] if ":" in remote else remote
        events.append({
            "time": payload.get("time") or "",
            "username": payload.get("username") or "",
            "ip": ip,
            "message": payload.get("message") or "",
        })
    return events


def parse_nginx_log(lines: list[str]) -> list[dict[str, Any]]:
    access_pattern = re.compile(
        r'^(\S+) - - \[[^\]]+\] "([^"]*)" (\d{3})'
    )
    events: list[dict[str, Any]] = []
    for raw in lines:
        line = raw.strip()
        match = access_pattern.match(line)
        if not match:
            continue
        ip = match.group(1)
        request = match.group(2)
        status = match.group(3)
        method = request.split(" ", 1)[0] if request else ""
        detail = "normal"
        if method == "CONNECT":
            detail = "proxy_connect"
        elif method.startswith("\\x") or not method.isalpha():
            detail = "tls_gibberish"
        elif status in {"400", "403", "444"}:
            detail = f"http_{status}"
        else:
            continue
        events.append({
            "ip": ip,
            "method": method,
            "path": request[:200],
            "status": status,
            "detail": detail,
        })
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
    result = run([
        "journalctl", "-u", "sshd", "--since", f"{since_hours} hours ago", "--no-pager",
        "--output=short-iso", "-n", "20000",
    ], timeout=90, allow_failure=True)
    parsed = parse_ssh_log(result["stdout"].splitlines())
    accepted = parsed["accepted"]
    failed = parsed["failed"]
    selected: list[dict[str, Any]] = []
    if focus in {"all", "accepted"}:
        selected.extend(accepted)
    if focus in {"all", "failed"}:
        selected.extend(failed)
    selected = selected[-limit:]
    return {
        "since_hours": since_hours,
        "focus": focus,
        "accepted_total": len(accepted),
        "failed_total": len(failed),
        "accepted_by_ip": _aggregate(accepted, "ip"),
        "accepted_by_detail": _aggregate(accepted, "detail"),
        "failed_by_ip": _aggregate(failed, "ip"),
        "recent": selected[-min(limit, 200):],
        "stdout_capped": len(result["stdout"]) >= 100_000,
    }


def _flytrap_attack_events(params: dict[str, Any]) -> dict[str, Any]:
    since_hours = _since_hours_arg(params)
    limit = _event_limit_arg(params, default=1000)
    result = run([
        "journalctl", "-u", "flytrap-agent", "--since", f"{since_hours} hours ago",
        "--no-pager", "--output=short-iso", "-n", "30000",
    ], timeout=90, allow_failure=True)
    events = parse_flytrap_log(result["stdout"].splitlines())
    return {
        "since_hours": since_hours,
        "total": len(events),
        "by_ip": _aggregate(events, "ip"),
        "by_username": _aggregate(events, "username"),
        "recent": events[-min(limit, 300):],
        "stdout_capped": len(result["stdout"]) >= 100_000,
    }


def _nginx_attack_events(params: dict[str, Any]) -> dict[str, Any]:
    since_hours = _since_hours_arg(params)
    limit = _event_limit_arg(params, default=1000)
    result = run([
        "docker", "logs", "--since", f"{since_hours}h", "-n", "30000", "cr_frontend",
    ], timeout=90, allow_failure=True)
    events = parse_nginx_log(result["stdout"].splitlines())
    return {
        "since_hours": since_hours,
        "total": len(events),
        "by_ip": _aggregate(events, "ip"),
        "by_detail": _aggregate(events, "detail"),
        "recent": events[-min(limit, 300):],
        "stdout_capped": len(result["stdout"]) >= 100_000,
    }


def _backup_audit() -> dict[str, Any]:
    backup_dir = BACKUP_DIR
    if not backup_dir.is_dir():
        return {"error": "备份目录不存在", "dir": str(backup_dir)}
    gzip_files = sorted(backup_dir.glob("*.sql.gz"))
    now = datetime.now(timezone.utc)
    rows: list[dict[str, Any]] = []
    total_gzip_bytes = 0
    for path in gzip_files:
        if not path.is_file() or path.is_symlink():
            continue
        size = path.stat().st_size
        total_gzip_bytes += size
        modified = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
        rows.append({
            "name": path.name,
            "size": size,
            "age_hours": round((now - modified).total_seconds() / 3600, 2),
            "has_sha256": path.with_name(path.name + ".sha256").is_file(),
            "has_meta": path.with_name(path.name + ".meta").is_file(),
            "modified_at": modified.isoformat(),
        })
    rows.sort(key=lambda item: item["modified_at"], reverse=True)
    other_bytes = 0
    other_count = 0
    for path in backup_dir.iterdir():
        if path.name.endswith((".sql.gz", ".sha256", ".meta")):
            continue
        try:
            if path.is_file():
                other_bytes += path.stat().st_size
                other_count += 1
            elif path.is_dir():
                sub_total = sum(item.stat().st_size for item in path.rglob("*") if item.is_file())
                other_bytes += sub_total
                other_count += 1
        except OSError:
            continue
    return {
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


# ── 生产数据库内部威胁信号（mysql.general_log 只读采样） ──
# 归一化 SQL 文本后按类别正则归类；仅返回聚合统计与截断样本，绝不回传参数/数据。
DB_SQL_MAX_LEN = 160
_DB_DESTRUCTIVE_RE = re.compile(
    r"\b(drop\s+table|drop\s+database|truncate\s+table|delete\s+from|"
    r"update\s+\w+\s+set|alter\s+table|rename\s+table|grant\b|revoke\b|"
    r"create\s+user|drop\s+user|set\s+password)\b"
)
_DB_DUMP_RE = re.compile(r"\b(select\s+.+\s+into\s+(out|dump)file|load_file\s*\()\b")
_DB_ERROR_RE = re.compile(r"\b(error|denied|access\s+denied)\b")
_DB_SQL_REDACT_RE = re.compile(r"('[^']*'|\"[^\"]*\"|\b\d{4,}\b)")


def _normalize_db_sql(text: str) -> str:
    """折叠空白并截断超长 SQL 文本。"""
    collapsed = re.sub(r"\s+", " ", str(text or "")).strip()
    return collapsed[:DB_SQL_MAX_LEN]


def _redact_db_sql(text: str) -> str:
    """脱敏 SQL 文本：字符串/长数字字面量替换为占位符，避免回传真实数据。"""
    return _DB_SQL_REDACT_RE.sub("?", text)


def parse_db_general_log(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """把 mysql.general_log 采样行归类为威胁信号聚合。

    纯函数便于单测。输入行为 dict（含 user_host / argument / event_time）。
    """
    categories: dict[str, list[dict[str, Any]]] = {
        "destructive": [],
        "dump_exfil": [],
        "error": [],
    }
    for row in rows:
        if not isinstance(row, dict):
            continue
        raw = _normalize_db_sql(row.get("argument"))
        if not raw:
            continue
        lowered = raw.lower()
        sample = {
            "user_host": str(row.get("user_host") or "")[:128],
            "sql": _redact_db_sql(raw),
            "event_time": str(row.get("event_time") or ""),
        }
        if _DB_DUMP_RE.search(lowered):
            categories["dump_exfil"].append(sample)
        elif _DB_DESTRUCTIVE_RE.search(lowered):
            categories["destructive"].append(sample)
        elif _DB_ERROR_RE.search(lowered):
            categories["error"].append(sample)

    def _agg(items: list[dict[str, Any]], key: str) -> list[dict[str, Any]]:
        counts: dict[str, int] = {}
        for item in items:
            value = str(item.get(key) or "")
            counts[value] = counts.get(value, 0) + 1
        return [
            {"value": value, "count": count}
            for value, count in sorted(counts.items(), key=lambda kv: kv[1], reverse=True)
        ][:20]

    return {
        "destructive_total": len(categories["destructive"]),
        "dump_exfil_total": len(categories["dump_exfil"]),
        "error_total": len(categories["error"]),
        "destructive_by_user": _agg(categories["destructive"], "user_host"),
        "dump_exfil_by_user": _agg(categories["dump_exfil"], "user_host"),
        "error_by_user": _agg(categories["error"], "user_host"),
        "samples": {
            "destructive": categories["destructive"][-20:],
            "dump_exfil": categories["dump_exfil"][-20:],
            "error": categories["error"][-20:],
        },
    }


def _db_threat_signals(params: dict[str, Any]) -> dict[str, Any]:
    """只读采样 mysql.general_log 并归类数据库内部威胁信号。

    要求运维已开启 general_log 且 log_output 含 TABLE；未开启时返回
    ok=False（不抛异常），不影响整体安全巡检。
    """
    since_hours = _since_hours_arg(params)
    limit = _event_limit_arg(params, default=4000)
    root_password = _read_env("MYSQL_ROOT_PASSWORD")
    database = _read_env("MYSQL_DATABASE")
    sql = (
        "SELECT user_host, argument, event_time FROM mysql.general_log "
        f"WHERE event_time >= DATE_SUB(NOW(), INTERVAL {since_hours} HOUR) "
        f"AND command_type IN ('Query','Execute') ORDER BY event_time DESC LIMIT {limit}"
    )
    result = run([
        "docker", "exec", "cr_mysql", "sh", "-c",
        f"MYSQL_PWD='{root_password}' mysql --protocol=TCP -h 127.0.0.1 -uroot "
        f"--batch --skip-column-names --raw -e \"{sql}\"",
    ], timeout=90, allow_failure=True)
    if result["exit_code"] != 0:
        return {
            "ok": False,
            "since_hours": since_hours,
            "reason": "general_log 未开启或不可读（须运维开启 general_log 且 log_output 含 TABLE）",
            "stderr": result["stderr"][:400],
        }
    rows: list[dict[str, Any]] = []
    for line in result["stdout"].splitlines():
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        rows.append({
            "user_host": parts[0],
            # 保留列中的制表符合并回 argument
            "argument": "\t".join(parts[1:-1]) if len(parts) > 3 else parts[1],
            "event_time": parts[-1],
        })
    parsed = parse_db_general_log(rows)
    parsed.update({
        "ok": True,
        "since_hours": since_hours,
        "sampled_rows": len(rows),
        "database": database,
        "stdout_capped": len(result["stdout"]) >= 100_000,
    })
    return parsed


_DB_RESTART_RE = re.compile(r"ready for connections", re.IGNORECASE)
_DB_RECOVERY_RE = re.compile(
    r"(InnoDB: (Starting crash recovery|Doing recovery|Database was not shutdown normally)|"
    r"Starting crash recovery|crash recovery|forcing InnoDB Recovery)", re.IGNORECASE,
)


def parse_db_error_log(lines: list[str]) -> dict[str, Any]:
    """解析 mysqld 容器日志，识别重启与崩溃恢复迹象（纯函数便于单测）。"""
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
    """采集 MySQL 可用性健康：容器重启计数、内存、近期崩溃恢复日志。

    用于提前发现 OOM 误杀 / 崩溃恢复这类直接威胁生产数据可用性的事件。
    """
    restart_count = run([
        "docker", "inspect", "cr_mysql", "--format", "{{.RestartCount}}",
    ], timeout=30, allow_failure=True)
    mem = run([
        "docker", "stats", "cr_mysql", "--no-stream", "--format", "{{.MemUsage}}",
    ], timeout=30, allow_failure=True)
    logs = run([
        "docker", "logs", "cr_mysql", "--since", "24h",
    ], timeout=60, allow_failure=True)
    combined = (logs.get("stdout") or "") + "\n" + (logs.get("stderr") or "")
    parsed = parse_db_error_log(combined.splitlines())
    parsed.update({
        "container_restart_count": restart_count.get("stdout", "").strip(),
        "mem_usage": mem.get("stdout", "").strip(),
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
    base_url = _threat_intel_base()
    url = f"{base_url}/{ip}?fields=status,message,country,regionName,city,isp,org,as,query"
    result = run(["curl", "-fsS", "--max-time", "15", url], timeout=20, allow_failure=True)
    try:
        data = json.loads(result["stdout"])
    except (json.JSONDecodeError, UnicodeDecodeError):
        data = {"error": "无法解析情报响应", "exit_code": result["exit_code"]}
    return {"ip": ip, "attribution": data, "command_exit": result["exit_code"]}


def _systemd_unit_action(params: dict[str, Any]) -> dict[str, Any]:
    operation = str(params.get("operation") or "")
    if operation == "daemon_reload":
        return run(["systemctl", "daemon-reload"], timeout=60)
    if operation not in {"start", "stop", "restart", "reload", "enable", "disable"}:
        raise ValueError("systemd operation 不合法")
    unit = str(params.get("unit") or "")
    if not UNIT_NAME.fullmatch(unit):
        raise ValueError("unit 名称不合法")
    load_state = run(["systemctl", "show", unit, "--property=LoadState", "--value"], timeout=30)["stdout"].strip()
    if load_state == "not-found":
        raise ValueError("systemd unit 不存在")
    result = run(["systemctl", operation, unit], timeout=180)
    state = run([
        "systemctl", "show", unit, "--property=LoadState,ActiveState,SubState,UnitFileState", "--no-pager",
    ], timeout=30, allow_failure=True)
    return {"operation": result, "state": state}


def _docker_container_action(params: dict[str, Any]) -> dict[str, Any]:
    operation = str(params.get("operation") or "")
    if operation not in {"start", "stop", "restart", "pause", "unpause"}:
        raise ValueError("Docker operation 不合法")
    container = str(params.get("container") or "")
    if not CONTAINER_NAME.fullmatch(container):
        raise ValueError("container 名称不合法")
    run(["docker", "inspect", container], timeout=30)
    args = ["docker", operation]
    if operation in {"stop", "restart"}:
        args.extend(["--time", "30"])
    args.append(container)
    result = run(args, timeout=180)
    state = run([
        "docker", "inspect", "--format",
        "{{json .State}}", container,
    ], timeout=30)
    return {"operation": result, "state": state}


def _write_text_file(params: dict[str, Any], *, request_id: str) -> dict[str, Any]:
    if not REQUEST_ID.fullmatch(request_id):
        raise ValueError("request_id 不合法")
    path = _safe_text_path(params.get("path"), must_exist=False)
    content = params.get("content")
    if not isinstance(content, str):
        raise ValueError("content 必须是 UTF-8 文本")
    payload = content.encode("utf-8")
    if len(payload) > MAX_TEXT_BYTES:
        raise ValueError(f"content 不能超过 {MAX_TEXT_BYTES} 字节")
    parent = path.parent
    if not parent.is_dir() or parent.is_symlink():
        raise ValueError("目标父目录不存在或不安全")
    expected = str(params.get("expected_sha256") or "")
    existed = path.exists()
    current_sha = _sha256_file(path) if existed else ""
    if expected and expected != current_sha:
        raise ValueError("文件已变化，expected_sha256 不匹配")
    requested_mode = str(params.get("mode") or "")
    if requested_mode:
        if not re.fullmatch(r"0?[0-7]{3}", requested_mode):
            raise ValueError("mode 必须是三位或四位八进制权限")
        file_mode = int(requested_mode, 8)
    else:
        file_mode = stat.S_IMODE(path.stat().st_mode) if existed else 0o644
    backup = ""
    if existed:
        backup_dir = FILE_BACKUP_DIR / request_id
        backup_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(backup_dir, 0o700)
        backup_path = backup_dir / f"{hashlib.sha256(str(path).encode()).hexdigest()}.bak"
        shutil.copy2(path, backup_path, follow_symlinks=False)
        os.chmod(backup_path, 0o600)
        backup = str(backup_path)
    descriptor, temp_name = tempfile.mkstemp(prefix=f".{path.name}.prism-", dir=str(parent))
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp_name, file_mode)
        os.replace(temp_name, path)
        directory_fd = os.open(parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)
    return {
        "path": str(path),
        "created": not existed,
        "previous_sha256": current_sha,
        "sha256": _sha256_file(path),
        "bytes": len(payload),
        "mode": format(file_mode, "04o"),
        "rollback_backup": backup,
    }


def _package_action(params: dict[str, Any]) -> dict[str, Any]:
    operation = str(params.get("operation") or "")
    if operation not in {"install", "upgrade", "remove"}:
        raise ValueError("package operation 不合法")
    packages = params.get("packages")
    if not isinstance(packages, list) or not 1 <= len(packages) <= 20:
        raise ValueError("packages 数量必须在 1 到 20 之间")
    normalized = [str(item) for item in packages]
    if any(not PACKAGE_NAME.fullmatch(item) for item in normalized):
        raise ValueError("软件包名称不合法")
    manager = shutil.which("dnf")
    if not manager:
        raise RuntimeError("服务器未安装 dnf")
    return run([manager, "-y", operation, *normalized], timeout=1800)


def _firewall_action(params: dict[str, Any]) -> dict[str, Any]:
    operation = str(params.get("operation") or "")
    if operation not in {"add", "remove"}:
        raise ValueError("firewall operation 不合法")
    target_type = str(params.get("target_type") or "")
    if target_type not in {"port", "service"}:
        raise ValueError("target_type 必须是 port 或 service")
    value = str(params.get("value") or "")
    if target_type == "port":
        match = re.fullmatch(r"([0-9]{1,5})/(tcp|udp)", value)
        if not match or not 1 <= int(match.group(1)) <= 65535:
            raise ValueError("端口必须是 1-65535/tcp|udp")
    elif not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", value):
        raise ValueError("firewalld service 名称不合法")
    zone = str(params.get("zone") or "public")
    if not ZONE_NAME.fullmatch(zone):
        raise ValueError("firewalld zone 名称不合法")
    firewall = shutil.which("firewall-cmd")
    if not firewall:
        raise RuntimeError("服务器未安装 firewall-cmd")
    option = f"--{operation}-{target_type}={value}"
    change = run([firewall, "--permanent", f"--zone={zone}", option], timeout=120)
    reload_result = run([firewall, "--reload"], timeout=120)
    return {"change": change, "reload": reload_result}


def _account_action(params: dict[str, Any]) -> dict[str, Any]:
    operation = str(params.get("operation") or "")
    if operation not in {"create_system", "lock", "unlock", "delete"}:
        raise ValueError("account operation 不合法")
    username = str(params.get("username") or "")
    if not USERNAME.fullmatch(username):
        raise ValueError("用户名不合法")
    if username in PROTECTED_ACCOUNTS:
        raise ValueError("该系统账户受保护，不能由 Agent 修改")
    exists = _account_exists(username)
    if operation == "create_system":
        if exists:
            raise ValueError("账户已存在")
        shell = str(params.get("shell") or "/sbin/nologin")
        if shell not in {"/sbin/nologin", "/usr/sbin/nologin", "/bin/bash"}:
            raise ValueError("shell 不在允许范围")
        result = run(["useradd", "--system", "--create-home", "--shell", shell, username], timeout=120)
    else:
        if not exists:
            raise ValueError("账户不存在")
        if operation == "lock":
            result = run(["usermod", "--lock", username], timeout=60)
        elif operation == "unlock":
            result = run(["usermod", "--unlock", username], timeout=60)
        else:
            args = ["userdel"]
            if bool(params.get("remove_home")):
                args.append("--remove")
            result = run([*args, username], timeout=120)
    return {"operation": result, "account": _account_summary(username)}


def _ssh_authorized_key_action(params: dict[str, Any], *, request_id: str) -> dict[str, Any]:
    if not REQUEST_ID.fullmatch(request_id):
        raise ValueError("request_id 不合法")
    operation = str(params.get("operation") or "")
    if operation not in {"add", "remove"}:
        raise ValueError("SSH key operation 不合法")
    username = str(params.get("username") or "")
    if not USERNAME.fullmatch(username) or not _account_exists(username):
        raise ValueError("账户不存在或用户名不合法")
    account = pwd.getpwnam(username)
    home = _absolute_path(account.pw_dir)
    if not home.is_absolute() or home == Path("/"):
        raise ValueError("账户 home 目录不安全")
    ssh_dir = home / ".ssh"
    auth_file = ssh_dir / "authorized_keys"
    _reject_symlink_components(ssh_dir)
    _reject_symlink_components(auth_file)
    ssh_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(ssh_dir, 0o700)
    existing = auth_file.read_text(encoding="utf-8").splitlines() if auth_file.exists() else []
    backup = ""
    if auth_file.exists():
        backup_dir = FILE_BACKUP_DIR / request_id
        backup_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        backup_path = backup_dir / f"authorized_keys_{username}.bak"
        shutil.copy2(auth_file, backup_path, follow_symlinks=False)
        os.chmod(backup_path, 0o600)
        backup = str(backup_path)
    if operation == "add":
        key = str(params.get("public_key") or "").strip()
        fingerprint = _ssh_key_fingerprint(key)
        if any(
            _line_fingerprint(line) == fingerprint
            for line in existing
            if line.strip() and not line.lstrip().startswith("#")
        ):
            raise ValueError("该 SSH 公钥已存在")
        output = [*existing, key]
    else:
        fingerprint = str(params.get("fingerprint") or "")
        if not re.fullmatch(r"SHA256:[A-Za-z0-9+/]{20,64}", fingerprint):
            raise ValueError("SSH 公钥指纹不合法")
        output = [line for line in existing if _line_fingerprint(line) != fingerprint]
        if len(output) == len(existing):
            raise ValueError("未找到对应 SSH 公钥")
    payload = "\n".join(output).rstrip() + "\n"
    descriptor, temp_name = tempfile.mkstemp(prefix=".authorized_keys.prism-", dir=str(ssh_dir))
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp_name, 0o600)
        os.chown(temp_name, account.pw_uid, account.pw_gid)
        os.replace(temp_name, auth_file)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)
    os.chown(ssh_dir, account.pw_uid, account.pw_gid)
    return {"username": username, "operation": operation, "fingerprint": fingerprint, "rollback_backup": backup}


def _account_exists(username: str) -> bool:
    try:
        pwd.getpwnam(username)
        return True
    except KeyError:
        return False


def _account_summary(username: str) -> dict[str, Any]:
    if not _account_exists(username):
        return {"username": username, "exists": False}
    account = pwd.getpwnam(username)
    return {
        "username": username,
        "exists": True,
        "uid": account.pw_uid,
        "gid": account.pw_gid,
        "home": account.pw_dir,
        "shell": account.pw_shell,
    }


def _ssh_key_fingerprint(value: str) -> str:
    parts = value.split()
    if len(parts) < 2 or parts[0] not in SSH_KEY_TYPES:
        raise ValueError("只接受合法 OpenSSH 公钥")
    try:
        decoded = base64.b64decode(parts[1], validate=True)
    except (ValueError, TypeError) as exc:
        raise ValueError("SSH 公钥 Base64 不合法") from exc
    if len(decoded) < 32 or len(decoded) > 16 * 1024:
        raise ValueError("SSH 公钥长度不合法")
    digest = base64.b64encode(hashlib.sha256(decoded).digest()).decode("ascii").rstrip("=")
    return f"SHA256:{digest}"


def _line_fingerprint(value: str) -> str:
    try:
        return _ssh_key_fingerprint(value.strip())
    except ValueError:
        return ""


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
    sanitized = dict(params)
    if action == "write_text_file":
        content = str(sanitized.pop("content", ""))
        sanitized["content_bytes"] = len(content.encode("utf-8"))
        sanitized["content_sha256"] = hashlib.sha256(content.encode("utf-8")).hexdigest()
    if action == "ssh_authorized_key_action":
        public_key = str(sanitized.pop("public_key", ""))
        if public_key:
            sanitized["fingerprint"] = _ssh_key_fingerprint(public_key)
    return sanitized


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
        raise RuntimeError("执行幂等账本格式错误，已禁止重试")
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
    path = (BACKUP_DIR / name).resolve()
    if path.parent != BACKUP_DIR or not path.is_file():
        raise ValueError("备份文件不存在")
    return path


def _read_env(key: str) -> str:
    for line in (DEPLOY_DIR / ".env").read_text(encoding="utf-8").splitlines():
        if line.startswith(f"{key}="):
            return line.split("=", 1)[1].strip()
    raise RuntimeError(f"缺少配置 {key}")


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


class Handler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:  # noqa: N802
        try:
            if self.path != "/execute":
                self._json(404, {"ok": False, "error": "not found"})
                return
            expected = f"Bearer {TOKEN}"
            if not TOKEN or not hmac.compare_digest(self.headers.get("Authorization", ""), expected):
                self._json(401, {"ok": False, "error": "unauthorized"})
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
                self._json(409, {"ok": False, "error": "动作已开始但结果未确认，已禁止自动重试", "duplicate": True})
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
                result = execute(action, params, request_id=request_id)
                response = {"ok": True, "action": action, "result": result, "duplicate": False}
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
            except Exception as exc:
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


class UnixHTTPServer(socketserver.UnixStreamServer):
    allow_reuse_address = True


def main() -> None:
    if len(TOKEN) < 32:
        raise SystemExit("OPS_EXECUTOR_TOKEN must contain at least 32 characters")
    SOCKET_PATH.parent.mkdir(parents=True, exist_ok=True)
    if SOCKET_PATH.exists():
        SOCKET_PATH.unlink()
    server = UnixHTTPServer(str(SOCKET_PATH), Handler)
    os.chmod(SOCKET_PATH, 0o660)
    try:
        server.serve_forever()
    finally:
        server.server_close()
        if SOCKET_PATH.exists():
            SOCKET_PATH.unlink()


if __name__ == "__main__":
    main()
