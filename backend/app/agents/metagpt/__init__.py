"""
MetaGPT 风格的 Agent 编排层 (v2.4)

宏观调控多 Agent 系统,借鉴 MetaGPT 的 Environment / Role / Message / Action 四要素:
    - Environment: 容纳多个 Role,管理消息广播与生命周期
    - Role: 抽象角色基类,有自己的状态、动作和记忆
    - Message: 角色间通信的标准消息结构
    - Action: 角色可以执行的具体动作(本实现合并入 Role,不单独建类)

设计原则:
    1. **不破坏现有代码**: 现有 BaseAgent / Orchestrator / DiscussionOrchestrator 不改动
    2. **可选上层编排**: 通过 RoleAdapter 把 BaseAgent 适配为 Role,
       现有 review_service / discussion_orchestrator 可选择性使用 Environment
    3. **统一消息总线**: Message 通过 Environment.publish 广播,所有订阅的 Role 都能收到
    4. **可观测**: 每条 Message 同步转发到 AgentEventBus,前端 SSE 可见

模块导出:
    - Message: 标准消息结构
    - Role: 抽象角色基类
    - RoleAdapter: BaseAgent → Role 适配器
    - Environment: 编排环境
    - build_review_environment: 从审查任务构建 Environment
    - build_discussion_environment: 从圆桌讨论构建 Environment
"""
from app.agents.metagpt.environment import Environment
from app.agents.metagpt.factory import (
    build_discussion_environment,
    build_review_environment,
    make_discussion_message,
    make_start_review_message,
)
from app.agents.metagpt.messages import Message, make_message
from app.agents.metagpt.role import Role
from app.agents.metagpt.role_adapter import RoleAdapter

__all__ = [
    "Message",
    "make_message",
    "Role",
    "RoleAdapter",
    "Environment",
    "build_review_environment",
    "build_discussion_environment",
    "make_start_review_message",
    "make_discussion_message",
]
