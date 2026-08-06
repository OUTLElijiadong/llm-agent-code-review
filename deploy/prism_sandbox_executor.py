#!/usr/bin/env python3
"""Fail-closed container sandbox executor exposed only through a Unix socket.

Protocol v1 accepts source ZIP bytes and a fixed language/purpose. It never
accepts a command, image, Docker option, environment variable, mount or host
path from the caller.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import io
import ipaddress
import json
import os
import re
import shutil
import socketserver
import stat
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import zipfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler
from pathlib import Path, PurePosixPath
from typing import Any


DEPLOY_DIR = Path(__file__).resolve().parent
SOCKET_PATH = Path(os.environ.get("SANDBOX_EXECUTOR_SOCKET", "/var/lib/prism-sandbox/agent.sock"))
TOKEN = os.environ.get("SANDBOX_EXECUTOR_TOKEN", "")
EXECUTOR_MODE = os.environ.get("SANDBOX_EXECUTOR_MODE", "strict").strip().lower()
REQUIRED_RUNTIME = os.environ.get("SANDBOX_RUNTIME", "runsc").strip()
ALLOW_RUNC_LOCAL = os.environ.get("SANDBOX_ALLOW_RUNC_LOCAL_DEVELOPMENT", "false").lower() == "true"
PROFILE_FILE = Path(os.environ.get("SANDBOX_PROFILE_FILE", str(DEPLOY_DIR / "sandbox" / "profiles.json")))
STATE_DIR = Path(os.environ.get("SANDBOX_STATE_DIR", "/var/lib/prism-sandbox/state"))
JOB_DIR = Path(os.environ.get("SANDBOX_JOB_DIR", "/var/lib/prism-sandbox/jobs"))
AUDIT_LOG = Path(os.environ.get("SANDBOX_AUDIT_LOG", "/var/log/prism-sandbox/events.jsonl"))
BROWSER_IMAGE = os.environ.get("PLAYWRIGHT_IMAGE", "").strip()
BROWSER_IMAGE_DIGEST = os.environ.get("PLAYWRIGHT_IMAGE_DIGEST", "").strip()
BROWSER_TIMEOUT_SECONDS = int(os.environ.get("PLAYWRIGHT_TIMEOUT_SECONDS", "90"))
BROWSER_SCRIPT = DEPLOY_DIR / "sandbox" / "browser_blackbox.js"
BROWSER_PROXY_SCRIPT = DEPLOY_DIR / "sandbox" / "browser_target_proxy.py"
BROWSER_EXECUTABLE = "/ms-playwright/chromium-1232/chrome-linux64/chrome"

MAX_CONCURRENCY = int(os.environ.get("SANDBOX_MAX_CONCURRENCY", "1"))
MAX_ARCHIVE_BYTES = int(os.environ.get("SANDBOX_MAX_ARCHIVE_BYTES", str(32 * 1024 * 1024)))
MAX_EXPANDED_BYTES = int(os.environ.get("SANDBOX_MAX_EXPANDED_BYTES", str(128 * 1024 * 1024)))
MAX_OUTPUT_BYTES = int(os.environ.get("SANDBOX_MAX_OUTPUT_BYTES", str(2 * 1024 * 1024)))
DEFAULT_TTL_SECONDS = int(os.environ.get("SANDBOX_DEFAULT_TTL_SECONDS", str(72 * 60 * 60)))
MAX_TTL_SECONDS = int(os.environ.get("SANDBOX_MAX_TTL_SECONDS", str(7 * 24 * 60 * 60)))
MAX_REQUEST_BYTES = int(os.environ.get("SANDBOX_MAX_REQUEST_BYTES", str(MAX_ARCHIVE_BYTES * 2 + 64 * 1024)))
MAX_MEMBER_BYTES = int(os.environ.get("SANDBOX_MAX_MEMBER_BYTES", str(32 * 1024 * 1024)))
MAX_ARCHIVE_FILES = int(os.environ.get("SANDBOX_MAX_ARCHIVE_FILES", "10000"))
MAX_COMPRESSION_RATIO = int(os.environ.get("SANDBOX_MAX_COMPRESSION_RATIO", "200"))
MAX_PREVIEW_REQUEST_BYTES = 1024 * 1024
MAX_PREVIEW_RESPONSE_BYTES = 2 * 1024 * 1024
PREVIEW_PORT = 8080
PROTOCOL_VERSION = "1.0"

REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{7,63}$")
SHA256_RE = re.compile(r"^sha256:[a-f0-9]{64}$")
HEX_SHA256_RE = re.compile(r"^[a-f0-9]{64}$")
IMAGE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/@:-]{0,299}$")
LANGUAGES = frozenset({"python", "node", "java", "go", "php"})
PURPOSES = frozenset({"test", "deploy"})
ACTIVE_STATUSES = frozenset(
    {"validating", "preparing", "running_whitebox", "starting", "health_checking", "running", "stopping"}
)
TERMINAL_STATUSES = frozenset({"succeeded", "failed", "blocked", "stopped", "expired"})
EXECUTE_KEYS = frozenset(
    {
        "request_id",
        "purpose",
        "language",
        "test_mode",
        "source_archive_base64",
        "source_sha256",
        "ttl_seconds",
        "image_digest",
    }
)
STATUS_KEYS = frozenset({"request_id", "after_sequence"})
STOP_KEYS = frozenset({"request_id"})
EXTEND_KEYS = frozenset({"request_id", "extend_seconds"})
BROWSER_KEYS = frozenset({"request_id", "target_url", "target_ip"})
SAFE_RESPONSE_HEADERS = frozenset(
    {"cache-control", "content-disposition", "content-language", "content-type", "etag", "last-modified", "location"}
)

STATE_LOCK = threading.RLock()
STATE_CONDITION = threading.Condition(STATE_LOCK)
MONITOR_THREADS: dict[str, threading.Thread] = {}
PENDING_SUBMISSIONS: dict[str, str] = {}
JANITOR_STOP = threading.Event()
BROWSER_LOCK = threading.Lock()


class SandboxError(RuntimeError):
    """Base protocol error."""


class BlockedError(SandboxError):
    """The requested job cannot safely run on this worker."""


class ConflictError(SandboxError):
    """The request conflicts with existing state."""


class NotFoundError(SandboxError):
    """The requested job does not exist."""


class CapacityError(SandboxError):
    """The worker has reached its fixed concurrency limit."""


@dataclass(frozen=True)
class Profile:
    language: str
    image: str
    digest: str
    memory_mb: int
    cpus: float
    pids: int
    test_timeout_seconds: int
    startup_timeout_seconds: int
    workspace_mb: int
    fingerprint: str

    def resource_policy(self) -> dict[str, Any]:
        return {
            "network": "none",
            "read_only_root": True,
            "cap_drop": ["ALL"],
            "no_new_privileges": True,
            "uid": 65532,
            "gid": 65532,
            "memory_mb": self.memory_mb,
            "memory_swap_mb": self.memory_mb,
            "cpus": self.cpus,
            "pids": self.pids,
            "workspace_mb": self.workspace_mb,
            "tmp_mb": 64,
            "output_bytes": MAX_OUTPUT_BYTES,
            "test_timeout_seconds": self.test_timeout_seconds,
            "startup_timeout_seconds": self.startup_timeout_seconds,
            "preview_port": PREVIEW_PORT,
        }


@dataclass(frozen=True)
class ResolvedImage:
    configured_ref: str
    configured_digest: str
    local_id: str
    run_ref: str


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime | None = None) -> str:
    return (value or _utcnow()).isoformat()


def _parse_iso(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _redact(value: str) -> str:
    output = re.sub(
        r"(?i)(password|passwd|token|secret|api[_-]?key|authorization)(\s*[:=]\s*)([^\s,;]+)",
        r"\1\2[REDACTED]",
        value,
    )
    return re.sub(r"(?i)(Bearer\s+)[A-Za-z0-9._~+/-]{8,}", r"\1[REDACTED]", output)


def _check_exact_keys(payload: dict[str, Any], allowed: frozenset[str]) -> None:
    extra = set(payload) - allowed
    if extra:
        raise ValueError(f"请求包含未允许字段: {sorted(extra)}")


def _validate_request_id(value: Any) -> str:
    request_id = str(value or "")
    if not REQUEST_ID_RE.fullmatch(request_id):
        raise ValueError("request_id 不合法")
    return request_id


def _load_profiles() -> dict[str, Profile]:
    if PROFILE_FILE.is_symlink() or not PROFILE_FILE.is_file():
        raise BlockedError("沙箱 profile 文件不存在或路径不安全")
    try:
        document = json.loads(PROFILE_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BlockedError("沙箱 profile 文件无法读取") from exc
    if not isinstance(document, dict) or set(document) != {"schema_version", "profiles"}:
        raise BlockedError("沙箱 profile 顶层结构不合法")
    if document.get("schema_version") != 1 or not isinstance(document.get("profiles"), dict):
        raise BlockedError("不支持的沙箱 profile schema")
    raw_profiles = document["profiles"]
    if set(raw_profiles) != LANGUAGES:
        raise BlockedError("沙箱 profile 必须精确配置五种受支持语言")
    profiles: dict[str, Profile] = {}
    expected_keys = {
        "image",
        "digest",
        "memory_mb",
        "cpus",
        "pids",
        "test_timeout_seconds",
        "startup_timeout_seconds",
        "workspace_mb",
    }
    for language in sorted(LANGUAGES):
        raw = raw_profiles.get(language)
        if not isinstance(raw, dict) or set(raw) != expected_keys:
            raise BlockedError(f"{language} profile 字段不合法")
        image = str(raw["image"])
        digest = str(raw["digest"])
        if not IMAGE_RE.fullmatch(image) or image.startswith("-") or "://" in image:
            raise BlockedError(f"{language} 镜像引用不合法")
        if digest and not SHA256_RE.fullmatch(digest):
            raise BlockedError(f"{language} 镜像 digest 不合法")
        if "@sha256:" in image and not image.endswith(f"@{digest}"):
            raise BlockedError(f"{language} 镜像引用与 digest 不一致")
        try:
            memory_mb = int(raw["memory_mb"])
            cpus = float(raw["cpus"])
            pids = int(raw["pids"])
            test_timeout = int(raw["test_timeout_seconds"])
            startup_timeout = int(raw["startup_timeout_seconds"])
            workspace_mb = int(raw["workspace_mb"])
        except (TypeError, ValueError) as exc:
            raise BlockedError(f"{language} 资源参数类型不合法") from exc
        if not 128 <= memory_mb <= 2048 or not 0.1 <= cpus <= 2.0 or not 16 <= pids <= 512:
            raise BlockedError(f"{language} CPU、内存或 PID 限制不合法")
        if not 10 <= test_timeout <= 900 or not 5 <= startup_timeout <= 180:
            raise BlockedError(f"{language} 时间限制不合法")
        if not 64 <= workspace_mb <= 1024:
            raise BlockedError(f"{language} 工作区限制不合法")
        normalized = json.dumps({"language": language, **raw}, sort_keys=True, separators=(",", ":"))
        profiles[language] = Profile(
            language=language,
            image=image,
            digest=digest,
            memory_mb=memory_mb,
            cpus=cpus,
            pids=pids,
            test_timeout_seconds=test_timeout,
            startup_timeout_seconds=startup_timeout,
            workspace_mb=workspace_mb,
            fingerprint=hashlib.sha256(normalized.encode("utf-8")).hexdigest(),
        )
    return profiles


def _run_command(
    args: list[str], *, timeout: int = 60, allow_failure: bool = False, input_bytes: bytes | None = None,
    max_output_bytes: int = MAX_OUTPUT_BYTES,
) -> dict[str, Any]:
    try:
        completed = subprocess.run(
            args,
            input=input_bytes,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise TimeoutError(f"命令在 {timeout} 秒后超时") from exc
    stdout_raw = completed.stdout or b""
    stderr_raw = completed.stderr or b""
    total_bytes = len(stdout_raw) + len(stderr_raw)
    remaining = max_output_bytes
    stdout_kept = stdout_raw[:remaining]
    remaining -= len(stdout_kept)
    stderr_kept = stderr_raw[:remaining]
    result = {
        "exit_code": completed.returncode,
        "stdout": _redact(stdout_kept.decode("utf-8", errors="replace")),
        "stderr": _redact(stderr_kept.decode("utf-8", errors="replace")),
        "output_bytes": total_bytes,
        "output_truncated": total_bytes > max_output_bytes,
    }
    if completed.returncode != 0 and not allow_failure:
        detail = result["stderr"] or result["stdout"] or "无输出"
        raise RuntimeError(f"受控命令失败 exit={completed.returncode}: {detail}"[:4000])
    return result


def _docker_runtimes() -> set[str]:
    try:
        result = _run_command(["docker", "info", "--format", "{{json .Runtimes}}"], timeout=30)
    except Exception as exc:
        raise BlockedError("Docker runtime 自检失败") from exc
    try:
        parsed = json.loads(result["stdout"])
    except json.JSONDecodeError as exc:
        raise BlockedError("Docker 未返回有效 runtime 列表") from exc
    if not isinstance(parsed, dict):
        raise BlockedError("Docker runtime 列表格式不合法")
    return {str(key) for key in parsed}


def _resolve_runtime() -> str:
    if EXECUTOR_MODE not in {"strict", "local_development"}:
        raise BlockedError("SANDBOX_EXECUTOR_MODE 只能是 strict 或 local_development")
    if REQUIRED_RUNTIME != "runsc":
        raise BlockedError("严格沙箱只支持将 runsc 配置为首选 runtime")
    runtimes = _docker_runtimes()
    if "runsc" in runtimes:
        return "runsc"
    if EXECUTOR_MODE == "local_development" and ALLOW_RUNC_LOCAL and "runc" in runtimes:
        return "runc"
    raise BlockedError("runsc 未安装或未通过 Docker runtime 自检，已失败关闭")


def _resolve_image(profile: Profile, requested_digest: str = "") -> ResolvedImage:
    if requested_digest and not SHA256_RE.fullmatch(requested_digest):
        raise ValueError("image_digest 格式不合法")
    if EXECUTOR_MODE == "strict" and not profile.digest:
        raise BlockedError(f"{profile.language} 镜像未配置不可变 digest")
    if requested_digest and not hmac.compare_digest(requested_digest, profile.digest):
        raise ConflictError("请求绑定的 image_digest 与服务端 allowlist 不一致")
    try:
        result = _run_command(["docker", "image", "inspect", profile.image], timeout=30)
    except Exception as exc:
        raise BlockedError(f"{profile.language} 镜像不可用") from exc
    try:
        documents = json.loads(result["stdout"])
        document = documents[0]
        local_id = str(document["Id"])
        repo_digests = [str(item) for item in (document.get("RepoDigests") or [])]
    except (IndexError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise BlockedError(f"{profile.language} 镜像 inspect 结果不合法") from exc
    if not SHA256_RE.fullmatch(local_id):
        raise BlockedError(f"{profile.language} 镜像本地 ID 不合法")
    if profile.digest:
        digest_matches = local_id == profile.digest or any(item.endswith(f"@{profile.digest}") for item in repo_digests)
        if not digest_matches:
            raise BlockedError(f"{profile.language} 镜像 digest 校验失败")
        configured_digest = profile.digest
    else:
        if EXECUTOR_MODE != "local_development":
            raise BlockedError(f"{profile.language} 镜像缺少 digest")
        configured_digest = local_id
    return ResolvedImage(
        configured_ref=profile.image,
        configured_digest=configured_digest,
        local_id=local_id,
        run_ref=local_id,
    )


def _resolve_fixed_image(image_ref: str, digest: str, label: str) -> ResolvedImage:
    if not image_ref or not IMAGE_RE.fullmatch(image_ref):
        raise BlockedError(f"{label} 镜像未配置")
    if not SHA256_RE.fullmatch(digest):
        raise BlockedError(f"{label} 镜像缺少不可变 digest")
    try:
        result = _run_command(["docker", "image", "inspect", image_ref], timeout=30)
        documents = json.loads(result["stdout"])
        document = documents[0]
        local_id = str(document["Id"])
        repo_digests = [str(item) for item in (document.get("RepoDigests") or [])]
    except Exception as exc:
        raise BlockedError(f"{label} 镜像不可用") from exc
    if not SHA256_RE.fullmatch(local_id):
        raise BlockedError(f"{label} 镜像本地 ID 不合法")
    if local_id != digest and not any(item.endswith(f"@{digest}") for item in repo_digests):
        raise BlockedError(f"{label} 镜像 digest 校验失败")
    return ResolvedImage(
        configured_ref=image_ref,
        configured_digest=digest,
        local_id=local_id,
        run_ref=local_id,
    )


def _browser_self_test_script() -> str:
    return (
        "const {chromium}=require('/app/node_modules/playwright');"
        f"chromium.launch({{headless:true,executablePath:'{BROWSER_EXECUTABLE}',"
        "args:['--no-sandbox','--disable-dev-shm-usage','--disable-background-networking']})"
        ".then(async b=>{await b.close()}).catch(e=>{console.error(String(e));process.exit(2)})"
    )


def _browser_health(runtime: str, profiles: dict[str, Profile]) -> dict[str, Any]:
    policy = {
        "runtime": "runsc",
        "network": "private_browser_to_fixed_target_proxy",
        "dns": "disabled_in_browser_container",
        "target": "single_https_origin_and_pinned_public_ip",
        "read_only_root": True,
        "cap_drop": ["ALL"],
        "no_new_privileges": True,
        "memory_mb": 1024,
        "cpus": 1.0,
        "pids": 256,
        "timeout_seconds": BROWSER_TIMEOUT_SECONDS,
        "service_workers": "blocked",
        "downloads": "blocked",
    }
    try:
        if runtime != "runsc":
            raise BlockedError("Playwright 必须使用 runsc")
        if not 30 <= BROWSER_TIMEOUT_SECONDS <= 180:
            raise BlockedError("Playwright 超时配置必须在 30 到 180 秒之间")
        for path in (BROWSER_SCRIPT, BROWSER_PROXY_SCRIPT):
            current = path.lstat()
            if path.is_symlink() or not path.is_file() or current.st_mode & 0o022:
                raise BlockedError("Playwright 固定脚本不是只读普通文件")
        browser = _resolve_fixed_image(BROWSER_IMAGE, BROWSER_IMAGE_DIGEST, "Playwright")
        browser_self_test = _browser_self_test_script()
        _run_command([
            "docker", "run", "--rm", "--runtime", runtime, "--network", "none",
            "--read-only", "--cap-drop", "ALL", "--security-opt", "no-new-privileges:true",
            "--user", "1000:1000", "--pids-limit", "256", "--memory", "1024m",
            "--memory-swap", "1024m", "--cpus", "1.0", "--ipc", "none",
            "--tmpfs", "/tmp:rw,exec,nosuid,nodev,size=256m,mode=1777,uid=1000,gid=1000",
            "--tmpfs", "/home/node:rw,exec,nosuid,nodev,size=64m,mode=0700,uid=1000,gid=1000",
            "--env", "HOME=/home/node", "--env", "XDG_CONFIG_HOME=/tmp/.chromium",
            "--env", "XDG_CACHE_HOME=/tmp/.cache",
            "--entrypoint", "node", browser.run_ref, "-e", browser_self_test,
        ], timeout=60)
        python_profile = profiles.get("python")
        if python_profile is None:
            raise BlockedError("Playwright 目标代理缺少 Python 固定镜像")
        proxy = _resolve_image(python_profile)
        fingerprint = hashlib.sha256(
            json.dumps(
                {
                    "browser": browser.configured_digest,
                    "proxy": proxy.configured_digest,
                    "policy": policy,
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        return {
            "ready": True,
            "image_ref": browser.configured_ref,
            "image_digest": browser.configured_digest,
            "proxy_image_digest": proxy.configured_digest,
            "egress_policy_fingerprint": fingerprint,
            "resource_policy": policy,
        }
    except Exception as exc:  # noqa: BLE001 - health must explain the fail-closed reason
        return {
            "ready": False,
            "image_ref": BROWSER_IMAGE,
            "image_digest": BROWSER_IMAGE_DIGEST,
            "resource_policy": policy,
            "error": _redact(str(exc))[:1000],
        }


def _browser_names(request_id: str) -> tuple[str, str, str, str]:
    suffix = hashlib.sha256(request_id.encode("utf-8")).hexdigest()[:20]
    return (
        f"prism-bbx-browser-{suffix}",
        f"prism-bbx-proxy-{suffix}",
        f"prism-bbx-private-{suffix}",
        f"prism-bbx-egress-{suffix}",
    )


def _remove_browser_resources(names: tuple[str, str, str, str]) -> None:
    browser_name, proxy_name, private_network, egress_network = names
    for container in (browser_name, proxy_name):
        _run_command(["docker", "rm", "-f", container], timeout=30, allow_failure=True)
    for network in (private_network, egress_network):
        _run_command(["docker", "network", "rm", network], timeout=30, allow_failure=True)


def run_browser_blackbox(payload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("浏览器请求体必须是对象")
    _check_exact_keys(payload, BROWSER_KEYS)
    request_id = _validate_request_id(payload.get("request_id"))
    target_url = str(payload.get("target_url") or "")
    parsed = urllib.parse.urlsplit(target_url)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.fragment
        or len(target_url) > 2048
    ):
        raise ValueError("浏览器目标必须是不含凭据和片段的 HTTPS URL")
    try:
        target_port = parsed.port or 443
        target_ip = ipaddress.ip_address(str(payload.get("target_ip") or ""))
    except ValueError as exc:
        raise ValueError("浏览器目标端口或固定 IP 无效") from exc
    if target_ip.version != 4 or not target_ip.is_global:
        raise ValueError("浏览器目标必须固定到公网 IPv4")
    runtime = _resolve_runtime()
    profiles = _load_profiles()
    browser_health = _browser_health(runtime, profiles)
    if not browser_health.get("ready"):
        raise BlockedError(str(browser_health.get("error") or "Playwright profile 未就绪"))
    if not BROWSER_LOCK.acquire(blocking=False):
        raise CapacityError("Playwright worker 已达到最大并发")

    names = _browser_names(request_id)
    browser_name, proxy_name, private_network, egress_network = names
    try:
        _remove_browser_resources(names)
        _run_command(["docker", "network", "create", "--driver", "bridge", egress_network], timeout=30)
        _run_command(
            ["docker", "network", "create", "--driver", "bridge", "--internal", private_network],
            timeout=30,
        )
        proxy_image = _resolve_image(profiles["python"])
        _run_command([
            "docker", "create", "--name", proxy_name,
            "--label", "prism.browser_blackbox=true",
            "--runtime", runtime, "--network", egress_network,
            "--read-only", "--cap-drop", "ALL", "--security-opt", "no-new-privileges:true",
            "--user", "65532:65532", "--pids-limit", "64", "--memory", "96m",
            "--memory-swap", "96m", "--cpus", "0.25", "--restart", "no", "--init",
            "--tmpfs", "/tmp:rw,noexec,nosuid,nodev,size=16m,mode=1777",
            "--mount", f"type=bind,src={BROWSER_PROXY_SCRIPT},dst=/opt/prism/browser_target_proxy.py,readonly,bind-propagation=rprivate",
            "--env", f"PRISM_TARGET_HOST={parsed.hostname.rstrip('.').casefold()}",
            "--env", f"PRISM_TARGET_PORT={target_port}", "--env", f"PRISM_TARGET_IP={target_ip}",
            "--entrypoint", "python", proxy_image.run_ref, "/opt/prism/browser_target_proxy.py",
        ], timeout=60)
        _run_command(["docker", "network", "connect", "--alias", "target-proxy", private_network, proxy_name], timeout=30)
        _run_command(["docker", "start", proxy_name], timeout=30)
        proxy_address_result = _run_command([
            "docker", "inspect", "--format",
            f'{{{{with index .NetworkSettings.Networks "{private_network}"}}}}{{{{.IPAddress}}}}{{{{end}}}}',
            proxy_name,
        ], timeout=30)
        try:
            proxy_address = ipaddress.ip_address(proxy_address_result["stdout"].strip())
        except ValueError as exc:
            raise RuntimeError("Playwright 固定代理私网地址无效") from exc
        if proxy_address.version != 4 or not proxy_address.is_private:
            raise RuntimeError("Playwright 固定代理没有获得私有 IPv4 地址")

        browser_image = _resolve_fixed_image(BROWSER_IMAGE, BROWSER_IMAGE_DIGEST, "Playwright")
        _run_command([
            "docker", "create", "--name", browser_name,
            "--label", "prism.browser_blackbox=true", "--label", f"prism.browser_blackbox.request_id={request_id}",
            "--runtime", runtime, "--network", private_network,
            "--dns", "127.0.0.1", "--add-host", f"target-proxy:{proxy_address}",
            "--read-only", "--cap-drop", "ALL", "--security-opt", "no-new-privileges:true",
            "--user", "1000:1000", "--pids-limit", "256", "--memory", "1024m",
            "--memory-swap", "1024m", "--cpus", "1.0", "--ipc", "none", "--restart", "no", "--init",
            "--tmpfs", "/tmp:rw,exec,nosuid,nodev,size=256m,mode=1777,uid=1000,gid=1000",
            "--tmpfs", "/home/node:rw,exec,nosuid,nodev,size=64m,mode=0700,uid=1000,gid=1000",
            "--mount", f"type=bind,src={BROWSER_SCRIPT},dst=/opt/prism/browser_blackbox.js,readonly,bind-propagation=rprivate",
            "--env", f"PRISM_TARGET_URL={target_url}", "--env", "PRISM_PROXY_SERVER=http://target-proxy:3128",
            "--env", f"PRISM_BROWSER_TIMEOUT_MS={BROWSER_TIMEOUT_SECONDS * 1000}",
            "--env", "HOME=/home/node", "--env", "XDG_CONFIG_HOME=/tmp/.chromium",
            "--env", "XDG_CACHE_HOME=/tmp/.cache", "--env", "NO_PROXY=",
            "--env", "HTTP_PROXY=", "--env", "HTTPS_PROXY=",
            "--entrypoint", "node", browser_image.run_ref, "/opt/prism/browser_blackbox.js",
        ], timeout=60)
        time.sleep(0.25)
        output = _run_command(
            ["docker", "start", "--attach", browser_name],
            timeout=BROWSER_TIMEOUT_SECONDS + 30,
            allow_failure=True,
            max_output_bytes=5 * 1024 * 1024,
        )
        lines = [line for line in output["stdout"].splitlines() if line.strip()]
        if not lines:
            raise RuntimeError(f"Playwright 未返回结构化证据: {output['stderr'][:500]}")
        try:
            result = json.loads(lines[-1])
        except json.JSONDecodeError as exc:
            raise RuntimeError("Playwright 结构化证据格式无效") from exc
        if not isinstance(result, dict) or result.get("protocol_version") != "1.0":
            raise RuntimeError("Playwright 结构化证据协议不匹配")
        result.update({
            "request_id": request_id,
            "runtime": runtime,
            "image_ref": browser_health["image_ref"],
            "image_digest": browser_health["image_digest"],
            "resolved_ip": str(target_ip),
            "egress_policy_fingerprint": browser_health["egress_policy_fingerprint"],
            "resource_policy": browser_health["resource_policy"],
            "output_truncated": output["output_truncated"],
        })
        _append_audit({
            "request_id": request_id,
            "event_type": "browser_blackbox",
            "timestamp": _iso(),
            "target_origin": f"https://{parsed.hostname}:{target_port}",
            "resolved_ip": str(target_ip),
            "passed": bool(result.get("passed")),
        })
        return result
    finally:
        _remove_browser_resources(names)
        BROWSER_LOCK.release()


def _ensure_directory(path: Path, mode: int) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=mode)
    if path.is_symlink() or not path.is_dir():
        raise RuntimeError(f"目录路径不安全: {path}")
    os.chmod(path, mode)


def _state_path(request_id: str) -> Path:
    safe_id = _validate_request_id(request_id)
    return STATE_DIR / f"{safe_id}.json"


def _job_path(request_id: str) -> Path:
    safe_id = _validate_request_id(request_id)
    suffix = hashlib.sha256(safe_id.encode("utf-8")).hexdigest()[:32]
    return JOB_DIR / f"job-{suffix}"


def _container_name(request_id: str) -> str:
    suffix = hashlib.sha256(request_id.encode("utf-8")).hexdigest()[:24]
    return f"prism-sandbox-{suffix}"


def _read_state(request_id: str) -> dict[str, Any]:
    path = _state_path(request_id)
    if not path.exists():
        raise NotFoundError("沙箱任务不存在")
    if path.is_symlink() or not path.is_file():
        raise RuntimeError("沙箱状态路径不安全")
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError("沙箱状态损坏，已失败关闭") from exc
    if not isinstance(state, dict) or state.get("request_id") != request_id:
        raise RuntimeError("沙箱状态内容不合法，已失败关闭")
    return state


def _write_state(state: dict[str, Any]) -> None:
    request_id = _validate_request_id(state.get("request_id"))
    _ensure_directory(STATE_DIR, 0o700)
    target = _state_path(request_id)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{request_id}.", dir=str(STATE_DIR))
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(state, handle, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, target)
        directory_fd = os.open(STATE_DIR, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _append_audit(event: dict[str, Any]) -> None:
    try:
        _ensure_directory(AUDIT_LOG.parent, 0o700)
        descriptor = os.open(AUDIT_LOG, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            payload = json.dumps(event, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
            os.write(descriptor, payload.encode("utf-8"))
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except Exception as exc:  # noqa: BLE001 - state ledger remains authoritative
        sys.stderr.write(f"prism-sandbox audit failure: {_redact(str(exc))[:500]}\n")


def _add_event(
    state: dict[str, Any], event_type: str, stage: str, message: str, payload: dict[str, Any] | None = None,
) -> None:
    events = state.setdefault("events", [])
    sequence = int(events[-1]["sequence"]) + 1 if events else 1
    event = {
        "sequence": sequence,
        "timestamp": _iso(),
        "event_type": event_type,
        "stage": stage,
        "message": _redact(message)[:500],
        "payload": payload or {},
    }
    events.append(event)
    if len(events) > 500:
        state["events"] = events[-500:]
    state["stage"] = stage
    state["updated_at"] = event["timestamp"]
    _append_audit({"request_id": state["request_id"], **event})


def _transition(
    state: dict[str, Any], status_value: str, stage: str, message: str, *, event_type: str = "lifecycle",
    payload: dict[str, Any] | None = None,
) -> None:
    state["status"] = status_value
    _add_event(state, event_type, stage, message, payload)
    _write_state(state)


def _validate_archive_envelope(payload: dict[str, Any]) -> None:
    encoded = payload.get("source_archive_base64")
    if not isinstance(encoded, str) or not encoded:
        raise ValueError("source_archive_base64 不能为空")
    if len(encoded) > (MAX_ARCHIVE_BYTES * 4 // 3) + 16:
        raise ValueError("源码压缩包超过允许大小")
    expected = str(payload.get("source_sha256") or "").lower()
    if not HEX_SHA256_RE.fullmatch(expected):
        raise ValueError("source_sha256 必须是 64 位小写十六进制")


def _decode_archive(payload: dict[str, Any]) -> bytes:
    _validate_archive_envelope(payload)
    encoded = payload["source_archive_base64"]
    try:
        archive = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("源码压缩包 Base64 不合法") from exc
    if not archive or len(archive) > MAX_ARCHIVE_BYTES:
        raise ValueError("源码压缩包大小不合法")
    expected = str(payload["source_sha256"]).lower()
    actual = hashlib.sha256(archive).hexdigest()
    if not hmac.compare_digest(actual, expected):
        raise ValueError("源码压缩包 SHA-256 校验失败")
    return archive


def _safe_zip_parts(name: str) -> tuple[str, ...]:
    if (
        not name
        or "\x00" in name
        or "\\" in name
        or name.startswith("/")
        or any(ord(character) < 32 or ord(character) == 127 for character in name)
    ):
        raise ValueError("压缩包成员路径不合法")
    if len(name.encode("utf-8")) > 2048:
        raise ValueError("压缩包成员路径过长")
    path = PurePosixPath(name)
    parts = path.parts
    if not parts or any(part in {"", ".", ".."} for part in parts):
        raise ValueError("压缩包成员路径包含越界段")
    if re.fullmatch(r"[A-Za-z]:", parts[0]) or any(":" in part for part in parts):
        raise ValueError("压缩包成员路径包含设备或盘符")
    if any(len(part.encode("utf-8")) > 255 for part in parts):
        raise ValueError("压缩包成员名称过长")
    return parts


def _validate_zip_info(info: zipfile.ZipInfo) -> tuple[tuple[str, ...], bool, bool]:
    parts = _safe_zip_parts(info.filename)
    if info.flag_bits & 0x1:
        raise ValueError("不接受加密压缩包成员")
    unix_mode = (info.external_attr >> 16) & 0xFFFF
    file_type = stat.S_IFMT(unix_mode)
    is_directory = info.is_dir() or file_type == stat.S_IFDIR
    if file_type not in {0, stat.S_IFREG, stat.S_IFDIR}:
        raise ValueError("压缩包包含符号链接或特殊文件")
    if info.file_size < 0 or info.file_size > MAX_MEMBER_BYTES:
        raise BlockedError("压缩包单文件超过沙箱执行安全限制")
    compressed_size = max(1, info.compress_size)
    if info.file_size > 1024 * 1024 and info.file_size / compressed_size > MAX_COMPRESSION_RATIO:
        raise BlockedError("压缩包成员压缩比超过沙箱执行安全限制")
    executable = bool(unix_mode & 0o111)
    return parts, is_directory, executable


def _extract_archive(archive: bytes, request_id: str) -> Path:
    _ensure_directory(JOB_DIR, 0o700)
    job_root = _job_path(request_id)
    if job_root.exists() or job_root.is_symlink():
        raise ConflictError("沙箱任务工作目录已存在")
    temporary = Path(tempfile.mkdtemp(prefix=".incoming-", dir=str(JOB_DIR)))
    source_root = temporary / "source"
    source_root.mkdir(mode=0o700)
    total_size = 0
    seen: set[str] = set()
    try:
        with zipfile.ZipFile(io.BytesIO(archive)) as bundle:
            infos = bundle.infolist()
            if not infos or len(infos) > MAX_ARCHIVE_FILES:
                raise ValueError("压缩包文件数量不合法")
            for info in infos:
                parts, is_directory, executable = _validate_zip_info(info)
                normalized = "/".join(parts)
                if normalized.casefold() in seen:
                    raise ValueError("压缩包包含重复或大小写冲突路径")
                seen.add(normalized.casefold())
                total_size += info.file_size
                if total_size > MAX_EXPANDED_BYTES:
                    raise ValueError("压缩包解压后总体积超过限制")
                target = source_root.joinpath(*parts)
                target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                if is_directory:
                    target.mkdir(parents=True, exist_ok=True, mode=0o700)
                    continue
                written = 0
                flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
                if hasattr(os, "O_NOFOLLOW"):
                    flags |= os.O_NOFOLLOW
                descriptor = os.open(target, flags, 0o600)
                try:
                    with os.fdopen(descriptor, "wb") as destination, bundle.open(info, "r") as source:
                        while True:
                            chunk = source.read(1024 * 1024)
                            if not chunk:
                                break
                            written += len(chunk)
                            if written > info.file_size or written > MAX_MEMBER_BYTES:
                                raise ValueError("压缩包成员实际大小超过声明")
                            destination.write(chunk)
                    if written != info.file_size:
                        raise ValueError("压缩包成员大小与声明不一致")
                except Exception:
                    target.unlink(missing_ok=True)
                    raise
                os.chmod(target, 0o555 if executable else 0o444)
        for directory, subdirectories, _files in os.walk(source_root, topdown=False):
            for name in subdirectories:
                os.chmod(Path(directory) / name, 0o555)
            os.chmod(directory, 0o555)
        os.replace(temporary, job_root)
        return job_root / "source"
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
def _remove_job_data(request_id: str) -> None:
    root = _job_path(request_id)
    if root.parent != JOB_DIR or not root.name.startswith("job-"):
        raise RuntimeError("拒绝清理不安全的沙箱目录")
    if root.is_symlink():
        raise RuntimeError("拒绝清理符号链接沙箱目录")
    if root.exists():
        for directory, subdirectories, files in os.walk(root, topdown=False, followlinks=False):
            for name in files:
                path = Path(directory) / name
                if path.is_symlink():
                    path.unlink()
                else:
                    os.chmod(path, 0o600)
            for name in subdirectories:
                path = Path(directory) / name
                if path.is_symlink():
                    path.unlink()
                else:
                    os.chmod(path, 0o700)
            os.chmod(directory, 0o700)
        shutil.rmtree(root)


def _raise_if_stopped(request_id: str) -> None:
    with STATE_LOCK:
        state = _read_state(request_id)
        if state.get("stop_requested") or state.get("status") in TERMINAL_STATUSES:
            raise ConflictError("沙箱请求已关闭")


def _build_docker_create_args(
    *, request_id: str, purpose: str, test_mode: str, profile: Profile, runtime: str, image: ResolvedImage, source_dir: Path,
) -> list[str]:
    if purpose not in PURPOSES or profile.language not in LANGUAGES:
        raise ValueError("沙箱用途或语言不合法")
    expected_source = _job_path(request_id) / "source"
    if source_dir != expected_source or source_dir.is_symlink() or not source_dir.is_dir():
        raise ValueError("源码目录不是执行器生成的内部路径")
    container = _container_name(request_id)
    return [
        "docker",
        "create",
        "--name",
        container,
        "--label",
        "prism.sandbox=true",
        "--label",
        f"prism.sandbox.request_id={request_id}",
        "--runtime",
        runtime,
        "--network",
        "none",
        "--read-only",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges:true",
        "--user",
        "65532:65532",
        "--pids-limit",
        str(profile.pids),
        "--memory",
        f"{profile.memory_mb}m",
        "--memory-swap",
        f"{profile.memory_mb}m",
        "--cpus",
        f"{profile.cpus:g}",
        "--ulimit",
        "nofile=256:256",
        "--ulimit",
        f"nproc={profile.pids}:{profile.pids}",
        "--ipc",
        "none",
        "--hostname",
        "prism-sandbox",
        "--restart",
        "no",
        "--stop-timeout",
        "5",
        "--init",
        "--log-driver",
        "local",
        "--log-opt",
        "max-size=1m",
        "--log-opt",
        "max-file=2",
        "--tmpfs",
        "/tmp:rw,noexec,nosuid,nodev,size=64m,mode=1777",
        "--tmpfs",
        f"/workspace:rw,exec,nosuid,nodev,size={profile.workspace_mb}m,mode=1777,uid=65532,gid=65532",
        "--mount",
        f"type=bind,src={source_dir},dst=/source,readonly,bind-propagation=rprivate",
        "--env",
        f"PRISM_ACTION={purpose}",
        "--env",
        f"PRISM_LANGUAGE={profile.language}",
        "--env",
        f"PRISM_TEST_MODE={test_mode}",
        "--env",
        f"PRISM_PREVIEW_PORT={PREVIEW_PORT}",
        "--env",
        "HOME=/tmp",
        "--env",
        "CI=true",
        "--env",
        "LANG=C.UTF-8",
        "--env",
        "TMPDIR=/tmp",
        "--env",
        "NO_PROXY=127.0.0.1,localhost",
        "--env",
        "HTTP_PROXY=",
        "--env",
        "HTTPS_PROXY=",
        image.run_ref,
    ]


def _active_job_count() -> int:
    if not STATE_DIR.is_dir() or STATE_DIR.is_symlink():
        return 0
    active = 0
    for path in STATE_DIR.glob("*.json"):
        if path.is_symlink() or not path.is_file():
            continue
        try:
            state = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(state, dict) and state.get("status") in ACTIVE_STATUSES:
            active += 1
    return active


def _pending_submission_count() -> int:
    with STATE_LOCK:
        return len(PENDING_SUBMISSIONS)


def _request_digest(payload: dict[str, Any]) -> str:
    normalized = {
        key: payload.get(key)
        for key in sorted(EXECUTE_KEYS - {"source_archive_base64"})
        if key in payload
    }
    raw = json.dumps(normalized, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _new_state(payload: dict[str, Any], profile: Profile, digest: str) -> dict[str, Any]:
    now = _utcnow()
    ttl_seconds = int(payload.get("ttl_seconds") or DEFAULT_TTL_SECONDS)
    if ttl_seconds < 60 or ttl_seconds > MAX_TTL_SECONDS:
        raise ValueError(f"ttl_seconds 必须在 60 到 {MAX_TTL_SECONDS} 之间")
    return {
        "request_id": payload["request_id"],
        "request_digest": digest,
        "purpose": payload["purpose"],
        "language": payload["language"],
        "test_mode": payload.get("test_mode", "whitebox"),
        "source_sha256": payload["source_sha256"],
        "status": "validating",
        "stage": "validating",
        "runtime": "",
        "image_ref": profile.image,
        "image_digest": "",
        "image_local_id": "",
        "profile_fingerprint": profile.fingerprint,
        "resource_policy": profile.resource_policy(),
        "container_name": "",
        "container_id": "",
        "created_at": _iso(now),
        "updated_at": _iso(now),
        "started_at": None,
        "finished_at": None,
        "expires_at": _iso(now + timedelta(seconds=ttl_seconds)),
        "events": [],
        "result": None,
        "error": "",
        "stop_requested": False,
    }


def _normalize_execute(payload: dict[str, Any]) -> tuple[dict[str, Any], Profile]:
    if not isinstance(payload, dict):
        raise ValueError("请求体必须是 JSON 对象")
    _check_exact_keys(payload, EXECUTE_KEYS)
    request_id = _validate_request_id(payload.get("request_id"))
    purpose = str(payload.get("purpose") or "")
    language = str(payload.get("language") or "").lower()
    test_mode = str(payload.get("test_mode") or "whitebox")
    if purpose not in PURPOSES:
        raise ValueError("purpose 只能是 test 或 deploy")
    if language not in LANGUAGES:
        raise ValueError("language 不在固定 profile 白名单")
    allowed_modes = {"whitebox"} if purpose == "deploy" else {"whitebox", "blackbox", "combined"}
    if test_mode not in allowed_modes:
        raise ValueError("当前 purpose 不支持该固定测试模式")
    normalized = dict(payload)
    normalized.update({"request_id": request_id, "purpose": purpose, "language": language, "test_mode": test_mode})
    profiles = _load_profiles()
    _validate_archive_envelope(normalized)
    return normalized, profiles[language]


def _validate_execute(payload: dict[str, Any]) -> tuple[dict[str, Any], bytes, Profile]:
    normalized, profile = _normalize_execute(payload)
    archive = _decode_archive(normalized)
    return normalized, archive, profile


def _public_state(state: dict[str, Any], *, after_sequence: int = 0) -> dict[str, Any]:
    events = [event for event in state.get("events", []) if int(event.get("sequence", 0)) > after_sequence]
    preview_supported = state.get("purpose") == "deploy" and state.get("status") == "running"
    return {
        "request_id": state["request_id"],
        "purpose": state["purpose"],
        "language": state["language"],
        "test_mode": state["test_mode"],
        "status": state["status"],
        "stage": state["stage"],
        "runtime": state.get("runtime", ""),
        "image_ref": state.get("image_ref", ""),
        "image_digest": state.get("image_digest", ""),
        "profile_fingerprint": state.get("profile_fingerprint", ""),
        "source_sha256": state["source_sha256"],
        "resource_policy": state.get("resource_policy", {}),
        "executor_ref": state.get("container_id", ""),
        "created_at": state.get("created_at"),
        "updated_at": state.get("updated_at"),
        "started_at": state.get("started_at"),
        "finished_at": state.get("finished_at"),
        "expires_at": state.get("expires_at"),
        "preview_supported": preview_supported,
        "preview_path": f"/preview/{state['request_id']}/" if preview_supported else None,
        "events": events,
        "last_sequence": int(state.get("events", [{}])[-1].get("sequence", 0)) if state.get("events") else 0,
        "result": state.get("result"),
        "error": state.get("error", ""),
    }


def submit_job(payload: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    normalized, profile = _normalize_execute(payload)
    request_id = normalized["request_id"]
    digest = _request_digest(normalized)
    with STATE_CONDITION:
        while True:
            try:
                existing = _read_state(request_id)
            except NotFoundError:
                existing = None
            if existing is not None:
                if not hmac.compare_digest(str(existing.get("request_digest") or ""), digest):
                    raise ConflictError("request_id 已绑定其他沙箱请求")
                return _public_state(existing), True
            pending_digest = PENDING_SUBMISSIONS.get(request_id)
            if pending_digest is not None:
                if not hmac.compare_digest(pending_digest, digest):
                    raise ConflictError("request_id 已绑定其他沙箱请求")
                STATE_CONDITION.wait()
                continue
            if _active_job_count() + len(PENDING_SUBMISSIONS) >= MAX_CONCURRENCY:
                raise CapacityError("沙箱 worker 已达到最大并发")
            PENDING_SUBMISSIONS[request_id] = digest
            break

    try:
        # Keep the reservation while decoding so another large submission cannot
        # bypass the fixed worker capacity during this memory-heavy operation.
        archive = _decode_archive(normalized)
        state = _new_state(normalized, profile, digest)
        with STATE_CONDITION:
            _add_event(state, "lifecycle", "validating", "已接收固定 profile 沙箱请求")
            _write_state(state)
            PENDING_SUBMISSIONS.pop(request_id, None)
            STATE_CONDITION.notify_all()
    except Exception:
        with STATE_CONDITION:
            PENDING_SUBMISSIONS.pop(request_id, None)
            STATE_CONDITION.notify_all()
        raise

    try:
        runtime = _resolve_runtime()
        image = _resolve_image(profile, str(normalized.get("image_digest") or ""))
        with STATE_LOCK:
            state = _read_state(request_id)
            state["runtime"] = runtime
            state["image_digest"] = image.configured_digest
            state["image_local_id"] = image.local_id
            _transition(
                state,
                "preparing",
                "preparing",
                "运行时、镜像 allowlist 与 digest 校验通过",
                payload={"runtime": runtime, "image_digest": image.configured_digest},
            )
        source_dir = _extract_archive(archive, request_id)
        _raise_if_stopped(request_id)
        args = _build_docker_create_args(
            request_id=request_id,
            purpose=normalized["purpose"],
            test_mode=normalized["test_mode"],
            profile=profile,
            runtime=runtime,
            image=image,
            source_dir=source_dir,
        )
        created = _run_command(args, timeout=60)
        container_id = created["stdout"].strip()
        if not re.fullmatch(r"[a-f0-9]{12,64}", container_id):
            raise RuntimeError("Docker create 未返回合法容器 ID")
        _raise_if_stopped(request_id)
        _run_command(["docker", "start", _container_name(request_id)], timeout=30)
        with STATE_LOCK:
            state = _read_state(request_id)
            if state.get("stop_requested") or state.get("status") in TERMINAL_STATUSES:
                raise ConflictError("沙箱请求已关闭")
            state["container_name"] = _container_name(request_id)
            state["container_id"] = container_id
            state["started_at"] = _iso()
            if normalized["purpose"] == "test":
                _transition(state, "running_whitebox", "running_whitebox", "白盒测试容器已启动")
            else:
                _transition(state, "starting", "starting", "持续部署容器已启动，等待回环健康检查")
        _start_monitor(request_id, profile)
    except BlockedError as exc:
        _finish_submission_error(request_id, "blocked", "blocked", str(exc))
    except Exception as exc:  # noqa: BLE001 - persist a terminal result for every accepted request
        _finish_submission_error(request_id, "failed", "failed", str(exc))
    with STATE_LOCK:
        return _public_state(_read_state(request_id)), False


def _finish_submission_error(request_id: str, status_value: str, stage: str, error: str) -> None:
    redacted = _redact(error)[:3500]
    _queue_terminal_cleanup(
        request_id,
        status_value=status_value,
        stage=stage,
        message=redacted,
        error=redacted,
        result={"outcome": status_value, "exit_code": None, "timed_out": False},
    )


def _queue_terminal_cleanup(
    request_id: str,
    *,
    status_value: str,
    stage: str,
    message: str,
    error: str,
    result: dict[str, Any],
) -> bool:
    if status_value not in TERMINAL_STATUSES:
        raise ValueError("资源回收后的目标状态必须是终态")
    with STATE_LOCK:
        state = _read_state(request_id)
        if state.get("status") in TERMINAL_STATUSES:
            return True
        if not isinstance(state.get("pending_terminal"), dict):
            state["pending_terminal"] = {
                "status": status_value,
                "stage": stage,
                "message": _redact(message)[:500],
                "error": _redact(error)[:3500],
                "result": result,
            }
            state["stop_requested"] = True
            state["result"] = result
            state["error"] = _redact(error)[:3500]
            _transition(state, "stopping", "stopping", "正在回收沙箱容器与源码目录")
    return _retry_pending_cleanup(request_id)


def _retry_pending_cleanup(request_id: str) -> bool:
    with STATE_LOCK:
        state = _read_state(request_id)
        if state.get("status") in TERMINAL_STATUSES:
            return True
        pending = state.get("pending_terminal")
        if not isinstance(pending, dict):
            raise RuntimeError("stopping 状态缺少待提交终态")

    cleanup_errors: list[str] = []
    try:
        _remove_container(_container_name(request_id))
    except Exception as exc:  # noqa: BLE001
        cleanup_errors.append(f"容器: {_redact(str(exc))[:500]}")
    try:
        _remove_job_data(request_id)
    except Exception as exc:  # noqa: BLE001
        cleanup_errors.append(f"源码: {_redact(str(exc))[:500]}")

    with STATE_LOCK:
        state = _read_state(request_id)
        if state.get("status") in TERMINAL_STATUSES:
            return True
        pending = state.get("pending_terminal")
        if not isinstance(pending, dict):
            raise RuntimeError("资源回收过程中待提交终态丢失")
        if cleanup_errors:
            cleanup_error = "; ".join(cleanup_errors)[:1500]
            result_value = dict(pending.get("result") or {})
            result_value["cleanup_error"] = cleanup_error
            state["result"] = result_value
            base_error = str(pending.get("error") or "")
            state["error"] = f"{base_error}; 资源回收待重试: {cleanup_error}".strip("; ")[:4000]
            _add_event(state, "cleanup_retry", "stopping", "资源回收未完成，janitor 将继续重试")
            _write_state(state)
            return False

        result_value = dict(pending.get("result") or {})
        result_value.pop("cleanup_error", None)
        state["result"] = result_value
        state["error"] = str(pending.get("error") or "")[:3500]
        state["finished_at"] = _iso()
        state.pop("pending_terminal", None)
        _transition(
            state,
            str(pending["status"]),
            str(pending["stage"]),
            str(pending["message"]),
            event_type="result",
        )
        return True


def _start_monitor(request_id: str, profile: Profile) -> None:
    existing = MONITOR_THREADS.get(request_id)
    if existing is not None and existing.is_alive():
        return
    target = _monitor_test if _read_state(request_id)["purpose"] == "test" else _monitor_deployment
    thread = threading.Thread(
        target=target,
        args=(request_id, profile),
        name=f"sandbox-{request_id}",
        daemon=True,
    )
    MONITOR_THREADS[request_id] = thread
    thread.start()


def _recover_jobs() -> None:
    """Reconcile persisted jobs after executor restart without recreating work."""
    try:
        profiles = _load_profiles()
    except Exception as exc:  # noqa: BLE001
        sys.stderr.write(f"prism-sandbox recovery blocked: {_redact(str(exc))[:500]}\n")
        return
    if not STATE_DIR.is_dir() or STATE_DIR.is_symlink():
        return
    for path in sorted(STATE_DIR.glob("*.json")):
        if path.is_symlink() or not path.is_file():
            continue
        try:
            state = _read_state(path.stem)
            status_value = str(state.get("status") or "")
            if status_value not in ACTIVE_STATUSES:
                continue
            request_id = _validate_request_id(state.get("request_id"))
            if status_value == "stopping":
                if not isinstance(state.get("pending_terminal"), dict):
                    _finish_submission_error(
                        request_id,
                        "failed",
                        "failed",
                        "执行器重启时发现缺少终态信息的回收任务",
                    )
                else:
                    _retry_pending_cleanup(request_id)
                continue
            existing = MONITOR_THREADS.get(request_id)
            if existing is not None and existing.is_alive():
                continue
            if status_value in {"validating", "preparing"}:
                raise RuntimeError(f"执行器重启时任务停留在不可恢复阶段 {status_value}")
            if state.get("purpose") == "deploy" and _utcnow() >= _parse_iso(state["expires_at"]):
                _expire_job(request_id)
                continue
            container = _container_name(request_id)
            inspected = _inspect_container(container)
            if not inspected.get("Running"):
                raise RuntimeError("执行器重启后发现沙箱容器不在运行")
            profile = profiles.get(str(state.get("language") or ""))
            if profile is None or profile.fingerprint != state.get("profile_fingerprint"):
                raise BlockedError("执行器重启后 profile 已变化，拒绝恢复旧沙箱")
            _start_monitor(request_id, profile)
        except Exception as exc:  # noqa: BLE001
            try:
                _finish_submission_error(path.stem, "failed", "failed", f"任务恢复失败: {exc}")
            except Exception as nested:  # noqa: BLE001
                sys.stderr.write(f"prism-sandbox recovery failure: {_redact(str(nested))[:500]}\n")


def _janitor_loop() -> None:
    while not JANITOR_STOP.wait(30):
        try:
            _recover_jobs()
        except Exception as exc:  # noqa: BLE001
            sys.stderr.write(f"prism-sandbox janitor failure: {_redact(str(exc))[:500]}\n")


def _inspect_container(container: str) -> dict[str, Any]:
    result = _run_command(
        ["docker", "inspect", "--format", "{{json .State}}", container],
        timeout=20,
    )
    try:
        parsed = json.loads(result["stdout"])
    except json.JSONDecodeError as exc:
        raise RuntimeError("Docker inspect 状态不是有效 JSON") from exc
    if not isinstance(parsed, dict):
        raise RuntimeError("Docker inspect 状态格式不合法")
    return parsed


def _collect_logs(container: str) -> dict[str, Any]:
    result = _run_command(
        ["docker", "logs", "--timestamps", "--tail", "10000", container],
        timeout=30,
        allow_failure=True,
        max_output_bytes=MAX_OUTPUT_BYTES,
    )
    text = result["stdout"]
    if result["stderr"]:
        text = f"{text}\n{result['stderr']}".strip()
    return {
        "text": text,
        "bytes_seen": result["output_bytes"],
        "truncated": result["output_truncated"],
    }


def _collect_logs_safe(container: str) -> dict[str, Any]:
    try:
        return _collect_logs(container)
    except Exception as exc:  # noqa: BLE001
        return {
            "text": "",
            "bytes_seen": 0,
            "truncated": False,
            "collection_error": _redact(str(exc))[:500],
        }


def _remove_container(container: str) -> None:
    _run_command(["docker", "rm", "-f", container], timeout=30, allow_failure=True)
    remaining = _run_command(["docker", "inspect", container], timeout=20, allow_failure=True)
    if remaining["exit_code"] == 0:
        raise RuntimeError("Docker 容器清理后仍存在")


def _monitor_test(request_id: str, profile: Profile) -> None:
    container = _container_name(request_id)
    timed_out = False
    exit_code: int | None = None
    try:
        try:
            waited = _run_command(
                ["docker", "wait", container],
                timeout=profile.test_timeout_seconds,
                allow_failure=True,
            )
            raw_exit = waited["stdout"].strip().splitlines()[-1]
            exit_code = int(raw_exit)
        except TimeoutError:
            timed_out = True
            _run_command(["docker", "kill", "--signal", "KILL", container], timeout=20, allow_failure=True)
            exit_code = 124
        inspected = _inspect_container(container)
        logs = _collect_logs_safe(container)
        oom_killed = bool(inspected.get("OOMKilled"))
        outcome = "succeeded" if exit_code == 0 and not timed_out and not oom_killed else "failed"
        result = {
            "outcome": outcome,
            "exit_code": exit_code,
            "timed_out": timed_out,
            "oom_killed": oom_killed,
            "container_state": {
                "status": inspected.get("Status"),
                "started_at": inspected.get("StartedAt"),
                "finished_at": inspected.get("FinishedAt"),
            },
            "logs": logs,
            "artifacts": [],
        }
        with STATE_LOCK:
            state = _read_state(request_id)
            if state.get("status") in TERMINAL_STATUSES or state.get("stop_requested"):
                return
        _queue_terminal_cleanup(
            request_id,
            status_value=outcome,
            stage=outcome,
            message="白盒测试完成" if outcome == "succeeded" else "白盒测试失败",
            error="" if outcome == "succeeded" else "白盒测试未通过",
            result=result,
        )
    except Exception as exc:  # noqa: BLE001
        _finish_submission_error(request_id, "failed", "failed", str(exc))
    finally:
        MONITOR_THREADS.pop(request_id, None)


def _probe_container(container: str) -> bool:
    result = _run_command(
        [
            "docker",
            "exec",
            container,
            "bash",
            "-c",
            (
                'port="$1"; exec 3<>"/dev/tcp/127.0.0.1/$port"; '
                'printf "GET / HTTP/1.0\\r\\nHost: localhost\\r\\nConnection: close\\r\\n\\r\\n" >&3; '
                'IFS= read -r line <&3; '
                'case "$line" in HTTP/*\\ [23][0-9][0-9]\\ *) '
                'code="${line#HTTP/* }"; printf "%s" "${code%% *}"; exit 0;; *) exit 1;; esac'
            ),
            "prism-health",
            str(PREVIEW_PORT),
        ],
        timeout=5,
        allow_failure=True,
        max_output_bytes=1024,
    )
    code = result["stdout"].strip()
    return result["exit_code"] == 0 and re.fullmatch(r"[23][0-9]{2}", code) is not None


def _monitor_deployment(request_id: str, profile: Profile) -> None:
    container = _container_name(request_id)
    try:
        with STATE_LOCK:
            state = _read_state(request_id)
            if state.get("status") not in TERMINAL_STATUSES and state.get("status") != "running":
                _transition(state, "health_checking", "health_checking", "正在检查容器内固定回环端口")
        recovered_running = _read_state(request_id).get("status") == "running"
        healthy = recovered_running and _probe_container(container)
        deadline = time.monotonic() + profile.startup_timeout_seconds
        while not healthy and time.monotonic() < deadline:
            state = _read_state(request_id)
            if state.get("stop_requested") or state.get("status") in TERMINAL_STATUSES:
                return
            inspected = _inspect_container(container)
            if not inspected.get("Running"):
                break
            if _probe_container(container):
                healthy = True
                break
            time.sleep(1)
        if not healthy:
            logs = _collect_logs_safe(container)
            raise RuntimeError(f"部署容器未在限定时间内通过回环健康检查: {logs['text'][-1000:]}")
        with STATE_LOCK:
            state = _read_state(request_id)
            if state.get("status") not in TERMINAL_STATUSES and state.get("status") != "running":
                _transition(
                    state,
                    "running",
                    "running",
                    "持续部署已就绪，可通过受保护预览路径访问",
                    payload={"preview_supported": True, "preview_port": PREVIEW_PORT},
                )
        while True:
            time.sleep(2)
            state = _read_state(request_id)
            if state.get("stop_requested") or state.get("status") in TERMINAL_STATUSES:
                return
            if _utcnow() >= _parse_iso(state["expires_at"]):
                _expire_job(request_id)
                return
            inspected = _inspect_container(container)
            if not inspected.get("Running"):
                logs = _collect_logs_safe(container)
                _queue_terminal_cleanup(
                    request_id,
                    status_value="failed",
                    stage="failed",
                    message="持续部署容器意外退出",
                    error="持续部署容器意外退出",
                    result={
                        "outcome": "failed",
                        "exit_code": inspected.get("ExitCode"),
                        "timed_out": False,
                        "oom_killed": bool(inspected.get("OOMKilled")),
                        "logs": logs,
                        "artifacts": [],
                    },
                )
                return
    except Exception as exc:  # noqa: BLE001
        _finish_submission_error(request_id, "failed", "failed", str(exc))
    finally:
        MONITOR_THREADS.pop(request_id, None)


def _expire_job(request_id: str) -> None:
    with STATE_LOCK:
        state = _read_state(request_id)
        if state.get("status") in TERMINAL_STATUSES:
            return
        if state.get("status") == "stopping" and isinstance(state.get("pending_terminal"), dict):
            pending = True
        else:
            pending = False
    if pending:
        _retry_pending_cleanup(request_id)
        return
    logs = _collect_logs_safe(_container_name(request_id))
    _queue_terminal_cleanup(
        request_id,
        status_value="expired",
        stage="expired",
        message="沙箱已到期并回收",
        error="",
        result={"outcome": "expired", "exit_code": None, "timed_out": False, "logs": logs, "artifacts": []},
    )


def status_job(payload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("请求体必须是 JSON 对象")
    _check_exact_keys(payload, STATUS_KEYS)
    request_id = _validate_request_id(payload.get("request_id"))
    after_sequence = int(payload.get("after_sequence") or 0)
    if after_sequence < 0:
        raise ValueError("after_sequence 不能小于 0")
    with STATE_LOCK:
        return _public_state(_read_state(request_id), after_sequence=after_sequence)


def stop_job(payload: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    if not isinstance(payload, dict):
        raise ValueError("请求体必须是 JSON 对象")
    _check_exact_keys(payload, STOP_KEYS)
    request_id = _validate_request_id(payload.get("request_id"))
    with STATE_LOCK:
        state = _read_state(request_id)
        if state.get("status") in TERMINAL_STATUSES:
            return _public_state(state), True
        if state.get("status") == "stopping" and isinstance(state.get("pending_terminal"), dict):
            pending = True
        else:
            pending = False
    if pending:
        _retry_pending_cleanup(request_id)
    else:
        logs = _collect_logs_safe(_container_name(request_id))
        _queue_terminal_cleanup(
            request_id,
            status_value="stopped",
            stage="stopped",
            message="沙箱已关闭并清理",
            error="",
            result={"outcome": "stopped", "exit_code": None, "timed_out": False, "logs": logs, "artifacts": []},
        )
    with STATE_LOCK:
        return _public_state(_read_state(request_id)), False


def extend_job(payload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("请求体必须是 JSON 对象")
    _check_exact_keys(payload, EXTEND_KEYS)
    request_id = _validate_request_id(payload.get("request_id"))
    try:
        extend_seconds = int(payload.get("extend_seconds"))
    except (TypeError, ValueError) as exc:
        raise ValueError("extend_seconds 必须是整数") from exc
    if extend_seconds < 60 or extend_seconds > MAX_TTL_SECONDS:
        raise ValueError(f"extend_seconds 必须在 60 到 {MAX_TTL_SECONDS} 之间")
    with STATE_LOCK:
        state = _read_state(request_id)
        if state.get("purpose") != "deploy":
            raise ConflictError("一次性测试不能续期")
        if state.get("status") not in {"starting", "health_checking", "running"}:
            raise ConflictError("只有活动的持续部署沙箱可以续期")
        current = _parse_iso(state["expires_at"])
        maximum = _parse_iso(state["created_at"]) + timedelta(seconds=MAX_TTL_SECONDS)
        proposed = current + timedelta(seconds=extend_seconds)
        if proposed > maximum:
            raise ConflictError("续期超过该沙箱允许的最大保留时间")
        state["expires_at"] = _iso(proposed)
        _add_event(
            state,
            "lifecycle",
            state["stage"],
            "持续部署沙箱已续期",
            {"extend_seconds": extend_seconds, "expires_at": state["expires_at"]},
        )
        _write_state(state)
        return _public_state(state)


def health() -> dict[str, Any]:
    runtime = ""
    runtime_error = ""
    try:
        runtime = _resolve_runtime()
    except Exception as exc:  # noqa: BLE001
        runtime_error = _redact(str(exc))[:1000]
    profile_results: dict[str, Any] = {}
    try:
        profiles = _load_profiles()
    except Exception as exc:  # noqa: BLE001
        profiles = {}
        runtime_error = runtime_error or _redact(str(exc))[:1000]
    for language, profile in profiles.items():
        try:
            resolved = _resolve_image(profile)
            profile_results[language] = {
                "ready": True,
                "image_ref": profile.image,
                "image_digest": resolved.configured_digest,
                "profile_fingerprint": profile.fingerprint,
                "resource_policy": profile.resource_policy(),
            }
        except Exception as exc:  # noqa: BLE001
            profile_results[language] = {
                "ready": False,
                "image_ref": profile.image,
                "image_digest": profile.digest,
                "profile_fingerprint": profile.fingerprint,
                "error": _redact(str(exc))[:1000],
            }
    browser_blackbox = _browser_health(runtime, profiles)
    ready = bool(runtime) and set(profile_results) == LANGUAGES and all(item["ready"] for item in profile_results.values())
    return {
        "ready": ready,
        "protocol_version": PROTOCOL_VERSION,
        "mode": EXECUTOR_MODE,
        "runtime": runtime,
        "error": runtime_error,
        "max_concurrency": MAX_CONCURRENCY,
        "active_jobs": _active_job_count(),
        "pending_submissions": _pending_submission_count(),
        "supported_languages": sorted(LANGUAGES),
        "supported_purposes": sorted(PURPOSES),
        "supported_test_modes": ["whitebox", "blackbox", "combined"],
        "preview_supported": True,
        "profiles": profile_results,
        "browser_blackbox": browser_blackbox,
    }


def _normalize_preview_target(raw_path: str, query: str) -> str:
    if len(raw_path) > 2048 or len(query) > 2048 or "\r" in raw_path + query or "\n" in raw_path + query:
        raise ValueError("预览路径或查询参数过长")
    decoded = urllib.parse.unquote(raw_path or "/")
    decoded_twice = urllib.parse.unquote(decoded)
    if (
        not decoded.startswith("/")
        or decoded.startswith("//")
        or "\\" in decoded
        or "\x00" in decoded
        or any(ord(character) < 32 or ord(character) == 127 for character in decoded)
    ):
        raise ValueError("预览路径不合法")
    if any(part == ".." for part in PurePosixPath(decoded).parts) or any(
        part == ".." for part in PurePosixPath(decoded_twice).parts
    ):
        raise ValueError("预览路径不能包含上级目录")
    if re.search(r"%(?![0-9A-Fa-f]{2})", query):
        raise ValueError("预览查询参数包含无效百分号编码")
    quoted = urllib.parse.quote(decoded, safe="/!$&'()*+,-./:;=@_~")
    quoted_query = urllib.parse.quote(query, safe="!$&'()*+,-./:;=?@_~%")
    return f"http://127.0.0.1:{PREVIEW_PORT}{quoted}" + (f"?{quoted_query}" if quoted_query else "")


def _proxy_preview(
    request_id: str, raw_path: str, query: str, method: str, request_headers: dict[str, str], body: bytes,
) -> tuple[int, dict[str, str], bytes]:
    request_id = _validate_request_id(request_id)
    if method not in {"GET", "HEAD", "POST"}:
        raise ValueError("预览只支持 GET、HEAD 和 POST")
    with STATE_LOCK:
        state = _read_state(request_id)
    if state.get("purpose") != "deploy" or state.get("status") != "running":
        raise ConflictError("持续部署沙箱尚未处于可预览状态")
    if _utcnow() >= _parse_iso(state["expires_at"]):
        raise ConflictError("持续部署沙箱已到期")
    container = str(state.get("container_name") or "")
    if container != _container_name(request_id):
        raise RuntimeError("沙箱容器标识不一致")
    target = _normalize_preview_target(raw_path, query)
    args = [
        "docker",
        "exec",
        "-i",
        container,
        "/opt/prism/runner.sh",
        "proxy",
        method,
        str(len(body) if method == "POST" else 0),
        str(MAX_PREVIEW_RESPONSE_BYTES + 64 * 1024 + 1),
    ]
    for header in ("Accept", "Accept-Language", "Content-Type"):
        value = request_headers.get(header, "")
        if value:
            if len(value) > 512 or "\r" in value or "\n" in value:
                raise ValueError("预览请求头不合法")
        args.append(value)
    args.append(target)
    try:
        completed = subprocess.run(
            args,
            input=body if method == "POST" else None,
            capture_output=True,
            timeout=25,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("预览上游请求超时") from exc
    if completed.returncode != 0:
        error = _redact((completed.stderr or b"").decode("utf-8", errors="replace"))[:1000]
        raise RuntimeError(f"预览上游请求失败: {error}")
    raw = completed.stdout or b""
    if len(raw) >= MAX_PREVIEW_RESPONSE_BYTES + 64 * 1024 + 1:
        raise RuntimeError("预览响应超过大小限制")
    separator = b"\r\n\r\n"
    offset = raw.find(separator)
    if offset < 0:
        separator = b"\n\n"
        offset = raw.find(separator)
    if offset < 0:
        raise RuntimeError("预览上游未返回有效 HTTP 响应头")
    header_block = raw[:offset].decode("iso-8859-1")
    response_body = raw[offset + len(separator):]
    lines = header_block.replace("\r\n", "\n").split("\n")
    status_match = re.fullmatch(r"HTTP/\S+\s+([1-5][0-9]{2})(?:\s+.*)?", lines[0])
    if not status_match:
        raise RuntimeError("预览上游状态行不合法")
    status_code = int(status_match.group(1))
    forwarded: dict[str, str] = {}
    parsed_headers: dict[str, list[str]] = {}
    for line in lines[1:]:
        if not line:
            continue
        if line[:1].isspace() or ":" not in line:
            raise RuntimeError("预览上游响应头不合法")
        name, value = line.split(":", 1)
        lowered = name.strip().lower()
        clean_value = value.strip()
        if not re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+", name.strip()):
            raise RuntimeError("预览上游响应头名称不合法")
        parsed_headers.setdefault(lowered, []).append(clean_value)
        if lowered in SAFE_RESPONSE_HEADERS and "\r" not in clean_value and "\n" not in clean_value:
            forwarded[name.strip()] = clean_value[:2048]
    transfer_encodings = parsed_headers.get("transfer-encoding", [])
    content_lengths = parsed_headers.get("content-length", [])
    if transfer_encodings and content_lengths:
        raise RuntimeError("预览上游响应同时包含 Transfer-Encoding 和 Content-Length")
    if method == "HEAD" or status_code in {204, 304}:
        response_body = b""
    elif transfer_encodings:
        if len(transfer_encodings) != 1 or transfer_encodings[0].lower() != "chunked":
            raise RuntimeError("预览上游使用了不支持的 Transfer-Encoding")
        response_body = _decode_chunked_body(response_body)
    elif content_lengths:
        if len(set(content_lengths)) != 1 or not re.fullmatch(r"[0-9]+", content_lengths[0]):
            raise RuntimeError("预览上游 Content-Length 不合法")
        expected_length = int(content_lengths[0])
        if expected_length != len(response_body):
            raise RuntimeError("预览上游响应体长度不一致")
    if len(response_body) > MAX_PREVIEW_RESPONSE_BYTES:
        raise RuntimeError("预览响应超过大小限制")
    forwarded["Content-Length"] = str(len(response_body))
    return status_code, forwarded, b"" if method == "HEAD" else response_body


def _decode_chunked_body(raw: bytes) -> bytes:
    output = bytearray()
    position = 0
    while True:
        line_end = raw.find(b"\r\n", position)
        if line_end < 0 or line_end - position > 128:
            raise RuntimeError("预览上游分块响应不合法")
        size_token = raw[position:line_end].split(b";", 1)[0].strip()
        if not size_token or not re.fullmatch(rb"[0-9A-Fa-f]+", size_token):
            raise RuntimeError("预览上游分块大小不合法")
        size = int(size_token, 16)
        position = line_end + 2
        if size == 0:
            trailer = raw[position:]
            if trailer == b"\r\n":
                return bytes(output)
            if not trailer.endswith(b"\r\n\r\n"):
                raise RuntimeError("预览上游分块尾部不合法")
            for line in trailer[:-4].split(b"\r\n"):
                if not line or b":" not in line or line[:1] in b" \t":
                    raise RuntimeError("预览上游分块尾部不合法")
            return bytes(output)
        chunk_end = position + size
        if chunk_end + 2 > len(raw) or raw[chunk_end:chunk_end + 2] != b"\r\n":
            raise RuntimeError("预览上游分块响应被截断")
        output.extend(raw[position:chunk_end])
        if len(output) > MAX_PREVIEW_RESPONSE_BYTES:
            raise RuntimeError("预览响应超过大小限制")
        position = chunk_end + 2


class ThreadingUnixHTTPServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True
    allow_reuse_address = True


class Handler(BaseHTTPRequestHandler):
    server_version = "PrismSandboxExecutor/1.0"

    def _authorized(self) -> bool:
        expected = f"Bearer {TOKEN}"
        return bool(TOKEN) and hmac.compare_digest(self.headers.get("Authorization", ""), expected)

    def _require_auth(self) -> bool:
        if self._authorized():
            return True
        self._json(401, {"ok": False, "error": "unauthorized"})
        return False

    def _read_body(self, maximum: int) -> bytes:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise ValueError("Content-Length 不合法") from exc
        if length < 0 or length > maximum:
            raise ValueError("请求体大小不合法")
        return self.rfile.read(length) if length else b""

    def _read_json(self, maximum: int) -> dict[str, Any]:
        raw = self._read_body(maximum)
        if not raw:
            raise ValueError("请求体不能为空")
        try:
            payload = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("请求体不是有效 JSON") from exc
        if not isinstance(payload, dict):
            raise ValueError("请求体必须是 JSON 对象")
        return payload

    def _json(self, status_code: int, payload: dict[str, Any]) -> None:
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(encoded)

    def _handle_error(self, exc: Exception) -> None:
        if isinstance(exc, NotFoundError):
            status_code = 404
        elif isinstance(exc, ConflictError):
            status_code = 409
        elif isinstance(exc, CapacityError):
            status_code = 429
        elif isinstance(exc, BlockedError):
            status_code = 503
        elif isinstance(exc, (ValueError, TypeError)):
            status_code = 400
        else:
            status_code = 502
        self._json(status_code, {"ok": False, "error": _redact(str(exc))[:4000]})

    def _preview_route(self) -> tuple[str, str, str] | None:
        parsed = urllib.parse.urlsplit(self.path)
        match = re.fullmatch(r"/preview/([A-Za-z0-9][A-Za-z0-9_.-]{7,63})(/.*)?", parsed.path)
        if not match:
            return None
        return match.group(1), match.group(2) or "/", parsed.query

    def _handle_preview(self) -> None:
        route = self._preview_route()
        if route is None:
            self._json(404, {"ok": False, "error": "not found"})
            return
        if not self._require_auth():
            return
        try:
            request_id, path, query = route
            body = self._read_body(MAX_PREVIEW_REQUEST_BYTES) if self.command == "POST" else b""
            request_headers = {key: self.headers.get(key, "") for key in ("Accept", "Accept-Language", "Content-Type")}
            status_code, headers, response_body = _proxy_preview(
                request_id,
                path,
                query,
                self.command,
                request_headers,
                body,
            )
            self.send_response(status_code)
            for name, value in headers.items():
                self.send_header(name, value)
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(response_body)
        except Exception as exc:  # noqa: BLE001
            self._handle_error(exc)

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/health":
            if self._require_auth():
                result = health()
                self._json(200 if result["ready"] else 503, {"ok": result["ready"], "result": result})
            return
        self._handle_preview()

    def do_HEAD(self) -> None:  # noqa: N802
        self._handle_preview()

    def do_POST(self) -> None:  # noqa: N802
        if self.path.startswith("/preview/"):
            self._handle_preview()
            return
        if not self._require_auth():
            return
        try:
            if self.path == "/execute":
                result, duplicate = submit_job(self._read_json(MAX_REQUEST_BYTES))
                self._json(200, {"ok": True, "duplicate": duplicate, "result": result})
            elif self.path == "/status":
                self._json(200, {"ok": True, "result": status_job(self._read_json(64 * 1024))})
            elif self.path == "/stop":
                result, duplicate = stop_job(self._read_json(64 * 1024))
                self._json(200, {"ok": True, "duplicate": duplicate, "result": result})
            elif self.path == "/extend":
                self._json(200, {"ok": True, "result": extend_job(self._read_json(64 * 1024))})
            elif self.path == "/browser-blackbox":
                self._json(200, {"ok": True, "result": run_browser_blackbox(self._read_json(64 * 1024))})
            else:
                self._json(404, {"ok": False, "error": "not found"})
        except Exception as exc:  # noqa: BLE001
            self._handle_error(exc)

    def log_message(self, format_string: str, *args: Any) -> None:
        sys.stderr.write("prism-sandbox http: " + (format_string % args) + "\n")


def serve() -> None:
    if len(TOKEN) < 32:
        raise RuntimeError("SANDBOX_EXECUTOR_TOKEN 必须至少 32 字符")
    if not 1 <= MAX_CONCURRENCY <= 8:
        raise RuntimeError("SANDBOX_MAX_CONCURRENCY 必须在 1 到 8 之间")
    if not 60 <= DEFAULT_TTL_SECONDS <= MAX_TTL_SECONDS <= 30 * 24 * 60 * 60:
        raise RuntimeError("沙箱 TTL 配置不合法")
    _ensure_directory(SOCKET_PATH.parent, 0o750)
    _ensure_directory(STATE_DIR, 0o700)
    _ensure_directory(JOB_DIR, 0o700)
    if SOCKET_PATH.exists() or SOCKET_PATH.is_symlink():
        if SOCKET_PATH.is_symlink() or not stat.S_ISSOCK(SOCKET_PATH.lstat().st_mode):
            raise RuntimeError("拒绝覆盖非 socket 路径")
        SOCKET_PATH.unlink()
    old_umask = os.umask(0o007)
    try:
        _recover_jobs()
        JANITOR_STOP.clear()
        janitor = threading.Thread(target=_janitor_loop, name="sandbox-janitor", daemon=True)
        janitor.start()
        with ThreadingUnixHTTPServer(str(SOCKET_PATH), Handler) as server:
            os.chmod(SOCKET_PATH, 0o660)
            server.serve_forever(poll_interval=0.5)
    finally:
        JANITOR_STOP.set()
        os.umask(old_umask)
        if SOCKET_PATH.exists() and not SOCKET_PATH.is_symlink() and stat.S_ISSOCK(SOCKET_PATH.lstat().st_mode):
            SOCKET_PATH.unlink()


if __name__ == "__main__":
    serve()
