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

from app.models.agent_governance import AgentAlert, AgentProfile
from app.models.ai_call_log import AiCallLog


@dataclass(frozen=True)
class DailyTokenBudgetSnapshot:
    """One Agent's persisted token usage for the current UTC day."""

    agent_code: str
    budget_tokens: int
    used_tokens: int
    unknown_usage_calls: int = 0

    @property
    def remaining_tokens(self) -> Optional[int]:
        if self.budget_tokens <= 0:
            return None
        return max(0, self.budget_tokens - self.used_tokens)

    @property
    def exceeded(self) -> bool:
        return self.budget_tokens > 0 and self.used_tokens >= self.budget_tokens

    @property
    def blocked(self) -> bool:
        return self.budget_tokens > 0 and (self.exceeded or self.unknown_usage_calls > 0)


class AutomaticTokenBudgetExceeded(RuntimeError):
    """Raised before unattended work when the Agent has spent its daily budget."""

    def __init__(self, snapshot: DailyTokenBudgetSnapshot) -> None:
        self.snapshot = snapshot
        if snapshot.unknown_usage_calls:
            message = (
                f"Agent {snapshot.agent_code} 今日有 {snapshot.unknown_usage_calls} 条调用缺少完整 token 用量，"
                f"当前可核算 {snapshot.used_tokens}/{snapshot.budget_tokens}；已暂停后台自动任务，需先核对用量"
            )
        else:
            message = (
                f"Agent {snapshot.agent_code} 当日自动任务 token 预算已用尽"
                f"({snapshot.used_tokens}/{snapshot.budget_tokens})"
            )
        super().__init__(message)


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


def _unknown_usage_calls(db: Session, agent_code: str, *, now: Optional[datetime]) -> int:
    """Count attempts whose usage cannot be totaled from total or both components."""
    start, end = _utc_day_bounds(now)
    incomplete = AiCallLog.total_tokens.is_(None) & (
        AiCallLog.prompt_tokens.is_(None) | AiCallLog.completion_tokens.is_(None)
    )
    value = (
        db.query(func.count(AiCallLog.id))
        .filter(
            AiCallLog.agent_label == agent_code,
            AiCallLog.create_time >= start,
            AiCallLog.create_time < end,
            incomplete,
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
        unknown_usage_calls=_unknown_usage_calls(db, agent_code, now=now),
    )


def daily_budget_snapshot(
    db: Session,
    agent_code: str,
    *,
    now: Optional[datetime] = None,
) -> DailyTokenBudgetSnapshot:
    """Read the configured budget and already-persisted usage without reserving it."""

    return _snapshot(db, agent_code, now=now)


def ensure_daily_token_threshold_alert(
    db: Session,
    agent_code: str,
    threshold_tokens: int,
    *,
    now: Optional[datetime] = None,
) -> Optional[AgentAlert]:
    """Create one warning per UTC day after an Agent crosses its configured usage threshold."""
    threshold = max(0, int(threshold_tokens or 0))
    if threshold == 0:
        return None
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    else:
        current = current.astimezone(timezone.utc)
    snapshot = _snapshot(db, agent_code, now=current)
    if snapshot.used_tokens < threshold:
        return None

    day = current.date().isoformat()
    fingerprint = f"daily-token:{agent_code}:{day}"
    existing = db.query(AgentAlert).filter(AgentAlert.fingerprint == fingerprint).first()
    if existing:
        return existing

    from app.services.observability_service import create_alert

    return create_alert(
        db,
        alert_type="ai.daily_token_threshold",
        severity="warning",
        title=f"{agent_code} 今日模型 Token 用量达到提醒阈值",
        detail={
            "agent_code": agent_code,
            "used_tokens": snapshot.used_tokens,
            "threshold_tokens": threshold,
            "date_utc": day,
            "action": "提醒管理员检查用量；不阻断用户会话",
        },
        category="cost",
        source="cost_controller",
        fingerprint=fingerprint,
    )


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
        if snapshot.blocked:
            raise AutomaticTokenBudgetExceeded(snapshot)
        yield snapshot
