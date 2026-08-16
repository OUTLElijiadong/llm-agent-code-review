"""模型分层:总调度者 pro / 子 Agent flash / Responses 运行时回退契约测试。"""
from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Dict, List, Mapping, Optional

import pytest

import app.services.agent_responses_service as service_module
from app.agents.orchestrator import Orchestrator
from app.core.config import Settings, settings
from app.services.deepseek_responses_runtime import (
    COMPLETED,
    FAILED,
    DeepSeekResponsesRuntime,
    InMemoryCheckpointStore,
    ToolExecutionResult,
)


def _message_response(text: str) -> Dict[str, Any]:
    return {
        "id": "resp_final",
        "object": "response",
        "status": COMPLETED,
        "output": [
            {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": text}],
            }
        ],
    }


class NoopExecutor:
    """不会产生工具调用的最小执行器。"""

    async def execute(self, call: Any, *, approved: bool = False) -> ToolExecutionResult:
        return ToolExecutionResult.success(None)


class ModelErrorThenSuccessTransport:
    """第一次抛指定异常,第二次起返回成功响应。"""

    def __init__(self, error: BaseException, success_text: str) -> None:
        self._error = error
        self._success_text = success_text
        self.calls = 0
        self.payloads: List[Dict[str, Any]] = []

    async def create_response(self, payload: Mapping[str, Any]) -> Dict[str, Any]:
        self.calls += 1
        self.payloads.append(dict(payload))
        if self.calls == 1:
            raise self._error
        return _message_response(self._success_text)


class AlwaysModelErrorTransport:
    """每次调用都抛模型不可用错误,用于验证只回退一次。"""

    def __init__(self) -> None:
        self.calls = 0
        self.payloads: List[Dict[str, Any]] = []

    async def create_response(self, payload: Mapping[str, Any]) -> Dict[str, Any]:
        self.calls += 1
        self.payloads.append(dict(payload))
        raise RuntimeError("The model `deepseek-v4-pro` not_found")


def _runtime(
    transport: Any,
    *,
    model: str = "deepseek-v4-pro",
    fallback_model: Optional[str] = "deepseek-v4-flash",
    **options: Any,
) -> DeepSeekResponsesRuntime:
    return DeepSeekResponsesRuntime(
        transport=transport,
        tool_executor=NoopExecutor(),
        checkpoint_store=InMemoryCheckpointStore(),
        model=model,
        fallback_model=fallback_model,
        **options,
    )


class CapturingRuntime:
    """记录 Responses 运行时收到的构造参数。"""

    captured: Dict[str, Any] = {}

    def __init__(self, **kwargs: Any) -> None:
        type(self).captured = dict(kwargs)


def test_settings_model_tiering_defaults() -> None:
    configured = Settings(_env_file=None)

    assert configured.deepseek_orchestrator_model == "deepseek-v4-pro"
    assert configured.deepseek_orchestrator_fallback_to_flash is True
    assert configured.agent_mesh_supervision_max_rounds == 3


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("surface", "user"),
    [
        ("user", SimpleNamespace(id=7, username="tester", role="user")),
        ("admin", SimpleNamespace(id=9, username="manager", role="admin")),
    ],
)
async def test_runtime_uses_orchestrator_pro_with_flash_fallback(
    db, monkeypatch, surface: str, user: Any
) -> None:
    monkeypatch.setattr(service_module.settings, "deepseek_model", "deepseek-v4-flash")
    monkeypatch.setattr(service_module.settings, "deepseek_orchestrator_model", "deepseek-v4-pro")
    monkeypatch.setattr(service_module.settings, "deepseek_orchestrator_fallback_to_flash", True)
    monkeypatch.setattr(
        service_module,
        "resolve_api_config",
        lambda *_args, **_kwargs: SimpleNamespace(
            model="deepseek-v4-flash", source="system",
        ),
    )
    monkeypatch.setattr(
        service_module,
        "NativeResponsesTransport",
        lambda *_args, **_kwargs: object(),
    )
    monkeypatch.setattr(service_module, "DeepSeekResponsesRuntime", CapturingRuntime)
    monkeypatch.setattr(
        service_module,
        "get_request_orchestrator",
        lambda *_args, **_kwargs: object(),
    )

    service = service_module.AgentResponsesService(
        db,
        user,
        surface=surface,
        session_key=f"session-{surface}",
    )
    await service._runtime(f"run-{surface}", None)

    assert CapturingRuntime.captured["model"] == "deepseek-v4-pro"
    assert CapturingRuntime.captured["fallback_model"] == "deepseek-v4-flash"


@pytest.mark.asyncio
async def test_runtime_fallback_disabled_passes_none(db, monkeypatch) -> None:
    monkeypatch.setattr(service_module.settings, "deepseek_model", "deepseek-v4-flash")
    monkeypatch.setattr(service_module.settings, "deepseek_orchestrator_model", "deepseek-v4-pro")
    monkeypatch.setattr(service_module.settings, "deepseek_orchestrator_fallback_to_flash", False)
    monkeypatch.setattr(
        service_module,
        "resolve_api_config",
        lambda *_args, **_kwargs: SimpleNamespace(
            model="deepseek-v4-flash", source="system",
        ),
    )
    monkeypatch.setattr(
        service_module,
        "NativeResponsesTransport",
        lambda *_args, **_kwargs: object(),
    )
    monkeypatch.setattr(service_module, "DeepSeekResponsesRuntime", CapturingRuntime)
    monkeypatch.setattr(
        service_module,
        "get_request_orchestrator",
        lambda *_args, **_kwargs: object(),
    )

    service = service_module.AgentResponsesService(
        db,
        SimpleNamespace(id=7, username="tester", role="user"),
        surface="user",
        session_key="session-user-no-fallback",
    )
    await service._runtime("run-user-no-fallback", None)

    assert CapturingRuntime.captured["model"] == "deepseek-v4-pro"
    assert CapturingRuntime.captured["fallback_model"] is None


@pytest.mark.asyncio
async def test_runtime_prefers_custom_api_config_model(db, monkeypatch) -> None:
    monkeypatch.setattr(service_module.settings, "deepseek_model", "deepseek-v4-flash")
    monkeypatch.setattr(service_module.settings, "deepseek_orchestrator_model", "deepseek-v4-pro")
    monkeypatch.setattr(service_module.settings, "deepseek_orchestrator_fallback_to_flash", True)
    monkeypatch.setattr(
        service_module,
        "resolve_api_config",
        lambda *_args, **_kwargs: SimpleNamespace(
            model="user-custom-pro", source="user",
        ),
    )
    monkeypatch.setattr(
        service_module,
        "NativeResponsesTransport",
        lambda *_args, **_kwargs: object(),
    )
    monkeypatch.setattr(service_module, "DeepSeekResponsesRuntime", CapturingRuntime)
    monkeypatch.setattr(
        service_module,
        "get_request_orchestrator",
        lambda *_args, **_kwargs: object(),
    )

    service = service_module.AgentResponsesService(
        db,
        SimpleNamespace(id=7, username="tester", role="user"),
        surface="user",
        session_key="session-user-custom",
    )
    await service._runtime("run-user-custom", None)

    assert CapturingRuntime.captured["model"] == "user-custom-pro"
    assert CapturingRuntime.captured["fallback_model"] == "deepseek-v4-flash"


def test_orchestrator_and_chat_agent_use_pro_while_subagents_use_flash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "deepseek_model", "deepseek-v4-flash")
    monkeypatch.setattr(settings, "deepseek_orchestrator_model", "deepseek-v4-pro")

    orch = Orchestrator(register=False)

    assert orch._model == "deepseek-v4-pro"
    assert orch.chat_agent._model == "deepseek-v4-pro"
    assert orch.lang_agent._model == "deepseek-v4-flash"


def test_orchestrator_restores_pro_model_when_api_config_is_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "deepseek_model", "deepseek-v4-flash")
    monkeypatch.setattr(settings, "deepseek_orchestrator_model", "deepseek-v4-pro")

    orch = Orchestrator(register=False)
    orch.set_api_config(
        SimpleNamespace(
            base_url="https://example.test/v1/",
            api_key="secret-test-key",
            model="custom-model",
            source="user",
        )
    )

    assert orch.chat_agent._model == "custom-model"

    orch.set_api_config(None)

    assert orch.chat_agent._model == "deepseek-v4-pro"


@pytest.mark.asyncio
async def test_runtime_falls_back_once_for_model_unavailable_error() -> None:
    transport = ModelErrorThenSuccessTransport(
        RuntimeError("The model `deepseek-v4-pro` does not exist or you do not have access to it."),
        "降级后完成",
    )
    runtime = _runtime(transport)

    result = await runtime.start("你好", run_id="run_fallback_success")

    assert result.status == COMPLETED
    assert result.output_text == "降级后完成"
    assert result.rounds == 1
    assert [payload["model"] for payload in transport.payloads] == [
        "deepseek-v4-pro",
        "deepseek-v4-flash",
    ]
    checkpoint = await runtime.get_checkpoint("run_fallback_success")
    assert checkpoint.model == "deepseek-v4-flash"
    assert checkpoint.status == COMPLETED


@pytest.mark.asyncio
async def test_runtime_does_not_fallback_for_non_model_error() -> None:
    transport = ModelErrorThenSuccessTransport(
        RuntimeError("Responses transport timed out while streaming model output"),
        "不应到达",
    )
    runtime = _runtime(transport)

    result = await runtime.start("你好", run_id="run_non_model_error")

    assert result.status == FAILED
    assert "Responses transport 调用失败" in result.error
    checkpoint = await runtime.get_checkpoint("run_non_model_error")
    assert checkpoint.model == "deepseek-v4-pro"
    assert transport.calls == 1
    assert len(transport.payloads) == 1


@pytest.mark.asyncio
async def test_runtime_retries_only_once_when_fallback_also_fails() -> None:
    transport = AlwaysModelErrorTransport()
    runtime = _runtime(transport)

    result = await runtime.start("你好", run_id="run_fallback_still_fails")

    assert result.status == FAILED
    assert "Responses transport 调用失败" in result.error
    checkpoint = await runtime.get_checkpoint("run_fallback_still_fails")
    assert checkpoint.model == "deepseek-v4-flash"
    assert transport.calls == 2
    assert [payload["model"] for payload in transport.payloads] == [
        "deepseek-v4-pro",
        "deepseek-v4-flash",
    ]
