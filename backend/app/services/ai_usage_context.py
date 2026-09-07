"""仅传播平台产生的精确归因；ContextVar 隔离并发，后台任务使用持久快照恢复。"""

from __future__ import annotations

from collections.abc import Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

ATTRIBUTION_FIELDS = (
    "root_agent_run_id",
    "agent_run_id",
    "tool_execution_id",
    "agent_team_id",
    "agent_team_task_id",
    "agent_execution_event_id",
)
_context: ContextVar[dict[str, Any] | None] = ContextVar("ai_usage_attribution", default=None)


def attribution_snapshot() -> dict[str, Any]:
    return dict(_context.get() or {})


def current_attribution(user_id: int | None) -> dict[str, int]:
    return _owned_fields(attribution_snapshot(), user_id)


def _owned_fields(snapshot: Mapping[str, Any], user_id: int | None) -> dict[str, int]:
    if not user_id or snapshot.get("owner_user_id") != user_id:
        return {}
    return {
        key: value
        for key in ATTRIBUTION_FIELDS
        if isinstance(value := snapshot.get(key), int) and not isinstance(value, bool) and value > 0
    }


def model_attribution(row: Any) -> dict[str, int]:
    return {
        key: value
        for key in ATTRIBUTION_FIELDS
        if isinstance(value := getattr(row, key, None), int) and not isinstance(value, bool) and value > 0
    }


@contextmanager
def usage_context(user_id: int, fields: Mapping[str, Any], *, db: Session | None = None):
    """只接收受信调用链/ORM 的字段，不接收模型参数或 HTTP 载荷。"""
    snapshot = {"owner_user_id": int(user_id), **dict(fields)}
    source_metadata = {
        key: value
        for key in ("_review_task_id", "_file_id", "_chunk_index")
        if isinstance(value := snapshot.get(key), int) and not isinstance(value, bool) and value >= 0
    }
    token = _context.set({"owner_user_id": int(user_id), **_owned_fields(snapshot, user_id), **source_metadata})
    logging_token = None
    if isinstance(db, Session):
        bind = db.get_bind()
        engine = getattr(bind, "engine", bind)
        logging_token = _logging_scope.set((int(user_id), sessionmaker(bind=engine, expire_on_commit=False)))
    try:
        yield
    finally:
        if logging_token is not None:
            _logging_scope.reset(logging_token)
        _context.reset(token)


def run_attribution(db: Session, user_id: int, run_id: str) -> dict[str, int]:
    from app.models.agent_response_run import AgentResponseRun

    row = (
        db.query(AgentResponseRun)
        .filter(
            AgentResponseRun.run_id == run_id,
            AgentResponseRun.user_id == user_id,
        )
        .first()
    )
    if row is None:
        return {}
    return {
        **model_attribution(row),
        "root_agent_run_id": int(row.root_agent_run_id or row.id),
        "agent_run_id": int(row.id),
    }


def log_attribution(
    db: Session, user_id: int | None, task_id: int | None = None, snapshot: Mapping[str, Any] | None = None
) -> dict[str, int]:
    # 原调用的快照即使为空也有意义：延迟写入不能串到随后执行的另一个根任务。
    if snapshot is not None:
        return _owned_fields(snapshot, user_id)
    active_snapshot = attribution_snapshot()
    if user_id and active_snapshot.get("owner_user_id") == user_id:
        return _owned_fields(active_snapshot, user_id)
    if task_id:
        from app.models.review_task import ReviewTask

        row = db.get(ReviewTask, task_id)
        if row is not None and row.user_id == user_id:
            return model_attribution(row)
    return current_attribution(user_id)


def usage_tokens(usage: Any, primary: str, alternate: str = "") -> int | None:
    """真实 0 保留；缺失、负数、布尔、字符串和浮点数均视为未知。"""
    if not isinstance(usage, Mapping):
        return None
    value = usage.get(primary)
    if value is None and alternate:
        value = usage.get(alternate)
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


# Context copies carry only an Engine-backed factory. Every attempt owns its audit Session.

_logging_scope: ContextVar[tuple[int, Any] | None] = ContextVar("ai_usage_logging_scope", default=None)


def recorded_usage_ids(value: Any) -> list[int]:
    return (
        [item for item in value if isinstance(item, int) and not isinstance(item, bool) and item > 0]
        if isinstance(value, list)
        else []
    )


class UsageAccountingError(RuntimeError):
    """A model request already happened, but its audit transaction could not be committed."""


def record_usage_attempt(
    *,
    model_name: str,
    agent_label: str,
    usage: Any = None,
    status: str,
    error: str = "",
    duration_ms: int | None = None,
    prompt: str = "",
    response: str = "",
    user_id: int | None = None,
    task_id: int | None = None,
    file_id: int | None = None,
    chunk_index: int | None = None,
    db: Session | None = None,
    source: tuple[int | None, Any] | None = None,
) -> int | None:
    """Record one actual HTTP attempt. No implicit production connection or fabricated owner."""
    from app.models.ai_call_log import AiCallLog

    scope = _logging_scope.get()
    scoped_user = scope[0] if scope else source[0] if source else attribution_snapshot().get("owner_user_id")
    owner = user_id or scoped_user
    if owner is None and not isinstance(db, Session) and source is None and scope is None:
        return None
    if source is not None and source[0] == owner:
        factory = source[1]
    elif isinstance(db, Session):
        bind = db.get_bind()
        factory = sessionmaker(bind=getattr(bind, "engine", bind), expire_on_commit=False)
    elif scope is not None and scoped_user == owner:
        factory = scope[1]
    else:
        return None
    session = factory()
    snapshot = attribution_snapshot()
    if snapshot.get("owner_user_id") == owner:
        task_id = task_id if task_id is not None else snapshot.get("_review_task_id")
        file_id = file_id if file_id is not None else snapshot.get("_file_id")
        chunk_index = chunk_index if chunk_index is not None else snapshot.get("_chunk_index")
    try:
        row = AiCallLog(
            **log_attribution(session, owner, task_id),
            user_id=owner,
            task_id=task_id,
            file_id=file_id,
            chunk_index=chunk_index,
            agent_label=agent_label,
            model_name=model_name,
            status=status,
            error_message=error[:500] or None,
            prompt_tokens=usage_tokens(usage, "prompt_tokens"),
            completion_tokens=usage_tokens(usage, "completion_tokens"),
            total_tokens=usage_tokens(usage, "total_tokens"),
            duration_ms=duration_ms,
            prompt=prompt[:200_000] or None,
            response=response[:200_000] or None,
        )
        session.add(row)
        session.commit()
        return int(row.id)
    except SQLAlchemyError as exc:
        raise UsageAccountingError("模型请求已执行，但用量审计事务提交失败；不能重发模型请求") from exc
    finally:
        session.close()


def enrich_recorded_usage(
    db: Session,
    user_id: int | None,
    log_ids: Any,
    *,
    task_id: int | None = None,
    file_id: int | None = None,
    chunk_index: int | None = None,
    status: str = "",
    error: str = "",
) -> bool:
    """Complete trusted deferred metadata on exact committed IDs; never create a duplicate call."""
    from app.models.ai_call_log import AiCallLog
    from app.models.code_file import CodeFile
    from app.models.review_task import ReviewTask

    ids = recorded_usage_ids(log_ids)
    if not ids:
        return False
    owner = user_id or attribution_snapshot().get("owner_user_id")
    if not isinstance(db, Session):
        raise ValueError("已记账模型调用缺少可信所属用户或数据库")
    bind = db.get_bind()
    with sessionmaker(bind=getattr(bind, "engine", bind), expire_on_commit=False)() as session:
        rows = session.query(AiCallLog).filter(AiCallLog.id.in_(ids), AiCallLog.user_id == owner).all()
        if len(rows) != len(set(ids)):
            raise ValueError("模型调用记账标识不存在或不属于当前用户")
        task = session.get(ReviewTask, task_id) if task_id else None
        if task_id and (task is None or task.user_id != owner):
            raise ValueError("延迟记账的审查任务不存在或不属于当前用户")
        code_file = session.get(CodeFile, file_id) if file_id else None
        if file_id and (code_file is None or (task is not None and code_file.project_id != task.project_id)):
            raise ValueError("延迟记账的文件与审查任务不匹配")
        for row in rows:
            for field, value in (("task_id", task_id), ("file_id", file_id), ("chunk_index", chunk_index)):
                if value is not None:
                    if getattr(row, field) not in (None, value):
                        raise ValueError("延迟记账元数据与原调用冲突")
                    setattr(row, field, value)
            if row.id == ids[-1] and status == "failed":
                row.status = "failed"
                row.error_message = error[:500] or row.error_message
        session.commit()
    return True
