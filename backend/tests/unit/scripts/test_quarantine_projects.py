"""显式项目隔离工具：在临时 SQLite 上验证 dry-run、精确校验与恢复。"""

from __future__ import annotations

import hashlib
import importlib
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy import event, text
from sqlalchemy.orm import Session

from app.models.ai_call_log import AiCallLog
from app.models.audit_log import AuditLog
from app.models.code_file import CodeFile
from app.models.project import Project
from app.models.project_member import ProjectMember
from app.models.review_task import ReviewTask
from app.models.user import User
from app.services.project_member_service import get_visible_project_ids, require_project_access

ROOT = Path(__file__).resolve().parents[4]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture
def tool():
    return importlib.import_module("scripts.quarantine_projects")


@pytest.fixture
def operation(db, tmp_path, tool):
    db.add(User(id=801, username="quarantine_tool_fixture", password="test-only", role="user", status=1))
    db.add_all([
        Project(id=9101, user_id=801, project_name="显式隔离单测", status="active", description="不得导出的描述"),
        Project(id=9102, user_id=801, project_name="保留归档单测", status="archived"),
        Project(id=9103, user_id=801, project_name="AC2-E2E-相似名不在白名单", status="active"),
        Project(id=9161, user_id=801, project_name="冒烟-仅名称线索", status="active"),
        ProjectMember(project_id=9101, user_id=801, role_in_project="owner"),
        AuditLog(action="fixture", detail="历史审计必须保留"),
        AiCallLog(model_name="fixture", prompt="不得导出的提示词", response="不得导出的模型记录"),
        CodeFile(project_id=9101, file_name="fixture.txt", language="text", content="不得导出的源码"),
    ])
    db.commit()
    backup = tmp_path / "fixture-only.backup"
    backup.write_bytes(b"synthetic backup checksum fixture; not a production backup")
    return {
        "engine": db.get_bind(),
        "targets": [tool.Target(9101, "显式隔离单测", 801), tool.Target(9102, "保留归档单测", 801)],
        "backup_file": backup,
        "backup_sha256": hashlib.sha256(backup.read_bytes()).hexdigest(),
        "manifest_path": tmp_path / "quarantine.json",
        "operator": "fixture-operator",
        "reason": "fixture-approved-isolation",
    }


def snapshot(engine):
    with engine.connect() as connection:
        return {
            name: [tuple(row) for row in connection.execute(text(f"SELECT * FROM {name} ORDER BY id"))]
            for name in ("project", "project_member", "audit_log", "ai_call_log", "code_file")
        }


def test_default_is_read_only_and_creates_no_manifest(tool, operation):
    before = snapshot(operation["engine"])
    result = tool.run_operation(**operation)
    assert result["state"] == "dry_run"
    assert result["changed_ids"] == [9101, 9102]
    assert not operation["manifest_path"].exists()
    assert snapshot(operation["engine"]) == before


def test_apply_restore_roundtrip_retains_all_related_data(tool, operation, tmp_path):
    before = snapshot(operation["engine"])
    result = tool.run_operation(**operation, apply=True)
    assert result["state"] == "committed"
    after = snapshot(operation["engine"])
    assert {name: rows for name, rows in before.items() if name != "project"} == {
        name: rows for name, rows in after.items() if name != "project"
    }
    assert before["project"][2:] == after["project"][2:]
    with Session(operation["engine"]) as session:
        actor = session.get(User, 801)
        assert set(get_visible_project_ids(session, actor)[0]) == {9103, 9161}
    manifest = json.loads(operation["manifest_path"].read_text())
    assert {row["status"] for row in manifest["before"]} == {"active", "archived"}
    assert {row["status"] for row in manifest["after"]} == {"quarantined"}
    assert operation["manifest_path"].stat().st_mode & 0o777 == 0o600
    for path in tmp_path.glob("*.json*"):
        content = path.read_text()
        forbidden = ("不得导出的描述", "不得导出的提示词", "不得导出的模型记录", "不得导出的源码", "test-only")
        assert all(secret not in content for secret in forbidden)
    restore = {**operation, "manifest_path": tmp_path / "restore.json"}
    restore.update(
        action="restore", restore_manifest=operation["manifest_path"], restore_sha256=result["manifest_sha256"],
    )
    assert tool.run_operation(**restore)["state"] == "dry_run"
    assert snapshot(operation["engine"]) == after
    assert tool.run_operation(**restore, apply=True)["state"] == "committed"
    assert snapshot(operation["engine"]) == before
    with Session(operation["engine"]) as session:
        actor = session.get(User, 801)
        assert set(get_visible_project_ids(session, actor)[0]) == {9101, 9102, 9103, 9161}
        assert require_project_access(session, 9102, actor) == "owner"


@pytest.mark.parametrize("target", [(9101, "错误名称", 801), (9101, "显式隔离单测", 802), (9999, "不存在", 801)])
def test_identity_mismatch_aborts_entire_batch(tool, operation, target):
    before = snapshot(operation["engine"])
    operation["targets"] = [operation["targets"][1], tool.Target(*target)]
    with pytest.raises(tool.QuarantineError):
        tool.run_operation(**operation, apply=True)
    assert snapshot(operation["engine"]) == before
    assert not operation["manifest_path"].exists()


@pytest.mark.parametrize("field", ["backup_file", "backup_sha256", "manifest_path", "operator", "reason"])
def test_apply_requires_backup_and_operation_evidence(tool, operation, field):
    before = snapshot(operation["engine"])
    operation.pop(field)
    with pytest.raises(tool.QuarantineError):
        tool.run_operation(**operation, apply=True)
    assert snapshot(operation["engine"]) == before


def test_bad_backup_hash_is_rejected(tool, operation):
    operation["backup_sha256"] = "0" * 64
    with pytest.raises(tool.QuarantineError):
        tool.run_operation(**operation, apply=True)
    assert not operation["manifest_path"].exists()


@pytest.mark.parametrize("invalid", [[], [9101, 9101]])
def test_empty_and_duplicate_targets_are_rejected(tool, operation, invalid):
    operation["targets"] = [operation["targets"][0] for _ in invalid]
    with pytest.raises(tool.QuarantineError):
        tool.run_operation(**operation)


@pytest.mark.parametrize("status", ["deleted", "quarantined", "importing", "import_failed"])
def test_only_stable_business_statuses_can_be_quarantined(tool, operation, status):
    with operation["engine"].begin() as connection:
        connection.execute(text("UPDATE project SET status=:status WHERE id=9102"), {"status": status})
    before = snapshot(operation["engine"])
    with pytest.raises(tool.QuarantineError):
        tool.run_operation(**operation, apply=True)
    assert snapshot(operation["engine"]) == before


def test_second_update_failure_rolls_back_first_and_preserves_manifest(tool, operation):
    before = snapshot(operation["engine"])
    statements = []

    def fail_second_update(connection, cursor, statement, parameters, context, executemany):
        if statement.lstrip().upper().startswith("UPDATE PROJECT"):
            statements.append(statement)
            if len(statements) == 2:
                raise RuntimeError("fixture failure before second update")

    event.listen(operation["engine"], "before_cursor_execute", fail_second_update)
    try:
        with pytest.raises(RuntimeError, match="fixture failure"):
            tool.run_operation(**operation, apply=True)
    finally:
        event.remove(operation["engine"], "before_cursor_execute", fail_second_update)
    assert len(statements) == 2
    assert snapshot(operation["engine"]) == before
    assert operation["manifest_path"].exists()


@pytest.mark.parametrize("drift", ["name", "user", "status", "time", "manifest", "hash", "targets", "database"])
def test_restore_refuses_drift_or_tampered_manifest(tool, operation, tmp_path, monkeypatch, drift):
    result = tool.run_operation(**operation, apply=True)
    restore = {
        **operation, "action": "restore", "restore_manifest": operation["manifest_path"],
        "restore_sha256": result["manifest_sha256"], "manifest_path": tmp_path / "restore.json",
    }
    changes = {
        "name": "project_name='改名单测'", "user": "user_id=9999", "status": "status='active'",
        "time": "update_time='2000-01-01 00:00:00.000000'",
    }
    if drift in changes:
        with operation["engine"].begin() as connection:
            connection.execute(text(f"UPDATE project SET {changes[drift]} WHERE id=9101"))
    elif drift == "manifest":
        operation["manifest_path"].write_text(operation["manifest_path"].read_text() + " ")
    elif drift == "hash":
        restore["restore_sha256"] = "0" * 64
    elif drift == "targets":
        restore["targets"] = restore["targets"][:1]
    else:
        monkeypatch.setattr(tool, "database_fingerprint", lambda engine: "different-database")
    before_restore = snapshot(operation["engine"])
    with pytest.raises(tool.QuarantineError):
        tool.run_operation(**restore, apply=True)
    assert snapshot(operation["engine"]) == before_restore


def test_existing_manifest_is_never_overwritten(tool, operation):
    operation["manifest_path"].write_text("preexisting")
    before = snapshot(operation["engine"])
    with pytest.raises((tool.QuarantineError, FileExistsError)):
        tool.run_operation(**operation, apply=True)
    assert operation["manifest_path"].read_text() == "preexisting"
    assert snapshot(operation["engine"]) == before


@pytest.mark.parametrize("status", ["pending", "running"])
@pytest.mark.parametrize("apply", [False, True])
def test_inflight_review_tasks_block_entire_quarantine_batch(db, tool, operation, status, apply):
    db.add(ReviewTask(project_id=9102, user_id=801, task_name="隔离门禁单测", status=status))
    db.commit()
    before = snapshot(operation["engine"])
    with pytest.raises(tool.QuarantineError, match="pending/running"):
        tool.run_operation(**operation, apply=apply)
    assert snapshot(operation["engine"]) == before
    assert not operation["manifest_path"].exists()


@pytest.mark.parametrize("status", ["success", "failed", "cancelled", "deleted"])
def test_terminal_or_unselected_tasks_do_not_block_quarantine(db, tool, operation, status):
    db.add_all([
        ReviewTask(project_id=9101, user_id=801, status=status),
        ReviewTask(project_id=9103, user_id=801, status="running"),
    ])
    db.commit()
    result = tool.run_operation(**operation, apply=True)
    assert result["state"] == "committed"
    db.expire_all()
    assert [task.status for task in db.query(ReviewTask).order_by(ReviewTask.id)] == [status, "running"]


def test_audit_failure_prevents_database_mutation(tool, operation, monkeypatch):
    before = snapshot(operation["engine"])

    def fail_audit(*args):
        raise OSError("fixture audit failure")

    monkeypatch.setattr(tool, "_audit", fail_audit)
    with pytest.raises(OSError, match="fixture audit failure"):
        tool.run_operation(**operation, apply=True)
    assert snapshot(operation["engine"]) == before


def test_missing_completion_receipt_can_be_reconciled_and_restored(tool, operation, tmp_path, monkeypatch):
    before = snapshot(operation["engine"])
    write_manifest = tool._write_manifest

    def fail_completion(path, content):
        if str(path).endswith(".result.json"):
            raise OSError("fixture receipt failure")
        return write_manifest(path, content)

    monkeypatch.setattr(tool, "_write_manifest", fail_completion)
    with pytest.raises(tool.QuarantineError, match="事务已提交"):
        tool.run_operation(**operation, apply=True)
    assert not Path(str(operation["manifest_path"]) + ".result.json").exists()
    monkeypatch.setattr(tool, "_write_manifest", write_manifest)
    restore = {
        **operation, "action": "restore", "restore_manifest": operation["manifest_path"],
        "restore_sha256": hashlib.sha256(operation["manifest_path"].read_bytes()).hexdigest(),
        "manifest_path": tmp_path / "restore-after-receipt-failure.json",
    }
    assert tool.run_operation(**restore, apply=True)["state"] == "committed"
    assert snapshot(operation["engine"]) == before


def test_cli_file_backed_roundtrip_is_rechecked_in_fresh_readonly_process(tool, operation, tmp_path):
    database_path = tmp_path / "synthetic-projects.sqlite"
    raw_connection = operation["engine"].raw_connection()
    try:
        with sqlite3.connect(str(database_path)) as destination:
            raw_connection.driver_connection.backup(destination)
    finally:
        raw_connection.close()
    environment = {**os.environ, "PRISM_QUARANTINE_DATABASE_URL": f"sqlite:///{database_path}"}
    target_args = [value for target in operation["targets"] for value in (
        "--target", str(target.project_id), target.expected_name, str(target.expected_user_id),
    )]

    def cli(action, *arguments):
        completed = subprocess.run(
            [sys.executable, "-B", str(ROOT / "scripts/quarantine_projects.py"), action, *target_args, *arguments],
            env=environment, capture_output=True, text=True, check=True, timeout=20,
        )
        return json.loads(completed.stdout)

    def read_states():
        program = (
            "import json, sqlite3, sys; "
            "connection=sqlite3.connect('file:'+sys.argv[1]+'?mode=ro', uri=True); "
            "print(json.dumps(connection.execute("
            "'SELECT id,user_id,project_name,status,create_time,update_time FROM project ORDER BY id').fetchall()))"
        )
        completed = subprocess.run(
            [sys.executable, "-I", "-B", "-c", program, str(database_path)],
            env=environment, capture_output=True, text=True, check=True, timeout=20,
        )
        return json.loads(completed.stdout)

    backup_path = tmp_path / "synthetic-prechange-backup.sqlite"
    with sqlite3.connect(f"file:{database_path}?mode=ro", uri=True) as source:
        with sqlite3.connect(str(backup_path)) as destination:
            source.backup(destination)
    apply_args = [
        "--apply", "--backup-file", str(backup_path),
        "--backup-sha256", hashlib.sha256(backup_path.read_bytes()).hexdigest(),
        "--operator", "cli-fixture", "--reason", "cli-fixture-approval",
    ]
    before = read_states()
    assert cli("quarantine")["state"] == "dry_run"
    assert read_states() == before
    manifest_path = tmp_path / "cli-quarantine.json"
    result = cli("quarantine", *apply_args, "--manifest", str(manifest_path))
    assert result["state"] == "committed"
    quarantined = read_states()
    assert [row[3] for row in quarantined] == ["quarantined", "quarantined", "active", "active"]
    restore_args = ["--restore-manifest", str(manifest_path), "--restore-sha256", result["manifest_sha256"]]
    assert cli("restore", *restore_args)["state"] == "dry_run"
    assert read_states() == quarantined
    restored = cli("restore", *restore_args, *apply_args, "--manifest", str(tmp_path / "cli-restore.json"))
    assert restored["state"] == "committed"
    assert read_states() == before


def test_cli_never_falls_back_to_app_configuration(tool, monkeypatch, capsys):
    monkeypatch.delenv("PRISM_QUARANTINE_DATABASE_URL", raising=False)
    assert tool.main(["quarantine", "--target", "9101", "显式隔离单测", "801"]) == 2
    assert "不加载 .env" in capsys.readouterr().err


def test_cli_hides_connection_errors_and_credentials(tool, monkeypatch, capsys):
    monkeypatch.setenv("PRISM_QUARANTINE_DATABASE_URL", "invalid+fixture://fixture-user:fixture-secret@invalid/db")
    assert tool.main(["quarantine", "--target", "9101", "显式隔离单测", "801"]) == 2
    output = capsys.readouterr()
    assert "fixture-secret" not in output.out + output.err
    assert "fixture-user" not in output.out + output.err
