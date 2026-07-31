"""
MetaGPT 风格 RoleAdapter (v2.4)

把现有 BaseAgent 适配为 MetaGPT Role,使 Environment 能统一编排现有 Agent。
设计原则:
    1. 不修改 BaseAgent 源码,通过组合(composition)持有 BaseAgent 实例
    2. _react 委托给 BaseAgent.call() / call_json()
    3. AgentResult → Message 转换由 _result_to_message 完成
    4. 通过 watch_actions 声明订阅的 cause_by 集合,实现选择性广播

使用示例:
    adapter = RoleAdapter(
        agent=code_reviewer,
        name="code_reviewer",
        profile="代码审查员",
        watch_actions={"StartReview", "CrossReview"},
        react_action="ReviewCode",
    )
    env.add_role(adapter)
"""
from __future__ import annotations

from typing import Optional, Set

from loguru import logger

from app.agents.base import AgentContext, AgentResult, BaseAgent
from app.agents.metagpt.messages import Message, make_message
from app.agents.metagpt.role import Role


class RoleAdapter(Role):
    """BaseAgent → Role 适配器

    把现有 BaseAgent 包装为 MetaGPT Role,使 Environment 能统一编排。

    Attributes:
        _agent: 被包装的 BaseAgent 实例
        _watch_actions: 订阅的 cause_by 集合(空集合表示接收所有消息)
        _react_action: 本角色反应时发出的 cause_by(用于下游订阅过滤)
        _ctx_template: 默认 AgentContext 模板(可被 Message.metadata 覆盖)
    """

    def __init__(
        self,
        agent: BaseAgent,
        name: Optional[str] = None,
        profile: Optional[str] = None,
        goal: str = "",
        constraints: str = "",
        watch_actions: Optional[Set[str]] = None,
        react_action: str = "AgentReply",
        ctx_template: Optional[AgentContext] = None,
    ) -> None:
        """初始化 RoleAdapter

        Args:
            agent: 被包装的 BaseAgent 实例(共享引用,不复制)
            name: 角色 code,默认使用 agent.name
            profile: 角色显示名,默认使用 agent.description
            goal: 角色目标描述
            constraints: 角色行为约束
            watch_actions: 订阅的 cause_by 集合;None 或空集合表示接收所有消息
            react_action: 本角色反应时发出的 cause_by,用于下游订阅过滤
            ctx_template: 默认 AgentContext 模板;Message.metadata 中的
                user_id/task_id/project_id/file_id/trace_id 会覆盖模板对应字段
        """
        super().__init__(name=name or agent.name, profile=profile or agent.description)
        self.goal = goal or f"调用 {self.name} 完成专项任务"
        self.constraints = constraints or "遵循项目现有规范与 Agent 系统提示词"
        self._agent: BaseAgent = agent
        self._watch_actions: Set[str] = set(watch_actions) if watch_actions else set()
        self._react_action: str = react_action
        self._ctx_template: AgentContext = ctx_template or AgentContext()

    def _watch(self) -> Set[str]:
        """返回订阅的 cause_by 集合

        Returns:
            Set[str]: 订阅的 cause_by 集合;空集合表示接收所有消息
        """
        return self._watch_actions

    def _react(self, msg: Message) -> Optional[Message]:
        """收到消息后,委托给 BaseAgent.call() 执行,把结果转为 Message

        流程:
            1. 把 Message.content 作为 user_message 传入 BaseAgent.call()
            2. 把 Message.metadata 合并到 AgentContext(覆盖模板)
            3. AgentResult.success → make_message(cause_by=self._react_action)
            4. AgentResult 失败 → make_message(cause_by="AgentError", content=error)

        Args:
            msg: 收到的消息

        Returns:
            Optional[Message]: 反应产生的新消息;None 表示不广播
        """
        ctx = self._build_ctx_from_msg(msg)
        user_message = msg.content or ""
        self._state = "thinking"
        try:
            result: AgentResult = self._agent.call(user_message, ctx=ctx)
        except Exception as e:
            logger.warning(f"[RoleAdapter:{self.name}] 调用异常: {e}")
            self._state = "error"
            return make_message(
                role=self.name,
                content=f"Agent {self.name} 调用异常: {e}",
                cause_by="AgentError",
                metadata={
                    "origin_msg_id": msg.id,
                    "agent_name": self.name,
                    "error": str(e),
                },
            )

        return self._result_to_message(result, msg)

    def _build_ctx_from_msg(self, msg: Message) -> AgentContext:
        """从 Message.metadata 构造 AgentContext(覆盖模板字段)

        Args:
            msg: 触发本次反应的消息

        Returns:
            AgentContext: 合并后的上下文(user_id/task_id/project_id/file_id/trace_id 取自 metadata)
        """
        meta = msg.metadata or {}
        extra = dict(self._ctx_template.extra or {})
        # 透传 trace_id(若 metadata 中存在)
        if "trace_id" in meta:
            extra["trace_id"] = meta["trace_id"]
        # 透传其他扩展字段
        for k, v in meta.items():
            if k in {"user_id", "task_id", "project_id", "file_id", "trace_id"}:
                continue
            extra.setdefault(k, v)
        return AgentContext(
            user_id=meta.get("user_id") or self._ctx_template.user_id,
            task_id=meta.get("task_id") or self._ctx_template.task_id,
            project_id=meta.get("project_id") or self._ctx_template.project_id,
            file_id=meta.get("file_id") or self._ctx_template.file_id,
            extra=extra,
        )

    def _result_to_message(self, result: AgentResult, origin_msg: Message) -> Optional[Message]:
        """把 AgentResult 转换为 Message

        成功时: cause_by=self._react_action,content=result.data(字符串或 JSON 序列化)
        失败时: cause_by="AgentError",content=result.error

        Args:
            result: BaseAgent 调用结果
            origin_msg: 触发本次反应的原消息(用于透传 metadata)

        Returns:
            Optional[Message]: 转换后的消息;始终返回非 None,失败也广播错误消息
        """
        # 透传上游 metadata,确保链路 user_id/task_id/project_id/file_id 不丢失
        out_meta = {
            k: v for k, v in (origin_msg.metadata or {}).items()
            if k in {"user_id", "task_id", "project_id", "file_id", "trace_id"}
        }
        out_meta["agent_name"] = self.name
        out_meta["origin_msg_id"] = origin_msg.id
        if result.success:
            content = result.data if isinstance(result.data, str) else str(result.data)
            out_meta["model"] = result.model
            out_meta["duration_ms"] = result.duration_ms
            if result.tokens:
                out_meta["tokens"] = result.tokens
            return make_message(
                role=self.name,
                content=content,
                cause_by=self._react_action,
                message_type="task.result",
                correlation_id=origin_msg.id,
                payload={"data": result.data},
                artifacts=(result.data.get("artifacts", []) if isinstance(result.data, dict) else []),
                metadata=out_meta,
            )
        out_meta["error"] = result.error or "未知错误"
        return make_message(
            role=self.name,
            content=f"Agent {self.name} 调用失败: {result.error}",
            cause_by="AgentError",
            message_type="task.error",
            correlation_id=origin_msg.id,
            errors=[{"code": "agent_call_failed", "message": result.error or "未知错误"}],
            metadata=out_meta,
        )

    def to_dict(self) -> dict:
        """转为可序列化 dict(含 BaseAgent 元数据)

        Returns:
            dict: 角色元数据 + 关联 Agent 元数据
        """
        base = super().to_dict()
        base.update({
            "agent_name": self._agent.name,
            "agent_description": self._agent.description,
            "react_action": self._react_action,
            "watch_actions": sorted(self._watch_actions),
        })
        return base
