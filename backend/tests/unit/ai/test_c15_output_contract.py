"""C15 output-budget and retry accounting contracts with an isolated upstream."""

from __future__ import annotations

import httpx
import pytest

from app.ai.deepseek_agent import DeepSeekAgent, DeepSeekOutputTruncatedError, _clamp_max_tokens
from app.services import roundtable_followup_service as followup


def _agent(*, retries: int = 0) -> DeepSeekAgent:
    agent = DeepSeekAgent.__new__(DeepSeekAgent)
    agent.base_url = "https://api.deepseek.com"
    agent.api_key = 'unit-test-api-key'
    agent.model = "deepseek-v4-flash"
    agent.timeout = 1
    agent.max_retries = retries
    return agent


@pytest.mark.parametrize("requested", [1, 32, 127, 128, 1024])
def test_explicit_small_output_limit_is_sent_unchanged(requested: int) -> None:
    agent = _agent()
    assert _clamp_max_tokens(requested) == requested
    assert agent._build_request("system", "user", max_tokens=requested)[2]["max_tokens"] == requested


def test_default_output_limit_remains_project_default() -> None:
    assert _clamp_max_tokens(None) == 4096


def test_explicit_one_token_length_is_failed_and_counted_once(monkeypatch: pytest.MonkeyPatch) -> None:
    agent = _agent(retries=2)
    requests = []
    usage_attempts = []

    def fake_request(_url, _headers, payload):
        requests.append(payload)
        return httpx.Response(200, json={
            "choices": [{"finish_reason": "length", "message": {"content": '{"issues":[]}'}}],
            "usage": {"prompt_tokens": 20, "completion_tokens": 1, "total_tokens": 21},
        }), 1

    monkeypatch.setattr(agent, "_do_request", fake_request)
    monkeypatch.setattr(
        "app.ai.deepseek_agent.record_usage_attempt",
        lambda **kwargs: usage_attempts.append(kwargs) or len(usage_attempts),
    )
    with pytest.raises(DeepSeekOutputTruncatedError, match="finish_reason=length") as error:
        agent.call_raw("system", "user", max_tokens=1)
    assert requests[0]["max_tokens"] == 1
    assert len(requests) == 1
    assert error.value.usage_log_ids == [1]
    assert [attempt["status"] for attempt in usage_attempts] == ["failed"]
    assert usage_attempts[0]["usage"]["completion_tokens"] == 1


def test_402_stops_but_429_503_timeout_retry_and_count_every_attempt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorded = []
    monkeypatch.setattr(
        "app.ai.deepseek_agent.record_usage_attempt",
        lambda **kwargs: recorded.append(kwargs) or len(recorded),
    )
    monkeypatch.setattr("app.ai.deepseek_agent.time.sleep", lambda _seconds: None)

    denied = _agent(retries=3)
    denied_requests = []
    monkeypatch.setattr(denied, "_do_request", lambda _url, _headers, payload: (
        denied_requests.append(payload) or httpx.Response(402, text="payment required"), 1,
    ))
    with pytest.raises(RuntimeError, match="确定性请求失败") as error:
        denied.call_raw("system", "user")
    assert len(denied_requests) == 1
    assert error.value.http_attempts == 1
    assert error.value.usage_log_ids == [1]
    assert [attempt["status"] for attempt in recorded] == ["failed"]

    recovered = _agent(retries=3)
    sequence = [
        httpx.Response(429, text="rate limited"),
        httpx.Response(503, text="unavailable"),
        httpx.ReadTimeout("timed out"),
        httpx.Response(200, json={
            "choices": [{"finish_reason": "stop", "message": {"content": "complete"}}],
            "usage": {"prompt_tokens": 20, "completion_tokens": 5, "total_tokens": 25},
        }),
    ]

    def fake_request(_url, _headers, _payload):
        response = sequence.pop(0)
        if isinstance(response, Exception):
            raise response
        return response, 1

    monkeypatch.setattr(recovered, "_do_request", fake_request)
    content, metadata = recovered.call_raw("system", "user")
    assert content == "complete"
    assert metadata["_http_attempts"] == 4
    assert metadata["_usage_log_ids"] == [2, 3, 4, 5]
    assert [attempt["status"] for attempt in recorded] == [
        "failed", "retry", "retry", "retry", "success",
    ]


@pytest.mark.parametrize(
    ("ceiling", "expected_budgets"),
    [(1024, [1024, 1024]), (65536, [8192, 16384, 32768])],
)
def test_roundtable_consecutive_length_never_returns_partial_answer(
    monkeypatch: pytest.MonkeyPatch, ceiling: int, expected_budgets: list[int],
) -> None:
    from app.ai import discussion_orchestrator as orchestrator

    monkeypatch.setattr(followup.settings, "deepseek_max_output_tokens", ceiling)
    monkeypatch.setattr(orchestrator, "_build_discussion_agents", lambda *_args: (object(), {}))
    monkeypatch.setattr(orchestrator, "_roundtable_history_records", lambda _turns: [("S1", "完整历史")])
    monkeypatch.setattr(orchestrator, "_roundtable_input_budget_error", lambda *_args, **_kwargs: None)
    budgets = []

    def always_truncated(*_args, **kwargs):
        budgets.append(kwargs["max_tokens"])
        raise DeepSeekOutputTruncatedError("finish_reason=length", finish_reason="length")

    monkeypatch.setattr(orchestrator, "_call_raw_for_task", always_truncated)
    session = type("Session", (), {
        "owner_user_id": 7, "report_task_id": 42, "file_id": 1, "file_name": "main.py",
    })()
    question = type("Question", (), {"seq": 2, "content": "请给完整结构化结论"})()
    with pytest.raises(DeepSeekOutputTruncatedError, match="finish_reason=length"):
        followup._answer(session, [], question)
    assert budgets == expected_budgets
