"""Server-owned long history keeps tool evidence and reopens archived images."""

from __future__ import annotations

import hashlib
import json
from types import SimpleNamespace
from typing import Any

import pytest

from app.models.agent_multimodal import AgentMultimodalAsset
from app.models.agent_response_run import AgentResponseRun
from app.services import agent_responses_service as service_module
from app.services import multimodal_service
from app.services.deepseek_responses_runtime import RuntimeResult


def test_incomplete_terminal_event_never_exposes_partial_message() -> None:
    result = RuntimeResult(
        run_id="limited",
        status="incomplete",
        output_text="",
        response={"output": [{
            "type": "message",
            "role": "assistant",
            "content": [{"type": "output_text", "text": "partial claim"}],
        }]},
    )
    event = service_module.terminal_event(result)
    assert event["type"] == "response.incomplete"
    assert event["response"]["output"] == []


def _archive_image(db, *, user_id: int = 7, surface: str = "user", session_key: str = "long-session") -> str:
    binary = b"archived visual evidence"
    digest = hashlib.sha256(binary).hexdigest()
    db.add(AgentResponseRun(
        run_id=f"old-{user_id}-{surface}-{session_key}",
        user_id=user_id,
        surface=surface,
        session_key=session_key,
        status="completed",
        checkpoint_json=json.dumps({"transcript": []}),
    ))
    db.add(AgentMultimodalAsset(
        run_id=f"old-{user_id}-{surface}-{session_key}",
        user_id=user_id,
        surface=surface,
        role="input",
        mime="image/png",
        sha256=digest,
        data=binary,
    ))
    db.commit()
    return digest


def test_history_image_assets_require_same_account_surface_and_session(db) -> None:
    digest = _archive_image(db)
    params = {"user_id": 7, "surface": "user", "session_key": "long-session", "hashes": {digest}}
    assert digest in multimodal_service.load_session_image_assets(db, **params)
    for changed in ({"user_id": 8}, {"surface": "admin"}, {"session_key": "other-session"}):
        with pytest.raises(Exception, match="不属于当前对话"):
            multimodal_service.load_session_image_assets(db, **{**params, **changed})


@pytest.mark.asyncio
async def test_service_continues_raw_tool_transcript_and_restores_own_image(db, monkeypatch) -> None:
    digest = _archive_image(db)
    captured: dict[str, Any] = {}

    class FakeExecutor:
        async def tool_schemas(self):
            return []

    class FakeRuntime:
        async def start(self, items, **kwargs):
            captured["items"] = items
            captured["runtime_args"] = kwargs
            return RuntimeResult(run_id="new-image-run", status="completed")

    async def fake_runtime(_run_id, _sink, *, vision_model="", image_assets=None):
        captured["vision_model"] = vision_model
        captured["image_assets"] = image_assets
        return FakeExecutor(), FakeRuntime()

    monkeypatch.setattr(multimodal_service, "resolve_vision_model", lambda _db: "test-vision")
    monkeypatch.setattr(service_module, "_instructions", lambda *_args, **_kwargs: "test instructions")
    monkeypatch.setattr(
        service_module.strategy_learning_service,
        "build_strategy_context",
        lambda *_args, **_kwargs: "",
    )
    service = service_module.AgentResponsesService(
        db, SimpleNamespace(id=7, username="tester", role="user"),
        surface="user", session_key="long-session",
    )
    monkeypatch.setattr(service, "_runtime", fake_runtime)
    history = [
        {"role": "user", "content": [
            {"type": "input_text", "text": "请看旧图"},
            {"type": "input_image", "image_url": f"prism-asset://{digest}"},
        ]},
        {"type": "function_call", "call_id": "old-tool", "name": "lookup", "arguments": "{}"},
        {"type": "function_call_output", "call_id": "old-tool", "output": '{"verified":true}'},
    ]

    result = await service.start(
        [{"role": "user", "content": "请结合之前核验继续"}],
        run_id="new-image-run", server_history_transcript=history,
    )

    assert result.status == "completed"
    assert captured["items"] == [*history, {"role": "user", "content": "请结合之前核验继续"}]
    assert captured["vision_model"] == "test-vision"
    assert captured["image_assets"][digest].startswith("data:image/png;base64,")
    assert captured["runtime_args"]["run_id"] == "new-image-run"


@pytest.mark.asyncio
async def test_service_refuses_history_image_from_other_conversation(db) -> None:
    digest = _archive_image(db, session_key="other-session")
    service = service_module.AgentResponsesService(
        db, SimpleNamespace(id=7, username="tester", role="user"),
        surface="user", session_key="long-session",
    )
    with pytest.raises(Exception, match="不属于当前对话"):
        await service.start(
            [{"role": "user", "content": "续问"}], run_id="new-image-run",
            server_history_transcript=[{"role": "user", "content": [
                {"type": "input_image", "image_url": f"prism-asset://{digest}"},
            ]}],
        )
