"""
MetaGPT 风格 Role 抽象基类 (v2.4)

Role 是 Environment 中的参与者,具备以下能力:
    - _watch: 订阅感兴趣的 cause_by 动作(空集合表示接收所有消息)
    - _react: 收到消息后的反应(子类实现)
    - _publish_message: 通过所属 Environment 广播消息

设计原则:
    1. 不强依赖 LLM,纯 Python 抽象;LLM 调用由具体子类(如 RoleAdapter)注入
    2. _react 是同步函数,异步需求由 Environment 的 run_async 控制
    3. 状态字段 self._state 用于 Role 生命周期管理(idle / thinking / done)

使用示例:
    class ReviewerRole(Role):
        def _watch(self) -> set[str]:
            return {"StartReview"}

        def _react(self, msg: Message) -> Optional[Message]:
            # 调用 LLM 审查代码,返回审查结果消息
            return make_message(
                role=self.name,
                content="发现 3 个问题...",
                cause_by="ReviewDone",
            )
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Optional, Set

from app.agents.metagpt.messages import Message, make_message

if TYPE_CHECKING:
    from app.agents.metagpt.environment import Environment


class Role:
    """MetaGPT 风格的抽象角色基类

    子类必须实现:
        - _watch: 返回订阅的 cause_by 集合(空集合表示接收所有消息)
        - _react: 收到消息后的反应,返回 None 表示不广播,返回 Message 表示广播

    Attributes:
        name: 角色 code(唯一,如 "code_reviewer")
        profile: 角色显示名(如 "代码审查员")
        goal: 角色目标描述
        constraints: 角色行为约束
    """

    name: str = "base_role"
    profile: str = "Base Role"
    goal: str = ""
    constraints: str = ""

    def __init__(self, name: Optional[str] = None, profile: Optional[str] = None):
        """初始化 Role

        Args:
            name: 可选,覆盖类属性 name
            profile: 可选,覆盖类属性 profile
        """
        if name is not None:
            self.name = name
        if profile is not None:
            self.profile = profile
        # 所属 Environment,由 Environment.add_role 注入
        self._environment: Optional["Environment"] = None
        # 角色状态:idle / thinking / acting / done
        self._state: str = "idle"
        # 角色本地记忆(收到的消息列表)
        self._memory: list[Message] = []

    def _watch(self) -> Set[str]:
        """返回订阅的 cause_by 集合

        Returns:
            Set[str]: 感兴趣的 cause_by 集合;空集合表示接收所有消息
        """
        return set()

    def _react(self, msg: Message) -> Optional[Message]:
        """收到消息后的反应

        子类必须实现。返回 None 表示不广播新消息,返回 Message 表示通过 Environment 广播。

        Args:
            msg: 收到的消息

        Returns:
            Optional[Message]: 要广播的新消息,None 表示不广播

        Raises:
            NotImplementedError: 子类未实现时抛出
        """
        raise NotImplementedError(f"Role {self.name} 未实现 _react")

    def _publish_message(self, msg: Message) -> None:
        """通过所属 Environment 广播消息

        Args:
            msg: 要广播的消息
        """
        if self._environment is None:
            return
        msg.sent_from = self.name
        if not msg.role:
            msg.role = self.name
        self._environment.publish(msg)

    def _remember(self, msg: Message) -> None:
        """把消息加入本地记忆

        Args:
            msg: 要记忆的消息
        """
        self._memory.append(msg)

    def _handle(self, msg: Message) -> None:
        """Environment 投递消息入口,内部串联记忆 + 反应 + 广播

        订阅过滤规则:
            - watched 为空:接收所有消息
            - watched 非空:仅当 cause_by 在 watched 中,或消息定向发给自己时才处理

        Args:
            msg: 收到的消息
        """
        # 检查订阅过滤
        watched = self._watch()
        is_directed_to_me = bool(msg.send_to) and msg.send_to == self.name
        if watched and msg.cause_by not in watched and not is_directed_to_me:
            return
        self._remember(msg)
        self._state = "thinking"
        try:
            new_msg = self._react(msg)
        except Exception as e:  # noqa: BLE001
            # 反应失败时,广播一条错误消息,避免阻塞 Environment
            self._state = "error"
            new_msg = make_message(
                role=self.name,
                content=f"角色 {self.name} 反应失败: {e}",
                cause_by="RoleError",
                metadata={"error": str(e), "origin_msg_id": msg.id},
            )
        if new_msg is not None:
            self._publish_message(new_msg)
        self._state = "idle"

    @property
    def memory(self) -> list[Message]:
        """返回本地记忆的只读视图"""
        return list(self._memory)

    def to_dict(self) -> dict:
        """转为可序列化 dict

        Returns:
            dict: 角色元数据
        """
        return {
            "name": self.name,
            "profile": self.profile,
            "goal": self.goal,
            "constraints": self.constraints,
            "state": self._state,
            "memory_size": len(self._memory),
        }
