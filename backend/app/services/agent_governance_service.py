"""Agent 治理画像服务。"""
import json
from datetime import datetime, timezone
from typing import Optional, Union

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.agents.registry import AgentRegistry
from app.core.exceptions import NotFoundError
from app.models.agent_governance import (
    AgentAlert,
    AgentJob,
    AgentKnowledgeDoc,
    AgentMemory,
    AgentProfile,
    AgentRewardEvent,
    AgentSkillBinding,
    AgentToolPermission,
    ApprovalItem,
    PolicyDecisionLog,
    ToolCallLog,
)

_DEFAULT_GOVERNANCE_AGENTS = (
    ("manager", "管理Agent", "治理 Agent 生命周期、配置、版本和策略", "governance", ("agent_management",)),
    ("approval", "审批Agent", "低风险自动审批，高风险升级", "governance", ("approval",)),
    ("policy", "安全策略Agent", "动作风险评分与策略决策", "security", ("policy",)),
    ("scheduler", "调度Agent", "每日抓取和周期任务", "operations", ("schedule",)),
    ("memory_manager", "记忆管理Agent", "独立记忆沉淀与检索", "knowledge", ("memory",)),
    ("knowledge_distiller", "知识蒸馏Agent", "抓取清洗蒸馏知识", "knowledge", ("knowledge",)),
    ("monitor", "监控Agent", "指标、SLA、成本和异常", "operations", ("observability",)),
    ("reflection", "自我反思Agent", "反思、奖惩和改进建议", "meta", ("reflection", "selfimprovingagent")),
    ("alert", "告警Agent", "异常告警和升级通知", "operations", ("alert",)),
)


def sync_profiles(db: Session) -> list[AgentProfile]:
    """同步运行时 Agent 和治理 Agent 到持久化画像。

    Args:
        db: 数据库会话。

    Returns:
        list[AgentProfile]: 同步后的 Agent 画像列表。
    """
    registry_items = AgentRegistry.instance().list_runtime()
    desired: list[dict] = []
    for item in registry_items:
        desired.append({
            "code": item["code"],
            "name": item["name"],
            "description": item.get("description", ""),
            "category": item.get("category", "general"),
            "status": item.get("status", "idle"),
            "model": item.get("model", ""),
            "icon": item.get("icon", "base"),
            "color": item.get("color", "#5B58E8"),
            "skills": item.get("skills", []),
        })
    for code, name, desc, category, skills in _DEFAULT_GOVERNANCE_AGENTS:
        desired.append({
            "code": code,
            "name": name,
            "description": desc,
            "category": category,
            "status": "idle",
            "model": "",
            "icon": code,
            "color": "#2A9D8F",
            "skills": list(skills),
        })

    for item in desired:
        profile = db.query(AgentProfile).filter(AgentProfile.code == item["code"]).first()
        if not profile:
            profile = AgentProfile(code=item["code"], name=item["name"])
            db.add(profile)
        profile.name = item["name"]
        profile.description = item.get("description", "")
        profile.category = item.get("category", "general")
        profile.status = item.get("status", profile.status or "idle")
        profile.model = item.get("model") or profile.model
        profile.icon = item.get("icon", "base")
        profile.color = item.get("color", "#5B58E8")
        _sync_skills(db, item["code"], item.get("skills", []))
    db.commit()
    return list_profiles(db)


def list_profiles(db: Session) -> list[AgentProfile]:
    """查询 Agent 治理画像。

    Args:
        db: 数据库会话。

    Returns:
        list[AgentProfile]: Agent 画像列表。
    """
    return db.query(AgentProfile).order_by(AgentProfile.category.asc(), AgentProfile.code.asc()).all()


def get_profile(db: Session, code: str) -> AgentProfile:
    """获取单个 Agent 治理画像。

    Args:
        db: 数据库会话。
        code: Agent 编码。

    Returns:
        AgentProfile: Agent 画像。

    Raises:
        NotFoundError: Agent 不存在。
    """
    profile = db.query(AgentProfile).filter(AgentProfile.code == code).first()
    if not profile:
        raise NotFoundError("Agent 不存在", code=40400)
    return profile


def update_profile(db: Session, code: str, payload: dict) -> AgentProfile:
    """更新 Agent 治理画像配置。

    Args:
        db: 数据库会话。
        code: Agent 编码。
        payload: 待更新字段。

    Returns:
        AgentProfile: 更新后的 Agent 画像。
    """
    profile = get_profile(db, code)
    for key in ("status", "budget_tokens_daily", "priority", "auto_approval_threshold", "is_enabled"):
        if key in payload and payload[key] is not None:
            setattr(profile, key, payload[key])
    if payload.get("config_json") is not None:
        profile.config_json = json.dumps(payload["config_json"], ensure_ascii=False)
    db.commit()
    db.refresh(profile)
    return profile


def profile_to_dict(db: Session, profile: AgentProfile) -> dict:
    """把 AgentProfile 转为前端输出字典。

    Args:
        db: 数据库会话。
        profile: AgentProfile ORM 对象。

    Returns:
        dict: 输出字典。
    """
    skills = [
        row.skill_name
        for row in db.query(AgentSkillBinding).filter(
            AgentSkillBinding.agent_code == profile.code,
            AgentSkillBinding.enabled == 1,
        ).all()
    ]
    return {
        "code": profile.code,
        "name": profile.name,
        "description": profile.description or "",
        "category": profile.category,
        "status": profile.status,
        "model": profile.model,
        "icon": profile.icon,
        "color": profile.color,
        "budget_tokens_daily": profile.budget_tokens_daily,
        "priority": profile.priority,
        "auto_approval_threshold": profile.auto_approval_threshold,
        "is_enabled": profile.is_enabled,
        # R5 修复:返回 config_json,使管理员可查看扩展配置
        # 数据库存储为 JSON 字符串,这里解析后返回(dict/list),非 dict 时返回 None
        "config_json": _safe_json_parse(profile.config_json),
        "skills": skills,
        "tool_count": db.query(AgentToolPermission).filter(AgentToolPermission.agent_code == profile.code).count(),
        "memory_count": db.query(AgentMemory).filter(AgentMemory.agent_code == profile.code).count(),
        "knowledge_count": db.query(AgentKnowledgeDoc).filter(AgentKnowledgeDoc.agent_code == profile.code).count(),
        "create_time": profile.create_time,
        "update_time": profile.update_time,
    }


def _safe_json_parse(value: Optional[str]) -> Optional[Union[dict, list]]:
    """安全解析 JSON 字符串字段。

    Args:
        value: 数据库 JSON 文本字段值。

    Returns:
        Optional[Union[dict, list]]: 解析后的 dict/list;空值或解析失败返回 None。
    """
    if not value:
        return None
    try:
        parsed = json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return None
    return parsed if isinstance(parsed, (dict, list)) else None


def governance_overview(db: Session) -> dict:
    """聚合 Agent 治理总览指标。

    Args:
        db: 数据库会话。

    Returns:
        dict: 大屏总览指标。
    """
    today = datetime.now(timezone.utc).date()
    agents_total = db.query(AgentProfile).count()
    agents_enabled = db.query(AgentProfile).filter(AgentProfile.is_enabled == 1).count()
    risk_rows = (
        db.query(PolicyDecisionLog.risk_level, func.count(PolicyDecisionLog.id))
        .group_by(PolicyDecisionLog.risk_level)
        .all()
    )
    recent_alerts = (
        db.query(AgentAlert)
        .filter(AgentAlert.status == "open")
        .order_by(AgentAlert.id.desc())
        .limit(5)
        .all()
    )
    return {
        "agents_total": agents_total,
        "agents_enabled": agents_enabled,
        "approvals_pending": db.query(ApprovalItem).filter(ApprovalItem.status == "pending").count(),
        "approvals_auto_today": db.query(ApprovalItem).filter(ApprovalItem.status == "auto_approved").count(),
        "policy_decisions_today": (
            db.query(PolicyDecisionLog)
            .filter(func.date(PolicyDecisionLog.create_time) == today)
            .count()
        ),
        "tool_calls_today": db.query(ToolCallLog).filter(func.date(ToolCallLog.create_time) == today).count(),
        "alerts_open": db.query(AgentAlert).filter(AgentAlert.status == "open").count(),
        "knowledge_docs_total": db.query(AgentKnowledgeDoc).count(),
        "memory_items_total": db.query(AgentMemory).count(),
        "jobs_enabled": db.query(AgentJob).filter(AgentJob.status == "enabled").count(),
        "reward_score_total": float(db.query(func.coalesce(func.sum(AgentRewardEvent.score), 0.0)).scalar() or 0.0),
        "risk_distribution": [{"risk_level": k or "unknown", "count": v} for k, v in risk_rows],
        "recent_alerts": [{"id": a.id, "title": a.title, "severity": a.severity} for a in recent_alerts],
    }


def _sync_skills(db: Session, agent_code: str, skills: list[str]) -> None:
    """同步 Agent 默认 skill 绑定。

    Args:
        db: 数据库会话。
        agent_code: Agent 编码。
        skills: Skill 名称列表。

    Returns:
        None。
    """
    # v3.0 起 AgentRegistry 的 skills 已升级为结构化 list[dict]
    # (形如 {"name": "orchestrator.self_improve", ...})。这里兼容性地
    # 归一化为技能名字符串,避免对 dict 取 set 触发 unhashable type 报错。
    normalized = [
        (item.get("name") if isinstance(item, dict) else item)
        for item in (skills or [])
    ]
    base_skills = {name for name in normalized if name}
    base_skills.update({"selfimprovingagent", "reflection"})
    for skill in sorted(base_skills):
        existing = db.query(AgentSkillBinding).filter(
            AgentSkillBinding.agent_code == agent_code,
            AgentSkillBinding.skill_code == skill,
        ).first()
        if existing:
            continue
        db.add(AgentSkillBinding(
            agent_code=agent_code,
            skill_code=skill,
            skill_name=skill,
            version="1.0.0",
            enabled=1,
        ))
