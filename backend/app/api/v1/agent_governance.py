"""Agent 治理平台管理端 API。"""
import json

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.dependencies import require_admin, require_super_admin
from app.models.agent_governance import (
    AgentArtifactVersion,
    AgentJob,
    AgentJobRun,
    AgentRewardEvent,
    AgentToolPermission,
    PolicyDecisionLog,
    PolicyRule,
    ToolCallLog,
)
from app.models.user import User
from app.schemas.agent_governance import (
    AgentAlertOut,
    AgentAlertResolveIn,
    AgentArtifactVersionCreateIn,
    AgentJobOut,
    AgentJobUpdateIn,
    AgentKnowledgeDocCreateIn,
    AgentKnowledgeDocOut,
    AgentKnowledgeSourceOut,
    AgentKnowledgeSourceUpsertIn,
    AgentMemoryCreateIn,
    AgentMemoryOut,
    AgentProfileOut,
    AgentProfileUpdateIn,
    AgentRewardCreateIn,
    AgentToolPermissionOut,
    AgentToolPermissionUpsertIn,
    ApprovalDecisionIn,
    ApprovalItemOut,
    GovernanceOverviewOut,
    PolicyDecisionOut,
    PolicyEvaluateIn,
    PolicyRuleOut,
    PolicyRuleUpsertIn,
    ToolCallLogOut,
)
from app.schemas.common import Resp
from app.services import (
    agent_governance_service,
    agent_knowledge_service,
    agent_memory_service,
    approval_service,
    observability_service,
    policy_engine,
    reward_service,
    rollback_service,
    scheduler_service,
)

router = APIRouter()


@router.get("/governance/overview", response_model=Resp[GovernanceOverviewOut])
def governance_overview(db: Session = Depends(get_db), _: User = Depends(require_admin)):
    """返回 Agent 治理大屏总览。

    Args:
        db: 数据库会话。
        _: 管理员用户。

    Returns:
        Resp[GovernanceOverviewOut]: 治理大屏指标。
    """
    agent_governance_service.sync_profiles(db)
    return Resp(data=GovernanceOverviewOut(**agent_governance_service.governance_overview(db)))


@router.get("/governance/agents", response_model=Resp[list[AgentProfileOut]])
def list_governance_agents(db: Session = Depends(get_db), _: User = Depends(require_admin)):
    """返回 Agent 治理清单。

    Args:
        db: 数据库会话。
        _: 管理员用户。

    Returns:
        Resp[list[AgentProfileOut]]: Agent 治理画像列表。
    """
    rows = agent_governance_service.sync_profiles(db)
    return Resp(data=[AgentProfileOut(**agent_governance_service.profile_to_dict(db, row)) for row in rows])


@router.get("/governance/agents/{agent_code}", response_model=Resp[AgentProfileOut])
def get_governance_agent(agent_code: str, db: Session = Depends(get_db), _: User = Depends(require_admin)):
    """返回单个 Agent 治理详情。

    Args:
        agent_code: Agent 编码。
        db: 数据库会话。
        _: 管理员用户。

    Returns:
        Resp[AgentProfileOut]: Agent 治理画像。
    """
    row = agent_governance_service.get_profile(db, agent_code)
    return Resp(data=AgentProfileOut(**agent_governance_service.profile_to_dict(db, row)))


@router.put("/governance/agents/{agent_code}", response_model=Resp[AgentProfileOut])
def update_governance_agent(
    agent_code: str,
    payload: AgentProfileUpdateIn,
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
):
    """更新 Agent 治理配置。

    Args:
        agent_code: Agent 编码。
        payload: 更新参数。
        db: 数据库会话。
        _: 管理员用户。

    Returns:
        Resp[AgentProfileOut]: 更新后的 Agent 治理画像。
    """
    row = agent_governance_service.update_profile(db, agent_code, payload.model_dump(exclude_none=True))
    return Resp(data=AgentProfileOut(**agent_governance_service.profile_to_dict(db, row)))


@router.get("/governance/agents/{agent_code}/memory", response_model=Resp[list[AgentMemoryOut]])
def list_agent_memory(agent_code: str, db: Session = Depends(get_db), _: User = Depends(require_admin)):
    """查询指定 Agent 的独立记忆。

    Args:
        agent_code: Agent 编码。
        db: 数据库会话。
        _: 管理员用户。

    Returns:
        Resp[list[AgentMemoryOut]]: 记忆列表。
    """
    rows = agent_memory_service.list_memory(db, agent_code=agent_code)
    return Resp(data=[AgentMemoryOut.model_validate(row) for row in rows])


@router.post("/governance/agents/{agent_code}/memory", response_model=Resp[AgentMemoryOut])
def create_agent_memory(
    agent_code: str,
    payload: AgentMemoryCreateIn,
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
):
    """为指定 Agent 创建独立记忆。

    Args:
        agent_code: Agent 编码。
        payload: 记忆输入。
        db: 数据库会话。
        _: 管理员用户。

    Returns:
        Resp[AgentMemoryOut]: 新增记忆。
    """
    row = agent_memory_service.add_memory(
        db,
        agent_code=agent_code,
        title=payload.title,
        content=payload.content,
        memory_type=payload.memory_type,
        weight=payload.weight,
        source_ref=payload.source_ref,
    )
    return Resp(data=AgentMemoryOut.model_validate(row))


@router.get("/governance/agents/{agent_code}/knowledge", response_model=Resp[list[AgentKnowledgeDocOut]])
def list_agent_knowledge(agent_code: str, db: Session = Depends(get_db), _: User = Depends(require_admin)):
    """查询指定 Agent 的知识文档。

    Args:
        agent_code: Agent 编码。
        db: 数据库会话。
        _: 管理员用户。

    Returns:
        Resp[list[AgentKnowledgeDocOut]]: 知识文档列表。
    """
    rows = agent_knowledge_service.list_docs(db, agent_code=agent_code)
    return Resp(data=[AgentKnowledgeDocOut.model_validate(row) for row in rows])


@router.post("/governance/knowledge/docs", response_model=Resp[AgentKnowledgeDocOut])
def create_agent_knowledge_doc(
    payload: AgentKnowledgeDocCreateIn,
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
):
    """创建 Agent 知识文档，必要时自动进入审批中心。

    Args:
        payload: 知识文档输入。
        db: 数据库会话。
        _: 管理员用户。

    Returns:
        Resp[AgentKnowledgeDocOut]: 知识文档。
    """
    row = agent_knowledge_service.add_document(
        db,
        agent_code=payload.agent_code,
        title=payload.title,
        content=payload.content,
        source_type=payload.source_type,
        source_ref=payload.source_ref,
        risk_level=payload.risk_level,
        confidence=payload.confidence,
    )
    return Resp(data=AgentKnowledgeDocOut.model_validate(row))


@router.post("/governance/knowledge/docs/{doc_id}/activate", response_model=Resp[AgentKnowledgeDocOut])
def activate_agent_knowledge_doc(
    doc_id: int,
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
):
    """手动激活 Agent 知识文档。

    Args:
        doc_id: 文档 ID。
        db: 数据库会话。
        _: 管理员用户。

    Returns:
        Resp[AgentKnowledgeDocOut]: 激活后的知识文档。
    """
    row = agent_knowledge_service.activate_document(db, doc_id)
    return Resp(data=AgentKnowledgeDocOut.model_validate(row))


@router.get("/governance/knowledge/sources", response_model=Resp[list[AgentKnowledgeSourceOut]])
def list_agent_knowledge_sources(
    agent_code: str = Query(""),
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
):
    """查询 Agent 知识来源配置。

    Args:
        agent_code: 可选 Agent 编码。
        db: 数据库会话。
        _: 管理员用户。

    Returns:
        Resp[list[AgentKnowledgeSourceOut]]: 来源列表。
    """
    rows = agent_knowledge_service.list_sources(db, agent_code=agent_code)
    return Resp(data=[AgentKnowledgeSourceOut.model_validate(row) for row in rows])


@router.post("/governance/knowledge/sources", response_model=Resp[AgentKnowledgeSourceOut])
def upsert_agent_knowledge_source(
    payload: AgentKnowledgeSourceUpsertIn,
    source_id: int = Query(0),
    db: Session = Depends(get_db),
    _: User = Depends(require_super_admin),
):
    """创建或更新 Agent 知识来源配置。

    Args:
        payload: 来源配置。
        source_id: 可选来源 ID。
        db: 数据库会话。
        _: 管理员用户。

    Returns:
        Resp[AgentKnowledgeSourceOut]: 来源记录。
    """
    row = agent_knowledge_service.upsert_source(
        db,
        agent_code=payload.agent_code,
        source_type=payload.source_type,
        source_uri=payload.source_uri,
        whitelist=payload.whitelist,
        enabled=payload.enabled,
        config=payload.config_json,
        source_id=source_id or None,
    )
    return Resp(data=AgentKnowledgeSourceOut.model_validate(row))


@router.post("/governance/knowledge/crawl", response_model=Resp[dict])
def crawl_agent_knowledge_sources(
    agent_code: str = Query(""),
    db: Session = Depends(get_db),
    _: User = Depends(require_super_admin),
):
    """手动抓取 Agent 知识来源。

    Args:
        agent_code: 可选 Agent 编码。
        db: 数据库会话。
        _: 管理员用户。

    Returns:
        Resp[dict]: 抓取结果。
    """
    return Resp(data=agent_knowledge_service.crawl_enabled_sources(db, agent_code=agent_code))


@router.get("/approvals", response_model=Resp[list[ApprovalItemOut]])
def list_approvals(
    status: str = Query(""),
    db: Session = Depends(get_db),
    actor: User = Depends(require_admin),
):
    """查询治理审批事项。

    Args:
        status: 可选状态过滤。
        db: 数据库会话。
        actor: 当前管理员。

    Returns:
        Resp[list[ApprovalItemOut]]: 审批事项列表。
    """
    rows = approval_service.list_items(db, status=status, actor=actor)
    return Resp(data=[ApprovalItemOut.model_validate(row) for row in rows])


@router.post("/approvals/{item_id}/approve", response_model=Resp[ApprovalItemOut])
def approve_item(
    item_id: int,
    payload: ApprovalDecisionIn,
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
):
    """管理员审批通过治理事项。

    Args:
        item_id: 审批事项 ID。
        payload: 审批说明。
        db: 数据库会话。
        admin: 管理员用户。

    Returns:
        Resp[ApprovalItemOut]: 更新后的审批事项。
    """
    row = approval_service.decide_item(db, admin, item_id, approve=True, note=payload.note)
    return Resp(data=ApprovalItemOut.model_validate(row))


@router.post("/approvals/{item_id}/reject", response_model=Resp[ApprovalItemOut])
def reject_item(
    item_id: int,
    payload: ApprovalDecisionIn,
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
):
    """管理员拒绝治理事项。

    Args:
        item_id: 审批事项 ID。
        payload: 审批说明。
        db: 数据库会话。
        admin: 管理员用户。

    Returns:
        Resp[ApprovalItemOut]: 更新后的审批事项。
    """
    row = approval_service.decide_item(db, admin, item_id, approve=False, note=payload.note)
    return Resp(data=ApprovalItemOut.model_validate(row))


@router.get("/policies", response_model=Resp[list[PolicyRuleOut]])
def list_policies(db: Session = Depends(get_db), _: User = Depends(require_admin)):
    """查询策略规则。

    Args:
        db: 数据库会话。
        _: 管理员用户。

    Returns:
        Resp[list[PolicyRuleOut]]: 策略规则列表。
    """
    rows = db.query(PolicyRule).order_by(PolicyRule.priority.asc(), PolicyRule.id.asc()).all()
    return Resp(data=[PolicyRuleOut.model_validate(row) for row in rows])


@router.post("/policies", response_model=Resp[PolicyRuleOut])
def upsert_policy(
    payload: PolicyRuleUpsertIn,
    rule_id: int = Query(0),
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
):
    """创建或更新策略规则。

    Args:
        payload: 策略规则输入。
        rule_id: 可选策略 ID。
        db: 数据库会话。
        _: 管理员用户。

    Returns:
        Resp[PolicyRuleOut]: 策略规则。
    """
    row = db.get(PolicyRule, rule_id) if rule_id else None
    if not row:
        row = PolicyRule(rule_code=payload.rule_code, name=payload.name)
        db.add(row)
    row.rule_code = payload.rule_code
    row.name = payload.name
    row.subject = payload.subject
    row.action = payload.action
    row.resource = payload.resource
    row.effect = payload.effect
    row.risk_level = payload.risk_level
    row.condition_json = json.dumps(payload.condition_json or {}, ensure_ascii=False)
    row.priority = payload.priority
    row.enabled = payload.enabled
    db.commit()
    db.refresh(row)
    snapshot = json.dumps({
        "rule_id": row.id,
        "rule_code": row.rule_code,
        "name": row.name,
        "subject": row.subject,
        "action": row.action,
        "resource": row.resource,
        "effect": row.effect,
        "risk_level": row.risk_level,
        "condition_json": row.condition_json,
        "priority": row.priority,
        "enabled": row.enabled,
    }, ensure_ascii=False)
    rollback_service.create_version(
        db,
        agent_code="policy",
        artifact_type="policy",
        version=f"policy-{row.id}-{row.update_time.isoformat() if row.update_time else row.id}",
        content=snapshot,
        snapshot=snapshot,
        status="stable",
    )
    return Resp(data=PolicyRuleOut.model_validate(row))


@router.post("/policies/evaluate", response_model=Resp[PolicyDecisionOut])
def evaluate_policy(
    payload: PolicyEvaluateIn,
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
):
    """试算策略决策。

    Args:
        payload: 策略试算输入。
        db: 数据库会话。
        _: 管理员用户。

    Returns:
        Resp[PolicyDecisionOut]: 策略决策。
    """
    decision = policy_engine.evaluate(
        db,
        subject=payload.subject,
        action=payload.action,
        resource=payload.resource,
        context=payload.context,
    )
    row = db.get(PolicyDecisionLog, decision.log_id) if decision.log_id else None
    if row:
        return Resp(data=PolicyDecisionOut.model_validate(row))
    return Resp(data=PolicyDecisionOut(**decision.__dict__))


@router.get("/policies/decisions", response_model=Resp[list[PolicyDecisionOut]])
def list_policy_decisions(db: Session = Depends(get_db), _: User = Depends(require_admin)):
    """查询策略决策日志。

    Args:
        db: 数据库会话。
        _: 管理员用户。

    Returns:
        Resp[list[PolicyDecisionOut]]: 决策日志列表。
    """
    rows = db.query(PolicyDecisionLog).order_by(PolicyDecisionLog.id.desc()).limit(100).all()
    return Resp(data=[PolicyDecisionOut.model_validate(row) for row in rows])


@router.get("/tools/calls", response_model=Resp[list[ToolCallLogOut]])
def list_tool_calls(db: Session = Depends(get_db), _: User = Depends(require_admin)):
    """查询 Agent 工具调用日志。

    Args:
        db: 数据库会话。
        _: 管理员用户。

    Returns:
        Resp[list[ToolCallLogOut]]: 工具调用日志列表。
    """
    rows = db.query(ToolCallLog).order_by(ToolCallLog.id.desc()).limit(100).all()
    return Resp(data=[ToolCallLogOut.model_validate(row) for row in rows])


@router.get("/tools/permissions", response_model=Resp[list[AgentToolPermissionOut]])
def list_tool_permissions(db: Session = Depends(get_db), _: User = Depends(require_admin)):
    """查询 Agent 工具权限配置。

    Args:
        db: 数据库会话。
        _: 管理员用户。

    Returns:
        Resp[list[AgentToolPermissionOut]]: 工具权限列表。
    """
    rows = db.query(AgentToolPermission).order_by(AgentToolPermission.id.desc()).limit(200).all()
    return Resp(data=[AgentToolPermissionOut.model_validate(row) for row in rows])


@router.post("/tools/permissions", response_model=Resp[AgentToolPermissionOut])
def upsert_tool_permission(
    payload: AgentToolPermissionUpsertIn,
    permission_id: int = Query(0),
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
):
    """创建或更新 Agent 工具权限。

    Args:
        payload: 工具权限输入。
        permission_id: 可选权限 ID。
        db: 数据库会话。
        _: 管理员用户。

    Returns:
        Resp[AgentToolPermissionOut]: 工具权限。
    """
    row = db.get(AgentToolPermission, permission_id) if permission_id else None
    if not row:
        row = AgentToolPermission(agent_code=payload.agent_code, tool_code=payload.tool_code)
        db.add(row)
    row.agent_code = payload.agent_code
    row.tool_code = payload.tool_code
    row.permission = payload.permission
    row.risk_level = payload.risk_level
    row.enabled = payload.enabled
    row.note = payload.note
    db.commit()
    db.refresh(row)
    return Resp(data=AgentToolPermissionOut.model_validate(row))


@router.get("/jobs", response_model=Resp[list[AgentJobOut]])
def list_jobs(db: Session = Depends(get_db), actor: User = Depends(require_admin)):
    """查询 Agent 调度任务。

    Args:
        db: 数据库会话。
        _: 管理员用户。

    Returns:
        Resp[list[AgentJobOut]]: 调度任务列表。
    """
    rows = scheduler_service.ensure_default_jobs(db)
    if not scheduler_service.can_access_restricted_jobs(db, actor):
        rows = [row for row in rows if not scheduler_service.requires_super_admin(row.job_type)]
    return Resp(data=[AgentJobOut.model_validate(row) for row in rows])


@router.put("/jobs/{job_id}", response_model=Resp[AgentJobOut])
def update_job(
    job_id: int,
    payload: AgentJobUpdateIn,
    db: Session = Depends(get_db),
    actor: User = Depends(require_admin),
):
    """更新 Agent 调度任务配置。

    Args:
        job_id: 调度任务 ID。
        payload: 更新输入。
        db: 数据库会话。
        _: 管理员用户。

    Returns:
        Resp[AgentJobOut]: 更新后的任务。
    """
    row = scheduler_service.update_job(db, job_id, payload.model_dump(exclude_none=True), actor=actor)
    return Resp(data=AgentJobOut.model_validate(row))


@router.post("/jobs/{job_id}/run", response_model=Resp[dict])
def run_job(job_id: int, db: Session = Depends(get_db), actor: User = Depends(require_admin)):
    """手动运行 Agent 调度任务。

    Args:
        job_id: 调度任务 ID。
        db: 数据库会话。
        _: 管理员用户。

    Returns:
        Resp[dict]: 调度运行结果。
    """
    row = scheduler_service.run_job(db, job_id, actor=actor)
    return Resp(data={
        "id": row.id,
        "job_id": row.job_id,
        "status": row.status,
        "result_json": row.result_json,
        "error": row.error,
    })


@router.get("/jobs/runs", response_model=Resp[list[dict]])
def list_job_runs(db: Session = Depends(get_db), actor: User = Depends(require_admin)):
    """查询 Agent 调度运行记录。

    Args:
        db: 数据库会话。
        _: 管理员用户。

    Returns:
        Resp[list[dict]]: 调度运行记录。
    """
    query = db.query(AgentJobRun).join(AgentJob, AgentJob.id == AgentJobRun.job_id)
    if not scheduler_service.can_access_restricted_jobs(db, actor):
        query = query.filter(~AgentJob.job_type.in_(scheduler_service.SUPER_ADMIN_JOB_TYPES))
    rows = query.order_by(AgentJobRun.id.desc()).limit(100).all()
    return Resp(data=[{
        "id": row.id,
        "job_id": row.job_id,
        "status": row.status,
        "started_at": row.started_at.isoformat() if row.started_at else None,
        "finished_at": row.finished_at.isoformat() if row.finished_at else None,
        "result_json": row.result_json,
        "error": row.error,
    } for row in rows])


@router.get("/observability/overview", response_model=Resp[dict])
def observability_overview(db: Session = Depends(get_db), _: User = Depends(require_admin)):
    """查询治理观测指标。

    Args:
        db: 数据库会话。
        _: 管理员用户。

    Returns:
        Resp[dict]: 观测指标。
    """
    return Resp(data=observability_service.overview(db))


@router.get("/observability/alerts", response_model=Resp[list[AgentAlertOut]])
def list_alerts(
    status: str = Query("open"),
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
):
    """查询治理告警。

    Args:
        status: 告警状态。
        db: 数据库会话。
        _: 管理员用户。

    Returns:
        Resp[list[AgentAlertOut]]: 告警列表。
    """
    rows = observability_service.list_alerts(db, status=status)
    return Resp(data=[AgentAlertOut.model_validate(row) for row in rows])


@router.post("/observability/alerts/{alert_id}/resolve", response_model=Resp[AgentAlertOut])
def resolve_alert(
    alert_id: int,
    payload: AgentAlertResolveIn,
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
):
    """关闭治理告警。

    Args:
        alert_id: 告警 ID。
        payload: 处理说明。
        db: 数据库会话。
        admin: 管理员用户。

    Returns:
        Resp[AgentAlertOut]: 关闭后的告警。
    """
    row = observability_service.resolve_alert(db, alert_id, admin.id, note=payload.note)
    return Resp(data=AgentAlertOut.model_validate(row))


@router.get("/rollback/versions", response_model=Resp[list[dict]])
def list_versions(
    agent_code: str = Query(""),
    artifact_type: str = Query(""),
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
):
    """查询 Agent artifact 版本。

    Args:
        agent_code: 可选 Agent 编码。
        artifact_type: 可选 artifact 类型。
        db: 数据库会话。
        _: 管理员用户。

    Returns:
        Resp[list[dict]]: 版本列表。
    """
    rows = rollback_service.list_versions(db, agent_code=agent_code, artifact_type=artifact_type)
    return Resp(data=[_version_to_dict(row) for row in rows])


@router.post("/rollback/versions", response_model=Resp[dict])
def create_version(
    payload: AgentArtifactVersionCreateIn,
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
):
    """创建 Agent artifact 版本。

    Args:
        payload: 版本输入。
        db: 数据库会话。
        _: 管理员用户。

    Returns:
        Resp[dict]: 新增版本。
    """
    row = rollback_service.create_version(
        db,
        agent_code=payload.agent_code,
        artifact_type=payload.artifact_type,
        version=payload.version,
        content=payload.content,
        snapshot=payload.snapshot,
        status=payload.status,
    )
    return Resp(data=_version_to_dict(row))


@router.post("/rollback/versions/{version_id}/rollback", response_model=Resp[dict])
def rollback_version(version_id: int, db: Session = Depends(get_db), _: User = Depends(require_admin)):
    """回滚 Agent artifact 版本。

    Args:
        version_id: 版本 ID。
        db: 数据库会话。
        _: 管理员用户。

    Returns:
        Resp[dict]: 回滚后的版本。
    """
    row = rollback_service.rollback_version(db, version_id)
    return Resp(data=_version_to_dict(row))


@router.get("/rewards/events", response_model=Resp[list[dict]])
def list_rewards(db: Session = Depends(get_db), _: User = Depends(require_admin)):
    """查询 Agent 奖励/惩罚事件。

    Args:
        db: 数据库会话。
        _: 管理员用户。

    Returns:
        Resp[list[dict]]: 奖惩事件列表。
    """
    rows = db.query(AgentRewardEvent).order_by(AgentRewardEvent.id.desc()).limit(100).all()
    return Resp(data=[{
        "id": row.id,
        "agent_code": row.agent_code,
        "event_type": row.event_type,
        "score": row.score,
        "reason": row.reason,
        "create_time": row.create_time.isoformat() if row.create_time else None,
    } for row in rows])


@router.post("/rewards/events", response_model=Resp[dict])
def create_reward(
    payload: AgentRewardCreateIn,
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
):
    """记录 Agent 奖励或惩罚事件。

    Args:
        payload: 奖惩输入。
        db: 数据库会话。
        _: 管理员用户。

    Returns:
        Resp[dict]: 奖惩事件。
    """
    row = reward_service.record_reward(
        db,
        agent_code=payload.agent_code,
        event_type=payload.event_type,
        score=payload.score,
        reason=payload.reason,
        impact=payload.impact,
    )
    return Resp(data={
        "id": row.id,
        "agent_code": row.agent_code,
        "event_type": row.event_type,
        "score": row.score,
        "reason": row.reason,
        "create_time": row.create_time.isoformat() if row.create_time else None,
    })


def _version_to_dict(row: AgentArtifactVersion) -> dict:
    """将 artifact 版本 ORM 对象转换为字典。

    Args:
        row: AgentArtifactVersion ORM 对象。

    Returns:
        dict: 版本字典。
    """
    return {
        "id": row.id,
        "agent_code": row.agent_code,
        "artifact_type": row.artifact_type,
        "version": row.version,
        "status": row.status,
        "content": row.content,
        "snapshot": row.snapshot,
        "create_time": row.create_time.isoformat() if row.create_time else None,
    }
