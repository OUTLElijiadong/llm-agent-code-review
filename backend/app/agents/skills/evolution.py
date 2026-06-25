"""EvolutionAgent 专属 Skill — AgentSkill 自进化与总调度升级

EvolutionSelfImprovementSkill: 复用现有 EvolutionAgent 的反馈聚合 + 假阳性提案 +
新规则蒸馏逻辑,产出 evolution_proposal(agent_name=evolution)。

EvolutionProactiveSkill: 主动监测采纳率/假阳性率趋势,触发进化或提问。

复用模块:
- feedback_service.aggregate_by_issue_type(反馈聚合)
- evolution_agent.generate_fp_proposals(假阳性提案纯函数)
- evolution_service._apply_proposal / rollback_proposal(应用与回滚)
- eval_gate(闸门评估,可选)
"""
import json
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional

from loguru import logger
from sqlalchemy.orm import Session

from app.agents.skills.self_improvement import SelfImprovementSkill
from app.agents.skills.proactive import ProactiveAction, ProactiveSkill

if TYPE_CHECKING:
    from app.agents.base import AgentContext


class EvolutionSelfImprovementSkill(SelfImprovementSkill):
    """evolution Agent 自进化 Skill

    复用现有 EvolutionAgent 的逻辑,进化对象为 review_rule。
    - aggregate_feedback: 复用 feedback_service.aggregate_by_issue_type
    - evolve_target: 复用 generate_fp_proposals + distiller(新规则蒸馏)
    - apply_proposal: 复用 evolution_service._apply_proposal

    Attributes:
        name: Skill 唯一标识 "evolution.self_improve"
        distiller: 新规则蒸馏器 callable(experience)->dict|None,默认 None(只产 fp 提案)
    """

    name = "evolution.self_improve"
    description = "从审查反馈蒸馏规则进化提案(新增/降级/收窄语言),复用 EvolutionAgent 七步闭环"

    def __init__(
        self,
        agent_name: str = "evolution",
        distiller: Optional[Callable] = None,
        min_samples: int = 20,
        min_distinct_tasks: int = 2,
        high_fp_rate: float = 0.6,
        disable_fp_rate: float = 0.8,
        new_rule_min_accepted: int = 3,
        max_new_rules: int = 3,
    ):
        """初始化 evolution 自进化 Skill

        Args:
            agent_name: Agent name(默认 evolution)
            distiller: 新规则蒸馏器 callable(ReviewExperience)->dict|None;
                默认 None 表示只产假阳性提案,不蒸馏新规则
            min_samples: 触发提案的最小已决样本量
            min_distinct_tasks: 触发提案需跨越的最小任务数
            high_fp_rate: 触发降级的假阳性率阈值
            disable_fp_rate: 触发禁用的假阳性率阈值
            new_rule_min_accepted: 蒸馏新规则的最小确认次数
            max_new_rules: 单轮最多蒸馏新规则数
        """
        super().__init__(
            agent_name,
            min_samples=min_samples,
            min_distinct_tasks=min_distinct_tasks,
            high_fp_rate=high_fp_rate,
            disable_fp_rate=disable_fp_rate,
        )
        self.distiller = distiller
        self.new_rule_min_accepted = new_rule_min_accepted
        self.max_new_rules = max_new_rules

    def aggregate_feedback(
        self, db: Session, window_days: int
    ) -> List[Dict[str, Any]]:
        """聚合反馈信号(复用 feedback_service.aggregate_by_issue_type)

        Args:
            db: 数据库会话
            window_days: 滑动窗口

        Returns:
            list[dict]: 聚合后的信号列表
        """
        from app.services import feedback_service

        return feedback_service.aggregate_by_issue_type(db, window_days)

    def evolve_target(
        self, db: Session, stats: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """从聚合信号产出候选提案(复用 generate_fp_proposals + distiller)

        Args:
            db: 数据库会话(只读,查询现有 ReviewRule)
            stats: aggregate_feedback 的输出

        Returns:
            list[dict]: 候选提案(fp 提案 + 新规则提案)
        """
        # 延迟 import 避免循环依赖
        from app.agents.evolution_agent import generate_fp_proposals
        from app.models.review_rule import ReviewRule
        from app.services import experience_service

        rules = db.query(ReviewRule).filter(ReviewRule.enabled == 1).all()

        # 1. 假阳性提案(纯函数)
        fp_proposals = generate_fp_proposals(
            stats,
            rules,
            min_samples=self.min_samples,
            min_distinct_tasks=self.min_distinct_tasks,
            high_fp_rate=self.high_fp_rate,
            disable_fp_rate=self.disable_fp_rate,
        )

        # 2. 新规则提案(需 distiller,默认不产)
        new_rule_proposals: List[Dict[str, Any]] = []
        if self.distiller is not None:
            new_rule_proposals = self._generate_new_rule_proposals(
                db, rules, self.distiller
            )

        return fp_proposals + new_rule_proposals

    def _generate_new_rule_proposals(
        self,
        db: Session,
        rules: list,
        distiller: Callable,
    ) -> List[Dict[str, Any]]:
        """从高权重经验蒸馏新规则提案

        复用 EvolutionAgent._generate_new_rule_proposals 逻辑,通过 distiller 注入 LLM 调用。

        Args:
            db: 数据库会话
            rules: 当前启用规则列表(用于去重)
            distiller: 蒸馏器 callable(ReviewExperience)->dict|None

        Returns:
            list[dict]: 新规则提案列表
        """
        from app.services import experience_service

        existing_codes = {r.rule_code for r in rules}
        experiences = experience_service.retrieve(
            db,
            language="",
            top_k=self.max_new_rules * 2,
            min_weight=float(self.new_rule_min_accepted) / 2,
        )
        experiences = [
            e for e in experiences
            if (e.accepted_count or 0) >= self.new_rule_min_accepted
        ]

        proposals: List[Dict[str, Any]] = []
        for exp in experiences[: self.max_new_rules]:
            try:
                rule = distiller(exp)
            except Exception as e:
                logger.warning(f"[{self.name}] 规则蒸馏失败,跳过: {e}")
                continue
            if not rule or not rule.get("rule_code") or not rule.get("rule_content"):
                continue
            if rule["rule_code"] in existing_codes:
                continue
            existing_codes.add(rule["rule_code"])
            proposals.append({
                "proposal_type": "new_rule",
                "target_rule_id": None,
                "title": (
                    f"新增规则「{rule.get('rule_name', rule['rule_code'])}」"
                    f"(源于 {exp.accepted_count} 次确认)"
                ),
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

    def apply_proposal(self, db: Session, proposal: Dict[str, Any]) -> int:
        """应用提案到进化对象(复用 evolution_service._apply_proposal)

        Args:
            db: 数据库会话
            proposal: 已审批通过的提案 dict

        Returns:
            int: affected_id(如 review_rule.id)
        """
        from app.models.evolution_proposal import EvolutionProposal
        from app.services import evolution_service

        # 查找 proposal 对应的 EvolutionProposal 记录
        proposal_id = proposal.get("proposal_id", 0)
        if proposal_id:
            p = evolution_service.get_proposal(db, proposal_id)
            if p is None:
                return 0
            result = evolution_service._apply_proposal(
                db, p, json.loads(p.payload or "{}")
            )
            return result.get("applied_rule_id", 0) or 0
        return 0


class EvolutionProactiveSkill(ProactiveSkill):
    """evolution Agent 主动行为 Skill

    主动监测采纳率/假阳性率趋势,触发进化或提问;从 ai_call_log 反思进化效果。

    Attributes:
        name: Skill 唯一标识 "evolution.proactive"
    """

    name = "evolution.proactive"
    description = "主动监测进化效果与反馈趋势,触发新一轮进化或提问建议"

    def __init__(self, agent_name: str = "evolution"):
        """初始化 evolution 主动行为 Skill

        Args:
            agent_name: Agent name(默认 evolution)
        """
        super().__init__(agent_name)

    def check_proactive(
        self, db: Session, ctx: Optional["AgentContext"] = None
    ) -> List[ProactiveAction]:
        """扫描近 7 天的进化指标,若假阳性率突增则触发进化或提问

        Args:
            db: 数据库会话
            ctx: Agent 上下文

        Returns:
            list[ProactiveAction]: 建议行动列表
        """
        from app.services import feedback_service

        actions: List[ProactiveAction] = []
        try:
            stats = feedback_service.aggregate_by_issue_type(db, window_days=7)
            high_fp_count = sum(
                1 for s in stats if s.get("false_positive_rate", 0) >= 0.6
            )
            if high_fp_count > 0:
                actions.append(ProactiveAction(
                    action_type="trigger_evolution",
                    priority="high",
                    title=f"检测到 {high_fp_count} 类规则假阳性率偏高",
                    detail="近 7 天反馈聚合显示部分规则假阳性率≥60%,建议触发一轮自进化",
                    payload={
                        "agent_name": self.agent_name,
                        "window_days": 7,
                        "high_fp_count": high_fp_count,
                    },
                ))
        except Exception as e:
            logger.warning(f"[{self.name}] check_proactive 异常: {e}")
        return actions

    def should_trigger_evolution(self, stats: Dict[str, Any]) -> bool:
        """主动进化触发判定

        Args:
            stats: 关键指标(如 {"high_fp_count": 3, "pending_proposals": 5})

        Returns:
            bool: high_fp_count≥1 或 pending_proposals≥10 时触发
        """
        return (
            stats.get("high_fp_count", 0) >= 1
            or stats.get("pending_proposals", 0) >= 10
        )

    def scan_domain(self, db: Session) -> List[Dict[str, Any]]:
        """主动巡检:找出近 7 天堆积的 pending 提案

        Args:
            db: 数据库会话

        Returns:
            list[dict]: 待审批提案列表
        """
        from app.models.evolution_proposal import EvolutionProposal

        try:
            rows = (
                db.query(EvolutionProposal)
                .filter(EvolutionProposal.status == "pending")
                .order_by(EvolutionProposal.create_time.desc())
                .limit(20)
                .all()
            )
            return [
                {
                    "id": r.id,
                    "title": r.title,
                    "proposal_type": r.proposal_type,
                    "agent_name": r.agent_name,
                    "create_time": str(r.create_time),
                }
                for r in rows
            ]
        except Exception as e:
            logger.warning(f"[{self.name}] scan_domain 异常: {e}")
            return []

    def reflect_from_logs(
        self, db: Session, window_days: int = 7
    ) -> List[Dict[str, Any]]:
        """从 ai_call_log 反思进化效果

        Args:
            db: 数据库会话
            window_days: 反思窗口

        Returns:
            list[dict]: 学习到的候选改进点
        """
        from app.models.ai_call_log import AiCallLog
        from datetime import datetime, timedelta

        try:
            cutoff = datetime.utcnow() - timedelta(days=window_days)
            # 统计 evolution agent 近 window_days 的调用成功率
            rows = (
                db.query(AiCallLog)
                .filter(
                    AiCallLog.agent_label == "evolution",
                    AiCallLog.create_time >= cutoff,
                )
                .all()
            )
            total = len(rows)
            if total == 0:
                return []
            success = sum(1 for r in rows if r.status == "success")
            fail_rate = 1 - (success / total)
            reflections: List[Dict[str, Any]] = []
            if fail_rate > 0.3:
                reflections.append({
                    "finding": "evolution 调用失败率偏高",
                    "fail_rate": round(fail_rate, 2),
                    "suggestion": "检查 LLM 蒸馏链路稳定性,考虑增加重试",
                })
            return reflections
        except Exception as e:
            logger.warning(f"[{self.name}] reflect_from_logs 异常: {e}")
            return []
