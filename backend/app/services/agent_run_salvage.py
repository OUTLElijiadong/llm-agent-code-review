"""小菱运行异常自愈：非人为中断自动从检查点恢复，人为停止立即停止。

- 进程内登记正在执行的 run_id，周期清扫只处理不在登记表中的僵尸运行；
- 执行抛出未处理异常时立即把运行转为带恢复标记的失败态并在新线程续跑，
  同一运行最多自动恢复 ``MAX_AUTO_RECOVERIES`` 次，避免确定性错误无限重试；
- 检查点里有 ``stop_requested`` 时一律收敛为已取消，不恢复。
"""

from __future__ import annotations

import json
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Iterator

from loguru import logger

from app.core.database import SessionLocal
from app.models.agent_response_run import AgentResponseRun

MAX_AUTO_RECOVERIES = 2
_LIVE_RUN_IDS: dict[str, int] = {}
_LOCK = threading.Lock()


@contextmanager
def live_run(run_id: str) -> Iterator[None]:
    """标记 run_id 正在本进程执行；可重入（恢复线程与流式执行共用）。"""
    with _LOCK:
        _LIVE_RUN_IDS[run_id] = _LIVE_RUN_IDS.get(run_id, 0) + 1
    try:
        yield
    finally:
        with _LOCK:
            remaining = _LIVE_RUN_IDS.get(run_id, 1) - 1
            if remaining <= 0:
                _LIVE_RUN_IDS.pop(run_id, None)
            else:
                _LIVE_RUN_IDS[run_id] = remaining


def is_live(run_id: str) -> bool:
    with _LOCK:
        return run_id in _LIVE_RUN_IDS


def _checkpoint(row: AgentResponseRun) -> dict:
    try:
        value = json.loads(row.checkpoint_json or "{}")
    except (TypeError, json.JSONDecodeError):
        value = {}
    return value if isinstance(value, dict) else {}


def _recovery_metadata(checkpoint: dict) -> dict:
    # 计数放在 context_metadata：运行时按检查点数据类回写，顶层未知键会丢失。
    metadata = checkpoint.get("context_metadata")
    return metadata if isinstance(metadata, dict) else {}


def _recovery_budget_exhausted(db, row: AgentResponseRun, checkpoint: dict, *, error: str, source: str) -> bool:
    """达到自动恢复上限时安全停止（保留进度与恢复记录），返回 True。"""
    metadata = _recovery_metadata(checkpoint)
    attempts = int(metadata.get("auto_recovery_attempts") or 0)
    if attempts < MAX_AUTO_RECOVERIES:
        return False
    checkpoint.update({
        "status": "failed",
        "pending": None,
        "recovery_requested": False,
        "error": (
            f"小菱连续 {attempts} 次自动恢复后仍出错，已安全停止并保留进度；"
            "可点击重试，或换个说法让小菱改用其他方案。"
        ),
    })
    checkpoint["context_metadata"] = _with_recovery_event(
        metadata, source=source, error=error, outcome="budget_exhausted",
    )
    row.status = "failed"
    row.checkpoint_json = json.dumps(checkpoint, ensure_ascii=False, default=str)
    row.version = int(row.version or 0) + 1
    db.commit()
    logger.warning("[salvage] 运行 {} 已达自动恢复上限({}): {}", row.run_id, source, error[:200])
    return True


def _with_recovery_event(metadata: dict, *, source: str, error: str, outcome: str) -> dict:
    events = metadata.get("recovery_events") if isinstance(metadata.get("recovery_events"), list) else []
    event = {
        "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source": source,
        "outcome": outcome,
        "error": str(error or "")[:300],
    }
    return {**metadata, "recovery_events": [*events, event][-10:]}


def _record_recovery_attempt(row: AgentResponseRun, checkpoint: dict, *, error: str, source: str) -> None:
    """自动恢复计数与恢复事件持久写入检查点；崩溃与僵尸清扫共用同一预算。"""
    metadata = _recovery_metadata(checkpoint)
    attempts = int(metadata.get("auto_recovery_attempts") or 0) + 1
    checkpoint["context_metadata"] = {
        **_with_recovery_event(metadata, source=source, error=error, outcome="recovering"),
        "auto_recovery_attempts": attempts,
    }
    row.checkpoint_json = json.dumps(checkpoint, ensure_ascii=False, default=str)


def _active_statuses() -> set[str]:
    from app.api.v1.agent_responses import _ACTIVE_RUN_STATUSES

    return set(_ACTIVE_RUN_STATUSES)


def _dispatch_recovery(row: AgentResponseRun, *, delay_seconds: float) -> None:
    from app.services.background_task_recovery import AgentRunRecovery, start_agent_run_recovery

    recovery = AgentRunRecovery(
        run_id=str(row.run_id),
        user_id=int(row.user_id),
        surface=str(row.surface),
        session_key=str(row.session_key),
    )

    def _later() -> None:
        # 等原调用栈回滚并释放数据库会话后再续跑。
        time.sleep(delay_seconds)
        if is_live(recovery.run_id):
            return
        start_agent_run_recovery([recovery])

    threading.Thread(target=_later, name=f"agent-run-salvage-{recovery.run_id[:24]}", daemon=True).start()


def salvage_crashed_run(run_id: str, *, error: str = "", delay_seconds: float = 2.0) -> str:
    """执行异常后收敛运行状态；返回 recovering / cancelled / failed / untouched。"""
    from app.api.v1.agent_responses import _recover_stale_active_run

    db = SessionLocal()
    try:
        row = db.query(AgentResponseRun).filter(AgentResponseRun.run_id == run_id).first()
        if row is None or row.status not in _active_statuses():
            return "untouched"
        checkpoint = _checkpoint(row)
        if checkpoint.get("stop_requested") is True:
            _recover_stale_active_run(db, row, force=True)
            return "cancelled"
        if _recovery_budget_exhausted(db, row, checkpoint, error=error, source="crash"):
            return "failed"
        _record_recovery_attempt(row, checkpoint, error=error, source="crash")
        db.flush()
        _recover_stale_active_run(db, row, force=True)
        db.refresh(row)
        if row.status == "failed" and _checkpoint(row).get("recovery_requested") is True:
            _dispatch_recovery(row, delay_seconds=delay_seconds)
            logger.warning("[salvage] 运行 {} 异常后自动恢复: {}", run_id, error[:200])
            return "recovering"
        return "untouched"
    except Exception:  # noqa: BLE001 - 自愈失败不能掩盖原异常
        db.rollback()
        logger.exception("[salvage] 运行 {} 状态收敛失败", run_id)
        return "untouched"
    finally:
        db.close()


def sweep_orphan_runs_once() -> int:
    """周期清扫：超过租约、且不在本进程执行的活跃运行转入自动恢复。"""
    from app.api.v1.agent_responses import _recover_stale_active_run

    db = SessionLocal()
    recovered = 0
    try:
        rows = (
            db.query(AgentResponseRun)
            .filter(AgentResponseRun.status.in_(sorted(_active_statuses())))
            .order_by(AgentResponseRun.id.asc())
            .limit(200)
            .all()
        )
        for row in rows:
            if is_live(str(row.run_id)):
                continue
            before = row.status
            _recover_stale_active_run(db, row)
            db.refresh(row)
            if row.status == before:
                continue
            recovered += 1
            checkpoint = _checkpoint(row)
            if row.status == "failed" and checkpoint.get("recovery_requested") is True:
                # 僵尸清扫与崩溃自愈共用恢复预算，确定性错误不会被无限重派。
                if _recovery_budget_exhausted(db, row, checkpoint, error="运行租约到期", source="orphan_sweep"):
                    continue
                _record_recovery_attempt(row, checkpoint, error="运行租约到期", source="orphan_sweep")
                db.commit()
                _dispatch_recovery(row, delay_seconds=0.5)
        return recovered
    except Exception:  # noqa: BLE001 - 周期任务失败等下一轮
        db.rollback()
        logger.exception("[salvage] 僵尸运行清扫失败")
        return recovered
    finally:
        db.close()
