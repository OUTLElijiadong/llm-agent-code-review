"""SkillRegistry 单例注册中心 — AgentSkill 自进化与总调度升级

维护 agent_name → list[BaseSkill] 映射,提供:
- register: 注册 Skill(线程安全)
- get: 获取指定 Agent 的指定 Skill
- list_for_agent: 列出 Agent 挂载的所有 Skill
- list_all: 列出所有 Skill
- list_tools: 转 LLM tools 列表(OpenAI function calling 格式),供 ChatPlanner 规划

线程安全:register 用 threading.Lock 保护,避免并发请求挂载 Skill 时互相覆盖。
"""
import threading
from typing import TYPE_CHECKING, Dict, List, Optional

from loguru import logger

if TYPE_CHECKING:
    from app.agents.skills.base import BaseSkill


class SkillRegistry:
    """Skill 注册中心(单例)

    维护 agent_name → list[BaseSkill] 映射,提供注册/查询/转 tools 能力。
    线程安全:register 用 threading.Lock 保护。

    用法:
        reg = SkillRegistry.instance()
        reg.register("code_reviewer", CodeReviewerSelfImprovementSkill("code_reviewer"))
        skill = reg.get("code_reviewer", "code_reviewer.self_improve")
        tools = reg.list_tools()  # OpenAI tools 格式
    """

    _instance: Optional["SkillRegistry"] = None
    _init_lock = threading.Lock()

    def __init__(self):
        """初始化 Skill 注册中心"""
        self._skills: Dict[str, List["BaseSkill"]] = {}
        self._lock = threading.Lock()

    @classmethod
    def instance(cls) -> "SkillRegistry":
        """获取单例

        Returns:
            SkillRegistry: 单例实例(双重检查锁保证线程安全)
        """
        if cls._instance is None:
            with cls._init_lock:
                if cls._instance is None:
                    cls._instance = cls()
        return cls._instance

    def register(self, agent_name: str, skill: "BaseSkill") -> None:
        """注册 Skill

        将 Skill 挂载到指定 agent_name 下,同一 agent_name 可挂载多个 Skill。
        线程安全:用 threading.Lock 保护并发挂载。

        Args:
            agent_name: 所属 Agent name(如 code_reviewer)
            skill: Skill 实例
        """
        with self._lock:
            bucket = self._skills.setdefault(agent_name, [])
            # 去重:同名 Skill 不重复注册(请求级实例多次挂载时幂等)
            for existing in bucket:
                if existing.name == skill.name:
                    logger.debug(
                        f"[SkillRegistry] Skill {skill.name} 已注册于 {agent_name},跳过"
                    )
                    return
            bucket.append(skill)
            logger.info(
                f"[SkillRegistry] 注册 Skill: {skill.name} → agent={agent_name} "
                f"type={skill.skill_type}"
            )

    def get(self, agent_name: str, skill_name: str) -> Optional["BaseSkill"]:
        """获取指定 Agent 的指定 Skill

        Args:
            agent_name: Agent name
            skill_name: Skill name(如 code_reviewer.self_improve)

        Returns:
            BaseSkill|None: Skill 实例,不存在返回 None
        """
        bucket = self._skills.get(agent_name, [])
        for s in bucket:
            if s.name == skill_name:
                return s
        return None

    def list_for_agent(self, agent_name: str) -> List["BaseSkill"]:
        """列出 Agent 挂载的所有 Skill

        Args:
            agent_name: Agent name

        Returns:
            list[BaseSkill]: Skill 列表(未挂载返回空列表)
        """
        return list(self._skills.get(agent_name, []))

    def list_all(self) -> List["BaseSkill"]:
        """列出所有已注册 Skill

        Returns:
            list[BaseSkill]: 全部 Skill 列表(按 agent_name 注册顺序)
        """
        result: List["BaseSkill"] = []
        for bucket in self._skills.values():
            result.extend(bucket)
        return result

    def list_agents(self) -> List[str]:
        """列出所有已挂载 Skill 的 Agent name

        Returns:
            list[str]: Agent name 列表
        """
        return list(self._skills.keys())

    def list_tools(
        self,
        agent_name_filter: Optional[str] = None,
        invocable_only: bool = True,
    ) -> List[Dict]:
        """转为 LLM tools 列表(OpenAI function calling 格式)

        供 ChatPlanner 构建 LLM tools 列表,实现双层调度的第二层动态规划。

        Args:
            agent_name_filter: 仅返回该 Agent 的 Skill(None=全部)
            invocable_only: 仅返回 invocable=True 的 Skill(默认 True)

        Returns:
            list[dict]: OpenAI tools 格式
                [{"type": "function", "function": {"name", "description", "parameters"}}]
        """
        if agent_name_filter is not None:
            skills = self.list_for_agent(agent_name_filter)
        else:
            skills = self.list_all()
        tools: List[Dict] = []
        for s in skills:
            if invocable_only and not s.invocable:
                continue
            tools.append(s.to_tool_schema())
        return tools

    def list_meta(
        self, agent_name_filter: Optional[str] = None
    ) -> List[Dict]:
        """列出 Skill 元数据(供 AgentRegistry.list_runtime 与前端 SkillManager)

        Args:
            agent_name_filter: 仅返回该 Agent 的 Skill(None=全部)

        Returns:
            list[dict]: Skill 元数据列表
                [{"name", "description", "type", "invocable", "agent_name"}]
        """
        if agent_name_filter is not None:
            skills = self.list_for_agent(agent_name_filter)
        else:
            skills = self.list_all()
        return [s.to_meta() for s in skills]

    def summary(self) -> Dict:
        """统计已注册 Skill 数量,按 type 分桶

        Returns:
            dict: {"total": int, "by_type": [{"type": str, "count": int}]}
        """
        all_skills = self.list_all()
        by_type: Dict[str, int] = {}
        for s in all_skills:
            by_type[s.skill_type] = by_type.get(s.skill_type, 0) + 1
        return {
            "total": len(all_skills),
            "by_type": [
                {"type": k, "count": v} for k, v in sorted(by_type.items())
            ],
            "agents": len(self._skills),
        }
