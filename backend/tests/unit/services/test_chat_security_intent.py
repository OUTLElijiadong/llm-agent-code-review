"""单元测试 (v2.1): ChatAgent 对 security_audit 意图的 Clarify/dispatch 链路"""
from app.agents.chat_agent import ChatAssistantAgent
from app.agents.clarify_store import ClarifyStore


def _reset_store():
    ClarifyStore._instance = None
    return ClarifyStore.instance()


def test_security_audit_clarify_when_missing_scope():
    """没有给 scope 时必须先追问 scope"""
    _reset_store()
    agent = ChatAssistantAgent()
    result = agent._maybe_clarify("security_audit", {}, ctx=None)
    assert result is not None
    clarify = result.data["clarify"]
    assert clarify["intent"] == "security_audit"
    keys = [q["key"] for q in clarify["questions"]]
    assert keys == ["scope"]


def test_security_audit_clarify_when_scope_file_missing_file_id():
    _reset_store()
    agent = ChatAssistantAgent()
    result = agent._maybe_clarify(
        "security_audit", {"scope": "file"}, ctx=None,
    )
    assert result is not None
    keys = [q["key"] for q in result.data["clarify"]["questions"]]
    assert "file_id" in keys


def test_security_audit_clarify_when_scope_project_missing_project_id():
    _reset_store()
    agent = ChatAssistantAgent()
    result = agent._maybe_clarify(
        "security_audit", {"scope": "project"}, ctx=None,
    )
    assert result is not None
    keys = [q["key"] for q in result.data["clarify"]["questions"]]
    assert "project_id" in keys


def test_security_audit_no_clarify_when_payload_complete():
    """scope+对应 id 齐全时不再追问"""
    agent = ChatAssistantAgent()
    assert agent._maybe_clarify(
        "security_audit", {"scope": "task", "task_id": 7}, ctx=None,
    ) is None
    assert agent._maybe_clarify(
        "security_audit", {"scope": "project", "project_id": 11}, ctx=None,
    ) is None
    assert agent._maybe_clarify(
        "security_audit", {"scope": "file", "file_id": 3}, ctx=None,
    ) is None


def test_security_audit_required_helper():
    agent = ChatAssistantAgent()
    assert agent._security_audit_required({}) == ["scope"]
    assert agent._security_audit_required({"scope": "file"}) == ["file_id"]
    assert agent._security_audit_required({"scope": "task"}) == ["task_id"]
    assert agent._security_audit_required({"scope": "project"}) == ["project_id"]
    assert agent._security_audit_required({"scope": "weird"}) == ["scope"]
