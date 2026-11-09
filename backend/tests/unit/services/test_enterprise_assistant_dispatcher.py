"""The recovery scanner only dispatches unstopped durable queue items."""

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.database import Base
from app.models.enterprise_assistant_runtime import EnterpriseAssistantRuntimeState
from app.models.enterprise_chat import EnterpriseAssistantRun
from app.services import enterprise_assistant_dispatcher as dispatcher


def test_dispatch_once_selects_only_queued_unstopped_runs(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine, tables=[
        EnterpriseAssistantRun.__table__, EnterpriseAssistantRuntimeState.__table__,
    ])
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory.begin() as db:
        for index, (run_status, state_status, stopped) in enumerate((
            ("queued", "queued", False),
            ("queued", "queued", True),
            ("retryable", "failed", False),
        ), start=1):
            run = EnterpriseAssistantRun(
                public_id=f"ear_dispatch_{index}", request_key=f"dispatch-{index}",
                enterprise_id=10, conversation_id=20, initiator_user_id=30,
                source_message_id=40 + index, status=run_status, attempt_count=0,
                prompt_sha256="a" * 64,
            )
            db.add(run)
            db.flush()
            db.add(EnterpriseAssistantRuntimeState(
                assistant_run_id=run.id, enterprise_id=10, conversation_id=20,
                initiator_user_id=30, source_message_id=40 + index, status=state_status,
                checkpoint_json="{}", permission_snapshot_json="{}",
                permission_snapshot_sha256="b" * 64, stop_requested=stopped,
                lease_generation=0, billing_invocation_count=0, version=1,
            ))

    calls = []

    async def fake_execute(_engine, *, enterprise_id, conversation_id, run_public_id):
        calls.append((enterprise_id, conversation_id, run_public_id))

    monkeypatch.setattr(dispatcher, "SessionLocal", factory)
    monkeypatch.setattr(dispatcher.settings, "enterprise_assistant_worker_enabled", True)
    monkeypatch.setattr(dispatcher.settings, "enterprise_assistant_worker_batch_size", 5)
    monkeypatch.setattr(
        "app.services.enterprise_assistant_runtime_service.execute_queued_run", fake_execute,
    )

    result = dispatcher.dispatch_once()

    assert result == {"queued": 1, "attempted": 1, "failed": 0}
    assert calls == [(10, 20, "ear_dispatch_1")]
    engine.dispose()
