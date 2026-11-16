"""Non-owner control flow must never become a durable ordinary tool failure."""

import copy

import pytest

from app.core.ownership_signals import NonOwnerAbort
from app.services.deepseek_responses_runtime import DeepSeekResponsesRuntime, InMemoryCheckpointStore


def tool_response():
    return {"id": "response", "status": "completed", "output": [
        {"type": "function_call", "call_id": "first", "name": "search", "arguments": "{}"},
        {"type": "function_call", "call_id": "second", "name": "fetch", "arguments": "{}"},
    ]}


class ScriptedTransport:
    def __init__(self):
        self.calls = 0

    async def create_response(self, _payload):
        self.calls += 1
        return tool_response()


@pytest.mark.asyncio
async def test_non_owner_tool_signal_does_not_save_result_or_continue():
    store = InMemoryCheckpointStore()
    transport = ScriptedTransport()
    captured = {}

    class Executor:
        async def execute(self, _call, *, approved=False):
            captured["before"] = copy.deepcopy((await store.load("run")).to_dict())
            raise NonOwnerAbort()

    machine = DeepSeekResponsesRuntime(transport=transport, tool_executor=Executor(), checkpoint_store=store)
    with pytest.raises(NonOwnerAbort):
        await machine.start("request", run_id="run")
    assert (await store.load("run")).to_dict() == captured["before"]
    assert transport.calls == 1


@pytest.mark.asyncio
async def test_non_owner_transport_signal_does_not_become_failed_checkpoint():
    store = InMemoryCheckpointStore()
    captured = {}

    class Transport:
        async def create_response(self, _payload):
            captured["before"] = copy.deepcopy((await store.load("run")).to_dict())
            raise NonOwnerAbort()

    machine = DeepSeekResponsesRuntime(transport=Transport(), tool_executor=object(), checkpoint_store=store)
    with pytest.raises(NonOwnerAbort):
        await machine.start("request", run_id="run")
    assert (await store.load("run")).to_dict() == captured["before"]


@pytest.mark.asyncio
async def test_non_owner_during_cancel_poll_does_not_mark_inflight_model_unknown(monkeypatch):
    import asyncio

    from app.services import billing_runtime_gate as billing_runtime
    from app.services.agent_responses_service import NativeResponsesTransport
    from app.utils.api_resolver import ApiConfig

    started = asyncio.Event()
    unknown = []
    rounds = []
    ticket = {"key": "held-by-owner", "done": False}
    monkeypatch.setattr(billing_runtime, "begin_attempt", lambda **_kw: ticket)
    monkeypatch.setattr(billing_runtime, "before_stream_send", lambda _ticket: None)
    monkeypatch.setattr(billing_runtime, "unknown_attempt", lambda _ticket: unknown.append(_ticket))

    async def never_finishes(_self, _payload):
        started.set()
        await asyncio.Event().wait()
        yield  # pragma: no cover - async generator

    monkeypatch.setattr(NativeResponsesTransport, "_stream_without_billing", never_finishes)

    class LosingStore(InMemoryCheckpointStore):
        async def load(self, run_id):
            if started.is_set():
                raise NonOwnerAbort()
            return await super().load(run_id)

    transport = NativeResponsesTransport(ApiConfig(api_key="unit", base_url="https://api.deepseek.com/v1",
                                                    model="deepseek-flash", provider="deepseek", source="system"))
    machine = DeepSeekResponsesRuntime(transport=transport, tool_executor=object(),
                                       checkpoint_store=LosingStore(), on_round=lambda value: rounds.append(value))
    with pytest.raises(NonOwnerAbort):
        await machine.start("request", run_id="run-poll-loss")
    assert started.is_set()
    assert unknown == []
    assert rounds == []


@pytest.mark.asyncio
async def test_non_owner_model_signal_wins_simultaneous_cancel_result():
    import asyncio

    from app.services.deepseek_responses_runtime import CANCELLED, RuntimeResult

    class Transport:
        async def create_response(self, _payload):
            raise NonOwnerAbort()

    class Machine(DeepSeekResponsesRuntime):
        async def _refresh_cancelled_checkpoint(self, checkpoint):
            if "_wait_for_cancel" in asyncio.current_task().get_coro().__qualname__:
                return RuntimeResult(run_id=checkpoint.run_id, status=CANCELLED)
            return None

    machine = Machine(transport=Transport(), tool_executor=object(), checkpoint_store=InMemoryCheckpointStore())
    with pytest.raises(NonOwnerAbort):
        await machine.start("request", run_id="same-tick")


@pytest.mark.asyncio
async def test_parent_cancel_racing_poll_non_owner_does_not_mark_unknown(monkeypatch):
    import asyncio

    from app.services import billing_runtime_gate as billing_runtime
    from app.services.agent_responses_service import NativeResponsesTransport
    from app.utils.api_resolver import ApiConfig

    owner_task = asyncio.current_task()
    started = asyncio.Event()
    unknown = []
    ticket = {"key": "held-by-owner", "done": False}
    monkeypatch.setattr(billing_runtime, "begin_attempt", lambda **_kw: ticket)
    monkeypatch.setattr(billing_runtime, "before_stream_send", lambda _ticket: None)
    monkeypatch.setattr(billing_runtime, "unknown_attempt", lambda _ticket: unknown.append(_ticket))

    async def never_finishes(_self, _payload):
        started.set()
        await asyncio.Event().wait()
        yield  # pragma: no cover - async generator

    monkeypatch.setattr(NativeResponsesTransport, "_stream_without_billing", never_finishes)

    class Machine(DeepSeekResponsesRuntime):
        async def _refresh_cancelled_checkpoint(self, checkpoint):
            task = asyncio.current_task()
            if "_wait_for_cancel" in task.get_coro().__qualname__:
                await started.wait()
                owner_task.cancel()
                raise NonOwnerAbort()
            return None

    transport = NativeResponsesTransport(ApiConfig(api_key="unit", base_url="https://api.deepseek.com/v1",
                                                    model="deepseek-flash", provider="deepseek", source="system"))
    machine = Machine(transport=transport, tool_executor=object(), checkpoint_store=InMemoryCheckpointStore())
    try:
        with pytest.raises(NonOwnerAbort):
            await machine.start("request", run_id="cancel-and-nonowner")
    finally:
        owner_task.uncancel()
    assert unknown == []


@pytest.mark.asyncio
async def test_streamed_partial_response_non_owner_signal_is_not_wrapped():
    store = InMemoryCheckpointStore()
    captured = {}

    class Transport:
        async def create_response(self, _payload):
            async def events():
                yield {"type": "response.output_text.delta", "delta": "partial"}
                captured["before"] = copy.deepcopy((await store.load("partial")).to_dict())
                raise NonOwnerAbort()
            return events()

    machine = DeepSeekResponsesRuntime(transport=Transport(), tool_executor=object(), checkpoint_store=store)
    with pytest.raises(NonOwnerAbort):
        await machine.start("request", run_id="partial")
    assert (await store.load("partial")).to_dict() == captured["before"]


@pytest.mark.asyncio
async def test_normal_model_cancellation_still_marks_inflight_hold_unknown(monkeypatch):
    import asyncio

    from app.services import billing_runtime_gate as billing_runtime
    from app.services.agent_responses_service import NativeResponsesTransport
    from app.utils.api_resolver import ApiConfig

    started = asyncio.Event()
    unknown = []
    ticket = {"key": "normal-cancel", "done": False}
    monkeypatch.setattr(billing_runtime, "begin_attempt", lambda **_kw: ticket)
    monkeypatch.setattr(billing_runtime, "before_stream_send", lambda _ticket: None)
    monkeypatch.setattr(billing_runtime, "unknown_attempt", lambda _ticket: unknown.append(_ticket))

    async def never_finishes(_self, _payload):
        started.set()
        await asyncio.Event().wait()
        yield  # pragma: no cover

    monkeypatch.setattr(NativeResponsesTransport, "_stream_without_billing", never_finishes)
    transport = NativeResponsesTransport(ApiConfig(api_key="unit", base_url="https://api.deepseek.com/v1",
                                                    model="deepseek-flash", provider="deepseek", source="system"))
    machine = DeepSeekResponsesRuntime(transport=transport, tool_executor=object(),
                                       checkpoint_store=InMemoryCheckpointStore())
    task = asyncio.create_task(machine.start("request", run_id="normal-cancel"))
    await asyncio.wait_for(started.wait(), timeout=2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert unknown == [ticket]


@pytest.mark.asyncio
@pytest.mark.parametrize("signal_in", ["model_cleanup", "poll_cleanup"])
async def test_non_owner_during_sibling_cleanup_is_not_discarded(signal_in):
    import asyncio

    from app.services.deepseek_responses_runtime import CANCELLED, RuntimeResult

    started = asyncio.Event()

    class Transport:
        async def create_response(self, _payload):
            started.set()
            if signal_in == "model_cleanup":
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    raise NonOwnerAbort() from None
            return {"id": "done", "status": "completed", "output": []}

    class Machine(DeepSeekResponsesRuntime):
        async def _refresh_cancelled_checkpoint(self, checkpoint):
            task = asyncio.current_task()
            if "_wait_for_cancel" not in task.get_coro().__qualname__:
                return None
            await started.wait()
            if signal_in == "model_cleanup":
                return RuntimeResult(run_id=checkpoint.run_id, status=CANCELLED)
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                raise NonOwnerAbort() from None

    machine = Machine(transport=Transport(), tool_executor=object(), checkpoint_store=InMemoryCheckpointStore())
    with pytest.raises(NonOwnerAbort):
        await machine.start("request", run_id=f"cleanup-{signal_in}")


@pytest.mark.asyncio
async def test_parent_cancel_stops_model_before_any_more_sends():
    import asyncio

    ready, release = asyncio.Event(), asyncio.Event()
    sent = []

    class Transport:
        async def create_response(self, _payload):
            ready.set()
            await release.wait()
            sent.append('provider_send')
            await asyncio.Event().wait()

    machine = DeepSeekResponsesRuntime(transport=Transport(), tool_executor=object(),
                                       checkpoint_store=InMemoryCheckpointStore())
    task = asyncio.create_task(machine.start('request', run_id='cancel-before-send'))
    await asyncio.wait_for(ready.wait(), timeout=2)
    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert sent == []


@pytest.mark.asyncio
@pytest.mark.xfail(strict=True, reason='激活阻断：轮询取消清理中迟到失权，账本写入尚未按 owner 事务围栏校验')
async def test_late_non_owner_during_cancel_cleanup_must_not_write_unknown(monkeypatch):
    import asyncio

    from app.services import billing_runtime_gate as billing_runtime
    from app.services.agent_responses_service import NativeResponsesTransport
    from app.utils.api_resolver import ApiConfig

    started, polling = asyncio.Event(), asyncio.Event()
    unknown = []
    ticket = {'key': 'held-by-owner', 'done': False}
    monkeypatch.setattr(billing_runtime, 'begin_attempt', lambda **_kw: ticket)
    monkeypatch.setattr(billing_runtime, 'before_stream_send', lambda _ticket: None)
    monkeypatch.setattr(billing_runtime, 'unknown_attempt', lambda value: unknown.append(value))

    async def never_finishes(_self, _payload):
        started.set()
        await asyncio.Event().wait()
        yield  # pragma: no cover

    monkeypatch.setattr(NativeResponsesTransport, '_stream_without_billing', never_finishes)

    class Machine(DeepSeekResponsesRuntime):
        async def _refresh_cancelled_checkpoint(self, checkpoint):
            if '_wait_for_cancel' not in asyncio.current_task().get_coro().__qualname__:
                return None
            polling.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                raise NonOwnerAbort() from None

    transport = NativeResponsesTransport(ApiConfig(api_key='unit', base_url='https://api.deepseek.com/v1',
                                                    model='deepseek-flash', provider='deepseek', source='system'))
    machine = Machine(transport=transport, tool_executor=object(), checkpoint_store=InMemoryCheckpointStore())
    task = asyncio.create_task(machine.start('request', run_id='late-owner'))
    await asyncio.wait_for(asyncio.gather(started.wait(), polling.wait()), timeout=2)
    task.cancel()
    with pytest.raises(NonOwnerAbort):
        await task
    assert unknown == []
