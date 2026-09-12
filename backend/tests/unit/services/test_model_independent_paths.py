"""独立模型执行入口和压缩图片的回归，禁止访问外部模型。"""
from unittest.mock import Mock

import pytest

from app.agents.operations_agent import OperationsAgent
from app.ai import discussion_orchestrator as discussion
from app.services import system_config_service as scs
from app.services.deepseek_responses_runtime import ContextBudgetError, compact_transcript
from app.services.multimodal_service import transcript_has_images
from app.utils.api_resolver import ApiConfig


def image_tool_transcript(image_text="x " * 100):
    items = [
        {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "previous"}]},
        {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "answer"}]},
        {"type": "message", "role": "user", "content": [
            {"type": "input_text", "text": image_text},
            {"type": "input_image", "image_url": "prism-asset://test-image"},
        ]},
    ]
    for index in range(12):
        items.extend([
            {"type": "function_call", "call_id": str(index), "name": "read", "arguments": "{}"},
            {"type": "function_call_output", "call_id": str(index), "output": "x " * 800},
        ])
    return items


def test_compaction_preserves_current_image_after_text_history_and_tool_loop():
    transcript = image_tool_transcript()
    projected, metadata = compact_transcript(transcript, context_window_tokens=3000,
        max_output_tokens=500, compaction_threshold_tokens=1500, keep_recent_tokens=500)
    assert metadata["compacted"]
    assert transcript_has_images(projected)
    assert projected != transcript
    assert transcript_has_images(transcript)


def test_compaction_rejects_budget_that_cannot_keep_image_input():
    with pytest.raises(ContextBudgetError):
        compact_transcript(image_tool_transcript("x " * 10000), context_window_tokens=3000,
            max_output_tokens=500, compaction_threshold_tokens=1500, keep_recent_tokens=500)


def test_scheduled_operations_consumes_named_model_assignment(db, monkeypatch):
    from app.agents import operations_agent as operations
    scs.replace_model_registry(db, [{"id": "ops-model"}])
    scs.update_model_assignments(db, {"agent:operations": "ops-model"})
    cfg = ApiConfig(api_key="test", base_url="https://example.invalid/v1", model="default", source="global")
    monkeypatch.setattr(operations, "resolve_api_config", lambda *_: cfg)
    agent = OperationsAgent()
    agent.call = Mock(return_value=Mock(success=True, data="ok", error=None))
    agent._log_call = Mock()
    agent.diagnose(db, None, {"status": "isolated"}, "test")
    assert agent.call.call_args.kwargs["api_config"].model == "ops-model"
    assert cfg.model == "default"


@pytest.mark.parametrize("source", ["global", "user"])
def test_discussion_clients_honor_named_assignments_and_personal_override(db, monkeypatch, source):
    from app.ai.multi_agent import get_agent_profiles
    scs.replace_model_registry(db, [{"id": "review-model"}, {"id": "security-model"}])
    scs.update_model_assignments(
        db, {"agent:code_reviewer": "review-model", "agent:security_sentinel": "security-model"},
    )
    cfg = ApiConfig(api_key="test", base_url="https://example.invalid/v1", model="personal", source=source)
    monkeypatch.setattr(discussion, "SessionLocal", lambda: db)
    monkeypatch.setattr(discussion, "resolve_api_config", lambda _db, user_id: cfg)
    primary, clients = discussion._build_discussion_agents(42, get_agent_profiles("full"))
    assert primary.model == ("personal" if source == "user" else "review-model")
    assert clients["security_sentinel"].model == ("personal" if source == "user" else "security-model")
    assert primary.base_url == "https://example.invalid/v1"
    assert clients["code_reviewer"] is primary


@pytest.mark.parametrize("module_name,class_name", [
    ("test_case_generator_agent", "TestCaseGeneratorAgent"),
    ("deployment_coordinator_agent", "DeploymentCoordinatorAgent"),
    ("test_review_reporter_agent", "TestReviewReporterAgent"),
    ("syntax_repair_agent", "SyntaxRepairAgent"),
    ("evolution_agent", "EvolutionAgent"),
])
@pytest.mark.parametrize("source", ["global", "user"])
def test_independent_instances_share_config_without_changing_other_requests(
    db, monkeypatch, module_name, class_name, source,
):
    import importlib

    from app.services.agent_model_service import configure_subagent
    cfg = ApiConfig(api_key="test", base_url="https://example.invalid/v1", model="original", source=source,
                    timeout_seconds=31, max_retries=2, temperature=0.2)
    scs.replace_model_registry(db, [{"id": "subagent-model"}])
    scs.update_model_assignments(db, {"subagent": "subagent-model"})
    monkeypatch.setattr("app.utils.api_resolver.resolve_api_config", lambda _db, user_id: cfg)
    agent_class = getattr(importlib.import_module("app.agents." + module_name), class_name)
    untouched = agent_class()
    original_model = untouched._model
    selected = configure_subagent(db, agent_class(), 42)
    assert selected._model == ("original" if source == "user" else "subagent-model")
    assert selected._base_url == cfg.base_url
    assert selected._api_key == "test"
    assert (selected._timeout, selected._max_retries, selected._temperature) == (31, 2, 0.2)
    assert untouched._model == original_model
    assert cfg.model == "original"


def test_deployment_service_consumes_config_before_missing_key_fallback(db, monkeypatch):
    from types import SimpleNamespace

    from app.agents.deployment_coordinator_agent import DeploymentCoordinatorAgent
    from app.services.sandbox_service import _generate_deployment_patch
    from tests.unit.services.test_agent_test_cases import _zip_with
    scs.replace_model_registry(db, [{"id": "sandbox-model"}])
    scs.update_model_assignments(db, {"subagent": "sandbox-model"})
    cfg = ApiConfig(api_key="test", base_url="https://example.invalid/v1", model="default", source="global")
    owners, seen = [], []
    def resolve(_db, user_id):
        owners.append(user_id)
        return cfg
    monkeypatch.setattr("app.utils.api_resolver.resolve_api_config", resolve)
    def plan(self, **kwargs):
        seen.append(self._model)
        return {"launch_script": "exec python main.py", "notes": "isolated"}
    monkeypatch.setattr(DeploymentCoordinatorAgent, "plan", plan)
    environment = SimpleNamespace(id=1, public_id="isolated", project_id=1, owner_id=42, test_mode="blackbox")
    result = _generate_deployment_patch(db, environment, _zip_with({"main.py": "pass"}), "python")
    assert result["launch_script"] == "exec python main.py"
    assert seen == ["sandbox-model"]
    assert owners == [42]


@pytest.mark.parametrize("source", ["global", "user"])
@pytest.mark.parametrize("surface", ["published", "pentest"])
def test_published_and_pentest_real_entry_consume_model_config(db, admin_user, monkeypatch, source, surface):
    from types import SimpleNamespace

    from app.ai.multi_agent import GENERAL_AGENT
    from app.services import pentest_service, published_agent_tools
    module = published_agent_tools if surface == "published" else pentest_service
    scs.replace_model_registry(db, [{"id": "child-model"}, {"id": "security-model"}])
    scs.update_model_assignments(db, {"subagent": "child-model", "agent:security_sentinel": "security-model"})
    cfg = ApiConfig(api_key="test", base_url="https://example.invalid/v1", model="personal", source=source)
    monkeypatch.setattr(module, "resolve_api_config", lambda *_: cfg)
    seen = []
    class Client:
        def __init__(self, api_config):
            seen.append(api_config)
        def chat(self, **kwargs):
            return '{"ok": true}', {}
        def call_raw(self, **kwargs):
            return '{"summary":"isolated","issues":[]}', {}
        def log_deferred(self, *args, **kwargs):
            pass
    monkeypatch.setattr(module, "DeepSeekAgent", Client)
    if surface == "published":
        monkeypatch.setattr(module, "_require_invoke_permission", lambda *_: None)
        monkeypatch.setattr(module.DeclarativeReviewAgentFactory, "resolve_published",
                            lambda *args, **kwargs: SimpleNamespace(to_profile=lambda: GENERAL_AGENT))
        result = module.invoke_published_agent(db, admin_user, agent_code="custom", code="pass", language="python")
        assert result["summary"] == "isolated"
    else:
        result = module._call_llm_json(db, admin_user.id, system_prompt="test", user_prompt="test", agent_label="test")
        assert result == {"ok": True}
    expected = "personal" if source == "user" else "child-model" if surface == "published" else "security-model"
    assert seen[0].model == expected
    assert seen[0].base_url == cfg.base_url
    assert cfg.model == "personal"
