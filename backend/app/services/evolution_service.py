"""自进化服务 — 进化提案全生命周期编排

状态机:
    pending ──evaluate──▶ eval_passed / eval_failed
    eval_passed ──approve──▶ promoted ──rollback──▶ rolled_back
    任意未决 ──reject──▶ rejected

红线:
- promote 前必须通过评估闸门(require_eval=True)。
- 所有写入 review_rule 的动作都留 applied_snapshot 以支持一键回滚,并记 audit_log。
"""
import json
from datetime import datetime, timezone
from typing import Callable, Optional

from loguru import logger
from sqlalchemy.orm import Session

from app.core.exceptions import ConflictError, NotFoundError, ValidationError
from app.models.evolution_proposal import EvolutionProposal
from app.models.review_experience import ReviewExperience
from app.models.review_rule import ReviewRule
from app.models.user import User
from app.services import audit_service, eval_gate, experience_service, feedback_service


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ── 反馈与经验(只读) ──

def aggregate_feedback(db: Session, window_days: int = 90) -> dict:
    """反馈信号总览(采纳率/假阳性率/按类型明细)"""
    return feedback_service.summary(db, window_days)


def list_experiences(db: Session, limit: int = 50) -> list[ReviewExperience]:
    """经验库列表(按权重降序)"""
    return (
        db.query(ReviewExperience)
        .order_by(ReviewExperience.weight.desc())
        .limit(limit)
        .all()
    )


# ── 进化运行 ──

def run_evolution(
    db: Session, user: Optional[User] = None,
    window_days: int = 90, distiller: Optional[Callable] = None,
) -> dict:
    """先沉淀经验,再让 EvolutionAgent 产出候选提案

    Args:
        db: 数据库会话
        user: 触发者(用于审计)
        window_days: 反馈窗口
        distiller: 可注入的新规则蒸馏器(测试用)

    Returns:
        dict: {harvest, agent} 两部分结果
    """
    from app.agents.evolution_agent import EvolutionAgent

    harvest = experience_service.harvest(db, window_days=window_days)
    agent = EvolutionAgent()
    agent.inject(db, user=user)
    result = agent.run(window_days=window_days, distiller=distiller)

    audit_service.log(
        db, user, "evolution", target_type="evolution", target_id="run",
        detail=f"运行进化: 经验{harvest['clusters']}条, 新提案{result.data.get('created', 0)}个",
    )
    return {"harvest": harvest, "agent": result.data if result.success else {"error": result.error}}


# ── 提案查询 ──

def list_proposals(db: Session, status: str = "", limit: int = 100) -> list[EvolutionProposal]:
    q = db.query(EvolutionProposal)
    if status:
        q = q.filter(EvolutionProposal.status == status)
    return q.order_by(EvolutionProposal.id.desc()).limit(limit).all()


def get_proposal(db: Session, proposal_id: int) -> EvolutionProposal:
    p = db.get(EvolutionProposal, proposal_id)
    if not p:
        raise NotFoundError("进化提案不存在", code=40400)
    return p


# ── 评估闸门 ──

def evaluate_proposal(
    db: Session, proposal_id: int, reviewer: Optional[Callable] = None,
) -> EvolutionProposal:
    """在黄金集上跑闸门,写回 eval_score 与状态"""
    p = get_proposal(db, proposal_id)
    if p.status in ("promoted", "rolled_back", "rejected"):
        raise ValidationError("该提案已终结,不能重复评估", code=40001)
    score = eval_gate.run_gate(db, p, reviewer=reviewer)
    p.eval_score = json.dumps(score, ensure_ascii=False)
    p.status = "eval_passed" if score.get("passed") else "eval_failed"
    db.commit()
    db.refresh(p)
    return p


# ── 审批 / 驳回 / 回滚 ──

def approve_proposal(
    db: Session, admin: User, proposal_id: int,
    require_eval: bool = True, ip: str = "",
) -> EvolutionProposal:
    """审批通过 → 应用提案到 review_rule 并 promote(留快照供回滚)"""
    p = get_proposal(db, proposal_id)
    if p.status == "promoted":
        raise ValidationError("提案已生效", code=40001)
    if require_eval and p.status != "eval_passed":
        raise ValidationError("需先通过评估闸门(eval_passed)才能审批生效", code=40001)

    payload = _json_load(p.payload)
    snapshot = _apply_proposal(db, p, payload)

    p.status = "promoted"
    p.reviewed_by = admin.id
    p.reviewed_at = _utcnow()
    p.applied_snapshot = json.dumps(snapshot, ensure_ascii=False)
    p.applied_rule_id = snapshot.get("rule_id")
    db.commit()
    db.refresh(p)

    audit_service.log(
        db, admin, "evolution", target_type="proposal", target_id=proposal_id,
        detail=f"审批生效进化提案: {p.title}", ip=ip,
    )
    logger.info(f"[evolution] 提案#{proposal_id} 已 promote,applied_rule_id={p.applied_rule_id}")
    return p


def reject_proposal(
    db: Session, admin: User, proposal_id: int, note: str = "", ip: str = "",
) -> EvolutionProposal:
    p = get_proposal(db, proposal_id)
    if p.status in ("promoted", "rolled_back"):
        raise ValidationError("已生效或已回滚的提案不能驳回", code=40001)
    p.status = "rejected"
    p.reviewed_by = admin.id
    p.reviewed_at = _utcnow()
    p.note = (note or "")[:500]
    db.commit()
    db.refresh(p)
    audit_service.log(
        db, admin, "evolution", target_type="proposal", target_id=proposal_id,
        detail=f"驳回进化提案: {p.title}", ip=ip,
    )
    return p


def rollback_proposal(
    db: Session, admin: User, proposal_id: int, note: str = "", ip: str = "",
) -> EvolutionProposal:
    """回滚已生效提案 — 按 applied_snapshot 还原 review_rule"""
    p = get_proposal(db, proposal_id)
    if p.status != "promoted":
        raise ValidationError("只能回滚已生效(promoted)的提案", code=40001)
    snapshot = _json_load(p.applied_snapshot)
    _reverse_proposal(db, snapshot)

    p.status = "rolled_back"
    p.note = (note or "")[:500]
    db.commit()
    db.refresh(p)
    audit_service.log(
        db, admin, "evolution", target_type="proposal", target_id=proposal_id,
        detail=f"回滚进化提案: {p.title}", ip=ip,
    )
    logger.info(f"[evolution] 提案#{proposal_id} 已回滚")
    return p


# ── 应用 / 还原 内部实现 ──

def _apply_proposal(db: Session, p: EvolutionProposal, payload: dict) -> dict:
    """把提案落到 review_rule,返回供回滚的改动前快照"""
    ptype = p.proposal_type

    if ptype == "new_rule":
        code = payload.get("rule_code")
        if not code or not payload.get("rule_content"):
            raise ValidationError("新规则提案缺少 rule_code/rule_content", code=40001)
        dup = db.query(ReviewRule.id).filter(ReviewRule.rule_code == code).first()
        if dup:
            raise ConflictError(f"规则代码已存在: {code}", code=40901)
        max_order = (
            db.query(ReviewRule.sort_order)
            .order_by(ReviewRule.sort_order.desc()).first()
        )
        rule = ReviewRule(
            user_id=None,  # 全局生效(本团队习得的规则)
            rule_code=code,
            rule_name=payload.get("rule_name", code),
            rule_type=payload.get("rule_type", "correctness"),
            rule_content=payload["rule_content"],
            language=payload.get("language", "*") or "*",
            severity=payload.get("severity", "中") or "中",
            enabled=1, is_builtin=0,
            sort_order=(max_order[0] + 1) if max_order else 100,
        )
        db.add(rule)
        db.flush()
        return {"action": "new_rule", "rule_id": rule.id}

    rule_id = payload.get("rule_id")
    rule = db.get(ReviewRule, rule_id) if rule_id else None
    if not rule:
        raise NotFoundError("目标规则不存在", code=40400)

    if ptype == "disable_rule":
        snap = {"action": "disable_rule", "rule_id": rule.id, "prev_enabled": rule.enabled}
        rule.enabled = 0
        return snap

    if ptype == "adjust_severity":
        snap = {"action": "adjust_severity", "rule_id": rule.id, "prev_severity": rule.severity}
        rule.severity = payload.get("to_severity", rule.severity)
        return snap

    if ptype == "narrow_language":
        snap = {"action": "narrow_language", "rule_id": rule.id, "prev_language": rule.language}
        rule.language = payload.get("to_language", rule.language)
        return snap

    raise ValidationError(f"未知提案类型: {ptype}", code=40001)


def _reverse_proposal(db: Session, snapshot: dict) -> None:
    """按快照还原 review_rule"""
    action = snapshot.get("action")
    rule_id = snapshot.get("rule_id")
    if action == "new_rule":
        rule = db.get(ReviewRule, rule_id)
        if rule:
            db.delete(rule)  # 新增规则回滚 = 删除
        return
    rule = db.get(ReviewRule, rule_id)
    if not rule:
        return
    if action == "disable_rule":
        rule.enabled = snapshot.get("prev_enabled", 1)
    elif action == "adjust_severity":
        rule.severity = snapshot.get("prev_severity", rule.severity)
    elif action == "narrow_language":
        rule.language = snapshot.get("prev_language", rule.language)


def _json_load(text: Optional[str]) -> dict:
    try:
        data = json.loads(text) if text else {}
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, TypeError):
        return {}
