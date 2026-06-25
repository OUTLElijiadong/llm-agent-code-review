"""security_sentinel Agent 专属 Skill — 安全哨兵 Agent 自进化与主动监测

SecuritySentinelSelfImprovementSkill: 从 ai_call_log 聚合安全扫描调用统计,
产出安全规则(security_static_rules / security_patterns 内存字典)新增/调整提案。

SecuritySentinelProactiveSkill: 主动监测近 7 天安全扫描失败率,触发进化或提问。

进化对象:
- security_static_rules(静态安全规则内存字典)
- security_patterns(安全正则库内存字典)
由于进化对象为内存字典,提案默认 pending,需人工审批后手动修改源码生效。
"""
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from loguru import logger
from sqlalchemy.orm import Session

from app.agents.skills.self_improvement import SelfImprovementSkill
from app.agents.skills.proactive import ProactiveAction, ProactiveSkill

if TYPE_CHECKING:
    from app.agents.base import AgentContext


class SecuritySentinelSelfImprovementSkill(SelfImprovementSkill):
    """security_sentinel Agent 自进化 Skill

    进化对象为安全规则(security_static_rules / security_patterns 内存字典)。
    aggregate_feedback 从 ai_call_log 挖掘 security_sentinel 调用统计,
    evolve_target 产出安全规则新增/调整提案(payload 存规则定义)。

    Attributes:
        name: Skill 唯一标识 "security_sentinel.self_improve"
    """

    name = "security_sentinel.self_improve"
    description = "从安全扫描反馈进化安全规则与正则库,产出新增/调整安全规则提案"

    def __init__(
        self,
        agent_name: str = "security_sentinel",
        min_samples: int = 20,
        min_distinct_tasks: int = 2,
        high_fp_rate: float = 0.6,
        disable_fp_rate: float = 0.8,
    ):
        """初始化 security_sentinel 自进化 Skill

        Args:
            agent_name: Agent name(默认 security_sentinel)
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
        """从 ai_call_log 聚合 security_sentinel 调用统计

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
                AiCallLog.agent_label == "security_sentinel",
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
        """基于调用统计产出安全规则优化提案

        失败率高→建议优化安全规则正则库,耗时高→建议简化扫描逻辑。

        Args:
            db: 数据库会话(只读)
            stats: aggregate_feedback 的输出

        Returns:
            list[dict]: 候选提案(安全规则新增/调整,payload 存规则定义)
        """
        proposals: List[Dict[str, Any]] = []
        for s in stats:
            if s.get("fail_rate", 0) >= 0.3:
                proposals.append({
                    "proposal_type": "new_rule",
                    "target_rule_id": None,
                    "title": (
                        f"安全哨兵调用失败率偏高({s['fail_rate']:.0%}),"
                        f"建议优化安全规则正则库"
                    ),
                    "payload": {
                        "rule_code": "security_sentinel_rule_optimize",
                        "rule_name": "安全规则正则库优化",
                        "rule_type": "security",
                        "rule_content": (
                            "优化 security_sentinel 的 security_static_rules / "
                            "security_patterns,降低误报与失败率"
                        ),
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
                        f"安全哨兵平均耗时偏高({s['avg_duration_ms']}ms),"
                        f"建议简化扫描逻辑"
                    ),
                    "payload": {
                        "rule_code": "security_sentinel_scan_simplify",
                        "rule_name": "安全扫描逻辑简化",
                        "rule_type": "performance",
                        "rule_content": "简化 security_sentinel 的扫描逻辑,降低耗时",
                        "language": "*",
                        "severity": "低",
                    },
                    "evidence": {"source": "ai_call_log", **s},
                })
        return proposals

    def apply_proposal(self, db: Session, proposal: Dict[str, Any]) -> int:
        """应用提案到安全规则(存 evolution_proposal,人工审批后手动应用)

        安全规则为内存字典(security_static_rules / security_patterns),
        需人工审批后手动修改源码,此处不自动生效。

        Args:
            db: 数据库会话
            proposal: 已审批通过的提案 dict

        Returns:
            int: 0(内存规则不返回 affected_id)
        """
        return 0


class SecuritySentinelProactiveSkill(ProactiveSkill):
    """security_sentinel Agent 主动行为 Skill

    主动监测近 7 天安全扫描失败率,若失败率突增则触发进化或提问。

    Attributes:
        name: Skill 唯一标识 "security_sentinel.proactive"
    """

    name = "security_sentinel.proactive"
    description = "主动监测安全扫描失败率趋势,触发自进化或提问建议"

    def __init__(self, agent_name: str = "security_sentinel"):
        """初始化 security_sentinel 主动行为 Skill

        Args:
            agent_name: Agent name(默认 security_sentinel)
        """
        super().__init__(agent_name)

    def check_proactive(
        self, db: Session, ctx: Optional["AgentContext"] = None
    ) -> List[ProactiveAction]:
        """扫描近 7 天 security_sentinel 调用异常,若失败率突增则触发进化

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
                    AiCallLog.agent_label == "security_sentinel",
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
                        title="安全哨兵近7天失败率偏高",
                        detail=(
                            f"失败率 {fail}/{total},"
                            f"建议触发自进化优化安全规则正则库"
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
        """主动巡检:找出近 7 天堆积的 pending 安全规则提案

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
        """从 ai_call_log 反思 security_sentinel 调用趋势

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
                    AiCallLog.agent_label == "security_sentinel",
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
                    "finding": "security_sentinel 调用失败率偏高",
                    "fail_rate": round(fail_rate, 2),
                    "suggestion": "检查安全扫描链路稳定性与正则库准确性",
                })
            return reflections
        except Exception as e:
            logger.warning(f"[{self.name}] reflect_from_logs 异常: {e}")
            return []
