"""审批决定与运行取消必须按相同顺序取得并发锁。"""

import asyncio
from types import SimpleNamespace

import pytest

from app.core.exceptions import ConflictError
from app.models.enterprise_assistant_runtime import EnterpriseAssistantApproval
from app.services import enterprise_assistant_runtime_service as runtime


def test_approval_decision_locks_run_state_company_member_then_approval(monkeypatch):
    lock_order: list[str] = []
    locator = SimpleNamespace(
        id=41,
        enterprise_id=10,
        assistant_run_id=51,
        conversation_id=61,
        run_public_id="approval-lock-order",
        initiator_user_id=71,
        status="cancelled",
    )
    approval_rebound_after_locator_read = SimpleNamespace(
        id=41,
        enterprise_id=10,
        assistant_run_id=51,
        conversation_id=61,
        run_public_id="approval-rebound-to-another-run",
        initiator_user_id=71,
        status="pending",
    )

    class ApprovalQuery:
        def __init__(self):
            self.locking = False

        def filter_by(self, **_kwargs):
            return self

        def populate_existing(self):
            return self

        def with_for_update(self):
            self.locking = True
            lock_order.append("approval_lock")
            return self

        def first(self):
            if not self.locking:
                lock_order.append("approval_locator_read")
                return locator
            return approval_rebound_after_locator_read

    class WorkSession:
        def query(self, model):
            assert model is EnterpriseAssistantApproval
            return ApprovalQuery()

    work = WorkSession()

    class Transaction:
        def __enter__(self):
            return work

        def __exit__(self, *_exc):
            return False

    class Factory:
        def begin(self):
            return Transaction()

    run = SimpleNamespace(id=51, public_id="approval-lock-order")
    state = SimpleNamespace(
        assistant_run_id=51,
        enterprise_id=10,
        conversation_id=61,
        initiator_user_id=71,
        source_message_id=81,
        permission_snapshot_json="{}",
        permission_snapshot_sha256="snapshot",
        status=runtime.CANCELLED,
        checkpoint_json="{}",
        lease_expires_at=None,
    )
    company = SimpleNamespace(status="active")

    monkeypatch.setattr(runtime, "_factory", lambda _db: Factory())

    def lock_run(_db, *_args, lock=False):
        assert lock is True
        lock_order.append("run_lock")
        return run

    def lock_state(_db, **kwargs):
        assert kwargs["lock"] is True
        lock_order.append("state_lock")
        return state

    monkeypatch.setattr(runtime, "_run_record", lock_run)
    monkeypatch.setattr(runtime, "_state_for_run", lock_state)

    def lock_company(_db, _enterprise_id, *, lock=False):
        assert lock is True
        lock_order.append("company_lock")
        return company

    def lock_admin_member(_db, _enterprise_id, _user_id, *, lock=False):
        assert lock is True
        lock_order.append("member_lock")

    monkeypatch.setattr(runtime.enterprise_org_service, "_company", lock_company)
    monkeypatch.setattr(runtime.enterprise_org_service, "require_enterprise_admin", lock_admin_member)
    monkeypatch.setattr(runtime.assistant_service, "_require_subscription", lambda *_args: None)
    monkeypatch.setattr(runtime, "_live_role_snapshot", lambda *_args: {})

    with pytest.raises(ConflictError, match="审批关联运行已变化"):
        asyncio.run(runtime.decide_approval(
            object(), enterprise_id=10, approval_id=41, user_id=91, decision="approve",
        ))

    assert lock_order == [
        "approval_locator_read",
        "run_lock",
        "state_lock",
        "company_lock",
        "member_lock",
        "approval_lock",
    ]
