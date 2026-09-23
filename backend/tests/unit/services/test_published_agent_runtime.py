"""Regression coverage for real published-Agent calls from Xiaoling and the catalog."""

from types import SimpleNamespace

import pytest

from app.ai.deepseek_agent import DeepSeekOutputTruncatedError
from app.ai.multi_agent import GENERAL_AGENT
from app.services import published_agent_tools


def _published_profile(max_tokens: int = 4096):
    return GENERAL_AGENT.__class__(
        **{**GENERAL_AGENT.__dict__, "code": "auth_boundary_reviewer", "max_tokens": max_tokens},
    )


def _prepare_invocation(monkeypatch, profile):
    monkeypatch.setattr(published_agent_tools, "_require_invoke_permission", lambda *_: None)
    monkeypatch.setattr(
        published_agent_tools.DeclarativeReviewAgentFactory,
        "resolve_published",
        lambda *_args, **_kwargs: SimpleNamespace(to_profile=lambda: profile),
    )


def test_xiaoling_can_pass_json_rules_to_published_agent(db, admin_user, monkeypatch):
    """Tool/API schemas expose JSON dictionaries; their fields must reach the review prompt."""
    _prepare_invocation(monkeypatch, _published_profile())
    prompts = []

    class Client:
        def __init__(self, api_config):
            pass

        def call_raw(self, **kwargs):
            prompts.append(kwargs["user_prompt"])
            return '{"summary":"已检查鉴权边界","score":100,"issues":[]}', {}

        def log_deferred(self, *_args, **_kwargs):
            pass

    monkeypatch.setattr(published_agent_tools, "DeepSeekAgent", Client)
    result = published_agent_tools.invoke_published_agent(
        db,
        admin_user,
        agent_code="auth_boundary_reviewer",
        code="if not owner: raise ForbiddenError()",
        language="python",
        file_name="approval_service.py",
        rules=[{
            "rule_type": "security",
            "rule_name": "跨账号鉴权",
            "rule_code": "account_boundary",
            "rule_content": "验证对象所属账号再读取记录",
            "severity": "高",
            "language": "python",
        }],
    )

    assert result["summary"] == "已检查鉴权边界"
    assert "跨账号鉴权" in prompts[0]
    assert "验证对象所属账号再读取记录" in prompts[0]


def test_published_agent_retries_truncated_response_with_review_budget(db, admin_user, monkeypatch):
    """A published 4096-token version must retry before reporting an incomplete review."""
    _prepare_invocation(monkeypatch, _published_profile())
    budgets = []

    class Client:
        def __init__(self, api_config):
            pass

        def call_raw(self, **kwargs):
            budgets.append(kwargs["max_tokens"])
            if len(budgets) == 1:
                raise DeepSeekOutputTruncatedError(
                    "DeepSeek 输出被截断 (finish_reason=length)", finish_reason="length",
                )
            return '{"summary":"已检查鉴权边界","score":100,"issues":[]}', {}

        def log_deferred(self, *_args, **_kwargs):
            pass

    monkeypatch.setattr(published_agent_tools, "DeepSeekAgent", Client)
    result = published_agent_tools.invoke_published_agent(
        db, admin_user, agent_code="auth_boundary_reviewer", code="pass", language="python",
    )

    assert result["summary"] == "已检查鉴权边界"
    assert budgets == [4096, 16_384]


def test_published_agent_does_not_repeat_at_output_ceiling(db, admin_user, monkeypatch):
    """A second attempt at the configured provider ceiling cannot recover truncation."""
    _prepare_invocation(monkeypatch, _published_profile(65_536))
    budgets = []

    class Client:
        def __init__(self, api_config):
            pass

        def call_raw(self, **kwargs):
            budgets.append(kwargs["max_tokens"])
            raise DeepSeekOutputTruncatedError(
                "DeepSeek 输出被截断 (finish_reason=length)", finish_reason="length",
            )

    monkeypatch.setattr(published_agent_tools, "DeepSeekAgent", Client)
    with pytest.raises(DeepSeekOutputTruncatedError):
        published_agent_tools.invoke_published_agent(
            db, admin_user, agent_code="auth_boundary_reviewer", code="pass",
        )
    assert budgets == [65_536]
