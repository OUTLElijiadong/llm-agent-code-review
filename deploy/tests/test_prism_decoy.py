"""诱捕层（decoy）解析、链序 fail-closed 与引流边界测试。

命令模拟器只返回结构化假数据；不触碰宿主机日志、ipset 或容器。
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

from tests.security_host import Host

MODULE = Path(__file__).resolve().parents[1] / "prism_security_block.py"
SPEC = importlib.util.spec_from_file_location("prism_security_block", MODULE)
assert SPEC and SPEC.loader
security = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(security)


def decoy_line(ip: str, path: str, ua: str = "curl/8.0", status: str = "200",
               stamp: str = "2026-10-05T23:10:11+08:00", size: str = "512") -> str:
    return f'{ip} - [{stamp}] "GET {path} HTTP/1.1" {status} {size} "{ua}"'


class DecoyHost(Host):
    """在共享宿主模拟器上补出诱捕层需要的 docker/iptables 行为。"""

    def __init__(self, *, hits: list[str] | None = None, chain_order: str = "decoy_first",
                 container_running: bool = True):
        super().__init__(ssh_lines=[], web_lines=[])
        # 默认样本用真实公网地址（8.8.4.4 / 9.9.9.9）；203.0.113.0/24 属文档保留段，
        # Python 的 is_private 为真，会被"非公网来源"正确拒绝，不应用于正向用例。
        self.hits = hits if hits is not None else [decoy_line("8.8.4.4", "/.env"),
                                                   decoy_line("8.8.4.4", "/wp-login.php"),
                                                   decoy_line("9.9.9.9", "/.git/config")]
        self.chain_order = chain_order
        self.container_running = container_running
        self.decoy_chain_exists = False
        self.decoy_sets: dict[str, set[str]] = {"prism-decoy-v4": set(), "prism-decoy-v6": set()}

    def run(self, args, **kwargs):
        self.commands.append(list(args))
        ok = {"exit_code": 0, "stdout": "", "stderr": ""}
        if args[0] == "docker":
            if args[1] == "inspect":
                return {**ok, "stdout": "true\n" if self.container_running else "false\n"}
            return ok
        if args[0] in {"iptables", "ip6tables"} and "-S" in args:
            chain = args[args.index("-S") + 1]
            if chain == "PRISM-DECOY-IN":
                return {**ok, "stdout": "-N PRISM-DECOY-IN\n"} if self.decoy_chain_exists else {**ok, "exit_code": 1}
            if chain in {"INPUT", "DOCKER-USER"}:
                if self.chain_order == "decoy_first":
                    return {**ok, "stdout": f"-P {chain} ACCEPT\n-A {chain} -j PRISM-DECOY-IN\n-A {chain} -j PRISM-SEC-IN\n"}
                return {**ok, "stdout": f"-P {chain} ACCEPT\n-A {chain} -j PRISM-SEC-IN\n-A {chain} -j PRISM-DECOY-IN\n"}
            return {**ok, "exit_code": 1}
        if args[0] == "ipset":
            name = args[2]
            if args[1] == "save":
                if name in self.decoy_sets:
                    family = "inet6" if name.endswith("v6") else "inet"
                    lines = [f"create {name} hash:ip family {family} timeout 3600 maxelem 1024"]
                    lines += [f"add {name} {ip} timeout 3600" for ip in sorted(self.decoy_sets[name])]
                    return {**ok, "stdout": "\n".join(lines)}
                return super().run(args, **kwargs)
            if args[1] == "add" and name in self.decoy_sets:
                self.decoy_sets[name].add(args[3])
                return ok
            if args[1] == "del" and name in self.decoy_sets:
                self.decoy_sets[name].discard(args[3])
                return ok
            return super().run(args, **kwargs)
        return super().run(args, **kwargs)


@pytest.fixture
def decoy_env(tmp_path, monkeypatch):
    host = DecoyHost()
    monkeypatch.setattr(security.shutil, "which", lambda name: name if name in host.which else None)
    monkeypatch.delenv("SECURITY_BLOCK_PROTECTED_CIDRS", raising=False)
    log = tmp_path / "decoy-access.log"
    log.write_text("\n".join(host.hits) + "\n", encoding="utf-8")
    monkeypatch.setattr(security, "DECOY_HIT_LOG", str(log))
    ctl = security.SecurityBlockController(tmp_path / "state", runner=host.run, clock=lambda: host.now)
    ctl.configure({"enabled": False, "ai_anomaly_enabled": False, "duration_seconds": 900,
                   "window_seconds": 300, "ssh_threshold": 20, "web_threshold": 30,
                   "allowlist_cidrs": [], "auto_escalate": False, "protected_ip": "117.141.246.34"})
    return ctl, host


def test_parse_decoy_hits_extracts_only_metadata():
    hits = security.parse_decoy_hits([
        decoy_line("203.0.113.9", "/.env", ua="sqlmap/1.7"),
        "not a log line",
        "",
    ])
    assert len(hits) == 1
    assert hits[0]["ip"] == "203.0.113.9"
    assert hits[0]["path"] == "/.env"
    assert hits[0]["user_agent"] == "sqlmap/1.7"
    assert set(hits[0]) == {"ip", "occurred_at", "method", "path", "http_status", "bytes", "user_agent"}


def test_summarize_decoy_hits_groups_by_source_and_paths():
    summary = security.summarize_decoy_hits(security.parse_decoy_hits([
        decoy_line("203.0.113.9", "/.env"),
        decoy_line("203.0.113.9", "/.env"),
        decoy_line("203.0.113.9", "/wp-login.php"),
        decoy_line("198.51.100.4", "/.git/config"),
    ]))
    top = summary["203.0.113.9"]
    assert top["count"] == 3
    assert top["path_count"] == 2
    assert top["paths"][0] == {"path": "/.env", "count": 2}
    assert top["first_seen"] and top["last_seen"]
    assert summary["198.51.100.4"]["count"] == 1


def test_decoy_status_reports_container_hits_and_members(decoy_env):
    ctl, host = decoy_env
    snapshot = ctl.decoy_status()
    assert snapshot["container_running"] is True
    assert snapshot["hit_total"] == 3
    assert snapshot["source_total"] == 2
    assert snapshot["chain_order_ok"] is True
    assert snapshot["redirect_chain"] == "PRISM-DECOY-IN"
    assert snapshot["errors"] == []


def test_decoy_status_flags_wrong_chain_order(decoy_env, monkeypatch):
    ctl, host = decoy_env
    host.chain_order = "drop_first"
    snapshot = ctl.decoy_status()
    assert snapshot["chain_order_ok"] is False
    assert any("排在 DROP 链之后" in item for item in snapshot["errors"])


def test_decoy_apply_refuses_when_chain_order_is_wrong(decoy_env):
    """链序不合法时必须拒绝写入任何规则——否则流量会在 DROP 处被吞掉。"""
    ctl, host = decoy_env
    host.chain_order = "drop_first"
    with pytest.raises(RuntimeError, match="拒绝写入引流规则"):
        ctl.decoy_apply({})
    assert host.decoy_sets["prism-decoy-v4"] == set()


def test_decoy_apply_adds_public_sources_and_skips_protected(decoy_env, monkeypatch):
    ctl, host = decoy_env
    # 203.0.113.0/24 属文档保留段（Python 视为 private），因此作为"应被拒绝"的样本；
    # 8.8.4.4 是真实公网地址，用来验证正常引流路径。
    monkeypatch.setattr(ctl, "_protected_sources", lambda policy: [{"cidr": "8.8.4.4/32", "reason": "当前管理员连接"}])
    result = ctl.decoy_apply({"reason": "诱捕命中累计"})
    applied = {row["ip"] for row in result["applied"]}
    skipped = {row["ip"]: row["reason"] for row in result["skipped"]}
    assert applied == {"9.9.9.9"}
    assert skipped.get("8.8.4.4") == "受保护来源"
    assert host.decoy_sets["prism-decoy-v4"] == {"9.9.9.9"}
    assert all(row["lease_seconds"] == security.ESCALATION_MAX_SECONDS for row in result["applied"])


def test_decoy_apply_skips_non_public_and_loopback(decoy_env, monkeypatch):
    ctl, host = decoy_env
    log = Path(security.DECOY_HIT_LOG)
    log.write_text("\n".join([
        decoy_line("10.0.0.9", "/.env"),
        decoy_line("127.0.0.1", "/.env"),
        decoy_line("8.8.4.4", "/.env"),
    ]) + "\n", encoding="utf-8")
    monkeypatch.setattr(ctl, "_protected_sources", lambda policy: [])
    result = ctl.decoy_apply({})
    applied = {row["ip"] for row in result["applied"]}
    skipped = {row["ip"]: row["reason"] for row in result["skipped"]}
    assert applied == {"8.8.4.4"}
    assert skipped.get("10.0.0.9") == "非公网来源"
    assert skipped.get("127.0.0.1") == "非公网来源"


def test_decoy_apply_is_read_only_when_log_missing(tmp_path, monkeypatch):
    host = DecoyHost()
    monkeypatch.setattr(security.shutil, "which", lambda name: name if name in host.which else None)
    monkeypatch.setattr(security, "DECOY_HIT_LOG", str(tmp_path / "missing.log"))
    ctl = security.SecurityBlockController(tmp_path / "state", runner=host.run, clock=lambda: host.now)
    result = ctl.decoy_apply({})
    assert result["applied"] == []
    assert result["hit_sources"] == 0
    assert result["errors"] == []


def test_execute_rejects_unknown_params_for_decoy_actions(tmp_path, monkeypatch):
    monkeypatch.setattr(security, "STATE_DIR", tmp_path)
    with pytest.raises(ValueError, match="不接收参数"):
        security.execute("security_decoy_status", {"enabled": True})
    with pytest.raises(ValueError, match="只接受 reason"):
        security.execute("security_decoy_apply", {"reason": "x", "ips": ["1.1.1.1"]})


def test_decoy_install_creates_chain_before_drop_chain(decoy_env):
    """引流链必须先插到 INPUT/DOCKER-USER 的第一条，否则会在 DROP 处终止。"""
    ctl, host = decoy_env
    result = ctl.decoy_install()
    assert result["chain_order_ok"] is True
    assert result["decoy_port"] == 8443
    inserted = [cmd for cmd in host.commands if "-I" in cmd and "PRISM-DECOY-IN" in cmd]
    parents = {cmd[cmd.index("-I") + 1] for cmd in inserted}
    assert parents == {"INPUT", "DOCKER-USER"}
    dnat = [cmd for cmd in host.commands if "DNAT" in cmd]
    assert dnat, "必须写出 DNAT 引流规则"
    targets = set()
    for cmd in dnat:
        assert "--to-destination" in cmd
        targets.add(cmd[cmd.index("--to-destination") + 1])
    # 按地址族分别使用回环目标：IPv4 127.0.0.1，IPv6 [::1]（ip6tables 不接受 127.0.0.1）
    assert targets == {"127.0.0.1:8443", "[::1]:8443"}, targets


def test_decoy_install_only_targets_web_ports(decoy_env):
    """只引流 80/443；其它端口（例如 SSH）绝不改写到诱捕层。"""
    ctl, host = decoy_env
    ctl.decoy_install()
    dnat_ports = {cmd[cmd.index("--dport") + 1] for cmd in host.commands if "DNAT" in cmd}
    assert dnat_ports == {"80", "443"}
    assert "22" not in dnat_ports


def test_decoy_install_is_idempotent_when_chain_exists(decoy_env):
    """链已存在时不重建，只校验链序（避免重复插入导致规则膨胀）。"""
    ctl, host = decoy_env
    host.decoy_chain_exists = True
    result = ctl.decoy_install()
    assert result["chain_order_ok"] is True
    assert not [cmd for cmd in host.commands if "-N" in cmd and "PRISM-DECOY-IN" in cmd]


def test_decoy_install_rejects_when_chain_order_is_wrong(decoy_env):
    ctl, host = decoy_env
    host.chain_order = "drop_first"
    result = ctl.decoy_install()
    assert result["chain_order_ok"] is False
    assert any("排在 DROP 链之后" in item for item in result["errors"])
