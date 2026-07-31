"""Agent 注册中心 — v2.0 升级版

除 v1.0 已有的 name → agent 注册映射,新增:
- list_runtime():       返回每个 Agent 的完整元数据(icon/color/category/skills)
- get_metadata(name):   读取单个 Agent 的元数据
- summary():            统计在岗 Agent 数量,按 category 分桶

供 /api/agents/runtime 路由直接消费,确保 UI 显示的 Agent 数量与列表
和后端 _真实注册_ 的 BaseAgent 完全同步,不再依赖 multi_agent 模块的静态画像。
"""
from typing import Dict, List, Optional

from loguru import logger


class AgentRegistry:
    """Agent 注册中心"""

    _instance: Optional["AgentRegistry"] = None

    def __init__(self):
        self._agents: Dict[str, object] = {}

    @classmethod
    def instance(cls) -> "AgentRegistry":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def register(self, agent: object) -> None:
        from app.agents.base import BaseAgent
        if isinstance(agent, BaseAgent):
            name = agent.name
        else:
            name = getattr(agent, "name", type(agent).__name__)
        self._agents[name] = agent
        logger.info(f"[Registry] 注册 Agent: {name}")

    def get(self, name: str) -> Optional[object]:
        return self._agents.get(name)

    def list(self) -> Dict[str, str]:
        from app.agents.base import BaseAgent
        return {
            name: a.description if isinstance(a, BaseAgent) else str(type(a).__name__)
            for name, a in self._agents.items()
        }

    def list_runtime(self) -> List[Dict]:
        """返回每个 Agent 的完整运行时元数据

        v3.0 AgentSkill 升级:skills 字段从 list[str] 升级为结构化 list[dict]
        (从 SkillRegistry 取元数据);若 Agent 未挂载 Skill,fallback 到原 tuple of str
        保持向后兼容。

        Returns:
            list[dict]: 字段对齐前端 AgentRuntimeOut Schema
        """
        from app.agents.base import BaseAgent
        from app.agents.contracts import domain_skill_meta
        from app.agents.skills.registry import SkillRegistry

        items: List[Dict] = []
        skill_reg = SkillRegistry.instance()
        for name, a in self._agents.items():
            if not isinstance(a, BaseAgent):
                continue
            # 优先取 SkillRegistry 结构化元数据;为空则 fallback 到 Agent.skills tuple
            skills_meta = skill_reg.list_meta(name)
            if not skills_meta:
                skills_meta = [
                    {"name": s, "description": s, "type": "legacy",
                     "invocable": False, "agent_name": name}
                    for s in (getattr(a, "skills", ()) or ())
                ]
            known = {item["name"] for item in skills_meta}
            skills_meta.extend(
                item for item in domain_skill_meta(name) if item["name"] not in known
            )
            items.append({
                "code": name,
                "name": getattr(a, "name", name),
                "description": getattr(a, "description", "") or "",
                "icon": getattr(a, "icon", "base"),
                "color": getattr(a, "color", "#5B58E8"),
                "category": getattr(a, "category", "general"),
                "skills": skills_meta,
                "status": "idle",     # M1 阶段统一返回 idle,运行时状态由 EventBus 推送(M2)
                "model": getattr(a, "_model", ""),
            })
        # 让 orchestrator/chat_assistant 排在最前,便于 UI 上做"主控/前台"分组
        priority = {"orchestrator": 0, "chat_assistant": 1}
        items.sort(key=lambda x: (priority.get(x["code"], 99), x["code"]))
        return items

    def get_metadata(self, name: str) -> Optional[Dict]:
        from app.agents.base import BaseAgent
        from app.agents.contracts import domain_skill_meta
        from app.agents.skills.registry import SkillRegistry

        a = self._agents.get(name)
        if not isinstance(a, BaseAgent):
            return None
        # v3.0: skills 字段升级为结构化 list[dict]
        skills_meta = SkillRegistry.instance().list_meta(name)
        if not skills_meta:
            skills_meta = [
                {"name": s, "description": s, "type": "legacy",
                 "invocable": False, "agent_name": name}
                for s in (a.skills or ())
            ]
        known = {item["name"] for item in skills_meta}
        skills_meta.extend(
            item for item in domain_skill_meta(name) if item["name"] not in known
        )
        return {
            "code": name,
            "name": a.name,
            "description": a.description or "",
            "icon": a.icon,
            "color": a.color,
            "category": a.category,
            "skills": skills_meta,
        }

    def summary(self) -> Dict:
        """按 category 分桶统计已注册 Agent"""
        runtime = self.list_runtime()
        by_cat: Dict[str, int] = {}
        for r in runtime:
            cat = r["category"] or "general"
            by_cat[cat] = by_cat.get(cat, 0) + 1
        return {
            "total": len(runtime),
            "by_category": [{"category": k, "count": v} for k, v in sorted(by_cat.items())],
        }
