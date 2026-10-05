"""来源溯源、防御面审计与流量元数据摘要的只读边界测试。

命令模拟器只返回结构化的假数据，不触碰宿主机日志、防火墙或网络。
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

MODULE = Path(__file__).resolve().parents[1] / "prism_security_block.py"
SPEC = importlib.util.spec_from_file_location("prism_security_block", MODULE)
assert SPEC and SPEC.loader
security = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(security)


def ssh_line(ip: str, user: str = "root", micros: int = 1_800_000_000_000_000) -> str:
    return json.dumps({
        "__REALTIME_TIMESTAMP": str(micros),
        "__CURSOR": f"c-{ip}-{user}-{micros}",
        "MESSAGE": f"Failed password for invalid user {user} from {ip} port 51022 ssh2",
    })


def web_line(ip: str, path: str, status: str = "404", stamp: str = "2026-10-05T10:00:00+00:00") -> str:
    return f'{ip} - - [{stamp}] "GET {path} HTTP/1.1" {status} 153 "-" "curl/8.0"'


class Host:
    """记录命令并返回可控输出的模拟器（不执行任何真实系统命令）。"""

    def __init__(self, *, ssh_lines: list[str] | None = None, web_lines: list[str] | None = None) -> None:
        self.commands: list[list[str]] = []
        self.ssh_lines = ssh_lines if ssh_lines is not None else [ssh_line("45.155.205.7"), ssh_line("45.155.205.7", "admin")]
        self.web_lines = web_lines if web_lines is not None else [
            web_line("45.155.205.7", "/.env"), web_line("45.155.205.7", "/.git/config"),
            web_line("45.155.205.7", "/.ssh/id_rsa"),
        ]
        self.now = 1_800_000_000.0
        self.which: set[str] = {
            "ipset", "iptables", "ip6tables", "ip", "ss", "docker", "journalctl", "getent", "curl", "whois",
            "fail2ban", "clamav", "nmap", "suricata", "zeek",
        }

    def run(self, args, **_kwargs):
        self.commands.append(list(args))
        ok = {"exit_code": 0, "stdout": "", "stderr": ""}
        if args[0] == "journalctl":
            return {**ok, "stdout": "\n".join(self.ssh_lines)}
        if args[0] == "docker":
            return {**ok, "stdout": "\n".join(self.web_lines)}
        if args[0] == "ip":
            return {**ok, "stdout": json.dumps([{"addr_info": [{"local": "9.9.9.9"}]}])}
        if args[0] == "ss":
            if "-lntup" in args:
                return {**ok, "stdout": (
                    'tcp LISTEN 0 128 0.0.0.0:443 0.0.0.0:* users:(("nginx",pid=1,fd=6))\n'
                    'tcp LISTEN 0 128 127.0.0.1:8000 0.0.0.0:* users:(("uvicorn",pid=2,fd=9))\n'
                    'tcp LISTEN 0 128 [::]:22 [::]:* users:(("sshd",pid=3,fd=3))\n'
                )}
            if "-tunap" in args:
                return {**ok, "stdout": (
                    'tcp ESTAB 0 0 10.0.0.5:443 45.155.205.7:51022 users:(("nginx",pid=1,fd=6))\n'
                    'tcp ESTAB 0 0 10.0.0.5:8000 127.0.0.1:40122 users:(("uvicorn",pid=2,fd=9))\n'
                    'tcp LISTEN 0 128 *:8080 *:*\n'
                )}
            return {**ok, "stdout": f"0 0 9.9.9.9:22 1.1.1.1:52000\n"}
        if args[0] in {"iptables", "ip6tables"}:
            chain = args[args.index("-S") + 1]
            return {**ok, "stdout": f"-P {chain} ACCEPT\n-A {chain} -j PRISM-SEC-IN\n"}
        if args[0] == "getent":
            return {**ok, "stdout": "45.155.205.7 vps.example.net"}
        if args[0] == "curl":
            body = {"status": "success", "country": "Netherlands", "regionName": "North Holland",
                    "city": "Amsterdam", "isp": "Example Hosting BV", "org": "Example Hosting",
                    "as": "AS64500 Example Hosting BV", "query": "45.155.205.7"}
            return {**ok, "stdout": json.dumps(body), "exit_code": 0}
        if args[0] == "whois":
            return {**ok, "stdout": "netname: EXAMPLE-NET\ncountry: NL\norgname: Example Hosting BV\nroute: 45.155.205.0/24\n"}
        return ok


@pytest.fixture
def controller(tmp_path, monkeypatch):
    host = Host()
    monkeypatch.setattr(security.shutil, "which", lambda name: name if name in host.which else None)
    monkeypatch.delenv("SECURITY_BLOCK_PROTECTED_CIDRS", raising=False)
    monkeypatch.delenv("THREAT_INTEL_BASE_URL", raising=False)
    ctl = security.SecurityBlockController(tmp_path, runner=host.run, clock=lambda: host.now)
    return ctl, host


def test_ssh_attacker_summary_counts_and_accounts():
    summary = security.parse_ssh_attacker_summary([
        ssh_line("45.155.205.7", "root", 1_800_000_000_000_000),
        ssh_line("45.155.205.7", "admin", 1_800_000_060_000_000),
        "not json",
        ssh_line("203.0.113.9", "root", 1_800_000_120_000_000),
    ])
    assert summary["45.155.205.7"]["count"] == 2
    assert summary["45.155.205.7"]["accounts_tried"] == [
        {"account": "admin", "count": 1}, {"account": "root", "count": 1},
    ]
    assert summary["45.155.205.7"]["first_seen"] is not None
    assert summary["45.155.205.7"]["last_seen"] >= summary["45.155.205.7"]["first_seen"]
    assert summary["203.0.113.9"]["count"] == 1


def test_web_attacker_summary_only_accepts_sensitive_paths():
    summary = security.parse_web_attacker_summary([
        web_line("45.155.205.7", "/.env"),
        web_line("45.155.205.7", "/.env?debug=1"),
        web_line("45.155.205.7", "/index.html", "200"),
        "broken line",
    ])
    assert summary["45.155.205.7"]["count"] == 2
    assert {row["path"] for row in summary["45.155.205.7"]["targets"]} == {"/.env"}
    assert summary["45.155.205.7"]["status_codes"] == {"404": 2}


def test_ip_trace_scores_local_evidence_and_marks_external_attribution_separately(controller):
    ctl, host = controller
    result = ctl.ip_trace({"ip": "45.155.205.7"})
    assert result["ip"] == "45.155.205.7"
    assert result["is_public"] is True
    assert result["ssh"]["count"] == 2
    assert result["web"]["count"] == 3
    assert result["web"]["target_count"] == 3
    # SSH 失败 3 分 + 敏感探测 3 分 + 双类证据 2 分 + 三目标 1 分 = 9 分
    assert result["risk"]["score"] == 90
    assert result["risk"]["level"] == "critical"
    assert "不含模型推断" in result["risk"]["basis"]
    assert result["attribution"]["ok"] is True
    assert result["attribution"]["attribution"]["country"] == "Netherlands"
    assert result["attribution"]["attribution"]["isp"] == "Example Hosting BV"
    assert result["reverse_dns"]["ok"] is True
    assert "country=NL" in result["whois"]["summary"]
    assert result["evidence_sources"] == ["journalctl -u sshd", "docker logs cr_frontend"]


def test_ip_trace_is_read_only_and_never_sends_probe_packets(controller):
    ctl, host = controller
    ctl.ip_trace({"ip": "45.155.205.7"})
    for command in host.commands:
        assert command[0] in {"journalctl", "docker", "ip", "ss", "getent", "curl", "whois"}
    # 只允许一次出网（威胁情报），且 URL 中只出现被溯源 IP。
    curl_commands = [command for command in host.commands if command[0] == "curl"]
    assert len(curl_commands) == 1
    # 默认情报端点为 ipinfo（HTTPS，生产实测可用性优于 ip-api 的 80 端口）
    assert curl_commands[0][-1] == "https://ipinfo.io/45.155.205.7/json"


def test_attribution_accepts_ipinfo_response_shape(controller):
    """ipinfo/ipwho.is 的 region/org/asn 字段也要能归一化，避免换端点后归因为空。"""
    ctl, host = controller

    def run(args, **kwargs):
        if args[0] == "curl":
            host.commands.append(list(args))
            return {"exit_code": 0, "stdout": json.dumps({
                "ip": "45.155.205.7", "city": "Amsterdam", "region": "North Holland",
                "country": "NL", "org": "AS64500 Example Hosting BV", "asn": "64500",
            }), "stderr": ""}
        return host.run(args, **kwargs)

    ctl.runner = run
    monkeypatch_base = security.os.environ.get("THREAT_INTEL_BASE_URL")
    security.os.environ["THREAT_INTEL_BASE_URL"] = "https://ipinfo.io"
    try:
        result = ctl._outbound_attribution("45.155.205.7")
    finally:
        if monkeypatch_base is None:
            security.os.environ.pop("THREAT_INTEL_BASE_URL", None)
        else:
            security.os.environ["THREAT_INTEL_BASE_URL"] = monkeypatch_base
    assert result["ok"] is True
    assert result["source"] == "ipinfo.io"
    assert result["attribution"]["region"] == "North Holland"
    assert result["attribution"]["org"] == "AS64500 Example Hosting BV"
    assert result["attribution"]["as"] == "AS64500"
    assert host.commands[-1][-1] == "https://ipinfo.io/45.155.205.7/json"



def test_ip_trace_rejects_networks_and_non_global_without_outbound(controller):
    ctl, host = controller
    result = ctl.ip_trace({"ip": "10.0.0.8"})
    assert result["is_public"] is False
    assert result["attribution"]["ok"] is False
    assert [command for command in host.commands if command[0] == "curl"] == []
    with pytest.raises(ValueError, match="禁止 CIDR"):
        ctl.ip_trace({"ip": "10.0.0.0/8"})


def test_ip_trace_marks_protected_sources_and_empty_evidence(tmp_path, monkeypatch):
    host = Host(ssh_lines=[], web_lines=[])
    monkeypatch.setattr(security.shutil, "which", lambda name: name if name in host.which else None)
    monkeypatch.delenv("SECURITY_BLOCK_PROTECTED_CIDRS", raising=False)
    ctl = security.SecurityBlockController(tmp_path, runner=host.run, clock=lambda: host.now)
    monkeypatch.setattr(ctl, "_protected_sources", lambda policy: [{"cidr": "45.155.205.0/24", "reason": "管理员配置白名单"}])
    result = ctl.ip_trace({"ip": "45.155.205.7"})
    assert result["is_protected"] is True
    assert any("受保护地址" in reason for reason in result["risk"]["reasons"])
    assert any("没有任何" in reason or "没有该来源" in reason for reason in result["risk"]["reasons"])


def test_surface_audit_reports_listeners_and_installed_applications(controller):
    ctl, host = controller
    result = ctl.surface_audit()
    assert result["public_listeners"], "应识别监听在 0.0.0.0 的端口"
    assert {row["port"] for row in result["public_listeners"]} == {"443", "22"}
    assert [row["port"] for row in result["public_listeners"] if row["address"] == "::"] == ["22"]
    assert "127.0.0.1" not in {row["address"] for row in result["public_listeners"]}
    assert result["firewall"]["ipv4"]["tool"] == "iptables"
    assert result["firewall"]["ipv4"]["chains"][0]["ok"] is True
    applications = {row["name"] for row in result["applications"] if row["installed"]}
    assert {"fail2ban", "clamav", "nmap", "suricata", "zeek", "ipset"} <= applications
    assert result["blocking"]["backend"] == "ipset"
    assert result["ssh_ports"] == "22"


def test_surface_audit_never_installs_or_modifies(controller):
    ctl, host = controller
    ctl.surface_audit()
    forbidden = {"yum", "dnf", "apt", "apt-get", "systemctl", "bash", "sh"}
    assert not [command for command in host.commands if command[0] in forbidden]


def test_traffic_summary_exposes_metadata_without_payload(controller):
    ctl, host = controller
    result = ctl.traffic_summary({"since_hours": 24})
    assert result["payload_captured"] is False
    assert result["window_hours"] == 24
    peers = {row["ip"]: row for row in result["peers"]}
    assert peers["45.155.205.7"]["connections"] == 1
    assert peers["45.155.205.7"]["ssh_failed_count"] == 2
    assert peers["45.155.205.7"]["sensitive_probe_count"] == 3
    assert peers["45.155.205.7"]["target_count"] == 3
    assert peers["127.0.0.1"]["connections"] == 1
    assert result["current_connections"] == 2
    assert result["recent_ssh_failed_sources"] == 1
    assert result["recent_probe_sources"] == 1


def test_protected_sources_include_established_public_peer_when_public_ip_is_not_on_nic(tmp_path, monkeypatch):
    """云主机公网地址不绑网卡时，非 SSH 端口的公网对端也必须受保护。"""
    host = Host()
    monkeypatch.setattr(security.shutil, "which", lambda name: name if name in host.which else None)
    monkeypatch.delenv("SECURITY_BLOCK_PROTECTED_CIDRS", raising=False)

    def run(args, **kwargs):
        # 模拟：SSH 日志里没有 Accepted 记录，但有一条 HTTPS 管理会话在连着。
        if args[0] == "journalctl":
            host.commands.append(list(args))
            return {"exit_code": 0, "stdout": "", "stderr": ""}
        if args[0] == "ip":
            return {"exit_code": 0, "stdout": json.dumps([{"addr_info": [{"local": "10.0.0.5"}]}]), "stderr": ""}
        if args[0] == "ss" and "established" in args:
            host.commands.append(list(args))
            return {"exit_code": 0, "stdout": "0 0 10.0.0.5:443 1.1.1.1:52000\n", "stderr": ""}
        return host.run(args, **kwargs)

    ctl = security.SecurityBlockController(tmp_path, runner=run, clock=lambda: host.now)
    sources = ctl._protected_sources({"allowlist_cidrs": [], "protected_ip": ""})
    reasons = {row["cidr"]: row["reason"] for row in sources}
    assert reasons.get("1.1.1.1/32") == "与本机已建立连接的公网对端"
    assert reasons.get("10.0.0.5/32") == "服务器本机地址"


def test_protected_sources_keep_ssh_peer_but_skip_private_web_peer(tmp_path, monkeypatch):
    """SSH 管理端口对端一律保护；非 SSH 端口的内网对端不进保护清单。"""
    host = Host()
    monkeypatch.setattr(security.shutil, "which", lambda name: name if name in host.which else None)
    monkeypatch.delenv("SECURITY_BLOCK_PROTECTED_CIDRS", raising=False)

    def run(args, **kwargs):
        if args[0] == "journalctl":
            return {"exit_code": 0, "stdout": "", "stderr": ""}
        if args[0] == "ip":
            return {"exit_code": 0, "stdout": json.dumps([{"addr_info": [{"local": "10.0.0.5"}]}]), "stderr": ""}
        if args[0] == "ss" and "established" in args:
            return {"exit_code": 0, "stdout": (
                "0 0 10.0.0.5:22 10.0.0.9:52000\n"
                "0 0 10.0.0.5:443 10.0.0.11:52100\n"
            ), "stderr": ""}
        return host.run(args, **kwargs)

    ctl = security.SecurityBlockController(tmp_path, runner=run, clock=lambda: host.now)
    sources = ctl._protected_sources({"allowlist_cidrs": [], "protected_ip": ""})
    reasons = {row["cidr"]: row["reason"] for row in sources}
    assert reasons.get("10.0.0.9/32") == "当前 SSH 管理连接"
    assert "10.0.0.11/32" not in reasons


def test_execute_rejects_extra_parameters_and_unknown_actions(tmp_path, monkeypatch):
    monkeypatch.setattr(security, "STATE_DIR", tmp_path)
    with pytest.raises(ValueError, match="只接受单个 ip"):
        security.execute("security_ip_trace", {"ip": "1.1.1.1", "command": "id"})
    with pytest.raises(ValueError, match="不接收参数"):
        security.execute("security_surface_audit", {"port": 22})
    with pytest.raises(ValueError, match="只接受 since_hours"):
        security.execute("security_traffic_summary", {"since_hours": 24, "capture": True})
    with pytest.raises(ValueError, match="未知安全防御动作"):
        security.execute("security_nuke", {})
