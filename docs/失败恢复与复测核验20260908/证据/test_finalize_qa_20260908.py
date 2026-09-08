"""Local-only contracts and real SQLite/auth/disable integration. Never calls production."""
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "backend"))
SPEC = importlib.util.spec_from_file_location("qa_finalize_20260908", Path(__file__).with_name("finalize_qa_20260908.py"))
m = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(m)


def manifest():
    return {"marker": m.MARKER, "role_code": m.ROLE_CODE, "role_id": 7, "accounts": {
        name: {"id": uid, "username": f"qa_{m.MARKER}_{name}", "password": "LOCAL_ONLY_Secret_Sentinel"}
        for name, uid in m.ACCOUNTS.items()
    }}


def snap(status=1, version=5):
    data = manifest()
    return {
        "context": {"database": "code_review", "readonly": 1},
        "users": [{"id": uid, "username": data["accounts"][name]["username"], "role": "user", "status": status,
                   "token_version": version} for name, uid in m.ACCOUNTS.items()],
        "role": [{"id": 7, "code": m.ROLE_CODE, "status": "active", "is_builtin": 0}],
        "assignments": [{"user_id": uid, "role_id": 7, "code": m.ROLE_CODE}
                        for name, uid in m.ACCOUNTS.items() if name != "no_permission"],
        "permissions": sorted(m.PERMISSIONS),
        "projects": [{"id": pid, "user_id": uid, "marker_match": 1, "status": "active", "row_sha256": "b" * 64}
                     for pid, uid in m.PROJECTS.items()],
        "memberships": [{"project_id": 164, "user_id": 107, "role_in_project": "owner"},
                        {"project_id": 164, "user_id": 108, "role_in_project": "reviewer"},
                        {"project_id": 165, "user_id": 109, "role_in_project": "owner"}],
        "files": [{"id": fid, "project_id": pid, "status": "active", "row_sha256": "c" * 64}
                  for fid, pid in m.FILES.items()],
        "counts": {"ai_call_log": 0, "review_task": 0},
    }


def test_default_plan_never_reads_credentials_or_uses_network(monkeypatch, capsys):
    monkeypatch.setattr(m, "read_manifest", lambda: pytest.fail("must not read credentials"))
    monkeypatch.setattr(m, "run", lambda *_a, **_k: pytest.fail("must not run command"))
    monkeypatch.setattr(m.urllib.request, "build_opener", lambda *_a, **_k: pytest.fail("no HTTP client"))
    assert m.main([]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["status"] == "prepared_not_executed"
    assert data["account_ids"] == [107, 108, 109, 110] and data["role_id"] == 7
    assert data["retained_project_ids"] == [164, 165] and data["retained_file_ids"] == [1894, 1895]
    assert data["credentials_read"] is False and data["production_requests"] == 0


@pytest.mark.parametrize("kind", ["old_id", "name", "marker", "role", "extra_account", "bool_role_id", "wrong_role_id"])
def test_manifest_identity_gate(kind):
    data = manifest()
    if kind == "old_id": data["accounts"]["owner_a"]["id"] = 103
    if kind == "name": data["accounts"]["member_a"]["username"] = "real_user"
    if kind == "marker": data["marker"] = "20260907"
    if kind == "role": data["role_code"] = "admin"
    if kind == "extra_account": data["accounts"]["extra"] = data["accounts"]["owner_a"]
    if kind == "bool_role_id": data["role_id"] = True
    if kind == "wrong_role_id": data["role_id"] = 6
    with pytest.raises(m.FinalizationError):
        m.validate_manifest(data)


@pytest.mark.parametrize("kind", [
    "writable", "other_database", "disabled", "admin", "missing_user", "extra_role", "no_permission_role",
    "real_user_has_qa_role", "model_permission", "missing_permission", "model_log", "review", "project_owner",
    "project_deleted", "extra_project", "member_owner", "extra_member", "file_project", "file_deleted",
    "extra_file", "bad_project_hash", "bad_file_hash",
])
def test_database_scope_gate(kind):
    data = snap()
    if kind == "writable": data["context"]["readonly"] = 0
    if kind == "other_database": data["context"]["database"] = "other"
    if kind == "disabled": data["users"][0]["status"] = 0
    if kind == "admin": data["users"][0]["role"] = "admin"
    if kind == "missing_user": data["users"].pop()
    if kind == "extra_role": data["role"].append(data["role"][0])
    if kind == "no_permission_role": data["assignments"].append({"user_id": 110, "role_id": 7, "code": m.ROLE_CODE})
    if kind == "real_user_has_qa_role": data["assignments"].append({"user_id": 1, "role_id": 7, "code": m.ROLE_CODE})
    if kind == "model_permission": data["permissions"].append("review:start")
    if kind == "missing_permission": data["permissions"].pop()
    if kind == "model_log": data["counts"]["ai_call_log"] = 1
    if kind == "review": data["counts"]["review_task"] = 1
    if kind == "project_owner": data["projects"][0]["user_id"] = 1
    if kind == "project_deleted": data["projects"][0]["status"] = "deleted"
    if kind == "extra_project": data["projects"].append(data["projects"][0])
    if kind == "member_owner": data["memberships"][1]["role_in_project"] = "owner"
    if kind == "extra_member": data["memberships"].append({"project_id": 161, "user_id": 108, "role_in_project": "reviewer"})
    if kind == "file_project": data["files"][0]["project_id"] = 161
    if kind == "file_deleted": data["files"][0]["status"] = "deleted"
    if kind == "extra_file": data["files"].append(data["files"][0])
    if kind == "bad_project_hash": data["projects"][0]["row_sha256"] = "Z" * 64
    if kind == "bad_file_hash": data["files"][0]["row_sha256"] = "Z" * 64
    with pytest.raises(m.FinalizationError):
        m.validate_snapshot(data, manifest(), status=1)


def test_readonly_sql_utf8_and_fixed_scope(monkeypatch):
    value = snap()
    def command(args, *, input):
        sql = input.decode("utf-8")
        assert "START TRANSACTION WITH CONSISTENT SNAPSHOT, READ ONLY" in sql and "ROLLBACK;" in sql
        assert all(word not in sql.upper().split() for word in ["INSERT", "UPDATE", "DELETE", "ALTER", "DROP"])
        assert "--default-character-set=utf8mb4" in args[-1]
        assert "--database=code_review" in args[-1]
        assert "MYSQL_ROOT_PASSWORD" in args[-1] and "Secret" not in sql
        assert "project_id IN (164,165) OR user_id IN (107,108,109,110)" in sql
        assert "id IN (1894,1895) OR project_id IN (164,165)" in sql
        assert "QA权限验收-20260908-a" in sql and "qa_permission_20260908" in sql
        assert "OR ur.role_id=7" in sql and "rp.role_id=7" in sql
        return "\n".join(k + "\t" + json.dumps(v) for k, v in value.items()).encode()
    monkeypatch.setattr(m, "run", command)
    assert m.snapshot(7) == value
    with pytest.raises(m.FinalizationError, match="invalid_role_id"):
        m.snapshot(6)


@pytest.mark.parametrize("kind", ["missing", "duplicate", "context"])
def test_snapshot_rejects_incomplete_or_wrong_database(monkeypatch, kind):
    data = snap()
    if kind == "missing": data.pop("files")
    if kind == "context": data["context"]["readonly"] = 0
    raw = "\n".join(k + "\t" + json.dumps(v) for k, v in data.items())
    if kind == "duplicate": raw += '\ncounts\t{}'
    monkeypatch.setattr(m, "run", lambda *_a, **_k: raw.encode())
    with pytest.raises(m.FinalizationError):
        m.snapshot(7)


def test_http_forbids_other_endpoints_and_does_not_retry():
    with pytest.raises(m.FinalizationError, match="http_operation_not_allowed"):
        m.http(None, "POST", "/api/reviews", payload={})
    calls = []
    def fails(*args, **kwargs):
        calls.append(1)
        raise OSError("SECRET_VALUE")
    with pytest.raises(m.FinalizationError, match="^http_transport_failed_no_retry$"):
        m.http(SimpleNamespace(open=fails), "POST", "/api/auth/login", payload={})
    assert calls == [1]


def test_command_timeout_is_uncertain_and_never_retried(monkeypatch):
    calls = []
    def fails(*args, **kwargs):
        calls.append(1)
        raise subprocess.TimeoutExpired("SECRET_VALUE", 1, output="SECRET_VALUE")
    monkeypatch.setattr(m.subprocess, "run", fails)
    with pytest.raises(m.FinalizationError, match="^command_unavailable_or_outcome_uncertain$"):
        m.run(["docker"])
    assert calls == [1]


def test_main_sanitizes_unexpected_errors(monkeypatch, capsys):
    monkeypatch.setattr(m, "read_manifest", lambda: (_ for _ in ()).throw(ValueError("SECRET_VALUE")))
    assert m.main(["--execute"]) == 1
    assert "SECRET_VALUE" not in capsys.readouterr().out


def test_existing_result_prevents_replay(monkeypatch, tmp_path, capsys):
    result = tmp_path / "result.json"
    result.write_text("existing")
    monkeypatch.setattr(m, "RESULT", result)
    monkeypatch.setattr(m, "read_manifest", manifest)
    monkeypatch.setattr(m, "finalize", lambda *_a: pytest.fail("no replay"))
    assert m.main(["--execute"]) == 1
    assert result.read_text() == "existing"
    assert json.loads(capsys.readouterr().out)["status"] == "failed"


def test_prepare_source_fingerprint_and_private_container_copy():
    assert hashlib.sha256((ROOT / "backend/scripts/prepare_permission_acceptance.py").read_bytes()).hexdigest() == m.PREPARE_SHA256
    assert m.PREPARE_SHA256 in m.DISABLE_WRAPPER
    assert "'--marker','20260908'" in m.DISABLE_WRAPPER
    assert "'account_ids':[107,108,109,110]" in m.DISABLE_WRAPPER
    assert "os.chmod(folder,0o700)" in m.DISABLE_WRAPPER and "os.O_EXCL,0o600" in m.DISABLE_WRAPPER
    assert "finally:" in m.DISABLE_WRAPPER and "shutil.rmtree(folder)" in m.DISABLE_WRAPPER
    assert "/root/prism-acceptance-20260908/credentials.json" == str(m.CREDENTIALS)


@pytest.mark.parametrize("stage,field", [("login", "projects"), ("login", "files"), ("disable", "projects"), ("disable", "files")])
def test_changed_resource_hash_stops_and_does_not_replay_disable(monkeypatch, stage, field):
    snapshots = [snap(), snap(version=6), snap(status=0, version=7)]
    snapshots[1 if stage == "login" else 2][field][0]["row_sha256"] = "d" * 64
    iterator = iter(snapshots)
    monkeypatch.setattr(m, "snapshot", lambda _role: next(iterator))
    calls = []
    monkeypatch.setattr(m, "disable", lambda _manifest: calls.append("disable"))
    account_iter = iter(manifest()["accounts"].values())
    current = None
    def fake_http(_client, method, _path, **_kwargs):
        nonlocal current
        if method == "POST":
            current = next(account_iter)
            return 200, {"code": 0, "data": {"user": {**current, "role": "user", "status": 1}, "access_token": "x" * 40}}
        return 200, {"code": 0, "data": {**current, "role": "user", "status": 1}}
    monkeypatch.setattr(m, "http", fake_http)
    with pytest.raises(m.FinalizationError, match="changed_during"):
        m.finalize(manifest(), None, {"accounts": []}, lambda: None)
    assert calls == ([] if stage == "login" else ["disable"])


def test_real_local_auth_existing_disable_preserves_history_and_exact_resources(monkeypatch, tmp_path, capsys):
    from fastapi.testclient import TestClient
    from sqlalchemy import create_engine, or_
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool
    from app.api.v1 import auth as auth_api
    from app.core.database import Base, get_db
    from app.core.rate_limit import LoginFailureLimiter
    from app.core.security import hash_password
    from app.main import app
    from app.models.ai_call_log import AiCallLog
    from app.models.code_file import CodeFile
    from app.models.project import Project
    from app.models.project_member import ProjectMember
    from app.models.rbac import Permission, Role, RolePermission, UserRole
    from app.models.review_task import ReviewTask
    from app.models.user import User

    data = manifest()
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    def row_fields(row):
        return {col.name: getattr(row, col.name) for col in row.__table__.columns}
    def row_hash(row):
        return hashlib.sha256(json.dumps(row_fields(row), default=str, sort_keys=True).encode()).hexdigest()
    with Session() as db:
        hashed = hash_password("LOCAL_ONLY_Secret_Sentinel")
        db.add(Role(id=7, name="local QA", code=m.ROLE_CODE, status="active", is_builtin=0))
        db.add(User(id=1, username="unrelated-real-fixture", role="admin", password=hashed, status=1, token_version=40))
        for name, uid in m.ACCOUNTS.items():
            db.add(User(id=uid, username=data["accounts"][name]["username"], role="user", password=hashed, status=1, token_version=5))
            if name != "no_permission": db.add(UserRole(user_id=uid, role_id=7))
        for index, permission in enumerate(sorted(m.PERMISSIONS), 1):
            db.add(Permission(id=index, code=permission, name=permission, module=permission.split(":")[0]))
            db.add(RolePermission(role_id=7, permission_id=index))
        for pid, uid in m.PROJECTS.items():
            db.add(Project(id=pid, user_id=uid, project_name=f"QA权限验收-20260908-{'a' if pid == 164 else 'b'}", status="active"))
        for row in snap()["memberships"]:
            db.add(ProjectMember(**row))
        for fid, pid in m.FILES.items():
            db.add(CodeFile(id=fid, project_id=pid, file_name="permission.py", file_path="permission.py", language="python", content="print('local fixture')", version_no=2))
        db.add(Project(id=161, user_id=1, project_name="unrelated historical fixture", status="active"))
        db.add(ReviewTask(id=161, user_id=1, project_id=161, task_name="historical state must remain", status="success", total_issues=16, summary="historical summary"))
        db.commit()
        untouched_before = {"user": row_fields(db.get(User, 1)), "project": row_fields(db.get(Project, 161)), "review": row_fields(db.get(ReviewTask, 161))}
        project_before = [row_fields(db.get(Project, pid)) for pid in m.PROJECTS]
        file_before = [row_fields(db.get(CodeFile, fid)) for fid in m.FILES]

    credential = tmp_path / "credentials.json"
    credential.write_text(json.dumps(data))
    credential.chmod(0o600)
    pspec = importlib.util.spec_from_file_location("actual_prepare_20260908", ROOT / "backend/scripts/prepare_permission_acceptance.py")
    prepare = importlib.util.module_from_spec(pspec)
    pspec.loader.exec_module(prepare)
    monkeypatch.setattr(prepare, "SessionLocal", Session)
    monkeypatch.setattr(auth_api, "login_failure_limiter", LoginFailureLimiter(redis_url="", limit=5, window_seconds=60))
    old_overrides = dict(app.dependency_overrides)
    def fixture_db():
        with Session() as db:
            yield db
    app.dependency_overrides[get_db] = fixture_db
    client = TestClient(app)
    tokens, requests, disables = [], [], []
    def open_local(request, timeout):
        assert request.full_url.startswith(m.BASE + "/api/auth/")
        requests.append((request.method, request.full_url.removeprefix(m.BASE)))
        res = client.request(request.method, request.full_url.removeprefix(m.BASE), headers=dict(request.header_items()), content=request.data)
        if request.method == "POST" and res.status_code == 200:
            tokens.append(res.json()["data"]["access_token"])
        body = io.BytesIO(res.content)
        body.code = res.status_code
        return body
    def local_snapshot(role_id):
        assert role_id == 7
        value = snap()
        with Session() as db:
            value["users"] = [{key: getattr(u, key) for key in ("id", "username", "role", "status", "token_version")}
                              for u in db.query(User).filter(User.id.in_(m.ACCOUNTS.values())).order_by(User.id)]
            value["role"] = [{key: getattr(r, key) for key in ("id", "code", "status", "is_builtin")}
                             for r in db.query(Role).filter(or_(Role.id == 7, Role.code == m.ROLE_CODE)).order_by(Role.id)]
            value["assignments"] = [{"user_id": ur.user_id, "role_id": ur.role_id, "code": role.code}
                                    for ur, role in db.query(UserRole, Role).join(Role, Role.id == UserRole.role_id)
                                    .filter(or_(UserRole.user_id.in_(m.ACCOUNTS.values()), UserRole.role_id == 7))
                                    .order_by(UserRole.user_id, UserRole.role_id)]
            value["permissions"] = [code for (code,) in db.query(Permission.code).join(RolePermission, RolePermission.permission_id == Permission.id)
                                    .filter(RolePermission.role_id == 7).order_by(Permission.code)]
            value["projects"] = [{"id": p.id, "user_id": p.user_id, "status": p.status, "marker_match": int(p.project_name == f"QA权限验收-20260908-{'a' if p.id == 164 else 'b'}"), "row_sha256": row_hash(p)}
                                 for p in db.query(Project).filter(or_(Project.id.in_(m.PROJECTS), Project.user_id.in_(m.ACCOUNTS.values()))).order_by(Project.id)]
            value["memberships"] = [{key: getattr(p, key) for key in ("project_id", "user_id", "role_in_project")}
                                    for p in db.query(ProjectMember).filter(or_(ProjectMember.project_id.in_(m.PROJECTS), ProjectMember.user_id.in_(m.ACCOUNTS.values()))).order_by(ProjectMember.project_id, ProjectMember.user_id)]
            value["files"] = [{"id": f.id, "project_id": f.project_id, "status": f.status, "row_sha256": row_hash(f)}
                              for f in db.query(CodeFile).filter(or_(CodeFile.id.in_(m.FILES), CodeFile.project_id.in_(m.PROJECTS))).order_by(CodeFile.id)]
            value["counts"] = {"ai_call_log": db.query(AiCallLog).filter(AiCallLog.user_id.in_(m.ACCOUNTS.values())).count(),
                               "review_task": db.query(ReviewTask).filter(or_(ReviewTask.user_id.in_(m.ACCOUNTS.values()), ReviewTask.project_id.in_(m.PROJECTS))).count()}
        return value
    def actual_disable(_manifest):
        disables.append("disable")
        monkeypatch.setattr(sys, "argv", ["prepare", "--marker", m.MARKER, "--credentials", str(credential), "--disable"])
        prepare.main()
    monkeypatch.setattr(m, "snapshot", local_snapshot)
    monkeypatch.setattr(m, "disable", actual_disable)
    report = {"accounts": []}
    try:
        m.finalize(data, SimpleNamespace(open=open_local), report, lambda: None)
        assert report["status"] == "passed" and disables == ["disable"]
        assert requests.count(("POST", "/api/auth/login")) == 4
        assert requests.count(("GET", "/api/auth/me")) == 8 and len(requests) == 12
        assert all(row["token_version_before"] == 6 and row["token_version_after"] == 7
                   and row["old_token_me_http"] == 403 and row["old_token_me_business_code"] == 40301 for row in report["accounts"])
        assert report["snapshots"]["initial"]["projects"] == report["snapshots"]["after_disable"]["projects"]
        assert report["snapshots"]["initial"]["files"] == report["snapshots"]["after_disable"]["files"]
        output = json.dumps(report) + capsys.readouterr().out
        assert data["accounts"]["owner_a"]["password"] not in output and all(token not in output for token in tokens)
        with Session() as db:
            assert {"user": row_fields(db.get(User, 1)), "project": row_fields(db.get(Project, 161)), "review": row_fields(db.get(ReviewTask, 161))} == untouched_before
            assert [row_fields(db.get(Project, pid)) for pid in m.PROJECTS] == project_before
            assert [row_fields(db.get(CodeFile, fid)) for fid in m.FILES] == file_before
            assert db.query(AiCallLog).count() == 0 and db.query(ReviewTask).count() == 1
    finally:
        client.close()
        app.dependency_overrides.clear()
        app.dependency_overrides.update(old_overrides)
        engine.dispose()
