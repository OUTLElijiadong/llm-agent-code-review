"""自进化代理 EvolutionAgent — Agent 自进化慢环主体

职责(反思 → 生成提案):
1. 消费 feedback_service 的反馈聚合(采纳率/假阳性率/样本量)。
2. 高假阳性且样本足、跨任务的类型 → 生成「降级/禁用」提案(纯逻辑,无 LLM)。
3. 经验库中高权重、反复确认的真实问题 → 调 LLM 蒸馏出更精准的「新规则」提案。

防翻车:
- 触发提案需满足 min_samples + 跨 ≥min_distinct_tasks 任务的双门槛。
- 仅对「非内置」规则提 disable;内置规则只提 adjust_severity(降级),交人工闸门定夺。
- 所有产出均为候选提案(status=pending),默认不生效。

纯函数(generate_fp_proposals / downgrade_severity 等)便于单元测试,
LLM 蒸馏通过可注入的 distiller 解耦,测试时离线运行。
"""
import json
from typing import Callable, Optional

from loguru import logger
from sqlalchemy.orm import Session

from app.agents.base import AgentContext, AgentResult, BaseAgent
from app.models.evolution_proposal import EvolutionProposal
from app.models.review_rule import ReviewRule
from app.services import experience_service, feedback_service

# 严重度由高到低
SEVERITY_ORDER = ["严重", "高", "中", "低"]

# 开放(未决)提案状态:用于去重,避免重复堆积同一提案
_OPEN_STATUSES = ("pending", "eval_passed", "eval_failed")


def downgrade_severity(severity: str) -> Optional[str]:
    """返回低一级的严重度;已是最低(低)则返回 None(应改用禁用)"""
    try:
        idx = SEVERITY_ORDER.index(severity)
    except ValueError:
        idx = SEVERITY_ORDER.index("中")
    if idx >= len(SEVERITY_ORDER) - 1:
        return None
    return SEVERITY_ORDER[idx + 1]


def pick_representative_rule(rules: list[ReviewRule]) -> Optional[ReviewRule]:
    """从同类型规则中挑代表:严重度最高、其次 sort_order 最小"""
    if not rules:
        return None

    def _key(r: ReviewRule):
        try:
            sev_rank = SEVERITY_ORDER.index(getattr(r, "severity", "中") or "中")
        except ValueError:
            sev_rank = SEVERITY_ORDER.index("中")
        return (sev_rank, getattr(r, "sort_order", 0) or 0)

    return sorted(rules, key=_key)[0]


def generate_fp_proposals(
    stats: list[dict],
    rules: list[ReviewRule],
    *,
    min_samples: int = 20,
    min_distinct_tasks: int = 2,
    high_fp_rate: float = 0.6,
    disable_fp_rate: float = 0.8,
) -> list[dict]:
    """从反馈聚合生成「假阳性抑制」提案(纯函数,无 LLM/DB)

    双门槛防翻车:仅当 decided≥min_samples 且 ignored 跨 ≥min_distinct_tasks
    个任务,且假阳性率≥high_fp_rate 时才提案,避免单用户偷懒批量忽略误触发。

    Args:
        stats: feedback_service.aggregate_by_issue_type 的输出
        rules: 当前启用规则列表(ReviewRule)
        min_samples: 最小已决样本量
        min_distinct_tasks: 假阳性需跨越的最小任务数
        high_fp_rate: 触发降级的假阳性率阈值
        disable_fp_rate: 触发禁用(仅非内置)的假阳性率阈值

    Returns:
        list[dict]: 提案 dict,payload/evidence 为 Python dict(持久化时再 JSON 序列化)
    """
    rules_by_type: dict[str, list[ReviewRule]] = {}
    for r in rules:
        rules_by_type.setdefault(r.rule_type, []).append(r)

    proposals: list[dict] = []
    for s in stats:
        # 防翻车双门槛
        if s["decided"] < min_samples:
            continue
        if s["distinct_ignored_tasks"] < min_distinct_tasks:
            continue
        if s["false_positive_rate"] < high_fp_rate:
            continue

        rule = pick_representative_rule(rules_by_type.get(s["rule_type"], []))
        if rule is None:
            continue  # 无对应启用规则,无从下手

        evidence = {
            "issue_type": s["issue_type"],
            "false_positive_rate": s["false_positive_rate"],
            "decided": s["decided"],
            "ignored": s["ignored"],
            "distinct_ignored_tasks": s["distinct_ignored_tasks"],
            "distinct_ignored_users": s["distinct_ignored_users"],
        }

        is_builtin = bool(getattr(rule, "is_builtin", 0))
        # 禁用仅对非内置且假阳性极高;否则一律降级,交人工闸门
        if (not is_builtin) and s["false_positive_rate"] >= disable_fp_rate:
            proposals.append({
                "proposal_type": "disable_rule",
                "target_rule_id": rule.id,
                "title": f"禁用高噪声规则「{rule.rule_name}」(假阳性率 {s['false_positive_rate']:.0%})",
                "payload": {
                    "rule_id": rule.id,
                    "rule_code": rule.rule_code,
                    "action": "disable",
                },
                "evidence": evidence,
            })
            continue

        new_sev = downgrade_severity(getattr(rule, "severity", "中") or "中")
        if new_sev is None:
            # 已是最低级且为内置 → 不自动处理,仅在证据里体现,留给人工
            continue
        proposals.append({
            "proposal_type": "adjust_severity",
            "target_rule_id": rule.id,
            "title": f"下调规则「{rule.rule_name}」严重度 {rule.severity}→{new_sev}"
                     f"(假阳性率 {s['false_positive_rate']:.0%})",
            "payload": {
                "rule_id": rule.id,
                "rule_code": rule.rule_code,
                "from_severity": getattr(rule, "severity", "中") or "中",
                "to_severity": new_sev,
            },
            "evidence": evidence,
        })
    return proposals


class EvolutionAgent(BaseAgent):
    """自进化代理"""

    name = "evolution"
    description = "自进化代理:从审查反馈蒸馏规则进化提案,经闸门+审批后生效"
    icon = "evolution"
    color = "#E76F51"
    category = "meta"
    skills = ("反馈聚合", "假阳性抑制", "规则蒸馏", "提案生成")

    def __init__(
        self,
        min_samples: int = 20,
        min_distinct_tasks: int = 2,
        high_fp_rate: float = 0.6,
        disable_fp_rate: float = 0.8,
        new_rule_min_accepted: int = 3,
        max_new_rules: int = 3,
    ):
        # 先设置子类属性(供 _init_skills 使用,因 BaseAgent.__init__ 末尾会触发 _init_skills)
        self._db: Optional[Session] = None
        self._user = None
        self.min_samples = min_samples
        self.min_distinct_tasks = min_distinct_tasks
        self.high_fp_rate = high_fp_rate
        self.disable_fp_rate = disable_fp_rate
        self.new_rule_min_accepted = new_rule_min_accepted
        self.max_new_rules = max_new_rules
        self._self_improve_skill = None
        # 调 super().__init__(触发 _init_skills 挂载专属 Skill)
        super().__init__(temperature=0.2, max_tokens=1024)

    def _init_skills(self) -> None:
        """子类 override:挂载 EvolutionSelfImprovementSkill + EvolutionProactiveSkill

        将七步闭环逻辑下沉到 Skill,EvolutionAgent.run() 委托给 SelfImprovementSkill.evolve()。
        distiller 传入 self._distill_rule 供新规则蒸馏使用。
        """
        from app.agents.skills.evolution import (
            EvolutionProactiveSkill,
            EvolutionSelfImprovementSkill,
        )

        self.attach_skill(
            EvolutionSelfImprovementSkill(
                agent_name=self.name,
                distiller=self._distill_rule,
                min_samples=self.min_samples,
                min_distinct_tasks=self.min_distinct_tasks,
                high_fp_rate=self.high_fp_rate,
                disable_fp_rate=self.disable_fp_rate,
                new_rule_min_accepted=self.new_rule_min_accepted,
                max_new_rules=self.max_new_rules,
            )
        )
        self.attach_skill(EvolutionProactiveSkill(self.name))
        self._self_improve_skill = self._skills[0]

    def inject(self, db: Session, user=None) -> None:
        self._db = db
        self._user = user

    # ── 慢环主流程 ──

    def run(
        self,
        window_days: int = 90,
        distiller: Optional[Callable] = None,
        ctx: Optional[AgentContext] = None,
    ) -> AgentResult:
        """执行一轮进化(委托给 SelfImprovementSkill.evolve)

        保持原签名兼容,内部委托给 _self_improve_skill.evolve()。
        七步闭环逻辑(聚合反馈→生成提案→闸门→持久化)下沉到 Skill。

        Args:
            window_days: 反馈滑动窗口天数
            distiller: 可注入的新规则蒸馏器 callable(experience)->dict|None;
                传入则临时替换 Skill 的 distiller(测试用),默认用 self._distill_rule
            ctx: 上下文

        Returns:
            AgentResult: data = {proposals, created, skipped}
        """
        if not self._db:
            return AgentResult(success=False, error="DB 未注入")
        # 支持测试注入 distiller(临时替换 Skill 的 distiller)
        if distiller is not None and self._self_improve_skill is not None:
            self._self_improve_skill.distiller = distiller
        # 委托给 SelfImprovementSkill.evolve()(七步闭环:聚合→反思→闸门→持久化)
        skill_result = self._self_improve_skill.evolve(self._db, window_days, ctx)
        return AgentResult(
            success=skill_result.success,
            data=skill_result.data,
            error=skill_result.error,
            duration_ms=skill_result.duration_ms,
        )

    def _is_duplicate(self, db: Session, proposal: dict) -> bool:
        """同类未决提案去重:相同类型+目标规则,或相同新规则 rule_code"""
        q = db.query(EvolutionProposal).filter(
            EvolutionProposal.status.in_(_OPEN_STATUSES),
            EvolutionProposal.proposal_type == proposal["proposal_type"],
        )
        if proposal.get("target_rule_id") is not None:
            exists = q.filter(
                EvolutionProposal.target_rule_id == proposal["target_rule_id"],
            ).first()
            return exists is not None
        code = (proposal.get("payload") or {}).get("rule_code")
        if not code:
            return False
        for row in q.all():
            try:
                if json.loads(row.payload).get("rule_code") == code:
                    return True
            except (json.JSONDecodeError, TypeError):
                continue
        return False

    def _generate_new_rule_proposals(
        self, db: Session, rules: list[ReviewRule], distiller: Callable,
    ) -> list[dict]:
        """从高权重经验蒸馏新规则提案(LLM 通过 distiller 注入)"""
        existing_codes = {r.rule_code for r in rules}
        # 取高权重、反复确认的经验作为蒸馏素材
        experiences = experience_service.retrieve(
            db, language="", top_k=self.max_new_rules * 2, min_weight=float(self.new_rule_min_accepted) / 2,
        )
        experiences = [e for e in experiences if (e.accepted_count or 0) >= self.new_rule_min_accepted]

        proposals: list[dict] = []
        for exp in experiences[: self.max_new_rules]:
            try:
                rule = distiller(exp)
            except Exception as e:
                logger.warning(f"[evolution] 规则蒸馏失败,跳过: {e}")
                continue
            if not rule or not rule.get("rule_code") or not rule.get("rule_content"):
                continue
            if rule["rule_code"] in existing_codes:
                continue
            existing_codes.add(rule["rule_code"])
            proposals.append({
                "proposal_type": "new_rule",
                "target_rule_id": None,
                "title": f"新增规则「{rule.get('rule_name', rule['rule_code'])}」(源于 {exp.accepted_count} 次确认)",
                "payload": rule,
                "evidence": {
                    "source": "experience",
                    "issue_type": exp.issue_type,
                    "accepted_count": exp.accepted_count,
                    "weight": round(exp.weight or 0.0, 3),
                    "fingerprint": exp.fingerprint,
                },
            })
        return proposals

    def _distill_rule(self, exp) -> Optional[dict]:
        """调用 LLM,把一条高频确认经验蒸馏为更精准的审查规则(JSON)"""
        rule_type = feedback_service.ISSUE_TYPE_TO_RULE_TYPE.get(exp.issue_type, "correctness")
        prompt = (
            "你是代码审查规则工程师。下面是某团队代码审查中【反复出现且被开发者确认修复】的真实问题,"
            "请把它提炼成一条更精准、可执行的审查规则(供审查 Prompt 使用)。\n\n"
            f"问题类型: {exp.issue_type}\n"
            f"代表标题: {exp.title}\n"
            f"历史确认次数: {exp.accepted_count}\n"
            f"适用语言: {exp.language}\n"
            f"参考修复建议: {exp.canonical_suggestion or '(无)'}\n\n"
            "只输出 JSON,字段:\n"
            '{"rule_code":"英文小写下划线唯一标识","rule_name":"中文规则名(<=20字)",'
            f'"rule_type":"{rule_type}","rule_content":"一句话可执行的检查指令(<=80字)",'
            f'"language":"{exp.language}","severity":"严重|高|中|低"}}'
        )
        result = self.call_json(prompt)
        if not result.success or not isinstance(result.data, dict):
            return None
        data = result.data
        # 兜底字段
        data.setdefault("rule_type", rule_type)
        data.setdefault("language", exp.language or "*")
        data.setdefault("severity", "中")
        return data
