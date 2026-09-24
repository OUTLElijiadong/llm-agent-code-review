"""Published Agent input coverage and approved Skill context regression tests."""

import hashlib
from types import SimpleNamespace

import pytest

from app.ai.multi_agent import GENERAL_AGENT
from app.core.exceptions import ValidationError
from app.services import declarative_agent_runtime, published_agent_tools
from app.services.declarative_agent_runtime import DeclarativeReviewAgentFactory


def test_all_approved_skills_reach_profile_in_order(monkeypatch):
    monkeypatch.setattr(
        DeclarativeReviewAgentFactory,
        "_compile_skill_version",
        lambda _db, _code, version_id, **_kwargs: f"[{version_id}]" + "文" * 7000,
    )
    compiled = DeclarativeReviewAgentFactory._compile_skills(
        None,
        "approved_agent",
        {"skills": [
            {"position": 2, "skill_version_id": 2},
            {"position": 1, "skill_version_id": 1},
        ]},
        user=None,
    )
    assert compiled.startswith("[1]")
    assert "[2]" in compiled
    assert compiled.endswith("文" * 7000)


def test_delegate_prompt_is_not_silently_cut(db, monkeypatch):
    long_prompt = "A" * 3000 + "唯一末尾约束"
    skill_version = SimpleNamespace(
        skill_id=11,
        definition_json='{"agent_code":"delegate_agent"}',
        skill_type="agent_delegate",
    )
    skill = SimpleNamespace(name="委派")
    target_asset = SimpleNamespace(
        code="delegate_agent",
        name="目标代理",
        is_enabled=1,
        current_published_version_id=12,
    )
    target_version = SimpleNamespace(
        version_number=1,
        prompt=long_prompt,
        review_focus="身份校验",
    )
    monkeypatch.setattr(db, "get", lambda model, pk: {
        10: skill_version,
        11: skill,
        12: target_version,
    }.get(pk))
    class Query:
        def filter(self, *_args):
            return self

        def first(self):
            return target_asset

    monkeypatch.setattr(db, "query", lambda *_args: Query())
    rendered = DeclarativeReviewAgentFactory._compile_skill_version(
        db, "main_agent", 10, user=None, depth=0, path=(),
    )
    assert "唯一末尾约束" in rendered


def test_readonly_tool_result_is_not_silently_cut(db, admin_user, monkeypatch):
    output = "B" * 3000 + "唯一末尾证据"
    monkeypatch.setattr(
        declarative_agent_runtime,
        "get_request_orchestrator",
        lambda *_args, **_kwargs: SimpleNamespace(
            call_tool=lambda *_args, **_kwargs: SimpleNamespace(success=True, data={"proof": output}),
        ),
    )
    monkeypatch.setattr(
        declarative_agent_runtime.tool_gateway,
        "execute",
        lambda _db, **kwargs: SimpleNamespace(success=True, data=kwargs["handler"]()),
    )
    rendered = DeclarativeReviewAgentFactory._run_readonly_tool(
        db, "main_agent", "只读证据", {"tool_code": "review_context"}, user=admin_user,
    )
    assert "唯一末尾证据" in rendered


def test_missing_approved_skill_fails_instead_of_skipping(db):
    with pytest.raises(ValidationError, match="无法完整执行"):
        DeclarativeReviewAgentFactory._compile_skill_version(
            db, "main_agent", -1, user=None, depth=0, path=(),
        )


def test_direct_published_agent_covers_all_code_chunks(db, admin_user, monkeypatch):
    profile = GENERAL_AGENT.__class__(
        **{**GENERAL_AGENT.__dict__, "code": "approved_agent", "max_tokens": 4096},
    )
    monkeypatch.setattr(published_agent_tools, "_require_invoke_permission", lambda *_args: None)
    monkeypatch.setattr(
        published_agent_tools.DeclarativeReviewAgentFactory,
        "resolve_published",
        lambda *_args, **_kwargs: SimpleNamespace(to_profile=lambda: profile),
    )
    monkeypatch.setattr(published_agent_tools.settings, "deepseek_chunk_threshold", 2000)
    prompts = []

    class Client:
        def __init__(self, api_config):
            pass

        def call_raw(self, **kwargs):
            prompts.append(kwargs["user_prompt"])
            return '{"summary":"已审查片段","score":95,"issues":[]}', {}

        def log_deferred(self, *_args, **_kwargs):
            pass

    monkeypatch.setattr(published_agent_tools, "DeepSeekAgent", Client)
    code = "".join(f"line_{n} = {n}\n" for n in range(700))
    result = published_agent_tools.invoke_published_agent(
        db, admin_user, agent_code="approved_agent", code=code, language="python",
    )
    assert len(prompts) > 1
    assert "line_0 = 0" in prompts[0]
    assert "line_699 = 699" in prompts[-1]
    source_windows = [
        prompt.split("## 代码内容\n```python\n", 1)[1].split("\n```", 1)[0]
        for prompt in prompts
    ]
    assert "".join(source_windows) == code
    assert result["coverage"]["completed_chunks"] == result["coverage"]["total_chunks"]
    assert result["coverage"]["source_sha256"] == hashlib.sha256(code.encode()).hexdigest()


def test_invalid_issue_in_one_chunk_cannot_be_reported_complete(db, admin_user, monkeypatch):
    profile = GENERAL_AGENT.__class__(
        **{**GENERAL_AGENT.__dict__, "code": "approved_agent", "max_tokens": 4096},
    )
    monkeypatch.setattr(published_agent_tools, "_require_invoke_permission", lambda *_args: None)
    monkeypatch.setattr(
        published_agent_tools.DeclarativeReviewAgentFactory,
        "resolve_published",
        lambda *_args, **_kwargs: SimpleNamespace(to_profile=lambda: profile),
    )
    monkeypatch.setattr(published_agent_tools.settings, "deepseek_chunk_threshold", 500)
    calls = []

    class Client:
        def __init__(self, api_config):
            pass

        def call_raw(self, **kwargs):
            calls.append(kwargs["user_prompt"])
            if len(calls) == 2:
                return (
                    '{"summary":"bad","score":0,"issues":['
                    '{"title":"valid","severity":"高"},'
                    '{"title":"invalid","severity":"unknown"}]}',
                    {},
                )
            return '{"summary":"good","score":100,"issues":[]}', {}

        def log_deferred(self, *_args, **_kwargs):
            pass

    monkeypatch.setattr(published_agent_tools, "DeepSeekAgent", Client)
    with pytest.raises(published_agent_tools.DeepSeekResponseError, match="无法声明完整审查"):
        published_agent_tools.invoke_published_agent(
            db,
            admin_user,
            agent_code="approved_agent",
            code="".join(f"line_{n} = {n}\n" for n in range(150)),
            language="python",
        )
    assert len(calls) == 2


def test_oversized_approved_skill_fails_before_model_call(db, admin_user, monkeypatch):
    profile = GENERAL_AGENT.__class__(
        **{
            **GENERAL_AGENT.__dict__,
            "code": "approved_agent",
            "instruction": "必须完整阅读" + "S" * 160_000,
        },
    )
    monkeypatch.setattr(published_agent_tools, "_require_invoke_permission", lambda *_args: None)
    monkeypatch.setattr(
        published_agent_tools.DeclarativeReviewAgentFactory,
        "resolve_published",
        lambda *_args, **_kwargs: SimpleNamespace(to_profile=lambda: profile),
    )
    monkeypatch.setattr(published_agent_tools.settings, "deepseek_context_window_tokens", 100_000)
    class Client:
        def __init__(self, api_config):
            pass

        def call_raw(self, **_kwargs):
            pytest.fail("model must not receive incomplete approved Skill context")

    monkeypatch.setattr(published_agent_tools, "DeepSeekAgent", Client)
    with pytest.raises(Exception, match="上下文|预算|Skill"):
        published_agent_tools.invoke_published_agent(
            db, admin_user, agent_code="approved_agent", code="x = 1", language="python",
        )


def test_unicode_single_line_adapts_to_input_budget_without_data_loss(monkeypatch):
    profile = GENERAL_AGENT.__class__(
        **{**GENERAL_AGENT.__dict__, "code": "approved_agent", "max_tokens": 4096},
    )
    monkeypatch.setattr(published_agent_tools.settings, "deepseek_context_window_tokens", 100_000)
    monkeypatch.setattr(published_agent_tools.settings, "deepseek_max_output_tokens", 65_536)
    code = "界" * 90_000
    source_sha256, calls = published_agent_tools._plan_complete_review(
        profile,
        code=code,
        language="plaintext",
        file_name="long.txt",
        rules=[],
        line_offset=0,
        experience="",
    )
    assert len(calls) > 1
    assert "".join(chunk.text for chunk, _, _ in calls) == code
    assert source_sha256 == hashlib.sha256(code.encode()).hexdigest()
    input_budget = 100_000 - 65_536 - 1024
    assert all(
        len(system.encode()) + len(user.encode()) <= input_budget
        for _, system, user in calls
    )
