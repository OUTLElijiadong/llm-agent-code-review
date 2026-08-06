"""DeepSeekAgent 非流式响应的完整性门禁。"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import httpx
import pytest

from app.ai.deepseek_agent import (
    DeepSeekAgent,
    DeepSeekOutputTruncatedError,
    DeepSeekResponseError,
)
from app.ai.exceptions import AiServiceError


class _Response:
    def __init__(self, status_code: int, body=None, text: str = ""):
        self.status_code = status_code
        self._body = body
        self.text = text

    def json(self):
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


def _agent(*, max_retries: int = 2) -> DeepSeekAgent:
    agent = DeepSeekAgent.__new__(DeepSeekAgent)
    agent.base_url = "https://api.deepseek.com"
    agent.api_key = "test-key"
    agent.model = "deepseek-chat"
    agent.api_config = None
    agent.timeout = 30
    agent.max_retries = max_retries
    return agent


def _completion(
    content: object = '{"result":"partial but parseable"}',
    *,
    finish_reason: object = "stop",
    usage: object = None,
) -> dict:
    return {
        "choices": [
            {
                "finish_reason": finish_reason,
                "message": {"content": content},
            },
        ],
        "usage": usage
        if usage is not None
        else {"prompt_tokens": 2, "completion_tokens": 3, "total_tokens": 5},
    }


def _install_responses(monkeypatch, agent: DeepSeekAgent, responses: list[_Response]):
    calls = []

    def fake_request(url, headers, payload):
        calls.append((url, headers, payload))
        return responses[len(calls) - 1], 17

    monkeypatch.setattr(agent, "_do_request", fake_request)
    return calls


def test_call_raw_rejects_nonempty_parseable_length_without_retry(monkeypatch):
    agent = _agent(max_retries=4)
    calls = _install_responses(
        monkeypatch,
        agent,
        [_Response(200, _completion(finish_reason="length"))],
    )

    with pytest.raises(DeepSeekOutputTruncatedError) as exc_info:
        agent.call_raw("system", "user", agent_label="security_sentinel")

    assert exc_info.value.finish_reason == "length"
    assert "finish_reason=length" in str(exc_info.value)
    assert len(calls) == 1


@pytest.mark.parametrize("finish_reason", ["content_filter", "insufficient_system_resource"])
def test_call_raw_rejects_non_stop_terminal_reason_without_retry(monkeypatch, finish_reason):
    agent = _agent(max_retries=4)
    calls = _install_responses(
        monkeypatch,
        agent,
        [_Response(200, _completion(finish_reason=finish_reason))],
    )

    with pytest.raises(DeepSeekResponseError, match="不完整终态"):
        agent.call_raw("system", "user")

    assert len(calls) == 1


@pytest.mark.parametrize(
    ("body", "message"),
    [
        ({}, "choices[0]"),
        ({"choices": []}, "choices[0]"),
        (_completion(finish_reason=None), "finish_reason"),
        (_completion(content={"not": "text"}), "content"),
        (_completion(content=""), "content 为空"),
        (_completion(usage=[]), "usage"),
        (ValueError("bad json"), "不是合法 JSON"),
    ],
)
def test_call_raw_rejects_malformed_success_response_without_retry(
    monkeypatch,
    body,
    message,
):
    agent = _agent(max_retries=3)
    calls = _install_responses(monkeypatch, agent, [_Response(200, body)])

    with pytest.raises(DeepSeekResponseError) as exc_info:
        agent.call_raw("system", "user")

    assert message in str(exc_info.value)
    assert len(calls) == 1


def test_call_raw_retries_429_and_5xx_then_returns_complete_response(monkeypatch):
    agent = _agent(max_retries=2)
    calls = _install_responses(
        monkeypatch,
        agent,
        [
            _Response(429, text="rate limited"),
            _Response(503, text="unavailable"),
            _Response(200, _completion(content="complete", finish_reason="stop")),
        ],
    )
    sleeps = []
    monkeypatch.setattr("app.ai.deepseek_agent.time.sleep", sleeps.append)

    content, meta = agent.call_raw("system", "user", agent_label="code_reviewer")

    assert content == "complete"
    assert meta["finish_reason"] == "stop"
    assert meta["total_tokens"] == 5
    assert len(calls) == 3
    assert sleeps == [2, 4]


def test_call_raw_does_not_retry_deterministic_4xx(monkeypatch):
    agent = _agent(max_retries=4)
    calls = _install_responses(monkeypatch, agent, [_Response(400, text="invalid request")])

    with pytest.raises(RuntimeError, match="确定性请求失败"):
        agent.call_raw("system", "user")

    assert len(calls) == 1


def test_call_raw_retries_transient_network_errors_until_exhausted(monkeypatch):
    agent = _agent(max_retries=2)
    calls = []

    def fail_request(*_args, **_kwargs):
        calls.append(True)
        raise httpx.ReadTimeout("temporary timeout")

    monkeypatch.setattr(agent, "_do_request", fail_request)
    monkeypatch.setattr("app.ai.deepseek_agent.time.sleep", lambda _seconds: None)

    with pytest.raises(RuntimeError, match="3 次重试均失败"):
        agent.call_raw("system", "user")

    assert len(calls) == 3


def test_chat_logs_length_as_failed_and_does_not_retry(monkeypatch):
    agent = _agent(max_retries=5)
    calls = _install_responses(
        monkeypatch,
        agent,
        [_Response(200, _completion(finish_reason="length"))],
    )
    db = MagicMock()

    with pytest.raises(AiServiceError) as exc_info:
        agent.chat(
            system_prompt="system",
            user_prompt="user",
            db=db,
            task_id=12,
            agent_label="security_sentinel",
        )

    assert exc_info.value.code == 50201
    assert "finish_reason=length" in exc_info.value.message
    assert len(calls) == 1
    db.flush.assert_called_once_with()
    record = db.add.call_args.args[0]
    assert record.status == "failed"
    assert record.response is None
    assert "finish_reason=length" in record.error_message


def test_chat_logs_malformed_content_as_failed_without_retry(monkeypatch):
    agent = _agent(max_retries=3)
    calls = _install_responses(
        monkeypatch,
        agent,
        [_Response(200, _completion(content=["not", "text"]))],
    )
    db = MagicMock()

    with pytest.raises(AiServiceError) as exc_info:
        agent.chat(system_prompt="system", user_prompt="user", db=db)

    assert "content 必须是字符串" in exc_info.value.message
    assert len(calls) == 1
    assert db.add.call_args.args[0].status == "failed"


def test_chat_retries_transient_http_errors_then_logs_success(monkeypatch):
    agent = _agent(max_retries=2)
    calls = _install_responses(
        monkeypatch,
        agent,
        [
            _Response(429, text="rate limited"),
            _Response(502, text="bad gateway"),
            _Response(200, _completion(content="complete")),
        ],
    )
    monkeypatch.setattr("app.ai.deepseek_agent.time.sleep", lambda _seconds: None)
    records = []
    db = SimpleNamespace(add=records.append, flush=lambda: None)

    content, meta = agent.chat(system_prompt="system", user_prompt="user", db=db)

    assert content == "complete"
    assert meta["finish_reason"] == "stop"
    assert len(calls) == 3
    assert [record.status for record in records] == ["retry", "retry", "success"]


def test_chat_does_not_retry_deterministic_4xx(monkeypatch):
    agent = _agent(max_retries=4)
    calls = _install_responses(monkeypatch, agent, [_Response(401, text="unauthorized")])
    records = []
    db = SimpleNamespace(add=records.append, flush=lambda: None)

    with pytest.raises(AiServiceError, match="401"):
        agent.chat(system_prompt="system", user_prompt="user", db=db)

    assert len(calls) == 1
    assert [record.status for record in records] == ["failed"]


def test_chat_retries_transient_network_errors_and_logs_final_failure(monkeypatch):
    agent = _agent(max_retries=2)
    calls = []

    def fail_request(*_args, **_kwargs):
        calls.append(True)
        raise httpx.ConnectError("temporary connection failure")

    monkeypatch.setattr(agent, "_do_request", fail_request)
    monkeypatch.setattr("app.ai.deepseek_agent.time.sleep", lambda _seconds: None)
    records = []
    db = SimpleNamespace(add=records.append, flush=lambda: None)

    with pytest.raises(AiServiceError, match="网络请求失败"):
        agent.chat(system_prompt="system", user_prompt="user", db=db)

    assert len(calls) == 3
    assert [record.status for record in records] == ["retry", "retry", "failed"]
