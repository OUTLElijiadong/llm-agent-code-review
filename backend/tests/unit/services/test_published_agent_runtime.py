"""Regression coverage for real published-Agent calls from Xiaoling and the catalog."""

from types import SimpleNamespace

import pytest

from app.ai.deepseek_agent import DeepSeekOutputTruncatedError, DeepSeekResponseError
from app.ai.multi_agent import GENERAL_AGENT
from app.core.exceptions import ValidationError
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
            prompts.append(f"{kwargs['system_prompt']}\n{kwargs['user_prompt']}")
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
    assert "evidence 必须逐字来自当前源码分片" in prompts[0]
    assert "证据不足、路径未闭合" in prompts[0]


def test_frozen_custom_delegate_runs_before_parent_and_result_is_included(db, admin_user, monkeypatch):
    parent_profile = _published_profile()
    parent_profile = parent_profile.__class__(
        **{**parent_profile.__dict__, "code": "root_reviewer", "release_id": 10, "version_id": 100},
    )
    child_profile = _published_profile().__class__(
        **{**_published_profile().__dict__, "code": "auth_child", "release_id": 20, "version_id": 200},
    )
    child_snapshot = {
        "kind": "custom",
        "agent_code": "auth_child",
        "release_id": 20,
        "version_id": 200,
        "package_checksum": "p" * 64,
        "template_checksum": "t" * 64,
        "max_depth": 2,
    }
    parent_definition = SimpleNamespace(
        to_profile=lambda: parent_profile,
        release_id=10,
        version_id=100,
        delegated_agents=(child_snapshot,),
    )
    child_definition = SimpleNamespace(
        to_profile=lambda: child_profile,
        release_id=20,
        version_id=200,
        delegated_agents=(),
    )
    monkeypatch.setattr(published_agent_tools, "_require_invoke_permission", lambda *_: None)
    monkeypatch.setattr(
        published_agent_tools.DeclarativeReviewAgentFactory,
        "resolve_published",
        lambda *_args, **_kwargs: parent_definition,
    )
    resolved_children = []

    def resolve_child(_db, code, **kwargs):
        resolved_children.append((code, kwargs))
        return child_definition

    monkeypatch.setattr(
        published_agent_tools.DeclarativeReviewAgentFactory,
        "resolve_release",
        resolve_child,
    )
    calls = []

    class Client:
        def __init__(self, api_config):
            pass

        def call_raw(self, **kwargs):
            calls.append(kwargs)
            label = kwargs["agent_label"]
            return '{"summary":"' + label + ' finished","score":90,"issues":[]}', {}

        def log_deferred(self, *_args, **_kwargs):
            pass

    monkeypatch.setattr(published_agent_tools, "DeepSeekAgent", Client)
    result = published_agent_tools.invoke_published_agent(
        db, admin_user, agent_code="root_reviewer", code="if user_id != owner_id: deny()",
        language="python", file_name="access.py",
    )

    assert [call["agent_label"] for call in calls] == ["auth_child", "root_reviewer"]
    assert resolved_children[0][0] == "auth_child"
    assert resolved_children[0][1]["release_id"] == 20
    assert resolved_children[0][1]["version_id"] == 200
    assert "auth_child finished" in calls[1]["user_prompt"]
    assert result["delegated_runs"][0]["release_id"] == 20
    assert result["delegated_runs"][0]["version_id"] == 200


def test_published_delegate_cycle_fails_before_any_model_call(db, admin_user, monkeypatch):
    profile = _published_profile()
    profile = profile.__class__(
        **{**profile.__dict__, "release_id": 10, "version_id": 100},
    )
    definition = SimpleNamespace(
        to_profile=lambda: profile,
        release_id=10,
        version_id=100,
        delegated_agents=({
            "kind": "custom", "agent_code": "cycle_agent", "release_id": 10, "version_id": 100,
            "package_checksum": "p" * 64, "template_checksum": "t" * 64, "max_depth": 2,
        },),
    )
    monkeypatch.setattr(published_agent_tools, "_require_invoke_permission", lambda *_: None)
    monkeypatch.setattr(
        published_agent_tools.DeclarativeReviewAgentFactory,
        "resolve_published", lambda *_args, **_kwargs: definition,
    )
    monkeypatch.setattr(
        published_agent_tools.DeclarativeReviewAgentFactory,
        "resolve_release", lambda *_args, **_kwargs: definition,
    )

    class UnexpectedClient:
        def __init__(self, *_args, **_kwargs):
            pytest.fail("循环委派必须在模型调用前被阻止")

    monkeypatch.setattr(published_agent_tools, "DeepSeekAgent", UnexpectedClient)
    with pytest.raises(ValidationError, match="循环"):
        published_agent_tools.invoke_published_agent(
            db, admin_user, agent_code="cycle_agent", code="pass", language="python",
        )


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


def test_published_agent_splits_source_after_output_budget_retry_is_truncated(
    db, admin_user, monkeypatch,
):
    """A still-truncated response must shrink source coverage instead of replaying one chunk."""
    _prepare_invocation(monkeypatch, _published_profile())
    monkeypatch.setattr(published_agent_tools.settings, "deepseek_chunk_threshold", 100_000)
    code = "line1 = first()\nline2 = second()\nline3 = third()\nline4 = fourth()\n"
    calls = []

    class Client:
        def __init__(self, api_config):
            pass

        def call_raw(self, **kwargs):
            calls.append(kwargs)
            if len(calls) <= 2:
                raise DeepSeekOutputTruncatedError(
                    "DeepSeek 输出被截断 (finish_reason=length)", finish_reason="length",
                )
            source_line = "line1 = first()" if "line1 = first()" in kwargs["user_prompt"] else "line3 = third()"
            return (
                '{"summary":"已检查焦点分片","score":100,"issues":[{'
                f'"line_number":1,"issue_type":"潜在Bug","severity":"高","title":"边界问题",'
                f'"description":"此处分支没有处理无效状态。","suggestion":"增加状态校验。",'
                f'"evidence":"{source_line}"' + "}]}",
                {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30},
            )

        def log_deferred(self, *_args, **_kwargs):
            pass

    monkeypatch.setattr(published_agent_tools, "DeepSeekAgent", Client)
    result = published_agent_tools.invoke_published_agent(
        db,
        admin_user,
        agent_code="auth_boundary_reviewer",
        code=code,
        language="python",
        file_name="sample.py",
    )

    assert [call["max_tokens"] for call in calls] == [4096, 16_384, 4096, 4096]
    assert [item["line_number"] for item in result["issues"]] == [1, 3]
    assert result["coverage"]["status"] == "complete"
    assert result["coverage"]["completed_chunks"] == result["coverage"]["total_chunks"] == 2
    assert "source_sha256" in result["coverage"]
    assert all("审查分片:" not in call["user_prompt"] for call in calls)
    assert all("审查焦点: 原文件行" in call["user_prompt"] for call in calls)


def test_published_agent_rejects_end_line_outside_focused_chunk():
    chunk = SimpleNamespace(text="one()\ntwo()", start_line=0, end_line=2)
    issue = SimpleNamespace(line_number=1, end_line=3, evidence="one()")

    with pytest.raises(DeepSeekResponseError, match="结束行"):
        published_agent_tools._validate_issue_source_evidence(issue, chunk)


@pytest.mark.parametrize("field", ["line_number", "end_line"])
def test_published_agent_rejects_boolean_source_lines(field):
    chunk = SimpleNamespace(text="one()\ntwo()", start_line=0, end_line=2)
    issue = SimpleNamespace(line_number=1, end_line=1, evidence="one()")
    setattr(issue, field, True)

    with pytest.raises(DeepSeekResponseError, match="行号|结束行"):
        published_agent_tools._validate_issue_source_evidence(issue, chunk)


@pytest.mark.parametrize("line_offset", [-1, 10_000_001, True])
def test_published_agent_rejects_invalid_line_offset(db, admin_user, monkeypatch, line_offset):
    _prepare_invocation(monkeypatch, _published_profile())

    class UnexpectedClient:
        def __init__(self, api_config):
            pytest.fail("invalid line_offset must be rejected before model invocation")

    monkeypatch.setattr(published_agent_tools, "DeepSeekAgent", UnexpectedClient)
    with pytest.raises(ValidationError, match="line_offset"):
        published_agent_tools.invoke_published_agent(
            db,
            admin_user,
            agent_code="auth_boundary_reviewer",
            code="pass",
            line_offset=line_offset,
        )
