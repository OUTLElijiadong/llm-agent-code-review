"""小菱安全事件导出与趋势聚合。"""

from __future__ import annotations

import csv
import io
import json
from datetime import date, datetime, timedelta, timezone
from typing import Any

from sqlalchemy import case, func
from sqlalchemy.orm import Session

from app.models.audit_log import AuditLog
from app.services.copilot_security_service import EVENT_KINDS, EVENT_PREFIX

_COLUMNS = (
    "id", "create_time", "kind", "status", "actor_id", "actor_name", "ip", "target", "detail",
)
_LABELS = {item["kind"]: item["label"] for item in EVENT_KINDS}


def _event_query(db: Session, *, kind: str = "", status: str = "", days: int = 7):
    query = db.query(AuditLog).filter(AuditLog.action.like(f"{EVENT_PREFIX}%"))
    if kind:
        query = query.filter(AuditLog.action == f"{EVENT_PREFIX}{kind}")
    if status:
        query = query.filter(AuditLog.status == status)
    if days:
        since = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=int(days))
        query = query.filter(AuditLog.create_time >= since)
    return query


def _kind(action: str | None) -> str:
    return str(action or "").removeprefix(EVENT_PREFIX)


def _event_dict(row: AuditLog) -> dict[str, Any]:
    return {
        "id": row.id,
        "create_time": row.create_time.isoformat() if row.create_time else None,
        "kind": _kind(row.action),
        "status": row.status,
        "actor_id": row.actor_id,
        "actor_name": row.actor_name,
        "ip": row.ip,
        "target": row.target_id,
        "detail": row.detail,
    }


def export_events(
    db: Session, *, kind: str = "", status: str = "", days: int = 7, fmt: str = "csv",
) -> tuple[str, str, str]:
    """导出时间窗内的小菱安全事件，返回内容、媒体类型与下载文件名。"""
    rows = _event_query(db, kind=kind, status=status, days=days).order_by(AuditLog.id.desc()).all()
    events = [_event_dict(row) for row in rows]
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d")
    if fmt == "json":
        return (
            json.dumps(events, ensure_ascii=False, separators=(",", ":")),
            "application/json",
            f"copilot-security-events-{stamp}.json",
        )
    if fmt != "csv":
        raise ValueError("fmt 只能是 csv 或 json")

    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=_COLUMNS, lineterminator="\r\n")
    writer.writeheader()
    writer.writerows(events)
    return (
        "\ufeff" + buffer.getvalue(),
        "text/csv; charset=utf-8",
        f"copilot-security-events-{stamp}.csv",
    )


def _window(days: int) -> tuple[date, list[date]]:
    end = datetime.now(timezone.utc).date()
    start = end - timedelta(days=days - 1)
    return start, [start + timedelta(days=offset) for offset in range(days)]


def _grouped_events(db: Session, days: int):
    start, dates = _window(days)
    end_exclusive = dates[-1] + timedelta(days=1)
    rows = (
        db.query(
            func.date(AuditLog.create_time).label("event_date"),
            AuditLog.action.label("action"),
            func.count(AuditLog.id).label("total"),
            func.sum(case((AuditLog.status == "failed", 1), else_=0)).label("blocked"),
            func.max(AuditLog.create_time).label("last_seen"),
        )
        .filter(
            AuditLog.action.like(f"{EVENT_PREFIX}%"),
            AuditLog.create_time >= datetime.combine(start, datetime.min.time()),
            AuditLog.create_time < datetime.combine(end_exclusive, datetime.min.time()),
        )
        .group_by(func.date(AuditLog.create_time), AuditLog.action)
        .all()
    )
    return dates, rows


def build_trend(db: Session, *, days: int = 7) -> dict:
    """按自然日汇总事件类型，输出连续日期轴和缺口补零的序列。"""
    if days < 1:
        raise ValueError("days 必须大于等于 1")
    dates, rows = _grouped_events(db, days)
    counts: dict[str, dict[str, int]] = {}
    totals: dict[str, int] = {}
    for row in rows:
        kind = _kind(row.action)
        day = row.event_date.isoformat() if hasattr(row.event_date, "isoformat") else str(row.event_date)
        counts.setdefault(kind, {})[day] = int(row.total or 0)
        totals[kind] = totals.get(kind, 0) + int(row.total or 0)
    kinds = list(_LABELS) + sorted(set(totals) - set(_LABELS))
    axis = [item.isoformat() for item in dates]
    series = [
        {"kind": kind, "label": _LABELS.get(kind, kind),
         "points": [counts.get(kind, {}).get(day, 0) for day in axis]}
        for kind in kinds
    ]
    return {
        "窗口": days,
        "日期轴": axis,
        "序列": series,
        "合计": {"total": sum(totals.values()), "by_kind": totals},
    }


def summarize_by_kind(db: Session, *, days: int = 7) -> list[dict]:
    """汇总时间窗内各事件类型总数、失败数及最近发生时间。"""
    if days < 1:
        raise ValueError("days 必须大于等于 1")
    _, rows = _grouped_events(db, days)
    summary: dict[str, dict[str, Any]] = {}
    for row in rows:
        kind = _kind(row.action)
        item = summary.setdefault(kind, {"kind": kind, "label": _LABELS.get(kind, kind),
                                         "total": 0, "blocked": 0, "last_seen": None})
        item["total"] += int(row.total or 0)
        item["blocked"] += int(row.blocked or 0)
        seen = row.last_seen.isoformat() if row.last_seen else None
        if seen and (item["last_seen"] is None or seen > item["last_seen"]):
            item["last_seen"] = seen
    return sorted(summary.values(), key=lambda item: (-item["total"], item["kind"]))
