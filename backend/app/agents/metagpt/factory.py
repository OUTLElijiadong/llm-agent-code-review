"""
MetaGPT 风格 Environment 工厂 (v2.4)

提供两个便捷工厂函数,从现有 AgentRegistry / DiscussionOrchestrator 上下文
快速构建 Environment,避免业务层手动拼装 RoleAdapter。

设计原则:
    1. 复用现有 AgentRegistry,不重新实例化 Agent
    2. 工厂函数返回 Environment 实例,业务层可选择调用 env.run() 或仅用 env.publish()
    3. 现有 review_service / discussion_orchestrator 不强制改造,Environment 是可选上层
"""
from __future__ import annotations

from typing import Optional

from loguru import logger

from app.agents.base import AgentContext
from app.agents.metagpt.environment import Environment
from app.agents.metagpt.messages import Message, make_message
from app.agents.metagpt.role_adapter import RoleAdapter
from app.agents.registry import AgentRegistry


def build_review_environment(
    trace_id: str,
    user_id: Optional[int] = None,
    task_id: Optional[int] = None,
    project_id: Optional[int] = None,
    file_id: Optional[int] = None,
    agent_codes: Optional[list[str]] = None,
    max_depth: int = 6,
) -> Environment:
    """构建代码审查 Environment

    从 AgentRegistry 取已注册的审查相关 Agent(code_reviewer / security_sentinel),
    包装为 RoleAdapter 并加入 Environment,订阅 StartReview / CrossReview 动作。

    Args:
        trace_id: 调用链 ID
        user_id: 用户 ID(透传到 AgentContext)
        task_id: 审查任务 ID
        project_id: 项目 ID
        file_id: 文件 ID
        agent_codes: 指定参与的 Agent code 列表;None 时默认取
            ["code_reviewer", "security_sentinel"]
        max_depth: 单次 run() 最大广播深度

    Returns:
        Environment: 已注册审查角色的环境(尚未 publish / run)
    """
    env = Environment(name="review_env", trace_id=trace_id, max_depth=max_depth)
    codes = agent_codes or ["code_reviewer", "security_sentinel"]
    ctx_template = AgentContext(
        user_id=user_id,
        task_id=task_id,
        project_id=project_id,
        file_id=file_id,
        extra={"trace_id": trace_id},
    )
    registry = AgentRegistry.instance()
    for code in codes:
        agent = registry.get(code)
        if agent is None:
            logger.warning(f"[factory] Agent {code} 未注册,跳过")
            continue
        adapter = RoleAdapter(
            agent=agent,
            name=code,
            profile=agent.description or code,
            goal=f"完成 {code} 专项审查",
            watch_actions={"StartReview", "CrossReview"},
            react_action=f"{code}_Reply",
            ctx_template=ctx_template,
        )
        env.add_role(adapter)
    return env


def build_discussion_environment(
    trace_id: str,
    user_id: Optional[int] = None,
    project_id: Optional[int] = None,
    file_id: Optional[int] = None,
    agent_codes: Optional[list[str]] = None,
    max_depth: int = 4,
) -> Environment:
    """构建圆桌讨论 Environment

    与 build_review_environment 的区别:
        - watch_actions 包含 DiscussTurn(订阅其他 Agent 的发言)
        - react_action 改为 {code}_Discuss(便于讨论流中区分)
        - max_depth 默认更小(讨论不需要长链式广播)

    Args:
        trace_id: 调用链 ID
        user_id: 用户 ID(透传到 AgentContext)
        project_id: 项目 ID
        file_id: 文件 ID
        agent_codes: 参与讨论的 Agent code 列表;None 时默认取
            ["code_reviewer", "security_sentinel"]
        max_depth: 单次 run() 最大广播深度,讨论场景默认 4

    Returns:
        Environment: 已注册讨论角色的环境(尚未 publish / run)
    """
    env = Environment(name="discussion_env", trace_id=trace_id, max_depth=max_depth)
    codes = agent_codes or ["code_reviewer", "security_sentinel"]
    ctx_template = AgentContext(
        user_id=user_id,
        project_id=project_id,
        file_id=file_id,
        extra={"trace_id": trace_id},
    )
    registry = AgentRegistry.instance()
    for code in codes:
        agent = registry.get(code)
        if agent is None:
            logger.warning(f"[factory] Agent {code} 未注册,跳过")
            continue
        adapter = RoleAdapter(
            agent=agent,
            name=code,
            profile=agent.description or code,
            goal=f"参与圆桌讨论,提供 {code} 视角的观点",
            watch_actions={"DiscussTurn", "StartDiscussion"},
            react_action=f"{code}_Discuss",
            ctx_template=ctx_template,
        )
        env.add_role(adapter)
    return env


def make_start_review_message(
    code: str,
    language: str,
    file_name: str,
    line_offset: int = 0,
    user_id: Optional[int] = None,
    task_id: Optional[int] = None,
    project_id: Optional[int] = None,
    file_id: Optional[int] = None,
    trace_id: Optional[str] = None,
    extra_context: str = "",
) -> Message:
    """构造启动审查的 Message(供 Environment.publish 使用)

    Args:
        code: 待审查代码内容
        language: 编程语言
        file_name: 文件名
        line_offset: 行号偏移(分片时使用)
        user_id: 用户 ID
        task_id: 审查任务 ID
        project_id: 项目 ID
        file_id: 文件 ID
        trace_id: 调用链 ID
        extra_context: 额外上下文(规则/经验/画像等)

    Returns:
        Message: cause_by="StartReview" 的启动消息
    """
    content = (
        f"## 审查请求\n"
        f"- 文件: {file_name}\n"
        f"- 语言: {language}\n"
        f"- 行号偏移: {line_offset}\n\n"
        f"### 代码\n```\n{code}\n```\n"
    )
    if extra_context:
        content += f"\n### 额外上下文\n{extra_context}\n"
    metadata = {
        "user_id": user_id,
        "task_id": task_id,
        "project_id": project_id,
        "file_id": file_id,
        "language": language,
        "file_name": file_name,
        "line_offset": line_offset,
    }
    if trace_id:
        metadata["trace_id"] = trace_id
    return make_message(
        role="user",
        content=content,
        cause_by="StartReview",
        metadata=metadata,
    )


def make_discussion_message(
    speaker: str,
    content: str,
    user_id: Optional[int] = None,
    project_id: Optional[int] = None,
    file_id: Optional[int] = None,
    trace_id: Optional[str] = None,
    turn_id: Optional[int] = None,
) -> Message:
    """构造讨论发言 Message(供 Environment.publish 使用)

    Args:
        speaker: 发言者角色 code(user / code_reviewer / security_sentinel 等)
        content: 发言内容
        user_id: 用户 ID
        project_id: 项目 ID
        file_id: 文件 ID
        trace_id: 调用链 ID
        turn_id: 讨论轮次 ID(可选)

    Returns:
        Message: cause_by="DiscussTurn" 的讨论消息
    """
    metadata = {
        "user_id": user_id,
        "project_id": project_id,
        "file_id": file_id,
    }
    if trace_id:
        metadata["trace_id"] = trace_id
    if turn_id is not None:
        metadata["turn_id"] = turn_id
    return make_message(
        role=speaker,
        content=content,
        cause_by="DiscussTurn",
        metadata=metadata,
    )
