"""模型分配与图片恢复回归；隔离数据库和模拟上游。"""

from types import SimpleNamespace

import pytest

from app.api.v1 import llm_config
from app.core.exceptions import ValidationError
from app.schemas.llm_config import LlmModelsIn, LlmModelsOut
from app.services import agent_responses_service as ars
from app.services import multimodal_service as ms
from app.services import system_config_service as scs
from app.services.deepseek_responses_runtime import RunCheckpoint
from app.utils.api_resolver import ApiConfig
from tests.unit.services.test_multimodal_service import PNG_URL


def test_flash_official_capability_and_no_name_guess(db):
    models, _ = scs.merge_pulled_models(db, ["deepseek-flash", "deepseek-v4-pro", "unknown-vision-name"])
    assert {m["id"]: m["vision"] for m in models} == {
        "deepseek-flash": True,
        "deepseek-v4-pro": False,
        "unknown-vision-name": False,
    }


def test_vision_assignment_must_support_images(db):
    scs.replace_model_registry(db, [{"id": "deepseek-v4-pro", "vision": False}])
    with pytest.raises(ValidationError, match="视觉"):
        scs.update_model_assignments(db, {"chat_vision": "deepseek-v4-pro"})


def test_assigned_model_cannot_be_removed_or_downgraded(db):
    scs.replace_model_registry(db, [{"id": "vision-custom", "vision": True}])
    scs.update_model_assignments(db, {"chat_vision": "vision-custom"})
    with pytest.raises(ValidationError):
        scs.replace_model_registry(db, [])
    with pytest.raises(ValidationError):
        scs.replace_model_registry(db, [{"id": "vision-custom", "vision": False}])
    assert scs.get_model_registry(db)[0]["vision"] is True


@pytest.mark.parametrize("success,fallback", [(False, True), (True, True)])
def test_failed_or_fallback_sync_does_not_mutate_registry(db, monkeypatch, success, fallback):
    scs.replace_model_registry(db, [{"id": "keep", "vision": False}])
    monkeypatch.setattr(
        llm_config,
        "_resolve_draft",
        lambda *_a: {"base_url": "https://api.deepseek.com", "api_key": "test", "model": "manual"},
    )
    monkeypatch.setattr(
        llm_config.api_config_service,
        "fetch_models",
        lambda *_a: LlmModelsOut(success=success, fallback=fallback, models=["manual"], message="上游不支持"),
    )
    monkeypatch.setattr(llm_config.audit_service, "log", lambda *_a, **_k: None)
    result = llm_config.sync_registry(LlmModelsIn(), db, SimpleNamespace(id=1))
    assert result.data.success is False
    assert [m["id"] for m in scs.get_model_registry(db)] == ["keep"]


@pytest.mark.asyncio
async def test_runtime_consumes_chat_assignment_and_retains_resume_assets(db, monkeypatch):
    scs.replace_model_registry(db, [{"id": "chat-custom", "vision": False}, {"id": "vision-custom", "vision": True}])
    scs.update_model_assignments(db, {"chat": "chat-custom", "chat_vision": "vision-custom"})
    config = ApiConfig(api_key="test", base_url="https://api.deepseek.com", model="system-base", source="system")
    monkeypatch.setattr(ars, "resolve_api_config", lambda *_a, **_k: config)
    monkeypatch.setattr(ars, "NativeResponsesTransport", lambda *_a, **_k: object())
    monkeypatch.setattr(ars, "get_request_orchestrator", lambda *_a, **_k: object())
    user = SimpleNamespace(id=7, role="user", username="test")
    service = ars.AgentResponsesService(db, user, surface="user", session_key="session-images")
    _, plain = await service._runtime("new-text-run", None)
    assert plain._model == "chat-custom"
    assets = ms.store_message_images(db, user_id=7, run_id="image-run", surface="user", images=[PNG_URL])
    checkpoint = RunCheckpoint(
        run_id="image-run",
        model="vision-custom",
        transcript=[{"role": "user", "content": ms.multimodal_content_parts("图", assets)}],
        tools=[],
        instructions="",
    )
    await service._store.create(checkpoint)
    _, resumed = await service._runtime("image-run", None)
    assert resumed._image_assets == ms.image_asset_map(assets)
    assert resumed._fallback_model is None
    _, next_plain = await service._runtime("next-text-run", None)
    assert next_plain._model == "chat-custom"
    assert next_plain._image_assets == {}
    other = ars.AgentResponsesService(db, user, surface="user", session_key="session-other")
    with pytest.raises(Exception):
        await other._runtime("image-run", None)


def test_assistant_images_and_historical_image_slots_rejected():
    from app.api.v1.agent_responses import AgentResponsesRequest

    with pytest.raises(ValueError):
        AgentResponsesRequest(
            session_id="session-images",
            messages=[{"role": "user", "content": "x"}, {"role": "assistant", "content": "y", "images": [PNG_URL]}],
        )
    with pytest.raises(ValueError):
        AgentResponsesRequest(
            session_id="session-images",
            messages=[
                {"role": "user", "content": "old", "images": [PNG_URL]},
                {"role": "assistant", "content": "y"},
                {"role": "user", "content": "new"},
            ],
        )


def test_subagent_config_is_request_scoped_and_preserves_user_override(db):
    from app.services.agent_model_service import resolve_subagent_config

    scs.replace_model_registry(db, [{"id": "worker-custom", "vision": False}])
    scs.update_model_assignments(db, {"subagent": "worker-custom"})
    original = ApiConfig(api_key="k", base_url="https://api.deepseek.com", model="default", source="global")
    selected = resolve_subagent_config(db, original)
    assert selected.model == "worker-custom"
    assert original.model == "default"
    original.source = "user"
    assert resolve_subagent_config(db, original).model == "default"


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal", ["completed", "failed", "cancelled"])
async def test_vision_terminal_does_not_change_next_plain_model(db, monkeypatch, terminal):
    scs.replace_model_registry(db, [{"id": "text-default", "vision": False}])
    scs.update_model_assignments(db, {"chat": "text-default"})
    cfg = ApiConfig(api_key="k", base_url="https://api.deepseek.com", model="default", source="global")
    monkeypatch.setattr(ars, "resolve_api_config", lambda *_a: cfg)
    monkeypatch.setattr(ars, "get_request_orchestrator", lambda *_a, **_k: object())
    monkeypatch.setattr(ars, "NativeResponsesTransport", lambda *_a: object())
    service = ars.AgentResponsesService(
        db, SimpleNamespace(id=1, role="user", username="a"), surface="user", session_key="same-session"
    )
    checkpoint = RunCheckpoint(
        run_id="terminal-image", model="deepseek-flash", status=terminal, transcript=[], tools=[], instructions=""
    )
    await service._store.create(checkpoint)
    _, next_runtime = await service._runtime("new-plain", None)
    assert next_runtime._model == "text-default"
    assert next_runtime._image_assets == {}
    assert cfg.model == "default"


@pytest.mark.asyncio
async def test_missing_asset_and_vision_unavailable_never_silently_drop_image():
    from app.services.deepseek_responses_runtime import DeepSeekResponsesRuntime, InMemoryCheckpointStore

    payloads = []

    async def unavailable(payload):
        payloads.append(payload)
        raise RuntimeError("model not_found")

    parts = ms.multimodal_content_parts("请看图", [{"sha256": "image-key"}])
    missing = DeepSeekResponsesRuntime(
        transport=unavailable,
        tool_executor=object(),
        checkpoint_store=InMemoryCheckpointStore(),
        model="deepseek-flash",
    )
    failed = await missing.start([{"role": "user", "content": parts}])
    assert failed.status == "failed" and "图片留档缺失" in failed.error
    assert payloads == []
    vision = DeepSeekResponsesRuntime(
        transport=unavailable,
        tool_executor=object(),
        checkpoint_store=InMemoryCheckpointStore(),
        model="deepseek-flash",
        fallback_model="text-only",
        image_assets={"image-key": PNG_URL},
    )
    failed = await vision.start([{"role": "user", "content": parts}])
    assert failed.status == "failed"
    assert len(payloads) == 1 and payloads[0]["model"] == "deepseek-flash"


@pytest.mark.asyncio
async def test_cancellation_does_not_require_models_or_images(db, monkeypatch):
    service = ars.AgentResponsesService(
        db, SimpleNamespace(id=1, role="user", username="a"), surface="user", session_key="cancel-session"
    )
    checkpoint = RunCheckpoint(
        run_id="cancel-image",
        model="deepseek-flash",
        transcript=[{"role": "user", "content": ms.multimodal_content_parts("图", [{"sha256": "lost"}])}],
        tools=[],
        instructions="",
    )
    await service._store.create(checkpoint)
    monkeypatch.setattr(ars, "resolve_api_config", lambda *_a: (_ for _ in ()).throw(RuntimeError("配置坏了")))
    result = await service.cancel(run_id="cancel-image", reason="停止")
    assert result.status == "cancelled"


@pytest.mark.asyncio
async def test_service_image_question_resume_and_same_session_text_round(db, monkeypatch):
    import json

    from app.models.agent_response_run import AgentResponseRun

    scs.replace_model_registry(db, [{"id": "plain-model", "vision": False}, {"id": "deepseek-flash", "vision": True}])
    scs.update_model_assignments(db, {"chat": "plain-model", "chat_vision": "deepseek-flash"})
    config = ApiConfig(api_key="test", base_url="https://api.deepseek.com", model="system-default", source="system")
    monkeypatch.setattr(ars, "resolve_api_config", lambda *_a, **_k: config)
    payloads = []

    class Executor:
        def __init__(self, *_a, **_k):
            pass

        async def tool_schemas(self):
            return []

    class Transport:
        def __init__(self, *_a, **_k):
            pass

        async def create_response(self, payload):
            payloads.append(payload)
            if len(payloads) == 1:
                return {
                    "id": "vision1",
                    "model": payload["model"],
                    "status": "completed",
                    "output": [
                        {
                            "type": "function_call",
                            "call_id": "ask-image",
                            "name": "ask_user",
                            "arguments": json.dumps({"question": "需要关注什么？"}),
                        }
                    ],
                }
            return {
                "id": "answer",
                "model": payload["model"],
                "status": "completed",
                "output": [{"type": "message", "content": [{"type": "output_text", "text": "图片标题清晰"}]}],
            }

    monkeypatch.setattr(ars, "PrismToolExecutor", Executor)
    monkeypatch.setattr(ars, "NativeResponsesTransport", Transport)
    user = SimpleNamespace(id=7, role="user", username="sample")
    service = ars.AgentResponsesService(db, user, surface="user", session_key="same-chat")
    first = await service.start(
        [{"role": "user", "content": "请看这张图", "images": [PNG_URL]}], run_id="vision-question"
    )
    assert first.status == "waiting_input"
    restored = ars.AgentResponsesService(db, user, surface="user", session_key="same-chat")
    second = await restored.resume(run_id="vision-question", action="answer", call_id="ask-image", answer="标题")
    assert second.status == "completed"
    assert payloads[1]["input"][0]["content"][1]["image_url"] == PNG_URL
    assert payloads[1]["model"] == "deepseek-flash"
    row = db.query(AgentResponseRun).filter_by(run_id="vision-question").one()
    assert "base64" not in row.checkpoint_json
    assert "图片标题清晰" in row.checkpoint_json
    third = await restored.start([{"role": "user", "content": "继续解释", "images": []}], run_id="next-text")
    assert third.status == "completed"
    assert payloads[2]["model"] == "plain-model"
    assert "input_image" not in json.dumps(payloads[2])


@pytest.mark.asyncio
async def test_admin_assignment_is_consumed_and_user_override_has_priority(db, monkeypatch):
    scs.replace_model_registry(db, [{"id": "admin-model", "vision": False}])
    scs.update_model_assignments(db, {"orchestrator": "admin-model"})
    config = ApiConfig(api_key="test", base_url="https://api.deepseek.com", model="default", source="global")
    monkeypatch.setattr(ars, "resolve_api_config", lambda *_a, **_k: config)
    monkeypatch.setattr(ars, "NativeResponsesTransport", lambda *_a: object())
    monkeypatch.setattr(ars, "get_request_orchestrator", lambda *_a, **_k: object())
    monkeypatch.setattr(ars, "_is_super_admin_actor", lambda *_a: False)
    service = ars.AgentResponsesService(
        db, SimpleNamespace(id=9, role="admin", username="admin"), surface="admin", session_key="admin-model-session"
    )
    _, runtime = await service._runtime("new-admin", None)
    assert runtime._model == "admin-model"
    config.source = "user"
    config.model = "personal-model"
    _, runtime = await service._runtime("new-admin-personal", None)
    assert runtime._model == "personal-model"


@pytest.mark.parametrize("source,expected", [("global", "assigned-worker"), ("user", "personal-worker")])
def test_base_agent_explicit_config_honors_subagent_assignment(monkeypatch, source, expected):
    from app.agents.base import BaseAgent
    from app.utils.public_http import PinnedPublicUrl

    sent = []

    class Client:
        def __init__(self, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def post(self, _url, **kwargs):
            sent.append(kwargs["json"])
            return SimpleNamespace(
                status_code=200,
                json=lambda: {"choices": [{"finish_reason": "stop", "message": {"content": "完成"}}], "usage": {}},
            )

    monkeypatch.setattr("app.agents.base.httpx.Client", Client)
    monkeypatch.setattr(
        "app.agents.base.pin_public_http_url",
        lambda url: PinnedPublicUrl(
            url, "https://93.184.216.34/chat/completions", "api.deepseek.com", "api.deepseek.com", "93.184.216.34"
        ),
    )
    agent = BaseAgent()
    agent._assigned_model = "assigned-worker"
    config = ApiConfig(api_key="test", base_url="https://api.deepseek.com", model="personal-worker", source=source)
    result = agent.call("执行", api_config=config)
    assert result.success is True
    assert sent[0]["model"] == expected
    assert config.model == "personal-worker"


def test_named_subagent_override_is_consumed_and_clear_restores_inheritance(db, monkeypatch):
    from app.agents.orchestrator import Orchestrator
    from app.services.agent_model_service import resolve_subagent_config

    roles = scs.get_model_assignment_roles()
    assert "agent:code_reviewer" in roles
    assert "agent:language_detector" in roles
    scs.replace_model_registry(
        db, [{"id": "worker-default", "vision": False}, {"id": "review-specialist", "vision": False}]
    )
    scs.update_model_assignments(db, {"subagent": "worker-default", "agent:code_reviewer": "review-specialist"})
    cfg = ApiConfig(api_key="test", base_url="https://api.deepseek.com", model="platform-default", source="global")
    monkeypatch.setattr("app.agents.orchestrator.resolve_api_config", lambda *_a: cfg)
    user = SimpleNamespace(id=7, role="user", username="sample")
    first = Orchestrator(register=False)
    first.inject_db(db, user)
    assert first.code_reviewer._model == "review-specialist"
    assert first.code_reviewer._assigned_model == "review-specialist"
    assert first.lang_agent._model == "worker-default"
    assert resolve_subagent_config(db, cfg, agent_name="code_reviewer").model == "review-specialist"
    scs.update_model_assignments(db, {"agent:code_reviewer": ""})
    second = Orchestrator(register=False)
    second.inject_db(db, user)
    assert second.code_reviewer._model == "worker-default"
    assert first.code_reviewer._model == "review-specialist"
    assert cfg.model == "platform-default"


def test_stale_named_agent_assignment_is_visible_and_can_be_cleared(db):
    import json

    scs.replace_model_registry(db, [{"id": "worker", "vision": False}])
    scs._set_raw(db, scs.MODEL_ASSIGNMENTS_KEY, json.dumps({"agent:removed_agent": "worker"}))
    view = llm_config._registry_view(db)
    assert "agent:removed_agent" in view.roles
    assert len(view.warnings) == 1 and "已失效" in view.warnings[0]
    with pytest.raises(ValidationError, match="失效"):
        scs.update_model_assignments(db, {"agent:removed_agent": "worker"})
    assert scs.update_model_assignments(db, {"agent:removed_agent": ""}) == {}
