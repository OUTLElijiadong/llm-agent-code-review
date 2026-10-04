"""最高管理员安全中心的只读时间线与受限监控策略。"""

from __future__ import annotations

import json
import math
import re
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.exceptions import ValidationError
from app.models.admin_chat import OpsExecution
from app.models.agent_governance import AgentAlert, AgentJob, AgentJobRun
from app.models.audit_log import AuditLog
from app.models.system_config import SystemConfig
from app.models.user import User
from app.services import audit_service, observability_service

POLICY_KEY = "security_monitor_policy"
_SEVERITY_ORDER = {"info": 0, "warning": 1, "high": 2, "critical": 3}
_SECURITY_ACTIONS = (
    "ssh_login_events",
    "nginx_attack_events",
    "flytrap_attack_events",
    "backup_audit",
    "db_health",
    "db_threat_signals",
    "status",
    "ip_attribution",
)
_ACTION_LABELS = {
    "ssh_login_events": "读取 SSH 登录日志",
    "nginx_attack_events": "读取 Nginx 请求日志",
    "flytrap_attack_events": "读取蜜罐攻击事件",
    "backup_audit": "核验备份状态",
    "db_health": "读取数据库健康状态",
    "db_threat_signals": "读取数据库威胁信号",
    "status": "读取运行环境状态",
    "ip_attribution": "查询来源归属信息",
}
_AUDIT_ACTION_LABELS = {
    "login": "账号登录记录",
    "logout": "账号退出记录",
    "user": "用户与账号管理",
    "rbac.user_roles_assign": "用户角色变更",
    "rbac.role_create": "角色创建",
    "rbac.role_update": "角色配置变更",
    "rbac.role_permissions_assign": "角色权限变更",
    "rbac.role_data_scope_update": "角色数据范围变更",
    "llm_config_update": "全局模型配置变更",
    "llm_model_registry_update": "模型注册表变更",
    "llm_model_assignments_update": "模型分配变更",
}
_SAFE_DETAIL_KEYS = {
    "ip",
    "kind",
    "failed_count",
    "failure_count",
    "scanner_count",
    "threshold",
    "window_hours",
    "source_truncated",
    "status_counts",
    "accepted_count",
    "total",
    "used_percent",
    "recovery_detected",
    "container_restart_count",
    "sql_gz_count",
    "source_line_limit",
    "attack_count",
    "age_hours",
}
_MAX_ROWS_PER_SOURCE = 500


def _describe_schedule(schedule: str | None) -> tuple[int | None, str]:
    """从持久化任务计划生成真实展示文案与过期判断周期。"""
    value = str(schedule or "").strip()
    interval = re.fullmatch(r"interval@(\d+)([ms])", value, re.IGNORECASE)
    if interval:
        amount = int(interval.group(1))
        unit = interval.group(2).lower()
        if amount < 1 or (unit == "m" and amount > 1440) or (unit == "s" and amount > 86400):
            return None, f"计划：{value}"
        minutes = amount if unit == "m" else max(1, math.ceil(amount / 60))
        return minutes, f"每 {amount} {'分钟' if unit == 'm' else '秒'}"
    daily = re.fullmatch(r"daily@(\d{2}:\d{2})", value)
    if daily:
        return None, f"每日 {daily.group(1)}"
    hourly = re.fullmatch(r"hourly@(?:\*:)?(\d{1,2})", value)
    if hourly and int(hourly.group(1)) < 60:
        return None, f"每小时第 {int(hourly.group(1))} 分钟"
    if value == "manual":
        return None, "手动触发"
    return None, f"计划：{value}" if value else "未配置巡检计划"


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def _default_policy() -> dict[str, Any]:
    popup_severity = str(settings.security_popup_min_severity)
    if popup_severity not in _SEVERITY_ORDER:
        popup_severity = "warning"
    return {
        "ssh_failed_threshold": int(settings.security_failed_login_threshold),
        "ssh_window_hours": int(settings.security_failed_login_window_hours),
        "nginx_failure_threshold": int(settings.security_nginx_failure_threshold),
        "nginx_window_hours": int(settings.security_nginx_window_hours),
        "popup_min_severity": popup_severity,
    }


def _is_at_least_as_sensitive(candidate: dict[str, Any], baseline: dict[str, Any]) -> bool:
    """比较策略强度，拒绝关闭监控或降低告警灵敏度。"""
    return (
        candidate["ssh_failed_threshold"] <= baseline["ssh_failed_threshold"]
        and candidate["ssh_window_hours"] >= baseline["ssh_window_hours"]
        and candidate["nginx_failure_threshold"] <= baseline["nginx_failure_threshold"]
        and candidate["nginx_window_hours"] >= baseline["nginx_window_hours"]
        and _SEVERITY_ORDER[candidate["popup_min_severity"]] <= _SEVERITY_ORDER[baseline["popup_min_severity"]]
    )


def _stored_policy(db: Session) -> tuple[dict[str, Any], dict[str, Any]]:
    baseline = _default_policy()
    row = db.query(SystemConfig).filter(SystemConfig.config_key == POLICY_KEY).first()
    if not row or not row.config_value:
        return baseline, {"source": "environment", "revision": 0, "updated_at": None, "updated_by": None}
    try:
        payload = json.loads(row.config_value)
        values = payload.get("values") if isinstance(payload, dict) else None
        if not isinstance(values, dict):
            raise ValueError("policy values are missing")
        candidate = {
            "ssh_failed_threshold": int(values["ssh_failed_threshold"]),
            "ssh_window_hours": int(values["ssh_window_hours"]),
            "nginx_failure_threshold": int(values["nginx_failure_threshold"]),
            "nginx_window_hours": int(values["nginx_window_hours"]),
            "popup_min_severity": str(values["popup_min_severity"]),
        }
        if not 1 <= candidate["ssh_failed_threshold"] <= baseline["ssh_failed_threshold"]:
            raise ValueError("invalid SSH threshold")
        if not baseline["ssh_window_hours"] <= candidate["ssh_window_hours"] <= 24:
            raise ValueError("invalid SSH window")
        if not 1 <= candidate["nginx_failure_threshold"] <= baseline["nginx_failure_threshold"]:
            raise ValueError("invalid Nginx threshold")
        if not baseline["nginx_window_hours"] <= candidate["nginx_window_hours"] <= 24:
            raise ValueError("invalid Nginx window")
        if candidate["popup_min_severity"] not in _SEVERITY_ORDER or not _is_at_least_as_sensitive(candidate, baseline):
            raise ValueError("stored policy weakens monitoring")
        meta = {
            "source": "database",
            "revision": max(1, int(payload.get("revision") or 1)),
            "updated_at": payload.get("updated_at"),
            "updated_by": payload.get("updated_by"),
        }
        return candidate, meta
    except (ValueError, TypeError, KeyError, json.JSONDecodeError):
        # 配置异常时回到环境基线；绝不从损坏记录加载更宽松策略。
        return baseline, {"source": "environment_fallback", "revision": 0, "updated_at": None, "updated_by": None}


def get_policy(db: Session) -> dict[str, Any]:
    values, meta = _stored_policy(db)
    return {
        **values,
        **meta,
        "monitoring_mode": "monitor_only",
        "automatic_blocking_enabled": False,
        "counterattack_enabled": False,
        "baseline": _default_policy(),
    }


def update_policy(db: Session, actor: User, values: dict[str, Any]) -> dict[str, Any]:
    current, meta = _stored_policy(db)
    candidate = {
        "ssh_failed_threshold": int(values["ssh_failed_threshold"]),
        "ssh_window_hours": int(values["ssh_window_hours"]),
        "nginx_failure_threshold": int(values["nginx_failure_threshold"]),
        "nginx_window_hours": int(values["nginx_window_hours"]),
        "popup_min_severity": current["popup_min_severity"],
    }
    if candidate["popup_min_severity"] not in _SEVERITY_ORDER or not _is_at_least_as_sensitive(candidate, current):
        raise ValidationError("安全监控设置只能提高或保持检测灵敏度", code=42200)
    if candidate["ssh_failed_threshold"] < 1 or candidate["nginx_failure_threshold"] < 1:
        raise ValidationError("阈值必须大于等于 1", code=42200)
    if candidate["ssh_window_hours"] > 24 or candidate["nginx_window_hours"] > 24:
        raise ValidationError("观察窗口不能超过 24 小时", code=42200)

    revision = int(meta["revision"]) + 1
    updated_at = _iso(_now_utc())
    payload = {
        "values": candidate,
        "revision": revision,
        "updated_at": updated_at,
        "updated_by": actor.username,
    }
    row = db.query(SystemConfig).filter(SystemConfig.config_key == POLICY_KEY).first()
    if row is None:
        row = SystemConfig(config_key=POLICY_KEY, config_value=json.dumps(payload, ensure_ascii=False))
        db.add(row)
    else:
        row.config_value = json.dumps(payload, ensure_ascii=False)
    audit_service.log(
        db,
        actor,
        "security_policy_update",
        target_type="security_monitor_policy",
        target_id=revision,
        detail=json.dumps({"before": current, "after": candidate, "revision": revision}, ensure_ascii=False),
        commit=False,
    )
    try:
        db.commit()
    except Exception:
        db.rollback()
        raise
    db.refresh(row)
    return get_policy(db)


def _safe_alert_detail(raw: Any) -> dict[str, Any]:
    try:
        value = json.loads(raw) if isinstance(raw, str) else raw
    except (json.JSONDecodeError, TypeError):
        return {"evidence_note": "详情格式不可解析；原始内容已隐藏"}
    if not isinstance(value, dict):
        return {"evidence_note": "详情不是结构化对象；原始内容已隐藏"}
    safe: dict[str, Any] = {}
    for key in _SAFE_DETAIL_KEYS:
        if key not in value:
            continue
        item = value[key]
        if key == "status_counts" and isinstance(item, dict):
            safe[key] = {
                str(k): int(v) for k, v in item.items() if str(k) in {"400", "403", "444"} and str(v).isdigit()
            }
        elif isinstance(item, (str, int, float, bool)) or item is None:
            safe[key] = item
    resolution = value.get("resolution")
    if isinstance(resolution, dict):
        safe["_resolution"] = {
            "note": str(resolution.get("note") or "")[:500],
            "resolved_by": resolution.get("resolved_by") if isinstance(resolution.get("resolved_by"), int) else None,
            "resolved_by_name": str(resolution.get("resolved_by_name") or "")[:80] or None,
            "resolved_at": str(resolution.get("resolved_at") or "")[:64] or None,
        }
    return safe


def _read_json(raw: str | None) -> dict[str, Any]:
    try:
        value = json.loads(raw or "{}")
    except (json.JSONDecodeError, TypeError):
        return {}
    return value if isinstance(value, dict) else {}


def _monitor_snapshot(db: Session) -> dict[str, Any]:
    job = db.query(AgentJob).filter(AgentJob.job_type == "security_monitor").order_by(AgentJob.id.asc()).first()
    run_pair = None
    if job is not None:
        run_pair = (
            db.query(AgentJobRun, AgentJob)
            .join(AgentJob, AgentJob.id == AgentJobRun.job_id)
            .filter(AgentJobRun.job_id == job.id)
            .order_by(AgentJobRun.id.desc())
            .first()
        )
    run = run_pair[0] if run_pair else None
    schedule = job.schedule if job else None
    interval_minutes, schedule_label = _describe_schedule(schedule)
    result = _read_json(run.result_json if run else None)
    errors = result.get("errors") if isinstance(result.get("errors"), list) else []
    completed = result.get("completed_actions") if isinstance(result.get("completed_actions"), list) else []
    all_sources = ["ssh_login_events", "nginx_attack_events", "backup_audit", "db_health", "status"]
    if settings.security_db_monitor_enabled:
        all_sources.append("db_threat_signals")
    if settings.security_flytrap_enabled:
        all_sources.append("flytrap_attack_events")
    source_set = set(all_sources)
    source_errors = [
        item for item in errors
        if isinstance(item, dict) and str(item.get("action") or "") in source_set
    ]
    failed_actions = {
        str(item["action"]) for item in source_errors if item.get("degraded") is not True
    }
    degraded_actions = {
        str(item["action"]) for item in source_errors if item.get("degraded") is True
    }
    sources = [
        {
            "code": source,
            "label": _ACTION_LABELS.get(source, source),
            "status": (
                "failed" if source in failed_actions else
                "degraded" if source in degraded_actions else
                "success" if source in completed else
                "unknown"
            ),
        }
        for source in all_sources
    ]
    return {
        "enabled": bool(settings.security_monitor_enabled and job and job.status == "enabled"),
        "schedule": schedule,
        "schedule_label": schedule_label,
        "interval_minutes": interval_minutes,
        "last_run": {
            "status": str(run.status) if run else "unknown",
            "started_at": _iso(run.started_at) if run else None,
            "finished_at": _iso(run.finished_at) if run else None,
            "completed_sources": len(completed),
            "failed_sources": len(failed_actions),
            "degraded_sources": len(degraded_actions),
            "degraded": bool(errors),
        },
        "sources": sources,
    }


def get_overview(db: Session) -> dict[str, Any]:
    since = _now_utc() - timedelta(hours=24)
    return {
        "generated_at": _iso(_now_utc()),
        "monitoring": _monitor_snapshot(db),
        "policy": get_policy(db),
        "open_alerts_24h": db.query(AgentAlert)
        .filter(
            observability_service.security_monitor_alert_clause(),
            AgentAlert.status == "open",
            AgentAlert.create_time >= since,
        )
        .count(),
        "open_alerts_total": db.query(AgentAlert)
        .filter(
            observability_service.security_monitor_alert_clause(),
            AgentAlert.status == "open",
        )
        .count(),
    }


def list_events(db: Session, *, hours: int = 24, page: int = 1, page_size: int = 20) -> dict[str, Any]:
    cutoff = _now_utc() - timedelta(hours=hours)
    events: list[dict[str, Any]] = []

    alerts = (
        db.query(AgentAlert)
        .filter(
            observability_service.security_monitor_alert_clause(),
            AgentAlert.create_time >= cutoff,
        )
        .order_by(AgentAlert.create_time.desc(), AgentAlert.id.desc())
        .limit(_MAX_ROWS_PER_SOURCE)
        .all()
    )
    for row in alerts:
        detail = _safe_alert_detail(row.detail_json)
        resolution = detail.pop("_resolution", None)
        events.append(
            {
                "id": f"alert:{row.id}",
                "alert_id": int(row.id),
                "recorded_at": _iso(row.create_time),
                "event_type": "alert",
                "layer": "规则告警",
                "severity": row.severity,
                "status": row.status,
                "actor": "安全监控规则",
                "title": row.title,
                "summary": "规则命中并生成告警；这不等同于攻击成功或已被拦截。",
                "evidence_summary": detail,
                "resolution": resolution if isinstance(resolution, dict) else None,
            }
        )

    audit_rows = (
        db.query(AuditLog)
        .filter(
            AuditLog.action.in_(_AUDIT_ACTION_LABELS),
            AuditLog.create_time >= cutoff,
        )
        .order_by(AuditLog.create_time.desc(), AuditLog.id.desc())
        .limit(_MAX_ROWS_PER_SOURCE)
        .all()
    )
    sensitive_audit_actions = {
        "rbac.user_roles_assign",
        "rbac.role_permissions_assign",
        "llm_config_update",
    }
    for row in audit_rows:
        failed_login = row.action == "login" and row.status == "failed"
        if failed_login:
            summary = "认证接口记录一次失败登录尝试；这不代表攻击者已成功进入系统。"
        elif row.action == "login":
            summary = "认证接口记录一次成功登录；仍需结合后续操作审计判断行为。"
        elif row.action in sensitive_audit_actions:
            summary = "系统记录到高影响账号、权限或模型配置变更；请核对操作者与授权依据。"
        else:
            summary = "系统记录到账号或角色管理操作；详情原文不在安全时间线回显。"
        evidence: dict[str, Any] = {"action_code": str(row.action)}
        if row.ip:
            evidence["ip"] = str(row.ip)[:64]
        if row.target_type:
            evidence["target_type"] = str(row.target_type)[:40]
        if row.action == "login" and row.status == "failed" and row.target_id:
            evidence["account"] = str(row.target_id)[:80]
        events.append(
            {
                "id": f"audit:{row.id}",
                "recorded_at": _iso(row.create_time),
                "event_type": "audit",
                "layer": "认证与权限审计",
                "severity": "warning" if failed_login else "high" if row.action in sensitive_audit_actions else "info",
                "status": str(row.status),
                "actor": str(row.actor_name or "未认证账号")[:80],
                "title": _AUDIT_ACTION_LABELS[row.action],
                "action_code": str(row.action),
                "summary": summary,
                "evidence_summary": evidence,
            }
        )

    ops_rows = (
        db.query(OpsExecution)
        .filter(OpsExecution.action.in_(_SECURITY_ACTIONS), OpsExecution.started_at >= cutoff)
        .order_by(OpsExecution.started_at.desc(), OpsExecution.id.desc())
        .limit(_MAX_ROWS_PER_SOURCE)
        .all()
    )
    actor_ids = {int(row.actor_id) for row in ops_rows if row.actor_id is not None}
    actors = (
        {user.id: user.username for user in db.query(User).filter(User.id.in_(actor_ids)).all()} if actor_ids else {}
    )
    request_ids = {str(row.request_id) for row in ops_rows if row.request_id}
    source_by_request: dict[str, str] = {}
    if request_ids:
        audit_rows = (
            db.query(AuditLog.target_id, AuditLog.detail)
            .filter(
                AuditLog.target_type == "production_ops",
                AuditLog.action.like("admin_copilot.ops.%"),
                AuditLog.target_id.in_(request_ids),
            )
            .all()
        )
        for target_id, detail in audit_rows:
            match = re.search(r"source=([A-Za-z0-9_.-]{1,80})", str(detail or ""))
            if target_id and match:
                source_by_request[str(target_id)] = match.group(1)
    for row in ops_rows:
        ok = row.status == "success"
        events.append(
            {
                "id": f"collector:{row.id}",
                "recorded_at": _iso(row.finished_at or row.started_at),
                "event_type": "collector",
                "layer": "只读采集",
                "severity": "info" if ok else "warning",
                "status": row.status,
                "actor": actors.get(row.actor_id, "定时巡检" if row.actor_id is None else "管理员"),
                "title": _ACTION_LABELS.get(row.action, "安全数据采集"),
                "action_code": row.action,
                "summary": "只读采集器执行回执；未执行封禁或反制。" if ok else "采集失败，相关数据源当前存在监控盲区。",
                "evidence_summary": {
                    "risk_level": row.risk_level,
                    "duration_ms": int(row.duration_ms or 0),
                    "source": source_by_request.get(str(row.request_id), "未记录"),
                },
            }
        )

    job_runs = (
        db.query(AgentJobRun)
        .join(AgentJob, AgentJob.id == AgentJobRun.job_id)
        .filter(AgentJob.job_type == "security_monitor", AgentJobRun.started_at >= cutoff)
        .order_by(AgentJobRun.started_at.desc(), AgentJobRun.id.desc())
        .limit(_MAX_ROWS_PER_SOURCE)
        .all()
    )
    for run in job_runs:
        result = _read_json(run.result_json)
        completed = result.get("completed_actions") if isinstance(result.get("completed_actions"), list) else []
        errors = result.get("errors") if isinstance(result.get("errors"), list) else []
        source_set = set(_SECURITY_ACTIONS)
        source_errors = [
            item for item in errors
            if isinstance(item, dict) and str(item.get("action") or "") in source_set
        ]
        failed_source_codes = sorted({
            str(item["action"]) for item in source_errors if item.get("degraded") is not True
        })
        degraded_source_codes = sorted({
            str(item["action"]) for item in source_errors if item.get("degraded") is True
        })
        has_errors = bool(errors)
        display_status = (
            "failed" if run.status == "failed" or (has_errors and not completed) else
            "warning" if has_errors else
            run.status
        )
        events.append(
            {
                "id": f"run:{run.id}",
                "recorded_at": _iso(run.finished_at or run.started_at),
                "event_type": "monitor_run",
                "layer": "安全监控任务",
                "severity": "warning" if display_status != "success" else "info",
                "status": display_status,
                "actor": "系统调度器",
                "title": "安全监控巡检",
                "summary": "巡检运行摘要，不代表已采取自动防御动作。",
                "evidence_summary": {
                    "completed_sources": len(completed),
                    "failed_sources": len(failed_source_codes),
                    "degraded_sources": len(degraded_source_codes),
                    "failed_source_codes": failed_source_codes,
                    "degraded_source_codes": degraded_source_codes,
                },
            }
        )

    policy_logs = (
        db.query(AuditLog)
        .filter(
            AuditLog.action == "security_policy_update",
            AuditLog.target_type == "security_monitor_policy",
            AuditLog.create_time >= cutoff,
        )
        .order_by(AuditLog.create_time.desc(), AuditLog.id.desc())
        .limit(_MAX_ROWS_PER_SOURCE)
        .all()
    )
    for row in policy_logs:
        detail = _read_json(row.detail)
        events.append(
            {
                "id": f"policy:{row.id}",
                "recorded_at": _iso(row.create_time),
                "event_type": "policy_change",
                "layer": "策略变更",
                "severity": "info",
                "status": row.status,
                "actor": row.actor_name or "管理员",
                "title": "安全监控策略调整",
                "summary": "仅调整检测阈值/告警灵敏度；自动封禁与反击仍关闭。",
                "evidence_summary": {
                    "before": detail.get("before", {}),
                    "after": detail.get("after", {}),
                    "revision": detail.get("revision"),
                },
            }
        )

    events.sort(key=lambda item: (item["recorded_at"] or "", item["id"]), reverse=True)
    total = len(events)
    offset = (page - 1) * page_size
    return {
        "items": events[offset : offset + page_size],
        "total": total,
        "page": page,
        "page_size": page_size,
        "pages": (total + page_size - 1) // page_size if total else 0,
        "truncated": any(
            len(rows) >= _MAX_ROWS_PER_SOURCE for rows in (alerts, audit_rows, ops_rows, job_runs, policy_logs)
        ),
        "hours": hours,
    }
