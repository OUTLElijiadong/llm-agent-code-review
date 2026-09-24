"""单元测试: EvolutionAgent(规则蒸馏 + 提案生成)"""
import json
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import Mock

from app.agents.base import AgentResult
from app.agents.evolution_agent import (
    _EXPERIENCE_SUMMARY_PROMPT,
    EvolutionAgent,
    downgrade_severity,
    generate_fp_proposals,
    pick_representative_rule,
)
from app.core.config import settings
from app.models.review_rule import ReviewRule
from app.services import experience_service

# ── 纯函数 ──

def test_downgrade_severity():
    assert downgrade_severity("严重") == "高"
    assert downgrade_severity("中") == "低"
    assert downgrade_severity("低") is None


def test_pick_representative_rule_prefers_high_severity():
    a = ReviewRule(rule_code="a", rule_name="A", rule_type="security",
                   rule_content="x", severity="中", sort_order=1)
    b = ReviewRule(rule_code="b", rule_name="B", rule_type="security",
                   rule_content="x", severity="严重", sort_order=9)
    assert pick_representative_rule([a, b]).rule_code == "b"


def _stat(**kw):
    base = {"issue_type": "安全漏洞", "rule_type": "security", "fixed": 3,
            "ignored": 17, "decided": 20, "acceptance_rate": 0.15,
            "false_positive_rate": 0.85, "distinct_tasks": 4,
            "distinct_ignored_tasks": 3, "distinct_ignored_users": 2}
    base.update(kw)
    return base


def _rule(rid=1, **kw):
    r = ReviewRule(rule_code=kw.get("rule_code", "security"), rule_name="安全漏洞",
                   rule_type="security", rule_content="x",
                   severity=kw.get("severity", "高"),
                   is_builtin=kw.get("is_builtin", 1), sort_order=1)
    r.id = rid
    return r


def test_fp_builtin_proposes_downgrade_not_disable():
    props = generate_fp_proposals([_stat()], [_rule(is_builtin=1)])
    assert len(props) == 1
    assert props[0]["proposal_type"] == "adjust_severity"


def test_fp_custom_extreme_proposes_disable():
    props = generate_fp_proposals([_stat(false_positive_rate=0.9)],
                                  [_rule(is_builtin=0, rule_code="custom")])
    assert props[0]["proposal_type"] == "disable_rule"


def test_fp_double_threshold_blocks_small_sample():
    assert generate_fp_proposals([_stat(decided=5)], [_rule()]) == []


def test_fp_double_threshold_blocks_single_task():
    assert generate_fp_proposals([_stat(distinct_ignored_tasks=1)], [_rule()]) == []


def test_fp_low_fp_rate_no_proposal():
    assert generate_fp_proposals([_stat(false_positive_rate=0.2)], [_rule()]) == []


# ── run() 端到端(注入离线 distiller) ──

def test_run_distills_new_rule_and_dedups(db, mk_issue):
    """高频确认的经验 → 蒸馏新规则提案;二次运行按 rule_code 去重"""
    now = datetime.now(timezone.utc)
    for i in range(3):
        mk_issue(db, issue_type="性能问题", status="fixed", task_id=i,
                 title="循环内查询", handled_at=now)
    experience_service.harvest(db, now=now)

    def fake_distiller(exp):
        return {"rule_code": "perf_loop_query", "rule_name": "循环内查询",
                "rule_type": "performance", "rule_content": "禁止在循环体内执行查询",
                "language": "*", "severity": "中"}

    agent = EvolutionAgent()
    agent.inject(db)
    r1 = agent.run(distiller=fake_distiller)
    assert r1.data["proposals"] >= 1
    assert r1.data["created"] >= 1

    # 二次运行:同 rule_code 的未决提案应被去重
    r2 = agent.run(distiller=fake_distiller)
    assert r2.data["created"] == 0


def test_model_distillation_failure_fails_whole_run_and_restores_distiller(db, mk_issue, monkeypatch):
    now = datetime.now(timezone.utc)
    for i in range(3):
        mk_issue(db, issue_type="性能问题", status="fixed", task_id=i,
                 title="循环内查询", handled_at=now)
    experience_service.harvest(db, now=now)

    agent = EvolutionAgent()
    agent.inject(db)
    default_distiller = agent._self_improve_skill.distiller
    monkeypatch.setattr(agent, "call_json", lambda _prompt, **_kwargs: AgentResult(
        success=False, error="输入超过上下文", failure_kind="input_exceeds_context",
    ))
    result = agent.run()
    assert result.success is False
    assert result.data == {}
    assert "input_exceeds_context" in result.error
    assert agent._self_improve_skill.distiller == default_distiller

    def broken(_exp):
        raise RuntimeError("蒸馏提供方不可用")

    result = agent.run(distiller=broken)
    assert result.success is False
    assert "蒸馏提供方不可用" in result.error
    assert agent._self_improve_skill.distiller == default_distiller


def test_long_experience_suggestion_compacts_every_source_before_rule(monkeypatch):
    monkeypatch.setattr(settings, "deepseek_context_window_tokens", 12_000)
    agent = EvolutionAgent()
    segments: list[str] = []
    final_prompts: list[str] = []

    def model(prompt, **kwargs):
        if kwargs.get("system_prompt") == _EXPERIENCE_SUMMARY_PROMPT:
            source = json.loads(prompt)
            segments.append(source["text"])
            return AgentResult(success=True, data={
                "source_id": source["source_id"], "summary": "修复建议来源片段",
                "quote": source["text"][:15],
            })
        final_prompts.append(prompt)
        return AgentResult(success=True, data={
            "rule_code": "avoid_loop_query", "rule_content": "循环内禁止重复查询",
        })

    agent.call_json = Mock(side_effect=model)
    original = "在循环内查询会增加开销。" * 1_000 + "结尾应改为批量查询。"
    exp = SimpleNamespace(
        issue_type="性能问题", title="循环内重复查询", accepted_count=3,
        language="python", canonical_suggestion=original,
    )
    rule = agent._distill_rule(exp)
    assert rule["rule_code"] == "avoid_loop_query"
    assert len(segments) > 1
    assert "".join(segments) == original
    assert final_prompts and all(f"E{index:03d}-" in final_prompts[0]
                                 for index in range(1, len(segments) + 1))
