"""Agent 自进化 API 路由

治理类接口,统一要求管理员权限(写入全局规则 + 人工闸门)。

v3.0 AgentSkill 升级新增:
- POST /evolution/trigger: 通过 Orchestrator.trigger_evolution 触发指定 Agent 的自进化,
  trigger_type="manual" 写 agent_skill_record
"""
from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.dependencies import require_admin
from app.models.eval_case import EvalCase
from app.models.user import User
from app.schemas.common import Resp
from app.schemas.evolution import (
    EvalCaseOut,
    ExperienceOut,
    ProposalOut,
    RejectIn,
    RunIn,
)
from app.services import evolution_service

router = APIRouter()


def _client_ip(request: Request) -> str:
    """从 Request 提取客户端 IP(优先 x-forwarded-for)

    Args:
        request: FastAPI Request 对象

    Returns:
        str: 客户端 IP 字符串
    """
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else ""


@router.get("/feedback", response_model=Resp[dict])
def feedback_summary(window_days: int = 90, db: Session = Depends(get_db),
                     _: User = Depends(require_admin)):
    """反馈信号总览:采纳率/假阳性率/按问题类型明细"""
    return Resp(data=evolution_service.aggregate_feedback(db, window_days))


@router.get("/experiences", response_model=Resp[list[ExperienceOut]])
def list_experiences(limit: int = 50, db: Session = Depends(get_db),
                     _: User = Depends(require_admin)):
    """经验记忆库(按权重降序)"""
    rows = evolution_service.list_experiences(db, limit)
    return Resp(data=[ExperienceOut.model_validate(r) for r in rows])


@router.get("/eval-cases", response_model=Resp[list[EvalCaseOut]])
def list_eval_cases(db: Session = Depends(get_db), _: User = Depends(require_admin)):
    """黄金回归集列表"""
    rows = db.query(EvalCase).order_by(EvalCase.id.asc()).all()
    return Resp(data=[EvalCaseOut.model_validate(r) for r in rows])


@router.post("/run", response_model=Resp[dict])
def run_evolution(payload: RunIn, request: Request, db: Session = Depends(get_db),
                  admin: User = Depends(require_admin)):
    """触发一轮进化:沉淀经验 + 生成候选提案(默认不生效)"""
    data = evolution_service.run_evolution(db, admin, window_days=payload.window_days)
    return Resp(data=data)


@router.get("/proposals", response_model=Resp[list[ProposalOut]])
def list_proposals(status: str = "", limit: int = 100, db: Session = Depends(get_db),
                   _: User = Depends(require_admin)):
    """进化提案列表(可按状态过滤)"""
    rows = evolution_service.list_proposals(db, status, limit)
    return Resp(data=[ProposalOut.model_validate(r) for r in rows])


@router.get("/proposals/{proposal_id}", response_model=Resp[ProposalOut])
def get_proposal(proposal_id: int, db: Session = Depends(get_db),
                 _: User = Depends(require_admin)):
    """进化提案详情"""
    p = evolution_service.get_proposal(db, proposal_id)
    return Resp(data=ProposalOut.model_validate(p))


@router.post("/proposals/{proposal_id}/evaluate", response_model=Resp[ProposalOut])
def evaluate_proposal(proposal_id: int, db: Session = Depends(get_db),
                      _: User = Depends(require_admin)):
    """在黄金集上跑评估闸门,写回跑分与状态"""
    p = evolution_service.evaluate_proposal(db, proposal_id)
    return Resp(data=ProposalOut.model_validate(p))


@router.post("/proposals/{proposal_id}/approve", response_model=Resp[ProposalOut])
def approve_proposal(proposal_id: int, request: Request, db: Session = Depends(get_db),
                     admin: User = Depends(require_admin)):
    """审批生效(需先通过闸门)→ 写入 review_rule,下次审查自动生效"""
    p = evolution_service.approve_proposal(db, admin, proposal_id, ip=_client_ip(request))
    return Resp(data=ProposalOut.model_validate(p))


@router.post("/proposals/{proposal_id}/reject", response_model=Resp[ProposalOut])
def reject_proposal(proposal_id: int, payload: RejectIn, request: Request,
                    db: Session = Depends(get_db), admin: User = Depends(require_admin)):
    """驳回提案"""
    p = evolution_service.reject_proposal(db, admin, proposal_id, payload.note,
                                          ip=_client_ip(request))
    return Resp(data=ProposalOut.model_validate(p))


@router.post("/proposals/{proposal_id}/rollback", response_model=Resp[ProposalOut])
def rollback_proposal(proposal_id: int, payload: RejectIn, request: Request,
                      db: Session = Depends(get_db), admin: User = Depends(require_admin)):
    """回滚已生效提案 — 按快照还原 review_rule"""
    p = evolution_service.rollback_proposal(db, admin, proposal_id, payload.note,
                                            ip=_client_ip(request))
    return Resp(data=ProposalOut.model_validate(p))


# =================== v3.0 AgentSkill 升级: per-Agent 自进化触发 ===================


@router.post("/trigger", response_model=Resp[dict])
def trigger_evolution(
    request: Request,
    agent_name: str = Query(
        "evolution",
        description="目标 Agent name(如 code_reviewer / security_sentinel / evolution)",
    ),
    window_days: int = Query(
        90, ge=1, le=365, description="反馈窗口天数(1-365, 默认 90)",
    ),
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
):
    """v3.0: 触发指定 Agent 的自进化(admin only)

    通过 Orchestrator.trigger_evolution 调用 {agent_name}.self_improve action=evolve,
    trigger_type="manual" 自动写 agent_skill_record 与 audit_log。

    与 /api/evolution/run 的区别:
        - /run: 调用 evolution_service.run_evolution(全局进化流程, 不区分 Agent)
        - /trigger: 调用 Orchestrator.trigger_evolution(per-Agent 自进化, 写 skill 记录)

    Args:
        request: FastAPI Request(用于审计 IP)
        agent_name: 目标 Agent name(默认 evolution)
        window_days: 反馈窗口天数(1-365, 默认 90)
        db: 数据库会话
        admin: 当前管理员用户(由 require_admin 注入)

    Returns:
        Resp[dict]: 自进化执行结果, 含 success/data/error/duration_ms
    """
    from loguru import logger

    from app.agents.base import AgentContext
    from app.agents.orchestrator import get_request_orchestrator

    logger.info(
        f"[evolution.trigger] admin={admin.id} agent={agent_name} "
        f"window={window_days}d ip={_client_ip(request)}"
    )

    orch = get_request_orchestrator(db, user=admin)
    ctx = AgentContext(
        user_id=admin.id,
        extra={
            "api": "evolution.trigger",
            "admin_id": admin.id,
            "client_ip": _client_ip(request),
        },
    )

    # Orchestrator.trigger_evolution 内部调用 invoke_skill,
    # 但其 trigger_type 默认为 "orchestrator", 这里需要 "manual" 以便审计归类
    # 所以直接调用 invoke_skill 而非 trigger_evolution
    skill_name = f"{agent_name}.self_improve"
    result = orch.invoke_skill(
        agent_name=agent_name,
        skill_name=skill_name,
        params={"action": "evolve", "window_days": window_days},
        ctx=ctx,
        trigger_type="manual",
        trigger_source=f"api:POST /evolution/trigger?agent_name={agent_name}",
    )

    data = result.data if isinstance(result.data, dict) else {}
    return Resp(data={
        "success": result.success,
        "data": data.get("data"),
        "error": result.error or data.get("error"),
        "effect": data.get("effect", "success" if result.success else "failed"),
        "duration_ms": data.get("duration_ms", result.duration_ms),
        "record_id": data.get("record_id"),
        "agent_name": agent_name,
        "skill_name": skill_name,
        "window_days": window_days,
    })
