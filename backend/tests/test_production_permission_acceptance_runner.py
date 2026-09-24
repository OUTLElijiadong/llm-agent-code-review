"""生产验收脚本先在隔离SQLite上执行真实登录与API；不向外部发送请求。"""

import importlib.util
import io
import json
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.database import Base, get_db
from app.core.security import hash_password
from app.main import app
from app.models.ai_call_log import AiCallLog
from app.models.project import Project
from app.models.rbac import Permission, Role, RolePermission, UserRole
from app.models.review_task import ReviewTask
from app.models.user import User

PATH = Path(__file__).resolve().parents[1] / "scripts/verify_permission_acceptance_https.py"
SPEC = importlib.util.spec_from_file_location("permission_https_runner", PATH)
runner_module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(runner_module)


def test_private_credentials_reject_symlink_and_loose_mode(tmp_path):
    path = tmp_path / "secret.json"
    path.write_text('{"private":true}')
    path.chmod(0o644)
    with pytest.raises(ValueError):
        runner_module.private_json(path)
    path.chmod(0o600)
    assert runner_module.private_json(path) == {"private": True}
    link = tmp_path / "link.json"
    link.symlink_to(path)
    with pytest.raises(OSError):
        runner_module.private_json(link)


@pytest.mark.parametrize(
    "url", ["http://example.invalid", "https://user:password@example.invalid", "https://example.invalid/api"]
)
def test_runner_rejects_unsafe_base_url_before_output_or_network(tmp_path, url):
    with pytest.raises(ValueError):
        runner_module.Runner(url, {"marker": "local"}, None, tmp_path / "log.json")
    assert not (tmp_path / "log.json").exists()


def test_plan_matches_actual_routes_and_rejects_changed_source(tmp_path):
    plan = runner_module.build_plan()
    runner_module.validate_plan(plan, PATH.parents[1])
    assert sum(row["anonymous"] == "ready" for row in plan["routes"]) == 321
    assert sum(row["no_permission"] == "ready" for row in plan["routes"]) == 252
    private_assets = {
        "/api/agent-responses/runs/{run_id}/assets",
        "/api/agent-responses/assets/{asset_id}/image",
    }
    asset_routes = [row for row in plan["routes"] if row["path"] in private_assets]
    assert {row["path"] for row in asset_routes} == private_assets
    assert all(row["method"] == "GET" and row["anonymous"] == row["no_permission"] == "ready"
               for row in asset_routes)
    assert all(row["guard"] == "app.core.rbac_dependency.require_permission.<locals>._dependency"
               for row in asset_routes)
    plan["source_sha256"]["app/core/dependencies.py"] = "0" * 64
    with pytest.raises(ValueError, match="不匹配"):
        runner_module.validate_plan(plan, PATH.parents[1])


def test_plan_is_portable_to_deployment_without_local_virtualenv(tmp_path):
    plan = runner_module.build_plan()
    assert all(name.startswith('app/') or name in {'scripts/verify_permission_acceptance_https.py', 'requirements.lock'}
               for name in plan['source_sha256'])
    for row in plan['routes']:
        if row['path'] in {'/api/auth/captcha', '/api/auth/register'}:
            assert row['endpoint'] == 'app/api/v1/auth.py'
            assert row['anonymous'] == row['no_permission'] == 'blocked'
    deployment = tmp_path / 'deployed-app'
    for relative in plan['source_sha256']:
        target = deployment / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(PATH.parents[1] / relative, target)
    assert not (deployment / '.venv').exists()
    runner_module.validate_plan(plan, deployment)


def test_runner_actual_login_permissions_crud_and_negative_matrix_without_network(tmp_path):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)()
    marker = "localrunner"
    manifest = {
        "marker": marker,
        "role_code": "user",
        "role_origin": "existing_builtin",
        "accounts": {},
    }
    role = Role(name="普通用户", code="user", status="active", is_builtin=1)
    db.add(role)
    db.flush()
    for code in runner_module.EXPECTED_PERMISSIONS:
        permission = Permission(code=code, name=code, module=code.split(":")[0], type="api")
        db.add(permission)
        db.flush()
        db.add(RolePermission(role_id=role.id, permission_id=permission.id))
    for account in runner_module.ACCOUNTS:
        user = User(
            username=f"qa_{marker}_{account}",
            password=hash_password("local-acceptance-password"),
            role="user",
            status=1,
        )
        db.add(user)
        db.flush()
        manifest["accounts"][account] = {
            "id": user.id,
            "username": user.username,
            "password": "local-acceptance-password",
        }
        if account != "no_permission":
            db.add(UserRole(user_id=user.id, role_id=role.id))
    db.commit()

    def fixture_db():
        yield db

    previous = dict(app.dependency_overrides)
    app.dependency_overrides.clear()
    app.dependency_overrides[get_db] = fixture_db
    client = TestClient(app)  # 不进入context，不启动真实scheduler/lifespan。

    def open_local(request, timeout):
        response = client.request(
            request.method,
            request.full_url.replace("https://example.invalid", ""),
            headers=dict(request.header_items()),
            content=request.data,
        )
        body = io.BytesIO(response.content)
        body.code = response.status_code
        body.headers = response.headers
        return body

    try:
        output = tmp_path / "evidence.json"
        runner = runner_module.Runner(
            "https://example.invalid", manifest, runner_module.build_plan(), output, interval=0
        )
        runner.client = SimpleNamespace(open=open_local)
        runner.run()
        assert runner.log["status"] == "passed"
        assert runner.log["summary"].get("failed", 0) == 0
        assert db.query(Project).count() == 2
        assert db.query(ReviewTask).count() == db.query(AiCallLog).count() == 0
        evidence = output.read_text()
        assert "local-acceptance-password" not in evidence
        assert all(token not in evidence for token in runner.tokens.values())
        saved = json.loads(evidence)
        assert len(saved["requests"]) > 600
        assert saved["resources"]["a"]["reviewer_user_id"] == manifest["accounts"]["member_a"]["id"]
    finally:
        client.close()
        app.dependency_overrides.clear()
        app.dependency_overrides.update(previous)
        db.close()
        engine.dispose()
