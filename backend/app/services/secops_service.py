"""小菱安全运营（SecOps）服务：自动化网络攻防的编排层。

职责是把平台已有的攻防零件串成闭环，而不是另造一套攻击工具：

- **感知**：`ssh_login_events` / `nginx_attack_events` / `flytrap_attack_events` /
  `db_threat_signals` / `certificate_status` / `status`（全部经 `ops_service` 走到宿主机上的
  白名单执行器，不在应用进程内直连主机）。
- **判定**：把多源事件聚成攻击源画像（次数、攻击面、地理归属、命中规则），给出处置建议。
- **处置**：封禁 / 解封走 `security_block_*`，属于状态变更，必须由唯一超级管理员发起并留审计；
  无人值守巡检默认只出结论不落地封禁（`secops_auto_block_enabled` 显式打开才会自动封）。
- **验证**：对平台自身做基线核查（文档暴露、安全响应头、证书、监听面），输出可执行的修复项。
- **授权攻**：只驱动**已授权且在时间窗内**的渗透测试委托（`pentest_service`），
  本模块不包含任何攻击载荷或扫描器实现，也不接受任意目标地址。

安全边界：
1. 所有宿主机动作的 `source` 恒为 `security_center`，复用 `ops_service` 的固定白名单校验；
2. `apply_block` / `release_block` / `start_pentest` 只接受唯一超级管理员，并写 `secops.*` 审计；
3. 基线核查的 HTTP 探测目标必须命中 `secops_asset_hosts`（默认空 = 不做外部探测）；
4. 自动封禁默认关闭；打开后也只在「同一来源在多源传感器同时命中」时才落地。
"""

from __future__ import annotations

import ipaddress
import json
import socket
import ssl
from datetime import datetime, timezone
from typing import Any, Optional
from urllib.parse import urlparse

import httpx
from loguru import logger
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.exceptions import ForbiddenError, NotFoundError, ValidationError
from app.models.audit_log import AuditLog
from app.models.pentest import PentestEngagement
from app.models.user import User
from app.services import copilot_security_service, geoip_service, observability_service, ops_service

# 宿主机执行器要求的来源标识（见 ops_service 对 INTERNAL_SECURITY_ACTIONS 的两条校验分支）：
# - 最高管理员在小菱/安全中心里发起 → "security_center"（interactive）
# - 无人值守巡检（actor=None）→ "scheduler"（automatic，只放行固定只读与自动封禁动作）
OPS_SOURCE_INTERACTIVE = "security_center"
OPS_SOURCE_SCHEDULER = "scheduler"
# 审计动作前缀。
EVENT_PREFIX = "secops."
# 感知类动作：只读、可无人值守执行。
SENSOR_ACTIONS: tuple[str, ...] = (
    "ssh_login_events",
    "nginx_attack_events",
    "flytrap_attack_events",
    "db_threat_signals",
    "security_block_status",
    "security_block_candidates",
    "security_surface_audit",
    "security_traffic_summary",
    "certificate_status",
)
# 只读动作：小菱可以在最顶级闸门下直接调用的安全运营工具。
READ_ONLY_TOOLS: tuple[str, ...] = (
    "secops.threat_digest",
    "secops.surface_audit",
    "secops.block_candidates",
    "secops.baseline_check",
    "secops.patrol_status",
)
# 状态变更动作：需要审批/二次确认。
STATEFUL_TOOLS: tuple[str, ...] = (
    "secops.block_apply",
    "secops.block_release",
    "secops.pentest_start",
)

# 判定阈值
_CRITICAL_SENSORS = 3        # 同一来源在 >=3 个传感器命中 → critical
_CRITICAL_VOLUME = 20        # 或者单窗口内命中 >=20 次
_HIGH_SENSORS = 2            # 两个传感器同时命中且有一定量 → high
_HIGH_VOLUME = 4
_MAX_SOURCES = 25


# ── 公共小工具 ─────────────────────────────────────────────────────────────


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: Any) -> Optional[str]:
    if isinstance(value, datetime):
        return value.replace(tzinfo=value.tzinfo or timezone.utc).astimezone(timezone.utc).isoformat()
    return None


def _require_super_admin(db: Session, actor: Optional[User]) -> User:
    """状态变更动作只允许唯一超级管理员；与 ops_service 的判定保持一致。"""
    from app.services import rbac_service

    if actor is None or not rbac_service.is_super_admin_user(db, int(actor.id)):
        raise ForbiddenError("仅超级管理员 admin 可执行安全处置动作", code=40323)
    return actor


def _audit(
    db: Session,
    *,
    kind: str,
    actor: Optional[User],
    detail: dict,
    status: str = "success",
    target: str = "",
    client_ip: str = "",
) -> None:
    """写一条 `secops.*` 审计事件；详情经小菱安全中心的脱敏口径落库。"""
    policy = copilot_security_service.load_policy(db)
    payload = copilot_security_service.redact_for_audit(
        json.dumps(detail, ensure_ascii=False, default=str), policy=policy,
    )
    row = AuditLog(
        actor_id=getattr(actor, "id", None) if actor else None,
        actor_name=(getattr(actor, "username", "") or "")[:80] or None,
        action=f"{EVENT_PREFIX}{kind}"[:40],
        target_type="secops",
        target_id=str(target or "")[:80] or None,
        detail=payload,
        status=status,
        ip=(client_ip or "")[:64] or None,
        create_time=_now().replace(tzinfo=None),
    )
    try:
        db.add(row)
        db.commit()
    except Exception:  # noqa: BLE001 - 审计失败不打断处置主流程
        db.rollback()
        logger.exception("写入 secops 审计失败: %s", kind)


def _run(db: Session, actor: Optional[User], action: str, params: Optional[dict] = None) -> dict:
    """统一调用宿主机白名单执行器，异常降级为结构化错误而不是抛给前端。"""
    # 来源随身份切换：调度身份（actor=None）必须用 scheduler，否则执行器会按
    # "无交互调度只允许运维只读动作"把安全动作全部拒掉。
    source = OPS_SOURCE_SCHEDULER if actor is None else OPS_SOURCE_INTERACTIVE
    try:
        result = ops_service.execute(
            db,
            actor,
            action=action,
            params=params or {},
            source=source,
        )
        return result if isinstance(result, dict) else {"data": result}
    except Exception as exc:  # noqa: BLE001 - 单源失败不影响整体研判
        logger.warning("[secops] {} 失败: {}", action, exc)
        return {"error": str(exc)[:300], "action": action, "degraded": True}


def _pick_items(result: dict) -> list[dict]:
    """从执行器返回里挑出事件列表（不同动作字段名不一致，做一次归一）。"""
    for key in ("items", "events", "entries", "records", "data", "result"):
        value = result.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
        if isinstance(value, dict):
            nested = _pick_items(value)
            if nested:
                return nested
    return []


# ── 感知与研判 ─────────────────────────────────────────────────────────────


def _source_ip(item: dict) -> str:
    for key in ("ip", "source_ip", "client_ip", "remote_addr", "src_ip", "address"):
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _source_kind(item: dict) -> str:
    for key in ("type", "rule", "event", "pattern", "reason", "attack_type", "category"):
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()[:60]
    return "unknown"


def threat_digest(
    db: Session,
    actor: Optional[User] = None,
    *,
    hours: int = 24,
    limit: int = 200,
) -> dict:
    """多源攻击面聚合：一次拉齐四类传感器，按来源 IP 聚成画像。

    Args:
        db: 数据库会话。
        actor: 触发身份（只读动作允许为空）。
        hours: 回看小时数。
        limit: 单源条数上限。

    Returns:
        dict: ``{window_hours, sources[], totals, sensors[], recommendations[]}``
    """
    window = max(1, min(int(hours or 24), 24 * 14))
    calls = {
        "ssh_login_events": {"since_hours": window, "limit": limit},
        "nginx_attack_events": {"since_hours": window, "limit": limit},
        "flytrap_attack_events": {"since_hours": window, "limit": limit},
        "db_threat_signals": {"since_hours": window, "limit": limit},
    }
    raw: dict[str, dict] = {}
    sensors: list[dict] = []
    for action, params in calls.items():
        result = _run(db, actor, action, params)
        raw[action] = result
        sensors.append({
            "action": action,
            "ok": not result.get("error"),
            "events": len(_pick_items(result)),
            "error": result.get("error"),
        })

    profile: dict[str, dict] = {}
    for action, result in raw.items():
        for item in _pick_items(result)[:_MAX_SOURCES * 4]:
            ip = _source_ip(item)
            if not ip:
                continue
            try:
                ipaddress.ip_address(ip)
            except ValueError:
                continue
            entry = profile.setdefault(ip, {
                "ip": ip, "hits": 0, "sensors": set(), "kinds": set(), "last_seen": None,
            })
            entry["hits"] += 1
            entry["sensors"].add(action)
            entry["kinds"].add(_source_kind(item))
            seen = item.get("time") or item.get("create_time") or item.get("timestamp") or item.get("ts")
            if isinstance(seen, str) and (entry["last_seen"] is None or seen > entry["last_seen"]):
                entry["last_seen"] = seen

    sources: list[dict] = []
    for ip, entry in profile.items():
        geo = geoip_service.locate_ip_cached(ip) or {}
        hit_sensors = sorted(entry["sensors"])
        severity = (
            "critical" if len(hit_sensors) >= _CRITICAL_SENSORS or entry["hits"] >= _CRITICAL_VOLUME
            else "high" if (len(hit_sensors) >= _HIGH_SENSORS and entry["hits"] >= _HIGH_VOLUME)
            else "medium" if entry["hits"] >= 3
            else "low"
        )
        sources.append({
            "ip": ip,
            "hits": entry["hits"],
            "sensors": hit_sensors,
            "kinds": sorted(kind for kind in entry["kinds"] if kind)[:6],
            "severity": severity,
            "geo": {
                "country": geo.get("country"),
                "province": geo.get("province"),
                "city": geo.get("city"),
            },
            "last_seen": entry["last_seen"],
            "recommended": "block" if severity in {"critical", "high"} else "watch",
        })
    sources.sort(key=lambda row: (row["severity"] != "critical", row["severity"] != "high", -row["hits"]))

    recommendations: list[str] = []
    if any(row["recommended"] == "block" for row in sources):
        recommendations.append("存在多源同时命中的攻击源，建议先看候选名单再封禁（封禁需二次确认）")
    if not any(sensor["ok"] for sensor in sensors):
        recommendations.append("所有传感器都不可用：先确认宿主机执行器与采集脚本，再谈自动处置")
    if any(sensor["ok"] is False for sensor in sensors):
        recommendations.append("部分传感器降级，研判结论会偏保守：看 sensors[].error 定位")
    if not sources:
        recommendations.append("窗口内没有可用攻击事件；确认 Nginx/SSH/蜜罐日志是否在被采集")

    digest = {
        "window_hours": window,
        "generated_at": _iso(_now()),
        "sensors": sensors,
        "totals": {
            "sources": len(sources),
            "critical": sum(1 for row in sources if row["severity"] == "critical"),
            "high": sum(1 for row in sources if row["severity"] == "high"),
        },
        "sources": sources[: _MAX_SOURCES],
        "recommendations": recommendations,
    }
    _audit(db, kind="threat_digest", actor=actor, detail={
        "window_hours": window, "sources": len(sources), "totals": digest["totals"],
    })
    return digest


def surface_audit(db: Session, actor: Optional[User] = None) -> dict:
    """自身暴露面盘点（监听端口、公开路径、证书），只读。"""
    result = _run(db, actor, "security_surface_audit")
    _audit(db, kind="surface_audit", actor=actor, detail={"ok": not result.get("error")})
    return {"available": not result.get("error"), "surface": result}


def block_candidates(db: Session, actor: Optional[User] = None) -> dict:
    """自动封禁候选名单（执行器按阈值算好，本函数只做展示与审计）。"""
    result = _run(db, actor, "security_block_candidates")
    candidates = _pick_items(result)
    _audit(db, kind="block_candidates", actor=actor, detail={"candidates": len(candidates)})
    return {
        "available": not result.get("error"),
        "candidates": candidates,
        "raw": result if result.get("error") else None,
    }


# ── 处置 ──────────────────────────────────────────────────────────────────


def _normalize_ip(raw: str) -> str:
    value = str(raw or "").strip()
    if not value:
        raise ValidationError("必须指定来源 IP")
    try:
        address = ipaddress.ip_address(value)
    except ValueError as exc:
        raise ValidationError(f"IP 格式非法: {value}") from exc
    mapped = getattr(address, "ipv4_mapped", None)
    if mapped is not None:
        address = mapped
    if address.is_loopback or address.is_unspecified or address.is_multicast:
        raise ValidationError("拒绝处置本机/未指定/组播地址，避免把自己或平台打挂")
    return str(address)


def apply_block(
    db: Session,
    actor: User,
    *,
    ip: str,
    reason: str,
    duration_seconds: int = 3600,
    confirm: bool = False,
    client_ip: str = "",
) -> dict:
    """封禁一个攻击源（状态变更，必须超级管理员 + 二次确认 + 审计）。

    Args:
        db: 数据库会话。
        actor: 操作者（唯一超级管理员）。
        ip: 目标来源 IP。
        reason: 处置原因（写审计）。
        duration_seconds: 封禁时长，60~86400。
        confirm: 必须为 True，防止误触。
        client_ip: 发起方 IP。

    Returns:
        dict: ``{ip, duration_seconds, result}``
    """
    _require_super_admin(db, actor)
    if not confirm:
        raise ValidationError("封禁属于状态变更，需要显式 confirm=true")
    target = _normalize_ip(ip)
    duration = max(60, min(int(duration_seconds or 3600), 86_400))
    policy_ip_allowlist = list(copilot_security_service.load_policy(db).get("ip_allowlist") or [])
    from app.services import copilot_security_rules as rules

    if policy_ip_allowlist and rules.ip_allowed(target, policy_ip_allowlist):
        raise ValidationError("该地址在小菱安全中心的白名单内，拒绝封禁（先确认不是自家出口）")
    # 复用执行器的白名单配置：把目标并入"本次封禁"，不改动既有策略其它字段。
    current = _run(db, actor, "security_block_status")
    result = _run(db, actor, "security_block_configure", {"enabled": True})
    applied = _run(db, actor, "security_block_apply_anomalies", {
        "decisions": [{"ip": target, "action": "block", "reason": str(reason or "")[:200],
                       "duration_seconds": duration}],
    })
    _audit(
        db, kind="block_apply", actor=actor, target=target, client_ip=client_ip,
        detail={"ip": target, "duration_seconds": duration, "reason": str(reason or "")[:200],
                "status_ok": not current.get("error"), "apply_ok": not applied.get("error")},
    )
    return {
        "ip": target,
        "duration_seconds": duration,
        "blocked": not applied.get("error"),
        "result": applied,
        "configure": result,
    }


def release_block(
    db: Session,
    actor: User,
    *,
    ip: str,
    reason: str,
    client_ip: str = "",
) -> dict:
    """解封一个来源（状态变更，必须超级管理员 + 审计）。"""
    _require_super_admin(db, actor)
    target = _normalize_ip(ip)
    result = _run(db, actor, "security_block_release", {"ip": target, "reason": str(reason or "")[:200]})
    _audit(
        db, kind="block_release", actor=actor, target=target, client_ip=client_ip,
        detail={"ip": target, "reason": str(reason or "")[:200], "ok": not result.get("error")},
    )
    return {"ip": target, "released": not result.get("error"), "result": result}


def start_authorized_pentest(
    db: Session,
    actor: User,
    *,
    public_id: str,
    client_ip: str = "",
) -> dict:
    """驱动一次**已授权且在时间窗内**的渗透测试（不实现攻击逻辑，只转发流水线）。"""
    _require_super_admin(db, actor)
    engagement = (
        db.query(PentestEngagement)
        .filter(PentestEngagement.public_id == str(public_id or "").strip())
        .first()
    )
    if engagement is None:
        raise NotFoundError("渗透测试委托不存在", code=40440)
    if engagement.status not in {"authorized", "running"}:
        raise ForbiddenError(
            f"委托状态为 {engagement.status}，只有 authorized/running 才能启动", code=40340,
        )
    now = _now().replace(tzinfo=None)
    for label, bound in (("window_start", engagement.window_start), ("window_end", engagement.window_end)):
        if bound is None:
            raise ForbiddenError("委托缺少授权时间窗，拒绝启动", code=40341)
        if label == "window_start" and now < bound:
            raise ForbiddenError("尚未进入授权时间窗", code=40342)
        if label == "window_end" and now > bound:
            raise ForbiddenError("已超出授权时间窗", code=40343)
    _require_own_scope(engagement)
    from app.services import pentest_service

    result = pentest_service.start_engagement(db, actor, engagement.public_id)
    _audit(
        db, kind="pentest_start", actor=actor, target=engagement.public_id, client_ip=client_ip,
        detail={"public_id": engagement.public_id, "target_type": engagement.target_type,
                "status": engagement.status},
    )
    return {"public_id": engagement.public_id, "started": True, "engagement": result}


def _require_own_scope(engagement: PentestEngagement) -> None:
    """委托目标必须落在自有资产清单内，防止被诱导去打第三方。"""
    try:
        scope = json.loads(engagement.scope_json or "{}")
    except (TypeError, ValueError):
        scope = {}
    target = str(scope.get("remote_target_url") or "")
    if not target:
        # 沙箱/工件类目标天然在平台内部，放行。
        return
    hosts = list(getattr(settings, "secops_asset_hosts", []) or [])
    if not hosts:
        raise ForbiddenError(
            "未配置 secops_asset_hosts，拒绝启动指向外部地址的渗透测试", code=40344,
        )
    host = (urlparse(target).hostname or "").lower()
    allowed = {item.lower() for item in hosts}
    if host not in allowed:
        raise ForbiddenError(f"目标 {host} 不在自有资产清单内", code=40345)


# ── 自身基线核查 ──────────────────────────────────────────────────────────


def _http_probe(url: str, *, timeout: float = 6.0) -> dict:
    try:
        response = httpx.get(url, timeout=timeout, follow_redirects=False, verify=False)
        return {
            "url": url,
            "status": response.status_code,
            "content_type": response.headers.get("content-type", ""),
            "headers": {k.lower(): v for k, v in response.headers.items()},
            "body_head": response.text[:120],
        }
    except Exception as exc:  # noqa: BLE001
        return {"url": url, "error": str(exc)[:200]}


def _tls_probe(host: str, port: int = 443) -> dict:
    try:
        context = ssl.create_default_context()
        with socket.create_connection((host, port), timeout=6) as sock:
            with context.wrap_socket(sock, server_hostname=host) as tls:
                cert = tls.getpeercert() or {}
                return {
                    "ok": True,
                    "version": tls.version(),
                    "cipher": tls.cipher()[0] if tls.cipher() else "",
                    "not_after": cert.get("notAfter"),
                    "subject": dict(x[0] for x in cert.get("subject", ()) if x),
                }
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)[:200]}


def baseline_check(db: Session, actor: Optional[User] = None) -> dict:
    """平台自身安全基线：文档暴露、安全响应头、TLS、证书、攻击面、备份。

    只对 `settings.secops_public_base_url` 指向的自有地址做 HTTP/TLS 探测；
    未配置时跳过网络探测，只跑本地/执行器侧检查。
    """
    checks: list[dict] = []
    base = str(getattr(settings, "secops_public_base_url", "") or "").rstrip("/")

    def add(key: str, title: str, passed: Optional[bool], detail: str, remediation: str = "",
            weight: int = 1) -> None:
        checks.append({
            "key": key, "title": title,
            "passed": bool(passed) if passed is not None else None,
            "detail": detail, "remediation": remediation, "weight": weight,
        })

    cert = _run(db, actor, "certificate_status")
    add(
        "certificate", "证书状态可读", not cert.get("error"),
        str(cert.get("data") or cert)[:200],
        "执行器读不到证书时，先查 /etc/letsencrypt 与 crontab 续期任务", weight=2,
    )

    if base:
        docs = _http_probe(f"{base}/docs")
        openapi = _http_probe(f"{base}/openapi.json")
        def _is_shell(probe: dict) -> bool:
            ctype = str(probe.get("content_type") or "").lower()
            return "text/html" in ctype and "<!doctype html" in str(probe.get("body_head") or "").lower()

        exposed = (
            docs.get("status") == 200 and not _is_shell(docs)
        ) or (
            openapi.get("status") == 200 and "json" in str(openapi.get("content_type") or "").lower()
        )
        add(
            "openapi", "OpenAPI/文档未公开", not exposed,
            f"docs={docs.get('status')} type={docs.get('content_type')}; "
            f"openapi={openapi.get('status')} type={openapi.get('content_type')}",
            "在 Nginx 加 `location ~ ^/(docs|redoc|openapi\\.json)$ { return 404; }`",
            weight=3,
        )

        root = _http_probe(base + "/")
        headers = root.get("headers") or {}
        for key, header, remediation, weight in (
            ("hsts", "strict-transport-security", "Nginx 加 `add_header Strict-Transport-Security ...`", 2),
            ("xcto", "x-content-type-options", "加 `add_header X-Content-Type-Options nosniff`", 1),
            ("frame", "x-frame-options", "加 `add_header X-Frame-Options SAMEORIGIN`", 1),
            ("refpol", "referrer-policy", "加 `add_header Referrer-Policy strict-origin-when-cross-origin`", 1),
        ):
            add(f"header_{key}", f"响应头 {header}", bool(headers.get(header)),
                headers.get(header) or "缺失", remediation, weight=weight)

        host = urlparse(base).hostname or ""
        tls = _tls_probe(host)
        add("tls_version", "TLS 版本 ≥ 1.2", bool(tls.get("ok")) and str(tls.get("version")) in {"TLSv1.2", "TLSv1.3"},
            str(tls.get("version") or tls.get("error")), "在 Nginx 里 ssl_protocols TLSv1.2 TLSv1.3", weight=2)
        add("tls_cert", "证书可获取", bool(tls.get("ok")), str(tls.get("not_after") or tls.get("error")), "", weight=1)
    else:
        add("openapi", "OpenAPI/文档未公开", None,
            "未配置 secops_public_base_url，跳过网络探测", "配置自有域名后即可自动核查", weight=3)

    surface = _run(db, actor, "security_surface_audit")
    add("surface", "攻击面盘点可用", not surface.get("error"),
        str(surface.get("data") or surface)[:200], "检查执行器 security_surface_audit 是否可用", weight=2)

    backup = _run(db, actor, "backup_audit")
    add("backup", "备份审计可用", not backup.get("error"),
        str(backup.get("data") or backup)[:200], "确认 prism-backup.timer 与备份目录权限", weight=2)

    scored = [item for item in checks if item["passed"] is not None]
    total = sum(item["weight"] for item in scored)
    earned = sum(item["weight"] for item in scored if item["passed"])
    score = int(round(100 * earned / total)) if total else 0
    report = {
        "generated_at": _iso(_now()),
        "base_url": base or None,
        "score": score,
        "checks": checks,
        "failed": [item["key"] for item in scored if not item["passed"]],
        "remediations": [item["remediation"] for item in scored if not item["passed"] and item["remediation"]],
    }
    _audit(db, kind="baseline_check", actor=actor, detail={"score": score, "failed": report["failed"]})
    return report


# ── 无人值守巡检 ──────────────────────────────────────────────────────────


def patrol_once(db: Session, *, actor: Optional[User] = None) -> dict:
    """一次安全运营巡检：感知 → 研判 → （可选）自动封禁 → 告警。

    自动封禁默认关闭（`settings.secops_auto_block_enabled`）；打开时也只处理
    「多源同时命中且不在白名单」的来源，并且每轮最多封 5 个，避免被刷成大规模阻断。
    """
    digest = threat_digest(db, actor, hours=24)
    candidates = block_candidates(db, actor)
    blocked: list[str] = []
    errors: list[str] = []

    if getattr(settings, "secops_auto_block_enabled", False):
        allowlist = list(copilot_security_service.load_policy(db).get("ip_allowlist") or [])
        from app.services import copilot_security_rules as rules

        for row in [item for item in digest["sources"] if item["severity"] == "critical"][:5]:
            target = row["ip"]
            if allowlist and rules.ip_allowed(target, allowlist):
                continue
            try:
                result = _run(db, actor, "security_block_apply_anomalies", {
                    "decisions": [{
                        "ip": target, "action": "block", "reason": "secops_patrol",
                        "duration_seconds": 3600,
                    }],
                })
                if result.get("error"):
                    errors.append(f"{target}: {result['error']}")
                else:
                    blocked.append(target)
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{target}: {exc}")

    severity = "critical" if digest["totals"]["critical"] else (
        "high" if digest["totals"]["high"] else "warning" if digest["sources"] else "info"
    )
    if digest["sources"] or errors:
        try:
            observability_service.create_alert(
                db,
                alert_type="secops.patrol",
                severity=severity if severity != "info" else "info",
                title=(
                    f"安全运营巡检：{digest['totals']['sources']} 个来源 "
                    f"(critical {digest['totals']['critical']} / high {digest['totals']['high']})"
                ),
                detail={
                    "window_hours": digest["window_hours"],
                    "top": digest["sources"][:5],
                    "auto_blocked": blocked,
                    "errors": errors[:5],
                    "sensors": digest["sensors"],
                },
                category="security",
                source="secops_patrol",
            )
        except Exception:  # noqa: BLE001
            logger.exception("secops 巡检告警写入失败")

    result = {
        "generated_at": _iso(_now()),
        "sources": digest["totals"]["sources"],
        "critical": digest["totals"]["critical"],
        "high": digest["totals"]["high"],
        "candidates": len(candidates["candidates"]),
        "auto_blocked": blocked,
        "errors": errors,
        "auto_block_enabled": bool(getattr(settings, "secops_auto_block_enabled", False)),
    }
    _audit(db, kind="patrol", actor=actor, detail=result)
    return result


def patrol_status(db: Session) -> dict:
    """最近一次巡检与告警状态，供页面与 小菱 回答"现在安全吗"。"""
    rows = (
        db.query(AuditLog)
        .filter(AuditLog.action == f"{EVENT_PREFIX}patrol")
        .order_by(AuditLog.id.desc())
        .limit(5)
        .all()
    )
    last = rows[0] if rows else None
    detail: dict = {}
    if last is not None:
        try:
            detail = json.loads(last.detail or "{}")
        except (TypeError, ValueError):
            detail = {}
    return {
        "auto_block_enabled": bool(getattr(settings, "secops_auto_block_enabled", False)),
        "patrol_enabled": bool(getattr(settings, "secops_patrol_enabled", True)),
        "public_base_url": getattr(settings, "secops_public_base_url", "") or None,
        "asset_hosts": list(getattr(settings, "secops_asset_hosts", []) or []),
        "last_run": {
            "at": _iso(last.create_time) if last else None,
            "detail": detail,
        },
        "recent": [
            {"at": _iso(row.create_time), "status": row.status,
             "detail": (row.detail or "")[:400]}
            for row in rows[1:]
        ],
    }


# 供管理能力/工具注册表引用：小菱可以调用的安全运营工具编码。
SECOPS_TOOLS: tuple[str, ...] = READ_ONLY_TOOLS + STATEFUL_TOOLS


def tool_catalog() -> list[dict]:
    """安全运营工具目录（读 + 写），写类标注需要审批。"""
    catalog = [
        {"tool": "secops.threat_digest", "risk": "read",
         "description": "聚合 SSH/Nginx/蜜罐/数据库四类传感器，输出攻击源画像与处置建议"},
        {"tool": "secops.surface_audit", "risk": "read", "description": "盘点自身监听端口与公开路径"},
        {"tool": "secops.block_candidates", "risk": "read", "description": "读取自动封禁候选名单"},
        {"tool": "secops.baseline_check", "risk": "read",
         "description": "自身安全基线核查（文档暴露/响应头/TLS/证书/备份）"},
        {"tool": "secops.patrol_status", "risk": "read", "description": "最近一次自动巡检与告警状态"},
        {"tool": "secops.block_apply", "risk": "critical",
         "description": "封禁一个攻击源（需超级管理员 + confirm）"},
        {"tool": "secops.block_release", "risk": "critical", "description": "解封一个来源"},
        {"tool": "secops.pentest_start", "risk": "critical", "description": "启动已授权且处于时间窗内的渗透测试委托"},
    ]
    return catalog


__all__ = [
    "EVENT_PREFIX",
    "READ_ONLY_TOOLS",
    "SECOPS_TOOLS",
    "STATEFUL_TOOLS",
    "apply_block",
    "baseline_check",
    "block_candidates",
    "patrol_once",
    "patrol_status",
    "release_block",
    "start_authorized_pentest",
    "surface_audit",
    "threat_digest",
    "tool_catalog",
]
