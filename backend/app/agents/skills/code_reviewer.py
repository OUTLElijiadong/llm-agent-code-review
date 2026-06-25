"""code_reviewer Agent 专属 Skill — 代码审查 Agent 自进化与主动监测

CodeReviewerSelfImprovementSkill: 复用 feedback_service 聚合审查反馈 +
generate_fp_proposals 产出假阳性规则降级/禁用提案,进化对象为 review_rule
(agent_name=code_reviewer)。

CodeReviewerProactiveSkill: 主动监测近 7 天假阳性率趋势,触发进化或提问。

复用模块:
- feedback_service.aggregate_by_issue_type(反馈聚合)
- evolution_agent.generate_fp_proposals(假阳性提案纯函数)
- evolution_service._apply_proposal(提案应用)
"""
import json
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from loguru import logger
from sqlalchemy.orm import Session

from app.agents.skills.self_improvement import SelfImprovementSkill
from app.agents.skills.proactive import ProactiveAction, ProactiveSkill

if TYPE_CHECKING:
    from app.agents.base import AgentContext


class CodeReviewerSelfImprovementSkill(SelfImprovementSkill):
    """code_reviewer Agent 自进化 Skill

    复用 feedback_service 聚合审查反馈,复用 generate_fp_proposals 产出
    假阳性规则降级/禁用提案,进化对象为 review_rule。

    Attributes:
        name: Skill 唯一标识 "code_reviewer.self_improve"
    """

    name = "code_reviewer.self_improve"
    description = "从审查反馈蒸馏规则进化提案(新增/降级/收窄语言),进化 review_rule"

    def __init__(
        self,
        agent_name: str = "code_reviewer",
        min_samples: int = 20,
        min_distinct_tasks: int = 2,
        high_fp_rate: float = 0.6,
        disable_fp_rate: float = 0.8,
    ):
        """初始化 code_reviewer 自进化 Skill

        Args:
            agent_name: Agent name(默认 code_reviewer)
            min_samples: 触发提案的最小已决样本量(防翻车双门槛之一)
            min_distinct_tasks: 触发提案需跨越的最小任务数(防翻车双门槛之二)
            high_fp_rate: 触发降级的假阳性率阈值
            disable_fp_rate: 触发禁用的假阳性率阈值
        """
        super().__init__(
            agent_name,
            min_samples=min_samples,
            min_distinct_tasks=min_distinct_tasks,
            high_fp_rate=high_fp_rate,
            disable_fp_rate=disable_fp_rate,
        )

    def aggregate_feedback(
        self, db: Session, window_days: int
    ) -> List[Dict[str, Any]]:
        """聚合审查反馈信号(复用 feedback_service.aggregate_by_issue_type)

        Args:
            db: 数据库会话
            window_days: 滑动窗口

        Returns:
            list[dict]: 聚合后的信号列表(按 issue_type 分组,含假阳性率等)
        """
        from app.services import feedback_service

        return feedback_service.aggregate_by_issue_type(db, window_days)

    def evolve_target(
        self, db: Session, stats: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """从聚合信号产出候选提案(复用 generate_fp_proposals)

        查询当前启用的 ReviewRule,基于假阳性率产出降级/禁用提案。

        Args:
            db: 数据库会话(只读,查询现有 ReviewRule)
            stats: aggregate_feedback 的输出

        Returns:
            list[dict]: 候选提案(假阳性降级/禁用提案)
        """
        from app.agents.evolution_agent import generate_fp_proposals
        from app.models.review_rule import ReviewRule

        rules = db.query(ReviewRule).filter(ReviewRule.enabled == 1).all()
        proposals = generate_fp_proposals(
            stats,
            rules,
            min_samples=self.min_samples,
            min_distinct_tasks=self.min_distinct_tasks,
            high_fp_rate=self.high_fp_rate,
            disable_fp_rate=self.disable_fp_rate,
        )
        return proposals

    def apply_proposal(self, db: Session, proposal: Dict[str, Any]) -> int:
        """应用提案到 review_rule(复用 evolution_service._apply_proposal)

        Args:
            db: 数据库会话
            proposal: 已审批通过的提案 dict(需含 proposal_id)

        Returns:
            int: affected_id(如 review_rule.id),提案不存在或失败返回 0
        """
        from app.services import evolution_service

        proposal_id = proposal.get("proposal_id", 0)
        if not proposal_id:
            return 0
        p = evolution_service.get_proposal(db, proposal_id)
        if p is None:
            return 0
        result = evolution_service._apply_proposal(
            db, p, json.loads(p.payload or "{}")
        )
        return result.get("applied_rule_id", 0) or 0


class CodeReviewerProactiveSkill(ProactiveSkill):
    """code_reviewer Agent 主动行为 Skill

    主动监测近 7 天审查反馈假阳性率趋势,若部分规则假阳性率偏高则触发进化或提问。

    Attributes:
        name: Skill 唯一标识 "code_reviewer.proactive"
    """

    name = "code_reviewer.proactive"
    description = "主动监测审查反馈假阳性率趋势,触发自进化或提问建议"

    def __init__(self, agent_name: str = "code_reviewer"):
        """初始化 code_reviewer 主动行为 Skill

        Args:
            agent_name: Agent name(默认 code_reviewer)
        """
        super().__init__(agent_name)

    def check_proactive(
        self, db: Session, ctx: Optional["AgentContext"] = None
    ) -> List[ProactiveAction]:
        """扫描近 7 天审查反馈,若假阳性率突增则触发进化

        Args:
            db: 数据库会话
            ctx: Agent 上下文

        Returns:
            list[ProactiveAction]: 建议行动列表(按 priority 排序)
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
                    detail="近 7 天审查反馈显示部分规则假阳性率≥60%,建议触发一轮自进化",
                    payload={
                        "agent_name": self.agent_name,
                        "window_days": 7,
                        "high_fp_count": high_fp_count,
                    },
                ))
        except Exception as e:
            logger.warning(f"[{self.name}] check_proactive 异常: {e}")
        return actions

    def scan_domain(self, db: Session) -> List[Dict[str, Any]]:
        """主动巡检:找出近 7 天堆积的 pending 进化提案

        Args:
            db: 数据库会话

        Returns:
            list[dict]: 待审批提案列表(含 id/title/proposal_type/create_time)
        """
        from app.models.evolution_proposal import EvolutionProposal

        try:
            rows = (
                db.query(EvolutionProposal)
                .filter(
                    EvolutionProposal.status == "pending",
                    EvolutionProposal.agent_name == self.agent_name,
                )
                .order_by(EvolutionProposal.create_time.desc())
                .limit(20)
                .all()
            )
            return [
                {
                    "id": r.id,
                    "title": r.title,
                    "proposal_type": r.proposal_type,
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
        """从 ai_call_log 反思 code_reviewer 调用趋势

        Args:
            db: 数据库会话
            window_days: 反思窗口

        Returns:
            list[dict]: 学习到的候选改进点(失败率偏高时给出建议)
        """
        from datetime import datetime, timedelta
        from app.models.ai_call_log import AiCallLog

        try:
            cutoff = datetime.utcnow() - timedelta(days=window_days)
            rows = (
                db.query(AiCallLog)
                .filter(
                    AiCallLog.agent_label == "code_reviewer",
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
                    "finding": "code_reviewer 调用失败率偏高",
                    "fail_rate": round(fail_rate, 2),
                    "suggestion": "检查 LLM 审查链路稳定性,考虑增加重试",
                })
            return reflections
        except Exception as e:
            logger.warning(f"[{self.name}] reflect_from_logs 异常: {e}")
            return []
