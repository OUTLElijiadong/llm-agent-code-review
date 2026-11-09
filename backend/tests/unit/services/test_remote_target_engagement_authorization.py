"""远程目标授权只认“已批准的远程网站评估委托”：服务端查询与团队执行闸门。

覆盖 `remote_pentest_service.find_active_authorization` 的各类拒绝条件，以及
`agent_mesh_dispatcher` 在团队执行远程黑盒任务前由服务端置位授权、未命中即结构化阻断。
"""

from __future__ import annotations

import json
from datetime import timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.agents.base import AgentResult
from app.core.database import Base
from app.models.audit_log import AuditLog
from app.models.remote_pentest import RemotePentestEngagement
from app.models.user import User
from app.services import agent_mesh_dispatcher
from app.services import remote_pentest_service as service
from tests.unit.services.remote_rbac_test_helpers import grant_remote_approval

TARGET = "https://example.com/"


@pytest.fixture
def remote_db(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'remote-target-engagement.sqlite3'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def _grant(db, monkeypatch, owner_id: int, approver_id: int) -> None:
    grant_remote_approval(db, approver_id)
    monkeypatch.setattr(
        service.rbac_service,
        "check_permission",
        lambda _db, uid, code: (
            (uid == owner_id and code == "pentest:start") or (uid == approver_id and code == "pentest:authorize")
        ),
    )
    monkeypatch.setattr(service.rbac_service, "is_admin_user", lambda *_args: False)


def _user(db, username: str, role: str = "user") -> User:
    row = User(username=username, password="x", role=role, status=1)
    db.add(row)
    db.commit()
    return row


def _approved(db, monkeypatch, *, target_url: str = TARGET, window_hours: int = 2):
    """走真实的创建、提交、独立批准流程，返回 (申请人, 批准人, 委托行)。"""
    owner = _user(db, "engagement-owner")
    approver = _user(db, "engagement-approver", role="reviewer")
    _grant(db, monkeypatch, owner.id, approver.id)
    task = service.create_task(
        db,
        owner,
        {"target_url": target_url, "authorization_basis": "本人负责维护该测试域名及其 HTTPS 配置"},
    )
    service.submit_authorization(db, owner, task["public_id"], {"ack_rules": True, "window_hours": window_hours})
    service.authorize_task(db, approver, task["public_id"])
    row = db.query(RemotePentestEngagement).filter_by(public_id=task["public_id"]).one()
    return owner, approver, row


def test_authorization_exists_only_after_independent_approval(remote_db, monkeypatch):
    db = remote_db
    owner = _user(db, "draft-owner")
    approver = _user(db, "draft-approver", role="reviewer")
    _grant(db, monkeypatch, owner.id, approver.id)
    task = service.create_task(
        db,
        owner,
        {"target_url": TARGET, "authorization_basis": "本人负责维护该测试域名及其 HTTPS 配置"},
    )
    assert service.find_active_authorization(db, owner.id, TARGET) is None
    service.submit_authorization(db, owner, task["public_id"], {"ack_rules": True, "window_hours": 2})
    assert service.find_active_authorization(db, owner.id, TARGET) is None
    service.authorize_task(db, approver, task["public_id"])

    found = service.find_active_authorization(db, owner.id, TARGET)

    assert found is not None
    assert found.public_id == task["public_id"]


def test_authorization_matches_origin_not_path_and_never_another_origin(remote_db, monkeypatch):
    db = remote_db
    owner, _approver, row = _approved(db, monkeypatch)

    for same_origin in (
        "https://example.com/admin/login?next=/a#x",
        "https://EXAMPLE.com",
        "https://example.com:443/",
        " https://example.com/health ",
        "https://example.com./",
    ):
        found = service.find_active_authorization(db, owner.id, same_origin)
        assert found is not None and found.public_id == row.public_id, same_origin

    for other_origin in (
        "https://other.example.com/",
        "https://sub.example.com/",
        "https://example.org/",
        "https://example.com:8443/",
        "http://example.com/",
        "https://user:pass@example.com/",
        "https://1.2.3.4/",
        "example.com",
        "",
        "not a url",
    ):
        assert service.find_active_authorization(db, owner.id, other_origin) is None, other_origin


def test_authorization_belongs_to_the_applicant_account_only(remote_db, monkeypatch):
    db = remote_db
    owner, approver, _row = _approved(db, monkeypatch)
    stranger = _user(db, "engagement-stranger")

    assert service.find_active_authorization(db, owner.id, TARGET) is not None
    assert service.find_active_authorization(db, stranger.id, TARGET) is None
    # 批准过该委托不等于获得使用权。
    assert service.find_active_authorization(db, approver.id, TARGET) is None


def test_authorization_rejects_closed_or_not_yet_open_window(remote_db, monkeypatch):
    db = remote_db
    owner, _approver, row = _approved(db, monkeypatch)

    row.window_end = service._now() - timedelta(seconds=1)
    db.commit()
    assert service.find_active_authorization(db, owner.id, TARGET) is None

    row.window_start = service._now() + timedelta(hours=1)
    row.window_end = service._now() + timedelta(hours=3)
    db.commit()
    assert service.find_active_authorization(db, owner.id, TARGET) is None

    row.window_start = service._now() - timedelta(minutes=1)
    db.commit()
    assert service.find_active_authorization(db, owner.id, TARGET) is not None


def test_authorization_rejects_tampered_scope_digest(remote_db, monkeypatch):
    db = remote_db
    owner, _approver, row = _approved(db, monkeypatch)

    row.authorization_basis = "批准后被改写的授权依据，摘要未同步更新"
    db.commit()
    assert service.find_active_authorization(db, owner.id, TARGET) is None

    row.authorization_basis = "本人负责维护该测试域名及其 HTTPS 配置"
    row.scope_digest = "0" * 64
    db.commit()
    assert service.find_active_authorization(db, owner.id, TARGET) is None


def test_authorization_requires_a_valid_and_different_approver(remote_db, monkeypatch):
    db = remote_db
    owner, approver, row = _approved(db, monkeypatch)

    monkeypatch.setattr(
        service.rbac_service,
        "check_permission",
        lambda _db, uid, code: uid == owner.id and code == "pentest:start",
    )
    assert service.find_active_authorization(db, owner.id, TARGET) is None

    _grant(db, monkeypatch, owner.id, approver.id)
    assert service.find_active_authorization(db, owner.id, TARGET) is not None

    row.authorized_by_user_id = owner.id
    db.commit()
    assert service.find_active_authorization(db, owner.id, TARGET) is None


def test_authorization_requires_applicant_to_keep_the_start_permission(remote_db, monkeypatch):
    db = remote_db
    owner, approver, _row = _approved(db, monkeypatch)

    monkeypatch.setattr(
        service.rbac_service,
        "check_permission",
        lambda _db, uid, code: uid == approver.id and code == "pentest:authorize",
    )

    assert service.find_active_authorization(db, owner.id, TARGET) is None


@pytest.mark.parametrize(
    ("status", "usable"),
    [
        ("authorized", True),
        # 在“远程网站评估”页点了启动会把状态推进到 running/completed，授权时间窗仍然有效，
        # 否则用户批准后点一下启动，小菱团队就又拿不到授权。
        ("running", True),
        ("completed", True),
        ("draft", False),
        ("pending_authorization", False),
        ("cancelled", False),
        ("expired", False),
        ("failed", False),
    ],
)
def test_authorization_status_gate(remote_db, monkeypatch, status, usable):
    db = remote_db
    owner, _approver, row = _approved(db, monkeypatch)
    row.status = status
    db.commit()

    found = service.find_active_authorization(db, owner.id, TARGET)

    assert (found is not None) is usable


def test_authorization_picks_the_longest_valid_window(remote_db, monkeypatch):
    db = remote_db
    owner, approver, first = _approved(db, monkeypatch, window_hours=1)
    second_task = service.create_task(
        db,
        owner,
        {"target_url": TARGET, "authorization_basis": "再次提交的授权依据，用于更长时间窗"},
    )
    service.submit_authorization(db, owner, second_task["public_id"], {"ack_rules": True, "window_hours": 8})
    service.authorize_task(db, approver, second_task["public_id"])

    found = service.find_active_authorization(db, owner.id, TARGET)

    assert found is not None
    assert found.public_id == second_task["public_id"]
    assert found.public_id != first.public_id


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("https://Example.com/a/b?token=secret#frag", "https://example.com/"),
        ("https://user:pw@example.com:8443/x", "https://example.com:8443/"),
        ("http://example.com", "http://example.com/"),
        ("https://example.com:443/", "https://example.com/"),
        ("not a url", ""),
        ("", ""),
        (None, ""),
    ],
)
def test_redacted_target_origin_drops_credentials_path_and_query(raw, expected):
    assert service.redact_target_origin(raw) == expected


class _FakeOrchestrator:
    """记录 run_project_tests 的真实入参；沙箱立即成功，避免轮询。"""

    def __init__(self):
        self.calls: list[dict] = []
        self.handoffs: list[tuple] = []

    def run_project_tests(self, **kwargs):
        self.calls.append(kwargs)
        return AgentResult(success=True, data={"public_id": "sbx_remote", "status": "succeeded"})


def _run_team_probe(db, monkeypatch, user, payload):
    fake = _FakeOrchestrator()
    monkeypatch.setattr("app.agents.orchestrator.get_request_orchestrator", lambda *_a, **_k: fake)
    monkeypatch.setattr(
        "app.services.agent_team_service.handoff_dependency_runtime_resources",
        lambda *args, **_kwargs: fake.handoffs.append(args) or [],
    )
    message = {
        "user_id": user.id,
        "trace_id": "team-trace",
        "message_id": "team-remote-1",
        "payload": {
            "operation": "run_project_tests",
            "project_id": 31,
            "language": "python",
            "test_mode": "blackbox",
            **payload,
        },
        "context": {"team_id": 9, "agent_team_task_id": 2, "lease_token": "lease-2"},
    }
    result = agent_mesh_dispatcher._runtime_handler(db, user, "test_verifier", message, trusted_team_execution=True)
    return fake, result


def test_team_remote_task_without_engagement_is_blocked_with_a_way_forward(remote_db, monkeypatch):
    db = remote_db
    owner = _user(db, "mesh-no-engagement")

    fake, result = _run_team_probe(
        db, monkeypatch, owner, {"remote_target_url": "https://user:secret@Example.com/health?token=abc"},
    )

    assert result["status"] == "blocked"
    assert result["retryable"] is False
    assert result["errors"][0]["code"] == "remote_target_authorization_required"
    assert result["next_action"] == {"open_path": "/remote-pentests/start", "target_url": "https://example.com/"}
    assert "尚未获得独立授权" in result["summary"]
    assert "另一位审批人" in result["summary"]
    assert fake.calls == []
    assert fake.handoffs == []
    serialized = json.dumps(result, ensure_ascii=False)
    assert "secret" not in serialized and "token=abc" not in serialized and "/health" not in serialized


def test_team_remote_task_with_approved_engagement_is_authorized_by_the_server_and_audited(remote_db, monkeypatch):
    db = remote_db
    owner, _approver, row = _approved(db, monkeypatch)

    fake, result = _run_team_probe(db, monkeypatch, owner, {"remote_target_url": "https://example.com/health"})

    assert result["status"] == "completed"
    assert len(fake.calls) == 1
    assert fake.calls[0]["remote_target_url"] == "https://example.com/health"
    assert fake.calls[0]["remote_target_authorized"] is True
    evidence = result["evidence"][0]
    assert evidence["source"] == "remote_pentest_authorization"
    assert evidence["data"]["engagement_public_id"] == row.public_id
    assert evidence["data"]["target_origin"] == "https://example.com/"
    audit = db.query(AuditLog).filter_by(action="remote_pentest_authorization_used").one()
    assert audit.actor_id == owner.id
    assert audit.target_id == row.public_id
    assert "team=9" in audit.detail and "task=2" in audit.detail


def test_authorization_booleans_written_by_the_model_are_ignored(remote_db, monkeypatch):
    db = remote_db
    owner = _user(db, "mesh-self-authorizing")

    fake, result = _run_team_probe(
        db,
        monkeypatch,
        owner,
        {
            "remote_target_url": TARGET,
            "remote_target_authorized": True,
            "remote_target_approval_token": "9.not-issued-by-the-server",
        },
    )

    assert result["status"] == "blocked"
    assert result["errors"][0]["code"] == "remote_target_authorization_required"
    assert fake.calls == []


def test_server_decision_does_not_depend_on_the_model_boolean_when_engagement_exists(remote_db, monkeypatch):
    db = remote_db
    owner, _approver, _row = _approved(db, monkeypatch)

    fake, result = _run_team_probe(
        db, monkeypatch, owner, {"remote_target_url": TARGET, "remote_target_authorized": False},
    )

    assert result["status"] == "completed"
    assert fake.calls[0]["remote_target_authorized"] is True


def test_authorization_boolean_never_reaches_a_local_validation(remote_db, monkeypatch):
    db = remote_db
    owner = _user(db, "mesh-local-validation")

    fake, result = _run_team_probe(
        db, monkeypatch, owner, {"remote_target_authorized": True, "remote_target_approval_token": "1.x"},
    )

    assert result["status"] == "completed"
    assert fake.calls[0]["remote_target_url"] == ""
    assert fake.calls[0]["remote_target_authorized"] is False
    assert db.query(AuditLog).filter_by(action="remote_pentest_authorization_used").count() == 0


def test_another_account_cannot_borrow_an_approved_engagement(remote_db, monkeypatch):
    db = remote_db
    _owner, _approver, _row = _approved(db, monkeypatch)
    stranger = _user(db, "mesh-stranger")

    fake, result = _run_team_probe(db, monkeypatch, stranger, {"remote_target_url": TARGET})

    assert result["status"] == "blocked"
    assert result["errors"][0]["code"] == "remote_target_authorization_required"
    assert fake.calls == []


def test_engagement_withdrawn_after_planning_blocks_execution(remote_db, monkeypatch):
    db = remote_db
    owner, _approver, row = _approved(db, monkeypatch)
    row.status = "cancelled"
    db.commit()

    fake, result = _run_team_probe(db, monkeypatch, owner, {"remote_target_url": TARGET})

    assert result["status"] == "blocked"
    assert fake.calls == []
    assert fake.handoffs == []
