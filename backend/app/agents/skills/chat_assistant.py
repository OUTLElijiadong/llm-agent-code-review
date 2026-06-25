"""chat_assistant Agent 专属 Skill — 聊天助手 Agent 自进化与主动监测

ChatAssistantSelfImprovementSkill: 从 ai_call_log 聚合 chat_assistant 调用统计,
产出意图识别 prompt 片段与路由策略优化提案。

ChatAssistantProactiveSkill: 主动监测近 7 天调用失败率,触发进化或提问。
"""
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from loguru import logger
from sqlalchemy.orm import Session

from app.agents.skills.self_improvement import SelfImprovementSkill
from app.agents.skills.proactive import ProactiveAction, ProactiveSkill

if TYPE_CHECKING:
    from app.agents.base import AgentContext


class ChatAssistantSelfImprovementSkill(SelfImprovementSkill):
    """chat_assistant Agent 自进化 Skill

    进化对象为意图识别 prompt 片段与路由策略。aggregate_feedback 从 ai_call_log
    聚合调用统计,evolve_target 基于统计产出优化提案
    (失败率高→优化提示词,耗时高→简化逻辑)。

    Attributes:
        name: Skill 唯一标识 "chat_assistant.self_improve"
    """

    name = "chat_assistant.self_improve"
    description = "从调用统计进化意图识别 prompt 片段与路由策略"

    def aggregate_feedback(
        self, db: Session, window_days: int
    ) -> List[Dict[str, Any]]:
        """从 ai_call_log 聚合该 Agent 的调用统计

        Args:
            db: 数据库会话
            window_days: 滑动窗口

        Returns:
            list[dict]: 聚合后的调用统计列表(含 total_calls/success_count/
                fail_count/fail_rate/avg_duration_ms/avg_tokens)
        """
        from app.models.ai_call_log import AiCallLog

        cutoff = datetime.utcnow() - timedelta(days=window_days)
        rows = (
            db.query(AiCallLog)
            .filter(
                AiCallLog.agent_label == "chat_assistant",
                AiCallLog.create_time >= cutoff,
            )
            .all()
        )
        total = len(rows)
        if total == 0:
            return []
        success = sum(1 for r in rows if r.status == "success")
        return [{
            "total_calls": total,
            "success_count": success,
            "fail_count": total - success,
            "fail_rate": round((total - success) / total, 3),
            "avg_duration_ms": sum(r.duration_ms or 0 for r in rows) // total,
            "avg_tokens": sum(r.total_tokens or 0 for r in rows) // total,
        }]

    def evolve_target(
        self, db: Session, stats: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """基于调用统计产出优化提案

        失败率高→优化提示词,耗时高→简化逻辑。

        Args:
            db: 数据库会话(只读)
            stats: aggregate_feedback 的输出

        Returns:
            list[dict]: 候选提案(意图识别 prompt 片段与路由策略优化)
        """
        proposals: List[Dict[str, Any]] = []
        for s in stats:
            if s.get("fail_rate", 0) >= 0.3:
                proposals.append({
                    "proposal_type": "new_rule",
                    "target_rule_id": None,
                    "title": (
                        f"聊天助手调用失败率偏高({s['fail_rate']:.0%}),"
                        f"建议优化提示词"
                    ),
                    "payload": {
                        "rule_code": "chat_assistant_prompt_optimize",
                        "rule_name": "聊天助手提示词优化",
                        "rule_type": "prompt",
                        "rule_content": "优化 chat_assistant 的 system prompt,降低失败率",
                        "language": "*",
                        "severity": "中",
                    },
                    "evidence": {"source": "ai_call_log", **s},
                })
            if s.get("avg_duration_ms", 0) >= 10000:
                proposals.append({
                    "proposal_type": "new_rule",
                    "target_rule_id": None,
                    "title": (
                        f"聊天助手平均耗时偏高({s['avg_duration_ms']}ms),"
                        f"建议简化逻辑"
                    ),
                    "payload": {
                        "rule_code": "chat_assistant_logic_simplify",
                        "rule_name": "聊天助手逻辑简化",
                        "rule_type": "performance",
                        "rule_content": "简化 chat_assistant 的处理逻辑,降低耗时",
                        "language": "*",
                        "severity": "低",
                    },
                    "evidence": {"source": "ai_call_log", **s},
                })
        return proposals

    def apply_proposal(self, db: Session, proposal: Dict[str, Any]) -> int:
        """应用提案(存 evolution_proposal,人工审批后手动应用)

        Args:
            db: 数据库会话
            proposal: 已审批通过的提案 dict

        Returns:
            int: 0(提案默认 pending,人工审批后手动应用)
        """
        return 0


class ChatAssistantProactiveSkill(ProactiveSkill):
    """chat_assistant Agent 主动行为 Skill

    主动监测近 7 天调用失败率,若失败率突增则触发进化或提问。

    Attributes:
        name: Skill 唯一标识 "chat_assistant.proactive"
    """

    name = "chat_assistant.proactive"
    description = "主动监测聊天助手调用趋势,触发自进化或提问建议"

    def check_proactive(
        self, db: Session, ctx: Optional["AgentContext"] = None
    ) -> List[ProactiveAction]:
        """扫描近 7 天调用异常,若失败率突增则触发进化

        Args:
            db: 数据库会话
            ctx: Agent 上下文

        Returns:
            list[ProactiveAction]: 建议行动列表(按 priority 排序)
        """
        from app.models.ai_call_log import AiCallLog

        actions: List[ProactiveAction] = []
        try:
            cutoff = datetime.utcnow() - timedelta(days=7)
            rows = (
                db.query(AiCallLog)
                .filter(
                    AiCallLog.agent_label == "chat_assistant",
                    AiCallLog.create_time >= cutoff,
                )
                .all()
            )
            total = len(rows)
            if total > 0:
                fail = sum(1 for r in rows if r.status != "success")
                if fail / total >= 0.3:
                    actions.append(ProactiveAction(
                        action_type="trigger_evolution",
                        priority="high",
                        title="聊天助手近7天失败率偏高",
                        detail=(
                            f"失败率 {fail}/{total},"
                            f"建议触发自进化优化提示词"
                        ),
                        payload={
                            "agent_name": self.agent_name,
                            "window_days": 7,
                        },
                    ))
        except Exception as e:
            logger.warning(f"[{self.name}] check_proactive 异常: {e}")
        return actions

    def scan_domain(self, db: Session) -> List[Dict[str, Any]]:
        """主动巡检:找出近 7 天堆积的 pending 提案

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
        """从 ai_call_log 反思该 Agent 的调用趋势

        Args:
            db: 数据库会话
            window_days: 反思窗口

        Returns:
            list[dict]: 学习到的候选改进点(失败率偏高时给出建议)
        """
        from app.models.ai_call_log import AiCallLog

        try:
            cutoff = datetime.utcnow() - timedelta(days=window_days)
            rows = (
                db.query(AiCallLog)
                .filter(
                    AiCallLog.agent_label == "chat_assistant",
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
                    "finding": "chat_assistant 调用失败率偏高",
                    "fail_rate": round(fail_rate, 2),
                    "suggestion": "检查 LLM 链路稳定性",
                })
            return reflections
        except Exception as e:
            logger.warning(f"[{self.name}] reflect_from_logs 异常: {e}")
            return []
