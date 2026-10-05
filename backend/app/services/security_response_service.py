"""固定自动防御与安全中心之间的受控接口。

候选来源、证据和内核租约全部由宿主机执行器核验。本服务不接收封禁
目标，也不使用模型输出或普通 HTTP 权限拒绝记录决定封禁。
"""

from __future__ import annotations

import ipaddress
import json
import uuid
from typing import Any

from sqlalchemy.orm import Session

from app.models.admin_chat import OpsExecution
from app.models.user import User
from app.schemas.security_center import AutomaticBlockingPolicyIn
from app.services import ops_service

_POLICY_KEYS = (
    "enabled", "ai_anomaly_enabled", "duration_seconds", "window_seconds", "ssh_threshold", "web_threshold",
    "allowlist_cidrs",
)
_ENTRY_STATUSES = {"active", "expired", "released", "failed", "unknown"}


def _safe_text(value: Any, limit: int = 500) -> str:
    return str(ops_service._redact_value(str(value or "")))[:limit]


def unavailable(reason: str, *, request_id: str = "", outcome_unknown: bool = False) -> dict[str, Any]:
    """没有 root 核验回执时不显示已封禁或已启用。"""
    return {
        "available": False,
        "verified": False,
        "enabled": False,
        "policy": {
            "enabled": False, "ai_anomaly_enabled": False, "duration_seconds": 900, "window_seconds": 300,
            "ssh_threshold": 20, "web_threshold": 30, "allowlist_cidrs": [], "activated_at": None,
        },
        "protected_sources": [],
        "active_blocks": [],
        "recent_blocks": [],
        "last_evaluated_at": None,
        "errors": [_safe_text(reason)],
        "backend": "ipset",
        "request_id": _safe_text(request_id, 64) or None,
        "outcome_unknown": outcome_unknown,
    }


def _entries(values: Any, *, active_only: bool = False) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    if not isinstance(values, list):
        return result
    for row in values[:500]:
        if not isinstance(row, dict):
            continue
        try:
            ip = str(ipaddress.ip_address(str(row.get("ip") or "")))
            count = max(0, int(row.get("evidence_count") or 0))
        except (TypeError, ValueError):
            continue
        status = str(row.get("status") or "unknown")
        if status not in _ENTRY_STATUSES:
            status = "unknown"
        if active_only and status != "active":
            continue
        result.append({
            "id": _safe_text(row.get("id"), 120),
            "ip": ip,
            "rule": _safe_text(row.get("rule"), 80),
            "evidence_count": count,
            "scope": _safe_text(row.get("scope"), 80),
            "source": str(row.get("source") or "deterministic_rule")
            if row.get("source") in {"xiaoling_anomaly", "deterministic_rule"} else "unknown",
            "status": status,
            "started_at": _safe_text(row.get("started_at"), 64) or None,
            "expires_at": _safe_text(row.get("expires_at"), 64) or None,
            "released_at": _safe_text(row.get("released_at"), 64) or None,
            "reason": _safe_text(row.get("reason"), 200),
        })
    return result


def _verified_snapshot(execution: dict[str, Any]) -> dict[str, Any]:
    wrapper = execution.get("result")
    payload = wrapper.get("result") if isinstance(wrapper, dict) else None
    if execution.get("status") != "success" or not isinstance(wrapper, dict) or wrapper.get("ok") is not True:
        return unavailable(
            execution.get("error") or "自动封禁执行结果尚未确认，请先刷新宿主机状态",
            request_id=str(execution.get("request_id") or ""),
            outcome_unknown=execution.get("status") == "running",
        )
    if not isinstance(payload, dict) or payload.get("verified") is not True or payload.get("available") is not True:
        errors = payload.get("errors") if isinstance(payload, dict) else None
        return unavailable(
            "; ".join(_safe_text(item) for item in errors[:10])
            if isinstance(errors, list) and errors else "宿主机自动封禁状态缺少有效核验回执",
            request_id=str(execution.get("request_id") or ""),
        )
    policy_raw = payload.get("policy")
    if not isinstance(policy_raw, dict):
        return unavailable("宿主机自动封禁策略回执不完整")
    try:
        policy = AutomaticBlockingPolicyIn.model_validate(
            {
                key: policy_raw.get(key, False) if key == "ai_anomaly_enabled" else policy_raw[key]
                for key in _POLICY_KEYS
            }
        ).model_dump()
    except (ValueError, TypeError, KeyError):
        return unavailable("宿主机自动封禁策略回执不合法")
    if payload.get("enabled") is not policy["enabled"] or payload.get("backend") != "ipset":
        return unavailable("宿主机自动封禁状态与策略回执不一致")
    protected_sources: list[dict[str, str]] = []
    sources = payload.get("protected_sources")
    for row in sources[:64] if isinstance(sources, list) else []:
        if not isinstance(row, dict):
            continue
        try:
            network = str(ipaddress.ip_network(str(row.get("cidr") or ""), strict=False))
        except ValueError:
            continue
        protected_sources.append({"cidr": network, "reason": _safe_text(row.get("reason"), 200)})
    errors = payload.get("errors")
    return {
        "available": True,
        "verified": True,
        "enabled": policy["enabled"],
        "policy": {**policy, "activated_at": _safe_text(policy_raw.get("activated_at"), 64) or None},
        "protected_sources": protected_sources,
        "active_blocks": _entries(payload.get("active_blocks"), active_only=True),
        "recent_blocks": _entries(payload.get("recent_blocks")),
        "last_evaluated_at": _safe_text(payload.get("last_evaluated_at"), 64) or None,
        "errors": [_safe_text(item) for item in errors[:20]] if isinstance(errors, list) else [],
        "backend": "ipset",
        "family_support": {
            "ipv4": isinstance(payload.get("family_support"), dict)
            and payload["family_support"].get("ipv4") is True,
            "ipv6": isinstance(payload.get("family_support"), dict)
            and payload["family_support"].get("ipv6") is True,
        },
        "request_id": _safe_text(execution.get("request_id"), 64) or None,
        "outcome_unknown": False,
    }


def _execute(
    db: Session, actor: User | None, action: str, params: dict[str, Any], *, source: str,
) -> dict[str, Any]:
    try:
        execution = ops_service.execute(
            db, actor, action=action, params=params, source=source, request_id=uuid.uuid4().hex,
        )
    except Exception as exc:  # noqa: BLE001 - 状态不可用不能伪称已防御
        return unavailable(str(exc))
    return _verified_snapshot(execution)


def get_status(db: Session, actor: User) -> dict[str, Any]:
    """独立 GET 获取 root 实时状态，不使用数据库策略作为生效状态。"""
    return _execute(db, actor, "security_block_status", {}, source="security_center")


def refresh_status(db: Session) -> dict[str, Any]:
    """固定巡检只读刷新；临时故障恢复后无需等待管理员打开页面。"""
    return _execute(db, None, "security_block_status", {}, source="security_response")


def configure(
    db: Session, actor: User, values: dict[str, Any], *, protected_ip: str,
) -> dict[str, Any]:
    """只在 root 执行和核验成功后返回生效；OpsExecution 保留失败/未知证据。"""
    policy = AutomaticBlockingPolicyIn.model_validate(values).model_dump()
    params = {**policy, "protected_ip": protected_ip}
    ops_service.validate_action_params("security_block_configure", params)
    return _execute(db, actor, "security_block_configure", params, source="security_center")


def release(db: Session, actor: User, *, ip: str, reason: str) -> dict[str, Any]:
    """人工解封经最高管理员 API 操作，模型无法调用。"""
    params = ops_service.validate_action_params("security_block_release", {"ip": ip, "reason": reason})
    return _execute(db, actor, "security_block_release", params, source="security_center")


def reconcile(db: Session) -> dict[str, Any]:
    """固定调度仅请求 root 重新读取日志，不传候选 IP、计数或模型意见。"""
    return _execute(db, None, "security_block_reconcile", {}, source="security_response")


def cached_status(db: Session) -> dict[str, Any]:
    """概览读取已审计回执；界面应通过独立 GET 确认实时内核状态。"""
    row = (
        db.query(OpsExecution)
        .filter(OpsExecution.action.in_(ops_service.INTERNAL_SECURITY_ACTIONS))
        .order_by(OpsExecution.id.desc())
        .first()
    )
    if row is None:
        return unavailable("自动封禁尚未配置或宿主机状态未核验")
    return execution_snapshot(row)


def execution_snapshot(row: OpsExecution) -> dict[str, Any]:
    """从不可变运维回执提取白名单摘要，供概览与时间线展示。"""
    try:
        wrapper = json.loads(row.result_json or "{}")
    except (TypeError, ValueError):
        return unavailable("自动封禁审计回执不可解析")
    result = _verified_snapshot({
        "status": row.status, "request_id": row.request_id, "error": row.error, "result": wrapper,
    })
    recorded_at = row.finished_at or row.started_at
    result["confirmed_at"] = recorded_at.isoformat() if recorded_at else None
    result["snapshot_source"] = "audited_receipt"
    return result
