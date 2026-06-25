"""ProactiveSkill 主动行为 Skill 基类 + ProactiveAction 数据结构 — AgentSkill 自进化与总调度升级

ProactiveSkill 封装 4 类主动行为(子类按需 override):
- should_trigger_evolution: 主动进化触发判定(根据指标趋势)
- build_clarify_question: 主动提问/建议(复用 clarify_store)
- scan_domain: 主动巡检/发疑(扫描自身领域发现潜在问题)
- reflect_from_logs: 主动学习/反思(从 ai_call_log 挖趋势)

ProactiveSkill 通常由定时任务(每小时)或事件驱动触发,不直接产出进化提案,
而是产出 ProactiveAction 列表,由调用方决定是否执行。
"""
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from loguru import logger
from sqlalchemy.orm import Session

from app.agents.skills.base import BaseSkill, SkillResult

if TYPE_CHECKING:
    from app.agents.base import AgentContext


@dataclass
class ProactiveAction:
    """主动行动建议数据结构

    ProactiveSkill.check_proactive() 返回此结构列表,供调用方(定时任务/事件订阅器)
    决定是否执行对应行动。

    Attributes:
        action_type: 行动类型(trigger_evolution / ask_question / scan_finding / learn_reflect)
        priority: 优先级(low / medium / high)
        title: 行动标题(简短,供前端展示)
        detail: 详细描述(行动理由与建议)
        payload: 行动参数(供执行使用,如 {"agent_name": "...", "window_days": 7})
    """

    action_type: str
    priority: str
    title: str
    detail: str
    payload: Dict[str, Any] = field(default_factory=dict)


class ProactiveSkill(BaseSkill):
    """主动行为 Skill 基类

    4 类主动行为(子类按需 override):
    - should_trigger_evolution: 主动进化触发判定
    - build_clarify_question: 主动提问/建议(复用 clarify_store)
    - scan_domain: 主动巡检/发疑
    - reflect_from_logs: 主动学习/反思

    ProactiveSkill 通常由定时任务(每小时)或事件驱动触发,不直接产出进化提案,
    而是产出 ProactiveAction 列表,由调用方决定是否执行。

    类属性:
        skill_type: 固定为 "proactive"
    """

    skill_type = "proactive"

    def check_proactive(
        self, db: Session, ctx: Optional["AgentContext"] = None
    ) -> List[ProactiveAction]:
        """扫描自身领域,返回建议行动列表(子类实现)

        Args:
            db: 数据库会话
            ctx: Agent 上下文

        Returns:
            list[ProactiveAction]: 建议行动列表(按 priority 排序)

        Raises:
            NotImplementedError: 子类未实现时抛出
        """
        raise NotImplementedError

    def should_trigger_evolution(self, stats: Dict[str, Any]) -> bool:
        """主动进化触发判定(子类 override)

        根据 stats(如假阳性率、采纳率、调用失败率)判定是否应触发自进化。

        Args:
            stats: 当前 Agent 的关键指标

        Returns:
            bool: 是否应触发自进化(默认 False)
        """
        return False

    def build_clarify_question(
        self, stats: Dict[str, Any]
    ) -> Optional[Dict[str, Any]]:
        """主动提问/建议(子类 override,复用 clarify_store)

        Args:
            stats: 当前 Agent 的关键指标

        Returns:
            dict|None: 提问内容(None 表示无需提问)
                {"label": "...", "type": "...", "hint": "..."}
        """
        return None

    def scan_domain(self, db: Session) -> List[Dict[str, Any]]:
        """主动巡检/发疑(子类 override)

        Args:
            db: 数据库会话

        Returns:
            list[dict]: 发现的潜在问题列表(默认空)
        """
        return []

    def reflect_from_logs(
        self, db: Session, window_days: int = 7
    ) -> List[Dict[str, Any]]:
        """主动学习/反思(子类 override,从 ai_call_log 挖趋势)

        Args:
            db: 数据库会话
            window_days: 反思窗口

        Returns:
            list[dict]: 学习到的候选改进点(默认空)
        """
        return []

    # ── 统一调用入口 ──

    def run(
        self, params: Dict[str, Any], ctx: Optional["AgentContext"] = None
    ) -> SkillResult:
        """统一调用入口

        Args:
            params: 调用参数,支持:
                - {"action_type": "check_proactive"} → 跑一轮主动检查
                - {"action_type": "trigger_evolution", "stats": {...}} → 触发进化判定
                - {"action_type": "scan_domain"} → 主动巡检
                - {"action_type": "reflect_from_logs", "window_days": 7} → 反思
            ctx: Agent 上下文

        Returns:
            SkillResult: 调用结果
        """
        action_type = params.get("action_type", "check_proactive")
        db = params.get("_db")
        t0 = time.time()
        trace_id = (ctx.extra or {}).get("trace_id") if ctx else None
        tid = f"tid={trace_id} " if trace_id else ""
        # 参数概要(排除 _db / stats 等大对象,避免日志不可读)
        param_keys = [k for k in params.keys() if k not in ("_db", "stats")]
        logger.info(
            f"[{self.name}] {tid}[Run/Start] action_type={action_type} "
            f"params_keys={param_keys} has_db={db is not None}"
        )

        try:
            if action_type == "check_proactive":
                if db is None:
                    logger.warning(
                        f"[{self.name}] {tid}[Run/check_proactive] 失败:缺少 _db 参数"
                    )
                    return SkillResult(
                        success=False,
                        error="check_proactive 需要 _db 参数(由 skill_service 注入)",
                        effect="failed",
                    )
                logger.info(
                    f"[{self.name}] {tid}[CheckProactive/Start] 扫描自身领域"
                )
                t_check = time.time()
                actions = self.check_proactive(db, ctx)
                check_ms = int((time.time() - t_check) * 1000)
                # 概要:每条 action 的类型+优先级+标题,便于排查"为何无建议"
                actions_summary = [
                    {
                        "action_type": a.action_type,
                        "priority": a.priority,
                        "title": a.title,
                    }
                    for a in actions
                ]
                logger.info(
                    f"[{self.name}] {tid}[CheckProactive/Done] "
                    f"actions={len(actions)}条 duration={check_ms}ms"
                )
                logger.debug(
                    f"[{self.name}] {tid}[CheckProactive/Detail] {actions_summary}"
                )
                return SkillResult(
                    success=True,
                    data={
                        "actions": [
                            {
                                "action_type": a.action_type,
                                "priority": a.priority,
                                "title": a.title,
                                "detail": a.detail,
                                "payload": a.payload,
                            }
                            for a in actions
                        ],
                        "count": len(actions),
                    },
                    effect="success",
                    duration_ms=int((time.time() - t0) * 1000),
                )

            if action_type == "trigger_evolution":
                stats = params.get("stats", {})
                logger.info(
                    f"[{self.name}] {tid}[TriggerEvolution/Start] "
                    f"stats_keys={list(stats.keys())}"
                )
                logger.debug(
                    f"[{self.name}] {tid}[TriggerEvolution/Detail] stats={stats}"
                )
                t_trig = time.time()
                should = self.should_trigger_evolution(stats)
                trig_ms = int((time.time() - t_trig) * 1000)
                logger.info(
                    f"[{self.name}] {tid}[TriggerEvolution/Done] "
                    f"should_trigger={should} duration={trig_ms}ms"
                )
                return SkillResult(
                    success=True,
                    data={"should_trigger": should},
                    effect="success",
                    duration_ms=int((time.time() - t0) * 1000),
                )

            if action_type == "scan_domain":
                if db is None:
                    logger.warning(
                        f"[{self.name}] {tid}[Run/scan_domain] 失败:缺少 _db 参数"
                    )
                    return SkillResult(
                        success=False,
                        error="scan_domain 需要 _db 参数",
                        effect="failed",
                    )
                logger.info(f"[{self.name}] {tid}[ScanDomain/Start] 主动巡检")
                t_scan = time.time()
                findings = self.scan_domain(db)
                scan_ms = int((time.time() - t_scan) * 1000)
                logger.info(
                    f"[{self.name}] {tid}[ScanDomain/Done] "
                    f"findings={len(findings)}条 duration={scan_ms}ms"
                )
                # DEBUG:每条 finding 概要
                for idx, f in enumerate(findings):
                    logger.debug(
                        f"[{self.name}] {tid}[ScanDomain/Finding#{idx}] {f}"
                    )
                return SkillResult(
                    success=True,
                    data={"findings": findings, "count": len(findings)},
                    effect="success",
                    duration_ms=int((time.time() - t0) * 1000),
                )

            if action_type == "reflect_from_logs":
                if db is None:
                    logger.warning(
                        f"[{self.name}] {tid}[Run/reflect_from_logs] 失败:缺少 _db 参数"
                    )
                    return SkillResult(
                        success=False,
                        error="reflect_from_logs 需要 _db 参数",
                        effect="failed",
                    )
                window_days = int(params.get("window_days", 7))
                logger.info(
                    f"[{self.name}] {tid}[Reflect/Start] "
                    f"window={window_days}d 从 ai_call_log 挖趋势"
                )
                t_refl = time.time()
                reflections = self.reflect_from_logs(db, window_days)
                refl_ms = int((time.time() - t_refl) * 1000)
                logger.info(
                    f"[{self.name}] {tid}[Reflect/Done] "
                    f"reflections={len(reflections)}条 duration={refl_ms}ms"
                )
                # DEBUG:每条 reflection 概要
                for idx, r in enumerate(reflections):
                    logger.debug(
                        f"[{self.name}] {tid}[Reflect/Item#{idx}] {r}"
                    )
                return SkillResult(
                    success=True,
                    data={
                        "reflections": reflections,
                        "count": len(reflections),
                    },
                    effect="success",
                    duration_ms=int((time.time() - t0) * 1000),
                )

            logger.warning(
                f"[{self.name}] {tid}[Run/Failed] 未知 action_type={action_type}"
            )
            return SkillResult(
                success=False,
                error=f"未知 action_type: {action_type}",
                effect="failed",
            )
        except Exception as e:
            run_ms = int((time.time() - t0) * 1000)
            logger.exception(
                f"[{self.name}] {tid}[Run/Failed] action_type={action_type} "
                f"异常 duration={run_ms}ms: {e}"
            )
            return SkillResult(
                success=False,
                error=f"Proactive 执行异常: {e}",
                effect="failed",
                duration_ms=run_ms,
            )

    def _params_schema(self) -> Dict[str, Any]:
        """返回参数 JSON Schema

        Returns:
            dict: 参数 JSON Schema
        """
        return {
            "type": "object",
            "properties": {
                "action_type": {
                    "type": "string",
                    "enum": [
                        "check_proactive",
                        "trigger_evolution",
                        "scan_domain",
                        "reflect_from_logs",
                    ],
                    "description": "主动行为类型",
                    "default": "check_proactive",
                },
                "window_days": {
                    "type": "integer",
                    "description": "反思窗口天数(reflect_from_logs)",
                    "default": 7,
                },
                "stats": {
                    "type": "object",
                    "description": "关键指标(trigger_evolution 判定用)",
                },
            },
        }
