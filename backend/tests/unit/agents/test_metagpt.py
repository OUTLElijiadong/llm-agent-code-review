"""单元测试:MetaGPT 编排层(v2.4)

验证:
1. Message 创建、序列化、反序列化、with_metadata
2. Role 订阅过滤(_watch + _handle)
3. Environment 消息广播、链式反应、深度限制
4. RoleAdapter 把 BaseAgent 适配为 Role(Mock LLM 调用)
5. factory 函数构建 Environment(不依赖 LLM 调用)
"""
from __future__ import annotations

import pytest

from app.agents.base import AgentResult, BaseAgent
from app.agents.metagpt import (
    Environment,
    Message,
    Role,
    RoleAdapter,
    build_discussion_environment,
    build_review_environment,
    make_discussion_message,
    make_message,
    make_start_review_message,
)

# ============ Message 测试 ============


class TestMessage:
    """Message 数据结构测试"""

    def test_make_message_basic(self):
        """测试 make_message 基本构造

        验证:
            - role/sent_from 一致
            - content/cause_by 正确传入
            - metadata 默认为空 dict
            - id 自动生成且唯一
        """
        msg = make_message(role="user", content="hello", cause_by="Start")
        assert msg.role == "user"
        assert msg.sent_from == "user"
        assert msg.content == "hello"
        assert msg.cause_by == "Start"
        assert msg.metadata == {}
        assert msg.id.startswith("msg_")

    def test_message_to_dict(self):
        """测试 to_dict 序列化包含所有字段"""
        msg = make_message(
            role="agent_a",
            content="response",
            cause_by="Reply",
            metadata={"user_id": 1},
        )
        d = msg.to_dict()
        assert set(d.keys()) == {
            "id", "role", "send_to", "content", "cause_by",
            "sent_from", "schema_version", "message_type", "correlation_id",
            "payload", "artifacts", "errors", "metadata", "timestamp",
        }
        assert d["role"] == "agent_a"
        assert d["metadata"]["user_id"] == 1

    def test_message_from_dict_roundtrip(self):
        """测试 from_dict 反序列化与原对象等价"""
        original = make_message(
            role="x", content="y", cause_by="Z",
            send_to="target", metadata={"k": "v"},
        )
        restored = Message.from_dict(original.to_dict())
        assert restored.id == original.id
        assert restored.role == original.role
        assert restored.content == original.content
        assert restored.cause_by == original.cause_by
        assert restored.send_to == original.send_to
        assert restored.metadata == original.metadata

    def test_with_metadata_returns_new_instance(self):
        """测试 with_metadata 返回新实例,原实例不变"""
        msg = make_message(role="u", content="c", cause_by="A")
        new_msg = msg.with_metadata(user_id=1, trace_id="t1")
        assert msg.metadata == {}  # 原实例不变
        assert new_msg.metadata == {"user_id": 1, "trace_id": "t1"}
        assert new_msg.id == msg.id  # id 保留


# ============ Role + Environment 测试 ============


class _MockRole(Role):
    """测试用 Mock 角色,不做 LLM 调用"""

    def __init__(self, name, watch_actions=None, react_action="MockReply"):
        super().__init__(name=name, profile=f"Mock {name}")
        self._mock_watch = set(watch_actions) if watch_actions else set()
        self._react_action = react_action

    def _watch(self):
        return self._mock_watch

    def _react(self, msg):
        return make_message(
            role=self.name,
            content=f"{self.name} reply",
            cause_by=self._react_action,
            metadata={"user_id": msg.metadata.get("user_id")},
        )


class TestRoleSubscription:
    """Role 订阅过滤测试"""

    def test_empty_watch_receives_all(self):
        """空 watch 集合应接收所有消息"""
        role = _MockRole("a", watch_actions=set())
        msg = make_message(role="user", content="x", cause_by="Anything")
        role._handle(msg)
        assert len(role.memory) == 1

    def test_non_empty_watch_filters_by_cause(self):
        """非空 watch 应过滤不在订阅列表的 cause_by"""
        role = _MockRole("a", watch_actions={"StartReview"})
        # cause_by 在 watch 中 → 处理
        msg_in = make_message(role="user", content="x", cause_by="StartReview")
        role._handle(msg_in)
        assert len(role.memory) == 1
        # cause_by 不在 watch 中,且是广播 → 不处理
        msg_out = make_message(role="user", content="y", cause_by="OtherAction")
        role._handle(msg_out)
        assert len(role.memory) == 1  # 仍然是 1,没有增加

    def test_directed_message_bypasses_watch(self):
        """定向消息(send_to=自己)应绕过 watch 过滤"""
        role = _MockRole("a", watch_actions={"StartReview"})
        # 定向发给 role_a,即使 cause_by 不在 watch 中
        msg = make_message(
            role="user", content="x", cause_by="OtherAction", send_to="a",
        )
        role._handle(msg)
        assert len(role.memory) == 1


class TestEnvironment:
    """Environment 编排测试"""

    def test_single_role_single_message(self):
        """单角色 + 单消息:Environment 广播后角色应反应"""
        env = Environment(name="t1", trace_id="t1")
        env.add_role(_MockRole("agent_a", watch_actions={"StartReview"}, react_action="A_Reply"))
        env.publish(make_message(role="user", content="go", cause_by="StartReview"))
        env.run()
        # user 消息 + agent_a 反应 = 2
        assert len(env.history) == 2
        assert env.history[1].sent_from == "agent_a"
        assert env.history[1].cause_by == "A_Reply"

    def test_chain_broadcast(self):
        """双角色链式广播:agent_a 反应后,agent_b 应订阅到并反应"""
        env = Environment(name="t2", trace_id="t2", max_depth=5)
        env.add_role(_MockRole("agent_a", watch_actions={"StartReview"}, react_action="AgentA_Reply"))
        env.add_role(_MockRole("agent_b", watch_actions={"AgentA_Reply"}, react_action="AgentB_Reply"))
        env.publish(make_message(role="user", content="start", cause_by="StartReview"))
        env.run()
        # user → agent_a → agent_b = 3
        assert len(env.history) == 3
        assert env.history[0].cause_by == "StartReview"
        assert env.history[1].cause_by == "AgentA_Reply"
        assert env.history[2].cause_by == "AgentB_Reply"

    def test_max_depth_limit(self):
        """最大深度限制:互相订阅的两个角色应被 max_depth 截断"""
        env = Environment(name="t3", trace_id="t3", max_depth=2)
        env.add_role(_MockRole("pinger", watch_actions={"Start", "Pong"}, react_action="Ping"))
        env.add_role(_MockRole("ponger", watch_actions={"Ping"}, react_action="Pong"))
        env.publish(make_message(role="user", content="start", cause_by="Start"))
        env.run()
        # max_depth=2:处理 2 条消息 → user + Ping + Pong = 3
        assert len(env.history) == 3

    def test_subscription_filter(self):
        """订阅过滤:不订阅 StartReview 的角色不应反应"""
        env = Environment(name="t4", trace_id="t4")
        env.add_role(_MockRole("agent_a", watch_actions={"StartReview"}, react_action="A_Reply"))
        env.add_role(_MockRole("agent_b", watch_actions={"OtherAction"}, react_action="B_Reply"))
        env.publish(make_message(role="user", content="go", cause_by="StartReview"))
        env.run()
        # 只有 agent_a 反应
        assert len(env.history) == 2
        assert env.history[1].sent_from == "agent_a"

    def test_directed_message(self):
        """定向消息:send_to 指定的角色即使不订阅也应处理"""
        env = Environment(name="t5", trace_id="t5")
        env.add_role(_MockRole("agent_a", watch_actions={"Trigger"}, react_action="A_Reply"))
        env.add_role(_MockRole("agent_b", watch_actions={"OtherAction"}, react_action="B_Reply"))
        # 定向发给 agent_b,即使 cause_by 不在 agent_b 的 watch 中
        env.publish(make_message(
            role="user", content="direct", cause_by="StartReview", send_to="agent_b",
        ))
        env.run()
        # agent_a:cause_by 不在 watch,不是定向给它 → 跳过
        # agent_b:定向给它,即使 cause_by 不在 watch → 处理
        assert len(env.history) == 2
        assert env.history[1].sent_from == "agent_b"

    def test_history_filters(self):
        """history_by_role / history_by_cause 筛选"""
        env = Environment(name="t6", trace_id="t6")
        env.add_role(_MockRole("agent_x", watch_actions={"Trigger"}, react_action="X_Reply"))
        env.publish(make_message(role="user", content="go", cause_by="Trigger"))
        env.run()
        assert len(env.history_by_role("agent_x")) == 1
        assert len(env.history_by_role("user")) == 1
        assert len(env.history_by_cause("Trigger")) == 1
        assert len(env.history_by_cause("X_Reply")) == 1

    def test_add_duplicate_role_raises(self):
        """添加重复角色 name 应抛 ValueError"""
        env = Environment(name="t7", trace_id="t7")
        env.add_role(_MockRole("dup"))
        with pytest.raises(ValueError, match="已存在"):
            env.add_role(_MockRole("dup"))

    def test_unknown_target_and_out_of_boundary_route_are_rejected(self):
        """定向消息必须命中已注册目标且通过双向协作白名单。"""
        env = Environment(name="boundary", trace_id="boundary")
        env.add_role(_MockRole("project_manager"))
        with pytest.raises(ValueError, match="目标不存在"):
            env.publish(make_message(
                role="code_reviewer", content="x", cause_by="Delegate",
                send_to="missing_agent",
            ))
        with pytest.raises(ValueError, match="协作越界"):
            env.publish(make_message(
                role="code_reviewer", content="x", cause_by="Delegate",
                send_to="project_manager",
            ))


# ============ RoleAdapter 测试 ============


class _StubAgent(BaseAgent):
    """测试用 Stub Agent,不实际调用 LLM"""

    name = "stub_agent"
    description = "Stub Agent for testing"

    def __init__(self, result: AgentResult):
        # 跳过 BaseAgent.__init__ 的 settings 依赖
        self._result = result
        self.name = "stub_agent"
        self.description = "Stub Agent for testing"

    def call(self, user_message, ctx=None, json_mode=False, api_config=None):
        return self._result


class TestRoleAdapter:
    """RoleAdapter 适配器测试"""

    def test_successful_reaction(self):
        """Agent 调用成功 → 返回 react_action 消息"""
        expected = AgentResult(
            success=True, data="审查通过", model="test-model",
            duration_ms=100, tokens={"total": 50},
        )
        agent = _StubAgent(expected)
        adapter = RoleAdapter(
            agent=agent, name="reviewer",
            watch_actions={"StartReview"}, react_action="ReviewDone",
        )
        env = Environment(name="adapter_t1", trace_id="at1")
        env.add_role(adapter)
        env.publish(make_message(
            role="user", content="审查代码", cause_by="StartReview",
            metadata={"user_id": 1, "task_id": 10, "project_id": 100},
        ))
        env.run()
        # user + adapter 反应 = 2
        assert len(env.history) == 2
        result_msg = env.history[1]
        assert result_msg.sent_from == "reviewer"
        assert result_msg.cause_by == "ReviewDone"
        assert result_msg.content == "审查通过"
        assert result_msg.message_type == "task.result"
        assert result_msg.correlation_id == env.history[0].id
        assert result_msg.payload == {"data": "审查通过"}
        # metadata 透传
        assert result_msg.metadata["user_id"] == 1
        assert result_msg.metadata["task_id"] == 10
        assert result_msg.metadata["project_id"] == 100
        assert result_msg.metadata["model"] == "test-model"
        assert result_msg.metadata["duration_ms"] == 100
        assert result_msg.metadata["tokens"]["total"] == 50

    def test_failed_reaction(self):
        """Agent 调用失败 → 返回 AgentError 消息"""
        expected = AgentResult(success=False, error="API 超时")
        agent = _StubAgent(expected)
        adapter = RoleAdapter(
            agent=agent, name="reviewer",
            watch_actions={"StartReview"}, react_action="ReviewDone",
        )
        env = Environment(name="adapter_t2", trace_id="at2")
        env.add_role(adapter)
        env.publish(make_message(role="user", content="x", cause_by="StartReview"))
        env.run()
        assert len(env.history) == 2
        result_msg = env.history[1]
        assert result_msg.cause_by == "AgentError"
        assert "API 超时" in result_msg.content
        assert result_msg.metadata["error"] == "API 超时"
        assert result_msg.message_type == "task.error"
        assert result_msg.correlation_id == env.history[0].id
        assert result_msg.errors == [{"code": "agent_call_failed", "message": "API 超时"}]


# ============ factory 函数测试 ============


class TestFactory:
    """factory 函数测试(不依赖 LLM 调用)"""

    def test_build_review_environment(self):
        """build_review_environment 应返回 review_env 命名的 Environment"""
        env = build_review_environment(
            trace_id="factory_t1", user_id=1, task_id=1, project_id=1, file_id=1,
        )
        assert env.name == "review_env"
        assert env.trace_id == "factory_t1"
        # 即使 Agent 未注册,也不应抛异常(只 warning)
        assert isinstance(env, Environment)

    def test_build_discussion_environment(self):
        """build_discussion_environment 应返回 discussion_env 命名的 Environment"""
        env = build_discussion_environment(
            trace_id="factory_t2", user_id=1, project_id=1, file_id=1,
        )
        assert env.name == "discussion_env"
        assert env.trace_id == "factory_t2"

    def test_make_start_review_message(self):
        """make_start_review_message 应返回 cause_by=StartReview 的消息"""
        msg = make_start_review_message(
            code="print(1)", language="python", file_name="t.py",
            user_id=1, task_id=2, project_id=3, file_id=4, trace_id="trc1",
        )
        assert msg.cause_by == "StartReview"
        assert msg.role == "user"
        assert msg.metadata["user_id"] == 1
        assert msg.metadata["task_id"] == 2
        assert msg.metadata["project_id"] == 3
        assert msg.metadata["file_id"] == 4
        assert msg.metadata["trace_id"] == "trc1"
        assert msg.metadata["language"] == "python"
        assert "print(1)" in msg.content

    def test_make_discussion_message(self):
        """make_discussion_message 应返回 cause_by=DiscussTurn 的消息"""
        msg = make_discussion_message(
            speaker="code_reviewer", content="我发现一个 bug",
            user_id=1, project_id=2, file_id=3, trace_id="t", turn_id=1,
        )
        assert msg.cause_by == "DiscussTurn"
        assert msg.role == "code_reviewer"
        assert msg.metadata["user_id"] == 1
        assert msg.metadata["turn_id"] == 1
