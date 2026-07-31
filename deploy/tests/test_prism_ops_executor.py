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
