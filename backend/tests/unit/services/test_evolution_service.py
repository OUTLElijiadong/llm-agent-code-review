"""单元测试: 自进化服务全生命周期(运行/评估/审批/驳回/回滚/闸门强制)"""
import json

import pytest

from app.core.exceptions import ValidationError
from app.models.eval_case import EvalCase
from app.models.evolution_proposal import EvolutionProposal
from app.models.review_rule import ReviewRule
from app.services import evolution_service


def _seed_case(db):
    db.add(EvalCase(
        name="sqli", language="python", code="x",
        expected_issues=json.dumps([{"issue_type": "安全漏洞", "keyword": "注入"}]),
        enabled=1, source="seed",
    ))
    db.commit()


def _pass_reviewer(code, lang, rules):
    # 恒命中期望、无噪声 → 任何非新增删除的提案都不退化
    return [{"issue_type": "安全漏洞", "title": "SQL注入", "description": "拼接"}]


def test_run_evolution_creates_fp_proposal_and_dedups(db, admin_user, mk_rule, mk_issue):
    """高假阳性 + 双门槛 → 生成降级提案;二次运行去重不重复堆积"""
    mk_rule(db, rule_code="security", rule_type="security", severity="高", is_builtin=1)
    # 安全漏洞: 3 fixed / 17 ignored 跨 3 个任务 → fp=0.85, decided=20
    for i in range(3):
        mk_issue(db, issue_type="安全漏洞", status="fixed", task_id=1, handled_by=1)
    for i in range(17):
        mk_issue(db, issue_type="安全漏洞", status="ignored",
                 task_id=(i % 3) + 1, handled_by=(i % 2) + 1)

    out = evolution_service.run_evolution(db, admin_user, distiller=lambda exp: None)
    assert out["agent"]["created"] >= 1
    props = evolution_service.list_proposals(db)
    assert any(p.proposal_type == "adjust_severity" for p in props)

    # 二次运行:同类未决提案应被去重
    out2 = evolution_service.run_evolution(db, admin_user, distiller=lambda exp: None)
    assert out2["agent"]["created"] == 0
    assert out2["agent"]["skipped"] >= 1


def test_evaluate_approve_newrule_then_rollback(db, admin_user, mk_rule):
    """新规则:评估通过 → 审批写入全局规则 → 回滚删除"""
    _seed_case(db)
    mk_rule(db, rule_code="security", rule_type="security")  # 基线规则
    p = EvolutionProposal(
        proposal_type="new_rule", title="新增SQLi规则",
        payload=json.dumps({
            "rule_code": "evolved_sqli", "rule_name": "SQLi强化",
            "rule_type": "security", "rule_content": "禁止字符串拼接SQL",
            "language": "*", "severity": "严重",
        }),
        status="pending",
    )
    db.add(p)
    db.commit()

    # 评估闸门(注入离线 reviewer)→ 通过
    p = evolution_service.evaluate_proposal(db, p.id, reviewer=_pass_reviewer)
    assert p.status == "eval_passed"

    # 审批生效 → 全局规则被创建且启用
    p = evolution_service.approve_proposal(db, admin_user, p.id)
    assert p.status == "promoted" and p.applied_rule_id
    rule = db.get(ReviewRule, p.applied_rule_id)
    assert rule.enabled == 1 and rule.is_builtin == 0 and rule.user_id is None
    assert rule.rule_code == "evolved_sqli"

    # 回滚 → 新增规则被删除
    p = evolution_service.rollback_proposal(db, admin_user, p.id, note="撤回")
    assert p.status == "rolled_back"
    assert db.get(ReviewRule, rule.id) is None


def test_approve_requires_eval_gate(db, admin_user):
    """未过闸门不得审批生效(红线)"""
    p = EvolutionProposal(proposal_type="new_rule", title="x",
                          payload=json.dumps({"rule_code": "z", "rule_content": "c"}),
                          status="pending")
    db.add(p)
    db.commit()
    with pytest.raises(ValidationError):
        evolution_service.approve_proposal(db, admin_user, p.id)


def test_disable_then_rollback_restores_enabled(db, admin_user, mk_rule):
    """禁用提案:审批后规则禁用,回滚后恢复启用"""
    rule = mk_rule(db, rule_code="custom_noise", is_builtin=0, enabled=1)
    p = EvolutionProposal(
        proposal_type="disable_rule", target_rule_id=rule.id, title="禁用噪声规则",
        payload=json.dumps({"rule_id": rule.id}), status="eval_passed",
    )
    db.add(p)
    db.commit()

    evolution_service.approve_proposal(db, admin_user, p.id)
    assert db.get(ReviewRule, rule.id).enabled == 0

    evolution_service.rollback_proposal(db, admin_user, p.id)
    assert db.get(ReviewRule, rule.id).enabled == 1


def test_reject_sets_status(db, admin_user):
    p = EvolutionProposal(proposal_type="adjust_severity", target_rule_id=1, title="x",
                          payload=json.dumps({"rule_id": 1, "to_severity": "低"}),
                          status="pending")
    db.add(p)
    db.commit()
    p = evolution_service.reject_proposal(db, admin_user, p.id, note="不认可")
    assert p.status == "rejected" and p.note == "不认可" and p.reviewed_by == admin_user.id
