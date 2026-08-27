"""Concurrency-safe token budget guard for unattended Agent work."""

from __future__ import annotations

import threading
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Iterator, Optional, Tuple

from sqlalchemy import case, func
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.models.agent_governance import AgentProfile
from app.models.ai_call_log import AiCallLog


@dataclass(frozen=True)
class DailyTokenBudgetSnapshot:
    """One Agent's persisted token usage for the current UTC day."""

    agent_code: str
    budget_tokens: int
    used_tokens: int

    @property
    def remaining_tokens(self) -> Optional[int]:
        if self.budget_tokens <= 0:
            return None
        return max(0, self.budget_tokens - self.used_tokens)

    @property
    def exceeded(self) -> bool:
        return self.budget_tokens > 0 and self.used_tokens >= self.budget_tokens


class AutomaticTokenBudgetExceeded(RuntimeError):
    """Raised before unattended work when the Agent has spent its daily budget."""

    def __init__(self, snapshot: DailyTokenBudgetSnapshot) -> None:
        self.snapshot = snapshot
        super().__init__(
            f"Agent {snapshot.agent_code} 当日自动任务 token 预算已用尽"
            f"({snapshot.used_tokens}/{snapshot.budget_tokens})"
        )


class AutomaticBudgetLockUnavailable(RuntimeError):
    """Raised when the guard cannot establish a trustworthy serialization lock."""


_process_locks_guard = threading.Lock()
_process_locks: dict[str, threading.RLock] = {}


def _utc_day_bounds(now: Optional[datetime] = None) -> tuple[datetime, datetime]:
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    else:
        current = current.astimezone(timezone.utc)
    start = current.replace(hour=0, minute=0, second=0, microsecond=0)
    return start, start + timedelta(days=1)


def _usage_tokens(db: Session, agent_code: str, *, now: Optional[datetime]) -> int:
    start, end = _utc_day_bounds(now)
    component_total = (
        func.coalesce(AiCallLog.prompt_tokens, 0)
        + func.coalesce(AiCallLog.completion_tokens, 0)
    )
    logged_total = func.coalesce(AiCallLog.total_tokens, 0)
    effective_total = case(
        (logged_total >= component_total, logged_total),
        else_=component_total,
    )
    value = (
        db.query(func.coalesce(func.sum(effective_total), 0))
        .filter(
            AiCallLog.agent_label == agent_code,
            AiCallLog.create_time >= start,
            AiCallLog.create_time < end,
        )
        .scalar()
    )
    return int(value or 0)


def _snapshot(
    db: Session,
    agent_code: str,
    *,
    profile: Optional[AgentProfile] = None,
    now: Optional[datetime] = None,
) -> DailyTokenBudgetSnapshot:
    if profile is None:
        profile = (
            db.query(AgentProfile)
            .filter(AgentProfile.code == agent_code)
            .first()
        )
    return DailyTokenBudgetSnapshot(
        agent_code=agent_code,
        budget_tokens=max(0, int(profile.budget_tokens_daily or 0)) if profile else 0,
        used_tokens=_usage_tokens(db, agent_code, now=now),
    )


def daily_budget_snapshot(
    db: Session,
    agent_code: str,
    *,
    now: Optional[datetime] = None,
) -> DailyTokenBudgetSnapshot:
    """Read the configured budget and already-persisted usage without reserving it."""

    return _snapshot(db, agent_code, now=now)


def _process_lock(agent_code: str) -> threading.RLock:
    with _process_locks_guard:
        return _process_locks.setdefault(agent_code, threading.RLock())


@contextmanager
def _database_agent_lock(
    db: Session,
    agent_code: str,
) -> Iterator[Tuple[Session, Optional[AgentProfile]]]:
    """Lock one profile row for cross-worker serialization.

    Production MySQL honors ``FOR UPDATE`` for the transaction lifetime. SQLite
    ignores it, so local/test runtimes additionally use an in-process lock.
    """

    bind = db.get_bind()
    if bind is None:
        raise AutomaticBudgetLockUnavailable("Agent 预算数据库连接不可用")
    process_lock = _process_lock(agent_code)
    with process_lock:
        locked_db = Session(bind=bind, autoflush=False, expire_on_commit=False)
        transaction = None
        try:
            transaction = locked_db.begin()
            query = locked_db.query(AgentProfile).filter(AgentProfile.code == agent_code)
            if bind.dialect.name != "sqlite":
                query = query.with_for_update()
            profile = query.first()
        except SQLAlchemyError as exc:
            if transaction is not None:
                transaction.rollback()
            locked_db.close()
            raise AutomaticBudgetLockUnavailable(
                f"Agent {agent_code} 预算锁获取失败"
            ) from exc

        try:
            yield locked_db, profile
        except BaseException:
            transaction.rollback()
            raise
        else:
            try:
                transaction.commit()
            except SQLAlchemyError as exc:
                raise AutomaticBudgetLockUnavailable(
                    f"Agent {agent_code} 预算锁释放失败"
                ) from exc
        finally:
            locked_db.close()


@contextmanager
def guard_automatic_model_call(
    db: Session,
    agent_code: str,
    *,
    now: Optional[datetime] = None,
) -> Iterator[DailyTokenBudgetSnapshot]:
    """Serialize and admit one unattended call based on persisted daily usage.

    The caller must keep this context open until the model call and its
    ``AiCallLog`` write are complete. Interactive user calls deliberately do not
    use this guard.
    """

    with _database_agent_lock(db, agent_code) as (locked_db, profile):
        snapshot = _snapshot(
            locked_db,
            agent_code,
            profile=profile,
            now=now,
        )
        if snapshot.exceeded:
            raise AutomaticTokenBudgetExceeded(snapshot)
        yield snapshot
