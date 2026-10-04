"""远程黑盒目标的服务端确认凭证必须逐账号、逐目标、逐模式、单次消费。"""

from __future__ import annotations

import json
from datetime import timedelta
from types import SimpleNamespace

import pytest

from app.core.exceptions import ForbiddenError
from app.models.agent_governance import ApprovalItem
from app.models.project import Project
from app.models.user import User
from app.services import sandbox_service


@pytest.fixture(autouse=True)
def _stub_public_dns(monkeypatch):
    monkeypatch.setattr(
        sandbox_service,
        "pin_public_http_url",
        lambda value, **_kwargs: SimpleNamespace(original_url=value),
    )
    monkeypatch.setattr(sandbox_service.audit_service, "log", lambda *_args, **_kwargs: None)


def _issue(db, actor=None):
    supplied = actor or SimpleNamespace(id=7, username="reviewer")
    # This ticket-consumption fixture is an authorized local admin; project/global
    # denial is tested independently against actual permission rows.
    actor = db.get(User, supplied.id)
    if actor is None:
        actor = User(id=supplied.id, username=f"ticket-local-{supplied.id}", password="local", role="admin", status=1)
        db.add(actor)
    if db.get(Project, 91) is None:
        db.add(Project(id=91, user_id=actor.id, project_name="local authorized target", status="active"))
    db.commit()
    return sandbox_service.issue_remote_target_authorization(
        db,
        actor,
        project_id=91,
        remote_target_url="https://target.example/path",
        test_mode="blackbox",
        confirmed=True,
    )


def test_direct_rest_boolean_is_not_a_server_approval():
    with pytest.raises(ForbiddenError, match="服务端逐目标确认"):
        sandbox_service._require_remote_target_authorization(
            {"remote_target_authorized": True},
            server_approval_required=True,
        )

    sandbox_service._require_remote_target_authorization(
        {"remote_target_approval_token": "41.secret"},
        server_approval_required=True,
    )


def test_remote_target_approval_is_bound_and_consumed_once(db):
    actor = SimpleNamespace(id=7, username="reviewer")
    ticket = _issue(db, actor)

    approval_id = sandbox_service._consume_remote_target_authorization(
        db,
        actor,
        approval_token=ticket["approval_token"],
        project_id=91,
        remote_target_url="https://target.example/path",
        test_mode="blackbox",
        sandbox_public_id="sbx_authorized_1",
    )
    db.commit()
    row = db.get(ApprovalItem, approval_id)
    request = json.loads(row.request_json)
    assert request["consumed_by"] == "sbx_authorized_1"

    with pytest.raises(ForbiddenError, match="已过期/使用"):
        sandbox_service._consume_remote_target_authorization(
            db,
            actor,
            approval_token=ticket["approval_token"],
            project_id=91,
            remote_target_url="https://target.example/path",
            test_mode="blackbox",
            sandbox_public_id="sbx_authorized_2",
        )


@pytest.mark.parametrize(
    "overrides",
    [
        {"project_id": 92},
        {"remote_target_url": "https://other.example/path"},
        {"test_mode": "combined"},
    ],
)
def test_remote_target_approval_rejects_changed_scope(db, overrides):
    actor = SimpleNamespace(id=7, username="reviewer")
    ticket = _issue(db, actor)
    arguments = {
        "approval_token": ticket["approval_token"],
        "project_id": 91,
        "remote_target_url": "https://target.example/path",
        "test_mode": "blackbox",
        "sandbox_public_id": "sbx_changed_scope",
    }
    arguments.update(overrides)

    with pytest.raises(ForbiddenError, match="账号、项目、目标、模式"):
        sandbox_service._consume_remote_target_authorization(db, actor, **arguments)


def test_remote_target_approval_rejects_other_account_replay(db):
    actor = SimpleNamespace(id=7, username="reviewer")
    ticket = _issue(db, actor)

    with pytest.raises(ForbiddenError, match="账号、项目、目标、模式"):
        sandbox_service._consume_remote_target_authorization(
            db,
            SimpleNamespace(id=8, username="another"),
            approval_token=ticket["approval_token"],
            project_id=91,
            remote_target_url="https://target.example/path",
            test_mode="blackbox",
            sandbox_public_id="sbx_cross_account",
        )


def test_remote_target_approval_rejects_expired_ticket(db):
    actor = SimpleNamespace(id=7, username="reviewer")
    ticket = _issue(db, actor)
    approval_id = int(ticket["approval_token"].split(".", 1)[0])
    approval = db.get(ApprovalItem, approval_id)
    request = json.loads(approval.request_json)
    request["expires_at"] = (sandbox_service._utcnow() - timedelta(seconds=1)).isoformat()
    approval.request_json = json.dumps(request)
    db.flush()

    with pytest.raises(ForbiddenError, match="账号、项目、目标、模式"):
        sandbox_service._consume_remote_target_authorization(
            db,
            actor,
            approval_token=ticket["approval_token"],
            project_id=91,
            remote_target_url="https://target.example/path",
            test_mode="blackbox",
            sandbox_public_id="sbx_expired",
        )
