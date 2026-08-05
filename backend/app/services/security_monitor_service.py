"""Agent 安全监控服务（规则引擎 + 去重 + 溯源富化 + SSE 弹窗推送）。

职责：
1. ``run_security_monitor``：通过 ``ops_service.execute`` 以调度身份拉取
   ssh_login_events / flytrap_attack_events / nginx_attack_events / backup_audit / status，
   逐动作 try/except，单动作失败不中断整体，再按规则生成告警。
2. ``query_security_status``：聚合安全态势（登录/攻击/备份/最近 open 告警），
   供最高管理员管理 Agent 查询与 /status API 使用。

规则阈值全部来自 Settings（security_*），弹窗仅对 severity >=
``security_popup_min_severity`` 的新告警推送 SSE 事件（去重命中的 open 告警只刷新
last_seen，不重复弹窗）。
"""
from __future__ import annotations

import ipaddress
import json
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy.orm import Session

from app.agents.event_bus import emit_event
from app.agents.events import AgentEventType, new_trace_id
from app.core.config import settings
from app.models.agent_governance import AgentAlert
from app.models.user import User
from app.services import observability_service, ops_service

# 严重度等级映射：数值越大越严重
SEVERITY_ORDER = {"info": 0, "warning": 1, "high": 2, "critical": 3}
# 可弹窗的严重度集合
_POPUP_SEVERITIES = {"warning", "high", "critical"}
# 需要 IP 溯源富化的高危来源类别
_ATTRIBUTION_CATEGORIES = {"login", "brute_force", "attack"}
# 告警来源标识
ALERT_SOURCE = "security_monitor"
# 磁盘使用率弹窗阈值（与 ops-check.sh 默认 85 保持一致的保守值 80）
_DISK_USED_PERCENT_THRESHOLD = 80
# TLS 乱码探测同 IP 触发阈值
_NGINX_TLS_GIBBERISH_THRESHOLD = 5


def _utcnow_iso() -> str:
    """返回当前 UTC 时间的 ISO 字符串。"""
    return datetime.now(timezone.utc).isoformat()


def _call_action(db: Session, action: str, params: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """以调度身份调用一次只读运维动作并解出执行器数据。

    Args:
        db: 数据库会话。
        action: 只读运维动作名。
        params: 动作参数。

    Returns:
        dict[str, Any]: 执行器返回的数据负载。

    Raises:
        RuntimeError: 动作执行失败（状态非 success 或负载解析失败）。
    """
    execution = ops_service.execute(
        db,
        None,
        action=action,
        params=params or {},
        source="scheduler",
        request_id=uuid.uuid4().hex,
    )
    executor_response = execution.get("result") if isinstance(execution.get("result"), dict) else {}
    payload = executor_response.get("result") if isinstance(executor_response.get("result"), dict) else {}
    if execution.get("status") != "success" or not executor_response.get("ok") or not isinstance(payload, dict):
        raise RuntimeError(execution.get("error") or f"动作 {action} 执行失败")
    return payload


def _find_admin(db: Session) -> Optional[User]:
    """查找弹窗目标：唯一超级管理员 admin（找不到返回 None，告警仍入库）。"""
    return db.query(User).filter(User.username == "admin", User.role == "super_admin").first()


def _parse_allowlist(cidrs: list[str]) -> list[ipaddress._BaseNetwork]:
    """解析 SSH 来源白名单 CIDR；非法条目直接忽略。"""
    networks: list[ipaddress._BaseNetwork] = []
    for raw in cidrs or []:
        text = str(raw).strip()
        if not text:
            continue
        try:
            networks.append(ipaddress.ip_network(text, strict=False))
        except ValueError:
            continue
    return networks


def _ip_allowed(ip: str, networks: list[ipaddress._BaseNetwork]) -> bool:
    """判断 IP 是否命中白名单网络。"""
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return any(addr in network for network in networks)


def _try_ip_attribution(db: Session, ip: str) -> Optional[dict[str, Any]]:
    """对来源 IP 做被动溯源富化；失败返回 None，不影响告警创建。"""
    try:
        payload = _call_action(db, "ip_attribution", {"ip": ip})
    except Exception:  # noqa: BLE001 - 溯源失败不应阻断告警
        return None
    return payload if isinstance(payload, dict) else None


def _maybe_notify(
    alert: AgentAlert,
    admin: Optional[User],
    *,
    severity: str,
    category: str,
    title: str,
    suggestion: str,
) -> None:
    """按弹窗条件推送 SSE 事件（仅对新告警调用）。

    弹窗条件：severity 属于 {warning, high, critical}，且等级不低于
    ``security_popup_min_severity``。找不到目标管理员时跳过 SSE，告警仍入库。
    """
    min_severity = settings.security_popup_min_severity
    if severity not in _POPUP_SEVERITIES:
        return
    if SEVERITY_ORDER.get(severity, 0) < SEVERITY_ORDER.get(min_severity, 0):
        return
    if admin is None:
        return
    emit_event(
        AgentEventType.ADMIN_ALERT,
        agent="operations",
        trace_id=new_trace_id(),
        parent="manager",
        message=title,
        payload={
            "alert_id": alert.id,
            "severity": severity,
            "category": category,
            "title": title,
            "suggestion": suggestion,
        },
        user_id=admin.id,
    )


def _record_alert(
    db: Session,
    created: list[dict[str, Any]],
    *,
    category: str,
    severity: str,
    fingerprint: str,
    title: str,
    suggestion: str = "",
    ip: Optional[str] = None,
    detail: Optional[dict[str, Any]] = None,
) -> None:
    """去重写入告警；新告警按严重度触发溯源富化与 SSE 弹窗。

    去重：status="open" 且 fingerprint 相同的告警已存在时，只刷新 detail_json
    里的 last_seen 并跳过新建（不重复弹窗）。
    """
    existing = db.query(AgentAlert).filter(
        AgentAlert.status == "open",
        AgentAlert.fingerprint == fingerprint,
    ).first()
    payload_detail = dict(detail or {})
    payload_detail["last_seen"] = _utcnow_iso()
    if existing is not None:
        existing.detail_json = json.dumps(payload_detail, ensure_ascii=False, default=str)
        db.commit()
        return

    admin = _find_admin(db)
    alert = observability_service.create_alert(
        db,
        alert_type=f"security.{category}",
        severity=severity,
        title=title,
        detail=payload_detail,
        category=category,
        source=ALERT_SOURCE,
        user_id=admin.id if admin else None,
        fingerprint=fingerprint,
    )
    # 高危来源 IP 被动溯源富化（失败不影响告警创建）
    if ip and category in _ATTRIBUTION_CATEGORIES and severity in _POPUP_SEVERITIES:
        attribution = _try_ip_attribution(db, ip)
        if attribution is not None:
            attribution_data = (
                attribution.get("attribution")
                if isinstance(attribution.get("attribution"), dict)
                else attribution
            )
            alert.detail_json = json.dumps(
                {**payload_detail, "attribution": attribution_data},
                ensure_ascii=False,
                default=str,
            )
            db.commit()
    _maybe_notify(alert, admin, severity=severity, category=category, title=title, suggestion=suggestion)
    created.append({
        "alert_id": alert.id,
        "fingerprint": fingerprint,
        "severity": severity,
        "category": category,
        "title": title,
    })


def _evaluate_ssh(db: Session, ssh: dict[str, Any], created: list[dict[str, Any]]) -> None:
    """SSH 规则：成功登录（非白名单 high / 白名单 info）与失败爆破 warning。"""
    allowlist = _parse_allowlist(settings.security_ssh_allowlist_cidrs)
    for item in ssh.get("recent") or []:
        if not isinstance(item, dict):
            continue
        if item.get("detail") in {"failed_password", "invalid_user"}:
            continue
        ip = str(item.get("ip") or "")
        user = str(item.get("user") or "unknown")
        if not ip:
            continue
        allowed = _ip_allowed(ip, allowlist)
        severity = "info" if allowed else "high"
        _record_alert(
            db,
            created,
            category="login",
            severity=severity,
            fingerprint=f"login:{ip}:{user}",
            title=f"SSH 登录：{ip}（{user}）",
            suggestion=(
                "" if allowed else "确认是否本人操作；非本人应立即吊销对应密钥并轮换"
            ),
            ip=ip,
            detail={"ip": ip, "user": user, "method": item.get("method") or ""},
        )

    failed_threshold = settings.security_failed_login_threshold
    for agg in ssh.get("failed_by_ip") or []:
        if not isinstance(agg, dict):
            continue
        ip = str(agg.get("value") or "")
        count = int(agg.get("count") or 0)
        if not ip or count < failed_threshold:
            continue
        _record_alert(
            db,
            created,
            category="brute_force",
            severity="warning",
            fingerprint=f"brute:{ip}",
            title=f"SSH 爆破：{ip}（{count} 次失败）",
            suggestion="建议封禁该来源 IP 并核对是否已有账户被攻破",
            ip=ip,
            detail={
                "ip": ip,
                "failed_count": count,
                "window_hours": settings.security_failed_login_window_hours,
                "threshold": failed_threshold,
            },
        )


def _evaluate_flytrap(db: Session, flytrap: dict[str, Any], created: list[dict[str, Any]]) -> None:
    """蜜罐规则：同 IP 触碰次数达到阈值触发 warning。"""
    flytrap_threshold = settings.security_flytrap_threshold
    for agg in flytrap.get("by_ip") or []:
        if not isinstance(agg, dict):
            continue
        ip = str(agg.get("value") or "")
        count = int(agg.get("count") or 0)
        if not ip or count < flytrap_threshold:
            continue
        _record_alert(
            db,
            created,
            category="attack",
            severity="warning",
            fingerprint=f"attack:{ip}",
            title=f"蜜罐触碰：{ip}（{count} 次）",
            suggestion="蜜罐已隔离攻击流量，可同步在防火墙封禁该来源 IP",
            ip=ip,
            detail={
                "ip": ip,
                "attack_count": count,
                "window_hours": settings.security_flytrap_window_hours,
                "threshold": flytrap_threshold,
            },
        )


def _evaluate_nginx(db: Session, nginx: dict[str, Any], created: list[dict[str, Any]]) -> None:
    """Nginx 规则：CONNECT 代理探测 info（不弹窗）；TLS 乱码同 IP ≥5 记 scanner。"""
    seen_proxy: set[str] = set()
    gibberish: dict[str, int] = {}
    for item in nginx.get("recent") or []:
        if not isinstance(item, dict):
            continue
        ip = str(item.get("ip") or "")
        if not ip:
            continue
        detail_kind = item.get("detail")
        if detail_kind == "proxy_connect":
            fingerprint = f"proxy:{ip}"
            if fingerprint in seen_proxy:
                continue
            seen_proxy.add(fingerprint)
            _record_alert(
                db,
                created,
                category="proxy_abuse",
                severity="info",
                fingerprint=fingerprint,
                title=f"Nginx 代理探测：{ip}",
                suggestion="",
                detail={"ip": ip, "kind": "proxy_connect"},
            )
        elif detail_kind == "tls_gibberish":
            gibberish[ip] = gibberish.get(ip, 0) + 1
    for ip, count in gibberish.items():
        if count < _NGINX_TLS_GIBBERISH_THRESHOLD:
            continue
        _record_alert(
            db,
            created,
            category="scanner",
            severity="info",
            fingerprint=f"scanner:{ip}",
            title=f"TLS 探测：{ip}（{count} 次）",
            suggestion="",
            detail={"ip": ip, "scanner_count": count},
        )


def _evaluate_backup(db: Session, backup: dict[str, Any], created: list[dict[str, Any]]) -> None:
    """备份规则：超龄 high / 校验缺失 critical / 目录过大 warning。"""
    newest = backup.get("newest") if isinstance(backup.get("newest"), dict) else None
    if newest is not None:
        try:
            age_hours = float(newest.get("age_hours") or 0)
        except (TypeError, ValueError):
            age_hours = 0.0
        if age_hours > settings.security_backup_max_age_hours:
            _record_alert(
                db,
                created,
                category="backup",
                severity="high",
                fingerprint="backup:age",
                title=f"备份超龄：最新备份 {age_hours:.1f} 小时前",
                suggestion="立即执行备份并验证，检查备份定时器是否中断",
                detail={"age_hours": age_hours, "max_age_hours": settings.security_backup_max_age_hours},
            )

    unverified = [
        row.get("name") for row in (backup.get("recent") or [])
        if isinstance(row, dict) and row.get("has_sha256") is False
    ]
    if unverified:
        _record_alert(
            db,
            created,
            category="backup",
            severity="critical",
            fingerprint="backup:verify",
            title="备份缺少 SHA256 校验文件",
            suggestion="立即执行备份校验（verify-backup），确认备份可恢复",
            detail={"unverified": unverified[:20]},
        )

    try:
        total_bytes = int(backup.get("sql_gz_bytes") or 0) + int(backup.get("other_bytes") or 0)
    except (TypeError, ValueError):
        total_bytes = 0
    max_bytes = settings.security_backup_dir_max_gb * 1_000_000_000
    if total_bytes > max_bytes:
        _record_alert(
            db,
            created,
            category="backup",
            severity="warning",
            fingerprint="backup:size",
            title=f"备份目录占用过大：{total_bytes / 1e9:.1f} GB",
            suggestion="清理超期备份与手工产物（审批后执行）",
            detail={"total_bytes": total_bytes, "max_bytes": max_bytes},
        )


def _evaluate_status(db: Session, status: dict[str, Any], created: list[dict[str, Any]]) -> None:
    """磁盘规则：ops-check used_percent ≥80 触发 optimization warning。"""
    checks = status.get("checks") if isinstance(status.get("checks"), dict) else {}
    disk = checks.get("disk") if isinstance(checks.get("disk"), dict) else {}
    used_percent = disk.get("used_percent")
    if isinstance(used_percent, str):
        try:
            used_percent = int(used_percent)
        except (TypeError, ValueError):
            return
    if not isinstance(used_percent, (int, float)):
        return
    if used_percent >= _DISK_USED_PERCENT_THRESHOLD:
        _record_alert(
            db,
            created,
            category="optimization",
            severity="warning",
            fingerprint="opt:disk",
            title=f"磁盘使用率过高：{used_percent}%",
            suggestion="清理超期备份/日志/旧镜像，或扩容（审批后执行）",
            detail={"used_percent": used_percent, "threshold": _DISK_USED_PERCENT_THRESHOLD},
        )


def _evaluate(db: Session, results: dict[str, Any]) -> list[dict[str, Any]]:
    """按规则表评估各动作结果并写入告警。"""
    created: list[dict[str, Any]] = []
    _evaluate_ssh(db, results.get("ssh_login_events") or {}, created)
    _evaluate_flytrap(db, results.get("flytrap_attack_events") or {}, created)
    _evaluate_nginx(db, results.get("nginx_attack_events") or {}, created)
    _evaluate_backup(db, results.get("backup_audit") or {}, created)
    _evaluate_status(db, results.get("status") or {}, created)
    return created


def run_security_monitor(db: Session, job: Any = None) -> dict[str, Any]:
    """执行一轮完整的安全监控巡检。

    依次拉取 ssh_login_events / flytrap_attack_events / nginx_attack_events /
    backup_audit / status，单动作失败不中断整体；最后按规则生成告警并返回摘要。

    Args:
        db: 数据库会话。
        job: 触发本次巡检的调度任务（可选）。

    Returns:
        dict[str, Any]: 各动作结果、新建告警列表与错误摘要。
    """
    ssh_window = settings.security_failed_login_window_hours
    attack_window = settings.security_flytrap_window_hours
    actions = (
        ("ssh_login_events", {"since_hours": ssh_window, "limit": 2000, "focus": "all"}),
        ("flytrap_attack_events", {"since_hours": attack_window, "limit": 2000}),
        ("nginx_attack_events", {"since_hours": attack_window, "limit": 2000}),
        ("backup_audit", {}),
        ("status", {}),
    )
    results: dict[str, Any] = {}
    errors: list[dict[str, Any]] = []
    for action, params in actions:
        try:
            results[action] = _call_action(db, action, params)
        except Exception as exc:  # noqa: BLE001 - 单动作失败不中断整体巡检
            errors.append({"action": action, "error": str(exc)})
            results[action] = {"ok": False, "error": str(exc)}

    created_alerts: list[dict[str, Any]] = []
    try:
        created_alerts = _evaluate(db, results)
    except Exception as exc:  # noqa: BLE001 - 评估失败不阻断返回摘要
        errors.append({"action": "evaluate", "error": str(exc)})

    return {
        "success": not errors,
        "created_alerts": created_alerts,
        "actions": results,
        "errors": errors,
        "job_id": job.id if job is not None else None,
    }


def query_security_status(db: Session, since_hours: int = 24) -> dict[str, Any]:
    """聚合安全态势，供 Agent 查询与 /status API 使用。

    Args:
        db: 数据库会话。
        since_hours: 回看窗口（小时）。

    Returns:
        dict[str, Any]: SSH 登录统计、攻击 Top IP、备份摘要与最近 open 告警。
    """
    actions = (
        ("ssh_login_events", {"since_hours": since_hours, "limit": 2000, "focus": "all"}),
        ("flytrap_attack_events", {"since_hours": since_hours, "limit": 2000}),
        ("nginx_attack_events", {"since_hours": since_hours, "limit": 2000}),
        ("backup_audit", {}),
    )
    results: dict[str, Any] = {}
    errors: list[dict[str, Any]] = []
    for action, params in actions:
        try:
            results[action] = _call_action(db, action, params)
        except Exception as exc:  # noqa: BLE001 - 单动作失败不阻断聚合
            errors.append({"action": action, "error": str(exc)})

    ssh = results.get("ssh_login_events") or {}
    flytrap = results.get("flytrap_attack_events") or {}
    nginx = results.get("nginx_attack_events") or {}
    backup = results.get("backup_audit") or {}
    open_alerts = (
        db.query(AgentAlert)
        .filter(AgentAlert.status == "open")
        .order_by(AgentAlert.id.desc())
        .limit(20)
        .all()
    )
    return {
        "since_hours": since_hours,
        "ssh": {
            "accepted_total": int(ssh.get("accepted_total") or 0),
            "failed_total": int(ssh.get("failed_total") or 0),
            "total": int(ssh.get("accepted_total") or 0) + int(ssh.get("failed_total") or 0),
            "accepted_top_ips": ssh.get("accepted_by_ip") or [],
            "failed_top_ips": ssh.get("failed_by_ip") or [],
        },
        "attacks": {
            "flytrap_total": int(flytrap.get("total") or 0),
            "flytrap_top_ips": flytrap.get("by_ip") or [],
            "nginx_total": int(nginx.get("total") or 0),
            "nginx_top_ips": nginx.get("by_ip") or [],
        },
        "backup": {
            "sql_gz_count": backup.get("sql_gz_count"),
            "sql_gz_bytes": backup.get("sql_gz_bytes"),
            "other_bytes": backup.get("other_bytes"),
            "older_than_14_days": backup.get("older_than_14_days"),
            "newest": backup.get("newest"),
            "oldest": backup.get("oldest"),
        },
        "open_alerts": [
            {
                "id": alert.id,
                "severity": alert.severity,
                "category": alert.category,
                "source": alert.source,
                "title": alert.title,
                "status": alert.status,
                "create_time": alert.create_time.isoformat() if alert.create_time else None,
            }
            for alert in open_alerts
        ],
        "errors": errors,
    }
