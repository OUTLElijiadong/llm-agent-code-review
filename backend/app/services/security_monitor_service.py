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
    # general_log 未开启是 db_threat_signals 的可观测降级，不影响其他
    # 安全源。其他采集器明确返回 ok=false 时必须记入本轮 errors，
    # 不能把空数据当成“没有风险”。
    if action != "db_threat_signals" and payload.get("ok") is False:
        reason = payload.get("source_error") or payload.get("reason") or payload.get("error")
        raise RuntimeError(str(reason or f"动作 {action} 数据源不可用"))
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
        # 白名单来源（本人/办公网段）不计入爆破，避免误报
        if _ip_allowed(ip, allowlist):
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


def _retired_flytrap_payload(since_hours: int) -> dict[str, Any]:
    """返回可向后兼容的退役态，不把已关停组件计为故障。"""
    return {
        "ok": True,
        "enabled": False,
        "status": "retired",
        "degraded": False,
        "can_continue": True,
        "reason": "FlyTrap 集成已退役",
        "human_actions": [],
        "since_hours": since_hours,
        "total": 0,
        "by_ip": [],
        "by_username": [],
        "recent": [],
    }


def _evaluate_flytrap(db: Session, flytrap: dict[str, Any], created: list[dict[str, Any]]) -> None:
    """蜜罐规则：同 IP 触碰次数达到阈值触发 warning。"""
    health_fingerprint = "integration:flytrap_upstream"
    if flytrap.get("status") == "retired" or flytrap.get("enabled") is False:
        existing = db.query(AgentAlert).filter(
            AgentAlert.status == "open",
            AgentAlert.fingerprint == health_fingerprint,
        ).first()
        if existing is not None:
            admin = _find_admin(db)
            observability_service.resolve_alert(
                db,
                existing.id,
                admin.id if admin else 0,
                note=json.dumps({
                    "resolution": "FlyTrap 集成已按退役计划停用",
                    "resolved_at": _utcnow_iso(),
                }, ensure_ascii=False),
            )
        return
    if flytrap.get("degraded") is True:
        health = flytrap.get("health") if isinstance(flytrap.get("health"), dict) else {}
        actions = flytrap.get("human_actions") if isinstance(flytrap.get("human_actions"), list) else []
        _record_alert(
            db,
            created,
            category="integration",
            severity="warning",
            fingerprint=health_fingerprint,
            title="FlyTrap 上游同步降级",
            suggestion=(
                "核对上游服务、防火墙和网络路由；本地蜜罐与持久队列可继续工作，"
                "恢复后确认同步成功日志"
            ),
            detail={"health": health, "human_actions": actions},
        )
    elif flytrap.get("ok") is True:
        existing = db.query(AgentAlert).filter(
            AgentAlert.status == "open",
            AgentAlert.fingerprint == health_fingerprint,
        ).first()
        if existing is not None:
            admin = _find_admin(db)
            observability_service.resolve_alert(
                db,
                existing.id,
                admin.id if admin else 0,
                note=json.dumps({
                    "resolution": "FlyTrap 上游同步已恢复",
                    "health": flytrap.get("health") or {},
                    "resolved_at": _utcnow_iso(),
                }, ensure_ascii=False, default=str),
            )

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

    retention_hours = 24 * 14  # 与 backup.sh 默认保留期一致；超期文件将删除，不再告警
    unverified = [
        row.get("name") for row in (backup.get("recent") or [])
        if isinstance(row, dict)
        and row.get("has_sha256") is False
        and float(row.get("age_hours") or 0) <= retention_hours
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


def _evaluate_db(db: Session, db_signals: dict[str, Any], created: list[dict[str, Any]]) -> None:
    """数据库内部威胁规则：破坏性写/数据外泄 critical；访问拒绝告警 warning。"""
    # general_log 未开启时 executor 返回 ok=False，不产出告警（不视为攻击）。
    if not db_signals.get("ok"):
        return
    destructive_threshold = settings.security_db_destructive_threshold
    destructive_total = int(db_signals.get("destructive_total") or 0)
    if destructive_total >= destructive_threshold:
        samples = (db_signals.get("samples") or {}).get("destructive") or []
        top_users = db_signals.get("destructive_by_user") or []
        _record_alert(
            db,
            created,
            category="db_threat",
            severity="critical",
            fingerprint="db:destructive",
            title=f"数据库破坏性操作：{destructive_total} 条（DROP/TRUNCATE/DELETE/权限变更）",
            suggestion=(
                "立即核对变更窗口与操作账号；若非授权变更，备份当前库并回滚，"
                "排查应用层注入或被盗账号"
            ),
            detail={
                "count": destructive_total,
                "window_hours": settings.security_db_window_hours,
                "by_user": top_users[:10],
                "samples": samples[:10],
            },
        )

    dump_threshold = settings.security_db_dump_threshold
    dump_total = int(db_signals.get("dump_exfil_total") or 0)
    if dump_total >= dump_threshold:
        samples = (db_signals.get("samples") or {}).get("dump_exfil") or []
        _record_alert(
            db,
            created,
            category="db_threat",
            severity="critical",
            fingerprint="db:dump_exfil",
            title=f"数据库疑似批量导出/外泄：{dump_total} 条（SELECT INTO OUTFILE/LOAD_FILE）",
            suggestion=(
                "立即排查是否发生数据外泄，审查导出文件落点与发起账号；"
                "必要时隔离数据库并轮换凭据"
            ),
            detail={
                "count": dump_total,
                "window_hours": settings.security_db_window_hours,
                "samples": samples[:10],
            },
        )

    error_threshold = settings.security_db_error_threshold
    error_total = int(db_signals.get("error_total") or 0)
    if error_total >= error_threshold:
        _record_alert(
            db,
            created,
            category="db_threat",
            severity="warning",
            fingerprint="db:errors",
            title=f"数据库访问异常/拒绝：{error_total} 条",
            suggestion="核对应用账号权限与连接来源，排查注入探测或异常调用",
            detail={
                "count": error_total,
                "window_hours": settings.security_db_window_hours,
                "by_user": (db_signals.get("error_by_user") or [])[:10],
            },
        )


def _evaluate_db_health(db: Session, health: dict[str, Any], created: list[dict[str, Any]]) -> None:
    """数据库可用性规则：仅真实异常（InnoDB 崩溃恢复 / Docker 容器重启）→ critical。

    正常发布重启（container_restart_count=0 且无崩溃恢复日志）不告警——
    部署事务本身会重启 MySQL，不能据此误报 OOM。
    """
    restart_lines = health.get("restart_lines") or []
    recovery_detected = bool(health.get("recovery_detected"))
    restart_count = int(health.get("restart_count") or 0)
    try:
        container_restart = int(str(health.get("container_restart_count") or "0"))
    except (TypeError, ValueError):
        container_restart = 0
    # 仅当出现崩溃恢复日志，或 Docker 记录的非正常容器重启时告警。
    if not (recovery_detected or container_restart > 0):
        return
    _record_alert(
        db,
        created,
        category="db_threat",
        severity="critical",
        fingerprint="db:health_restart",
        title=(
            f"数据库异常重启（容器重启 {container_restart} 次"
            + ("，检测到 InnoDB 崩溃恢复）" if recovery_detected else "）")
        ),
        suggestion=(
            "数据库曾被杀/崩溃，可能导致批准/写入中断。检查容器内存限额与 mysqld "
            "内存配置（performance_schema/buffer_pool），防止 OOM 复发；必要时提高 mem_limit"
        ),
        detail={
            "restart_count": restart_count,
            "recovery_detected": recovery_detected,
            "container_restart_count": health.get("container_restart_count"),
            "mem_usage": health.get("mem_usage"),
            "restart_lines": restart_lines[:5],
        },
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
    _evaluate_db(db, results.get("db_threat_signals") or {}, created)
    _evaluate_db_health(db, results.get("db_health") or {}, created)
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
    actions = [
        ("ssh_login_events", {"since_hours": ssh_window, "limit": 2000, "focus": "all"}),
        ("nginx_attack_events", {"since_hours": attack_window, "limit": 2000}),
        ("backup_audit", {}),
        ("db_health", {}),
        ("status", {}),
    ]
    if settings.security_db_monitor_enabled:
        actions.insert(
            4,
            (
                "db_threat_signals",
                {"since_hours": settings.security_db_window_hours, "limit": settings.security_db_sample_limit},
            ),
        )
    if settings.security_flytrap_enabled:
        actions.insert(1, ("flytrap_attack_events", {"since_hours": attack_window, "limit": 2000}))
    results: dict[str, Any] = {
        "flytrap_attack_events": _retired_flytrap_payload(attack_window)
    } if not settings.security_flytrap_enabled else {}
    errors: list[dict[str, Any]] = []
    completed_actions: list[str] = []
    failed_actions: list[str] = []
    degraded_actions: list[str] = []
    human_actions: list[dict[str, Any]] = []
    for action, params in actions:
        try:
            payload = _call_action(db, action, params)
            results[action] = payload
            completed_actions.append(action)
            if payload.get("degraded") is True:
                degraded_actions.append(action)
                action_human_actions = (
                    payload.get("human_actions")
                    if isinstance(payload.get("human_actions"), list)
                    else []
                )
                for item in action_human_actions:
                    if isinstance(item, dict) and item not in human_actions:
                        human_actions.append(item)
                next_action = next(
                    (
                        str(item.get("message") or item.get("label") or "")
                        for item in action_human_actions
                        if isinstance(item, dict)
                    ),
                    "请人工检查该数据源并在恢复后重新运行巡检",
                )
                errors.append({
                    "action": action,
                    "error": str(payload.get("reason") or f"动作 {action} 处于降级状态"),
                    "degraded": True,
                    "retryable": True,
                    "next_action": next_action,
                })
        except Exception as exc:  # noqa: BLE001 - 单动作失败不中断整体巡检
            errors.append({"action": action, "error": str(exc)})
            failed_actions.append(action)
            results[action] = {"ok": False, "error": str(exc)}

    created_alerts: list[dict[str, Any]] = []
    try:
        created_alerts = _evaluate(db, results)
    except Exception as exc:  # noqa: BLE001 - 评估失败不阻断返回摘要
        errors.append({"action": "evaluate", "error": str(exc)})
        failed_actions.append("evaluate")

    # “部分失败”可以继续下一轮并保留人工复核入口；全部数据源失败则必须
    # 在运行记录中如实标成失败，避免把安全监控盲区伪装成无风险。
    partial_failure = bool(errors) and bool(completed_actions)
    fatal_failure = bool(errors) and not completed_actions

    return {
        "success": not errors,
        "can_continue": bool(completed_actions),
        "partial_failure": partial_failure,
        "fatal_failure": fatal_failure,
        "completed_actions": completed_actions,
        "failed_actions": failed_actions,
        "degraded_actions": degraded_actions,
        "human_actions": human_actions,
        "created_alerts": created_alerts,
        "actions": results,
        "errors": errors,
        **(
            {"error": "安全监控所有数据源均不可用，请人工核验执行器和日志"}
            if fatal_failure
            else {}
        ),
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
    actions = [
        ("ssh_login_events", {"since_hours": since_hours, "limit": 2000, "focus": "all"}),
        ("nginx_attack_events", {"since_hours": since_hours, "limit": 2000}),
        ("backup_audit", {}),
    ]
    if settings.security_flytrap_enabled:
        actions.insert(1, ("flytrap_attack_events", {"since_hours": since_hours, "limit": 2000}))
    if settings.security_db_monitor_enabled:
        actions.append(
            ("db_threat_signals", {"since_hours": since_hours, "limit": settings.security_db_sample_limit})
        )
    results: dict[str, Any] = {
        "flytrap_attack_events": _retired_flytrap_payload(since_hours)
    } if not settings.security_flytrap_enabled else {}
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
    db_signals = results.get("db_threat_signals") or {}
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
            "flytrap_enabled": bool(settings.security_flytrap_enabled),
            "flytrap_status": str(
                flytrap.get("status") or ("active" if settings.security_flytrap_enabled else "retired")
            ),
            "flytrap_total": int(flytrap.get("total") or 0),
            "flytrap_top_ips": flytrap.get("by_ip") or [],
            "flytrap_degraded": bool(flytrap.get("degraded")),
            "flytrap_health": flytrap.get("health") or {},
            "flytrap_human_actions": flytrap.get("human_actions") or [],
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
        "db_threat": {
            "enabled": bool(settings.security_db_monitor_enabled),
            "ok": bool(db_signals.get("ok")),
            "reason": db_signals.get("reason"),
            "destructive_total": int(db_signals.get("destructive_total") or 0),
            "dump_exfil_total": int(db_signals.get("dump_exfil_total") or 0),
            "error_total": int(db_signals.get("error_total") or 0),
            "destructive_by_user": db_signals.get("destructive_by_user") or [],
            "error_by_user": db_signals.get("error_by_user") or [],
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
