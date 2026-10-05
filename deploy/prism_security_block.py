#!/usr/bin/env python3
"""确定性短时防御：root 管理单 IP 租约，ipset 内核 timeout 负责自然解封。

只读取本机可信日志，不接收模型指定的封禁目标，不改变防火墙默认策略。
"""
from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
import ipaddress
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import unquote, urlsplit

DEPLOY_DIR = Path(__file__).resolve().parent
DEPLOYMENT_CONFIG_KEYS = frozenset({
    "SECURITY_BLOCK_STATE_DIR",
    "SECURITY_BLOCK_PROTECTED_CIDRS",
    "SECURITY_BLOCK_SSH_PORTS",
})


def _deployment_setting(key: str, default: str = "") -> str:
    """读取单个显式允许的部署值，不把其余 dotenv 内容放入进程环境。"""
    if key not in DEPLOYMENT_CONFIG_KEYS:
        raise ValueError("部署配置键不在允许范围")
    if key in os.environ:
        return os.environ[key]

    path = DEPLOY_DIR / ".env"
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return default
    if path.is_symlink() or not path.is_file():
        raise RuntimeError("部署环境文件必须是普通文件")
    if metadata.st_mode & 0o077:
        raise RuntimeError("部署环境文件不能对组或其他用户开放")

    found: str | None = None
    with path.open("r", encoding="utf-8") as stream:
        for line in stream:
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                continue
            name, value = stripped.split("=", 1)
            if name.strip() != key:
                continue
            if found is not None:
                raise RuntimeError(f"部署环境文件中的 {key} 重复")
            value = value.strip()
            if value[:1] in {"'", '"'}:
                if len(value) < 2 or value[-1] != value[0]:
                    raise RuntimeError(f"部署环境文件中的 {key} 引号不匹配")
                value = value[1:-1]
            found = value
    return default if found is None else found


STATE_DIR = Path(_deployment_setting("SECURITY_BLOCK_STATE_DIR") or "/var/lib/prism-ops/security-block")
SETS = {4: "prism-sec-v4", 6: "prism-sec-v6"}
CHAINS = {"INPUT": "PRISM-SEC-IN", "DOCKER-USER": "PRISM-SEC-DK"}
MAX_ACTIVE = 64
MAX_DAILY = 200
MAX_HISTORY = 2000
LOG_LIMIT = 20000
SCOPE = "host_ingress_and_docker_web"
CONFIG_KEYS = {"enabled", "ai_anomaly_enabled", "duration_seconds", "window_seconds", "ssh_threshold",
              "web_threshold", "allowlist_cidrs", "protected_ip", "auto_escalate"}
# 自动升级：同一来源 24 小时内被处置过再次触发时，租约按倍数递增，硬上限 6 小时。
ESCALATION_MAX_SECONDS = 3600
ESCALATION_FACTOR = 4
DEFAULT_POLICY = {"enabled": False, "ai_anomaly_enabled": False, "duration_seconds": 900, "window_seconds": 300,
                  "ssh_threshold": 20, "web_threshold": 30, "allowlist_cidrs": [], "auto_escalate": False,
                  "protected_ip": "", "activated_at": None}
SENSITIVE_TARGETS = frozenset({"/.env", "/.env.local", "/.env.production", "/.git/config", "/.git/head",
                             "/.git/index", "/.svn/entries", "/.aws/credentials", "/.ssh/authorized_keys",
                             "/.ssh/id_rsa", "/.ssh/id_ed25519"})
# 溯源与自我审计的只读边界：不接收命令、路径或端口参数，不写任何宿主配置。
TRACE_ABSENCE_TTL = 86400
TRAFFIC_WINDOW_SECONDS = 86400
# 诱捕层（decoy）：命中署名路径的来源被引流到诱饵容器，而不是简单 DROP。
DECOY_SET = {4: "prism-decoy-v4", 6: "prism-decoy-v6"}
DECOY_CHAIN = "PRISM-DECOY-IN"
DECOY_PORT = 8443
DECOY_HIT_LOG = "/var/log/prism-decoy/access.log"
DECOY_SAFE_PORTS = (80, 443)
FIREWALL_FAMILIES = {"ipv4": "iptables", "ipv6": "ip6tables"}
HARDENING_APPLICATIONS = (
    ("fail2ban", "登录爆破自动处置"),
    ("clamav", "恶意文件扫描"),
    ("nmap", "端口与服务扫描（需管理员显式安装）"),
    ("suricata", "流量入侵检测（需管理员显式安装）"),
    ("zeek", "流量元数据审计（需管理员显式安装）"),
    ("ipset", "短时 IP 租约集合"),
)


def _iso(value: float | None) -> str | None:
    return datetime.fromtimestamp(value, timezone.utc).isoformat() if value is not None else None


def _epoch(value: Any) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()


def _ip(value: Any) -> ipaddress.IPv4Address | ipaddress.IPv6Address:
    raw = str(value).strip()
    if "/" in raw or "%" in raw:
        raise ValueError("只允许单个 IP，禁止 CIDR 或接口范围")
    address = ipaddress.ip_address(raw)
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
        address = address.ipv4_mapped
    return address


def _event_key(kind: str, timestamp: float, message: str) -> str:
    return hashlib.sha256(f"{kind}\0{timestamp}\0{message}".encode()).hexdigest()


def _public_scope(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """判断地址是否属于"非内网"范围（含 IPv4 保留段，例如云厂商使用的 100.64/10）。

    ``is_global`` 对文档段、共享地址段等保留范围返回 False，但这些地址在云主机
    连接表里确实代表外部对端；内网判定只认私有、回环、链路本地与组播。
    """
    return not (
        address.is_private or address.is_loopback or address.is_link_local
        or address.is_multicast or address.is_unspecified
    )


def parse_ssh_evidence(lines: list[str]) -> list[dict[str, Any]]:
    """一次 Failed password 为一个证据，不重复统计 Invalid user。"""
    result = []
    pattern = re.compile(r"Failed password for (?:invalid user )?\S+ from (\S+) port \d+")
    for line in lines:
        try:
            row = json.loads(line)
            message = str(row.get("MESSAGE") or "")
            match = pattern.search(message)
            if not match:
                continue
            timestamp = int(row["__REALTIME_TIMESTAMP"]) / 1_000_000
            address = str(_ip(match.group(1)))
            # journal cursor 区分同一微秒发生的不同失败；缺 cursor 时内容散列保守去重。
            identity = str(row.get("__CURSOR") or message)
            result.append({"ip": address, "rule": "ssh_failed_password", "occurred_at": timestamp,
                           "key": _event_key("ssh", timestamp, identity), "target": "ssh"})
        except (ValueError, KeyError, TypeError):
            continue
    return result


def parse_web_evidence(lines: list[str]) -> list[dict[str, Any]]:
    """只匹配直接入口记录的 remote_addr 与固定敏感探测路径，不信任 XFF。"""
    result = []
    pattern = re.compile(r'^([^ ]+) ([^ ]+) - [^ ]+ \[[^\]]+\] "([A-Z]+) ([^ ]+) HTTP/[^\"]+" (\d{3})')
    for line in lines:
        match = pattern.match(line.strip())
        if not match:
            continue
        raw_time, raw_ip, method, target, status = match.groups()
        if method not in {"GET", "HEAD", "POST"}:
            continue
        try:
            address = str(_ip(raw_ip))
            timestamp = _epoch(raw_time)
            path = unquote(urlsplit(target).path).lower()
        except (ValueError, TypeError):
            continue
        if path not in SENSITIVE_TARGETS:
            continue
        # 目标即使返回 200 仍是敏感探测；仅普通 4xx 永不作为封禁证据。
        result.append({"ip": address, "rule": "web_sensitive_probe", "occurred_at": timestamp,
                       "key": _event_key("web", timestamp, line), "target": path, "http_status": status})
    return result


def parse_ssh_attacker_summary(lines: list[str]) -> dict[str, dict[str, Any]]:
    """按来源 IP 汇总 SSH 爆破细节：次数、尝试账号与时间范围。

    与 ``parse_ssh_evidence`` 的差异：本函数供只读溯源使用，只输出来源、计数与
    账号名，不输出日志正文，也不进入封禁决策。
    """
    summary: dict[str, dict[str, Any]] = {}
    pattern = re.compile(r"Failed password for (?:invalid user )?(\S+) from (\S+) port \d+")
    for line in lines:
        try:
            row = json.loads(line)
            match = pattern.search(str(row.get("MESSAGE") or ""))
            if not match:
                continue
            address = str(_ip(match.group(2)))
            timestamp = int(row["__REALTIME_TIMESTAMP"]) / 1_000_000
        except (ValueError, KeyError, TypeError):
            continue
        item = summary.setdefault(address, {"count": 0, "_users": {}, "first_seen": None, "last_seen": None})
        item["count"] += 1
        user = str(match.group(1))[:64]
        item["_users"][user] = item["_users"].get(user, 0) + 1
        item["first_seen"] = timestamp if item["first_seen"] is None else min(item["first_seen"], timestamp)
        item["last_seen"] = timestamp if item["last_seen"] is None else max(item["last_seen"], timestamp)
    for item in summary.values():
        accounts = sorted(item.pop("_users").items(), key=lambda pair: (-pair[1], pair[0]))[:10]
        item["accounts_tried"] = [{"account": name, "count": count} for name, count in accounts]
        item["first_seen"] = _iso(item["first_seen"])
        item["last_seen"] = _iso(item["last_seen"])
    return summary


def parse_web_attacker_summary(lines: list[str]) -> dict[str, dict[str, Any]]:
    """按来源 IP 汇总敏感目标探测：次数、请求方法与路径清单（截断）。"""
    summary: dict[str, dict[str, Any]] = {}
    pattern = re.compile(r'^([0-9a-fA-F.:]+) ([^ ]+) - \[([^\]]+)\] "([A-Z]+) ([^ ]+) HTTP/[^"]+" (\d{3})')
    for line in lines:
        match = pattern.match(line.strip())
        if not match:
            continue
        raw_ip, _remote_user, raw_time, method, target, status = match.groups()
        try:
            address = str(_ip(raw_ip))
            timestamp = _epoch(raw_time)
            path = urlsplit(target).path[:160]
        except (ValueError, IndexError, TypeError):
            continue
        if path.lower() not in SENSITIVE_TARGETS:
            continue
        item = summary.setdefault(address, {
            "count": 0, "_targets": {}, "methods": {}, "status_codes": {}, "first_seen": None, "last_seen": None,
        })
        item["count"] += 1
        item["_targets"][path] = item["_targets"].get(path, 0) + 1
        item["methods"][method] = item["methods"].get(method, 0) + 1
        item["status_codes"][status] = item["status_codes"].get(status, 0) + 1
        item["first_seen"] = timestamp if item["first_seen"] is None else min(item["first_seen"], timestamp)
        item["last_seen"] = timestamp if item["last_seen"] is None else max(item["last_seen"], timestamp)
    for item in summary.values():
        targets = sorted(item.pop("_targets").items(), key=lambda pair: (-pair[1], pair[0]))[:10]
        item["targets"] = [{"path": path, "count": count} for path, count in targets]
        item["target_count"] = len(item["targets"])
        item["first_seen"] = _iso(item["first_seen"])
        item["last_seen"] = _iso(item["last_seen"])
    return summary


def parse_decoy_hits(lines: list[str]) -> list[dict[str, Any]]:
    """解析诱捕层访问日志（nginx log_format prism_decoy）。

    只提取来源、时间、请求行、状态与 UA；不解析、不保留任何请求正文。
    """
    pattern = re.compile(
        r'^([0-9a-fA-F.:]+) - \[([^\]]+)\] "([A-Z]+) ([^ ]+) HTTP/[^"]*" (\d{3}) (\d+) "([^"]*)"$'
    )
    hits: list[dict[str, Any]] = []
    for raw in lines:
        match = pattern.match(str(raw).strip())
        if not match:
            continue
        raw_ip, raw_time, method, target, status, size, user_agent = match.groups()
        try:
            address = str(_ip(raw_ip))
            timestamp = _epoch(raw_time)
        except (ValueError, TypeError):
            continue
        hits.append({
            "ip": address,
            "occurred_at": timestamp,
            "method": method,
            "path": target[:200],
            "http_status": status,
            "bytes": int(size),
            "user_agent": user_agent[:200],
        })
    return hits


def summarize_decoy_hits(hits: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """按来源汇总诱捕命中：命中路径数、总次数、工具指纹与时间范围。"""
    summary: dict[str, dict[str, Any]] = {}
    for hit in hits:
        item = summary.setdefault(hit["ip"], {
            "count": 0, "_paths": {}, "user_agents": {}, "first_seen": None, "last_seen": None,
        })
        item["count"] += 1
        item["_paths"][hit["path"]] = item["_paths"].get(hit["path"], 0) + 1
        if hit["user_agent"]:
            item["user_agents"][hit["user_agent"]] = item["user_agents"].get(hit["user_agent"], 0) + 1
        stamp = hit["occurred_at"]
        item["first_seen"] = stamp if item["first_seen"] is None else min(item["first_seen"], stamp)
        item["last_seen"] = stamp if item["last_seen"] is None else max(item["last_seen"], stamp)
    for item in summary.values():
        paths = sorted(item.pop("_paths").items(), key=lambda pair: (-pair[1], pair[0]))[:10]
        item["paths"] = [{"path": path, "count": count} for path, count in paths]
        item["path_count"] = len(item["paths"])
        agents = sorted(item["user_agents"].items(), key=lambda pair: (-pair[1], pair[0]))[:5]
        item["user_agents"] = [{"user_agent": ua, "count": count} for ua, count in agents]
        item["first_seen"] = _iso(item["first_seen"])
        item["last_seen"] = _iso(item["last_seen"])
    return summary


def run_command(args: list[str], *, timeout: int = 30) -> dict[str, Any]:
    completed = subprocess.run(args, capture_output=True, text=True, timeout=timeout, check=False)
    return {"exit_code": completed.returncode, "stdout": completed.stdout, "stderr": completed.stderr[-2000:]}


class SecurityBlockController:
    def __init__(self, state_dir: Path = STATE_DIR, *, runner: Callable[..., dict[str, Any]] = run_command,
                 clock: Callable[[], float] = time.time):
        self.state_dir = Path(state_dir)
        self.runner = runner
        self.clock = clock

    @contextlib.contextmanager
    def _locked(self):
        self.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.state_dir.is_symlink():
            raise RuntimeError("防御账本目录不能是符号链接")
        os.chmod(self.state_dir, 0o700)
        lock_path = self.state_dir / "state.lock"
        descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    def _read(self) -> dict[str, Any]:
        path = self.state_dir / "state.json"
        if not path.exists():
            return {"policy": dict(DEFAULT_POLICY), "entries": [], "last_evaluated_at": None, "errors": []}
        if path.is_symlink() or not path.is_file():
            raise RuntimeError("防御账本路径不安全")
        try:
            state = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(state, dict) or not isinstance(state["policy"], dict) or not isinstance(state["entries"], list):
                raise ValueError("invalid state")
            # 向前兼容：老账本缺少后来新增的策略键时按默认值补齐并回写，而不是整本判损坏。
            # 默认值始终是保守值（例如 auto_escalate=False），不会因为补键而放大防御动作。
            missing = [key for key in CONFIG_KEYS if key not in state["policy"]]
            if missing:
                for key in missing:
                    state["policy"][key] = DEFAULT_POLICY[key]
                self._write(state)
                self._audit("policy_backfilled", {"keys": sorted(missing)})
            self._validate_policy({key: state["policy"].get(key) for key in CONFIG_KEYS}, require_protection=False)
            return state
        except (ValueError, KeyError, TypeError) as exc:
            raise RuntimeError("防御账本损坏，已禁止执行") from exc

    def _write(self, state: dict[str, Any]) -> None:
        descriptor, temporary = tempfile.mkstemp(prefix=".state-", dir=self.state_dir)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(state, handle, ensure_ascii=False, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temporary, 0o600)
            os.replace(temporary, self.state_dir / "state.json")
            directory = os.open(self.state_dir, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def _audit(self, action: str, payload: dict[str, Any]) -> None:
        descriptor = os.open(self.state_dir / "audit.jsonl", os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
        try:
            raw = json.dumps({"time": _iso(self.clock()), "action": action, **payload}, ensure_ascii=False).encode() + b"\n"
            os.write(descriptor, raw)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    @staticmethod
    def _validate_policy(params: dict[str, Any], *, require_protection: bool = True) -> dict[str, Any]:
        if set(params) != CONFIG_KEYS or not isinstance(params.get("enabled"), bool) or not isinstance(params.get("ai_anomaly_enabled"), bool):
            raise ValueError("防御策略字段不完整或 enabled 不是布尔值")
        if not isinstance(params.get("auto_escalate"), bool):
            raise ValueError("auto_escalate 必须是布尔值")
        ranges = {"duration_seconds": (60, ESCALATION_MAX_SECONDS), "window_seconds": (60, 900),
                  "ssh_threshold": (20, 200), "web_threshold": (30, 500)}
        for key, (minimum, maximum) in ranges.items():
            value = params.get(key)
            if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
                raise ValueError(f"{key} 必须是 {minimum}–{maximum} 范围整数")
        raw_networks = params.get("allowlist_cidrs")
        if not isinstance(raw_networks, list) or len(raw_networks) > 32:
            raise ValueError("白名单最多允许 32 个网段")
        networks = []
        for raw in raw_networks:
            if not isinstance(raw, str):
                raise ValueError("白名单必须是 CIDR 字符串")
            network = ipaddress.ip_network(raw, strict=True)
            if network.prefixlen < (24 if network.version == 4 else 64):
                raise ValueError("白名单网段过宽：IPv4 至少 /24，IPv6 至少 /64")
            networks.append(str(network))
        protected = params.get("protected_ip")
        if not isinstance(protected, str):
            raise ValueError("protected_ip 必须是字符串")
        if protected:
            protected = str(_ip(protected))
        if require_protection and params["enabled"] and not protected:
            raise ValueError("启用防御必须保护当前最高管理员 IP")
        return {**params, "allowlist_cidrs": list(dict.fromkeys(networks)), "protected_ip": protected}

    def _run(self, args: list[str], *, required: bool = True) -> dict[str, Any]:
        result = self.runner(args, timeout=30)
        if required and result.get("exit_code") != 0:
            raise RuntimeError(f"防御执行失败：{args[0]} {args[1]}: {str(result.get('stderr') or '')[:300]}")
        return result

    def _capabilities(self) -> tuple[dict[int, bool], list[str]]:
        supported, errors = {4: False, 6: False}, []
        for dependency in ("ipset", "ip", "ss"):
            if not shutil.which(dependency):
                errors.append(f"缺少 {dependency}，保持监控")
        if errors:
            return supported, errors
        for family, tool in ((4, "iptables"), (6, "ip6tables")):
            if not shutil.which(tool):
                errors.append(f"IPv{family} 缺少 {tool}，不可封禁")
                continue
            try:
                for chain in ("INPUT", "DOCKER-USER"):
                    self._run([tool, "-w", "5", "-S", chain])
                supported[family] = True
            except (RuntimeError, OSError, subprocess.SubprocessError) as exc:
                errors.append(f"IPv{family} 链路不可用：{exc}")
        return supported, errors

    def _protected_sources(self, policy: dict[str, Any]) -> list[dict[str, str]]:
        sources = [{"cidr": network, "reason": "管理员配置白名单"} for network in policy["allowlist_cidrs"]]
        root_value = _deployment_setting("SECURITY_BLOCK_PROTECTED_CIDRS").strip()
        root_networks = json.loads(root_value) if root_value.startswith("[") else re.split(r"[,\s]+", root_value)
        if not isinstance(root_networks, list) or any(not isinstance(value, str) for value in root_networks):
            raise ValueError("root 保护配置必须是 CIDR 字符串列表")
        for raw in root_networks:
            if raw:
                sources.append({"cidr": str(ipaddress.ip_network(raw, strict=True)), "reason": "root 固定保护来源"})
        if policy["protected_ip"]:
            address = _ip(policy["protected_ip"])
            sources.append({"cidr": f"{address}/{address.max_prefixlen}", "reason": "当前最高管理员连接"})
        addresses = json.loads(self._run(["ip", "-j", "address", "show"])["stdout"])
        for interface in addresses:
            for info in interface.get("addr_info", []):
                address = _ip(info["local"])
                sources.append({"cidr": f"{address}/{address.max_prefixlen}", "reason": "服务器本机地址"})
        raw_ports = _deployment_setting("SECURITY_BLOCK_SSH_PORTS", "22")
        ports = {int(item) for item in re.split(r"[,\s]+", raw_ports.strip()) if item}
        if not ports or any(not 1 <= port <= 65535 for port in ports):
            raise RuntimeError("root SSH 保护端口配置不合法")
        # 云主机常见"公网地址不绑定在网卡上"（NAT/弹性地址），此时网卡枚举拿不到
        # 自身公网地址。因此对已建立连接：SSH 管理端口的对端一律保护；其他端口的
        # 公网对端也一并保护，避免管理入口或平台自身的公网访问被误处置。
        connections = self._run(["ss", "-H", "-tn", "state", "established"])["stdout"]
        for line in connections.splitlines():
            columns = line.split()
            if len(columns) < 4:
                continue
            local, peer = columns[-2:]
            try:
                local_port = int(local.rsplit(":", 1)[1])
                address = _ip(peer.rsplit(":", 1)[0].strip("[]"))
            except (ValueError, IndexError):
                continue
            if local_port in ports:
                sources.append({"cidr": f"{address}/{address.max_prefixlen}", "reason": "当前 SSH 管理连接"})
            elif address.is_global:
                sources.append({"cidr": f"{address}/{address.max_prefixlen}", "reason": "与本机已建立连接的公网对端"})
            elif _public_scope(address):
                # 云主机自身公网地址可能落在保留段/运营商段内，连接表里的对端也可能是
                # 这类地址；它们同样不是内网对端，需要与公网对端一样受保护。
                sources.append({"cidr": f"{address}/{address.max_prefixlen}", "reason": "与本机已建立连接的公网对端"})
        accepted_log = self._run(["journalctl", "-u", "sshd", "--since", f"@{int(self.clock() - 3600)}", "--no-pager", "--output=json", "-n", str(LOG_LIMIT)])["stdout"]
        accepted_lines = accepted_log.splitlines()
        if len(accepted_lines) >= LOG_LIMIT:
            raise RuntimeError("最近 SSH 成功来源保护日志达到采样上限，保持监控")
        accepted_pattern = re.compile(r"Accepted (?:publickey|password|keyboard-interactive(?:/pam)?) for \S+ from (\S+) port \d+")
        for line in accepted_lines:
            try:
                row = json.loads(line)
                match = accepted_pattern.search(str(row.get("MESSAGE") or ""))
                timestamp = int(row["__REALTIME_TIMESTAMP"]) / 1_000_000
                if not match or not self.clock() - 3600 <= timestamp <= self.clock():
                    continue
                address = _ip(match.group(1))
                sources.append({"cidr": f"{address}/{address.max_prefixlen}", "reason": "最近一小时 SSH 成功登录来源"})
            except (ValueError, KeyError, TypeError):
                continue
        return list({(row["cidr"], row["reason"]): row for row in sources}.values())

    def _members(self, family: int) -> dict[str, int] | None:
        result = self._run(["ipset", "save", SETS[family]], required=False)
        if result.get("exit_code") != 0:
            return None
        lines = str(result.get("stdout") or "").splitlines()
        expected_family = "inet" if family == 4 else "inet6"
        create = next((line.split() for line in lines if line.startswith(f"create {SETS[family]} ")), [])
        if "hash:ip" not in create or "family" not in create or create[create.index("family") + 1] != expected_family or "timeout" not in create:
            raise RuntimeError("专用 ipset 类型或原生到期配置不匹配，拒绝修改")
        members = {}
        for line in lines:
            parts = line.split()
            if len(parts) >= 3 and parts[:2] == ["add", SETS[family]]:
                if "timeout" not in parts:
                    raise RuntimeError("专用集合成员缺少原生到期时间")
                remaining = int(parts[parts.index("timeout") + 1])
                if not 0 < remaining <= 900:
                    raise RuntimeError("专用集合成员到期时间超出安全范围")
                members[str(_ip(parts[2]))] = remaining
        return members

    def _ensure_paths(self, supported: dict[int, bool]) -> None:
        for family in (4, 6):
            if not supported[family]:
                continue
            try:
                self._ensure_family_paths(family)
            except (RuntimeError, ValueError, OSError, subprocess.SubprocessError):
                if family == 4:
                    raise
                # IPv6 前置链存在但内核模块不支持时，不能影响已验证IPv4。
                supported[family] = False

    def _ensure_family_paths(self, family: int) -> None:
        tool = "iptables" if family == 4 else "ip6tables"
        if self._members(family) is None:
            self._run(["ipset", "create", SETS[family], "hash:ip", "family", "inet" if family == 4 else "inet6", "timeout", "900", "maxelem", str(MAX_ACTIVE)])
        for parent, chain in CHAINS.items():
            if self._run([tool, "-w", "5", "-S", chain], required=False)["exit_code"] != 0:
                self._run([tool, "-w", "5", "-N", chain])
            if parent == "INPUT":
                specifications = [["-m", "set", "--match-set", SETS[family], "src", "-j", "DROP"]]
            else:
                specifications = [["-p", "tcp", "-m", "conntrack", "--ctdir", "ORIGINAL", "--ctorigdstport", port,
                                   "-m", "set", "--match-set", SETS[family], "src", "-j", "DROP"] for port in ("80", "443")]
            for rule in specifications:
                if self._run([tool, "-w", "5", "-C", chain, *rule], required=False)["exit_code"] != 0:
                    self._run([tool, "-w", "5", "-A", chain, *rule])
            if self._run([tool, "-w", "5", "-C", parent, "-j", chain], required=False)["exit_code"] != 0:
                self._run([tool, "-w", "5", "-I", parent, "1", "-j", chain])

    def _verify_paths(self, family: int) -> bool:
        tool = "iptables" if family == 4 else "ip6tables"
        if self._members(family) is None:
            return False
        for parent, chain in CHAINS.items():
            if self._run([tool, "-w", "5", "-C", parent, "-j", chain], required=False)["exit_code"] != 0:
                return False
            rules = [["-m", "set", "--match-set", SETS[family], "src", "-j", "DROP"]] if parent == "INPUT" else [
                ["-p", "tcp", "-m", "conntrack", "--ctdir", "ORIGINAL", "--ctorigdstport", port,
                 "-m", "set", "--match-set", SETS[family], "src", "-j", "DROP"] for port in ("80", "443")]
            for rule in rules:
                if self._run([tool, "-w", "5", "-C", chain, *rule], required=False)["exit_code"] != 0:
                    return False
        return True

    def _release_protected(self, state: dict[str, Any], protected: list[dict[str, str]]) -> None:
        """新增白名单或新管理连接必须立即获得保护，不能等旧租约到期。"""
        networks = [ipaddress.ip_network(row["cidr"]) for row in protected]
        for entry in state["entries"]:
            if entry["status"] not in {"active", "unknown"}:
                continue
            address = _ip(entry["ip"])
            if not any(address.version == network.version and address in network for network in networks):
                continue
            self._audit("protected_release_requested", {"id": entry["id"], "ip": str(address)})
            self._run(["ipset", "del", SETS[address.version], str(address), "-exist"])
            members = self._members(address.version)
            if members is None or str(address) in members:
                raise RuntimeError("新增保护来源的解封状态未确认")
            entry.update(status="released", released_at=_iso(self.clock()), reason="来源新增白名单或管理员连接保护")
            self._write(state)
            self._audit("protected_released", {"id": entry["id"], "ip": str(address)})

    def _sync(self, state: dict[str, Any], supported: dict[int, bool], errors: list[str]) -> None:
        members = {}
        for family in (4, 6):
            try:
                members[family] = self._members(family) if supported[family] else None
            except (RuntimeError, ValueError, OSError, subprocess.SubprocessError) as exc:
                members[family] = None
                errors.append(str(exc))
        now = self.clock()
        for entry in state["entries"]:
            if entry["status"] not in {"active", "unknown"}:
                continue
            family = _ip(entry["ip"]).version
            if members[family] is None:
                entry["status"] = "unknown"
            elif entry["ip"] in members[family]:
                entry["status"] = "active"
            elif now >= _epoch(entry["expires_at"]):
                entry.update(status="expired", released_at=_iso(now))
                self._audit("expired", {"id": entry["id"], "ip": entry["ip"], "reason": "内核 TTL 已到期且集合查询确认移除"})
            else:
                entry["status"] = "unknown"
                errors.append(f"{entry['ip']} 未到期但集合中缺失，结果未确认")

    def _snapshot(self, state: dict[str, Any]) -> dict[str, Any]:
        supported, errors = self._capabilities()
        try:
            protected = self._protected_sources(state["policy"])
        except (RuntimeError, ValueError, OSError, subprocess.SubprocessError) as exc:
            protected = []
            supported = {4: False, 6: False}
            errors.append(f"保护来源读取失败，拒绝封禁：{exc}")
        if state["policy"]["enabled"]:
            for family in (4, 6):
                try:
                    if supported[family] and not self._verify_paths(family):
                        supported[family] = False
                        errors.append(f"IPv{family} 专用防御链未验证，保持监控")
                except (RuntimeError, ValueError, OSError, subprocess.SubprocessError) as exc:
                    supported[family] = False
                    errors.append(f"IPv{family} 验证失败：{exc}")
        self._sync(state, supported, errors)
        all_errors = list(dict.fromkeys([*state.get("errors", []), *errors]))[-20:]
        self._write(state)
        public_entries = [{key: value for key, value in entry.items() if not key.startswith("_")} for entry in state["entries"]]
        return {"verified": bool(supported[4]), "available": bool(supported[4]), "enabled": bool(state["policy"]["enabled"]),
                "policy": state["policy"], "protected_sources": protected,
                "active_blocks": [entry for entry in public_entries if entry["status"] == "active"],
                "recent_blocks": sorted(public_entries, key=lambda entry: entry["started_at"], reverse=True)[:100],
                "last_evaluated_at": state.get("last_evaluated_at"), "errors": all_errors,
                "family_support": {"ipv4": supported[4], "ipv6": supported[6]}, "backend": "ipset"}

    def status(self) -> dict[str, Any]:
        with self._locked():
            return self._snapshot(self._read())

    def configure(self, params: dict[str, Any]) -> dict[str, Any]:
        policy = self._validate_policy(params)
        with self._locked():
            state = self._read()
            previous = state["policy"]
            policy["activated_at"] = previous.get("activated_at") if previous["enabled"] and policy["enabled"] else _iso(self.clock()) if policy["enabled"] else None
            supported, errors = self._capabilities()
            if policy["enabled"]:
                if not supported[4]:
                    raise RuntimeError("IPv4 原生到期与 Docker 链路不可用，不能启用自动封禁")
                protected = self._protected_sources(policy)  # 保护来源读取失败时不能配置启用。
                self._ensure_paths(supported)
            self._audit("configure", {"policy": policy})
            if policy["enabled"]:
                self._release_protected(state, protected)
            if not policy["enabled"]:
                for family in (4, 6):
                    if supported[family] and self._members(family) is not None:
                        self._run(["ipset", "flush", SETS[family]])
                        members = self._members(family)
                        if members is None or members:
                            raise RuntimeError("关闭防御后的实际集合清空状态未确认")
                for entry in state["entries"]:
                    if entry["status"] in {"active", "unknown"}:
                        if supported[_ip(entry["ip"]).version]:
                            entry.update(status="released", released_at=_iso(self.clock()), reason="最高管理员关闭自动防御")
            state.update(policy=policy, errors=errors)
            self._write(state)
            return self._snapshot(state)

    def _collect_evidence(self, since: float) -> tuple[list[dict[str, Any]], list[str]]:
        errors, evidence = [], []
        # --since Unix timestamp 与 Docker RFC3339 均使用服务器时钟；只读本机可信日志。
        commands = [("ssh", ["journalctl", "-u", "sshd", "--since", f"@{int(since)}", "--no-pager", "--output=json", "-n", str(LOG_LIMIT)]),
                    ("web", ["docker", "logs", "--timestamps", "--since", str(_iso(since)), "--tail", str(LOG_LIMIT), "cr_frontend"])]
        for kind, command in commands:
            try:
                result = self._run(command)
                lines = str(result.get("stdout") or "").splitlines()
                if len(lines) >= LOG_LIMIT:
                    errors.append(f"{kind} 日志达到 {LOG_LIMIT} 行采样上限；封禁阈值仅按已读取证据下界判断")
                evidence.extend(parse_ssh_evidence(lines) if kind == "ssh" else parse_web_evidence(lines))
            except (RuntimeError, OSError, subprocess.SubprocessError) as exc:
                errors.append(f"{kind} 证据采集失败：{exc}")
        return evidence, errors

    def _candidate_groups(self, state: dict[str, Any], supported: dict[int, bool],
                          protected: list[dict[str, str]], evidence: list[dict[str, Any]], since: float) -> dict:
        grouped: dict[tuple[str, str], dict[str, dict[str, Any]]] = defaultdict(dict)
        networks = [ipaddress.ip_network(item["cidr"]) for item in protected]
        used, terminal_cutoffs = set(), {}
        for entry in state["entries"]:
            used.update(entry.get("_evidence_keys", []))
            if entry["status"] in {"expired", "released", "failed", "unknown"}:
                terminal_cutoffs[entry["ip"]] = max(terminal_cutoffs.get(entry["ip"], 0), _epoch(entry.get("released_at") or entry["started_at"]))
        active_ips = {entry["ip"] for entry in state["entries"] if entry["status"] in {"active", "unknown"}}
        for item in evidence:
            try:
                address = _ip(item["ip"])
                timestamp = _epoch(item["occurred_at"])
                if (not address.is_global or address.is_multicast or address.is_reserved or address.is_unspecified
                        or address.is_loopback or address.is_private or not supported[address.version] or str(address) in active_ips):
                    continue
                if any(address in network for network in networks if address.version == network.version):
                    continue
                if timestamp < since or timestamp > self.clock() or timestamp <= terminal_cutoffs.get(str(address), 0) or item["key"] in used:
                    continue
                if item["rule"] not in {"ssh_failed_password", "web_sensitive_probe"}:
                    continue
                grouped[(str(address), str(item["rule"]))][item["key"]] = item
            except (ValueError, KeyError, TypeError):
                continue
        return grouped

    def _recent_handled(self, state: dict[str, Any], address: str, now: float) -> int:
        """统计该来源近 24 小时已被处置过的次数（含已到期/已解封），用于自动升级判定。"""
        return sum(
            entry.get("ip") == address and _epoch(entry.get("started_at")) >= now - 86400
            for entry in state["entries"]
        )

    def _create_block(self, state: dict[str, Any], address: str, rule: str, items: dict[str, dict[str, Any]],
                      since: float, duration: int, errors: list[str], *, source: str = "deterministic_rule",
                      reason: str = "") -> None:
        now = self.clock()
        active = [entry for entry in state["entries"] if entry["status"] in {"active", "unknown"}]
        daily = sum(_epoch(entry["started_at"]) >= now - 86400 for entry in state["entries"])
        if len(active) >= MAX_ACTIVE or daily >= MAX_DAILY:
            errors.append("自动封禁达到并发或每日安全上限，额外来源仅告警")
            return
        if any(entry["ip"] == address for entry in active):
            return
        # 自动升级：只有管理员显式开启 auto_escalate 才生效；始终受 ESCALATION_MAX_SECONDS 硬上限约束。
        previous = self._recent_handled(state, address, now)
        escalated = bool(state["policy"].get("auto_escalate")) and previous > 0
        if escalated:
            duration = min(duration * (ESCALATION_FACTOR ** previous), ESCALATION_MAX_SECONDS)
        identity = hashlib.sha256(f"{address}:{rule}:{now}:{','.join(sorted(items))}".encode()).hexdigest()[:24]
        entry = {"id": identity, "ip": address, "rule": rule, "evidence_count": len(items), "scope": SCOPE,
                 "status": "unknown", "started_at": _iso(now), "expires_at": _iso(now + duration),
                 "escalated": escalated, "previous_blocks_24h": previous, "duration_seconds": duration,
                 "released_at": None, "reason": reason or "可信日志短窗口达到确定性防御阈值", "source": source,
                 "_evidence_keys": sorted(items), "evidence_window_start": _iso(since),
                 "evidence_window_end": _iso(now), "evidence_is_lower_bound": bool(errors),
                 "evidence_target_count": len({item["target"] for item in items.values()})}
        state["entries"].append(entry)
        self._write(state)
        self._audit("block_requested", {key: value for key, value in entry.items() if not key.startswith("_")})
        family = _ip(address).version
        try:
            self._run(["ipset", "add", SETS[family], address, "timeout", str(duration)])
            members = self._members(family)
            if members is None or address not in members or not self._verify_paths(family):
                raise RuntimeError("封禁后实际集合或流量链路未确认")
            entry["status"] = "active"
            self._audit("blocked", {"id": identity, "ip": address, "expires_at": entry["expires_at"]})
        except (RuntimeError, ValueError, OSError, subprocess.SubprocessError) as exc:
            try:
                self._run(["ipset", "del", SETS[family], address, "-exist"], required=False)
                members = self._members(family)
            except (RuntimeError, ValueError, OSError, subprocess.SubprocessError):
                members = None
            entry["status"] = "failed" if members is not None and address not in members else "unknown"
            entry["reason"] = str(exc)[:500]
            errors.append(str(exc))
            self._audit("block_failed", {"id": identity, "ip": address, "status": entry["status"], "reason": entry["reason"]})
        self._write(state)

    def reconcile(self) -> dict[str, Any]:
        with self._locked():
            state = self._read()
            if not state["policy"]["enabled"]:
                return self._snapshot(state)
            supported, errors = self._capabilities()
            state["last_evaluated_at"] = _iso(self.clock())
            if not supported[4]:
                state["errors"] = errors
                return self._snapshot(state)
            try:
                protected = self._protected_sources(state["policy"])
                self._ensure_paths(supported)
                self._sync(state, supported, errors)
                self._release_protected(state, protected)
            except (RuntimeError, ValueError, OSError, subprocess.SubprocessError) as exc:
                state["errors"] = [*errors, str(exc)]
                return self._snapshot(state)
            now = self.clock()
            activated = _epoch(state["policy"]["activated_at"])
            since = max(activated, now - state["policy"]["window_seconds"])
            evidence, source_errors = self._collect_evidence(since)
            errors.extend(source_errors)
            grouped = self._candidate_groups(state, supported, protected, evidence, since)
            for (address, rule), items in sorted(grouped.items()):
                threshold = state["policy"]["ssh_threshold"] if rule == "ssh_failed_password" else state["policy"]["web_threshold"]
                if len(items) < threshold or (rule == "web_sensitive_probe" and len({item["target"] for item in items.values()}) < 3):
                    continue
                self._create_block(state, address, rule, items, since, state["policy"]["duration_seconds"], errors)
            # 始终保留活跃/未知项；裁剪历史不影响每日上限和最近证据去重。
            terminal = [entry for entry in state["entries"] if entry["status"] not in {"active", "unknown"}]
            state["entries"] = [entry for entry in state["entries"] if entry["status"] in {"active", "unknown"}] + terminal[-MAX_HISTORY:]
            state["errors"] = errors[-20:]
            self._write(state)
            return self._snapshot(state)

    def release(self, params: dict[str, Any]) -> dict[str, Any]:
        if set(params) != {"ip", "reason"} or not isinstance(params["reason"], str) or not 1 <= len(params["reason"].strip()) <= 200:
            raise ValueError("解封必须提供单个 IP 和 1–200 字原因")
        address = str(_ip(params["ip"]))
        family = _ip(address).version
        with self._locked():
            state = self._read()
            targets = [entry for entry in state["entries"] if entry["ip"] == address and entry["status"] in {"active", "unknown"}]
            if targets:
                self._audit("release_requested", {"ip": address, "reason": params["reason"]})
                self._run(["ipset", "del", SETS[family], address, "-exist"])
                members = self._members(family)
                if members is None or address in members:
                    raise RuntimeError("解封后的实际集合状态未确认")
                for entry in targets:
                    entry.update(status="released", released_at=_iso(self.clock()), reason=params["reason"].strip())
                self._write(state)
                self._audit("released", {"ip": address, "reason": params["reason"]})
            return self._snapshot(state)

    def candidates(self) -> dict[str, Any]:
        """给小菱的内部候选：未启用 AI 时不采集、不发候选。"""
        with self._locked():
            state = self._read()
            snapshot = self._snapshot(state)
            candidates = []
            if snapshot["verified"] and state["policy"]["enabled"] and state["policy"]["ai_anomaly_enabled"]:
                supported, errors = self._capabilities()
                protected = self._protected_sources(state["policy"])
                since = max(_epoch(state["policy"]["activated_at"]), self.clock() - state["policy"]["window_seconds"])
                evidence, collection_errors = self._collect_evidence(since)
                errors.extend(collection_errors)
                groups = self._candidate_groups(state, supported, protected, evidence, since)
                cache = []
                old_cache = state.get("_candidate_cache", [])
                for (address, rule), items in sorted(groups.items(), key=lambda row: (-len(row[1]), row[0])):
                    targets = {item["target"] for item in items.values()}
                    if len(items) < 10 or (rule == "web_sensitive_probe" and len(targets) < 3):
                        continue
                    keys = sorted(items)
                    prior = next((row for row in old_cache if row["ip"] == address and row["rule"] == rule and row["_keys"] == keys and row["expires_at"] > self.clock()), None)
                    # 同一真实证据保持稳定编号，缓存自然过期不会重复消耗模型。
                    row = prior or {"candidate_id": hashlib.sha256(f"{address}:{rule}:{state['policy']['activated_at']}:{','.join(keys)}".encode()).hexdigest()[:32],
                                    "ip": address, "rule": rule, "_keys": keys, "created_at": self.clock(),
                                    "expires_at": self.clock() + 60, "window_start": since}
                    cache.append(row)
                    candidates.append({"candidate_id": row["candidate_id"], "rule": rule, "evidence_count": len(items),
                                       "target_kinds": len(targets), "window_seconds": state["policy"]["window_seconds"],
                                       "expires_at": _iso(row["expires_at"]), "evidence_is_lower_bound": bool(collection_errors)})
                    if len(candidates) >= 20:
                        break
                state["_candidate_cache"] = cache
                state["errors"] = errors[-20:]
                self._write(state)
                snapshot = self._snapshot(state)
            return {**snapshot, "candidates": candidates}

    def apply_anomalies(self, params: dict[str, Any]) -> dict[str, Any]:
        if set(params) != {"decisions"} or not isinstance(params["decisions"], list) or not 1 <= len(params["decisions"]) <= 3:
            raise ValueError("异常处置每批必须是 1–3 个候选决定")
        decisions = params["decisions"]
        seen = set()
        for decision in decisions:
            if (not isinstance(decision, dict) or set(decision) != {"candidate_id", "reason"}
                    or not isinstance(decision.get("candidate_id"), str) or not re.fullmatch(r"[a-f0-9]{32}", decision["candidate_id"])
                    or not isinstance(decision.get("reason"), str) or not 1 <= len(decision["reason"].strip()) <= 200
                    or decision["candidate_id"] in seen):
                raise ValueError("异常决定只能引用唯一候选编号与 1–200 字原因")
            seen.add(decision["candidate_id"])
        with self._locked():
            state = self._read()
            if not state["policy"]["enabled"] or not state["policy"]["ai_anomaly_enabled"]:
                raise RuntimeError("最高管理员未启用小菱异常处置")
            supported, errors = self._capabilities()
            if not supported[4]:
                raise RuntimeError("内核链路无法验证，拒绝异常处置")
            protected = self._protected_sources(state["policy"])
            self._ensure_paths(supported)
            self._sync(state, supported, errors)
            self._release_protected(state, protected)
            since = max(_epoch(state["policy"]["activated_at"]), self.clock() - state["policy"]["window_seconds"])
            evidence, collection_errors = self._collect_evidence(since)
            errors.extend(collection_errors)
            groups = self._candidate_groups(state, supported, protected, evidence, since)
            verified = []
            for decision in decisions:
                cached = next((row for row in state.get("_candidate_cache", []) if row["candidate_id"] == decision["candidate_id"]), None)
                if not cached or cached["expires_at"] <= self.clock():
                    raise ValueError("候选编号不存在或已超过 60 秒有效期")
                items = groups.get((cached["ip"], cached["rule"]), {})
                selected = {key: items[key] for key in cached["_keys"] if key in items}
                if len(selected) != len(cached["_keys"]) or len(selected) < 10 or (cached["rule"] == "web_sensitive_probe" and len({item["target"] for item in selected.values()}) < 3):
                    raise ValueError("真实证据已过期、已处置或来源已受保护，禁止封禁")
                verified.append((cached, selected, decision["reason"].strip()))
            # 全批次验证通过后才开始，模型没有自定义 IP / TTL 的入口。
            for cached, selected, reason in verified:
                self._create_block(state, cached["ip"], cached["rule"], selected, since,
                                   min(120, state["policy"]["duration_seconds"]), errors,
                                   source="xiaoling_anomaly", reason=reason)
            state["_candidate_cache"] = [row for row in state.get("_candidate_cache", []) if row["candidate_id"] not in seen]
            state["last_evaluated_at"] = _iso(self.clock())
            state["errors"] = errors[-20:]
            self._write(state)
            return self._snapshot(state)


    def _defense_record(self, address: str) -> list[dict[str, Any]]:
        """从租约账本提取与该来源相关的处置记录，不含保护来源与本机地址。"""
        records = []
        for entry in self._read()["entries"]:
            if entry.get("ip") != address:
                continue
            records.append({
                "id": str(entry.get("id") or "")[:120],
                "rule": str(entry.get("rule") or "")[:80],
                "source": str(entry.get("source") or "deterministic_rule")[:40],
                "status": str(entry.get("status") or "unknown"),
                "started_at": entry.get("started_at"),
                "expires_at": entry.get("expires_at"),
                "released_at": entry.get("released_at"),
                "evidence_count": int(entry.get("evidence_count") or 0),
                "reason": str(entry.get("reason") or "")[:200],
            })
        return sorted(records, key=lambda row: str(row.get("started_at") or ""), reverse=True)[:20]

    def _log_lines(self, kind: str, since: float) -> tuple[list[str], list[str]]:
        """只读采集可信日志；单源失败降级为错误说明，不影响其余来源。"""
        errors: list[str] = []
        if kind == "ssh":
            command = ["journalctl", "-u", "sshd", "--since", f"@{int(since)}", "--no-pager", "--output=json", "-n", str(LOG_LIMIT)]
        else:
            command = ["docker", "logs", "--timestamps", "--since", str(_iso(since)), "--tail", str(LOG_LIMIT), "cr_frontend"]
        try:
            result = self._run(command)
        except (RuntimeError, OSError, subprocess.SubprocessError) as exc:
            return [], [f"{kind} 证据采集失败：{exc}"]
        lines = str(result.get("stdout") or "").splitlines()
        if len(lines) >= LOG_LIMIT:
            errors.append(f"{kind} 日志达到 {LOG_LIMIT} 行采样上限，统计为下界")
        return lines, errors

    def _optional_command(self, name: str, args: list[str], *, timeout: int = 20) -> dict[str, Any]:
        """可选只读外部查询：缺工具或失败都显式记录，不伪装成成功。"""
        executable = shutil.which(args[0])
        if not executable:
            return {"source": name, "ok": False, "note": f"未安装 {args[0]}"}
        try:
            result = self.runner(args, timeout=timeout)
        except (OSError, subprocess.SubprocessError) as exc:
            return {"source": name, "ok": False, "note": f"查询失败：{str(exc)[:200]}"}
        text = str(result.get("stdout") or "").strip()
        if result.get("exit_code") != 0:
            return {"source": name, "ok": False, "note": f"退出码 {result.get('exit_code')}：{str(result.get('stderr') or '')[:200]}"}
        return {"source": name, "ok": True, "output": text[:4000]}

    def _outbound_attribution(self, address: str) -> dict[str, Any]:
        """出网威胁情报：只发送攻击来源 IP，不携带本机信息与日志正文。"""
        base = os.environ.get("THREAT_INTEL_BASE_URL") or ""
        if not base:
            path = DEPLOY_DIR / ".env"
            if path.is_file() and not path.is_symlink():
                for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
                    name, _, value = line.strip().partition("=")
                    if name.strip() == "THREAT_INTEL_BASE_URL":
                        base = value.strip().strip("'\"")
                        break
        base = base or "https://ipinfo.io"
        if not re.fullmatch(r"https?://[A-Za-z0-9.-]+(/[A-Za-z0-9._~/-]*)*", base):
            return {"ok": False, "note": "THREAT_INTEL_BASE_URL 不合法"}
        root = base.rstrip("/")
        url = f"{root}/{address}/json" if root.endswith(("ipinfo.io", "ipwho.is")) else (
            f"{root}/{address}?fields=status,message,country,regionName,city,isp,org,as,query"
        )
        result = self._optional_command("threat_intel", ["curl", "-fsS", "--max-time", "12", url], timeout=18)
        if not result.get("ok"):
            return {"ok": False, "note": result.get("note")}
        try:
            data = json.loads(result["output"])
        except json.JSONDecodeError:
            return {"ok": False, "note": "无法解析情报响应"}
        if not isinstance(data, dict) or data.get("status") == "fail" or data.get("success") is False:
            return {"ok": False, "note": "情报源未返回可用归因"}
        # 兼容两类响应形状：ip-api 用 regionName/isp/as，ipinfo 与 ipwho.is 用 region/org/asn（或 as）。
        org = str(data.get("org") or data.get("isp") or "")[:96]
        as_value = str(data.get("as") or data.get("asn") or "")[:96]
        return {"ok": True, "source": url.split("/", 3)[2], "attribution": {
            "country": str(data.get("country") or "")[:64],
            "region": str(data.get("regionName") or data.get("region") or "")[:64],
            "city": str(data.get("city") or "")[:64],
            "isp": str(data.get("isp") or org)[:96],
            "org": org,
            "as": as_value if as_value.upper().startswith("AS") else (f"AS{as_value}" if as_value else ""),
        }}

    def _decoy_log_hits(self) -> tuple[list[dict[str, Any]], list[str]]:
        """只读诱捕层访问日志；文件不存在属正常（诱捕层未启用）。"""
        path = Path(DECOY_HIT_LOG)
        if not path.exists():
            return [], []
        if path.is_symlink() or not path.is_file():
            return [], ["诱捕日志路径不安全，拒绝读取"]
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError as exc:
            return [], [f"诱捕日志读取失败：{str(exc)[:200]}"]
        if len(lines) > LOG_LIMIT:
            lines = lines[-LOG_LIMIT:]
        return parse_decoy_hits(lines), []

    def _decoy_chain_order_ok(self) -> tuple[bool, str]:
        """引流链必须排在 DROP 链之前，否则流量到不了诱捕层（fail-closed 校验）。"""
        for table, chain in (("iptables", "INPUT"), ("iptables", "DOCKER-USER")):
            result = self._run([table, "-S", chain], required=False)
            if result.get("exit_code") != 0:
                return False, f"{chain} 链不可读"
            rules = [line for line in str(result.get("stdout") or "").splitlines()]
            decoy_at = next((i for i, line in enumerate(rules) if DECOY_CHAIN in line), None)
            drop_at = next((i for i, line in enumerate(rules) if "PRISM-SEC-IN" in line or "PRISM-SEC-DK" in line), None)
            if decoy_at is None:
                continue
            if drop_at is not None and decoy_at > drop_at:
                return False, f"{chain} 中 {DECOY_CHAIN} 排在 DROP 链之后，拒绝启用引流"
        return True, ""

    def decoy_status(self) -> dict[str, Any]:
        """诱捕层状态：容器存活、命中汇总、引流集合与链序可读性（全部只读）。"""
        with self._locked():
            errors: list[str] = []
            hits, log_errors = self._decoy_log_hits()
            errors.extend(log_errors)
            summary = summarize_decoy_hits(hits)
            ranked = sorted(summary.items(), key=lambda pair: pair[1]["count"], reverse=True)[:50]
            running = self._run(["docker", "inspect", "-f", "{{.State.Running}}", "cr_decoy"], required=False)
            container_running = running.get("exit_code") == 0 and "true" in str(running.get("stdout") or "").lower()
            members: dict[str, Any] = {}
            for family in (4, 6):
                saved = self._run(["ipset", "save", DECOY_SET[family]], required=False)
                members[str(family)] = sorted(
                    line.split()[2] for line in str(saved.get("stdout") or "").splitlines()
                    if line.startswith("add ")
                )
            order_ok, order_error = self._decoy_chain_order_ok()
            if order_error:
                errors.append(order_error)
            return {
                "generated_at": _iso(self.clock()),
                "container_running": container_running,
                "hit_total": len(hits),
                "sources": [
                    {"ip": ip, **{key: value for key, value in item.items() if key != "count"}, "count": item["count"]}
                    for ip, item in ranked
                ],
                "source_total": len(summary),
                "redirect_members": members,
                "redirect_total": sum(len(value) for value in members.values()),
                "chain_order_ok": order_ok,
                "redirect_chain": DECOY_CHAIN,
                "redirect_port": DECOY_PORT,
                "log_path": DECOY_HIT_LOG,
                "errors": errors[-10:],
            }

    def decoy_apply(self, params: dict[str, Any]) -> dict[str, Any]:
        """把命中诱饵的来源加入引流集合，并确保引流链存在且顺序正确。

        安全边界：只加入公网地址；保护来源、非公网、已在引流集合中的地址一律跳过；
        链序不合法时**拒绝写入任何规则**（fail-closed）。
        """
        hits, log_errors = self._decoy_log_hits()
        summary = summarize_decoy_hits(hits)
        with self._locked():
            state = self._read()
            errors = list(log_errors)
            try:
                protected = [row["cidr"] for row in self._protected_sources(state["policy"])]
            except (RuntimeError, ValueError, OSError, subprocess.SubprocessError) as exc:
                protected = []
                errors.append(f"保护来源读取失败：{str(exc)[:200]}")
            networks = []
            for cidr in protected:
                try:
                    networks.append(ipaddress.ip_network(cidr))
                except ValueError:
                    continue
            order_ok, order_error = self._decoy_chain_order_ok()
            if not order_ok and order_error:
                raise RuntimeError(f"{order_error}；拒绝写入引流规则")
            supported, cap_errors = self._capabilities()
            errors.extend(cap_errors)
            applied: list[dict[str, Any]] = []
            skipped: list[dict[str, str]] = []
            for ip, item in sorted(summary.items(), key=lambda pair: pair[1]["count"], reverse=True):
                if len(applied) >= 64:
                    errors.append("引流单批上限 64 条，其余来源下轮处理")
                    break
                try:
                    address = _ip(ip)
                except ValueError:
                    skipped.append({"ip": str(ip)[:64], "reason": "地址不合法"})
                    continue
                if not _public_scope(address) or address.is_loopback:
                    skipped.append({"ip": str(address), "reason": "非公网来源"})
                    continue
                if any(address in network for network in networks if network.version == address.version):
                    skipped.append({"ip": str(address), "reason": "受保护来源"})
                    continue
                if not supported[address.version]:
                    skipped.append({"ip": str(address), "reason": f"IPv{address.version} 内核链路不可用"})
                    continue
                family = address.version
                self._run(["ipset", "add", DECOY_SET[family], str(address), "timeout",
                           str(ESCALATION_MAX_SECONDS)], required=False)
                members = self._members_for(DECOY_SET[family])
                if members is None or str(address) not in members:
                    skipped.append({"ip": str(address), "reason": "内核集合未确认写入"})
                    continue
                applied.append({"ip": str(address), "hits": item["count"], "paths": item["path_count"],
                                "lease_seconds": ESCALATION_MAX_SECONDS})
            return {
                "generated_at": _iso(self.clock()),
                "applied": applied,
                "skipped": skipped[:50],
                "hit_sources": len(summary),
                "chain_order_ok": True,
                "errors": errors[-10:],
            }

    def _members_for(self, name: str) -> dict[str, int] | None:
        """读取任意 ipset 集合成员；供引流集合复用。"""
        result = self._run(["ipset", "save", name], required=False)
        if result.get("exit_code") != 0:
            return None
        members: dict[str, int] = {}
        for line in str(result.get("stdout") or "").splitlines():
            if not line.startswith("add "):
                continue
            parts = line.split()
            if len(parts) < 3:
                continue
            members[parts[2]] = 0
        return members

    def ip_trace(self, params: dict[str, Any]) -> dict[str, Any]:
        """单来源取证：可信日志统计 + 被动归因 + 处置记录 + 确定性风险评分。

        只接受一个 IP。溯源是只读动作：不向目标发包、不连接目标端口、不写入任何
        防火墙规则；出网仅访问独立配置的被动情报源。评分只用本机可信日志证据。
        """
        address = str(_ip(params["ip"]))
        since = self.clock() - TRACE_ABSENCE_TTL
        with self._locked():
            state = self._read()
            ssh_lines, ssh_errors = self._log_lines("ssh", since)
            web_lines, web_errors = self._log_lines("web", since)
            errors = [*ssh_errors, *web_errors]
            ssh = parse_ssh_attacker_summary(ssh_lines).get(address, {})
            web = parse_web_attacker_summary(web_lines).get(address, {})
            try:
                protected = [row["cidr"] for row in self._protected_sources(state["policy"])]
            except (RuntimeError, ValueError, OSError, subprocess.SubprocessError) as exc:
                protected = []
                errors.append(f"保护来源读取失败：{exc}")
            networks = []
            for cidr in protected:
                try:
                    networks.append(ipaddress.ip_network(cidr))
                except ValueError:
                    continue
            parsed = _ip(address)
            is_protected = any(parsed in network for network in networks if network.version == parsed.version)
            severity = 0
            reasons: list[str] = []
            if ssh:
                severity += 3
                reasons.append(f"SSH 密码失败 {ssh['count']} 次（尝试账号 {len(ssh.get('accounts_tried') or [])} 个）")
            if web:
                severity += 3
                reasons.append(f"敏感目标探测 {web['count']} 次、不同目标 {web.get('target_count') or 0} 个")
            if ssh and web:
                severity += 2
                reasons.append("同一来源同时命中 SSH 与 Web 两类证据")
            if (web.get("target_count") or 0) >= 3:
                severity += 1
                reasons.append("覆盖 3 个以上敏感目标，符合自动化扫描特征")
            if is_protected:
                severity += 2
                reasons.append("该来源属于受保护地址（管理员连接/服务器地址/白名单），不参与自动处置")
            blocked = [row for row in self._defense_record(address) if row["status"] == "active"]
            if blocked:
                severity += 1
                reasons.append(f"当前存在 {len(blocked)} 条生效中的临时封禁租约")
            score = min(100, severity * 10)
            level = "critical" if score >= 70 else "high" if score >= 50 else "medium" if score >= 30 else "low"
            if not ssh and not web:
                reasons.append("近 24 小时可信日志中没有该来源的攻击证据")
            public = _ip(address).is_global
            attribution = self._outbound_attribution(address) if public else {"ok": False, "note": "非公网地址，不做外部归因"}
            dns = self._optional_command("reverse_dns", ["getent", "hosts", address], timeout=15) if public else {"source": "reverse_dns", "ok": False, "note": "非公网地址"}
            whois = self._optional_command("whois", ["whois", address], timeout=25) if public and shutil.which("whois") else {
                "source": "whois", "ok": False, "note": "未安装 whois 或非公网地址",
            }
            whois_summary = ""
            if whois.get("ok"):
                keys = ("orgname", "org-name", "netname", "country", "descr", "origin", "route")
                picked = []
                for line in str(whois.get("output") or "").splitlines():
                    name, _, value = line.partition(":")
                    if name.strip().lower() in keys and value.strip():
                        picked.append(f"{name.strip()}={value.strip()[:80]}")
                    if len(picked) >= 8:
                        break
                whois_summary = "; ".join(picked)
            return {
                "ip": address,
                "generated_at": _iso(self.clock()),
                "window_hours": TRACE_ABSENCE_TTL // 3600,
                "is_public": public,
                "is_protected": is_protected,
                "risk": {"score": score, "level": level, "reasons": reasons,
                         "basis": "本机可信日志证据，不含模型推断；外部归因不参与评分"},
                "ssh": {"count": int(ssh.get("count") or 0), "accounts_tried": ssh.get("accounts_tried") or [],
                        "first_seen": ssh.get("first_seen"), "last_seen": ssh.get("last_seen")},
                "web": {"count": int(web.get("count") or 0), "target_count": int(web.get("target_count") or 0),
                        "targets": web.get("targets") or [], "methods": web.get("methods") or {},
                        "status_codes": web.get("status_codes") or {},
                        "first_seen": web.get("first_seen"), "last_seen": web.get("last_seen")},
                "defense_records": self._defense_record(address),
                "attribution": attribution,
                "reverse_dns": dns,
                "whois": {"ok": bool(whois.get("ok")), "summary": whois_summary,
                          "note": str(whois.get("note") or "")[:200]},
                "evidence_sources": ["journalctl -u sshd", "docker logs cr_frontend"],
                "errors": errors[-10:],
            }

    def surface_audit(self) -> dict[str, Any]:
        """本机防御面自我审计：只读快照，不安装软件、不修改防火墙与端口。"""
        with self._locked():
            errors: list[str] = []
            listeners: list[dict[str, Any]] = []
            listening = self._run(["ss", "-H", "-lntup"], required=False)
            if listening.get("exit_code") != 0:
                errors.append("监听端口读取失败")
            for line in str(listening.get("stdout") or "").splitlines():
                columns = line.split()
                if len(columns) < 5:
                    continue
                protocol = columns[0]
                local = columns[4] if columns[0].startswith(("tcp", "udp")) else columns[3]
                address, _, port = local.rpartition(":")
                process = ""
                if "users:" in line:
                    process = line.split("users:", 1)[1].strip()[:120]
                listeners.append({"protocol": protocol, "address": address.strip("[]") or "*",
                                  "port": port, "process": process})
            public_listeners = [row for row in listeners
                                if row["address"] in {"0.0.0.0", "*", "::", "[::]"}]
            firewall: dict[str, Any] = {}
            for family, tool in FIREWALL_FAMILIES.items():
                chain_rows: list[dict[str, Any]] = []
                for chain in ("INPUT", "DOCKER-USER"):
                    result = self._run([tool, "-S", chain], required=False)
                    if result.get("exit_code") != 0:
                        chain_rows.append({"chain": chain, "ok": False, "note": "读取失败或链路不存在"})
                        continue
                    lines = [line for line in str(result.get("stdout") or "").splitlines()][:400]
                    chain_rows.append({"chain": chain, "ok": True, "policy": next(
                        (line for line in lines if line.startswith(f"-P {chain}")), f"-P {chain} -"), "rules": lines})
                tools = [name for name, _ in HARDENING_APPLICATIONS if shutil.which(name)]
                firewall[family] = {"tool": tool, "chains": chain_rows, "tools_present": tools}
            applications = [
                {"name": name, "purpose": purpose, "installed": bool(shutil.which(name))}
                for name, purpose in HARDENING_APPLICATIONS
            ]
            state = self._read()
            return {
                "generated_at": _iso(self.clock()),
                "listeners": listeners[:200],
                "public_listeners": public_listeners[:100],
                "firewall": firewall,
                "applications": applications,
                "ipset": {"sets": [SETS[4], SETS[6]], "present": bool(shutil.which("ipset"))},
                "blocking": {"enabled": bool(state["policy"]["enabled"]), "backend": "ipset",
                             "active_leases": len([row for row in state["entries"] if row["status"] == "active"])},
                "ssh_ports": str(_deployment_setting("SECURITY_BLOCK_SSH_PORTS", "22"))[:64],
                "errors": errors,
            }

    def traffic_summary(self, params: dict[str, Any]) -> dict[str, Any]:
        """流量元数据取证：只输出对端 IP、端口、协议、状态与字节计数，不含载荷。"""
        hours = params.get("since_hours", 24)
        since = self.clock() - hours * 3600
        with self._locked():
            errors: list[str] = []
            connections: list[dict[str, Any]] = []
            collected = self._run(["ss", "-H", "-tunap"], required=False)
            if collected.get("exit_code") != 0:
                errors.append("连接表读取失败")
            for line in str(collected.get("stdout") or "").splitlines()[:4000]:
                columns = line.split()
                if len(columns) < 5:
                    continue
                protocol = columns[0]
                local, peer = columns[4], columns[5]
                address, _, port = peer.rpartition(":")
                try:
                    peer_ip = None if address.strip("[]") in {"*", "0.0.0.0", "::"} else str(_ip(address.strip("[]")))
                except ValueError:
                    continue
                if peer_ip is None:
                    continue
                process = line.split("users:", 1)[1].strip()[:120] if "users:" in line else ""
                connections.append({"protocol": protocol, "peer_ip": peer_ip, "peer_port": port,
                                    "local": local, "process": process, "state": columns[1] if len(columns) > 1 else ""})
            ssh_lines, ssh_errors = self._log_lines("ssh", since)
            web_lines, web_errors = self._log_lines("web", since)
            errors.extend([*ssh_errors, *web_errors])
            ssh_by_ip = parse_ssh_attacker_summary(ssh_lines)
            web_by_ip = parse_web_attacker_summary(web_lines)
            peers: dict[str, dict[str, Any]] = {}
            for row in connections:
                item = peers.setdefault(row["peer_ip"], {"ip": row["peer_ip"], "connections": 0, "protocols": {},
                                                         "peer_ports": {}, "processes": set(), "states": {}})
                item["connections"] += 1
                item["protocols"][row["protocol"]] = item["protocols"].get(row["protocol"], 0) + 1
                item["peer_ports"][row["peer_port"]] = item["peer_ports"].get(row["peer_port"], 0) + 1
                item["states"][row["state"]] = item["states"].get(row["state"], 0) + 1
                if row["process"]:
                    item["processes"].add(row["process"])
            for ip, summary in ssh_by_ip.items():
                item = peers.setdefault(ip, {"ip": ip, "connections": 0, "protocols": {}, "peer_ports": {},
                                             "processes": set(), "states": {}})
                item["ssh_failed_count"] = summary["count"]
                item["last_seen"] = summary["last_seen"]
            for ip, summary in web_by_ip.items():
                item = peers.setdefault(ip, {"ip": ip, "connections": 0, "protocols": {}, "peer_ports": {},
                                             "processes": set(), "states": {}})
                item["sensitive_probe_count"] = summary["count"]
                item["target_count"] = summary["target_count"]
                item["last_seen"] = summary["last_seen"]
            rows = []
            for item in peers.values():
                item["processes"] = sorted(item["processes"])[:5]
                rows.append(item)
            rows.sort(key=lambda row: (row.get("ssh_failed_count", 0) + row.get("sensitive_probe_count", 0),
                                       row.get("connections", 0)), reverse=True)
            return {
                "generated_at": _iso(self.clock()),
                "window_hours": hours,
                "peers": rows[:200],
                "peer_total": len(rows),
                "current_connections": len(connections),
                "recent_ssh_failed_sources": len(ssh_by_ip),
                "recent_probe_sources": len(web_by_ip),
                "payload_captured": False,
                "note": "只采集连接元数据与可信日志计数，不捕获也不存储流量载荷。",
                "errors": errors[-10:],
            }


def execute(action: str, params: dict[str, Any]) -> dict[str, Any]:
    controller = SecurityBlockController()
    if action in {"security_block_status", "security_block_reconcile", "security_block_candidates",
                  "security_surface_audit"} and params:
        raise ValueError("安全状态和内部巡检不接收参数")
    if action == "security_block_status":
        return controller.status()
    if action == "security_block_configure":
        return controller.configure(params)
    if action == "security_block_reconcile":
        return controller.reconcile()
    if action == "security_block_release":
        return controller.release(params)
    if action == "security_block_candidates":
        return controller.candidates()
    if action == "security_block_apply_anomalies":
        return controller.apply_anomalies(params)
    if action == "security_decoy_status":
        if params:
            raise ValueError("诱捕层状态不接收参数")
        return controller.decoy_status()
    if action == "security_decoy_apply":
        if set(params) - {"reason"}:
            raise ValueError("诱捕层引流只接受 reason 参数")
        return controller.decoy_apply(params)
    if action == "security_ip_trace":
        if set(params) != {"ip"}:
            raise ValueError("来源溯源只接受单个 ip 参数")
        return controller.ip_trace(params)
    if action == "security_surface_audit":
        return controller.surface_audit()
    if action == "security_traffic_summary":
        if set(params) - {"since_hours"}:
            raise ValueError("流量元数据摘要只接受 since_hours 参数")
        return controller.traffic_summary(params)
    raise ValueError("未知安全防御动作")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Prism root 限时自动封禁巡检")
    parser.add_argument("--reconcile", action="store_true")
    args = parser.parse_args()
    snapshot = SecurityBlockController().reconcile() if args.reconcile else SecurityBlockController().status()
    output = snapshot if not args.reconcile else {
        "available": snapshot["available"], "verified": snapshot["verified"], "enabled": snapshot["enabled"],
        "active_count": len(snapshot["active_blocks"]), "last_evaluated_at": snapshot["last_evaluated_at"],
        "errors": [str(error)[:200] for error in snapshot["errors"][:5]],
    }
    print(json.dumps(output, ensure_ascii=False))
    raise SystemExit(0 if not snapshot["enabled"] or snapshot["available"] else 1)
