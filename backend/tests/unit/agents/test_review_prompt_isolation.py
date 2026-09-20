"""同一注册 Agent 的并发请求不能共享或残留本轮提示词。"""

import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from app.agents.base import AgentResult, BaseAgent
from app.agents.review_agent import CodeReviewerAgent


def test_shared_reviewer_uses_request_local_prompt_under_concurrency(monkeypatch):
    agent = CodeReviewerAgent()
    original = agent._system_prompt
    a_entered = threading.Event()
    b_entered = threading.Event()
    a_observed = threading.Event()
    observed = {}
    monkeypatch.setattr("app.agents.review_agent.build_prompt",
                        lambda **kwargs: (f"rules-{kwargs['language']}", kwargs["language"]))

    def call(message, **kwargs):
        if message == "A":
            a_entered.set()
            assert b_entered.wait(2)
        else:
            b_entered.set()
            assert a_observed.wait(2)
        observed[message] = kwargs.get("system_prompt", agent._system_prompt)
        if message == "A":
            a_observed.set()
        return AgentResult(success=False, error="isolated model failure")

    monkeypatch.setattr(agent, "call", call)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(agent.execute_review, code="x=1", rules=[], language="A", file_name="A")
        assert a_entered.wait(2)
        second = pool.submit(agent.execute_review, code="x=1", rules=[], language="B", file_name="B")
        results = [first, second]
        for result in results:
            assert not result.result().success
    assert "rules-A" in observed["A"] and "rules-B" not in observed["A"]
    assert "rules-B" in observed["B"] and "rules-A" not in observed["B"]
    assert agent._system_prompt == original


def test_prompt_does_not_mutate_when_call_raises(monkeypatch):
    agent = CodeReviewerAgent()
    original = agent._system_prompt

    def call(*args, **kwargs):
        assert agent._system_prompt == original
        raise RuntimeError("isolated exception")

    monkeypatch.setattr(agent, "call", call)
    with pytest.raises(RuntimeError, match="isolated exception"):
        agent.execute_review(code="x=1", rules=[], language="python", file_name="one.py")
    assert agent._system_prompt == original


def test_base_call_override_does_not_change_call_json_default(monkeypatch):
    payloads = []

    class Client:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def post(self, url, **kwargs):
            payloads.append(kwargs["json"])
            return SimpleNamespace(status_code=200, json=lambda: {
                "choices": [{"finish_reason": "stop", "message": {"content": '{"ok":true}'}}],
            })

    monkeypatch.setattr("app.agents.base.httpx.Client", Client)
    monkeypatch.setattr("app.agents.base.pin_public_http_url", lambda _: SimpleNamespace(
        request_url="https://model.invalid/chat/completions", host_header="model.invalid", request_extensions={},
    ))
    agent = BaseAgent(system_prompt="default-system")
    assert agent.call("message", system_prompt="request-system").success
    result = agent.call_json("second")
    assert result.success and result.data == {"ok": True}
    assert [item["messages"][0]["content"] for item in payloads] == ["request-system", "default-system"]
    assert agent._system_prompt == "default-system"
