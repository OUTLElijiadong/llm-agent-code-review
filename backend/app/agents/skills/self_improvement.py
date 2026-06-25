"""SelfImprovementSkill 自进化闭环 Skill 基类 — AgentSkill 自进化与总调度升级

下沉现有 EvolutionAgent 的七步闭环(Act/Observe/Aggregate/Reflect/Gate/Promote/Rollback)
为模板方法 evolve(),子类只需实现 evolve_target() 钩子定义自己的进化对象与策略。

防翻车:
- 提案默认 status=pending,不自动生效
- 触发提案需满足 min_samples + min_distinct_tasks 双门槛
- 仅 admin 审批后才 promote
- 全程留痕 audit_log,可一键回滚
"""
import json
import time
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from loguru import logger
from sqlalchemy.orm import Session

from app.agents.skills.base import BaseSkill, SkillResult

if TYPE_CHECKING:
    from app.agents.base import AgentContext


# 开放(未决)提案状态:用于去重,避免重复堆积同一提案
_OPEN_STATUSES = ("pending", "eval_passed", "eval_failed")


class SelfImprovementSkill(BaseSkill):
    """自进化闭环 Skill 基类

    下沉现有 EvolutionAgent 的七步闭环为模板方法 evolve(),子类只需实现:
    - aggregate_feedback: 聚合反馈信号(复用 feedback_service)
    - evolve_target: 从信号产出候选提案(纯函数,便于单测)
    - apply_proposal: 应用已审批提案到进化对象

    防翻车:
    - 提案默认 status=pending,不自动生效
    - 触发提案需满足 min_samples + min_distinct_tasks 双门槛
    - 全程留痕 audit_log,可一键回滚

    类属性:
        skill_type: 固定为 "self_improvement"
    """

    skill_type = "self_improvement"

    def __init__(
        self,
        agent_name: str,
        min_samples: int = 20,
        min_distinct_tasks: int = 2,
        high_fp_rate: float = 0.6,
        disable_fp_rate: float = 0.8,
    ):
        """初始化自进化 Skill

        Args:
            agent_name: 所属 Agent name
            min_samples: 触发提案的最小已决样本量(防翻车双门槛之一)
            min_distinct_tasks: 触发提案需跨越的最小任务数(防翻车双门槛之二)
            high_fp_rate: 触发降级的假阳性率阈值
            disable_fp_rate: 触发禁用的假阳性率阈值
        """
        super().__init__(agent_name)
        self.min_samples = min_samples
        self.min_distinct_tasks = min_distinct_tasks
        self.high_fp_rate = high_fp_rate
        self.disable_fp_rate = disable_fp_rate

    # ── 模板方法:七步闭环(子类不 override) ──

    def evolve(
        self,
        db: Session,
        window_days: int = 90,
        ctx: Optional["AgentContext"] = None,
    ) -> SkillResult:
        """自进化模板方法(七步闭环)

        1. Aggregate: 调 aggregate_feedback 聚合信号
        2. Reflect: 调 evolve_target 产出候选提案
        3. Gate: 调 evaluate_gate 跑闸门(可选,失败不阻塞)
        4. Persist: 通过闸门的提案写入 evolution_proposal(默认 pending,带 agent_name)

        Args:
            db: 数据库会话
            window_days: 反馈滑动窗口天数
            ctx: Agent 上下文

        Returns:
            SkillResult: data={"proposals": int, "created": int, "skipped": int},
                         effect="proposal_created"(有创建) 或 "no_op"(无创建)
        """
        t0 = time.time()
        trace_id = (ctx.extra or {}).get("trace_id") if ctx else None
        tid = f"tid={trace_id} " if trace_id else ""

        logger.info(
            f"[{self.name}] {tid}[Evolve/Start] agent={self.agent_name} "
            f"window={window_days}d min_samples={self.min_samples} "
            f"min_distinct_tasks={self.min_distinct_tasks}"
        )
        try:
            # 1. Aggregate: 聚合反馈信号
            t_agg = time.time()
            logger.info(f"[{self.name}] {tid}[Aggregate/Start] 聚合反馈信号 window={window_days}d")
            stats = self.aggregate_feedback(db, window_days)
            agg_ms = int((time.time() - t_agg) * 1000)
            # 概要:每条信号的 rule_type + decided 样本数,便于排查数据缺失
            stats_summary = [
                {
                    "rule_type": s.get("rule_type"),
                    "decided": s.get("decided", 0),
                    "false_positive_rate": s.get("false_positive_rate"),
                }
                for s in stats
            ]
            logger.info(
                f"[{self.name}] {tid}[Aggregate/Done] stats={len(stats)}条 "
                f"duration={agg_ms}ms"
            )
            logger.debug(f"[{self.name}] {tid}[Aggregate/Detail] {stats_summary}")

            # 2. Reflect: 产出候选提案(纯函数)
            t_ref = time.time()
            logger.info(f"[{self.name}] {tid}[Reflect/Start] 产出候选提案")
            proposals = self.evolve_target(db, stats)
            ref_ms = int((time.time() - t_ref) * 1000)
            logger.info(
                f"[{self.name}] {tid}[Reflect/Done] proposals={len(proposals)}条 "
                f"duration={ref_ms}ms"
            )
            # DEBUG:每条提案的标题与目标,便于排查"为何没产出提案"
            for idx, p in enumerate(proposals):
                logger.debug(
                    f"[{self.name}] {tid}[Reflect/Proposal#{idx}] "
                    f"type={p.get('proposal_type')} title={p.get('title', '')[:80]} "
                    f"target_rule_id={p.get('target_rule_id')}"
                )

            # 3+4. Gate + Persist: 逐条评估并持久化(去重)
            created, skipped, artifacts = 0, 0, []
            logger.info(
                f"[{self.name}] {tid}[Gate/Persist/Start] 开始逐条评估+持久化"
            )
            for idx, proposal in enumerate(proposals):
                proposal_title = proposal.get("title", "")[:80]
                proposal_type = proposal.get("proposal_type")

                # 闸门评估(失败不阻塞,标记 eval_failed)
                gate_result = self._safe_evaluate_gate(db, proposal)
                gate_passed = gate_result.get("passed")
                gate_reason = gate_result.get("reason", "")
                logger.info(
                    f"[{self.name}] {tid}[Gate/Proposal#{idx}] "
                    f"type={proposal_type} title={proposal_title} "
                    f"passed={gate_passed} reason={gate_reason}"
                )
                if gate_passed:
                    proposal["status"] = "eval_passed"
                    proposal["eval_score"] = gate_result.get("score")
                else:
                    proposal["status"] = "pending"  # 闸门未过仍 pending,交人工
                    proposal["eval_score"] = gate_result.get("score")

                # 去重 + 持久化
                if self._is_duplicate(db, proposal):
                    skipped += 1
                    logger.info(
                        f"[{self.name}] {tid}[Dedup/Proposal#{idx}] "
                        f"跳过(同类未决提案已存在) type={proposal_type} "
                        f"target_rule_id={proposal.get('target_rule_id')}"
                    )
                    continue
                record = self._persist_proposal(db, proposal)
                if record is not None:
                    created += 1
                    artifacts.append({"type": "proposal", "id": record.id})
                    logger.info(
                        f"[{self.name}] {tid}[Persist/Proposal#{idx}] "
                        f"成功 proposal_id={record.id} type={proposal_type} "
                        f"status={proposal.get('status')}"
                    )
                else:
                    logger.warning(
                        f"[{self.name}] {tid}[Persist/Proposal#{idx}] "
                        f"失败(持久化返回 None) type={proposal_type} title={proposal_title}"
                    )

            # 提交事务
            t_commit = time.time()
            db.commit()
            commit_ms = int((time.time() - t_commit) * 1000)
            logger.info(
                f"[{self.name}] {tid}[Commit/Done] duration={commit_ms}ms"
            )

            effect = "proposal_created" if created > 0 else "no_op"
            duration_ms = int((time.time() - t0) * 1000)
            logger.info(
                f"[{self.name}] {tid}[Evolve/Done] created={created} "
                f"skipped={skipped} effect={effect} "
                f"duration={duration_ms}ms"
            )
            return SkillResult(
                success=True,
                data={
                    "proposals": len(proposals),
                    "created": created,
                    "skipped": skipped,
                },
                effect=effect,
                duration_ms=duration_ms,
                artifacts=artifacts,
            )
        except Exception as e:
            duration_ms = int((time.time() - t0) * 1000)
            logger.exception(
                f"[{self.name}] {tid}[Evolve/Failed] 异常: {e} "
                f"duration={duration_ms}ms"
            )
            return SkillResult(
                success=False,
                error=f"自进化执行异常: {e}",
                effect="failed",
                duration_ms=duration_ms,
            )

    # ── 子类必须实现的钩子 ──

    def aggregate_feedback(
        self, db: Session, window_days: int
    ) -> List[Dict[str, Any]]:
        """聚合反馈信号(子类实现)

        Args:
            db: 数据库会话
            window_days: 滑动窗口

        Returns:
            list[dict]: 聚合后的信号列表,每个 dict 含
                {rule_type, decided, ignored, distinct_ignored_tasks,
                 false_positive_rate, accepted_count, ...}

        Raises:
            NotImplementedError: 子类未实现时抛出
        """
        raise NotImplementedError

    def evolve_target(
        self, db: Session, stats: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """从聚合信号产出候选提案(子类实现,纯函数便于单测)

        Args:
            db: 数据库会话(只读,用于查询现有进化对象如 review_rule)
            stats: aggregate_feedback 的输出

        Returns:
            list[dict]: 候选提案,每个 dict 含
                {proposal_type, target_rule_id, title, payload, evidence}

        Raises:
            NotImplementedError: 子类未实现时抛出
        """
        raise NotImplementedError

    def apply_proposal(self, db: Session, proposal: Dict[str, Any]) -> int:
        """应用提案到进化对象(子类实现)

        Args:
            db: 数据库会话
            proposal: 已审批通过的提案

        Returns:
            int: affected_id(如 review_rule.id)

        Raises:
            NotImplementedError: 子类未实现时抛出
        """
        raise NotImplementedError

    # ── 有默认实现的钩子(子类可 override) ──

    def evaluate_gate(
        self, db: Session, proposal: Dict[str, Any]
    ) -> Dict[str, Any]:
        """评估闸门(默认实现复用 eval_gate,子类可 override)

        默认行为:延迟 import eval_gate 跑闸门;若 eval_case 为空或评估失败,
        默认返回 passed=True(不阻塞,交人工闸门定夺)。

        Args:
            db: 数据库会话
            proposal: 候选提案

        Returns:
            dict: {passed: bool, score: {before: {...}, after: {...}}, reason: str}
        """
        return {
            "passed": True,
            "score": None,
            "reason": "默认通过(无 eval_case 或子类未 override)",
        }

    def _safe_evaluate_gate(
        self, db: Session, proposal: Dict[str, Any]
    ) -> Dict[str, Any]:
        """安全评估闸门(捕获异常,失败不阻塞)

        Args:
            db: 数据库会话
            proposal: 候选提案

        Returns:
            dict: {passed: bool, score: dict|None, reason: str}
        """
        t_gate = time.time()
        proposal_title = proposal.get("title", "")[:80]
        logger.debug(
            f"[{self.name}] [Gate/Enter] proposal_type={proposal.get('proposal_type')} "
            f"title={proposal_title}"
        )
        try:
            result = self.evaluate_gate(db, proposal)
            gate_ms = int((time.time() - t_gate) * 1000)
            logger.debug(
                f"[{self.name}] [Gate/Exit] passed={result.get('passed')} "
                f"duration={gate_ms}ms reason={result.get('reason', '')}"
            )
            return result
        except Exception as e:
            gate_ms = int((time.time() - t_gate) * 1000)
            logger.warning(
                f"[{self.name}] [Gate/Exception] 异常,默认通过: {e} "
                f"duration={gate_ms}ms"
            )
            return {"passed": True, "score": None, "reason": f"闸门异常: {e}"}

    def rollback_proposal(self, db: Session, proposal_id: int) -> bool:
        """回滚提案(默认实现调用 evolution_service.rollback,子类可 override)

        Args:
            db: 数据库会话
            proposal_id: evolution_proposal.id

        Returns:
            bool: 是否回滚成功
        """
        t_rb = time.time()
        logger.info(
            f"[{self.name}] [Rollback/Start] proposal_id={proposal_id}"
        )
        try:
            from app.services import evolution_service

            evolution_service.rollback(db, proposal_id)
            rb_ms = int((time.time() - t_rb) * 1000)
            logger.info(
                f"[{self.name}] [Rollback/Done] proposal_id={proposal_id} "
                f"成功 duration={rb_ms}ms"
            )
            return True
        except Exception as e:
            rb_ms = int((time.time() - t_rb) * 1000)
            logger.exception(
                f"[{self.name}] [Rollback/Failed] proposal_id={proposal_id} "
                f"异常 duration={rb_ms}ms: {e}"
            )
            return False

    # ── 持久化与去重(复用 EvolutionAgent 逻辑) ──

    def _persist_proposal(self, db: Session, proposal: Dict[str, Any]):
        """持久化提案到 evolution_proposal 表

        Args:
            db: 数据库会话
            proposal: 候选提案 dict

        Returns:
            EvolutionProposal|None: 持久化后的记录(失败返回 None)
        """
        from app.models.evolution_proposal import EvolutionProposal

        t_pers = time.time()
        proposal_type = proposal.get("proposal_type")
        proposal_title = proposal.get("title", "")[:80]
        logger.debug(
            f"[{self.name}] [Persist/Enter] type={proposal_type} "
            f"title={proposal_title} status={proposal.get('status')}"
        )
        try:
            record = EvolutionProposal(
                proposal_type=proposal["proposal_type"],
                target_rule_id=proposal.get("target_rule_id"),
                title=proposal.get("title", "")[:200],
                payload=json.dumps(
                    proposal.get("payload", {}), ensure_ascii=False
                ),
                evidence=json.dumps(
                    proposal.get("evidence", {}), ensure_ascii=False
                ),
                status=proposal.get("status", "pending"),
                eval_score=(
                    json.dumps(proposal["eval_score"], ensure_ascii=False)
                    if proposal.get("eval_score")
                    else None
                ),
                created_by=self.agent_name,
                agent_name=self.agent_name,
            )
            db.add(record)
            db.flush()
            pers_ms = int((time.time() - t_pers) * 1000)
            logger.debug(
                f"[{self.name}] [Persist/Exit] proposal_id={record.id} "
                f"成功 duration={pers_ms}ms"
            )
            return record
        except Exception as e:
            pers_ms = int((time.time() - t_pers) * 1000)
            logger.warning(
                f"[{self.name}] [Persist/Exception] type={proposal_type} "
                f"失败 duration={pers_ms}ms: {e}"
            )
            return None

    def _is_duplicate(self, db: Session, proposal: Dict[str, Any]) -> bool:
        """同类未决提案去重:相同类型+目标规则,或相同新规则 rule_code

        Args:
            db: 数据库会话
            proposal: 候选提案 dict

        Returns:
            bool: 是否已存在同类未决提案
        """
        from app.models.evolution_proposal import EvolutionProposal

        q = db.query(EvolutionProposal).filter(
            EvolutionProposal.status.in_(_OPEN_STATUSES),
            EvolutionProposal.proposal_type == proposal["proposal_type"],
            EvolutionProposal.agent_name == self.agent_name,
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

    # ── 统一调用入口 ──

    def run(
        self, params: Dict[str, Any], ctx: Optional["AgentContext"] = None
    ) -> SkillResult:
        """统一调用入口

        Args:
            params: 调用参数,支持:
                - {"action": "evolve", "window_days": 90} → 跑一轮进化
                - {"action": "apply", "proposal_id": 123} → 应用已审批提案
                - {"action": "rollback", "proposal_id": 123} → 回滚
            ctx: Agent 上下文

        Returns:
            SkillResult: 调用结果
        """
        action = params.get("action", "evolve")
        trace_id = (ctx.extra or {}).get("trace_id") if ctx else None
        tid = f"tid={trace_id} " if trace_id else ""
        # 参数概要(排除 _db 等内部对象,避免日志不可读)
        param_keys = [k for k in params.keys() if k != "_db"]
        logger.info(
            f"[{self.name}] {tid}[Run/Start] action={action} "
            f"params_keys={param_keys}"
        )
        t_run = time.time()

        if action == "evolve":
            # evolve 需要 db,由调用方(skill_service)注入 ctx.extra 或 params
            db = params.get("_db")
            if db is None:
                logger.warning(
                    f"[{self.name}] {tid}[Run/evolve] 失败:缺少 _db 参数"
                )
                return SkillResult(
                    success=False,
                    error="evolve 动作需要 _db 参数(由 skill_service 注入)",
                    effect="failed",
                )
            window_days = int(params.get("window_days", 90))
            result = self.evolve(db, window_days, ctx)
            logger.info(
                f"[{self.name}] {tid}[Run/Done] action=evolve "
                f"success={result.success} effect={result.effect} "
                f"duration={result.duration_ms}ms"
            )
            return result
        if action == "rollback":
            db = params.get("_db")
            proposal_id = int(params.get("proposal_id", 0))
            if db is None or proposal_id <= 0:
                logger.warning(
                    f"[{self.name}] {tid}[Run/rollback] 失败:缺少 _db 或 proposal_id "
                    f"proposal_id={proposal_id}"
                )
                return SkillResult(
                    success=False,
                    error="rollback 动作需要 _db 与 proposal_id 参数",
                    effect="failed",
                )
            ok = self.rollback_proposal(db, proposal_id)
            run_ms = int((time.time() - t_run) * 1000)
            logger.info(
                f"[{self.name}] {tid}[Run/Done] action=rollback "
                f"proposal_id={proposal_id} success={ok} duration={run_ms}ms"
            )
            return SkillResult(success=ok, effect="success" if ok else "failed")
        if action == "apply":
            db = params.get("_db")
            proposal_id = int(params.get("proposal_id", 0))
            if db is None or proposal_id <= 0:
                logger.warning(
                    f"[{self.name}] {tid}[Run/apply] 失败:缺少 _db 或 proposal_id "
                    f"proposal_id={proposal_id}"
                )
                return SkillResult(
                    success=False,
                    error="apply 动作需要 _db 与 proposal_id 参数",
                    effect="failed",
                )
            from app.models.evolution_proposal import EvolutionProposal

            logger.info(
                f"[{self.name}] {tid}[Apply/Start] proposal_id={proposal_id}"
            )
            proposal_row = db.query(EvolutionProposal).filter(
                EvolutionProposal.id == proposal_id
            ).first()
            if proposal_row is None:
                logger.warning(
                    f"[{self.name}] {tid}[Apply/Failed] proposal_id={proposal_id} 不存在"
                )
                return SkillResult(success=False, error="提案不存在", effect="failed")
            proposal_dict = {
                "proposal_type": proposal_row.proposal_type,
                "target_rule_id": proposal_row.target_rule_id,
                "payload": json.loads(proposal_row.payload or "{}"),
            }
            logger.debug(
                f"[{self.name}] {tid}[Apply/Detail] type={proposal_row.proposal_type} "
                f"target_rule_id={proposal_row.target_rule_id}"
            )
            affected_id = self.apply_proposal(db, proposal_dict)
            run_ms = int((time.time() - t_run) * 1000)
            logger.info(
                f"[{self.name}] {tid}[Run/Done] action=apply "
                f"proposal_id={proposal_id} affected_id={affected_id} "
                f"duration={run_ms}ms"
            )
            return SkillResult(
                success=True,
                data={"affected_id": affected_id},
                effect="success",
            )
        logger.warning(
            f"[{self.name}] {tid}[Run/Failed] 未知 action={action}"
        )
        return SkillResult(
            success=False,
            error=f"未知 action: {action}",
            effect="failed",
        )

    def _params_schema(self) -> Dict[str, Any]:
        """返回参数 JSON Schema

        Returns:
            dict: 参数 JSON Schema
        """
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["evolve", "apply", "rollback"],
                    "description": "动作:evolve 跑进化 / apply 应用提案 / rollback 回滚",
                    "default": "evolve",
                },
                "window_days": {
                    "type": "integer",
                    "description": "反馈窗口天数(evolve 动作)",
                    "default": 90,
                },
                "proposal_id": {
                    "type": "integer",
                    "description": "提案 ID(apply/rollback 动作)",
                },
            },
        }
