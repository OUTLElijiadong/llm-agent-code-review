"""DeepSeek Responses API 的无状态 Agent 工具循环内核。

该模块只负责协议状态机，不依赖 FastAPI、SQLAlchemy 或具体 Agent 实现：

* 每轮把历史 ``output`` 项和 ``function_call_output`` 原样回放到 ``input``；
* 工具执行器、模型 transport 与检查点存储全部由调用方注入；
* 审批与模型动态追问会暂停，并从同一个工具调用检查点续跑；
* ``InMemoryCheckpointStore`` 仅供单元测试和本地开发，生产必须注入持久化实现。
"""
from __future__ import annotations

import asyncio
import copy
import hashlib
import inspect
import json
import math
import uuid
from dataclasses import dataclass, field
from typing import (
    Any,
    AsyncIterable,
    Dict,
    List,
    Mapping,
    Optional,
    Protocol,
    Sequence,
    Tuple,
    Union,
)

RUNNING = "running"
WAITING_APPROVAL = "waiting_approval"
WAITING_INPUT = "waiting_input"
APPROVING = "approving"
REJECTING = "rejecting"
ANSWERING = "answering"
COMPLETED = "completed"
INCOMPLETE = "incomplete"
FAILED = "failed"
MAX_ROUNDS_EXCEEDED = "max_rounds_exceeded"

DEFAULT_CONTEXT_WINDOW_TOKENS = 1_000_000
DEFAULT_MAX_OUTPUT_TOKENS = 32_768
DEFAULT_COMPACTION_THRESHOLD_TOKENS = 850_000
DEFAULT_KEEP_RECENT_TOKENS = 200_000
COMPACTION_STRATEGY_VERSION = "agent-transcript-v1"


class RunNotFoundError(LookupError):
    """请求续跑的检查点不存在。"""


class InvalidRunStateError(RuntimeError):
    """当前运行状态不允许所请求的恢复动作。"""


class IncompleteResponseStreamError(RuntimeError):
    """Responses 事件流在明确终止帧之前意外结束。"""


class ContextBudgetError(RuntimeError):
    """Agent 上下文无法在不破坏协议完整性的前提下压缩到预算内。"""


@dataclass(frozen=True)
class ToolCall:
    """模型发出的单个函数工具调用。"""

    call_id: str
    name: str
    arguments: Dict[str, Any]
    raw_arguments: str
    parse_error: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "call_id": self.call_id,
            "name": self.name,
            "arguments": copy.deepcopy(self.arguments),
            "raw_arguments": self.raw_arguments,
            "parse_error": self.parse_error,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ToolCall":
        return cls(
            call_id=str(value.get("call_id") or ""),
            name=str(value.get("name") or ""),
            arguments=copy.deepcopy(dict(value.get("arguments") or {})),
            raw_arguments=str(value.get("raw_arguments") or "{}"),
            parse_error=str(value.get("parse_error") or ""),
        )


@dataclass(frozen=True)
class ToolExecutionResult:
    """工具执行器返回的标准结果。"""

    status: str
    output: Any = None
    error: str = ""
    operation: str = ""
    impact: str = ""
    danger: bool = False
    approval_id: Optional[Union[int, str]] = None
    preview: Any = None

    @classmethod
    def success(cls, output: Any) -> "ToolExecutionResult":
        return cls(status="success", output=output)

    @classmethod
    def failure(cls, error: str) -> "ToolExecutionResult":
        return cls(status="error", error=error)

    @classmethod
    def approval_required(
        cls,
        *,
        operation: str,
        impact: str,
        danger: bool,
        approval_id: Optional[Union[int, str]] = None,
        preview: Any = None,
    ) -> "ToolExecutionResult":
        return cls(
            status="approval_required",
            operation=operation,
            impact=impact,
            danger=danger,
            approval_id=approval_id,
            preview=copy.deepcopy(preview),
        )


@dataclass
class PendingAction:
    """审批或追问暂停点，以及同一模型响应中尚未执行的调用。"""

    kind: str
    call: ToolCall
    remaining_calls: List[ToolCall] = field(default_factory=list)
    operation: str = ""
    impact: str = ""
    danger: bool = False
    approval_id: Optional[Union[int, str]] = None
    preview: Any = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "kind": self.kind,
            "call": self.call.to_dict(),
            "remaining_calls": [item.to_dict() for item in self.remaining_calls],
            "operation": self.operation,
            "impact": self.impact,
            "danger": self.danger,
            "approval_id": self.approval_id,
            "preview": copy.deepcopy(self.preview),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "PendingAction":
        return cls(
            kind=str(value.get("kind") or ""),
            call=ToolCall.from_dict(dict(value.get("call") or {})),
            remaining_calls=[
                ToolCall.from_dict(dict(item)) for item in list(value.get("remaining_calls") or [])
            ],
            operation=str(value.get("operation") or ""),
            impact=str(value.get("impact") or ""),
            danger=bool(value.get("danger", False)),
            approval_id=value.get("approval_id"),
            preview=copy.deepcopy(value.get("preview")),
        )


@dataclass
class RunCheckpoint:
    """可被数据库/Redis 适配器序列化的完整运行检查点。"""

    run_id: str
    model: str
    transcript: List[Dict[str, Any]]
    tools: List[Dict[str, Any]]
    instructions: str = ""
    status: str = RUNNING
    rounds: int = 0
    pending: Optional[PendingAction] = None
    output_text: str = ""
    last_response: Dict[str, Any] = field(default_factory=dict)
    context_metadata: Dict[str, Any] = field(default_factory=dict)
    error: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "run_id": self.run_id,
            "model": self.model,
            "transcript": copy.deepcopy(self.transcript),
            "tools": copy.deepcopy(self.tools),
            "instructions": self.instructions,
            "status": self.status,
            "rounds": self.rounds,
            "pending": self.pending.to_dict() if self.pending else None,
            "output_text": self.output_text,
            "last_response": copy.deepcopy(self.last_response),
            "context_metadata": copy.deepcopy(self.context_metadata),
            "error": self.error,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "RunCheckpoint":
        pending = value.get("pending")
        return cls(
            run_id=str(value.get("run_id") or ""),
            model=str(value.get("model") or ""),
            transcript=copy.deepcopy(list(value.get("transcript") or [])),
            tools=copy.deepcopy(list(value.get("tools") or [])),
            instructions=str(value.get("instructions") or ""),
            status=str(value.get("status") or RUNNING),
            rounds=int(value.get("rounds") or 0),
            pending=PendingAction.from_dict(dict(pending)) if pending else None,
            output_text=str(value.get("output_text") or ""),
            last_response=copy.deepcopy(dict(value.get("last_response") or {})),
            context_metadata=copy.deepcopy(dict(value.get("context_metadata") or {})),
            error=str(value.get("error") or ""),
        )


@dataclass(frozen=True)
class RuntimeResult:
    """一次启动或恢复动作的可序列化结果。"""

    run_id: str
    status: str
    output_text: str = ""
    response: Mapping[str, Any] = field(default_factory=dict)
    pending: Optional[Mapping[str, Any]] = None
    error: str = ""
    rounds: int = 0
    events: Tuple[Mapping[str, Any], ...] = ()


TransportOutput = Union[
    Mapping[str, Any],
    AsyncIterable[Mapping[str, Any]],
    Sequence[Mapping[str, Any]],
]


class ResponsesTransport(Protocol):
    """DeepSeek Responses transport；适配器可返回完整响应或事件流。"""

    async def create_response(self, payload: Mapping[str, Any]) -> TransportOutput:
        """提交单轮无状态 Responses 请求。"""


class ToolExecutor(Protocol):
    """平台工具执行器；``approved=True`` 表示用户已批准同一个调用。

    生产实现必须以 ``ToolCall.call_id`` 作为幂等键。检查点会阻止并发重复批准，
    但进程可能在副作用完成后、结果保存前崩溃，恢复时仍需执行器返回同一结果。
    """

    async def execute(self, call: ToolCall, *, approved: bool = False) -> ToolExecutionResult:
        """执行或评估工具调用。"""

    async def reject(self, call: ToolCall, *, reason: str) -> None:
        """在运行时原子取得拒绝权后，持久化同一调用的拒绝决定。"""


class CheckpointStore(Protocol):
    """运行检查点持久化端口；生产 ``create/claim`` 必须跨进程原子。"""

    async def create(self, checkpoint: RunCheckpoint) -> bool:
        """仅当运行 ID 不存在时原子创建检查点。"""

    async def save(self, checkpoint: RunCheckpoint) -> None:
        """原子保存完整检查点。"""

    async def load(self, run_id: str) -> Optional[RunCheckpoint]:
        """按运行 ID 读取检查点。"""

    async def delete(self, run_id: str) -> None:
        """删除检查点。"""

    async def claim(
        self,
        run_id: str,
        *,
        expected_status: str,
        claimed_status: str,
        tool_call_id: Optional[str] = None,
    ) -> Optional[RunCheckpoint]:
        """以状态 CAS 原子取得恢复动作的唯一执行权。"""


class InMemoryCheckpointStore:
    """进程内检查点存储，仅用于单元测试和本地开发。"""

    def __init__(self) -> None:
        self._items: Dict[str, Dict[str, Any]] = {}
        self._lock = asyncio.Lock()

    async def create(self, checkpoint: RunCheckpoint) -> bool:
        async with self._lock:
            if checkpoint.run_id in self._items:
                return False
            self._items[checkpoint.run_id] = checkpoint.to_dict()
            return True

    async def save(self, checkpoint: RunCheckpoint) -> None:
        async with self._lock:
            self._items[checkpoint.run_id] = checkpoint.to_dict()

    async def load(self, run_id: str) -> Optional[RunCheckpoint]:
        async with self._lock:
            value = self._items.get(run_id)
            return RunCheckpoint.from_dict(copy.deepcopy(value)) if value else None

    async def delete(self, run_id: str) -> None:
        async with self._lock:
            self._items.pop(run_id, None)

    async def claim(
        self,
        run_id: str,
        *,
        expected_status: str,
        claimed_status: str,
        tool_call_id: Optional[str] = None,
    ) -> Optional[RunCheckpoint]:
        async with self._lock:
            value = self._items.get(run_id)
            if value is None or value.get("status") != expected_status:
                return None
            pending = value.get("pending") or {}
            call = pending.get("call") or {}
            if tool_call_id and call.get("call_id") != tool_call_id:
                return None
            claimed = copy.deepcopy(value)
            claimed["status"] = claimed_status
            self._items[run_id] = claimed
            return RunCheckpoint.from_dict(copy.deepcopy(claimed))


class DeepSeekResponsesRuntime:
    """可暂停、可恢复的 Responses Agent 工具循环。"""

    def __init__(
        self,
        *,
        transport: ResponsesTransport,
        tool_executor: ToolExecutor,
        checkpoint_store: CheckpointStore,
        model: str = "deepseek-v4-flash",
        max_rounds: int = 12,
        stream: bool = True,
        context_window_tokens: int = DEFAULT_CONTEXT_WINDOW_TOKENS,
        max_output_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS,
        compaction_threshold_tokens: int = DEFAULT_COMPACTION_THRESHOLD_TOKENS,
        keep_recent_tokens: int = DEFAULT_KEEP_RECENT_TOKENS,
    ) -> None:
        if max_rounds < 1:
            raise ValueError("max_rounds 必须大于 0")
        _validate_context_budget(
            context_window_tokens=context_window_tokens,
            max_output_tokens=max_output_tokens,
            compaction_threshold_tokens=compaction_threshold_tokens,
            keep_recent_tokens=keep_recent_tokens,
        )
        self._transport = transport
        self._tool_executor = tool_executor
        self._store = checkpoint_store
        self._model = model
        self._max_rounds = max_rounds
        self._stream = stream
        self._context_window_tokens = context_window_tokens
        self._max_output_tokens = max_output_tokens
        self._compaction_threshold_tokens = compaction_threshold_tokens
        self._keep_recent_tokens = keep_recent_tokens
        self._locks: Dict[str, asyncio.Lock] = {}

    async def start(
        self,
        input_value: Union[str, Sequence[Mapping[str, Any]]],
        *,
        instructions: str = "",
        tools: Optional[Sequence[Mapping[str, Any]]] = None,
        run_id: Optional[str] = None,
    ) -> RuntimeResult:
        """启动新运行并持续执行，直到完成、暂停或达到轮数上限。"""
        actual_run_id = run_id or f"run_{uuid.uuid4().hex}"
        lock = self._locks.setdefault(actual_run_id, asyncio.Lock())
        async with lock:
            checkpoint = RunCheckpoint(
                run_id=actual_run_id,
                model=self._model,
                transcript=_normalize_input(input_value),
                tools=_with_ask_user_tool(tools or []),
                instructions=instructions,
            )
            if not await self._store.create(checkpoint):
                raise InvalidRunStateError(
                    f"运行 {actual_run_id} 无法创建：标识已存在或当前会话仍有待处理运行"
                )
            return await self._drive(checkpoint)

    async def approve(self, run_id: str, tool_call_id: Optional[str] = None) -> RuntimeResult:
        """批准暂停的精确调用，执行后自动继续模型工具循环。"""
        lock = self._locks.setdefault(run_id, asyncio.Lock())
        async with lock:
            checkpoint = await self._claim_pending(
                run_id,
                expected_status=WAITING_APPROVAL,
                claimed_status=APPROVING,
                tool_call_id=tool_call_id,
            )
            pending = checkpoint.pending
            assert pending is not None
            execution = await self._execute_tool(pending.call, approved=True)
            if _is_approval_required(execution.status):
                checkpoint.error = "工具在已批准后仍要求审批，已停止以避免重复审批循环"
                checkpoint.status = FAILED
                await self._store.save(checkpoint)
                return self._result(checkpoint)
            checkpoint.pending = None
            checkpoint.status = RUNNING
            checkpoint.transcript.append(_tool_output_item(pending.call, execution))
            await self._store.save(checkpoint)
            paused = await self._process_calls(checkpoint, pending.remaining_calls)
            return paused or await self._drive(checkpoint)

    async def reject(
        self,
        run_id: str,
        tool_call_id: Optional[str] = None,
        *,
        reason: str = "用户拒绝执行该操作",
    ) -> RuntimeResult:
        """拒绝精确调用，并把拒绝结果回灌模型继续判断。"""
        lock = self._locks.setdefault(run_id, asyncio.Lock())
        async with lock:
            checkpoint = await self._claim_pending(
                run_id,
                expected_status=WAITING_APPROVAL,
                claimed_status=REJECTING,
                tool_call_id=tool_call_id,
            )
            pending = checkpoint.pending
            assert pending is not None
            try:
                await _invoke_tool_rejection(
                    self._tool_executor,
                    pending.call,
                    reason=reason,
                )
            except Exception as exc:  # noqa: BLE001 - 审批记录失败时禁止继续模型链
                checkpoint.status = FAILED
                checkpoint.error = f"工具拒绝决定持久化失败: {exc}"
                await self._store.save(checkpoint)
                return self._result(checkpoint)
            checkpoint.pending = None
            checkpoint.status = RUNNING
            checkpoint.transcript.append(
                _function_call_output(
                    pending.call.call_id,
                    {"status": "rejected", "error": reason},
                )
            )
            await self._store.save(checkpoint)
            paused = await self._process_calls(checkpoint, pending.remaining_calls)
            return paused or await self._drive(checkpoint)

    async def answer(
        self,
        run_id: str,
        text: str,
        tool_call_id: Optional[str] = None,
    ) -> RuntimeResult:
        """回答模型动态生成的问题，并从该 ``ask_user`` 调用自动续跑。"""
        lock = self._locks.setdefault(run_id, asyncio.Lock())
        async with lock:
            checkpoint = await self._claim_pending(
                run_id,
                expected_status=WAITING_INPUT,
                claimed_status=ANSWERING,
                tool_call_id=tool_call_id,
            )
            pending = checkpoint.pending
            assert pending is not None
            options = _normalize_ask_user_options(pending.call.arguments.get("options"))
            allow_free_text = bool(pending.call.arguments.get("allow_free_text", True))
            if options and not allow_free_text:
                accepted = {
                    value
                    for item in options
                    for value in (item["value"], item["label"])
                }
                if text not in accepted:
                    checkpoint.status = WAITING_INPUT
                    await self._store.save(checkpoint)
                    raise InvalidRunStateError("该追问仅接受界面中列出的选项")
            checkpoint.pending = None
            checkpoint.status = RUNNING
            checkpoint.transcript.append(
                _function_call_output(
                    pending.call.call_id,
                    {"status": "answered", "answer": text},
                )
            )
            await self._store.save(checkpoint)
            paused = await self._process_calls(checkpoint, pending.remaining_calls)
            return paused or await self._drive(checkpoint)

    async def get_checkpoint(self, run_id: str) -> RunCheckpoint:
        """读取检查点副本，供 API 适配器展示状态。"""
        return await self._require_checkpoint(run_id)

    async def _drive(self, checkpoint: RunCheckpoint) -> RuntimeResult:
        events: List[Mapping[str, Any]] = []
        while True:
            if checkpoint.rounds >= self._max_rounds:
                checkpoint.status = MAX_ROUNDS_EXCEEDED
                checkpoint.error = f"模型工具循环超过最大轮数 {self._max_rounds}"
                await self._store.save(checkpoint)
                return self._result(checkpoint, events=events)

            overhead_tokens = estimate_tokens(
                {
                    "instructions": checkpoint.instructions,
                    "tools": checkpoint.tools,
                    "tool_choice": "auto",
                }
            )
            try:
                projected_input, context_metadata = compact_transcript(
                    checkpoint.transcript,
                    context_window_tokens=self._context_window_tokens,
                    max_output_tokens=self._max_output_tokens,
                    compaction_threshold_tokens=self._compaction_threshold_tokens,
                    keep_recent_tokens=self._keep_recent_tokens,
                    overhead_tokens=overhead_tokens,
                )
            except ContextBudgetError as exc:
                checkpoint.status = FAILED
                checkpoint.error = f"Responses 上下文超出安全预算，已拒绝发送: {exc}"
                await self._store.save(checkpoint)
                return self._result(checkpoint, events=events)

            previous_compactions = int(checkpoint.context_metadata.get("compaction_count") or 0)
            if context_metadata["compacted"]:
                context_metadata["compaction_count"] = previous_compactions + 1
            else:
                context_metadata["compaction_count"] = previous_compactions
            checkpoint.context_metadata = context_metadata

            payload: Dict[str, Any] = {
                "model": checkpoint.model,
                "input": projected_input,
                "tools": copy.deepcopy(checkpoint.tools),
                "tool_choice": "auto",
                "stream": self._stream,
                "max_output_tokens": self._max_output_tokens,
            }
            if checkpoint.instructions:
                payload["instructions"] = checkpoint.instructions

            checkpoint.rounds += 1
            await self._store.save(checkpoint)
            try:
                transport_output = await _invoke_transport(self._transport, payload)
                response, turn_events = await _collect_response(transport_output)
            except Exception as exc:  # noqa: BLE001 - transport 错误转为可恢复检查点
                checkpoint.status = FAILED
                checkpoint.error = f"Responses transport 调用失败: {exc}"
                await self._store.save(checkpoint)
                return self._result(checkpoint, events=events)

            events.extend(turn_events)
            checkpoint.last_response = copy.deepcopy(dict(response))
            output_items = [
                copy.deepcopy(dict(item))
                for item in list(response.get("output") or [])
                if isinstance(item, Mapping)
            ]
            checkpoint.transcript.extend(copy.deepcopy(output_items))
            await self._store.save(checkpoint)

            upstream_status = str(response.get("status") or "")
            if upstream_status != COMPLETED:
                checkpoint.output_text = _extract_output_text(output_items)
                if upstream_status == FAILED:
                    checkpoint.status = FAILED
                    error_value = response.get("error")
                    checkpoint.error = _stringify_output(error_value) if error_value else "模型响应失败"
                elif upstream_status == INCOMPLETE:
                    checkpoint.status = INCOMPLETE
                    checkpoint.error = "模型响应未完整结束，未执行其中的工具调用"
                else:
                    checkpoint.status = FAILED
                    checkpoint.error = "模型响应缺少明确的 completed 终态，未执行其中的工具调用"
                await self._store.save(checkpoint)
                return self._result(checkpoint, events=events)

            calls = _extract_tool_calls(output_items)
            if calls:
                paused = await self._process_calls(checkpoint, calls)
                if paused:
                    return self._result(checkpoint, events=events + list(paused.events))
                continue

            checkpoint.output_text = _extract_output_text(output_items)
            checkpoint.status = COMPLETED
            await self._store.save(checkpoint)
            return self._result(checkpoint, events=events)

    async def _process_calls(
        self,
        checkpoint: RunCheckpoint,
        calls: Sequence[ToolCall],
    ) -> Optional[RuntimeResult]:
        for index, call in enumerate(calls):
            remaining = list(calls[index + 1 :])
            if call.parse_error:
                checkpoint.transcript.append(
                    _function_call_output(
                        call.call_id,
                        {"status": "error", "error": call.parse_error},
                    )
                )
                await self._store.save(checkpoint)
                continue

            if call.name == "ask_user":
                question = str(call.arguments.get("question") or "").strip()
                options = _normalize_ask_user_options(call.arguments.get("options"))
                allow_free_text = bool(call.arguments.get("allow_free_text", True))
                if not question:
                    checkpoint.transcript.append(
                        _function_call_output(
                            call.call_id,
                            {"status": "error", "error": "ask_user 缺少非空 question"},
                        )
                    )
                    await self._store.save(checkpoint)
                    continue
                if not options and not allow_free_text:
                    checkpoint.transcript.append(
                        _function_call_output(
                            call.call_id,
                            {
                                "status": "error",
                                "error": "ask_user 禁止自由输入时必须提供至少一个有效选项",
                            },
                        )
                    )
                    await self._store.save(checkpoint)
                    continue
                checkpoint.status = WAITING_INPUT
                checkpoint.pending = PendingAction(
                    kind="input",
                    call=call,
                    remaining_calls=remaining,
                    operation="ask_user",
                    impact=str(call.arguments.get("context") or ""),
                    danger=False,
                )
                await self._store.save(checkpoint)
                event = {
                    "type": "response.input.required",
                    "run_id": checkpoint.run_id,
                    "tool_call_id": call.call_id,
                    "name": call.name,
                    "arguments": copy.deepcopy(call.arguments),
                    "question": question,
                    "options": options,
                    "allow_free_text": allow_free_text,
                }
                return self._result(checkpoint, events=[event])

            execution = await self._execute_tool(call, approved=False)
            if _is_approval_required(execution.status):
                checkpoint.status = WAITING_APPROVAL
                checkpoint.pending = PendingAction(
                    kind="approval",
                    call=call,
                    remaining_calls=remaining,
                    operation=execution.operation or call.name,
                    impact=execution.impact,
                    danger=execution.danger,
                    approval_id=execution.approval_id,
                    preview=copy.deepcopy(execution.preview),
                )
                await self._store.save(checkpoint)
                event = {
                    "type": "response.approval.required",
                    "run_id": checkpoint.run_id,
                    "tool_call_id": call.call_id,
                    "name": call.name,
                    "arguments": copy.deepcopy(call.arguments),
                    "operation": execution.operation or call.name,
                    "impact": execution.impact,
                    "danger": execution.danger,
                    "approval_id": execution.approval_id,
                    "preview": copy.deepcopy(execution.preview),
                }
                return self._result(checkpoint, events=[event])

            checkpoint.transcript.append(_tool_output_item(call, execution))
            await self._store.save(checkpoint)
        return None

    async def _execute_tool(self, call: ToolCall, *, approved: bool) -> ToolExecutionResult:
        try:
            raw_result = await _invoke_tool_executor(self._tool_executor, call, approved=approved)
            return _normalize_execution_result(raw_result)
        except Exception as exc:  # noqa: BLE001 - 工具异常必须回灌模型继续推理
            return ToolExecutionResult.failure(f"工具 {call.name} 执行失败: {exc}")

    async def _require_checkpoint(self, run_id: str) -> RunCheckpoint:
        checkpoint = await self._store.load(run_id)
        if checkpoint is None:
            raise RunNotFoundError(f"运行 {run_id} 不存在或检查点已失效")
        return checkpoint

    async def _claim_pending(
        self,
        run_id: str,
        *,
        expected_status: str,
        claimed_status: str,
        tool_call_id: Optional[str],
    ) -> RunCheckpoint:
        checkpoint = await self._store.claim(
            run_id,
            expected_status=expected_status,
            claimed_status=claimed_status,
            tool_call_id=tool_call_id,
        )
        if checkpoint is not None and checkpoint.pending is not None:
            return checkpoint
        current = await self._store.load(run_id)
        if current is None:
            raise RunNotFoundError(f"运行 {run_id} 不存在或检查点已失效")
        if (
            tool_call_id
            and current.pending is not None
            and current.status == expected_status
            and current.pending.call.call_id != tool_call_id
        ):
            raise InvalidRunStateError(
                f"tool_call_id 不匹配：当前等待 {current.pending.call.call_id}"
            )
        raise InvalidRunStateError(
            f"运行 {run_id} 当前状态为 {current.status}，恢复动作已被处理或不能执行"
        )

    @staticmethod
    def _require_pending(
        checkpoint: RunCheckpoint,
        expected_status: str,
        tool_call_id: Optional[str],
    ) -> PendingAction:
        if checkpoint.status != expected_status or checkpoint.pending is None:
            raise InvalidRunStateError(
                f"运行 {checkpoint.run_id} 当前状态为 {checkpoint.status}，不能执行该恢复动作"
            )
        if tool_call_id and checkpoint.pending.call.call_id != tool_call_id:
            raise InvalidRunStateError(
                f"tool_call_id 不匹配：当前等待 {checkpoint.pending.call.call_id}"
            )
        return checkpoint.pending

    @staticmethod
    def _result(
        checkpoint: RunCheckpoint,
        *,
        events: Sequence[Mapping[str, Any]] = (),
    ) -> RuntimeResult:
        pending = None
        if checkpoint.pending:
            pending = {
                "kind": checkpoint.pending.kind,
                "tool_call_id": checkpoint.pending.call.call_id,
                "name": checkpoint.pending.call.name,
                "arguments": copy.deepcopy(checkpoint.pending.call.arguments),
                "operation": checkpoint.pending.operation,
                "impact": checkpoint.pending.impact,
                "danger": checkpoint.pending.danger,
                "approval_id": checkpoint.pending.approval_id,
            }
            if checkpoint.pending.preview is not None:
                pending["preview"] = copy.deepcopy(checkpoint.pending.preview)
        return RuntimeResult(
            run_id=checkpoint.run_id,
            status=checkpoint.status,
            output_text=checkpoint.output_text,
            response=copy.deepcopy(checkpoint.last_response),
            pending=pending,
            error=checkpoint.error,
            rounds=checkpoint.rounds,
            events=tuple(copy.deepcopy(list(events))),
        )


def estimate_tokens(value: Any) -> int:
    """对 Responses JSON 进行保守、确定性的 token 预估。

    上游当前未提供官方本地 tokenizer。ASCII 按每 4 字符一个 token，
    非 ASCII 按每字符 2 个 token 计，留出足够安全余量。
    """
    try:
        text = json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)
    except (TypeError, ValueError):
        text = str(value)
    ascii_count = sum(1 for char in text if ord(char) < 128)
    non_ascii_count = len(text) - ascii_count
    return max(1, math.ceil(ascii_count / 4) + non_ascii_count * 2)


def compact_transcript(
    transcript: Sequence[Mapping[str, Any]],
    *,
    context_window_tokens: int,
    max_output_tokens: int,
    compaction_threshold_tokens: int,
    keep_recent_tokens: int,
    overhead_tokens: int = 0,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """为单次模型请求生成可审计的上下文投影。

    ``transcript`` 不会被修改；压缩时保留首条目标、最近上下文，并确保
    ``function_call`` 与 ``function_call_output`` 以 ``call_id`` 成对保留。
    """
    _validate_context_budget(
        context_window_tokens=context_window_tokens,
        max_output_tokens=max_output_tokens,
        compaction_threshold_tokens=compaction_threshold_tokens,
        keep_recent_tokens=keep_recent_tokens,
    )
    if overhead_tokens < 0:
        raise ValueError("overhead_tokens 不能为负数")

    items = [copy.deepcopy(dict(item)) for item in transcript]
    original_tokens = estimate_tokens(items)
    transcript_budget = context_window_tokens - max_output_tokens - overhead_tokens
    if transcript_budget <= 0:
        raise ContextBudgetError(
            f"系统指令与工具定义已占用 {overhead_tokens} tokens，"
            f"无法从 {context_window_tokens} token 窗口预留 {max_output_tokens} token 输出"
        )

    effective_threshold = min(compaction_threshold_tokens, transcript_budget)
    base_metadata: Dict[str, Any] = {
        "strategy_version": COMPACTION_STRATEGY_VERSION,
        "context_window_tokens": context_window_tokens,
        "max_output_tokens": max_output_tokens,
        "overhead_tokens": overhead_tokens,
        "transcript_budget_tokens": transcript_budget,
        "compaction_threshold_tokens": effective_threshold,
        "original_tokens": original_tokens,
    }
    if original_tokens <= effective_threshold:
        return items, {
            **base_metadata,
            "projected_tokens": original_tokens,
            "compacted": False,
            "omitted_items": 0,
            "summary_sha256": "",
        }

    if not items:
        raise ContextBudgetError("空上下文的预估异常超出预算")

    selected = {0}
    recent_cost = 0
    for index in range(len(items) - 1, 0, -1):
        related = _paired_item_indices(items, index)
        additions = related - selected
        if not additions:
            continue
        addition_cost = sum(estimate_tokens(items[item_index]) for item_index in additions)
        if recent_cost + addition_cost <= keep_recent_tokens:
            selected.update(additions)
            recent_cost += addition_cost

    if len(items) > 1 and selected == {0}:
        latest = _paired_item_indices(items, len(items) - 1)
        if sum(estimate_tokens(items[index]) for index in latest) <= transcript_budget:
            selected.update(latest)

    protected = {0, max(selected)}
    protected.update(_paired_item_indices(items, max(selected)))

    while True:
        projected, metadata = _build_compacted_projection(
            items,
            selected,
            base_metadata=base_metadata,
        )
        if metadata["projected_tokens"] <= transcript_budget:
            return projected, metadata

        removable = sorted(selected - protected)
        if not removable:
            raise ContextBudgetError(
                f"首条目标与最近完整调用预估 {metadata['projected_tokens']} tokens，"
                f"超过可用输入预算 {transcript_budget} tokens"
            )
        selected.difference_update(_paired_item_indices(items, removable[0]))


def _validate_context_budget(
    *,
    context_window_tokens: int,
    max_output_tokens: int,
    compaction_threshold_tokens: int,
    keep_recent_tokens: int,
) -> None:
    if context_window_tokens <= 0:
        raise ValueError("context_window_tokens 必须大于 0")
    if max_output_tokens <= 0 or max_output_tokens >= context_window_tokens:
        raise ValueError("max_output_tokens 必须大于 0 且小于上下文窗口")
    input_budget = context_window_tokens - max_output_tokens
    if compaction_threshold_tokens <= 0 or compaction_threshold_tokens > input_budget:
        raise ValueError("compaction_threshold_tokens 必须在可用输入预算内")
    if keep_recent_tokens <= 0 or keep_recent_tokens > compaction_threshold_tokens:
        raise ValueError("keep_recent_tokens 必须大于 0 且不超过压缩阈值")


def _paired_item_indices(items: Sequence[Mapping[str, Any]], index: int) -> set[int]:
    related = {index}
    item = items[index]
    item_type = str(item.get("type") or "")
    if item_type not in {"function_call", "function_call_output"}:
        return related
    call_id = str(item.get("call_id") or "")
    if not call_id:
        return related
    for candidate_index, candidate in enumerate(items):
        if str(candidate.get("call_id") or "") != call_id:
            continue
        if str(candidate.get("type") or "") in {"function_call", "function_call_output"}:
            related.add(candidate_index)
    return related


def _build_compacted_projection(
    items: Sequence[Mapping[str, Any]],
    selected: set[int],
    *,
    base_metadata: Mapping[str, Any],
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    omitted = [copy.deepcopy(dict(item)) for index, item in enumerate(items) if index not in selected]
    omitted_json = json.dumps(omitted, ensure_ascii=False, separators=(",", ":"), default=str)
    summary_sha256 = hashlib.sha256(omitted_json.encode("utf-8")).hexdigest()
    summary_text = _compaction_summary(omitted, summary_sha256)
    summary_item = {
        "type": "message",
        "role": "system",
        "content": [{"type": "input_text", "text": summary_text}],
    }
    projected = [summary_item] + [
        copy.deepcopy(dict(items[index])) for index in sorted(selected)
    ]
    return projected, {
        **dict(base_metadata),
        "projected_tokens": estimate_tokens(projected),
        "compacted": True,
        "omitted_items": len(omitted),
        "summary_sha256": summary_sha256,
    }


def _compaction_summary(items: Sequence[Mapping[str, Any]], digest: str) -> str:
    type_counts: Dict[str, int] = {}
    call_ids: List[str] = []
    snippets: List[str] = []
    for item in items:
        item_type = str(item.get("type") or item.get("role") or "unknown")
        type_counts[item_type] = type_counts.get(item_type, 0) + 1
        call_id = str(item.get("call_id") or "")
        if call_id and call_id not in call_ids and len(call_ids) < 20:
            call_ids.append(call_id)
        if len(snippets) < 6:
            snippet = _context_item_snippet(item)
            if snippet:
                snippets.append(snippet[:120])
    counts = ",".join(f"{name}:{count}" for name, count in sorted(type_counts.items()))
    calls = ",".join(call_ids) or "none"
    evidence = " | ".join(snippets) or "none"
    return (
        "[平台上下文压缩] 完整历史仍保存在审计检查点中；本条仅是模型输入投影。"
        f"省略 {len(items)} 项，类型={counts}，call_id={calls}，"
        f"摘要={evidence}，sha256={digest}。"
    )


def _context_item_snippet(item: Mapping[str, Any]) -> str:
    if item.get("type") == "function_call":
        return f"{item.get('name', '')}({item.get('arguments', '')})"
    if item.get("type") == "function_call_output":
        return str(item.get("output") or "")
    content = item.get("content")
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, Sequence) and not isinstance(content, (str, bytes, bytearray)):
        parts: List[str] = []
        for value in content:
            if not isinstance(value, Mapping):
                continue
            text = value.get("text")
            if isinstance(text, str) and text:
                parts.append(text)
        return " ".join(parts).strip()
    return ""


def _normalize_input(input_value: Union[str, Sequence[Mapping[str, Any]]]) -> List[Dict[str, Any]]:
    if isinstance(input_value, str):
        return [{"role": "user", "content": input_value}]
    return [copy.deepcopy(dict(item)) for item in input_value]


def _normalize_ask_user_options(value: Any) -> List[Dict[str, str]]:
    """只向客户端暴露结构完整、值唯一的动态候选项。"""
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        return []
    normalized: List[Dict[str, str]] = []
    seen: set[str] = set()
    for raw in list(value)[:12]:
        if not isinstance(raw, Mapping):
            continue
        label = str(raw.get("label") or "").strip()
        option_value = str(raw.get("value") or "").strip()
        description = str(raw.get("description") or "").strip()
        if not label or not option_value or option_value in seen:
            continue
        seen.add(option_value)
        normalized.append(
            {
                "label": label[:200],
                "value": option_value[:500],
                "description": description[:500],
            }
        )
    return normalized


def _with_ask_user_tool(tools: Sequence[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    normalized = [copy.deepcopy(dict(tool)) for tool in tools]
    if any(tool.get("type") == "function" and tool.get("name") == "ask_user" for tool in normalized):
        return normalized
    normalized.append(
        {
            "type": "function",
            "name": "ask_user",
            "description": "当完成任务确实缺少关键信息时，向用户提出由模型动态生成的问题",
            "parameters": {
                "type": "object",
                "properties": {
                    "question": {"type": "string", "description": "需要用户回答的具体问题"},
                    "context": {
                        "type": ["string", "null"],
                        "description": "该信息会如何影响后续执行；没有补充上下文时传 null",
                    },
                    "options": {
                        "type": "array",
                        "maxItems": 12,
                        "description": "模型根据当前上下文动态生成的候选项；没有候选项时传空数组",
                        "items": {
                            "type": "object",
                            "properties": {
                                "label": {"type": "string", "description": "展示给用户的选项文字"},
                                "value": {"type": "string", "description": "选择后回传给模型的稳定值"},
                                "description": {"type": "string", "description": "该选项的影响或含义"},
                            },
                            "required": ["label", "value", "description"],
                            "additionalProperties": False,
                        },
                    },
                    "allow_free_text": {
                        "type": "boolean",
                        "description": "是否允许用户不选择候选项而自行输入",
                    },
                },
                "required": ["question", "context", "options", "allow_free_text"],
                "additionalProperties": False,
            },
            "strict": True,
        }
    )
    return normalized


async def _invoke_transport(transport: ResponsesTransport, payload: Mapping[str, Any]) -> TransportOutput:
    method = getattr(transport, "create_response", None)
    if method is None and callable(transport):
        method = transport
    if method is None:
        raise TypeError("transport 必须实现 create_response(payload) 或可调用接口")
    result = method(payload)
    if inspect.isawaitable(result):
        result = await result
    return result


async def _invoke_tool_executor(
    executor: ToolExecutor,
    call: ToolCall,
    *,
    approved: bool,
) -> Any:
    method = getattr(executor, "execute", None)
    if method is None and callable(executor):
        method = executor
    if method is None:
        raise TypeError("tool_executor 必须实现 execute(call, approved=...) 或可调用接口")
    result = method(call, approved=approved)
    if inspect.isawaitable(result):
        result = await result
    return result


async def _invoke_tool_rejection(
    executor: ToolExecutor,
    call: ToolCall,
    *,
    reason: str,
) -> None:
    """调用可选拒绝钩子；旧执行器没有钩子时保持兼容。"""
    method = getattr(executor, "reject", None)
    if method is None:
        return
    result = method(call, reason=reason)
    if inspect.isawaitable(result):
        await result


async def _collect_response(output: TransportOutput) -> Tuple[Dict[str, Any], List[Mapping[str, Any]]]:
    if isinstance(output, Mapping):
        return copy.deepcopy(dict(output)), []

    events: List[Mapping[str, Any]] = []
    if hasattr(output, "__aiter__"):
        async for event in output:  # type: ignore[union-attr]
            if isinstance(event, Mapping):
                events.append(copy.deepcopy(dict(event)))
    elif isinstance(output, Sequence):
        events.extend(copy.deepcopy(dict(event)) for event in output if isinstance(event, Mapping))
    else:
        raise TypeError("transport 返回值必须是响应对象或 Responses 事件流")
    terminal_types = {"response.completed", "response.incomplete", "response.failed"}
    if not any(str(event.get("type") or "") in terminal_types for event in events):
        raise IncompleteResponseStreamError("Responses 事件流在终止帧之前结束")
    return _response_from_events(events), events


def _response_from_events(events: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    completed_response: Optional[Dict[str, Any]] = None
    items: Dict[Union[int, str], Dict[str, Any]] = {}
    text_deltas: List[str] = []
    status = ""

    for event in events:
        event_type = str(event.get("type") or "")
        if event_type in {"response.completed", "response.incomplete", "response.failed"}:
            candidate = event.get("response")
            if isinstance(candidate, Mapping):
                completed_response = copy.deepcopy(dict(candidate))
            status = event_type.removeprefix("response.")
        elif event_type in {"response.output_item.added", "response.output_item.done"}:
            item = event.get("item")
            if isinstance(item, Mapping):
                key: Union[int, str] = event.get("output_index", item.get("id", len(items)))  # type: ignore[arg-type]
                items[key] = copy.deepcopy(dict(item))
        elif event_type == "response.function_call_arguments.delta":
            key = event.get("output_index", event.get("item_id", len(items)))
            item = items.setdefault(key, {"type": "function_call", "arguments": ""})
            item["arguments"] = str(item.get("arguments") or "") + str(event.get("delta") or "")
        elif event_type == "response.function_call_arguments.done":
            key = event.get("output_index", event.get("item_id", len(items)))
            item = items.setdefault(key, {"type": "function_call"})
            item["arguments"] = str(event.get("arguments") or item.get("arguments") or "")
        elif event_type == "response.output_text.delta":
            delta = event.get("delta")
            if isinstance(delta, str) and delta:
                text_deltas.append(delta)

    if completed_response is not None and completed_response.get("output") is not None:
        return completed_response

    ordered_items = list(items.values())
    if text_deltas and not _extract_output_text(ordered_items):
        ordered_items.append(
            {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "".join(text_deltas)}],
            }
        )
    return {"object": "response", "status": status, "output": ordered_items}


def _extract_tool_calls(output_items: Sequence[Mapping[str, Any]]) -> List[ToolCall]:
    calls: List[ToolCall] = []
    for item in output_items:
        if item.get("type") != "function_call":
            continue
        call_id = str(item.get("call_id") or item.get("id") or "")
        name = str(item.get("name") or "")
        raw_value = item.get("arguments", "{}")
        raw_arguments = raw_value if isinstance(raw_value, str) else json.dumps(raw_value, ensure_ascii=False)
        parse_error = ""
        arguments: Dict[str, Any] = {}
        try:
            parsed = json.loads(raw_arguments)
            if not isinstance(parsed, dict):
                raise ValueError("工具参数必须是 JSON 对象")
            arguments = parsed
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            parse_error = f"工具 {name or '<unknown>'} 参数解析失败: {exc}"
        if not call_id or not name:
            parse_error = parse_error or "function_call 缺少 call_id 或 name"
        calls.append(
            ToolCall(
                call_id=call_id,
                name=name,
                arguments=arguments,
                raw_arguments=raw_arguments,
                parse_error=parse_error,
            )
        )
    return calls


def _normalize_execution_result(value: Any) -> ToolExecutionResult:
    if isinstance(value, ToolExecutionResult):
        return value
    if isinstance(value, Mapping):
        status = str(value.get("status") or ("success" if value.get("success", True) else "error"))
        output = value.get("output", value.get("data"))
        return ToolExecutionResult(
            status=status,
            output=output,
            error=str(value.get("error") or ""),
            operation=str(value.get("operation") or ""),
            impact=str(value.get("impact") or ""),
            danger=bool(value.get("danger", False)),
            approval_id=value.get("approval_id"),
            preview=copy.deepcopy(value.get("preview")),
        )
    if hasattr(value, "status"):
        status = str(getattr(value, "status", "") or "error")
        return ToolExecutionResult(
            status=status,
            output=getattr(value, "output", getattr(value, "data", None)),
            error=str(getattr(value, "error", "") or ""),
            operation=str(getattr(value, "operation", "") or ""),
            impact=str(getattr(value, "impact", "") or ""),
            danger=bool(getattr(value, "danger", False)),
            approval_id=getattr(value, "approval_id", None),
            preview=copy.deepcopy(getattr(value, "preview", None)),
        )
    if hasattr(value, "success"):
        succeeded = bool(getattr(value, "success", False))
        return ToolExecutionResult(
            status="success" if succeeded else "error",
            output=getattr(value, "data", None),
            error=str(getattr(value, "error", "") or ""),
        )
    return ToolExecutionResult.success(value)


def _is_approval_required(status: str) -> bool:
    return status in {"approval_required", "needs_approval", "escalated", "pending_approval"}


def _tool_output_item(call: ToolCall, execution: ToolExecutionResult) -> Dict[str, Any]:
    if execution.status in {"success", "completed", "ok"}:
        payload = {"status": "success", "output": execution.output}
    else:
        payload = {
            "status": "error",
            "error": execution.error or f"工具返回状态 {execution.status}",
        }
        if execution.output is not None:
            payload["output"] = execution.output
    return _function_call_output(call.call_id, payload)


def _function_call_output(call_id: str, payload: Any) -> Dict[str, Any]:
    return {
        "type": "function_call_output",
        "call_id": call_id,
        "output": _stringify_output(payload),
    }


def _stringify_output(value: Any) -> str:
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)
    except (TypeError, ValueError):
        return str(value)


def _extract_output_text(output_items: Sequence[Mapping[str, Any]]) -> str:
    parts: List[str] = []
    for item in output_items:
        if item.get("type") != "message":
            continue
        for content in list(item.get("content") or []):
            if not isinstance(content, Mapping) or content.get("type") != "output_text":
                continue
            text = content.get("text")
            if isinstance(text, str) and text:
                parts.append(text)
    return "".join(parts).strip()
