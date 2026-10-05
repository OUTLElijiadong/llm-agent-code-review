"""限时自动封禁的 root 边界；命令模拟器不触碰宿主机防火墙。"""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path

import pytest

MODULE = Path(__file__).resolve().parents[1] / "prism_security_block.py"
SPEC = importlib.util.spec_from_file_location("prism_security_block", MODULE)
assert SPEC and SPEC.loader
security = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(security)


from tests.security_host import Host



@pytest.fixture
def controller(tmp_path, monkeypatch):
    host = Host()
    monkeypatch.setattr(security.shutil, "which", lambda name: name)
    monkeypatch.delenv("SECURITY_BLOCK_PROTECTED_CIDRS", raising=False)
    ctl = security.SecurityBlockController(tmp_path, runner=host.run, clock=lambda: host.now)
    return ctl, host


def config(**extra):
    return {"enabled": True, "ai_anomaly_enabled": False, "duration_seconds": 300, "window_seconds": 300,
            "ssh_threshold": 20, "web_threshold": 30, "allowlist_cidrs": [], "auto_escalate": False,
            "protected_ip": "1.1.1.1", **extra}


def test_deployment_config_reads_only_allowlisted_values_without_exporting_secrets(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text(
        'API_KEY=do-not-load\n'
        'SECURITY_BLOCK_PROTECTED_CIDRS=\'["192.0.2.0/24"]\'\n'
        "SECURITY_BLOCK_SSH_PORTS='22,2222'\n",
        encoding="utf-8",
    )
    env_file.chmod(0o600)
    monkeypatch.setattr(security, "DEPLOY_DIR", tmp_path)
    monkeypatch.delenv("SECURITY_BLOCK_PROTECTED_CIDRS", raising=False)
    monkeypatch.delenv("SECURITY_BLOCK_SSH_PORTS", raising=False)
    monkeypatch.delenv("SECURITY_BLOCK_STATE_DIR", raising=False)
    monkeypatch.delenv("API_KEY", raising=False)

    assert security._deployment_setting("SECURITY_BLOCK_SSH_PORTS", "22") == "22,2222"
    assert security._deployment_setting("SECURITY_BLOCK_PROTECTED_CIDRS") == '["192.0.2.0/24"]'
    assert "API_KEY" not in os.environ
    assert security._deployment_setting("SECURITY_BLOCK_STATE_DIR", "/default") == "/default"


def test_deployment_config_rejects_duplicate_or_insecure_file(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    monkeypatch.setattr(security, "DEPLOY_DIR", tmp_path)
    monkeypatch.delenv("SECURITY_BLOCK_SSH_PORTS", raising=False)
    env_file.write_text("SECURITY_BLOCK_SSH_PORTS=22\nSECURITY_BLOCK_SSH_PORTS=2222\n")
    env_file.chmod(0o600)
    with pytest.raises(RuntimeError, match="重复"):
        security._deployment_setting("SECURITY_BLOCK_SSH_PORTS")

    env_file.write_text("SECURITY_BLOCK_SSH_PORTS=22\n")
    env_file.chmod(0o644)
    with pytest.raises(RuntimeError, match="不能对组或其他用户开放"):
        security._deployment_setting("SECURITY_BLOCK_SSH_PORTS")


def evidence(ip="8.8.8.8", rule="ssh_failed_password", count=20, start=1_800_000_001):
    return [{"ip": ip, "rule": rule, "key": f"{rule}:{start}:{i}", "occurred_at": start + i,
             "target": "/.env" if i % 3 == 0 else "/.git/config" if i % 3 == 1 else "/.git/HEAD"}
            for i in range(count)]


@pytest.mark.parametrize("params", [config(duration_seconds=0), config(duration_seconds=3601),
    config(enabled=1), config(ssh_threshold=19), config(web_threshold=29),
    config(protected_ip=""), config(allowlist_cidrs=["0.0.0.0/0"]),
    config(allowlist_cidrs=["2001:4860::/32"]), config(allowlist_cidrs=["8.8.8.0/24"] * 33)])
def test_config_rejects_invalid_before_any_mutation(controller, params):
    ctl, host = controller
    with pytest.raises(ValueError):
        ctl.configure(params)
    assert not any(a[0] == "ipset" and a[1] in {"create", "add", "del", "flush"} for a in host.commands)


def test_default_disabled_and_no_unbounded_firewall_operations(controller):
    ctl, host = controller
    status = ctl.status()
    assert status["enabled"] is False
    ctl.reconcile()
    assert not any(a[0] == "ipset" and a[1] == "add" for a in host.commands)


def test_enable_installs_both_paths_and_protects_sources(controller):
    ctl, host = controller
    result = ctl.configure(config(allowlist_cidrs=["8.8.8.0/24"]))
    assert result["verified"] is True
    assert result["enabled"] is True
    assert result["policy"]["activated_at"] is not None
    assert {item["cidr"] for item in result["protected_sources"]} >= {"1.1.1.1/32", "9.9.9.9/32", "8.8.4.4/32", "8.8.8.0/24"}
    assert any("INPUT" in args and "-I" in args for args in host.commands)
    assert any("DOCKER-USER" in args and "-I" in args for args in host.commands)
    assert not any("-P" in args or "--permanent" in args for args in host.commands)


@pytest.mark.parametrize("ip", ["1.1.1.1", "8.8.4.4", "9.9.9.9", "127.0.0.1", "10.2.3.4", "0.0.0.0", "224.0.0.1", "8.8.8.0/24"])
def test_protected_or_nonpublic_sources_are_not_blocked(controller, monkeypatch, ip):
    ctl, host = controller
    ctl.configure(config())
    host.now += 30
    monkeypatch.setattr(ctl, "_collect_evidence", lambda *_args: (evidence(ip), []))
    result = ctl.reconcile()
    assert result["active_blocks"] == []


def test_verified_active_and_no_extension_for_same_evidence(controller, monkeypatch):
    ctl, host = controller
    ctl.configure(config())
    host.now += 30
    monkeypatch.setattr(ctl, "_collect_evidence", lambda *_args: (evidence(), []))
    first = ctl.reconcile()["active_blocks"][0]
    expiry = first["expires_at"]
    host.now += 10
    second = ctl.reconcile()["active_blocks"][0]
    assert second["expires_at"] == expiry
    assert sum(args[:2] == ["ipset", "add"] for args in host.commands) == 1


def test_native_expiry_and_old_evidence_cannot_reban(controller, monkeypatch):
    ctl, host = controller
    ctl.configure(config())
    host.now += 30
    monkeypatch.setattr(ctl, "_collect_evidence", lambda *_args: (evidence(), []))
    ctl.reconcile()
    host.now += 301
    result = ctl.reconcile()
    assert result["active_blocks"] == []
    assert result["recent_blocks"][0]["status"] == "expired"
    assert sum(args[:2] == ["ipset", "add"] for args in host.commands) == 1


def test_release_and_new_evidence_required(controller, monkeypatch):
    ctl, host = controller
    ctl.configure(config())
    host.now += 30
    monkeypatch.setattr(ctl, "_collect_evidence", lambda *_args: (evidence(), []))
    ctl.reconcile()
    result = ctl.release({"ip": "8.8.8.8", "reason": "管理员确认误报"})
    assert result["active_blocks"] == []
    assert result["recent_blocks"][0]["status"] == "released"
    assert ctl.reconcile()["active_blocks"] == []
    host.now += 30
    monkeypatch.setattr(ctl, "_collect_evidence", lambda *_args: (evidence(start=1_800_000_031), []))
    assert len(ctl.reconcile()["active_blocks"]) == 1


def test_pre_activation_events_never_block(controller, monkeypatch):
    ctl, host = controller
    ctl.configure(config())
    monkeypatch.setattr(ctl, "_collect_evidence", lambda *_args: (evidence(start=int(host.now)-60), []))
    assert ctl.reconcile()["active_blocks"] == []


def test_web_requires_distinct_sensitive_targets(controller, monkeypatch):
    ctl, host = controller
    ctl.configure(config())
    host.now += 60
    items = evidence(rule="web_sensitive_probe", count=30)
    for item in items:
        item["target"] = "/.env"
    monkeypatch.setattr(ctl, "_collect_evidence", lambda *_args: (items, []))
    assert ctl.reconcile()["active_blocks"] == []
    monkeypatch.setattr(ctl, "_collect_evidence", lambda *_args: (evidence(rule="web_sensitive_probe", count=30), []))
    assert len(ctl.reconcile()["active_blocks"]) == 1


def test_command_failure_never_claims_active(controller, monkeypatch):
    ctl, host = controller
    ctl.configure(config())
    host.now += 30
    host.fail_add = True
    monkeypatch.setattr(ctl, "_collect_evidence", lambda *_args: (evidence(), []))
    status = ctl.reconcile()
    assert status["active_blocks"] == []
    assert status["recent_blocks"][0]["status"] == "failed"
    assert status["errors"]


def test_disabled_clears_only_own_sets(controller, monkeypatch):
    ctl, host = controller
    ctl.configure(config())
    host.now += 30
    monkeypatch.setattr(ctl, "_collect_evidence", lambda *_args: (evidence(), []))
    ctl.reconcile()
    assert ctl.configure(config(enabled=False))["active_blocks"] == []
    assert all(args[2].startswith("prism-sec-") for args in host.commands if args[:2] == ["ipset", "flush"])


def test_parser_ignores_invalid_user_and_generic_http_errors():
    timestamp = 1_800_000_001
    ssh = [json.dumps({"__REALTIME_TIMESTAMP": str(timestamp * 1_000_000), "MESSAGE": "Invalid user admin from 8.8.8.8 port 1200"}),
           json.dumps({"__REALTIME_TIMESTAMP": str(timestamp * 1_000_000), "MESSAGE": "Failed password for invalid user admin from 8.8.8.8 port 1200 ssh2"})]
    items = security.parse_ssh_evidence(ssh)
    assert len(items) == 1 and items[0]["rule"] == "ssh_failed_password"
    web = ['2027-01-15T08:00:01Z 8.8.8.8 - - [15/Jan/2027:08:00:01 +0000] "GET /api/me HTTP/1.1" 403 10',
           '2027-01-15T08:00:01Z 8.8.8.8 - - [15/Jan/2027:08:00:01 +0000] "GET /.env HTTP/1.1" 404 10']
    assert len(security.parse_web_evidence(web)) == 1


def test_root_json_protection_is_honored(controller, monkeypatch):
    ctl, host = controller
    monkeypatch.setenv("SECURITY_BLOCK_PROTECTED_CIDRS", '["8.8.8.8/32"]')
    ctl.configure(config())
    host.now += 30
    monkeypatch.setattr(ctl, "_collect_evidence", lambda *_args: (evidence(), []))
    assert ctl.reconcile()["active_blocks"] == []
    assert {row["cidr"] for row in ctl.status()["protected_sources"]} >= {"8.8.8.8/32"}


def test_ipv6_unsupported_does_not_extrapolate_ipv4_coverage(controller, monkeypatch):
    ctl, host = controller
    host.chains.remove(("ip6tables", "DOCKER-USER"))
    result = ctl.configure(config())
    assert result["available"] is True and result["family_support"] == {"ipv4": True, "ipv6": False}
    host.now += 30
    monkeypatch.setattr(ctl, "_collect_evidence", lambda *_args: (evidence(ip="2001:4860:4860::8888"), []))
    assert ctl.reconcile()["active_blocks"] == []


def test_ipv6_single_source_is_supported_when_both_paths_exist(controller, monkeypatch):
    ctl, host = controller
    ctl.configure(config())
    host.now += 30
    monkeypatch.setattr(ctl, "_collect_evidence", lambda *_args: (evidence(ip="2001:4860:4860::8888"), []))
    active = ctl.reconcile()["active_blocks"]
    assert active[0]["ip"] == "2001:4860:4860::8888"
    assert host.sets[security.SETS[6]]["entries"]


def test_new_allowlist_immediately_releases_existing_member(controller, monkeypatch):
    ctl, host = controller
    ctl.configure(config())
    host.now += 30
    monkeypatch.setattr(ctl, "_collect_evidence", lambda *_args: (evidence(), []))
    ctl.reconcile()
    result = ctl.configure(config(allowlist_cidrs=["8.8.8.8/32"]))
    assert result["active_blocks"] == []
    assert result["recent_blocks"][0]["status"] == "released"


def test_ai_candidates_are_sanitized_and_native_ttl_is_bounded(controller, monkeypatch):
    ctl, host = controller
    ctl.configure(config(ai_anomaly_enabled=True, duration_seconds=900))
    host.now += 30
    items = evidence(count=10)
    monkeypatch.setattr(ctl, "_collect_evidence", lambda *_args: (items, []))
    assert ctl.reconcile()["active_blocks"] == []  # 不够常规规则阈值。
    candidate = ctl.candidates()["candidates"][0]
    assert "ip" not in candidate and "target" not in candidate and "_keys" not in candidate
    result = ctl.apply_anomalies({"decisions": [{"candidate_id": candidate["candidate_id"], "reason": "短窗持续认证失败，先短时隔离"}]})
    active = result["active_blocks"][0]
    assert active["source"] == "xiaoling_anomaly"
    assert security._epoch(active["expires_at"]) - security._epoch(active["started_at"]) == 120
    assert host.sets[security.SETS[4]]["entries"]["8.8.8.8"] == host.now + 120


@pytest.mark.parametrize("mutation", ["expired", "removed", "protected"])
def test_ai_candidate_is_rechecked_on_apply(controller, monkeypatch, mutation):
    ctl, host = controller
    ctl.configure(config(ai_anomaly_enabled=True))
    host.now += 30
    monkeypatch.setattr(ctl, "_collect_evidence", lambda *_args: (evidence(count=10), []))
    candidate = ctl.candidates()["candidates"][0]
    if mutation == "expired":
        host.now += 61
    elif mutation == "removed":
        monkeypatch.setattr(ctl, "_collect_evidence", lambda *_args: ([], []))
    else:
        host.ssh_peer = "8.8.8.8"
    with pytest.raises(ValueError):
        ctl.apply_anomalies({"decisions": [{"candidate_id": candidate["candidate_id"], "reason": "小菱异常"}]})
    assert not any(args[:2] == ["ipset", "add"] for args in host.commands)


def test_ai_is_disabled_by_default_and_cannot_choose_arbitrary_ip(controller):
    ctl, _host = controller
    ctl.configure(config())
    assert ctl.candidates()["candidates"] == []
    with pytest.raises(ValueError):
        ctl.apply_anomalies({"decisions": [{"candidate_id": "a" * 32, "ip": "8.8.8.8", "reason": "异常"}]})
    with pytest.raises(RuntimeError):
        ctl.apply_anomalies({"decisions": [{"candidate_id": "a" * 32, "reason": "异常"}]})


def test_ai_web_requires_three_distinct_targets(controller, monkeypatch):
    ctl, host = controller
    ctl.configure(config(ai_anomaly_enabled=True))
    host.now += 30
    items = evidence(rule="web_sensitive_probe", count=10)
    for row in items:
        row["target"] = "/.env"
    monkeypatch.setattr(ctl, "_collect_evidence", lambda *_args: (items, []))
    assert ctl.candidates()["candidates"] == []


def test_fsync_state_failure_prevents_kernel_mutation(controller, monkeypatch):
    ctl, host = controller
    ctl.configure(config())
    host.now += 30
    monkeypatch.setattr(ctl, "_collect_evidence", lambda *_args: (evidence(), []))
    def fail_write(_state):
        raise OSError("disk full")
    monkeypatch.setattr(ctl, "_write", fail_write)
    with pytest.raises(OSError):
        ctl.reconcile()
    assert not any(args[:2] == ["ipset", "add"] for args in host.commands)


def test_missing_docker_chain_refuses_enable(controller):
    ctl, host = controller
    host.chains.remove(("iptables", "DOCKER-USER"))
    assert ctl.status()["available"] is False
    with pytest.raises(RuntimeError):
        ctl.configure(config())
    assert not any(args[:2] == ["ipset", "add"] for args in host.commands)


def test_git_head_normalization_is_a_sensitive_target():
    line = '2027-01-15T08:00:01Z 8.8.8.8 - - [15/Jan/2027:08:00:01 +0000] "GET /.git/HEAD HTTP/1.1" 404 10'
    result = security.parse_web_evidence([line])
    assert result[0]["target"] == "/.git/head"


def test_recent_successful_ssh_source_is_protected(controller, monkeypatch):
    ctl, host = controller
    old_runner = ctl.runner
    def with_accepted(args, **kwargs):
        if args[0] == "journalctl":
            return {"exit_code": 0, "stderr": "", "stdout": json.dumps({
                "__REALTIME_TIMESTAMP": str(int(host.now - 60) * 1_000_000),
                "MESSAGE": "Accepted publickey for user from 8.8.8.8 port 1200 ssh2"})}
        return old_runner(args, **kwargs)
    ctl.runner = with_accepted
    ctl.configure(config())
    host.now += 30
    monkeypatch.setattr(ctl, "_collect_evidence", lambda *_args: (evidence(), []))
    result = ctl.reconcile()
    assert result["active_blocks"] == []
    assert any(row["cidr"] == "8.8.8.8/32" and "成功登录" in row["reason"] for row in result["protected_sources"])


def test_unreadable_ssh_protection_fails_closed(controller):
    ctl, host = controller
    old_runner = ctl.runner
    def fail_journal(args, **kwargs):
        if args[0] == "journalctl":
            return {"exit_code": 1, "stderr": "journal unavailable", "stdout": ""}
        return old_runner(args, **kwargs)
    ctl.runner = fail_journal
    assert ctl.status()["verified"] is False
    with pytest.raises(RuntimeError):
        ctl.configure(config())
    assert not any(args[:2] == ["ipset", "add"] for args in host.commands)


def test_parallel_reconciliation_never_duplicate_or_extend_lease(controller, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    ctl, host = controller
    ctl.configure(config())
    host.now += 30
    monkeypatch.setattr(ctl, "_collect_evidence", lambda *_args: (evidence(), []))
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _value: ctl.reconcile(), range(2)))
    assert all(len(result["active_blocks"]) == 1 for result in results)
    assert sum(args[:2] == ["ipset", "add"] for args in host.commands) == 1


def test_candidate_id_is_stable_for_same_evidence_after_cache_expiry(controller, monkeypatch):
    ctl, host = controller
    ctl.configure(config(ai_anomaly_enabled=True))
    host.now += 30
    monkeypatch.setattr(ctl, "_collect_evidence", lambda *_args: (evidence(count=10), []))
    first = ctl.candidates()["candidates"][0]
    host.now += 61
    second = ctl.candidates()["candidates"][0]
    assert first["candidate_id"] == second["candidate_id"]
    assert second["expires_at"] > first["expires_at"]


def test_ai_whole_batch_validation_before_any_block(controller, monkeypatch):
    ctl, host = controller
    ctl.configure(config(ai_anomaly_enabled=True))
    host.now += 30
    monkeypatch.setattr(ctl, "_collect_evidence", lambda *_args: (evidence(count=10), []))
    candidate = ctl.candidates()["candidates"][0]
    with pytest.raises(ValueError):
        ctl.apply_anomalies({"decisions": [{"candidate_id": candidate["candidate_id"], "reason": "异常"},
                                           {"candidate_id": "a" * 32, "reason": "不存在的异常"}]})
    assert not any(args[:2] == ["ipset", "add"] for args in host.commands)


def test_audit_failure_before_mutation_never_installs_member(controller, monkeypatch):
    ctl, host = controller
    ctl.configure(config())
    host.now += 30
    monkeypatch.setattr(ctl, "_collect_evidence", lambda *_args: (evidence(), []))
    monkeypatch.setattr(ctl, "_audit", lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("audit disk full")))
    with pytest.raises(OSError):
        ctl.reconcile()
    assert not any(args[:2] == ["ipset", "add"] for args in host.commands)


def test_docker_jump_loss_is_reported_unknown_and_reconcile_reconnects(controller, monkeypatch):
    ctl, host = controller
    ctl.configure(config())
    host.now += 30
    monkeypatch.setattr(ctl, "_collect_evidence", lambda *_args: (evidence(), []))
    ctl.reconcile()
    host.rules.remove(("iptables", ("DOCKER-USER", "-j", security.CHAINS["DOCKER-USER"])))
    status = ctl.status()
    assert status["verified"] is False
    assert status["active_blocks"] == []
    assert status["recent_blocks"][0]["status"] == "unknown"
    fixed = ctl.reconcile()
    assert fixed["verified"] is True and len(fixed["active_blocks"]) == 1
    assert sum(args[:2] == ["ipset", "add"] for args in host.commands) == 1


def test_web_forwarded_ip_is_not_used_as_source():
    line = '2027-01-15T08:00:01Z 8.8.8.8 - - [15/Jan/2027:08:00:01 +0000] "GET /.env HTTP/1.1" 403 10 "-" "agent" "1.1.1.1"'
    assert security.parse_web_evidence([line])[0]["ip"] == "8.8.8.8"


def test_executor_parameter_contract_and_fixed_module_dispatch(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("security_executor_contract", MODULE.parent / "prism_ops_executor.py")
    assert spec and spec.loader
    executor = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(executor)
    assert executor.ACTION_PARAM_KEYS["security_block_reconcile"] == set()
    assert executor.ACTION_PARAM_KEYS["security_block_candidates"] == set()
    assert executor.ACTION_PARAM_KEYS["security_block_apply_anomalies"] == {"decisions"}
    assert "security_block_reconcile" not in executor.READ_ONLY_ACTIONS
    monkeypatch.setenv("SECURITY_BLOCK_STATE_DIR", str(tmp_path / "executor-state"))
    with pytest.raises(ValueError, match="未允许参数"):
        executor.execute("security_block_reconcile", {"ip": "8.8.8.8"})
    result = executor.execute("security_block_status", {})
    assert result["enabled"] is False and result["backend"] == "ipset"


def test_new_log_evidence_changes_candidate_identity(controller, monkeypatch):
    ctl, host = controller
    ctl.configure(config(ai_anomaly_enabled=True))
    host.now += 30
    monkeypatch.setattr(ctl, "_collect_evidence", lambda *_args: (evidence(count=10), []))
    first = ctl.candidates()["candidates"][0]
    monkeypatch.setattr(ctl, "_collect_evidence", lambda *_args: (evidence(count=11), []))
    second = ctl.candidates()["candidates"][0]
    assert first["candidate_id"] != second["candidate_id"]


def test_ipv6_kernel_set_failure_is_reported_without_disabling_ipv4(controller):
    ctl, host = controller
    original = ctl.runner
    def fail_ipv6(args, **kwargs):
        if args[:3] == ["ipset", "create", security.SETS[6]]:
            return {"exit_code": 1, "stdout": "", "stderr": "kernel family unsupported"}
        return original(args, **kwargs)
    ctl.runner = fail_ipv6
    result = ctl.configure(config())
    assert result["verified"] is True and result["family_support"]["ipv4"] is True
    assert result["family_support"]["ipv6"] is False
    assert any("IPv6" in error for error in result["errors"])
