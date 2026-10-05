"""来源溯源、自我防御面审计与流量元数据摘要的受控读取。

三类数据全部来自宿主机 root 执行器的只读动作：
``security_ip_trace`` / ``security_surface_audit`` / ``security_traffic_summary``。

边界（与既有自动封禁一致）：
1. 只读取本机可信日志与内核状态，不向任何目标发起扫描、连接或探测；
2. 溯源只接受单个 IP，不接受命令、端口、路径或网段；
3. 风险评分只使用本机可核验证据，出网归因结果单独分栏且不参与评分；
4. 结果交给界面展示，不写回执时不伪装成功。
"""

from __future__ import annotations

import ipaddress
import uuid
from typing import Any

from sqlalchemy.orm import Session

from app.models.user import User
from app.services import ops_service, security_response_service

# 溯源出网归因允许出现的字段；其余字段不回显，避免情报源注入无关内容。
_ATTRIBUTION_FIELDS = ("country", "region", "city", "isp", "org", "as")
# 执行器返回体中允许继续展示的记录上限。
_MAX_LIST = 200


def _text(value: Any, limit: int = 200) -> str:
    return str(ops_service._redact_value(str(value or "")))[:limit]


def _int(value: Any, *, default: int = 0, upper: int = 1_000_000) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(0, min(number, upper))


def _ip_or_none(value: Any) -> str | None:
    try:
        return str(ipaddress.ip_address(str(value or "").strip()))
    except ValueError:
        return None


def _error_entries(values: Any) -> list[str]:
    if not isinstance(values, list):
        return []
    return [_text(item) for item in values[:10] if str(item or "").strip()]


def unavailable(reason: str, *, kind: str) -> dict[str, Any]:
    """没有 root 核验回执时返回明确的不可用状态，不编造归因结果。"""
    return {"available": False, "verified": False, "kind": kind, "errors": [_text(reason)]}


def _payload(execution: dict[str, Any], *, kind: str) -> dict[str, Any]:
    """校验执行回执；只有 status=success 且 ok=true 才认为数据可用。

    不可用时返回带 ``available=False`` 的说明快照，由调用方原样交给界面，
    不使用默认值冒充已经取得证据。
    """
    wrapper = execution.get("result")
    if execution.get("status") != "success" or not isinstance(wrapper, dict) or wrapper.get("ok") is not True:
        reason = execution.get("error") or "宿主机溯源结果尚未确认，请先刷新状态"
        result = unavailable(reason, kind=kind)
        result["outcome_unknown"] = execution.get("status") == "running"
        return result
    payload = wrapper.get("result")
    if not isinstance(payload, dict):
        return unavailable("宿主机溯源回执不完整", kind=kind)
    payload = dict(payload)
    payload.update({"available": True, "verified": True, "kind": kind})
    return payload


def _execute(
    db: Session, actor: User | None, action: str, params: dict[str, Any], *, kind: str, source: str,
) -> dict[str, Any]:
    try:
        execution = ops_service.execute(
            db, actor, action=action, params=params, source=source, request_id=uuid.uuid4().hex,
        )
    except Exception as exc:  # noqa: BLE001 - 读取失败不能伪称已拿到证据
        return unavailable(str(exc), kind=kind)
    payload = _payload(execution, kind=kind)
    if not isinstance(payload, dict):
        return unavailable("宿主机溯源结果不可解析", kind=kind)
    payload["request_id"] = _text(execution.get("request_id"), 64) or None
    return payload


def _normalize_attribution(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or value.get("ok") is not True:
        return {"ok": False, "note": _text((value or {}).get("note") if isinstance(value, dict) else "", 160)}
    raw = value.get("attribution")
    fields = {key: _text((raw or {}).get(key), 96) for key in _ATTRIBUTION_FIELDS} if isinstance(raw, dict) else {}
    return {"ok": True, **fields}


def _normalize_records(values: Any) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for row in values[:_MAX_LIST] if isinstance(values, list) else []:
        if not isinstance(row, dict):
            continue
        ip = _ip_or_none(row.get("ip"))
        rows.append({
            "id": _text(row.get("id"), 120),
            "ip": ip or "",
            "rule": _text(row.get("rule"), 80),
            "source": _text(row.get("source"), 40) or "deterministic_rule",
            "status": _text(row.get("status"), 20) or "unknown",
            "started_at": _text(row.get("started_at"), 64) or None,
            "expires_at": _text(row.get("expires_at"), 64) or None,
            "released_at": _text(row.get("released_at"), 64) or None,
            "evidence_count": _int(row.get("evidence_count"), upper=100_000),
            "reason": _text(row.get("reason"), 200),
        })
    return rows


def _normalize_accounts(values: Any) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for row in values[:20] if isinstance(values, list) else []:
        if not isinstance(row, dict):
            continue
        rows.append({"account": _text(row.get("account"), 64), "count": _int(row.get("count"), upper=100_000)})
    return rows


def _normalize_targets(values: Any) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for row in values[:20] if isinstance(values, list) else []:
        if not isinstance(row, dict):
            continue
        rows.append({"path": _text(row.get("path"), 160), "count": _int(row.get("count"), upper=100_000)})
    return rows


def _normalize_counts(value: Any, *, limit: int = 20) -> dict[str, int]:
    if not isinstance(value, dict):
        return {}
    pairs = [(str(key)[:48], _int(count, upper=100_000)) for key, count in value.items()]
    pairs.sort(key=lambda pair: pair[1], reverse=True)
    return dict(pairs[:limit])


def trace_ip(db: Session, actor: User, *, ip: str) -> dict[str, Any]:
    """最高管理员发起的单来源被动溯源。"""
    params = ops_service.validate_action_params("security_ip_trace", {"ip": ip})
    payload = _execute(db, actor, "security_ip_trace", params, kind="ip_trace", source="security_center")
    if not payload.get("verified"):
        return payload
    raw_risk = payload.get("risk") if isinstance(payload.get("risk"), dict) else {}
    raw_ssh = payload.get("ssh") if isinstance(payload.get("ssh"), dict) else {}
    raw_web = payload.get("web") if isinstance(payload.get("web"), dict) else {}
    whois = payload.get("whois") if isinstance(payload.get("whois"), dict) else {}
    dns = payload.get("reverse_dns") if isinstance(payload.get("reverse_dns"), dict) else {}
    return {
        "available": True,
        "verified": True,
        "kind": "ip_trace",
        "request_id": payload.get("request_id"),
        "ip": _ip_or_none(payload.get("ip")) or "",
        "generated_at": _text(payload.get("generated_at"), 64) or None,
        "window_hours": _int(payload.get("window_hours"), default=24, upper=168),
        "is_public": bool(payload.get("is_public")),
        "is_protected": bool(payload.get("is_protected")),
        "risk": {
            "score": min(100, _int(raw_risk.get("score"), upper=100)),
            "level": _text(raw_risk.get("level"), 16) or "low",
            "reasons": [_text(item, 240) for item in (raw_risk.get("reasons") or [])[:12]
                        if str(item or "").strip()],
            "basis": _text(raw_risk.get("basis"), 240),
        },
        "ssh": {
            "count": _int(raw_ssh.get("count"), upper=1_000_000),
            "accounts_tried": _normalize_accounts(raw_ssh.get("accounts_tried")),
            "first_seen": _text(raw_ssh.get("first_seen"), 64) or None,
            "last_seen": _text(raw_ssh.get("last_seen"), 64) or None,
        },
        "web": {
            "count": _int(raw_web.get("count"), upper=1_000_000),
            "target_count": _int(raw_web.get("target_count"), upper=1_000),
            "targets": _normalize_targets(raw_web.get("targets")),
            "methods": _normalize_counts(raw_web.get("methods"), limit=8),
            "status_codes": _normalize_counts(raw_web.get("status_codes"), limit=8),
            "first_seen": _text(raw_web.get("first_seen"), 64) or None,
            "last_seen": _text(raw_web.get("last_seen"), 64) or None,
        },
        "defense_records": _normalize_records(payload.get("defense_records")),
        "attribution": _normalize_attribution(payload.get("attribution")),
        "reverse_dns": {"ok": bool(dns.get("ok")), "output": _text(dns.get("output"), 240),
                        "note": _text(dns.get("note"), 200)},
        "whois": {"ok": bool(whois.get("ok")), "summary": _text(whois.get("summary"), 480),
                  "note": _text(whois.get("note"), 200)},
        "evidence_sources": [_text(item, 120) for item in (payload.get("evidence_sources") or [])[:6]],
        "errors": _error_entries(payload.get("errors")),
    }


def surface_audit(db: Session, actor: User) -> dict[str, Any]:
    """本机防御面只读审计：监听面、防火墙链、可用加固应用。"""
    payload = _execute(db, actor, "security_surface_audit", {}, kind="surface_audit", source="security_center")
    if not payload.get("verified"):
        return payload
    listeners: list[dict[str, Any]] = []
    for row in (payload.get("listeners") or [])[:_MAX_LIST]:
        if not isinstance(row, dict):
            continue
        port = _text(row.get("port"), 8)
        if not port.isdigit():
            continue
        listeners.append({
            "protocol": _text(row.get("protocol"), 16),
            "address": _text(row.get("address"), 64) or "*",
            "port": int(port),
            "process": _text(row.get("process"), 160),
        })
    firewall: dict[str, Any] = {}
    raw_firewall = payload.get("firewall") if isinstance(payload.get("firewall"), dict) else {}
    for family in ("ipv4", "ipv6"):
        raw = raw_firewall.get(family) if isinstance(raw_firewall.get(family), dict) else {}
        chains: list[dict[str, Any]] = []
        for row in (raw.get("chains") or [])[:4]:
            if not isinstance(row, dict):
                continue
            chains.append({
                "chain": _text(row.get("chain"), 32),
                "ok": bool(row.get("ok")),
                "policy": _text(row.get("policy"), 64),
                "rules": [_text(item, 240) for item in (row.get("rules") or [])[:400]],
                "note": _text(row.get("note"), 160),
            })
        firewall[family] = {
            "tool": _text(raw.get("tool"), 32),
            "chains": chains,
            "tools_present": [_text(item, 32) for item in (raw.get("tools_present") or [])[:16]],
        }
    applications = []
    for row in (payload.get("applications") or [])[:16]:
        if not isinstance(row, dict):
            continue
        applications.append({
            "name": _text(row.get("name"), 48),
            "purpose": _text(row.get("purpose"), 120),
            "installed": bool(row.get("installed")),
        })
    blocking = payload.get("blocking") if isinstance(payload.get("blocking"), dict) else {}
    return {
        "available": True,
        "verified": True,
        "kind": "surface_audit",
        "request_id": payload.get("request_id"),
        "generated_at": _text(payload.get("generated_at"), 64) or None,
        "listeners": listeners,
        "public_listener_count": len([row for row in listeners if row["address"] in {"0.0.0.0", "::", "*"}]),
        "firewall": firewall,
        "applications": applications,
        "ipset": {
            "present": bool((payload.get("ipset") or {}).get("present")),
            "sets": [_text(item, 48) for item in ((payload.get("ipset") or {}).get("sets") or [])[:4]],
        },
        "blocking": {
            "enabled": bool(blocking.get("enabled")),
            "backend": _text(blocking.get("backend"), 24),
            "active_leases": _int(blocking.get("active_leases"), upper=10_000),
        },
        "ssh_ports": _text(payload.get("ssh_ports"), 64),
        "errors": _error_entries(payload.get("errors")),
    }


def traffic_summary(db: Session, actor: User, *, since_hours: int = 24) -> dict[str, Any]:
    """流量元数据摘要：对端 IP、端口、协议、状态与日志计数，不含载荷。"""
    params = ops_service.validate_action_params("security_traffic_summary", {"since_hours": since_hours})
    payload = _execute(db, actor, "security_traffic_summary", params, kind="traffic_summary", source="security_center")
    if not payload.get("verified"):
        return payload
    peers = []
    for row in (payload.get("peers") or [])[:_MAX_LIST]:
        if not isinstance(row, dict):
            continue
        ip = _ip_or_none(row.get("ip"))
        if ip is None:
            continue
        peers.append({
            "ip": ip,
            "connections": _int(row.get("connections"), upper=100_000),
            "protocols": _normalize_counts(row.get("protocols"), limit=6),
            "peer_ports": _normalize_counts(row.get("peer_ports"), limit=8),
            "processes": [_text(item, 120) for item in (row.get("processes") or [])[:5]],
            "states": _normalize_counts(row.get("states"), limit=6),
            "ssh_failed_count": _int(row.get("ssh_failed_count"), upper=1_000_000),
            "sensitive_probe_count": _int(row.get("sensitive_probe_count"), upper=1_000_000),
            "target_count": _int(row.get("target_count"), upper=1_000),
            "last_seen": _text(row.get("last_seen"), 64) or None,
        })
    return {
        "available": True,
        "verified": True,
        "kind": "traffic_summary",
        "request_id": payload.get("request_id"),
        "generated_at": _text(payload.get("generated_at"), 64) or None,
        "window_hours": _int(payload.get("window_hours"), default=since_hours, upper=168),
        "peers": peers,
        "peer_total": _int(payload.get("peer_total"), upper=1_000_000),
        "current_connections": _int(payload.get("current_connections"), upper=100_000),
        "recent_ssh_failed_sources": _int(payload.get("recent_ssh_failed_sources"), upper=100_000),
        "recent_probe_sources": _int(payload.get("recent_probe_sources"), upper=100_000),
        "payload_captured": False,
        "note": _text(payload.get("note"), 240),
        "errors": _error_entries(payload.get("errors")),
    }


def decoy_status(db: Session, actor: User) -> dict[str, Any]:
    """诱捕层状态与命中来源（只读）：容器存活、命中汇总、引流集合与链序。"""
    payload = _execute(db, actor, "security_decoy_status", {}, kind="decoy_status", source="security_center")
    if not payload.get("verified"):
        return payload
    sources: list[dict[str, Any]] = []
    for row in (payload.get("sources") or [])[:100]:
        if not isinstance(row, dict):
            continue
        ip = _ip_or_none(row.get("ip"))
        if ip is None:
            continue
        paths = []
        for item in (row.get("paths") or [])[:10]:
            if not isinstance(item, dict):
                continue
            paths.append({"path": _text(item.get("path"), 160), "count": _int(item.get("count"), upper=100_000)})
        agents = []
        for item in (row.get("user_agents") or [])[:5]:
            if not isinstance(item, dict):
                continue
            agents.append({"user_agent": _text(item.get("user_agent"), 200),
                           "count": _int(item.get("count"), upper=100_000)})
        sources.append({
            "ip": ip,
            "count": _int(row.get("count"), upper=1_000_000),
            "path_count": _int(row.get("path_count"), upper=1_000),
            "paths": paths,
            "user_agents": agents,
            "first_seen": _text(row.get("first_seen"), 64) or None,
            "last_seen": _text(row.get("last_seen"), 64) or None,
        })
    members: dict[str, list[str]] = {}
    raw_members = payload.get("redirect_members") if isinstance(payload.get("redirect_members"), dict) else {}
    for family in ("4", "6"):
        rows = []
        for value in (raw_members.get(family) or [])[:500]:
            address = _ip_or_none(value)
            if address is not None:
                rows.append(address)
        members[family] = rows
    return {
        "available": True,
        "verified": True,
        "kind": "decoy_status",
        "request_id": payload.get("request_id"),
        "generated_at": _text(payload.get("generated_at"), 64) or None,
        "container_running": bool(payload.get("container_running")),
        "hit_total": _int(payload.get("hit_total"), upper=1_000_000),
        "source_total": _int(payload.get("source_total"), upper=100_000),
        "sources": sources,
        "redirect_total": _int(payload.get("redirect_total"), upper=10_000),
        "redirect_members": members,
        "chain_order_ok": bool(payload.get("chain_order_ok")),
        "redirect_chain": _text(payload.get("redirect_chain"), 64),
        "redirect_port": _int(payload.get("redirect_port"), upper=65_535),
        "log_path": _text(payload.get("log_path"), 160),
        "errors": _error_entries(payload.get("errors")),
    }


def decoy_apply(db: Session, actor: User, *, reason: str) -> dict[str, Any]:
    """把命中诱饵的来源加入引流集合；原因必填并进入操作审计。"""
    params = ops_service.validate_action_params("security_decoy_apply", {"reason": reason})
    payload = _execute(db, actor, "security_decoy_apply", params, kind="decoy_apply", source="security_center")
    if not payload.get("verified"):
        return payload
    applied = []
    for row in (payload.get("applied") or [])[:200]:
        if not isinstance(row, dict):
            continue
        ip = _ip_or_none(row.get("ip"))
        if ip is None:
            continue
        applied.append({
            "ip": ip,
            "hits": _int(row.get("hits"), upper=1_000_000),
            "paths": _int(row.get("paths"), upper=1_000),
            "lease_seconds": _int(row.get("lease_seconds"), upper=86_400),
        })
    skipped = []
    for row in (payload.get("skipped") or [])[:200]:
        if not isinstance(row, dict):
            continue
        skipped.append({"ip": _text(row.get("ip"), 64), "reason": _text(row.get("reason"), 120)})
    return {
        "available": True,
        "verified": True,
        "kind": "decoy_apply",
        "request_id": payload.get("request_id"),
        "generated_at": _text(payload.get("generated_at"), 64) or None,
        "applied": applied,
        "skipped": skipped,
        "hit_sources": _int(payload.get("hit_sources"), upper=100_000),
        "chain_order_ok": bool(payload.get("chain_order_ok")),
        "errors": _error_entries(payload.get("errors")),
    }


def cached_blocking(db: Session) -> dict[str, Any]:
    """复用自动封禁的已审计回执，供溯源面板显示处置闭环状态。"""
    return security_response_service.cached_status(db)
