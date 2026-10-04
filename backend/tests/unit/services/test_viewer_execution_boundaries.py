"""Only local rows and benign stubs: no model, network, worker or scan execution."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy.orm import Session

from app.agents.base import AgentContext, AgentResult
from app.agents.fullchain_audit_agent import FullChainAuditOrchestrator
from app.agents.security_sentinel_agent import SecuritySentinelAgent
from app.core.exceptions import ForbiddenError
from app.core.permission_codes import PermissionCode
from app.models.agent_capability import SandboxEnvironment, SandboxWorker
from app.models.code_file import CodeFile
from app.models.project import Project
from app.models.project_member import ProjectMember
from app.models.rbac import Permission, Role, RolePermission, UserRole
from app.models.review_task import ReviewTask
from app.models.user import User
from app.services import managed_mcp_adapter
from app.services import sandbox_service as sandbox


def seed(db, *, member_role="viewer", global_permissions=None):
    owner = User(username="boundary-owner", password="local", role="user", status=1)
    actor = User(username="boundary-actor", password="local", role="reviewer", status=1)
    role = Role(name="local reviewer", code="reviewer", status="active")
    db.add_all([owner, actor, role])
    db.flush()
    project = Project(user_id=owner.id, project_name="benign local scope", language="python", status="active")
    db.add(project)
    db.flush()
    member = ProjectMember(project_id=project.id, user_id=actor.id, role_in_project=member_role)
    file = CodeFile(
        project_id=project.id,
        file_name="main.py",
        file_path="main.py",
        language="python",
        content="print('ok')\n",
        size_bytes=12,
        raw_size=12,
        line_count=1,
        status="active",
    )
    task = ReviewTask(project_id=project.id, user_id=actor.id, task_name="local existing", status="success")
    db.add_all([member, file, task, UserRole(user_id=actor.id, role_id=role.id)])
    permissions = (
        global_permissions
        if global_permissions is not None
        else [
            PermissionCode.PROJECT_VIEW,
            PermissionCode.FILE_VIEW,
            PermissionCode.SECURITY_SCAN,
        ]
    )
    for code in permissions:
        permission = Permission(code=code, name=code, module="local", type="api")
        db.add(permission)
        db.flush()
        db.add(RolePermission(role_id=role.id, permission_id=permission.id))
    env = SandboxEnvironment(
        public_id="sbx_local_viewer",
        project_id=project.id,
        owner_id=actor.id,
        agent_code="test_verifier",
        purpose="test",
        language="python",
        test_mode="whitebox",
        status="queued",
        runtime="runsc",
        image_ref="local-unused",
        source_sha256="0" * 64,
        resource_policy_json="{}",
        agent_config_json="{}",
        execution_token="local-lease",
        expires_at=datetime.utcnow() + timedelta(hours=1),
    )
    db.add(env)
    db.commit()
    return SimpleNamespace(owner=owner, actor=actor, project=project, member=member, file=file, task=task, env=env)


@pytest.mark.parametrize("entry", ["create", "mcp_create", "authorize"])
def test_viewer_rejected_before_environment_or_target_work(db, monkeypatch, tmp_path, entry):
    rows = seed(db)
    marker = tmp_path / "maintenance"
    marker.write_text("local")
    monkeypatch.setattr(sandbox.settings, "sandbox_maintenance_file", str(marker))
    calls = []

    def pin(*args, **kwargs):
        calls.append("target")
        raise AssertionError("no network operation permitted")

    monkeypatch.setattr(sandbox, "pin_public_http_url", pin)
    monkeypatch.setattr(managed_mcp_adapter, "managed_kind_ready", lambda *a: True)
    with pytest.raises(ForbiddenError):
        if entry == "authorize":
            sandbox.issue_remote_target_authorization(
                db,
                rows.actor,
                project_id=rows.project.id,
                remote_target_url="https://example.invalid/",
                test_mode="blackbox",
                confirmed=True,
            )
        elif entry == "mcp_create":
            managed_mcp_adapter.call_managed_tool(
                db,
                rows.actor,
                "prism-sandbox",
                "create_test",
                {"project_id": rows.project.id, "language": "python", "test_mode": "whitebox"},
            )
        else:
            sandbox.create_environment(
                db,
                rows.actor,
                {"project_id": rows.project.id, "purpose": "test", "language": "python", "test_mode": "whitebox"},
            )
    assert calls == []
    assert db.query(SandboxEnvironment).count() == 1


@pytest.mark.parametrize("entry", ["extend", "browser", "preview", "preview_token"])
def test_downgraded_creator_cannot_continue_execution(db, monkeypatch, entry):
    rows = seed(db)
    rows.env.purpose = "deploy" if entry.startswith("preview") else "test"
    rows.env.status = "ready" if entry.startswith("preview") else "running"
    db.commit()
    monkeypatch.setattr(sandbox, "_call_worker", lambda *a, **k: pytest.fail("worker must not run"))
    with pytest.raises(ForbiddenError):
        if entry == "extend":
            sandbox.extend_environment(db, rows.actor, rows.env.public_id, 1)
        elif entry == "browser":
            sandbox.run_browser_blackbox(db, rows.actor, rows.env.public_id, "https://example.invalid/")
        elif entry == "preview":
            sandbox.create_preview_session(db, rows.actor, rows.env.public_id)
        else:
            import jwt

            token = jwt.encode(
                {
                    "sub": str(rows.actor.id),
                    "ver": rows.actor.token_version,
                    "typ": "sandbox_preview",
                    "sbx": rows.env.public_id,
                    "exp": int((datetime.now(timezone.utc) + timedelta(minutes=1)).timestamp()),
                },
                sandbox.settings.jwt_secret,
                algorithm=sandbox.settings.jwt_algorithm,
            )
            sandbox.authenticate_preview_session(db, rows.env.public_id, token)


def test_viewer_capability_false_but_own_cleanup_and_metadata_remain(db):
    rows = seed(db)
    payload = sandbox.environment_to_dict(db, rows.env, rows.actor)
    assert payload.get("can_execute") is False
    assert payload.get("can_stop") is True
    assert sandbox.get_environment(db, rows.actor, rows.env.public_id)["public_id"] == rows.env.public_id
    result = sandbox.stop_environment(db, rows.actor, rows.env.public_id)
    assert result["status"] == "stopped"


@pytest.mark.parametrize("scope", ["file", "task", "project"])
def test_sentinel_viewer_fails_before_model_static_or_archive_work(db, monkeypatch, scope):
    rows = seed(db)
    agent = SecuritySentinelAgent()
    agent.inject(db, rows.actor)
    monkeypatch.setattr(agent, "call_json", lambda *a, **k: pytest.fail("model must not run"))
    monkeypatch.setattr(agent, "_emit", lambda *a, **k: None)
    monkeypatch.setattr(
        "app.agents.security_sentinel_agent.scan_secrets", lambda *a, **k: pytest.fail("static must not run")
    )
    monkeypatch.setattr(
        "app.agents.security_sentinel_agent.project_source_service.begin_source_archive_audit",
        lambda *a, **k: pytest.fail("archive must not run"),
    )
    result = {
        "file": lambda: agent.scan_file(rows.file.id),
        "task": lambda: agent.scan_task(rows.task.id),
        "project": lambda: agent.scan_project(rows.project.id),
    }[scope]()
    assert result.success is False
    assert result.failure_kind == "authorization_revoked"


def test_fullchain_explicit_actor_cannot_bypass_unbound_sentinel(db, monkeypatch):
    rows = seed(db)
    agent = SecuritySentinelAgent()
    agent.inject(db)
    chain = FullChainAuditOrchestrator(agent)
    calls = []
    monkeypatch.setattr(agent, "_emit", lambda *a, **k: None)
    monkeypatch.setattr(chain, "_recon", lambda *a, **k: calls.append("recon"))
    monkeypatch.setattr(chain, "_analysis", lambda *a, **k: AgentResult(success=False, error="benign stop"))
    result = chain.run(rows.project.id, rows.actor)
    assert result.success is False
    assert result.failure_kind == "authorization_revoked"
    assert calls == []


def test_execution_permission_revoked_does_not_revoke_cleanup_lease(db):
    rows = seed(db, member_role="reviewer")
    sandbox._require_execution_lease(db, rows.env.id, "local-lease")
    rows.member.role_in_project = "viewer"
    db.commit()
    assert sandbox._execution_lease_valid(db, rows.env.id, "local-lease") is True
    with pytest.raises(ForbiddenError):
        sandbox._require_execution_lease(db, rows.env.id, "local-lease")
    sandbox._commit_execution(db, rows.env.id, "local-lease")


@pytest.mark.parametrize("role", ["reviewer", "owner"])
def test_authorized_creator_keeps_capability_and_cleanup(db, role):
    rows = seed(db, member_role=role)
    sandbox._require_execution_lease(db, rows.env.id, "local-lease")
    assert sandbox.environment_to_dict(db, rows.env, rows.actor)["can_execute"] is True
    assert sandbox.environment_to_dict(db, rows.env, None)["can_execute"] is False
    assert sandbox.stop_environment(db, rows.actor, rows.env.public_id)["status"] == "stopped"


@pytest.mark.parametrize("missing", [PermissionCode.PROJECT_VIEW, PermissionCode.FILE_VIEW])
def test_project_execution_does_not_override_missing_global_permission(db, monkeypatch, tmp_path, missing):
    grants = [p for p in [PermissionCode.PROJECT_VIEW, PermissionCode.FILE_VIEW] if p != missing]
    rows = seed(db, member_role="reviewer", global_permissions=grants)
    marker = tmp_path / "maintenance"
    marker.write_text("local")
    monkeypatch.setattr(sandbox.settings, "sandbox_maintenance_file", str(marker))
    with pytest.raises(ForbiddenError):
        sandbox.create_environment(
            db,
            rows.actor,
            {"project_id": rows.project.id, "purpose": "test", "language": "python", "test_mode": "whitebox"},
        )
    assert sandbox.environment_to_dict(db, rows.env, rows.actor)["can_execute"] is False
    assert sandbox.stop_environment(db, rows.actor, rows.env.public_id)["status"] == "stopped"


@pytest.mark.parametrize("member_role", ["viewer", "unknown", "", "VIEWER"])
def test_sandbox_queued_work_rechecks_role_and_still_reclaims_worker(db, monkeypatch, member_role):
    rows = seed(db, member_role=member_role)
    worker = SandboxWorker(
        code="local-unused",
        name="local-unused",
        worker_type="local",
        endpoint="unused",
        supported_languages_json='["python"]',
        supported_modes_json='["whitebox"]',
    )
    db.add(worker)
    db.flush()
    rows.env.worker_id = worker.id
    db.commit()
    monkeypatch.setattr(sandbox, "SessionLocal", lambda: Session(bind=db.get_bind(), expire_on_commit=False))
    monkeypatch.setattr(sandbox, "_call_worker", lambda *a, **k: pytest.fail("no worker execution"))
    monkeypatch.setattr(sandbox, "configure_subagent", lambda *a, **k: pytest.fail("no model setup"))
    cleanup = []
    monkeypatch.setattr(sandbox, "_stop_registered_worker_requests", lambda *a: cleanup.append(rows.env.public_id))
    monkeypatch.setattr(sandbox, "_emit", lambda *a, **k: None)
    sandbox._execute_environment(rows.env.id, execution_token="local-lease")
    db.refresh(rows.env)
    assert rows.env.status == "failed"
    assert cleanup == [rows.env.public_id]
    assert sandbox._execution_lease_valid(db, rows.env.id, "local-lease") is True


def test_model_guard_rechecks_fresh_role_before_previous_callback(db):
    rows = seed(db, member_role="reviewer")
    agent = SecuritySentinelAgent()
    agent.inject(db, rows.actor)
    previous = []
    original = AgentContext(project_id=rows.project.id, extra={"before_model_call": lambda: previous.append("old")})
    guarded = agent._execution_context(rows.project.id, original)
    assert guarded is not original
    guarded.extra["before_model_call"]()
    assert previous == ["old"]
    with Session(bind=db.get_bind()) as writer:
        writer.query(ProjectMember).filter_by(id=rows.member.id).update({"role_in_project": "viewer"})
        writer.commit()
    with pytest.raises(ForbiddenError):
        guarded.extra["before_model_call"]()
    with pytest.raises(ForbiddenError):
        sandbox._execution_model_guard(db, rows.env)()
    assert previous == ["old"]


def test_security_scope_also_requires_existing_global_scan_permission(db):
    rows = seed(db, member_role="reviewer", global_permissions=[PermissionCode.PROJECT_VIEW, PermissionCode.FILE_VIEW])
    agent = SecuritySentinelAgent()
    agent.inject(db, rows.actor)
    result = agent.scan_task(rows.task.id)
    assert result.success is False
    assert result.failure_kind == "authorization_revoked"


def test_fullchain_stops_between_phases_after_role_downgrade(db, monkeypatch):
    rows = seed(db, member_role="reviewer")
    agent = SecuritySentinelAgent()
    agent.inject(db)
    chain = FullChainAuditOrchestrator(agent)
    calls = []

    def benign_recon(*args):
        calls.append("recon")
        with Session(bind=db.get_bind()) as writer:
            writer.query(ProjectMember).filter_by(id=rows.member.id).update({"role_in_project": "viewer"})
            writer.commit()

    monkeypatch.setattr(chain, "_recon", benign_recon)
    monkeypatch.setattr(chain, "_analysis", lambda *a: pytest.fail("revoked analysis must not start"))
    monkeypatch.setattr(agent, "_emit", lambda *a, **k: None)
    result = chain.run(rows.project.id, rows.actor)
    assert result.failure_kind == "authorization_revoked"
    assert calls == ["recon"]


def test_fullchain_authorized_reviewer_keeps_benign_stubbed_phase_sequence(db, monkeypatch):
    rows = seed(db, member_role="reviewer")
    agent = SecuritySentinelAgent()
    agent.inject(db, rows.actor)
    chain = FullChainAuditOrchestrator(agent)
    calls = []
    monkeypatch.setattr(chain, "_recon", lambda *a: calls.append("recon"))
    monkeypatch.setattr(chain, "_analysis", lambda *a: (calls.append("analysis") or AgentResult(success=True, data={})))
    monkeypatch.setattr(chain, "_verification", lambda *a, **k: (calls.append("verification") or {}))
    monkeypatch.setattr(chain, "_report", lambda *a: (calls.append("report") or {}))
    monkeypatch.setattr(agent, "_emit", lambda *a, **k: None)
    result = chain.run(rows.project.id, rows.actor)
    assert result.success is True
    assert calls == ["recon", "analysis", "verification", "report"]


def test_cleanup_capability_keeps_exact_original_creator_and_super_admin_scope(db, super_admin_user):
    rows = seed(db)
    ordinary_admin = User(username="ordinary-local-admin", password="local", role="admin", status=1)
    db.add(ordinary_admin)
    db.commit()
    assert sandbox.environment_to_dict(db, rows.env, rows.actor)["can_stop"] is True
    assert sandbox.environment_to_dict(db, rows.env, rows.owner)["can_stop"] is False
    assert sandbox.environment_to_dict(db, rows.env, ordinary_admin)["can_stop"] is False
    assert sandbox.environment_to_dict(db, rows.env, ordinary_admin)["can_execute"] is False
    assert sandbox.environment_to_dict(db, rows.env, super_admin_user)["can_stop"] is True
    assert sandbox.environment_to_dict(db, rows.env, super_admin_user)["can_execute"] is True
    assert sandbox.environment_to_dict(db, rows.env, None)["can_stop"] is False
    rows.member.role_in_project = "unknown"
    db.commit()
    assert sandbox.environment_to_dict(db, rows.env, rows.actor)["can_stop"] is False


@pytest.mark.parametrize("scope", ["reviewer", "owner", "admin"])
def test_authorized_creation_keeps_original_maintenance_boundary(db, admin_user, monkeypatch, tmp_path, scope):
    from app.core.exceptions import ServiceUnavailableError

    rows = seed(db, member_role="reviewer")
    actor = admin_user if scope == "admin" else rows.actor
    if scope == "owner":
        rows.project.user_id = rows.actor.id
        db.commit()
    marker = tmp_path / "maintenance"
    marker.write_text("local")
    monkeypatch.setattr(sandbox.settings, "sandbox_maintenance_file", str(marker))
    with pytest.raises(ServiceUnavailableError, match="维护"):
        sandbox.create_environment(
            db,
            actor,
            {"project_id": rows.project.id, "purpose": "test", "language": "python", "test_mode": "whitebox"},
        )
    assert db.query(SandboxEnvironment).count() == 1


def test_model_context_keeps_original_actor_when_agent_is_reinjected(db):
    rows = seed(db, member_role="reviewer")
    agent = SecuritySentinelAgent()
    agent.inject(db, rows.actor)
    guarded = agent._execution_context(rows.project.id, AgentContext(project_id=rows.project.id))
    agent.inject(db, rows.owner)
    guarded.extra["before_model_call"]()
    rows.member.role_in_project = "viewer"
    db.commit()
    with pytest.raises(ForbiddenError):
        guarded.extra["before_model_call"]()


def test_disabled_executor_denied_while_cleanup_token_remains_valid(db):
    rows = seed(db, member_role="reviewer")
    rows.actor.status = 0
    db.commit()
    with pytest.raises(ForbiddenError):
        sandbox._require_execution_lease(db, rows.env.id, "local-lease")
    assert sandbox._execution_lease_valid(db, rows.env.id, "local-lease") is True


@pytest.mark.parametrize("member_role, expected", [("reviewer", True), ("viewer", False)])
def test_shared_preview_keeps_original_noncreator_scope(db, member_role, expected):
    rows = seed(db, member_role=member_role)
    rows.env.owner_id = rows.owner.id
    db.commit()
    payload = sandbox.environment_to_dict(db, rows.env, rows.actor)
    assert payload["can_execute"] is False
    assert payload["can_stop"] is False
    assert payload["can_preview"] is expected


@pytest.mark.parametrize("missing", [PermissionCode.PROJECT_VIEW, PermissionCode.FILE_VIEW])
def test_preview_capability_denies_missing_global_point(db, missing):
    grants = [p for p in [PermissionCode.PROJECT_VIEW, PermissionCode.FILE_VIEW] if p != missing]
    rows = seed(db, member_role="reviewer", global_permissions=grants)
    assert sandbox.environment_to_dict(db, rows.env, rows.actor)["can_preview"] is False
