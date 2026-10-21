"""DeepSeekAgent 非流式响应的完整性门禁。"""

from __future__ import annotations

import json
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import MagicMock

import httpx
import pytest

from app.ai.deepseek_agent import (
    DeepSeekAgent,
    DeepSeekJsonOutputError,
    DeepSeekOutputTruncatedError,
    DeepSeekResponseError,
    _parse_completion_response,
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
    assert exc_info.value.meta["prompt_tokens"] == 2
    assert exc_info.value.meta["completion_tokens"] == 3
    assert exc_info.value.meta["total_tokens"] == 5
    assert exc_info.value.meta["_http_attempts"] == 1
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
            _Response(200, _completion(content='{"issues": []}', finish_reason="stop")),
        ],
    )
    sleeps = []
    monkeypatch.setattr("app.ai.deepseek_agent.time.sleep", sleeps.append)

    content, meta = agent.call_raw("system", "user", agent_label="code_reviewer")

    assert json.loads(content) == {"issues": []}
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


def test_call_raw_classifies_402_without_retrying_or_exposing_provider_body(monkeypatch):
    agent = _agent(max_retries=4)
    calls = _install_responses(
        monkeypatch,
        agent,
        [_Response(402, text='{"error":{"message":"private provider account details"}}')],
    )
    sleeps = []
    monkeypatch.setattr("app.ai.deepseek_agent.time.sleep", sleeps.append)

    with pytest.raises(RuntimeError) as exc_info:
        agent.call_raw("system", "user")

    assert getattr(exc_info.value, "failure_kind", "") == "billing_error"
    assert "402" in str(exc_info.value)
    assert "private provider account details" not in str(exc_info.value)
    assert len(calls) == 1
    assert sleeps == []


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


def test_chat_classifies_402_as_nonretryable_without_exposing_provider_body(monkeypatch):
    agent = _agent(max_retries=5)
    calls = _install_responses(
        monkeypatch,
        agent,
        [_Response(402, text='{"error":{"message":"private provider account details"}}')],
    )
    sleeps = []
    monkeypatch.setattr("app.ai.deepseek_agent.time.sleep", sleeps.append)
    db = MagicMock()

    with pytest.raises(AiServiceError) as exc_info:
        agent.chat(
            system_prompt="system",
            user_prompt="user",
            db=db,
            task_id=12,
            agent_label="security_sentinel",
        )

    assert exc_info.value.retryable is False
    assert exc_info.value.http_status == 502
    assert getattr(exc_info.value, "failure_kind", "") == "billing_error"
    assert "402" in exc_info.value.message
    assert "private provider account details" not in exc_info.value.message
    assert len(calls) == 1
    assert sleeps == []
    record = db.add.call_args.args[0]
    assert record.status == "failed"
    assert record.response is None
    assert "402" in record.error_message
    assert "private provider account details" not in record.error_message


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
            _Response(200, _completion(content='{"issues": []}')),
        ],
    )
    monkeypatch.setattr("app.ai.deepseek_agent.time.sleep", lambda _seconds: None)
    records = []
    db = SimpleNamespace(add=records.append, flush=lambda: None)

    content, meta = agent.chat(system_prompt="system", user_prompt="user", db=db)

    assert json.loads(content) == {"issues": []}
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


@pytest.mark.parametrize("method", ["raw", "chat"])
def test_json_mode_retries_invalid_content_and_accounts_every_attempt(monkeypatch, method):
    agent = _agent(max_retries=2)
    requests, attempts = [], []
    valid = '{"issues":[{"title":"命令注入","evidence":"os.system(user_input)"}]}'
    responses = [
        _Response(200, _completion(content='{"issues":[...]}')),
        _Response(200, _completion(content=valid)),
    ]

    def request(url, headers, payload):
        requests.append(deepcopy(payload))
        return responses[len(requests) - 1], 17

    monkeypatch.setattr(agent, "_do_request", request)
    monkeypatch.setattr("app.ai.deepseek_agent.time.sleep", lambda _seconds: None)
    monkeypatch.setattr(
        "app.ai.deepseek_agent.record_usage_attempt",
        lambda **kwargs: attempts.append(kwargs) or len(attempts),
    )
    if method == "raw":
        content, meta = agent.call_raw("system", "完整源码", agent_label="security_sentinel")
    else:
        content, meta = agent.chat(
            system_prompt="system", user_prompt="完整源码", db=MagicMock(),
            agent_label="security_sentinel",
        )

    assert json.loads(content) == json.loads(valid)
    assert len(requests) == 2
    assert requests[0] == requests[1]  # 重试仍审查同一份完整源码。
    assert [item["status"] for item in attempts] == ["retry", "success"]
    assert [item["usage"]["total_tokens"] for item in attempts] == [5, 5]
    assert all(item["agent_label"] == "security_sentinel" for item in attempts)
    assert attempts[0]["response"] == '{"issues":[...]}'
    assert meta["_usage_log_ids"] == [1, 2]
    if method == "raw":
        assert meta["_http_attempts"] == 2


@pytest.mark.parametrize("method", ["raw", "chat"])
@pytest.mark.parametrize("max_retries", [0, 2])
@pytest.mark.parametrize(
    "content",
    [
        '{"issues": [...]}',
        '{"issues":[{"title":"未闭合截断"}',
        '{"issues":[{"title":"重复字段","title":"歧义值"}]}',
    ],
    ids=["invalid-json", "unrecoverable-truncation", "duplicate-field"],
)
def test_json_mode_exhaustion_remains_failed_with_usage(
    monkeypatch, method, max_retries, content,
):
    agent = _agent(max_retries=max_retries)
    calls = _install_responses(monkeypatch, agent, [
        _Response(200, _completion(content=content))
        for _ in range(max_retries + 1)
    ])
    attempts = []
    monkeypatch.setattr("app.ai.deepseek_agent.time.sleep", lambda _seconds: None)
    monkeypatch.setattr(
        "app.ai.deepseek_agent.record_usage_attempt",
        lambda **kwargs: attempts.append(kwargs) or len(attempts),
    )
    with pytest.raises((DeepSeekResponseError, AiServiceError), match="JSON") as error:
        if method == "raw":
            agent.call_raw("system", "source")
        else:
            agent.chat(system_prompt="system", user_prompt="source", db=MagicMock())

    assert len(calls) == max_retries + 1
    assert [item["status"] for item in attempts] == ["retry"] * max_retries + ["failed"]
    assert sum(item["usage"]["total_tokens"] for item in attempts) == 5 * len(calls)
    if method == "raw":
        assert isinstance(error.value, DeepSeekJsonOutputError)
    else:
        assert isinstance(error.value.__cause__, DeepSeekJsonOutputError)
    if method == "raw":
        assert error.value.meta["_http_attempts"] == len(calls)
        assert error.value.meta["_usage_log_ids"] == list(range(1, len(calls) + 1))
        assert error.value.meta["total_tokens"] == 5


@pytest.mark.parametrize("method", ["raw", "chat"])
def test_json_mode_repairs_only_format_and_returns_strict_json(monkeypatch, method):
    agent = _agent()
    raw = '```json\n{"issues":[{"title":"命令注入","evidence":"os.system(x)"},],}\n```'
    calls = _install_responses(monkeypatch, agent, [_Response(200, _completion(content=raw))])
    if method == "raw":
        content, _ = agent.call_raw("system", "source")
    else:
        content, _ = agent.chat(system_prompt="system", user_prompt="source", db=MagicMock())
    assert json.loads(content) == {"issues": [{"title": "命令注入", "evidence": "os.system(x)"}]}
    assert len(calls) == 1


def test_plain_text_mode_preserves_content_without_json_repair(monkeypatch):
    agent = _agent()
    raw = '自然语言讨论，保留 ```json 和未闭合 { 原文'
    calls = _install_responses(monkeypatch, agent, [_Response(200, _completion(content=raw))])
    content, _ = agent.call_raw("system", "source", json_mode=False)
    assert content == raw
    assert "response_format" not in calls[0][2]


@pytest.mark.parametrize("method", ["raw", "chat"])
@pytest.mark.parametrize("content", ['{"issues": []}', '{"issues":[...]}'])
def test_usage_accounting_failure_never_resends_model_request(monkeypatch, method, content):
    from app.services.ai_usage_context import UsageAccountingError

    agent = _agent(max_retries=2)
    calls = _install_responses(monkeypatch, agent, [_Response(200, _completion(content=content))])

    def reject_accounting(**_kwargs):
        raise UsageAccountingError("审计提交失败")

    monkeypatch.setattr("app.ai.deepseek_agent.record_usage_attempt", reject_accounting)
    with pytest.raises(UsageAccountingError, match="审计提交失败"):
        if method == "raw":
            agent.call_raw("system", "source")
        else:
            agent.chat(system_prompt="system", user_prompt="source", db=MagicMock())
    assert len(calls) == 1


@pytest.mark.parametrize("method", ["raw", "chat"])
def test_json_mode_repairs_bare_severity_without_retry_or_losing_evidence(monkeypatch, method):
    agent = _agent(max_retries=0)
    evidence = 'literal "severity":低, escaped \\" text'
    expected = {"issues": [
        {"title": "脱敏发现一", "severity": "低", "evidence": evidence},
        {"title": "脱敏发现二", "severity": "低", "fixed_code": "check(value)"},
    ]}
    raw = json.dumps(expected, ensure_ascii=False).replace('"severity": "低"', '"severity":低')
    calls = _install_responses(monkeypatch, agent, [_Response(200, _completion(content=raw))])
    attempts = []
    monkeypatch.setattr(
        "app.ai.deepseek_agent.record_usage_attempt",
        lambda **kwargs: attempts.append(kwargs) or len(attempts),
    )
    if method == "raw":
        content, _ = agent.call_raw("system", "source")
    else:
        content, _ = agent.chat(system_prompt="system", user_prompt="source", db=MagicMock())

    assert json.loads(content) == expected
    assert len(calls) == 1
    assert [attempt["status"] for attempt in attempts] == ["success"]
    if method == "raw":
        assert attempts[0]["response"] == raw
    else:
        assert json.loads(attempts[0]["response"]) == expected


@pytest.mark.parametrize(
    ("field", "malformed_value"),
    [
        ("issue_type", "安全漏洞"),
        ("issue_type", '安全漏洞"'),
        ("issue_type", '"安全漏洞'),
        ("severity", '高"'),
        ("severity", '"高'),
    ],
    ids=[
        "bare-issue-type",
        "issue-type-missing-opening-quote",
        "issue-type-missing-closing-quote",
        "severity-missing-opening-quote",
        "severity-missing-closing-quote",
    ],
)
def test_parse_completion_response_repairs_registered_enums_without_losing_findings(
    field, malformed_value,
):
    expected = {
        "issues": [
            {
                "issue_type": "安全漏洞",
                "severity": "高",
                "title": "脱敏发现一",
                "evidence": 'literal "severity":低, escaped \\" text',
            },
            {
                "issue_type": "代码规范",
                "severity": "低",
                "title": "脱敏发现二",
                "evidence": 'source contains "issue_type":代码规范',
            },
        ],
    }
    raw = json.dumps(expected, ensure_ascii=False)
    original_value = json.dumps(expected["issues"][0][field], ensure_ascii=False)
    raw = raw.replace(
        f'"{field}": {original_value}', f'"{field}":{malformed_value}', 1,
    )

    content, usage, finish_reason = _parse_completion_response(
        _Response(200, _completion(content=raw)),
    )

    parsed = json.loads(content)
    assert content == json.dumps(expected, ensure_ascii=False)
    assert parsed == expected
    assert len(parsed["issues"]) == len(expected["issues"])
    assert [issue["evidence"] for issue in parsed["issues"]] == [
        issue["evidence"] for issue in expected["issues"]
    ]
    assert usage["total_tokens"] == 5
    assert finish_reason == "stop"
