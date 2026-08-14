"""单元测试 (v2.0 M4): Clarify 主动提问机制"""
import time

from app.agents.chat_agent import ChatAssistantAgent
from app.agents.clarify_store import ClarifyStore


def _reset_store():
    ClarifyStore._instance = None
    return ClarifyStore.instance()


def test_clarify_store_put_and_pop():
    store = _reset_store()
    store.put("clr_1", {"intent": "x", "payload": {}})
    assert store.peek("clr_1") == {"intent": "x", "payload": {}}
    assert store.pop("clr_1") == {"intent": "x", "payload": {}}
    assert store.pop("clr_1") is None


def test_clarify_store_ttl_expiry():
    store = ClarifyStore(ttl_seconds=1)
    store.put("k", {"a": 1})
    time.sleep(1.05)
    assert store.pop("k") is None


def test_chat_agent_clarify_for_missing_project_id():
    """没给 project_id 的 delete_project 应触发 Clarify 而非直接报错"""
    _reset_store()
    agent = ChatAssistantAgent()
    result = agent._maybe_clarify("delete_project", {}, ctx=None)
    assert result is not None
    assert result.success is True
    assert isinstance(result.data, dict)
    assert "clarify" in result.data
    clarify = result.data["clarify"]
    assert clarify["intent"] == "delete_project"
    assert len(clarify["questions"]) == 1
    assert clarify["questions"][0]["key"] == "project_id"
    assert clarify["questions"][0]["type"] == "select_project"
    # clarify_id 应能在 store 中找回
    saved = ClarifyStore.instance().peek(clarify["clarify_id"])
    assert saved is not None
    assert saved["intent"] == "delete_project"


def test_delete_project_danger_confirmation_accepts_button_click():
    """危险确认只需点击确认按钮即可放行；未点击或取消仍被拦截。"""
    agent = ChatAssistantAgent()
    result = agent._maybe_clarify(
        "delete_project", {"project_id": 42}, ctx=None,
    )
    assert result is not None
    question = result.data["clarify"]["questions"][0]
    assert question["type"] == "danger_confirm"
    assert "无需输入确认词" in result.data["content"]

    for confirmation in (True, "确认", ""):
        assert agent._maybe_clarify(
            "delete_project", {"project_id": 42, "_write_confirmation": confirmation}, ctx=None,
        ) is None

    cancelled = agent._maybe_clarify(
        "delete_project", {"project_id": 42, "_write_confirmation": "取消"}, ctx=None,
    )
    assert cancelled is not None
    assert cancelled.data == "操作已取消，没有修改任何数据。"


def test_create_project_confirmation_can_be_cancelled_without_execution():
    agent = ChatAssistantAgent()
    result = agent._maybe_clarify(
        "create_project", {"project_name": "demo", "_write_confirmation": "取消"}, ctx=None,
    )
    assert result is not None
    assert result.data == "操作已取消，没有修改任何数据。"


def test_chat_agent_clarify_for_intent_without_required_fields():
    """非关键 intent 不应触发 Clarify"""
    agent = ChatAssistantAgent()
    assert agent._maybe_clarify("list_projects", {}, ctx=None) is None
    assert agent._maybe_clarify("dashboard", {}, ctx=None) is None
