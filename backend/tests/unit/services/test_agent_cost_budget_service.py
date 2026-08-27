"""Automatic Agent token-budget guard regression tests."""

from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.models.agent_governance import AgentProfile
from app.models.ai_call_log import AiCallLog


def test_budget_snapshot_uses_current_utc_day_and_agent_code(db: Any) -> None:
    from app.services import agent_cost_budget_service

    now = datetime(2026, 8, 27, 6, 0, tzinfo=timezone.utc)
    db.add_all([
        AgentProfile(
            code="code_reviewer",
            name="代码审查",
            category="quality",
            status="idle",
            is_enabled=1,
            budget_tokens_daily=120,
        ),
        AiCallLog(
            model_name="deepseek-v4-flash",
            agent_label="code_reviewer",
            status="success",
            total_tokens=70,
            create_time=now - timedelta(hours=1),
        ),
        AiCallLog(
            model_name="deepseek-v4-flash",
            agent_label="code_reviewer",
            status="success",
            prompt_tokens=20,
            completion_tokens=10,
            create_time=now - timedelta(hours=2),
        ),
        AiCallLog(
            model_name="deepseek-v4-flash",
            agent_label="code_reviewer",
            status="success",
            prompt_tokens=0,
            completion_tokens=0,
            total_tokens=0,
            create_time=now - timedelta(hours=3),
        ),
        AiCallLog(
            model_name="deepseek-v4-flash",
            agent_label="other_agent",
            status="success",
            total_tokens=999,
            create_time=now - timedelta(minutes=1),
        ),
        AiCallLog(
            model_name="deepseek-v4-flash",
            agent_label="code_reviewer",
            status="success",
            total_tokens=999,
            create_time=now - timedelta(days=1),
        ),
    ])
    db.commit()

    snapshot = agent_cost_budget_service.daily_budget_snapshot(
        db,
        "code_reviewer",
        now=now,
    )

    assert snapshot.budget_tokens == 120
    assert snapshot.used_tokens == 100
    assert snapshot.remaining_tokens == 20
    assert snapshot.exceeded is False


def test_automatic_budget_guard_serializes_same_agent_and_rechecks_usage(
    tmp_path: Any,
) -> None:
    from app.core.database import Base
    from app.services import agent_cost_budget_service

    engine = create_engine(
        f"sqlite:///{tmp_path / 'budget.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    setup = Session()
    setup.add(AgentProfile(
        code="code_reviewer",
        name="代码审查",
        category="quality",
        status="idle",
        is_enabled=1,
        budget_tokens_daily=10,
    ))
    setup.commit()
    setup.close()

    first_entered = threading.Event()
    second_started = threading.Event()
    release_first = threading.Event()
    outcomes: list[str] = []
    failures: list[BaseException] = []

    def first_call() -> None:
        session = Session()
        try:
            with agent_cost_budget_service.guard_automatic_model_call(
                session,
                "code_reviewer",
            ):
                outcomes.append("first_admitted")
                first_entered.set()
                assert release_first.wait(timeout=3)
                session.add(AiCallLog(
                    model_name="deepseek-v4-flash",
                    agent_label="code_reviewer",
                    status="success",
                    total_tokens=10,
                    create_time=datetime.now(timezone.utc),
                ))
                session.commit()
        except BaseException as exc:  # pragma: no cover - surfaced by final assertion
            failures.append(exc)
        finally:
            session.close()

    def second_call() -> None:
        session = Session()
        second_started.set()
        try:
            with agent_cost_budget_service.guard_automatic_model_call(
                session,
                "code_reviewer",
            ):
                outcomes.append("second_admitted")
        except agent_cost_budget_service.AutomaticTokenBudgetExceeded:
            outcomes.append("second_blocked")
        except BaseException as exc:  # pragma: no cover - surfaced by final assertion
            failures.append(exc)
        finally:
            session.close()

    first = threading.Thread(target=first_call)
    second = threading.Thread(target=second_call)
    first.start()
    assert first_entered.wait(timeout=3)
    second.start()
    assert second_started.wait(timeout=3)
    release_first.set()
    first.join(timeout=5)
    second.join(timeout=5)
    engine.dispose()

    assert failures == []
    assert first.is_alive() is False
    assert second.is_alive() is False
    assert outcomes == ["first_admitted", "second_blocked"]


def test_database_lock_failure_is_fail_closed(db: Any, monkeypatch: Any) -> None:
    from app.services import agent_cost_budget_service

    db.add(AgentProfile(
        code="code_reviewer",
        name="代审",
        category="quality",
        status="idle",
        is_enabled=1,
        budget_tokens_daily=10,
    ))
    db.commit()

    class UnavailableLock:
        def __enter__(self) -> None:
            raise agent_cost_budget_service.AutomaticBudgetLockUnavailable(
                "lock unavailable"
            )

        def __exit__(self, *_args: Any) -> None:
            return None

    monkeypatch.setattr(
        agent_cost_budget_service,
        "_database_agent_lock",
        lambda *_args, **_kwargs: UnavailableLock(),
    )

    with pytest.raises(
        agent_cost_budget_service.AutomaticBudgetLockUnavailable,
        match="lock unavailable",
    ):
        with agent_cost_budget_service.guard_automatic_model_call(
            db,
            "code_reviewer",
        ):
            pytest.fail("lock failure must not admit an automatic model call")


def test_zero_budget_remains_unlimited(db: Any) -> None:
    from app.services import agent_cost_budget_service

    db.add_all([
        AgentProfile(
            code="code_reviewer",
            name="代审",
            category="quality",
            status="idle",
            is_enabled=1,
            budget_tokens_daily=0,
        ),
        AiCallLog(
            model_name="deepseek-v4-flash",
            agent_label="code_reviewer",
            status="success",
            total_tokens=1_000_000,
            create_time=datetime.now(timezone.utc),
        ),
    ])
    db.commit()

    with agent_cost_budget_service.guard_automatic_model_call(
        db,
        "code_reviewer",
    ) as snapshot:
        assert snapshot.budget_tokens == 0
        assert snapshot.remaining_tokens is None
        assert snapshot.exceeded is False
