"""Skill 抽象层包 — AgentSkill 自进化与总调度升级

模块结构:
- base.py: BaseSkill 抽象基类 + SkillResult 数据结构
- self_improvement.py: SelfImprovementSkill 自进化闭环模板方法基类
- proactive.py: ProactiveSkill 主动行为基类 + ProactiveAction 数据结构
- registry.py: SkillRegistry 单例注册中心
- <agent>.py × 14: per-Agent 专属 Skill 子类(每个文件含 1 个 SelfImprovement + 1 个 Proactive 子类)

设计原则:
- 基类充分抽象,子类只实现钩子(evolve_target / check_proactive)
- 模板方法封装七步闭环,防翻车双门槛
- Skill 可工具化(to_tool_schema 转 OpenAI tools 格式)供 LLM function calling
"""
from app.agents.skills.base import BaseSkill, SkillResult
from app.agents.skills.proactive import ProactiveAction, ProactiveSkill
from app.agents.skills.self_improvement import SelfImprovementSkill

__all__ = [
    "BaseSkill",
    "SkillResult",
    "SelfImprovementSkill",
    "ProactiveSkill",
    "ProactiveAction",
]
