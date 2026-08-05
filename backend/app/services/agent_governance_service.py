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
    (
        "manager",
        "管理Agent",
        "管理全部管理员页面、Agent 生命周期、配置、版本和策略",
        "governance",
        ("admin_capabilities", "agent_management"),
    ),
    (
        "operations",
        "全服管理Agent",
        "宿主机全域巡检、受批准变更、验证与回滚",
        "operations",
        (
            "host_inspection",
            "systemd",
            "container_operations",
            "file_management",
            "package_management",
            "firewall",
            "account_keys",
            "backup_restore",
            "incident_response",
        ),  # noqa: E501
    ),
    ("approval", "审批Agent", "低风险自动审批，高风险升级", "governance", ("approval",)),
    ("policy", "安全策略Agent", "动作风险评分与策略决策", "security", ("policy",)),
    ("scheduler", "调度Agent", "每日抓取和周期任务", "operations", ("schedule",)),
    ("memory_manager", "记忆管理Agent", "独立记忆沉淀与检索", "knowledge", ("memory",)),
    ("knowledge_distiller", "知识蒸馏Agent", "抓取清洗蒸馏知识", "knowledge", ("knowledge",)),
    ("monitor", "监控Agent", "指标、SLA、成本和异常", "operations", ("observability",)),
    ("reflection", "自我反思Agent", "反思、奖惩和改进建议", "meta", ("reflection", "selfimprovingagent")),
    ("alert", "告警Agent", "异常告警和升级通知", "operations", ("alert",)),
    ("test_verifier", "测试验证Agent", "执行回归验证并归档可复核结果", "quality", ("test", "verification")),
    (
        "sandbox_deployer",
        "沙箱部署Agent",
        "部署、续期和关闭隔离运行环境",
        "operations",
        ("sandbox", "deploy", "lifecycle"),
    ),
    (
        "quality_evaluator",
        "质量评估Agent",
        "汇总代码质量信号并评估改进收益",
        "quality",
        ("quality_evaluation", "reflection"),
    ),
    (
        "cost_controller",
        "成本控制Agent",
        "分析模型消耗、预算和异常调用",
        "operations",
        ("cost_analysis", "budget_guard"),
    ),
    (
        "model_evaluator",
        "模型评测Agent",
        "运行黄金集并比较模型表现",
        "quality",
        ("model_evaluation", "benchmark"),
    ),
    (
        "report_verifier",
        "报告校验Agent",
        "校验审查报告完整性和证据引用",
        "quality",
        ("report_validation",),
    ),
    (
        "data_integrity",
        "数据一致性Agent",
        "核验任务、问题、审计和指标之间的关联",
        "governance",
        ("data_validation", "audit"),
    ),
    (
        "incident_responder",
        "事件响应Agent",
        "处置告警并生成恢复与复盘记录",
        "operations",
        ("incident_response", "alert"),
    ),
)

_MANAGER_ADMIN_CAPABILITY_TOOL = "admin_execute_capability"
_MANAGER_ADMIN_CAPABILITY_CONTRACT_VERSION = "manager_admin_capability_v1"
_MANAGER_ADMIN_CAPABILITY_SCOPE = "管理全部管理员页面并通过真实业务 API 执行已登记能力"
_MANAGER_ADMIN_CAPABILITY_PERMISSION_NOTE = (
    "manager_admin_capability_v1 protected manager admin capability gateway"
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
        desired.append(
            {
                "code": item["code"],
                "name": item["name"],
                "description": item.get("description", ""),
                "category": item.get("category", "general"),
                "status": item.get("status", "idle"),
                "model": item.get("model", ""),
                "icon": item.get("icon", "base"),
                "color": item.get("color", "#5B58E8"),
                "skills": item.get("skills", []),
            }
        )
    for code, name, desc, category, skills in _DEFAULT_GOVERNANCE_AGENTS:
        desired.append(
            {
                "code": code,
                "name": name,
                "description": desc,
                "category": category,
                "status": "idle",
                "model": "",
                "icon": code,
                "color": "#2A9D8F",
                "skills": list(skills),
            }
        )

    # 同一 Agent 可能同时出现在运行时注册表和治理默认表。按编码合并，
    # 后写入的治理元数据覆盖运行时展示字段，避免单事务重复插入触发唯一索引。
    desired_by_code = {item["code"]: item for item in desired}

    for item in desired_by_code.values():
        profile = db.query(AgentProfile).filter(AgentProfile.code == item["code"]).first()
        if not profile:
            profile = AgentProfile(code=item["code"], name=item["name"])
            db.add(profile)
        profile.name = item["name"]
        profile.description = item.get("description", "")
        profile.category = item.get("category", "general")
        profile.status = "disabled" if profile.is_enabled == 0 else item.get("status", profile.status or "idle")
        profile.model = item.get("model") or profile.model
        profile.icon = item.get("icon", "base")
        profile.color = item.get("color", "#5B58E8")
        if profile.code == "manager":
            _ensure_manager_admin_capability_contract(db, profile)
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


def is_runtime_enabled(db: Session, code: str) -> bool:
    """判断 Agent 是否允许执行；未建治理画像的旧 Agent 保持兼容。"""
    profile = db.query(AgentProfile).filter(AgentProfile.code == code).first()
    return profile is None or bool(profile.is_enabled)


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


def update_profile(
    db: Session,
    code: str,
    payload: dict,
    *,
    commit: bool = True,
) -> AgentProfile:
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
    if profile.code == "manager":
        _ensure_manager_admin_capability_contract(db, profile)
    if commit:
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
        for row in db.query(AgentSkillBinding)
        .filter(
            AgentSkillBinding.agent_code == profile.code,
            AgentSkillBinding.enabled == 1,
        )
        .all()
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


def _ensure_manager_admin_capability_contract(db: Session, profile: AgentProfile) -> None:
    """保证受保护管理 Agent 始终能进入已登记的管理页面能力。

    该入口仅负责转发到固定能力注册表；真实执行仍由能力权限、
    OpenAPI 参数校验、风险分级和审批门禁约束。显式工具权限记录如果
    已存在则保留，以允许运维人员在更高级治理层收紧该入口。
    """
    parsed = _safe_json_parse(profile.config_json)
    config = dict(parsed) if isinstance(parsed, dict) else {}
    raw_boundary = config.get("governance_boundary")
    boundary = dict(raw_boundary) if isinstance(raw_boundary, dict) else {}

    def _tool_set(key: str) -> set[str]:
        values = boundary.get(key, [])
        if not isinstance(values, (list, tuple, set)):
            return set()
        return {str(value) for value in values if value}

    allowed_tools = _tool_set("allowed_tools")
    approval_tools = _tool_set("approval_tools")
    blocked_tools = _tool_set("blocked_tools")
    allowed_tools.add(_MANAGER_ADMIN_CAPABILITY_TOOL)
    approval_tools.discard(_MANAGER_ADMIN_CAPABILITY_TOOL)
    blocked_tools.discard(_MANAGER_ADMIN_CAPABILITY_TOOL)
    boundary.update(
        {
            "scope": str(boundary.get("scope") or _MANAGER_ADMIN_CAPABILITY_SCOPE),
            "allowed_tools": sorted(allowed_tools),
            "approval_tools": sorted(approval_tools),
            "blocked_tools": sorted(blocked_tools),
        }
    )
    config["governance_boundary"] = boundary
    config["manager_admin_capability_boundary_version"] = _MANAGER_ADMIN_CAPABILITY_CONTRACT_VERSION
    profile.config_json = json.dumps(config, ensure_ascii=False, sort_keys=True)

    permission = (
        db.query(AgentToolPermission)
        .filter(
            AgentToolPermission.agent_code == "manager",
            AgentToolPermission.tool_code == _MANAGER_ADMIN_CAPABILITY_TOOL,
        )
        .order_by(AgentToolPermission.id.asc())
        .first()
    )
    if permission is None:
        db.add(
            AgentToolPermission(
                agent_code="manager",
                tool_code=_MANAGER_ADMIN_CAPABILITY_TOOL,
                permission="allow",
                risk_level="low",
                enabled=1,
                note=_MANAGER_ADMIN_CAPABILITY_PERMISSION_NOTE,
            )
        )


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
        db.query(AgentAlert).filter(AgentAlert.status == "open").order_by(AgentAlert.id.desc()).limit(5).all()
    )
    from app.services import agent_service

    return {
        "agents_total": agents_total,
        "agents_enabled": agents_enabled,
        "callable_agents_total": agent_service.get_runtime_summary(db)["total"],
        "approvals_pending": db.query(ApprovalItem).filter(ApprovalItem.status == "pending").count(),
        "approvals_auto_today": db.query(ApprovalItem).filter(ApprovalItem.status == "auto_approved").count(),
        "policy_decisions_today": (
            db.query(PolicyDecisionLog).filter(func.date(PolicyDecisionLog.create_time) == today).count()
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
    normalized = [(item.get("name") if isinstance(item, dict) else item) for item in (skills or [])]
    base_skills = {name for name in normalized if name}
    base_skills.update({"selfimprovingagent", "reflection"})
    for skill in sorted(base_skills):
        existing = (
            db.query(AgentSkillBinding)
            .filter(
                AgentSkillBinding.agent_code == agent_code,
                AgentSkillBinding.skill_code == skill,
            )
            .first()
        )
        if existing:
            continue
        db.add(
            AgentSkillBinding(
                agent_code=agent_code,
                skill_code=skill,
                skill_name=skill,
                version="1.0.0",
                enabled=1,
            )
        )
