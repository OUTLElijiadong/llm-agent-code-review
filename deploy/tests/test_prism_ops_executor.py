"""Root 运维执行器安全边界回归。"""

from __future__ import annotations

import importlib.util
import json
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


def test_text_paths_reject_sensitive_files_and_symlink_components(tmp_path: Path) -> None:
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


def test_write_text_file_is_atomic_and_creates_rollback_backup(tmp_path: Path, monkeypatch) -> None:
    backup_dir = tmp_path / "backups"
    monkeypatch.setattr(executor, "FILE_BACKUP_DIR", backup_dir)
    target = tmp_path / "service.conf"
    target.write_text("before\n", encoding="utf-8")
    old_sha = executor._sha256_file(target)

    result = executor._write_text_file(
        {"path": str(target), "content": "after\n", "expected_sha256": old_sha, "mode": "0640"},
        request_id="request-write-01",
    )

    assert target.read_text(encoding="utf-8") == "after\n"
    assert target.stat().st_mode & 0o777 == 0o640
    rollback = Path(result["rollback_backup"])
    assert rollback.read_text(encoding="utf-8") == "before\n"
    assert rollback.stat().st_mode & 0o777 == 0o600
    with pytest.raises(ValueError, match="expected_sha256"):
        executor._write_text_file(
            {"path": str(target), "content": "stale\n", "expected_sha256": old_sha},
            request_id="request-write-02",
        )


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


def test_audit_never_persists_file_content_or_public_key() -> None:
    write_params = executor._audit_params("write_text_file", {"path": "/tmp/a", "content": "secret"})
    assert "content" not in write_params
    assert write_params["content_bytes"] == 6
    assert len(write_params["content_sha256"]) == 64

    public_key = "ssh-ed25519 " + ("QUFB" * 12)
    key_params = executor._audit_params(
        "ssh_authorized_key_action",
        {"operation": "add", "username": "deploy", "public_key": public_key},
    )
    serialized = json.dumps(key_params)
    assert public_key not in serialized
    assert key_params["fingerprint"].startswith("SHA256:")


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


def test_database_signal_parser_redacts_and_avoids_normal_update_false_positive() -> None:
    result = executor.parse_db_general_log([
        {"user_host": "root@localhost", "argument": "DROP TABLE users", "event_time": "now"},
        {"user_host": "app@backend", "argument": "UPDATE jobs SET error='none' WHERE id=12345", "event_time": "now"},
        {"user_host": "app@backend", "argument": "Access denied for user 'app'", "event_time": "now"},
        {"user_host": "root@localhost", "argument": "SELECT * FROM users INTO OUTFILE '/tmp/users.sql'", "event_time": "now"},
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
