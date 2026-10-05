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

STATE_DIR = Path(os.environ.get("SECURITY_BLOCK_STATE_DIR", "/var/lib/prism-ops/security-block"))
SETS = {4: "prism-sec-v4", 6: "prism-sec-v6"}
CHAINS = {"INPUT": "PRISM-SEC-IN", "DOCKER-USER": "PRISM-SEC-DK"}
MAX_ACTIVE = 64
MAX_DAILY = 200
MAX_HISTORY = 2000
LOG_LIMIT = 20000
SCOPE = "host_ingress_and_docker_web"
CONFIG_KEYS = {"enabled", "ai_anomaly_enabled", "duration_seconds", "window_seconds", "ssh_threshold", "web_threshold", "allowlist_cidrs", "protected_ip"}
DEFAULT_POLICY = {"enabled": False, "ai_anomaly_enabled": False, "duration_seconds": 900, "window_seconds": 300,
                  "ssh_threshold": 20, "web_threshold": 30, "allowlist_cidrs": [],
                  "protected_ip": "", "activated_at": None}
SENSITIVE_TARGETS = frozenset({"/.env", "/.env.local", "/.env.production", "/.git/config", "/.git/head",
                             "/.git/index", "/.svn/entries", "/.aws/credentials", "/.ssh/authorized_keys",
                             "/.ssh/id_rsa", "/.ssh/id_ed25519"})


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
        ranges = {"duration_seconds": (60, 900), "window_seconds": (60, 900),
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
        root_value = os.environ.get("SECURITY_BLOCK_PROTECTED_CIDRS", "").strip()
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
        raw_ports = os.environ.get("SECURITY_BLOCK_SSH_PORTS", "22")
        ports = {int(item) for item in re.split(r"[,\s]+", raw_ports.strip()) if item}
        if not ports or any(not 1 <= port <= 65535 for port in ports):
            raise RuntimeError("root SSH 保护端口配置不合法")
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

    def _create_block(self, state: dict[str, Any], address: str, rule: str, items: dict[str, dict[str, Any]],
                      since: float, duration: int, errors: list[str], *, source: str = "deterministic_rule", reason: str = "") -> None:
        now = self.clock()
        active = [entry for entry in state["entries"] if entry["status"] in {"active", "unknown"}]
        daily = sum(_epoch(entry["started_at"]) >= now - 86400 for entry in state["entries"])
        if len(active) >= MAX_ACTIVE or daily >= MAX_DAILY:
            errors.append("自动封禁达到并发或每日安全上限，额外来源仅告警")
            return
        if any(entry["ip"] == address for entry in active):
            return
        identity = hashlib.sha256(f"{address}:{rule}:{now}:{','.join(sorted(items))}".encode()).hexdigest()[:24]
        entry = {"id": identity, "ip": address, "rule": rule, "evidence_count": len(items), "scope": SCOPE,
                 "status": "unknown", "started_at": _iso(now), "expires_at": _iso(now + duration),
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


def execute(action: str, params: dict[str, Any]) -> dict[str, Any]:
    controller = SecurityBlockController()
    if action in {"security_block_status", "security_block_reconcile", "security_block_candidates"} and params:
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
