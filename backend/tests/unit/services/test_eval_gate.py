"""单元测试: 评估闸门(自进化 不退化守门人)"""
import json

from app.models.eval_case import EvalCase
from app.models.evolution_proposal import EvolutionProposal
from app.services import eval_gate

# ── 纯函数 ──

def test_score_case_recall_and_noise():
    produced = [
        {"issue_type": "安全漏洞", "title": "SQL注入", "description": "拼接"},
        {"issue_type": "代码规范", "title": "缩进", "description": ""},
    ]
    expected = [{"issue_type": "安全漏洞", "keyword": "注入"}]
    s = eval_gate.score_case(produced, expected)
    assert s["matched"] == 1 and s["recall"] == 1.0
    assert s["noise"] == 1  # 代码规范那条是锚点外的噪声


def test_score_case_keyword_miss():
    produced = [{"issue_type": "安全漏洞", "title": "越权", "description": "无关"}]
    expected = [{"issue_type": "安全漏洞", "keyword": "注入"}]
    assert eval_gate.score_case(produced, expected)["recall"] == 0.0


def test_apply_proposal_new_rule_appends():
    from types import SimpleNamespace
    base = SimpleNamespace(id=1, rule_code="a", rule_name="A", rule_type="security",
                           rule_content="x", language="*", severity="高")
    after = eval_gate.apply_proposal_to_rules([base], "new_rule",
                                              {"rule_code": "b", "rule_content": "y"})
    assert len(after) == 2
    assert any(getattr(r, "rule_code", "") == "b" for r in after)


def test_apply_proposal_disable_removes(db, mk_rule):
    rule = mk_rule(db, rule_code="security")
    base = [rule]
    after = eval_gate.apply_proposal_to_rules(base, "disable_rule", {"rule_id": rule.id})
    assert after == []


# ── run_gate(注入离线 reviewer) ──

def _case(db):
    c = EvalCase(name="sqli", language="python", code="x",
                 expected_issues=json.dumps([{"issue_type": "安全漏洞", "keyword": "注入"}]),
                 enabled=1, source="seed")
    db.add(c)
    db.commit()
    return c


def test_run_gate_blocks_when_no_eval_cases(db):
    p = EvolutionProposal(proposal_type="new_rule", title="t",
                          payload=json.dumps({"rule_code": "x", "rule_content": "y"}))
    db.add(p)
    db.commit()
    res = eval_gate.run_gate(db, p, reviewer=lambda *a: [])
    assert res["passed"] is False and res["reason"] == "no_eval_cases"


def test_run_gate_passes_when_non_regressive(db, mk_rule):
    _case(db)
    mk_rule(db, rule_code="security")
    p = EvolutionProposal(proposal_type="new_rule", title="t",
                          payload=json.dumps({"rule_code": "extra", "rule_content": "y"}))
    db.add(p)
    db.commit()
    # reviewer 始终命中期望、无额外噪声 → 前后召回相同 → 放行
    def reviewer(code, lang, rules):
        return [{"issue_type": "安全漏洞", "title": "SQL注入", "description": "拼接"}]
    res = eval_gate.run_gate(db, p, reviewer=reviewer)
    assert res["passed"] is True
    assert res["recall_after"] >= res["recall_before"]


def test_run_gate_fails_when_recall_drops(db, mk_rule):
    """禁用规则导致锚点上的真问题漏检 → 闸门拦截"""
    _case(db)
    rule = mk_rule(db, rule_code="security")
    p = EvolutionProposal(proposal_type="disable_rule", target_rule_id=rule.id,
                          title="禁用", payload=json.dumps({"rule_id": rule.id}))
    db.add(p)
    db.commit()
    # reviewer 只在 security 规则在场时才发现该问题;禁用后漏检
    def reviewer(code, lang, rules):
        if any(getattr(r, "rule_code", "") == "security" for r in rules):
            return [{"issue_type": "安全漏洞", "title": "SQL注入", "description": "拼接"}]
        return []
    res = eval_gate.run_gate(db, p, reviewer=reviewer)
    assert res["passed"] is False
    assert res["recall_after"] < res["recall_before"]


def test_run_gate_severity_change_ignores_noise_variance(db, mk_rule):
    """adjust_severity 只改标签:即便 LLM 波动让噪声升高,只要召回不退化也应放行"""
    _case(db)
    rule = mk_rule(db, rule_code="code_style", rule_type="style", severity="中")
    p = EvolutionProposal(proposal_type="adjust_severity", target_rule_id=rule.id,
                          title="降级", payload=json.dumps({"rule_id": rule.id, "to_severity": "低"}))
    db.add(p)
    db.commit()

    # 降级后(规则集中出现"低"严重度)reviewer 多吐一条无关噪声,模拟 LLM 非确定性
    def reviewer(code, lang, rules):
        out = [{"issue_type": "安全漏洞", "title": "SQL注入", "description": "拼接"}]
        if any(getattr(r, "severity", "") == "低" for r in rules):
            out.append({"issue_type": "代码规范", "title": "风格", "description": ""})
        return out

    res = eval_gate.run_gate(db, p, reviewer=reviewer)
    assert res["noise_after"] > res["noise_before"]   # 噪声确实升高
    assert res["noise_checked"] is False              # 但标签类改动不检查噪声
    assert res["passed"] is True                      # 召回不退化 → 放行


def test_run_gate_new_rule_fails_on_large_noise(db, mk_rule):
    """检出类改动(new_rule)若引入大量误报,超过容差则拦截"""
    _case(db)
    mk_rule(db, rule_code="security")
    p = EvolutionProposal(proposal_type="new_rule", title="新规则",
                          payload=json.dumps({"rule_code": "extra", "rule_content": "y"}))
    db.add(p)
    db.commit()

    # 新规则在场(after)时狂吐误报
    def reviewer(code, lang, rules):
        out = [{"issue_type": "安全漏洞", "title": "SQL注入", "description": "拼接"}]
        if any(getattr(r, "rule_code", "") == "extra" for r in rules):
            out += [{"issue_type": "代码规范", "title": f"噪声{i}", "description": ""} for i in range(5)]
        return out

    res = eval_gate.run_gate(db, p, reviewer=reviewer)
    assert res["noise_checked"] is True
    assert res["passed"] is False


def test_run_gate_blocks_nondeterministic_reviewer_output(db, mk_rule):
    """同一输入重复结果不一致时，不能把偶然命中当成可发布改进。"""
    _case(db)
    mk_rule(db, rule_code="security")
    p = EvolutionProposal(
        proposal_type="new_rule",
        title="波动规则",
        payload=json.dumps({"rule_code": "extra", "rule_content": "y"}),
    )
    db.add(p)
    db.commit()
    call_count = 0

    def reviewer(code, lang, rules):
        nonlocal call_count
        call_count += 1
        if call_count % 2:
            return [{"issue_type": "安全漏洞", "title": "SQL注入", "description": "拼接"}]
        return []

    res = eval_gate.run_gate(db, p, reviewer=reviewer, stability_runs=3)

    assert res["stability_runs"] == 3
    assert res["stability_ok"] is False
    assert res["passed"] is False
