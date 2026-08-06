"""Root 运维执行器安全边界回归。"""

from __future__ import annotations

import importlib.util
import json
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


# ── 最高管理员安全监控：只读安全动作 ──────────────────────────


def test_parse_ssh_log_classifies_accepted_failed_invalid() -> None:
    lines = [
        (
            "2026-08-05T17:39:58+0800 vm sshd[1247]: Accepted publickey for root from "
            "45.135.228.155 port 33249 ssh2: ED25519 SHA256:wbLkqbw/AAAA"
        ),
        (
            "2026-08-05T17:40:01+0800 vm sshd[1247]: Failed password for invalid user "
            "admin from 193.32.162.30 port 35828 ssh2"
        ),
        "2026-08-05T17:40:02+0800 vm sshd[1247]: Invalid user li from 45.135.228.155 port 43553",
        "2026-08-05T17:40:03+0800 vm sshd[1247]: Accepted password for root from 10.0.0.2 port 1000 ssh2",
    ]
    parsed = executor.parse_ssh_log(lines)
    assert len(parsed["accepted"]) == 2
    assert parsed["accepted"][0]["ip"] == "45.135.228.155"
    assert parsed["accepted"][0]["detail"].startswith("ED25519")
    assert parsed["accepted"][1]["method"] == "password"
    assert [item["detail"] for item in parsed["failed"]] == ["failed_password", "invalid_user"]


def test_parse_flytrap_log_skips_non_json_lines() -> None:
    lines = [
        (
            '{"level":"info","username":"lyf","remote":"193.32.162.30:35828",'
            '"time":"2026-08-05T17:39:58+08:00","caller":"x","message":"捕获 SSH 密码认证尝试"}'
        ),
        "not json",
        (
            '{"level":"info","username":"rp","remote":"138.197.180.155:40284",'
            '"time":"2026-08-05T17:40:01+08:00","caller":"y","message":"捕获 SSH 密码认证尝试"}'
        ),
    ]
    events = executor.parse_flytrap_log(lines)
    assert len(events) == 2
    assert events[0]["ip"] == "193.32.162.30"
    assert events[1]["username"] == "rp"


def test_parse_nginx_log_connects_gibberish_and_normal() -> None:
    lines = [
        '104.249.59.148 - - [04/Aug/2026:16:46:06 +0000] "CONNECT dnspod.qcloud.com:443 HTTP/1.1" 400 157 "-" "-" "-"',
        '172.236.228.227 - - [04/Aug/2026:17:46:19 +0000] "\\x16\\x03\\x01\\x01" 400 157 "-" "-" "-"',
        '43.157.38.131 - - [04/Aug/2026:17:23:50 +0000] "GET / HTTP/1.1" 200 255 "-" "Mozilla" "-"',
    ]
    events = executor.parse_nginx_log(lines)
    assert [item["detail"] for item in events] == ["proxy_connect", "tls_gibberish"]


def test_security_actions_reject_invalid_params() -> None:
    with pytest.raises(ValueError, match="since_hours"):
        executor._ssh_login_events({"since_hours": 0})
    with pytest.raises(ValueError, match="since_hours"):
        executor._flytrap_attack_events({"since_hours": 9999})
    with pytest.raises(ValueError, match="limit"):
        executor._nginx_attack_events({"limit": 99999})
    with pytest.raises(ValueError, match="focus"):
        executor._ssh_login_events({"focus": "bogus"})
    with pytest.raises(ValueError, match="ip 不是合法"):
        executor._ip_attribution({"ip": "not-an-ip"})


def test_backup_audit_reports_retention_and_other_entries(tmp_path: Path, monkeypatch) -> None:
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    (backup_dir / "code_review_20260804T000000Z_abcd.sql.gz").write_bytes(b"x" * 1024)
    (backup_dir / "code_review_20260804T000000Z_abcd.sql.gz.sha256").write_text("x", encoding="utf-8")
    (backup_dir / "manual-snapshot.tar.gz").write_bytes(b"y" * 2048)
    (backup_dir / "release_20260701").mkdir()
    (backup_dir / "release_20260701" / "payload.bin").write_bytes(b"z" * 512)
    monkeypatch.setattr(executor, "BACKUP_DIR", backup_dir)

    audit = executor._backup_audit()
    assert audit["sql_gz_count"] == 1
    assert audit["sql_gz_bytes"] == 1024
    assert audit["newest"]["has_sha256"] is True
    assert audit["older_than_14_days"] == 0
    assert audit["other_entries_count"] == 2
    assert audit["other_bytes"] == 2048 + 512


def test_parse_db_general_log_classifies_threats() -> None:
    rows = [
        {"user_host": "root[root] @ localhost []", "argument": "DROP TABLE users", "event_time": "2026-08-05 10:00:00"},
        {"user_host": "app[app] @ 10.0.0.1 []", "argument": "DELETE FROM project WHERE id=1234", "event_time": "2026-08-05 10:01:00"},
        {"user_host": "app[app] @ 10.0.0.1 []", "argument": "SELECT * FROM user INTO OUTFILE '/tmp/dump.sql'", "event_time": "2026-08-05 10:02:00"},
        {"user_host": "app[app] @ 10.0.0.1 []", "argument": "select password from users limit 1", "event_time": "2026-08-05 10:03:00"},
        {"user_host": "app[app] @ 10.0.0.1 []", "argument": "Access denied for user 'app'", "event_time": "2026-08-05 10:04:00"},
        {"user_host": "app[app] @ 10.0.0.1 []", "argument": "SELECT * FROM project LIMIT 10", "event_time": "2026-08-05 10:05:00"},
    ]
    parsed = executor.parse_db_general_log(rows)
    # DROP + DELETE → destructive
    assert parsed["destructive_total"] == 2
    # INTO OUTFILE → dump_exfil
    assert parsed["dump_exfil_total"] == 1
    # Access denied → error（普通 SELECT 不计入任何类别）
    assert parsed["error_total"] == 1
    assert parsed["destructive_by_user"][0]["value"].startswith("root[root]")
    # 样本 SQL 已脱敏：字符串/长数字字面量被替换
    dumped = parsed["samples"]["dump_exfil"][0]["sql"]
    assert "/tmp/dump.sql" not in dumped
    deleted = parsed["samples"]["destructive"][1]["sql"]
    assert "1234" not in deleted


def test_parse_db_general_log_empty_and_non_dict() -> None:
    parsed = executor.parse_db_general_log([{}, {"argument": ""}, "not-a-dict", {"argument": "SELECT 1"}])
    assert parsed["destructive_total"] == 0
    assert parsed["dump_exfil_total"] == 0
    assert parsed["error_total"] == 0
    assert parsed["samples"]["destructive"] == []


def test_redact_db_sql_masks_literals() -> None:
    masked = executor._redact_db_sql("DELETE FROM t WHERE name='secret' AND id=98765")
    assert "secret" not in masked
    assert "98765" not in masked
    assert "DELETE FROM t WHERE name=" in masked


def test_parse_db_error_log_detects_restart_and_recovery() -> None:
    lines = [
        "2026-08-05T11:58:16Z [Note] InnoDB: Starting crash recovery",
        "2026-08-05T11:58:20Z [Note] InnoDB: Database was not shutdown normally!",
        "2026-08-05T11:58:42Z [Note] /usr/sbin/mysqld: ready for connections. port: 3306",
        "2026-08-05T12:00:00Z [Note] some ordinary log line",
    ]
    parsed = executor.parse_db_error_log(lines)
    assert parsed["restart_count"] == 1
    assert parsed["recovery_detected"] is True
    assert parsed["recovery_lines"]


def test_parse_db_error_log_clean() -> None:
    parsed = executor.parse_db_error_log([
        "2026-08-05T12:00:00Z [Note] ordinary line",
        "another line",
    ])
    assert parsed["restart_count"] == 0
    assert parsed["recovery_detected"] is False
