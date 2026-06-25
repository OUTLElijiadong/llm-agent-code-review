"""
MetaGPT 风格 Environment (v2.4)

Environment 是多 Agent 编排的"舞台":
    - 容纳多个 Role,管理它们的注册与查询
    - 通过 publish() 广播 Message 给所有订阅的 Role
    - 通过 run() 同步执行一轮消息驱动(可选,现有 review_service 不依赖此入口)
    - 与 AgentEventBus 集成,把每条 Message 转换为 DISCUSS 事件,前端 SSE 可见

设计原则:
    1. 单实例隔离: 每次 Environment.run() 创建独立实例,不共享状态
    2. 不破坏现有代码: review_service / discussion_orchestrator 仍按原流程运行,
       Environment 是可选的上层编排工具
    3. 防止无限循环: 最大广播深度默认 8,超过即停止
    4. 错误隔离: 单个 Role 反应失败不阻塞 Environment,转为 RoleError 消息继续传播

使用示例:
    env = Environment(name="review_env", trace_id="review_123")
    env.add_role(role_a)
    env.add_role(role_b)
    env.publish(make_message(role="user", content="开始审查", cause_by="StartReview"))
    history = env.run(max_depth=4)
"""
from __future__ import annotations

from collections import deque
from typing import Deque, Dict, List, Optional

from loguru import logger

from app.agents.event_bus import AgentEventBus
from app.agents.events import AgentEvent, AgentEventType, new_trace_id
from app.agents.metagpt.messages import Message
from app.agents.metagpt.role import Role


class Environment:
    """MetaGPT 风格的 Agent 编排环境

    Attributes:
        name: 环境名称(用于日志与事件标识)
        trace_id: 调用链 ID(透传到 AgentEvent)
        _roles: 角色 code → Role 实例映射
        _history: 已广播的消息历史(按时间顺序)
        _queue: 待处理消息队列(由 publish 推入,run 消费)
        _max_depth: 单次 run() 最大广播深度,防止无限循环
    """

    DEFAULT_MAX_DEPTH = 8

    def __init__(
        self,
        name: str = "env",
        trace_id: Optional[str] = None,
        max_depth: int = DEFAULT_MAX_DEPTH,
    ) -> None:
        """初始化 Environment

        Args:
            name: 环境名称,用于日志与事件标识
            trace_id: 调用链 ID;None 时自动生成
            max_depth: 单次 run() 最大广播深度,防止无限循环
        """
        self.name: str = name
        self.trace_id: str = trace_id or new_trace_id()
        self._roles: Dict[str, Role] = {}
        self._history: List[Message] = []
        self._queue: Deque[Message] = deque()
        self._max_depth: int = max(1, min(max_depth, 32))

    # ────────────────── Role 管理 ──────────────────

    def add_role(self, role: Role) -> "Environment":
        """添加角色到环境

        Args:
            role: Role 实例

        Returns:
            Environment: self(链式调用)

        Raises:
            ValueError: 角色 name 重复
        """
        if role.name in self._roles:
            raise ValueError(f"角色 {role.name} 已存在")
        role._environment = self
        self._roles[role.name] = role
        logger.debug(f"[Env:{self.name}] 添加角色 {role.name}({role.profile})")
        return self

    def get_role(self, name: str) -> Optional[Role]:
        """按 name 获取角色

        Args:
            name: 角色 code

        Returns:
            Optional[Role]: 角色实例;不存在返回 None
        """
        return self._roles.get(name)

    def list_roles(self) -> List[str]:
        """列出所有角色 name

        Returns:
            List[str]: 角色 name 列表(按添加顺序)
        """
        return list(self._roles.keys())

    # ────────────────── 消息广播 ──────────────────

    def publish(self, msg: Message) -> None:
        """广播消息给所有订阅的 Role,并同步到 AgentEventBus

        流程:
            1. 把 msg 加入 _history 与 _queue
            2. 同步发布 DISCUSS 事件到 AgentEventBus(SSE 可见)
            3. 由 run() 消费 _queue 触发各 Role._handle()

        Args:
            msg: 要广播的消息
        """
        if not msg.role:
            msg.role = "system"
        if not msg.sent_from:
            msg.sent_from = msg.role
        self._history.append(msg)
        self._queue.append(msg)
        self._emit_discuss_event(msg)

    def _emit_discuss_event(self, msg: Message) -> None:
        """把 Message 转换为 AgentEvent 并发布到 AgentEventBus

        使前端 SSE 订阅者能实时看到 Environment 中的消息流。

        Args:
            msg: 要广播的消息
        """
        try:
            user_id = msg.metadata.get("user_id") if msg.metadata else None
            payload = {
                "env": self.name,
                "message_id": msg.id,
                "role": msg.role,
                "send_to": msg.send_to,
                "cause_by": msg.cause_by,
                "content_preview": (msg.content or "")[:200],
                "metadata": msg.metadata,
            }
            AgentEventBus.instance().publish(AgentEvent(
                type=AgentEventType.DISCUSS,
                agent=msg.role or "system",
                trace_id=self.trace_id,
                message=f"[{self.name}] {msg.role} → {msg.send_to or 'all'}: {msg.cause_by}",
                payload=payload,
                user_id=user_id,
            ))
        except Exception as e:
            logger.warning(f"[Env:{self.name}] emit DISCUSS 事件失败: {e}")

    # ────────────────── 运行 ──────────────────

    def run(self, max_depth: Optional[int] = None) -> List[Message]:
        """同步消费 _queue,触发订阅 Role 的反应,直到队列空或达到最大深度

        每条消息出队后,所有订阅该 cause_by 的 Role 都会调用 _handle(),
        Role._react() 返回的新消息会再次进入 _queue,实现链式广播。

        Args:
            max_depth: 单次 run 的最大广播深度;None 使用 self._max_depth

        Returns:
            List[Message]: 本次 run 期间产生的所有消息(按时间顺序,即 _history 增量)
        """
        depth_limit = max_depth or self._max_depth
        start_history_len = len(self._history)
        depth = 0
        while self._queue and depth < depth_limit:
            depth += 1
            current_msg = self._queue.popleft()
            self._dispatch_to_roles(current_msg)
        if self._queue:
            logger.warning(
                f"[Env:{self.name}] 达到最大深度 {depth_limit},"
                f"剩余 {len(self._queue)} 条消息未处理"
            )
        return self._history[start_history_len:]

    def _dispatch_to_roles(self, msg: Message) -> None:
        """把消息投递给所有订阅的 Role

        订阅规则(由 Role._watch 决定):
            - 空 _watch 集合: 接收所有消息
            - 非空 _watch 集合: cause_by 命中或 send_to 命中时接收
            - send_to 非空且不等于自己 name: 跳过(定向消息)

        Args:
            msg: 要投递的消息
        """
        for role_name, role in self._roles.items():
            # 定向消息:仅目标角色处理
            if msg.send_to and msg.send_to != role_name:
                continue
            # 跳过自己发出的消息,避免自循环
            if msg.sent_from == role_name:
                continue
            try:
                role._handle(msg)
            except Exception as e:
                logger.warning(
                    f"[Env:{self.name}] 角色 {role_name} 处理消息 {msg.id} 异常: {e}"
                )

    # ────────────────── 查询 ──────────────────

    @property
    def history(self) -> List[Message]:
        """返回消息历史的只读视图

        Returns:
            List[Message]: 历史消息列表(按时间顺序)
        """
        return list(self._history)

    def history_by_role(self, role_name: str) -> List[Message]:
        """筛选指定角色发出的消息

        Args:
            role_name: 角色 code

        Returns:
            List[Message]: 该角色发出的所有消息
        """
        return [m for m in self._history if m.sent_from == role_name]

    def history_by_cause(self, cause_by: str) -> List[Message]:
        """筛选指定 cause_by 的消息

        Args:
            cause_by: 动作名

        Returns:
            List[Message]: 该 cause_by 的所有消息
        """
        return [m for m in self._history if m.cause_by == cause_by]

    def to_dict(self) -> dict:
        """转为可序列化 dict(用于调试/日志)

        Returns:
            dict: 环境元数据 + 角色列表 + 消息计数
        """
        return {
            "name": self.name,
            "trace_id": self.trace_id,
            "roles": [r.to_dict() for r in self._roles.values()],
            "message_count": len(self._history),
            "pending_count": len(self._queue),
        }
