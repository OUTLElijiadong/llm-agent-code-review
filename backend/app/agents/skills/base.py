"""BaseSkill 抽象基类 + SkillResult 数据结构 — AgentSkill 自进化与总调度升级

所有 Skill 必须继承 BaseSkill 并实现 run() 钩子。Skill 是可挂载到 Agent 的能力模块,
具备:唯一标识(name)、描述(供 LLM 工具列表与前端展示)、可调用入口(run)、
可工具化(to_tool_schema 转 OpenAI tools 格式)。
"""
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Dict, List, Optional

if TYPE_CHECKING:
    from app.agents.base import AgentContext


@dataclass
class SkillResult:
    """Skill 调用结果数据结构

    所有 Skill.run() 必须返回此结构,供 skill_service 记录与调用方消费。

    Attributes:
        success: 是否成功
        data: 输出数据(任意类型,由 Skill 子类定义)
        error: 失败原因(success=False 时填写)
        effect: 效果标签(success / failed / no_op / proposal_created)
        duration_ms: 执行耗时(毫秒)
        artifacts: 产出物列表(如生成的提案 id 列表 [{"type": "proposal", "id": 123}])
    """

    success: bool
    data: Any = None
    error: Optional[str] = None
    effect: str = "success"
    duration_ms: int = 0
    artifacts: List[Dict[str, Any]] = field(default_factory=list)


class BaseSkill:
    """Skill 抽象基类

    所有 Skill 必须继承此类并实现 run() 钩子。Skill 是可挂载到 Agent 的能力模块,
    具备:
    - 唯一标识(name):形如 "{agent_name}.self_improve" / "{agent_name}.proactive"
    - 描述(description):供 LLM 工具列表与前端 SkillManager 展示
    - 可调用入口(run):统一调用入口,子类实现具体逻辑
    - 可工具化(to_tool_schema):转 OpenAI function calling 工具描述,供 ChatPlanner 规划

    类属性:
        name: Skill 唯一标识
        description: Skill 描述
        agent_name: 所属 Agent name(实例属性,__init__ 赋值)
        invocable: 是否可被外部调用(False 则仅供内部触发)
        skill_type: Skill 类型(base / self_improvement / proactive)
    """

    name: str = "base_skill"
    description: str = ""
    agent_name: str = ""
    invocable: bool = True
    skill_type: str = "base"

    def __init__(self, agent_name: str):
        """初始化 Skill

        Args:
            agent_name: 所属 Agent name(如 code_reviewer / evolution)
        """
        self.agent_name = agent_name

    def run(self, params: Dict[str, Any],
            ctx: Optional["AgentContext"] = None) -> SkillResult:
        """Skill 调用入口(子类必须实现)

        Args:
            params: 调用参数(由调用方传入,Skill 子类自定义 schema)
            ctx: Agent 上下文(含 user_id / trace_id 等)

        Returns:
            SkillResult: 调用结果

        Raises:
            NotImplementedError: 子类未实现时抛出
        """
        raise NotImplementedError

    def to_tool_schema(self) -> Dict[str, Any]:
        """转为 OpenAI function calling 工具描述

        供 ChatPlanner 构建 LLM tools 列表,实现双层调度的第二层动态规划。

        Returns:
            dict: OpenAI tools 格式的工具描述
                {
                    "type": "function",
                    "function": {
                        "name": "...",
                        "description": "...",
                        "parameters": {...JSON Schema...}
                    }
                }
        """
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self._params_schema(),
            },
        }

    def _params_schema(self) -> Dict[str, Any]:
        """子类 override 返回参数 JSON Schema,默认空对象

        Returns:
            dict: 参数 JSON Schema,默认 {"type": "object", "properties": {}}
        """
        return {"type": "object", "properties": {}}

    def to_meta(self) -> Dict[str, Any]:
        """转为元数据 dict(供 AgentRegistry.list_runtime 与前端展示)

        Returns:
            dict: Skill 元数据
                {"name", "description", "type", "invocable", "agent_name"}
        """
        return {
            "name": self.name,
            "description": self.description,
            "type": self.skill_type,
            "invocable": self.invocable,
            "agent_name": self.agent_name,
        }
