"""评估闸门 — Agent 自进化的「不退化」守门人

进化提案 promote 前,在 eval_case 黄金集上分别用「改动前规则」和
「改动后规则」复跑审查,比对召回率与噪声:
- 召回率不下降(锚点上的已知真问题仍能被发现)
- 噪声不上升(不会引入更多无关误报)
两者都满足才放行。无可用 eval_case 时直接判不通过(红线:无基准不准 promote)。

score_case / apply_proposal_to_rules 为纯函数,便于单测;
reviewer 通过参数注入,测试时离线运行,不调用真实 LLM。
"""
import json
import math
from dataclasses import dataclass
from typing import Callable, Optional

from loguru import logger
from sqlalchemy.orm import Session

from app.models.eval_case import EvalCase
from app.models.review_rule import ReviewRule

# 召回不退化容差(召回是安全关键指标,从严)
_RECALL_EPS = 1e-6

# 仅这些提案会改变「检出内容」,才对噪声把关;
# adjust_severity 只改严重度标签、不可能影响是否检出,故仅看召回,
# 避免被 LLM 逐次审查的非确定性波动(同代码两次产出略有差异)误伤。
_DETECTION_CHANGING = {"new_rule", "disable_rule", "narrow_language"}


@dataclass
class _RuleShim:
    """轻量规则替身 — 用于模拟「改动后」规则集,避免改动 ORM/Session 状态"""
    rule_code: str
    rule_name: str
    rule_type: str
    rule_content: str
    language: str = "*"
    severity: str = "中"


def _issue_matches(produced: dict, expected: dict) -> bool:
    """单条产出是否命中某期望项:类型一致且(无关键词 或 关键词出现在标题/描述)"""
    if (produced.get("issue_type") or "") != expected.get("issue_type"):
        return False
    kw = (expected.get("keyword") or "").strip().lower()
    if not kw:
        return True
    text = f"{produced.get('title','')} {produced.get('description','')}".lower()
    return kw in text


def score_case(produced: list[dict], expected: list[dict]) -> dict:
    """对单个用例打分(纯函数)

    Returns:
        dict: matched(命中的期望数) / expected_total / produced_total /
              noise(未命中任何期望的产出数) / recall
    """
    expected = expected or []
    produced = produced or []
    matched = 0
    for exp in expected:
        if any(_issue_matches(p, exp) for p in produced):
            matched += 1
    # noise:产出里不对应任何期望的条目(锚点用例视为潜在误报)
    noise = sum(
        1 for p in produced if not any(_issue_matches(p, exp) for exp in expected)
    )
    recall = matched / len(expected) if expected else 1.0
    return {
        "matched": matched,
        "expected_total": len(expected),
        "produced_total": len(produced),
        "noise": noise,
        "recall": round(recall, 4),
    }


def apply_proposal_to_rules(rules: list, proposal_type: str, payload: dict) -> list:
    """生成「改动后」规则集(纯函数,不触碰 ORM/Session)

    Args:
        rules: 基线规则(ReviewRule 或具备同名属性的对象)
        proposal_type: new_rule/disable_rule/adjust_severity/narrow_language
        payload: 提案 payload(已反序列化为 dict)

    Returns:
        list: 模拟改动后的规则集(原对象 + _RuleShim 混合)
    """
    def _shim(r) -> _RuleShim:
        return _RuleShim(
            rule_code=r.rule_code, rule_name=r.rule_name, rule_type=r.rule_type,
            rule_content=r.rule_content,
            language=getattr(r, "language", "*") or "*",
            severity=getattr(r, "severity", "中") or "中",
        )

    if proposal_type == "new_rule":
        after = list(rules)
        after.append(_RuleShim(
            rule_code=payload.get("rule_code", "evolved_rule"),
            rule_name=payload.get("rule_name", payload.get("rule_code", "evolved_rule")),
            rule_type=payload.get("rule_type", "correctness"),
            rule_content=payload.get("rule_content", ""),
            language=payload.get("language", "*") or "*",
            severity=payload.get("severity", "中") or "中",
        ))
        return after

    target_id = payload.get("rule_id")
    after = []
    for r in rules:
        if getattr(r, "id", None) == target_id:
            if proposal_type == "disable_rule":
                continue  # 模拟禁用 = 从规则集移除
            if proposal_type == "adjust_severity":
                s = _shim(r)
                s.severity = payload.get("to_severity", s.severity)
                after.append(s)
                continue
            if proposal_type == "narrow_language":
                s = _shim(r)
                s.language = payload.get("to_language", s.language)
                after.append(s)
                continue
        after.append(r)
    return after


def run_gate(
    db: Session,
    proposal,
    reviewer: Optional[Callable] = None,
    eval_cases: Optional[list] = None,
    stability_runs: int = 3,
) -> dict:
    """在黄金集上复跑并判定提案是否「不退化」

    Args:
        db: 数据库会话
        proposal: EvolutionProposal 对象
        reviewer: callable(code, language, rules) -> list[issue dict];
            默认走真实 LLM,测试可注入离线实现。
        eval_cases: 指定用例(默认取所有 enabled 的 eval_case)
        stability_runs: 每套规则对同一用例重复次数，范围 2-5。

    Returns:
        dict: 评分明细 + passed 布尔
    """
    if eval_cases is None:
        eval_cases = db.query(EvalCase).filter(EvalCase.enabled == 1).all()
    if not eval_cases:
        return {"passed": False, "reason": "no_eval_cases",
                "detail": "无可用黄金集,按红线不允许 promote"}

    reviewer = reviewer or _default_reviewer(db)
    stability_runs = max(2, min(5, int(stability_runs or 3)))
    baseline_rules = db.query(ReviewRule).filter(ReviewRule.enabled == 1).all()
    try:
        payload = json.loads(proposal.payload) if proposal.payload else {}
    except (json.JSONDecodeError, TypeError):
        payload = {}
    after_rules = apply_proposal_to_rules(baseline_rules, proposal.proposal_type, payload)

    recall_before = recall_after = 0.0
    noise_before = noise_after = 0
    stability_ok = True
    per_case = []
    for case in eval_cases:
        expected = _load_expected(case)
        lang = case.language or "*"
        try:
            before_outputs = [
                reviewer(case.code, lang, baseline_rules)
                for _ in range(stability_runs)
            ]
            after_outputs = [
                reviewer(case.code, lang, after_rules)
                for _ in range(stability_runs)
            ]
        except Exception as exc:
            logger.warning("[eval_gate] 黄金集调用失败: %s", exc)
            return {
                "passed": False,
                "reason": "review_failed",
                "detail": str(exc)[:500],
                "stability_runs": stability_runs,
                "stability_ok": False,
            }
        before_scores = [score_case(output, expected) for output in before_outputs]
        after_scores = [score_case(output, expected) for output in after_outputs]
        before = _conservative_score(before_scores)
        after = _conservative_score(after_scores)
        before_stable = len({_review_signature(output) for output in before_outputs}) == 1
        after_stable = len({_review_signature(output) for output in after_outputs}) == 1
        stability_ok = stability_ok and before_stable and after_stable
        recall_before += before["recall"]
        recall_after += after["recall"]
        noise_before += before["noise"]
        noise_after += after["noise"]
        per_case.append({
            "case": case.name,
            "before": before,
            "after": after,
            "before_runs": before_scores,
            "after_runs": after_scores,
            "before_stable": before_stable,
            "after_stable": after_stable,
        })

    n = len(eval_cases)
    recall_before /= n
    recall_after /= n

    # 噪声容差:吸收单条发现级别的 LLM 波动,真实大幅误报仍会被拦
    noise_tolerance = max(1, math.ceil(0.15 * n))
    recall_ok = recall_after >= recall_before - _RECALL_EPS
    detection_changing = proposal.proposal_type in _DETECTION_CHANGING
    noise_ok = (noise_after <= noise_before + noise_tolerance) if detection_changing else True
    passed = recall_ok and noise_ok and stability_ok
    result = {
        "passed": bool(passed),
        "cases": n,
        "recall_before": round(recall_before, 4),
        "recall_after": round(recall_after, 4),
        "recall_ok": recall_ok,
        "noise_before": noise_before,
        "noise_after": noise_after,
        "noise_tolerance": noise_tolerance,
        "noise_checked": detection_changing,
        "noise_ok": noise_ok,
        "stability_runs": stability_runs,
        "stability_ok": stability_ok,
        "per_case": per_case,
    }
    logger.info(
        f"[eval_gate] proposal#{getattr(proposal,'id','?')} {proposal.proposal_type} "
        f"recall {recall_before:.2f}→{recall_after:.2f} "
        f"noise {noise_before}→{noise_after} passed={passed}",
    )
    return result


def _review_signature(produced: list[dict]) -> str:
    """构造与返回顺序无关的稳定输出签名。"""
    normalized = sorted(
        (
            str(item.get("issue_type") or "").strip(),
            str(item.get("title") or "").strip(),
            str(item.get("description") or "").strip(),
        )
        for item in (produced or [])
        if isinstance(item, dict)
    )
    return json.dumps(normalized, ensure_ascii=False, separators=(",", ":"))


def _conservative_score(scores: list[dict]) -> dict:
    """重复结果使用最低召回、最高噪声，避免偶然好结果放行。"""
    first = scores[0]
    return {
        "matched": min(item["matched"] for item in scores),
        "expected_total": first["expected_total"],
        "produced_total": max(item["produced_total"] for item in scores),
        "noise": max(item["noise"] for item in scores),
        "recall": min(item["recall"] for item in scores),
    }


def _load_expected(case: EvalCase) -> list[dict]:
    try:
        data = json.loads(case.expected_issues) if case.expected_issues else []
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, TypeError):
        return []


def _default_reviewer(db: Session) -> Callable:
    """构造默认 reviewer:用 DeepSeek 单轮审查黄金集代码,产出问题 dict 列表

    闸门调用统一写入 ai_call_log(task_id=NULL,agent_label=eval_gate)。
    """
    from app.ai.deepseek_agent import DeepSeekAgent
    from app.ai.prompt_builder import build_prompt
    from app.ai.result_parser import parse

    agent = DeepSeekAgent()

    def _review(code: str, language: str, rules: list) -> list[dict]:
        system_prompt, user_prompt = build_prompt(
            language=language, file_name="eval_case", code=code, rules=rules,
        )
        try:
            raw, _ = agent.chat(
                system_prompt=system_prompt, user_prompt=user_prompt,
                db=db, task_id=None, user_id=None, file_id=None,
                chunk_index=None, agent_label="eval_gate",
            )
            parsed = parse(raw)
            return [
                {"issue_type": it.issue_type, "title": it.title or "",
                 "description": it.description or ""}
                for it in parsed.issues
            ]
        except Exception as e:
            logger.warning(f"[eval_gate] reviewer 调用失败: {e}")
            raise RuntimeError(f"黄金集模型调用失败: {e}") from e

    return _review
