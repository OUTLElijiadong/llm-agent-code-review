"""Agent 中心 API 的鉴权、SSE、澄清、MetaGPT 与 Skill 补充测试。"""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any, Iterator, Optional
from unittest.mock import MagicMock

import pytest

from app.agents.base import AgentResult
from app.agents.clarify_store import ClarifyStore
from app.agents.events import AgentEvent, AgentEventType
from app.ai.exceptions import AiServiceError
from app.api.v1 import agents as module
from app.core.exceptions import AuthError, ForbiddenError
from app.schemas.agent import SkillInvokeIn


class FakeSubscription:
    """按预置顺序向 SSE 端点提供有限事件。"""

    def __init__(self, events: list[AgentEvent]):
        """保存待发送事件；参数 events 为事件顺序列表，无返回值。"""
        self._events = iter(events)

    def __aiter__(self) -> "FakeSubscription":
        """返回异步迭代器自身；无参数，返回当前订阅对象。"""
        return self

    async def __anext__(self) -> AgentEvent:
        """返回下一条事件；无参数，耗尽时抛出 StopAsyncIteration。"""
        try:
            return next(self._events)
        except StopIteration as exc:
            raise StopAsyncIteration from exc


class FakeEventBus:
    """记录 replay 参数并返回有限订阅流。"""

    def __init__(self, events: list[AgentEvent]):
        """保存事件列表；参数 events 为订阅数据，无返回值。"""
        self.events = events
        self.replays: list[int] = []

    def subscribe(self, replay: int = 0) -> FakeSubscription:
        """创建订阅；参数 replay 为回放数，返回 FakeSubscription。"""
        self.replays.append(replay)
        return FakeSubscription(self.events)


class TimeoutThenCancel:
    """先制造一次 SSE 心跳，再模拟客户端取消连接。"""

    def __init__(self):
        """初始化调用计数；无参数，无返回值。"""
        self.calls = 0

    async def __call__(self, awaitable: Any, timeout: float) -> Any:
        """关闭传入协程；首次抛超时，后续抛取消，无正常返回值。"""
        self.calls += 1
        if hasattr(awaitable, "close"):
            awaitable.close()
        assert timeout == 25.0
        if self.calls == 1:
            raise asyncio.TimeoutError
        raise asyncio.CancelledError


class FakeChatAgent:
    """记录澄清续跑参数并返回预置 AgentResult。"""

    def __init__(self, result: AgentResult):
        """保存结果；参数 result 为调度返回值，无返回值。"""
        self.result = result
        self.calls: list[tuple[str, dict[str, Any], Any]] = []

    def dispatch_with_payload(self, intent: str, payload: dict[str, Any], ctx: Any) -> AgentResult:
        """记录 intent、payload、ctx；返回构造时传入的 AgentResult。"""
        self.calls.append((intent, payload, ctx))
        return self.result


class FakeRole:
    """提供 MetaGPT 角色的最小序列化接口。"""

    def __init__(self, name: str):
        """保存角色名；参数 name 为角色标识，无返回值。"""
        self.name = name

    def to_dict(self) -> dict[str, Any]:
        """返回角色元数据；无参数，返回包含 name 的字典。"""
        return {"name": self.name, "actions": ["review"]}


class FakeEnvironment:
    """提供 MetaGPT 预览端点所需的环境接口。"""

    def __init__(self, trace_id: str):
        """初始化环境；参数 trace_id 为链路标识，无返回值。"""
        self.name = "preview-env"
        self.trace_id = trace_id
        self._max_depth = 3
        self._roles = {
            "reviewer": FakeRole("reviewer"),
            "plain": FakeRole("plain"),
            "missing": None,
        }

    def list_roles(self) -> list[str]:
        """返回角色名列表；无参数，返回所有角色键。"""
        return list(self._roles)

    def get_role(self, name: str) -> Optional[FakeRole]:
        """按名称读取角色；参数 name 为角色名，返回角色或 None。"""
        return self._roles.get(name)


class FakeRegistry:
    """为 MetaGPT API 提供稳定的注册中心数据。"""

    def list_runtime(self) -> list[dict[str, Any]]:
        """返回可适配 Agent 元数据；无参数，返回运行时字典列表。"""
        return [
            {
                "name": "reviewer",
                "description": "review code",
                "category": "review",
                "icon": "shield",
                "color": "#123456",
            }
        ]

    def list(self) -> dict[str, str]:
        """返回注册 Agent 映射；无参数，返回名称到描述的映射。"""
        return {"reviewer": "review code", "plain": "plain role"}

    def get(self, name: str) -> Any:
        """按名称返回 Agent；参数 name 为角色名，返回对象或 None。"""
        if name == "reviewer":
            return SimpleNamespace(icon="shield", color="#123456", category="review")
        return None


async def _collect_stream(response: Any) -> list[str]:
    """消费有限 StreamingResponse；参数 response 为响应，返回字符串块列表。"""
    chunks: list[str] = []
    async for chunk in response.body_iterator:
        if isinstance(chunk, bytes):
            chunks.append(chunk.decode())
        else:
            chunks.append(chunk)
    return chunks


def _event(trace_id: str, user_id: Optional[int]) -> AgentEvent:
    """构造 SSE 事件；参数为 trace_id 与归属用户，返回 AgentEvent。"""
    return AgentEvent(
        type=AgentEventType.PROGRESS,
        agent="reviewer",
        trace_id=trace_id,
        payload={"trace": trace_id},
        user_id=user_id,
    )


@pytest.fixture
def clarify_store() -> Iterator[ClarifyStore]:
    """隔离 ClarifyStore 单例；无业务参数，返回全新临时存储。"""
    original = ClarifyStore._instance
    store = ClarifyStore()
    ClarifyStore._instance = store
    try:
        yield store
    finally:
        ClarifyStore._instance = original


def test_basic_agent_routes_adapt_service_payloads(monkeypatch: pytest.MonkeyPatch) -> None:
    """基础、运行时与态势路由应按普通用户 ID 调用服务并序列化结果。"""
    profile = {
        "code": "security",
        "name": "Security",
        "focus": "security",
        "issue_types": ["漏洞"],
        "instruction": "scan",
    }
    mapping = {"review_type": "security", "label": "安全", "agent_codes": ["security"]}
    usage = {"code": "security", "name": "Security", "call_count": 2}
    runtime = {
        "code": "security_sentinel",
        "name": "Security Sentinel",
        "skills": [{"name": "scan"}],
    }
    overview = {"agents": [profile], "type_mappings": [mapping], "usage": [usage]}
    situation = {"online": 2, "idle": 1, "working": 1, "today_calls": 9}
    db = object()
    user = SimpleNamespace(id=7, role="member")

    monkeypatch.setattr(module.agent_service, "list_profiles", MagicMock(return_value=[profile]))
    monkeypatch.setattr(module.agent_service, "list_type_mappings", MagicMock(return_value=[mapping]))
    usage_mock = MagicMock(return_value=[usage])
    overview_mock = MagicMock(return_value=overview)
    runtime_mock = MagicMock(return_value=[runtime])
    situation_mock = MagicMock(return_value=situation)
    monkeypatch.setattr(module.agent_service, "get_usage", usage_mock)
    monkeypatch.setattr(module.agent_service, "get_overview", overview_mock)
    monkeypatch.setattr(module.agent_service, "get_runtime_agents", runtime_mock)
    monkeypatch.setattr(
        module.agent_service,
        "get_runtime_summary",
        MagicMock(return_value={"total": 2, "by_category": [{"category": "review", "count": 2}]}),
    )
    monkeypatch.setattr(module.agent_service, "get_situation", situation_mock)

    assert module.list_agents(user).data[0].code == "security"
    assert module.list_type_mappings(user).data[0].agent_codes == ["security"]
    assert module.get_usage(db, user).data[0].call_count == 2
    assert module.get_overview(db, user).data.agents[0].name == "Security"
    assert module.list_runtime_agents(db, user).data[0].skills == ["scan"]
    assert module.get_runtime_summary(user).data.total == 2
    assert module.get_situation(30, db, user).data.today_calls == 9
    usage_mock.assert_called_once_with(db, 7)
    overview_mock.assert_called_once_with(db, 7)
    runtime_mock.assert_called_once_with(db, 7)
    situation_mock.assert_called_once_with(db, 7, 30)


def test_admin_agent_routes_request_unscoped_data(monkeypatch: pytest.MonkeyPatch) -> None:
    """管理员统计、运行时和态势查询应以 None 用户 ID 请求全局数据。"""
    usage_mock = MagicMock(return_value=[])
    overview_mock = MagicMock(return_value={"agents": [], "type_mappings": [], "usage": []})
    runtime_mock = MagicMock(return_value=[])
    situation_mock = MagicMock(return_value={"online": 0})
    monkeypatch.setattr(module.agent_service, "get_usage", usage_mock)
    monkeypatch.setattr(module.agent_service, "get_overview", overview_mock)
    monkeypatch.setattr(module.agent_service, "get_runtime_agents", runtime_mock)
    monkeypatch.setattr(module.agent_service, "get_situation", situation_mock)
    db = object()
    admin = SimpleNamespace(id=1, role="admin")

    assert module.get_usage(db, admin).data == []
    assert module.get_overview(db, admin).data.agents == []
    assert module.list_runtime_agents(db, admin).data == []
    assert module.get_situation(60, db, admin).data.online == 0
    usage_mock.assert_called_once_with(db, None)
    overview_mock.assert_called_once_with(db, None)
    runtime_mock.assert_called_once_with(db, None)
    situation_mock.assert_called_once_with(db, None, 60)


def test_resolve_sse_user_accepts_bearer_and_query_tokens(monkeypatch: pytest.MonkeyPatch) -> None:
    """SSE 鉴权应优先 Bearer token，并在无 Bearer 时接受查询参数 token。"""
    user = SimpleNamespace(id=7, status=1)
    db = SimpleNamespace(get=MagicMock(return_value=user))
    decoder = MagicMock(return_value={"sub": "7"})
    monkeypatch.setattr(module, "decode_token", decoder)

    assert module._resolve_sse_user("Bearer header-token", "query-token", db) is user
    decoder.assert_called_once_with("header-token")
    db.get.assert_called_once_with(module.User, 7)

    decoder.reset_mock()
    db.get.reset_mock()
    assert module._resolve_sse_user("Basic ignored", "query-token", db) is user
    decoder.assert_called_once_with("query-token")
    db.get.assert_called_once_with(module.User, 7)


def test_resolve_sse_user_rejects_missing_or_invalid_tokens(monkeypatch: pytest.MonkeyPatch) -> None:
    """SSE 鉴权应把缺失 token 与解码失败分别映射为稳定 AuthError。"""
    db = SimpleNamespace(get=MagicMock())

    with pytest.raises(AuthError) as missing:
        module._resolve_sse_user(None, None, db)
    assert missing.value.code == 40100

    monkeypatch.setattr(module, "decode_token", MagicMock(side_effect=ValueError("bad token")))
    with pytest.raises(AuthError) as invalid:
        module._resolve_sse_user("Bearer broken", None, db)
    assert invalid.value.code == 40101


@pytest.mark.parametrize("resolved_user", [None, SimpleNamespace(id=7, status=0)])
def test_resolve_sse_user_rejects_missing_or_disabled_accounts(
    monkeypatch: pytest.MonkeyPatch,
    resolved_user: Any,
) -> None:
    """SSE 鉴权应拒绝不存在或已禁用账号；参数 resolved_user 模拟数据库结果。"""
    monkeypatch.setattr(module, "decode_token", MagicMock(return_value={"sub": "7"}))
    db = SimpleNamespace(get=MagicMock(return_value=resolved_user))

    with pytest.raises(ForbiddenError) as exc:
        module._resolve_sse_user("Bearer valid", None, db)

    assert exc.value.code == 40301


@pytest.mark.asyncio
async def test_sse_filters_other_users_but_delivers_system_and_owner_events(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """普通用户 SSE 应过滤他人事件，并保留系统级与本人事件。"""
    bus = FakeEventBus([_event("other", 8), _event("system", None), _event("owner", 7)])
    monkeypatch.setattr(module, "_resolve_sse_user", MagicMock(return_value=SimpleNamespace(id=7, role="member")))
    monkeypatch.setattr(module.AgentEventBus, "instance", MagicMock(return_value=bus))

    response = await module.stream_agent_events(replay=9, authorization="Bearer x", token=None, db=object())
    chunks = await _collect_stream(response)
    payload = "".join(chunks)

    assert chunks[0] == ":connected\n\n"
    assert "other" not in payload
    assert "system" in payload
    assert "owner" in payload
    assert payload.count("event: agent") == 2
    assert bus.replays == [9]


@pytest.mark.asyncio
async def test_admin_sse_delivers_all_events(monkeypatch: pytest.MonkeyPatch) -> None:
    """管理员 SSE 应接收任意 user_id 的事件。"""
    bus = FakeEventBus([_event("foreign", 88)])
    monkeypatch.setattr(module, "_resolve_sse_user", MagicMock(return_value=SimpleNamespace(id=1, role="admin")))
    monkeypatch.setattr(module.AgentEventBus, "instance", MagicMock(return_value=bus))

    response = await module.stream_agent_events(replay=0, authorization=None, token="x", db=object())
    payload = "".join(await _collect_stream(response))

    assert "foreign" in payload
    assert payload.count("event: agent") == 1


@pytest.mark.asyncio
async def test_sse_emits_heartbeat_and_stops_on_cancellation(monkeypatch: pytest.MonkeyPatch) -> None:
    """SSE 等待超时应发送心跳，客户端取消后应正常结束生成器。"""
    waiter = TimeoutThenCancel()
    bus = FakeEventBus([])
    monkeypatch.setattr(module, "_resolve_sse_user", MagicMock(return_value=SimpleNamespace(id=7, role="member")))
    monkeypatch.setattr(module.AgentEventBus, "instance", MagicMock(return_value=bus))
    monkeypatch.setattr(module.asyncio, "wait_for", waiter)

    response = await module.stream_agent_events(replay=0, authorization=None, token="x", db=object())
    chunks = await _collect_stream(response)

    assert chunks == [":connected\n\n", ":heartbeat\n\n"]
    assert waiter.calls == 2


def test_submit_clarification_returns_expired_response(clarify_store: ClarifyStore) -> None:
    """不存在的 clarify_id 应返回 41001，而不是触发后续 Agent 调度。"""
    response = module.submit_clarification(
        module.ClarifyAnswers(clarify_id="missing", answers={"x": 1}),
        db=object(),
        user=SimpleNamespace(id=7, role="member"),
    )

    assert clarify_store.size() == 0
    assert response.code == 41001
    assert response.data == {}


def test_submit_clarification_rejects_non_owner(clarify_store: ClarifyStore) -> None:
    """非管理员不得回填其他用户的追问。"""
    clarify_store.put("clarify-1", {"user_id": 8, "intent": "review", "payload": {}})

    with pytest.raises(ForbiddenError) as exc:
        module.submit_clarification(
            module.ClarifyAnswers(clarify_id="clarify-1", answers={}),
            db=object(),
            user=SimpleNamespace(id=7, role="member"),
        )

    assert exc.value.code == 40300


def test_submit_clarification_merges_payload_and_returns_dict_result(
    monkeypatch: pytest.MonkeyPatch,
    clarify_store: ClarifyStore,
) -> None:
    """本人回填应覆盖旧字段、注入用户上下文并保留 content/clarify/model。"""
    clarify_store.put(
        "clarify-2",
        {
            "user_id": 7,
            "intent": "review",
            "payload": {"project_id": 1, "mode": "old"},
            "question_keys": ["mode", "file_id"],
        },
    )
    chat_agent = FakeChatAgent(
        AgentResult(
            success=True,
            data={"content": "done", "clarify": {"next": False}, "ignored": True},
            model="model-a",
        )
    )
    orchestrator = SimpleNamespace(chat_agent=chat_agent)
    factory = MagicMock(return_value=orchestrator)
    monkeypatch.setattr(module, "get_request_orchestrator", factory)
    db = object()
    user = SimpleNamespace(id=7, role="member")

    response = module.submit_clarification(
        module.ClarifyAnswers(
            clarify_id="clarify-2",
            answers={"mode": "new", "file_id": 3, "_write_confirmation": "确认执行"},
        ),
        db=db,
        user=user,
    )

    assert response.data == {"content": "done", "clarify": {"next": False}, "model": "model-a"}
    factory.assert_called_once_with(db, user=user)
    intent, payload, ctx = chat_agent.calls[0]
    assert intent == "review"
    assert payload == {"project_id": 1, "mode": "new", "file_id": 3}
    assert ctx.user_id == 7
    assert ctx.extra == {}


def test_submit_clarification_allows_admin_and_returns_scalar_result(
    monkeypatch: pytest.MonkeyPatch,
    clarify_store: ClarifyStore,
) -> None:
    """管理员可代填他人追问，非字典结果应映射到 content 与 model。"""
    clarify_store.put("clarify-3", {"user_id": 8, "intent": "summary", "payload": {}})
    chat_agent = FakeChatAgent(AgentResult(success=True, data="plain text", model="model-b"))
    monkeypatch.setattr(
        module,
        "get_request_orchestrator",
        MagicMock(return_value=SimpleNamespace(chat_agent=chat_agent)),
    )

    response = module.submit_clarification(
        module.ClarifyAnswers(clarify_id="clarify-3", answers={}),
        db=object(),
        user=SimpleNamespace(id=1, role="admin"),
    )

    assert response.data == {"content": "plain text", "model": "model-b"}


def test_submit_clarification_maps_agent_failure_to_ai_error(
    monkeypatch: pytest.MonkeyPatch,
    clarify_store: ClarifyStore,
) -> None:
    """续跑 Agent 失败时应抛出带 50202 业务码的 AiServiceError。"""
    clarify_store.put("clarify-4", {"user_id": None, "intent": "review", "payload": {}})
    chat_agent = FakeChatAgent(AgentResult(success=False, error="upstream failed"))
    monkeypatch.setattr(
        module,
        "get_request_orchestrator",
        MagicMock(return_value=SimpleNamespace(chat_agent=chat_agent)),
    )

    with pytest.raises(AiServiceError) as exc:
        module.submit_clarification(
            module.ClarifyAnswers(clarify_id="clarify-4", answers={}),
            db=object(),
            user=SimpleNamespace(id=7, role="member"),
        )

    assert exc.value.code == 50202
    assert exc.value.message == "upstream failed"


def test_get_metagpt_info_lists_components_and_adaptable_agents(monkeypatch: pytest.MonkeyPatch) -> None:
    """MetaGPT 信息端点应返回组件说明、工厂清单和规范化 Agent 元数据。"""
    from app.agents import registry as registry_module

    monkeypatch.setattr(registry_module.AgentRegistry, "instance", MagicMock(return_value=FakeRegistry()))

    response = module.get_metagpt_info(SimpleNamespace(id=7))

    assert response.data["version"] == "v2.4"
    assert response.data["components"]["Environment"]
    assert "build_review_environment" in response.data["factories"]
    assert response.data["adaptable_agents"] == [
        {
            "name": "reviewer",
            "description": "review code",
            "category": "review",
            "icon": "shield",
            "color": "#123456",
        }
    ]


@pytest.mark.parametrize("mode", ["review", "discussion"])
def test_preview_metagpt_environment_uses_mode_factory_and_enriches_roles(
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
) -> None:
    """预览应按 mode 选择工厂、跳过空角色并补充已注册 Agent 展示信息。"""
    from app.agents import metagpt as metagpt_module
    from app.agents import registry as registry_module

    trace_id = f"preview_7_{mode}"
    environment = FakeEnvironment(trace_id)
    review_factory = MagicMock(return_value=environment)
    discussion_factory = MagicMock(return_value=environment)
    monkeypatch.setattr(metagpt_module, "build_review_environment", review_factory)
    monkeypatch.setattr(metagpt_module, "build_discussion_environment", discussion_factory)
    monkeypatch.setattr(registry_module.AgentRegistry, "instance", MagicMock(return_value=FakeRegistry()))

    response = module.preview_metagpt_environment(mode, SimpleNamespace(id=7))

    selected = review_factory if mode == "review" else discussion_factory
    unselected = discussion_factory if mode == "review" else review_factory
    selected.assert_called_once_with(trace_id=trace_id, user_id=7)
    unselected.assert_not_called()
    assert response.data["mode"] == mode
    assert response.data["trace_id"] == trace_id
    assert response.data["max_depth"] == 3
    assert response.data["registered_agent_count"] == 2
    assert [item["name"] for item in response.data["roles"]] == ["reviewer", "plain"]
    reviewer = response.data["roles"][0]
    assert reviewer["agent_icon"] == "shield"
    assert reviewer["agent_color"] == "#123456"
    assert reviewer["agent_category"] == "review"
    assert "agent_icon" not in response.data["roles"][1]


def test_list_agent_skills_serializes_orchestrator_metadata(monkeypatch: pytest.MonkeyPatch) -> None:
    """Skill 列表端点应转发 Agent 名并序列化所有公开元数据。"""
    from app.agents import orchestrator as orchestrator_module

    orchestrator = SimpleNamespace(
        list_agent_skills=MagicMock(
            return_value=[
                {
                    "name": "reviewer.self_improve",
                    "description": "reflect",
                    "type": "self_improvement",
                    "invocable": True,
                    "agent_name": "reviewer",
                }
            ]
        )
    )
    monkeypatch.setattr(orchestrator_module, "get_orchestrator", MagicMock(return_value=orchestrator))

    response = module.list_agent_skills("reviewer", SimpleNamespace(id=7))

    assert response.data[0].name == "reviewer.self_improve"
    assert response.data[0].type == "self_improvement"
    orchestrator.list_agent_skills.assert_called_once_with("reviewer")


def test_invoke_agent_skill_builds_context_and_maps_success(monkeypatch: pytest.MonkeyPatch) -> None:
    """手动 Skill 调用应生成 trace、让显式 action 优先并映射记录字段。"""
    from app.agents import events as events_module

    result = AgentResult(
        success=True,
        data={
            "data": {"proposal_id": 9},
            "effect": "proposal_created",
            "duration_ms": 33,
            "record_id": 101,
        },
        duration_ms=99,
    )
    orchestrator = SimpleNamespace(invoke_skill=MagicMock(return_value=result))
    factory = MagicMock(return_value=orchestrator)
    monkeypatch.setattr(module, "get_request_orchestrator", factory)
    monkeypatch.setattr(events_module, "new_trace_id", MagicMock(return_value="trace-skill"))
    db = object()
    admin = SimpleNamespace(id=1, role="admin")

    response = module.invoke_agent_skill(
        "reviewer",
        "reviewer.self_improve",
        SkillInvokeIn(action="evolve", params={"action": "tampered", "limit": 2}),
        db=db,
        admin=admin,
    )

    factory.assert_called_once_with(db, user=admin)
    call = orchestrator.invoke_skill.call_args.kwargs
    assert call["agent_name"] == "reviewer"
    assert call["skill_name"] == "reviewer.self_improve"
    assert call["params"] == {"action": "evolve", "limit": 2}
    assert call["ctx"].user_id == 1
    assert call["ctx"].extra == {"api": "invoke_agent_skill", "trace_id": "trace-skill"}
    assert call["trigger_type"] == "manual"
    assert call["trigger_source"].endswith("/reviewer/skills/reviewer.self_improve/invoke")
    assert response.data.success is True
    assert response.data.data == {"proposal_id": 9}
    assert response.data.effect == "proposal_created"
    assert response.data.duration_ms == 33
    assert response.data.record_id == 101


def test_invoke_agent_skill_maps_non_dict_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """Skill 返回非字典失败结果时应保留错误、默认 failed 效果和原始耗时。"""
    orchestrator = SimpleNamespace(
        invoke_skill=MagicMock(return_value=AgentResult(success=False, data="ignored", error="denied", duration_ms=12))
    )
    monkeypatch.setattr(module, "get_request_orchestrator", MagicMock(return_value=orchestrator))

    response = module.invoke_agent_skill(
        "reviewer",
        "reviewer.self_improve",
        SkillInvokeIn(),
        db=object(),
        admin=SimpleNamespace(id=1, role="admin"),
    )

    assert response.data.success is False
    assert response.data.data is None
    assert response.data.error == "denied"
    assert response.data.effect == "failed"
    assert response.data.duration_ms == 12
    assert response.data.record_id is None
    assert orchestrator.invoke_skill.call_args.kwargs["params"] == {}


def test_list_skill_records_forwards_filters_and_serializes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Skill 记录端点应完整转发筛选参数并构造响应模型。"""
    records = [
        {
            "id": 5,
            "agent_name": "reviewer",
            "skill_name": "reviewer.self_improve",
            "trigger_type": "manual",
            "trigger_source": "api",
            "effect": "success",
            "success": True,
            "duration_ms": 15,
            "output_summary": "done",
            "create_time": "2026-07-10T00:00:00+00:00",
        }
    ]
    service = MagicMock(return_value=records)
    monkeypatch.setattr(module.skill_service, "list_recent_records", service)
    db = object()

    response = module.list_skill_records(
        agent_name="reviewer",
        skill_name="reviewer.self_improve",
        trigger_type="manual",
        limit=10,
        db=db,
        admin=SimpleNamespace(id=1, role="admin"),
    )

    assert response.data[0].id == 5
    assert response.data[0].success is True
    service.assert_called_once_with(
        db=db,
        agent_name="reviewer",
        skill_name="reviewer.self_improve",
        trigger_type="manual",
        limit=10,
    )


def test_sse_event_payload_is_valid_json(monkeypatch: pytest.MonkeyPatch) -> None:
    """SSE agent 数据行应为可解析 JSON，便于前端稳定消费。"""
    event = _event("json-event", 7)
    serialized = json.dumps(event.to_dict(), ensure_ascii=False)

    assert json.loads(serialized)["trace_id"] == "json-event"
    assert json.loads(serialized)["type"] == "progress"
