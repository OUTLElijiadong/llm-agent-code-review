"""Root 运维执行器安全边界回归。"""

from __future__ import annotations

import importlib.util
import json
import os
import threading
import time
from pathlib import Path

import pytest

MODULE_PATH = Path(__file__).resolve().parents[1] / "prism_ops_executor.py"
SPEC = importlib.util.spec_from_file_location("prism_ops_executor", MODULE_PATH)
assert SPEC and SPEC.loader
executor = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(executor)


def test_rejects_unknown_parameters_before_any_operation() -> None:
    with pytest.raises(ValueError, match="未允许参数"):
        executor.execute("status", {"command": "id"}, request_id="request-unknown-01")


@pytest.mark.parametrize(
    ("action", "params"),
    [
        ("write_text_file", {"path": "/etc/systemd/system/prism-probe.service", "content": "[Service]"}),
        ("systemd_unit_action", {"unit": "prism-probe.service", "operation": "daemon_reload"}),
        ("package_action", {"operation": "install", "packages": ["attacker-package"]}),
        ("firewall_action", {"operation": "add", "target_type": "port", "value": "8621"}),
        ("account_action", {"operation": "create_system", "username": "prism_probe", "shell": "/bin/bash"}),
        ("ssh_authorized_key_action", {"operation": "add", "username": "root", "public_key": "ssh-ed25519 AAAA"}),
        ("docker_container_action", {"operation": "start", "container": "auto-surface-mm-local"}),
    ],
)
def test_unbounded_host_mutations_are_rejected_before_side_effects(action, params, monkeypatch) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(executor, "run", lambda args, **_kwargs: calls.append(args))

    with pytest.raises(ValueError, match="动作不在白名单"):
        executor.execute(action, params, request_id="request-blocked-01")

    assert calls == []


def test_peer_credentials_require_exact_backend_uid_and_primary_gid(monkeypatch) -> None:
    monkeypatch.setattr(executor.socket, "SO_PEERCRED", 17, raising=False)
    class PeerSocket:
        def __init__(self, uid: int, gid: int):
            self.uid = uid
            self.gid = gid

        def getsockopt(self, _level: int, _option: int, _size: int) -> bytes:
            import struct

            return struct.pack("3i", 1234, self.uid, self.gid)

    assert executor._authorized_peer(PeerSocket(10001, 991)) is True
    assert executor._authorized_peer(PeerSocket(10001, 992)) is False
    assert executor._authorized_peer(PeerSocket(0, 991)) is False
    assert executor._authorized_peer(object()) is False


def test_peer_credentials_fail_closed_when_platform_support_is_missing(monkeypatch) -> None:
    monkeypatch.delattr(executor.socket, "SO_PEERCRED", raising=False)

    assert executor._authorized_peer(object()) is False


def test_socket_directory_is_traversable_but_not_writable_by_backend(tmp_path: Path, monkeypatch) -> None:
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    directory = runtime / "prism-ops"
    monkeypatch.setattr(executor, "SOCKET_PATH", directory / "agent.sock")
    monkeypatch.setattr(executor, "SOCKET_DIRECTORY_UID", os.getuid())
    monkeypatch.setattr(executor, "SOCKET_DIRECTORY_GID", os.stat(runtime).st_gid)

    executor._prepare_socket_directory()

    assert directory.stat().st_mode & 0o7777 == 0o710
    assert directory.stat().st_uid == os.getuid()
    assert directory.stat().st_gid == os.stat(runtime).st_gid


@pytest.mark.parametrize("wrong_owner", ["uid", "gid"])
def test_socket_directory_rejects_unexpected_owner_or_group(tmp_path: Path, monkeypatch, wrong_owner: str) -> None:
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    directory = runtime / "prism-ops"
    monkeypatch.setattr(executor, "SOCKET_PATH", directory / "agent.sock")
    actual = os.stat(runtime)
    monkeypatch.setattr(executor, "SOCKET_DIRECTORY_UID", actual.st_uid + (wrong_owner == "uid"))
    monkeypatch.setattr(executor, "SOCKET_DIRECTORY_GID", actual.st_gid + (wrong_owner == "gid"))

    with pytest.raises(RuntimeError, match="属主"):
        executor._prepare_socket_directory()


def test_stale_socket_cleanup_refuses_regular_files_and_symlinks(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "agent.sock"
    path.write_text("preserve me", encoding="utf-8")
    monkeypatch.setattr(executor, "SOCKET_PATH", path)

    with pytest.raises(RuntimeError, match="非 socket"):
        executor._remove_stale_socket()

    assert path.read_text(encoding="utf-8") == "preserve me"

    linked = tmp_path / "linked.sock"
    linked.symlink_to(path)
    monkeypatch.setattr(executor, "SOCKET_PATH", linked)
    with pytest.raises(RuntimeError, match="符号链接"):
        executor._remove_stale_socket()
    assert path.exists()


def test_text_paths_reject_sensitive_files_and_symlink_components(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(executor, "READABLE_TEXT_ROOTS", (tmp_path,))
    sensitive = tmp_path / ".env"
    sensitive.write_text("TOKEN=should-not-leak\n", encoding="utf-8")
    with pytest.raises(ValueError, match="凭据或私钥"):
        executor._safe_text_path(str(sensitive), must_exist=True)

    real_dir = tmp_path / "real"
    real_dir.mkdir()
    target = real_dir / "config.txt"
    target.write_text("ok\n", encoding="utf-8")
    linked_dir = tmp_path / "linked"
    linked_dir.symlink_to(real_dir, target_is_directory=True)
    with pytest.raises(ValueError, match="符号链接"):
        executor._safe_text_path(str(linked_dir / "config.txt"), must_exist=True)


def test_text_file_and_directory_reads_are_limited_to_operational_roots(tmp_path: Path, monkeypatch) -> None:
    allowed = tmp_path / "logs"
    denied = tmp_path / "secrets"
    allowed.mkdir()
    denied.mkdir()
    allowed_file = allowed / "secure.log"
    denied_file = denied / "service.conf"
    allowed_file.write_text("authorized\n", encoding="utf-8")
    denied_file.write_text("private\n", encoding="utf-8")
    monkeypatch.setattr(executor, "READABLE_TEXT_ROOTS", (allowed,))
    monkeypatch.setattr(executor, "LISTABLE_DIRECTORY_ROOTS", (allowed,))

    assert executor._read_text_file({"path": str(allowed_file)})["content"] == "authorized\n"
    with pytest.raises(ValueError, match="允许范围"):
        executor._read_text_file({"path": str(denied_file)})
    with pytest.raises(ValueError, match="允许范围"):
        executor._list_directory({"path": str(denied)})


def test_directory_listing_allows_only_the_configured_backup_directory(tmp_path: Path, monkeypatch) -> None:
    backup_dir = tmp_path / "persistent-backups"
    sibling = tmp_path / "other-data"
    backup_dir.mkdir()
    sibling.mkdir()
    (backup_dir / "backup.sql.gz").write_bytes(b"backup")
    (sibling / "private.txt").write_text("private", encoding="utf-8")
    monkeypatch.setenv("BACKUP_DIR", str(backup_dir))
    monkeypatch.setattr(executor, "LISTABLE_DIRECTORY_ROOTS", (Path("/var/log"),))

    listing = executor._list_directory({"path": str(backup_dir)})

    assert [entry["name"] for entry in listing["entries"]] == ["backup.sql.gz"]
    with pytest.raises(ValueError, match="允许范围"):
        executor._list_directory({"path": str(sibling)})


def test_journal_queries_reject_unapproved_units_before_command(monkeypatch) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(executor, "run", lambda args, **_kwargs: calls.append(args))

    with pytest.raises(ValueError, match="unit 不在只读查询白名单"):
        executor._journal_query({"unit": "attacker.service"})

    assert calls == []


def test_backup_audit_and_restore_lookup_use_configured_backup_dir(tmp_path: Path, monkeypatch) -> None:
    backup_dir = tmp_path / "persistent-backups"
    backup_dir.mkdir()
    backup = backup_dir / "code_review_test.sql.gz"
    backup.write_bytes(b"gzip placeholder")
    monkeypatch.setenv("BACKUP_DIR", str(backup_dir))

    audit = executor._backup_audit()
    lookup = executor._backup_file(backup.name)

    assert audit["ok"] is True
    assert audit["dir"] == str(backup_dir.resolve())
    assert audit["sql_gz_count"] == 1
    assert lookup == backup.resolve()


def test_ledger_is_atomic_and_request_digest_binds_arguments(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(executor, "LEDGER_DIR", tmp_path / "ledger")
    first_digest = executor._request_digest("host_inventory", {})
    executor._write_ledger(
        "request-ledger-01",
        {"status": "running", "request_digest": first_digest},
    )
    recorded = executor._read_ledger("request-ledger-01")
    assert recorded == {"status": "running", "request_digest": first_digest}
    assert first_digest != executor._request_digest("list_directory", {"path": "/tmp"})
    assert (tmp_path / "ledger" / "request-ledger-01.json").stat().st_mode & 0o777 == 0o600


def test_audit_never_persists_legacy_host_mutation_payloads() -> None:
    params = executor._audit_params(
        "write_text_file",
        {
            "path": "/etc/systemd/system/example.service",
            "content": "private payload",
            "nested": {"token": "secret-token"},
        },
    )

    serialized = json.dumps(params)
    assert "private payload" not in serialized
    assert "secret-token" not in serialized
    assert params["content"] == "[REDACTED]"
    assert params["nested"]["token"] == "[REDACTED]"


def test_removed_mutation_actions_are_not_listed() -> None:
    removed = {
        "systemd_unit_action", "docker_container_action", "write_text_file", "package_action",
        "firewall_action", "account_action", "ssh_authorized_key_action",
    }
    assert removed.isdisjoint(executor.ACTION_PARAM_KEYS)


def test_status_preserves_degraded_semantics(monkeypatch) -> None:
    payload = {
        "status": "degraded",
        "can_continue": True,
        "summary": "磁盘压力需要人工审阅",
        "checks": {"disk": {"status": "degraded", "ok": False}},
    }
    monkeypatch.setattr(
        executor,
        "run",
        lambda *_args, **_kwargs: {"exit_code": 0, "stdout": json.dumps(payload), "stderr": ""},
    )

    result = executor.execute("status", {}, request_id="request-status-01")

    assert result["health_status"] == "degraded"
    assert result["can_continue"] is True
    assert result["checks"]["checks"]["disk"]["status"] == "degraded"


@pytest.mark.parametrize(
    ("file_conf_dir", "environment_conf_dir", "expected_relative"),
    [
        (None, None, "certbot/conf"),
        ("", None, "certbot/conf"),
        ("./certbot/conf", None, "certbot/conf"),
        ("../shared/certificates", None, "../shared/certificates"),
        ("./ignored/certificates", "/persistent/certbot/conf", None),
    ],
)
def test_certificate_status_uses_compose_certificate_directory(
    monkeypatch, tmp_path: Path, file_conf_dir: str | None,
    environment_conf_dir: str | None, expected_relative: str | None,
) -> None:
    deploy_dir = tmp_path / "releases" / "release-a" / "deploy"
    deploy_dir.mkdir(parents=True)
    monkeypatch.setattr(executor, "DEPLOY_DIR", deploy_dir)
    monkeypatch.delenv("CERTBOT_CONF_DIR", raising=False)
    if environment_conf_dir is not None:
        monkeypatch.setenv("CERTBOT_CONF_DIR", environment_conf_dir)

    env = {"APP_DOMAIN": "example.invalid"}
    if file_conf_dir is not None:
        env["CERTBOT_CONF_DIR"] = file_conf_dir

    def read_env(key: str) -> str:
        if key not in env:
            raise RuntimeError(f"missing {key}")
        return env[key]

    commands: list[list[str]] = []

    def fake_run(args: list[str], **_kwargs):
        commands.append(args)
        return {"exit_code": 0, "stdout": "notAfter=Dec 29 2026", "stderr": ""}

    monkeypatch.setattr(executor, "_read_env", read_env)
    monkeypatch.setattr(executor, "run", fake_run)

    executor.execute("certificate_status", {})

    if environment_conf_dir:
        expected_conf = Path(environment_conf_dir)
    elif expected_relative:
        expected_conf = deploy_dir / expected_relative
    else:
        expected_conf = Path("/persistent/certbot/conf")
    expected_cert = (expected_conf / "live" / "example.invalid" / "fullchain.pem").resolve()
    assert [Path(command[-1]).resolve() for command in commands] == [expected_cert, expected_cert]


def test_certificate_status_absolute_shared_path_survives_release_switch(monkeypatch, tmp_path: Path) -> None:
    shared_conf = tmp_path / "persistent" / "certbot" / "conf"
    monkeypatch.setenv("CERTBOT_CONF_DIR", str(shared_conf))
    monkeypatch.setattr(executor, "_read_env", lambda key: "example.invalid" if key == "APP_DOMAIN" else "")
    commands: list[list[str]] = []
    monkeypatch.setattr(
        executor,
        "run",
        lambda args, **_kwargs: commands.append(args)
        or {"exit_code": 0, "stdout": "notAfter=Dec 29 2026", "stderr": ""},
    )

    for release in ("release-a", "release-b"):
        monkeypatch.setattr(executor, "DEPLOY_DIR", tmp_path / "releases" / release / "deploy")
        executor.execute("certificate_status", {})

    expected = (shared_conf / "live" / "example.invalid" / "fullchain.pem").resolve()
    assert [Path(command[-1]).resolve() for command in commands] == [expected] * 4


def test_parse_security_sources_and_keep_collection_failure_visible(monkeypatch) -> None:
    ssh = executor.parse_ssh_log([
        "Accepted publickey for root from 10.0.0.2 port 1000 ssh2: ED25519 SHA256:test",
        "Failed password for invalid user admin from 10.0.0.3 port 1001 ssh2",
    ])
    assert ssh["accepted"][0]["ip"] == "10.0.0.2"
    assert ssh["failed"][0]["detail"] == "failed_password"

    nginx = executor.parse_nginx_log([
        '10.0.0.4 - - [01/Sep/2026:00:00:00 +0000] "CONNECT example.com:443 HTTP/1.1" 400 0',
        '10.0.0.5 - - [01/Sep/2026:00:00:01 +0000] "GET / HTTP/1.1" 200 10',
    ])
    assert [item["detail"] for item in nginx] == ["proxy_connect"]

    monkeypatch.setattr(
        executor,
        "run",
        lambda *_args, **_kwargs: {"exit_code": 1, "stdout": "", "stderr": "journal unavailable"},
    )
    result = executor._ssh_login_events({"since_hours": 1, "limit": 10})
    assert result["ok"] is False
    assert result["source_exit_code"] == 1
    assert "journal unavailable" in result["source_error"]


def test_nginx_http_failure_aggregation_uses_full_window_not_recent_sample(monkeypatch) -> None:
    """HTTP 错误按完整日志窗口聚合，recent 的展示上限不能造成漏报。"""
    ip = "203.0.113.88"
    lines = [
        f'{ip} - - [01/Sep/2026:00:00:{index % 60:02d} +0000] "GET /probe HTTP/1.1" 403 10'
        for index in range(350)
    ]
    below_threshold_ip = "203.0.113.89"
    lines.extend(
        f'{below_threshold_ip} - - [01/Sep/2026:00:01:{index % 60:02d} +0000] "POST /probe HTTP/1.1" 400 10'
        for index in range(19)
    )
    monkeypatch.setattr(
        executor,
        "run",
        lambda *_args, **_kwargs: {"exit_code": 0, "stdout": "\n".join(lines), "stderr": ""},
    )

    result = executor._nginx_attack_events({"since_hours": 1, "limit": 1, "failure_threshold": 20})

    assert result["ok"] is True
    assert result["total"] == 369
    assert len(result["recent"]) == 1
    assert result["http_failure_threshold"] == 20
    assert result["http_failures_by_ip"] == [
        {"ip": ip, "failure_count": 350, "status_counts": {"403": 350}},
    ]


def test_nginx_http_failure_payload_marks_line_limit_as_incomplete(monkeypatch) -> None:
    monkeypatch.setattr(executor, "NGINX_LOG_LINE_LIMIT", 2)
    lines = [
        '203.0.113.90 - - [01/Sep/2026:00:00:00 +0000] "GET /a HTTP/1.1" 403 10',
        '203.0.113.90 - - [01/Sep/2026:00:00:01 +0000] "GET /b HTTP/1.1" 403 10',
    ]
    monkeypatch.setattr(
        executor,
        "run",
        lambda *_args, **_kwargs: {"exit_code": 0, "stdout": "\n".join(lines), "stderr": ""},
    )

    result = executor._nginx_attack_events({"since_hours": 1, "limit": 1, "failure_threshold": 2})

    assert result["source_truncated"] is True
    assert result["source_line_limit"] == 2
    assert result["http_failures_by_ip"][0]["failure_count"] == 2


def test_nginx_large_stdout_is_not_misreported_as_capped(monkeypatch) -> None:
    monkeypatch.setattr(
        executor,
        "run",
        lambda *_args, **_kwargs: {"exit_code": 0, "stdout": "x" * 150_000, "stderr": ""},
    )

    result = executor._nginx_attack_events({"since_hours": 1, "limit": 1, "failure_threshold": 20})

    assert result["stdout_capped"] is False
    assert result["source_truncated"] is False


def test_flytrap_parser_ignores_operational_json_without_remote_source() -> None:
    events = executor.parse_flytrap_log([
        json.dumps({
            "time": "2026-09-01T14:00:00+08:00",
            "remote": "203.0.113.9:52222",
            "username": "root",
            "message": "SSH honeypot login",
        }),
        json.dumps({
            "time": "2026-09-01T14:00:01+08:00",
            "message": "Heartbeat 调用失败，进入退避",
            "error": "connection timed out",
        }),
        json.dumps({
            "time": "2026-09-01T14:00:02+08:00",
            "remote": "not-an-ip:1234",
            "message": "malformed source",
        }),
    ])

    assert events == [{
        "time": "2026-09-01T14:00:00+08:00",
        "username": "root",
        "ip": "203.0.113.9",
        "message": "SSH honeypot login",
    }]


def test_flytrap_health_marks_upstream_timeout_as_non_blocking_degradation() -> None:
    health = executor.summarize_flytrap_health(
        agent_active=True,
        sync_active=True,
        agent_lines=["Heartbeat 调用失败，进入退避: connection timed out"],
        sync_lines=[
            "2026-09-01 13:58:00 INFO 节点同步: 2 个",
            "2026-09-01 14:00:00 ERROR 同步周期异常: timed out",
        ],
    )

    assert health["status"] == "degraded"
    assert health["can_continue"] is True
    assert health["requires_human"] is True
    assert {item["code"] for item in health["issues"]} == {
        "flytrap_agent_upstream_error",
        "flytrap_sync_error",
    }


def test_flytrap_health_accepts_newer_sync_success_and_active_services() -> None:
    health = executor.summarize_flytrap_health(
        agent_active=True,
        sync_active=True,
        agent_lines=[],
        sync_lines=[
            "2026-09-01 13:58:00 ERROR 同步周期异常: timed out",
            "2026-09-01 14:00:00 INFO 节点同步: 2 个",
        ],
    )

    assert health["status"] == "ok"
    assert health["issues"] == []
    assert health["requires_human"] is False


def test_retired_flytrap_returns_without_touching_host(monkeypatch) -> None:
    calls: list[list[str]] = []
    monkeypatch.setenv("PRISM_FLYTRAP_ENABLED", "false")
    monkeypatch.setattr(
        executor,
        "run",
        lambda command, **_kwargs: calls.append(command) or {"exit_code": 0, "stdout": "", "stderr": ""},
    )

    result = executor._flytrap_attack_events({"since_hours": 24, "limit": 100})

    assert result == {
        "ok": True,
        "enabled": False,
        "status": "retired",
        "degraded": False,
        "can_continue": True,
        "reason": "FlyTrap 集成已退役",
        "human_actions": [],
        "since_hours": 24,
        "total": 0,
        "by_ip": [],
        "by_username": [],
        "recent": [],
    }
    assert calls == []


def test_database_signal_parser_redacts_and_avoids_normal_update_false_positive() -> None:
    result = executor.parse_db_general_log([
        {"user_host": "root@localhost", "argument": "DROP TABLE users", "event_time": "now"},
        {"user_host": "app@backend", "argument": "UPDATE jobs SET error='none' WHERE id=12345", "event_time": "now"},
        {"user_host": "app@backend", "argument": "Access denied for user 'app'", "event_time": "now"},
        {
            "user_host": "root@localhost",
            "argument": "SELECT * FROM users INTO OUTFILE '/tmp/users.sql'",
            "event_time": "now",
        },
    ])

    assert result["destructive_total"] == 1
    assert result["error_total"] == 1
    assert result["dump_exfil_total"] == 1
    serialized = json.dumps(result, ensure_ascii=False)
    assert "/tmp/users.sql" not in serialized
    assert "12345" not in serialized


def test_security_action_contract_is_in_sync_with_backend_scheduler() -> None:
    expected = {
        "ssh_login_events",
        "flytrap_attack_events",
        "nginx_attack_events",
        "backup_audit",
        "db_threat_signals",
        "db_health",
        "ip_attribution",
    }
    assert expected <= executor.ACTION_PARAM_KEYS.keys()
    assert expected <= executor.READ_ONLY_ACTIONS
    assert {"since_hours", "limit", "failure_threshold"} <= executor.ACTION_PARAM_KEYS["nginx_attack_events"]


def test_executor_server_handles_independent_requests_concurrently() -> None:
    assert issubclass(executor.UnixHTTPServer, executor.socketserver.ThreadingMixIn)
    assert executor.UnixHTTPServer.daemon_threads is True


def test_mutating_actions_are_serialized_while_reads_remain_available(monkeypatch) -> None:
    active = 0
    peak = 0
    gate = threading.Lock()

    def fake_execute(*_args, **_kwargs):
        nonlocal active, peak
        with gate:
            active += 1
            peak = max(peak, active)
        time.sleep(0.02)
        with gate:
            active -= 1
        return {"ok": True}

    monkeypatch.setattr(executor, "execute", fake_execute)
    workers = [
        threading.Thread(
            target=executor._execute_with_concurrency_policy,
            args=("restart_service", {"service": "backend"}, f"request-serial-{index}"),
        )
        for index in range(3)
    ]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join()

    assert peak == 1
