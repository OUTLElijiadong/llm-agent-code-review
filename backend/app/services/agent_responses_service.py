"""Prism 普通用户与管理员共用的 Responses Agent 运行适配层。"""

from __future__ import annotations

import copy
import hashlib
import inspect
import json
import re
from datetime import datetime, timezone
from typing import Any, AsyncIterator, Awaitable, Callable, Dict, Mapping, Optional, Sequence

import httpx
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.agents import AgentContext
from app.agents.orchestrator import get_request_orchestrator
from app.agents.skills.registry import SkillRegistry
from app.agents.tool_contracts import (
    FixedToolArgumentError,
    get_fixed_tool_description,
    get_fixed_tool_names,
    get_fixed_tool_schema,
    is_fixed_tool,
    validate_fixed_tool_arguments,
)
from app.core.config import settings
from app.core.permission_codes import PermissionCode
from app.models.agent_governance import ApprovalItem
from app.models.agent_response_run import AgentResponseRun, AgentToolExecution
from app.models.user import User
from app.services import (
    admin_agent_tools,
    admin_capability_service,
    agent_governance_service,
    ops_service,
    policy_engine,
    published_agent_tools,
    rbac_service,
    tool_gateway,
)
from app.services.admin_capability_registry import (
    CAPABILITY_BY_CODE,
    describe_capabilities,
    discovery_tool_schema,
    execution_tool_schema,
)
from app.services.admin_capability_registry import (
    CRITICAL as CAPABILITY_CRITICAL,
)
from app.services.admin_capability_registry import (
    READ as CAPABILITY_READ,
)
from app.services.deepseek_responses_runtime import (
    COMPLETED,
    FAILED,
    INCOMPLETE,
    MAX_ROUNDS_EXCEEDED,
    WAITING_APPROVAL,
    WAITING_INPUT,
    DeepSeekResponsesRuntime,
    InvalidRunStateError,
    RunCheckpoint,
    RuntimeResult,
    ToolCall,
    ToolExecutionResult,
)
from app.services.mcp_tool_provider import McpToolProvider
from app.utils.api_resolver import ApiConfig, resolve_api_config

EventSink = Callable[[Mapping[str, Any]], Optional[Awaitable[None]]]

_ADMIN_TOOL_PREFIX = "admin_"
_WRITE_TOOLS = {
    "create_project",
    "delete_project",
    "start_review",
    "trigger_evolution",
    "admin_set_user_role",
    "admin_delete_user",
    "admin_delete_users",
    "admin_toggle_agent",
    "admin_decide_agent_release",
    "admin_execute_operation",
}
_DANGER_TOOLS = {
    "delete_project",
    "admin_delete_user",
    "admin_delete_users",
    "admin_execute_operation",
}
_MUTATION_SUCCESS_PATTERNS = (
    re.compile(
        r"(?:成功(?:地)?|已经完成|已完成|完成了).{0,24}"
        r"(?:创建|新增|添加|修改|更新|设置|启用|停用|禁用|删除|移除|重置|生成|发布|批准|拒绝|回滚|写入|保存|上传|绑定|分配)"
    ),
    re.compile(
        r"(?:创建|新增|添加|修改|更新|设置|启用|停用|禁用|删除|移除|重置|生成|发布|批准|拒绝|回滚|写入|保存|上传|绑定|分配)"
        r".{0,16}(?:已成功完成|成功完成|已完成|成功|完成了)"
    ),
    re.compile(
        r"\b(?:created|updated|deleted|removed|enabled|disabled|reset|published|approved|rejected|executed)\b"
        r".{0,20}\bsuccess(?:fully)?\b",
        re.I,
    ),
    re.compile(
        r"\bsuccess(?:fully)?\b.{0,20}"
        r"\b(?:created|updated|deleted|removed|enabled|disabled|reset|published|approved|rejected|executed)\b",
        re.I,
    ),
    re.compile(r'\b(?:deleted|updated|created)_count\b\s*[":=]+\s*[1-9]\d*', re.I),
)
_ADMIN_MUTATION_REQUEST = re.compile(
    r"(?:^请|请帮|帮我|帮忙|给我|现在|立即|马上|需要|我要|请通过|请在|把|将)"
    r".{0,80}(?:创建|新增|添加|修改|更新|设置|启用|停用|禁用|删除|移除|重置|生成|发布|批准|拒绝|回滚|写入|保存|上传|绑定|分配)"
    r"|^(?:创建|新增|添加|修改|更新|设置|启用|停用|禁用|删除|移除|重置|生成|发布|批准|拒绝|回滚|写入|保存|上传|绑定|分配)"
)
_ADMIN_MUTATION_DISCUSSION = re.compile(
    r"(?:如果|假如|假设|若|是否|能否|可以吗|怎么|如何|为什么|是什么|什么意思|"
    r"原理|说明|解释|文档|教程|用途|参数|风险|成功后|失败后)"
)
_NON_TERMINAL_RUN_STATUSES = {
    "running",
    "approving",
    "rejecting",
    "answering",
    "waiting_approval",
    "waiting_input",
}


def _is_admin_actor(db: Session, user: User) -> bool:
    """兼容旧角色字段，并保留新版 RBAC 管理员绑定。"""

    if str(getattr(user, "role", "")) in {"admin", "super_admin"}:
        return True
    return rbac_service.is_admin_user(db, int(user.id))


_OPS_READ_ONLY = ops_service.READ_ONLY_ACTIONS
_TOOL_NAME_SAFE = re.compile(r"[^A-Za-z0-9_-]+")
_SENSITIVE_EVENT_KEY = re.compile(
    r"(?:password|passwd|secret|token|api[_-]?key|authorization|cookie|private[_-]?key|"
    r"reasoning(?:[_-]?content)?|encrypted[_-]?content)",
    re.IGNORECASE,
)
_SENSITIVE_TEXT_ASSIGNMENT = re.compile(
    r"(?P<prefix>(?<![A-Za-z0-9_])['\"]?"
    r"(?:password|passwd|secret|token|api[_-]?key|authorization|cookie|private[_-]?key|"
    r"reasoning(?:[_-]?content)?|encrypted[_-]?content)"
    r"['\"]?\s*[:=]\s*)"
    r"(?P<value>\"(?:\\.|[^\"\\])*\"|'(?:\\.|[^'\\])*'|"
    r"Bearer\s+[^\s,;\]}&]+|[^\s,;\]}&]+)",
    re.IGNORECASE,
)
_BEARER_SECRET = re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]+", re.IGNORECASE)
_API_KEY_SECRET = re.compile(r"\bsk-[A-Za-z0-9_-]{8,}\b", re.IGNORECASE)
_PRIVATE_KEY_BLOCK = re.compile(
    r"-----BEGIN [^-\r\n]*PRIVATE KEY-----[\s\S]*?-----END [^-\r\n]*PRIVATE KEY-----",
    re.IGNORECASE,
)


class DatabaseCheckpointStore:
    """把运行检查点写入 MySQL/SQLite，并强制绑定用户、表面和会话。"""

    def __init__(self, db: Session, *, user_id: int, surface: str, session_key: str) -> None:
        self._db = db
        self._user_id = user_id
        self._surface = surface
        self._session_key = session_key

    async def create(self, checkpoint: RunCheckpoint) -> bool:
        active = (
            self._db.query(AgentResponseRun.id)
            .filter(
                AgentResponseRun.user_id == self._user_id,
                AgentResponseRun.surface == self._surface,
                AgentResponseRun.session_key == self._session_key,
                AgentResponseRun.status.in_(_NON_TERMINAL_RUN_STATUSES),
            )
            .with_for_update()
            .first()
        )
        if active is not None:
            self._db.rollback()
            return False
        if self._db.query(AgentResponseRun.id).filter(AgentResponseRun.run_id == checkpoint.run_id).first():
            self._db.rollback()
            return False
        self._db.add(
            AgentResponseRun(
                run_id=checkpoint.run_id,
                user_id=self._user_id,
                surface=self._surface,
                session_key=self._session_key,
                status=checkpoint.status,
                checkpoint_json=json.dumps(checkpoint.to_dict(), ensure_ascii=False, default=str),
                version=1,
            )
        )
        try:
            self._db.commit()
        except IntegrityError:
            self._db.rollback()
            return False
        return True

    async def save(self, checkpoint: RunCheckpoint) -> None:
        row = self._db.query(AgentResponseRun).filter(AgentResponseRun.run_id == checkpoint.run_id).first()
        if row is None:
            row = AgentResponseRun(
                run_id=checkpoint.run_id,
                user_id=self._user_id,
                surface=self._surface,
                session_key=self._session_key,
                status=checkpoint.status,
                checkpoint_json=json.dumps(checkpoint.to_dict(), ensure_ascii=False, default=str),
                version=1,
            )
            self._db.add(row)
        else:
            self._assert_owner(row)
            row.status = checkpoint.status
            row.checkpoint_json = json.dumps(checkpoint.to_dict(), ensure_ascii=False, default=str)
            row.version = int(row.version or 0) + 1
        self._db.commit()

    async def load(self, run_id: str) -> Optional[RunCheckpoint]:
        row = (
            self._db.query(AgentResponseRun)
            .filter(
                AgentResponseRun.run_id == run_id,
                AgentResponseRun.user_id == self._user_id,
                AgentResponseRun.surface == self._surface,
                AgentResponseRun.session_key == self._session_key,
            )
            .first()
        )
        if row is None:
            return None
        try:
            value = json.loads(row.checkpoint_json)
        except (TypeError, json.JSONDecodeError) as exc:
            raise InvalidRunStateError(f"运行 {run_id} 的检查点损坏") from exc
        return RunCheckpoint.from_dict(value)

    async def delete(self, run_id: str) -> None:
        row = (
            self._db.query(AgentResponseRun)
            .filter(
                AgentResponseRun.run_id == run_id,
                AgentResponseRun.user_id == self._user_id,
                AgentResponseRun.surface == self._surface,
                AgentResponseRun.session_key == self._session_key,
            )
            .first()
        )
        if row is not None:
            self._db.delete(row)
            self._db.commit()

    async def claim(
        self,
        run_id: str,
        *,
        expected_status: str,
        claimed_status: str,
        tool_call_id: Optional[str] = None,
    ) -> Optional[RunCheckpoint]:
        row = (
            self._db.query(AgentResponseRun)
            .filter(
                AgentResponseRun.run_id == run_id,
                AgentResponseRun.user_id == self._user_id,
                AgentResponseRun.surface == self._surface,
                AgentResponseRun.session_key == self._session_key,
            )
            .with_for_update()
            .first()
        )
        if row is None or row.status != expected_status:
            self._db.rollback()
            return None
        try:
            checkpoint = RunCheckpoint.from_dict(json.loads(row.checkpoint_json))
        except (TypeError, json.JSONDecodeError) as exc:
            self._db.rollback()
            raise InvalidRunStateError(f"运行 {run_id} 的检查点损坏") from exc
        if checkpoint.pending is None:
            self._db.rollback()
            return None
        if tool_call_id and checkpoint.pending.call.call_id != tool_call_id:
            self._db.rollback()
            return None
        checkpoint.status = claimed_status
        row.status = claimed_status
        row.checkpoint_json = json.dumps(checkpoint.to_dict(), ensure_ascii=False, default=str)
        row.version = int(row.version or 0) + 1
        self._db.commit()
        return checkpoint

    def _assert_owner(self, row: AgentResponseRun) -> None:
        if (
            int(row.user_id) != int(self._user_id)
            or row.surface != self._surface
            or row.session_key != self._session_key
        ):
            raise InvalidRunStateError("运行标识已被其他用户或会话占用")


class NativeResponsesTransport:
    """使用当前用户解析出的 API 配置调用原生 Responses SSE。"""

    def __init__(self, api_config: ApiConfig, event_sink: Optional[EventSink] = None) -> None:
        self._config = api_config
        self._event_sink = event_sink

    async def create_response(self, payload: Mapping[str, Any]) -> AsyncIterator[Mapping[str, Any]]:
        return self._stream(payload)

    async def _stream(self, payload: Mapping[str, Any]) -> AsyncIterator[Mapping[str, Any]]:
        url = _responses_url(self._config.base_url)
        headers = {
            "Authorization": f"Bearer {self._config.api_key}",
            "Accept": "text/event-stream",
            "Content-Type": "application/json",
            "Accept-Encoding": "identity",
        }
        timeout = httpx.Timeout(float(settings.deepseek_timeout), read=float(settings.deepseek_timeout))
        async with httpx.AsyncClient(timeout=timeout, trust_env=False) as client:
            async with client.stream("POST", url, headers=headers, json=dict(payload)) as response:
                if response.status_code >= 400:
                    raw = (await response.aread()).decode("utf-8", errors="replace")
                    raise RuntimeError(_upstream_error(raw, response.status_code))
                event_name = ""
                data_lines: list[str] = []
                async for line in response.aiter_lines():
                    if line == "":
                        event = _decode_sse_event(event_name, data_lines)
                        event_name, data_lines = "", []
                        if event is None:
                            continue
                        await _emit(self._event_sink, event)
                        yield event
                        continue
                    if line.startswith(":"):
                        continue
                    if line.startswith("event:"):
                        event_name = line[6:].strip()
                    elif line.startswith("data:"):
                        data_lines.append(line[5:].lstrip())
                event = _decode_sse_event(event_name, data_lines)
                if event is not None:
                    await _emit(self._event_sink, event)
                    yield event


class PrismToolExecutor:
    """复用 Orchestrator、Skill、MCP 与 OperationsAgent 的受控工具执行器。"""

    def __init__(
        self,
        db: Session,
        user: User,
        *,
        surface: str,
        run_id: str,
        mcp_provider: McpToolProvider,
        event_sink: Optional[EventSink] = None,
    ) -> None:
        self._db = db
        self._user = user
        self._surface = surface
        self._run_id = run_id
        self._is_admin = surface == "admin" and _is_admin_actor(db, user)
        self._mcp = mcp_provider
        self._event_sink = event_sink
        self._orch = get_request_orchestrator(db, user=user)
        self._skill_bindings: Dict[str, str] = {}

    async def tool_schemas(self) -> list[Dict[str, Any]]:
        tools: list[Dict[str, Any]] = []
        is_admin = self._surface == "admin" and self._is_admin
        for name in get_fixed_tool_names():
            if name.startswith(_ADMIN_TOOL_PREFIX) and not is_admin:
                continue
            tools.append(
                {
                    "type": "function",
                    "name": name,
                    "description": get_fixed_tool_description(name),
                    "parameters": get_fixed_tool_schema(name),
                }
            )

        if is_admin:
            tools.extend((discovery_tool_schema(), execution_tool_schema()))

        for schema in SkillRegistry.instance().list_tools(invocable_only=True):
            function = schema.get("function") if isinstance(schema, Mapping) else None
            if not isinstance(function, Mapping):
                continue
            original = str(function.get("name") or "")
            if not original:
                continue
            model_name = _unique_tool_name(f"skill_{original}", set(self._skill_bindings) | {t["name"] for t in tools})
            self._skill_bindings[model_name] = original
            parameters = function.get("parameters")
            tools.append(
                {
                    "type": "function",
                    "name": model_name,
                    "description": str(function.get("description") or original),
                    "parameters": dict(parameters) if isinstance(parameters, Mapping) else {"type": "object"},
                }
            )

        if is_admin and rbac_service.check_permission(self._db, self._user.id, PermissionCode.SERVER_OPS_VIEW):
            if agent_governance_service.is_runtime_enabled(self._db, "operations"):
                tools.append(_operations_tool_schema())
        tools.extend(await self._mcp.discover())
        return tools

    async def execute(self, call: ToolCall, *, approved: bool = False) -> ToolExecutionResult:
        if call.name.startswith(_ADMIN_TOOL_PREFIX) and not self._is_admin:
            return await self._failed_attempt(call, "当前用户没有管理员工具权限")
        if is_fixed_tool(call.name):
            try:
                arguments = validate_fixed_tool_arguments(call.name, call.arguments)
            except FixedToolArgumentError as exc:
                return await self._failed_attempt(call, str(exc))
            call = ToolCall(
                call_id=call.call_id,
                name=call.name,
                arguments=arguments,
                raw_arguments=call.raw_arguments,
                parse_error=call.parse_error,
            )
        if self._mcp.has_tool(call.name):
            if not approved:
                return self._approval(call, danger=False, impact="将向已配置的 MCP Server 发送本次工具参数")
            self._mark_approval(call, approve=True)
            return await self._execute_once(
                call,
                lambda: self._mcp.call(call.name, call.arguments),
            )

        if call.name == "admin_describe_capabilities":
            return await self._describe_admin_capabilities(call)

        if call.name == "admin_execute_capability":
            return await self._execute_admin_capability(call, approved=approved)

        if call.name in self._skill_bindings:
            if not approved:
                return self._approval(call, danger=False, impact="将执行已注册的 Agent Skill")
            self._mark_approval(call, approve=True)
            return await self._execute_once(
                call,
                lambda: self._agent_result(
                    call,
                    self._orch.invoke_tool(
                        self._skill_bindings[call.name],
                        call.arguments,
                        AgentContext(user_id=self._user.id, extra={"run_id": self._run_id}),
                    ),
                ),
            )

        if call.name == "admin_execute_operation":
            return await self._execute_operation(call, approved=approved)

        if call.name == "search_published_agents":
            return await self._execute_once(
                call,
                lambda: published_agent_tools.search_published_agents(
                    self._db,
                    self._user,
                    query=str(call.arguments.get("query") or ""),
                    limit=int(call.arguments.get("limit") or 8),
                ),
            )

        if call.name == "invoke_published_agent":
            return await self._execute_once(
                call,
                lambda: published_agent_tools.invoke_published_agent(
                    self._db,
                    self._user,
                    agent_code=str(call.arguments["agent_code"]),
                    code=str(call.arguments["code"]),
                    language=str(call.arguments.get("language") or "plaintext"),
                    file_name=str(call.arguments.get("file_name") or "snippet.txt"),
                    rules=list(call.arguments.get("rules") or []),
                    line_offset=int(call.arguments.get("line_offset") or 0),
                    experience=str(call.arguments.get("experience") or ""),
                ),
            )

        if call.name == "admin_list_agent_release_approvals":
            return await self._execute_once(
                call,
                lambda: self._agent_result(
                    call,
                    admin_agent_tools.admin_list_agent_release_approvals(
                        self._db,
                        self._user,
                        approval_id=call.arguments.get("approval_id"),
                        status=str(call.arguments.get("status", "pending")),
                        limit=int(call.arguments.get("limit") or 50),
                    ),
                ),
            )

        if call.name == "admin_delete_users":
            return await self._execute_batch_user_delete(call, approved=approved)

        if call.name == "admin_decide_agent_release":
            return await self._execute_release_decision(call, approved=approved)

        if call.name in _WRITE_TOOLS and not approved:
            return self._approval(
                call,
                danger=call.name in _DANGER_TOOLS,
                impact="将使用当前登录用户权限执行该平台写操作",
            )
        if call.name in _WRITE_TOOLS:
            self._mark_approval(call, approve=True)

        if call.name in {"admin_set_user_role", "admin_delete_user", "admin_toggle_agent"}:
            return await self._execute_once(call, lambda: self._execute_admin_write(call))
        return await self._execute_once(
            call,
            lambda: self._agent_result(
                call,
                self._orch.invoke_tool(
                    call.name,
                    call.arguments,
                    AgentContext(user_id=self._user.id, extra={"run_id": self._run_id}),
                ),
            ),
        )

    async def _execute_batch_user_delete(
        self,
        call: ToolCall,
        *,
        approved: bool,
    ) -> ToolExecutionResult:
        if not approved:
            preview_result = admin_agent_tools.preview_delete_users(
                self._db,
                self._user,
                list(call.arguments["user_ids"]),
            )
            if not preview_result.success:
                return await self._failed_attempt(call, str(preview_result.error or "批量删除目标校验失败"))
            preview = dict(preview_result.data or {})
            targets = preview.get("targets") or []
            labels = [f"{item['id']}:{item['username']}" for item in targets[:10]]
            suffix = " 等" if len(targets) > 10 else ""
            return self._approval(
                call,
                danger=True,
                operation="批量软删除用户",
                impact=f"将一次性软删除 {preview['count']} 个账号：{', '.join(labels)}{suffix}",
                preview=preview,
                request_extra={"target_snapshot": preview["target_snapshot"]},
            )
        request = self._approval_request(call)
        self._mark_approval(call, approve=True)
        return await self._execute_once(
            call,
            lambda: self._agent_result(
                call,
                admin_agent_tools.admin_delete_users(
                    self._db,
                    self._user,
                    list(call.arguments["user_ids"]),
                    expected_snapshot=str(request.get("target_snapshot") or ""),
                    context={
                        "copilot_request_id": _request_id(self._run_id, call.call_id),
                        "run_id": self._run_id,
                    },
                ),
            ),
        )

    async def _execute_release_decision(
        self,
        call: ToolCall,
        *,
        approved: bool,
    ) -> ToolExecutionResult:
        approval_id = int(call.arguments["approval_id"])
        decision = str(call.arguments["decision"])
        note = str(call.arguments.get("note") or "")
        if not approved:
            preview_result = admin_agent_tools.preview_agent_release_decision(
                self._db,
                self._user,
                approval_id=approval_id,
                decision=decision,
                note=note,
            )
            if not preview_result.success:
                return await self._failed_attempt(call, str(preview_result.error or "发布审批目标校验失败"))
            preview = dict(preview_result.data or {})
            action_text = "批准发布" if decision == "approve" else "驳回发布"
            agent = (preview.get("approval") or {}).get("agent") or {}
            return self._approval(
                call,
                danger=False,
                operation=f"{action_text} Agent",
                impact=f"将{action_text}「{agent.get('name') or agent.get('code') or approval_id}」",
                preview=preview,
                request_extra={"target_snapshot": preview["target_snapshot"]},
            )
        request = self._approval_request(call)
        self._mark_approval(call, approve=True)
        return await self._execute_once(
            call,
            lambda: self._agent_result(
                call,
                admin_agent_tools.admin_decide_agent_release(
                    self._db,
                    self._user,
                    approval_id=approval_id,
                    decision=decision,
                    note=note,
                    expected_snapshot=str(request.get("target_snapshot") or ""),
                ),
            ),
        )

    def reject(self, call: ToolCall, *, reason: str) -> None:
        self._mark_approval(call, approve=False, reason=reason)

    async def _failed_attempt(self, call: ToolCall, error: str) -> ToolExecutionResult:
        await self._emit_tool_event("response.tool.started", call)
        result = ToolExecutionResult.failure(error)
        await self._emit_tool_result(call, result)
        return result

    async def _emit_tool_event(
        self,
        event_type: str,
        call: ToolCall,
        **extra: Any,
    ) -> None:
        event = {
            "type": event_type,
            "run_id": self._run_id,
            "tool_call_id": call.call_id,
            "call_id": call.call_id,
            "tool_name": call.name,
            "agent_code": self._tool_agent_code(call),
            "arguments": _redact_event_value(self._persisted_arguments(call)),
            **extra,
        }
        await _emit(self._event_sink, event)

    async def _emit_tool_result(
        self,
        call: ToolCall,
        result: ToolExecutionResult,
        *,
        cached: bool = False,
    ) -> None:
        if result.status == "success":
            await self._emit_tool_event(
                "response.tool.completed",
                call,
                status="success",
                cached=cached,
                output_summary=_summarize_tool_value(result.output),
            )
            return
        await self._emit_tool_event(
            "response.tool.failed",
            call,
            status="failed",
            cached=cached,
            error=_summarize_tool_value(result.error or "工具执行失败"),
        )

    def _tool_agent_code(self, call: ToolCall) -> str:
        if call.name == "admin_execute_operation":
            return "operations"
        if call.name == "invoke_published_agent":
            return str(call.arguments.get("agent_code") or "custom_agent")
        if call.name in self._skill_bindings:
            return self._skill_bindings[call.name].split(".", 1)[0]
        return "manager" if self._surface == "admin" else "chat_assistant"

    async def _execute_once(
        self,
        call: ToolCall,
        operation: Callable[[], Any],
    ) -> ToolExecutionResult:
        """持久化占位后至多执行一次；不确定结果绝不自动重复副作用。"""
        await self._emit_tool_event("response.tool.started", call)
        request_id = _request_id(self._run_id, call.call_id)
        row = self._db.query(AgentToolExecution).filter(AgentToolExecution.request_id == request_id).first()
        if row is not None:
            recorded = self._recorded_execution(row)
            await self._emit_tool_result(call, recorded, cached=True)
            return recorded

        row = AgentToolExecution(
            request_id=request_id,
            run_id=self._run_id,
            call_id=call.call_id,
            user_id=self._user.id,
            tool_name=call.name,
            status="executing",
            arguments_json=json.dumps(self._persisted_arguments(call), ensure_ascii=False, default=str),
        )
        self._db.add(row)
        try:
            self._db.commit()
        except IntegrityError:
            self._db.rollback()
            existing = self._db.query(AgentToolExecution).filter(AgentToolExecution.request_id == request_id).first()
            if existing is None:
                raise
            recorded = self._recorded_execution(existing)
            await self._emit_tool_result(call, recorded, cached=True)
            return recorded

        try:
            raw_result = operation()
            if inspect.isawaitable(raw_result):
                raw_result = await raw_result
            result = (
                raw_result if isinstance(raw_result, ToolExecutionResult) else ToolExecutionResult.success(raw_result)
            )
        except Exception as exc:  # noqa: BLE001 - 错误也必须进入幂等结果账本
            result = ToolExecutionResult.failure(str(exc))

        row.status = "success" if result.status == "success" else "failed"
        persisted_output = _redact_event_value(result.output)
        row.result_json = json.dumps(
            {
                "status": result.status,
                "output": persisted_output,
                "error": result.error,
            },
            ensure_ascii=False,
            default=str,
        )
        row.error = result.error or None
        self._db.commit()
        await self._emit_tool_result(call, result)
        return result

    @staticmethod
    def _recorded_execution(row: AgentToolExecution) -> ToolExecutionResult:
        if row.status == "executing":
            return ToolExecutionResult.failure(
                "该工具调用已经开始执行但结果尚未确认；为避免重复副作用，系统不会自动重试"
            )
        try:
            payload = json.loads(row.result_json or "{}")
        except json.JSONDecodeError:
            return ToolExecutionResult.failure("工具执行账本结果损坏，已阻止重复执行")
        if payload.get("status") == "success":
            return ToolExecutionResult.success(payload.get("output"))
        return ToolExecutionResult.failure(str(payload.get("error") or row.error or "工具执行失败"))

    def _approval(
        self,
        call: ToolCall,
        *,
        danger: bool,
        impact: str,
        operation: str = "",
        preview: Any = None,
        request_extra: Optional[Mapping[str, Any]] = None,
    ) -> ToolExecutionResult:
        request_id = _request_id(self._run_id, call.call_id)
        row = self._db.query(ApprovalItem).filter(ApprovalItem.copilot_request_id == request_id).first()
        if row is None:
            request_payload = {
                "run_id": self._run_id,
                "call_id": call.call_id,
                "tool": call.name,
                "arguments": self._persisted_arguments(call),
                "owner_user_id": self._user.id,
                "preview": preview,
                **dict(request_extra or {}),
            }
            row = ApprovalItem(
                title=f"Responses Agent 请求执行 {operation or call.name}",
                agent_code="manager" if self._surface == "admin" else "chat_assistant",
                action=f"responses.{call.name}",
                resource=f"response_run:{self._run_id}",
                risk_level="critical" if danger else "high",
                status="pending",
                decision="escalate",
                decision_reason="等待当前用户在 Agent 对话中批准",
                request_json=json.dumps(request_payload, ensure_ascii=False, default=str),
                copilot_request_id=request_id,
            )
            self._db.add(row)
            self._db.commit()
            self._db.refresh(row)
        request_payload = self._approval_request(call, row=row)
        return ToolExecutionResult.approval_required(
            operation=operation or call.name,
            impact=impact,
            danger=danger,
            approval_id=row.id,
            preview=copy.deepcopy(request_payload.get("preview")),
        )

    def _approval_request(
        self,
        call: ToolCall,
        *,
        row: Optional[ApprovalItem] = None,
    ) -> Dict[str, Any]:
        request_id = _request_id(self._run_id, call.call_id)
        approval = row or (self._db.query(ApprovalItem).filter(ApprovalItem.copilot_request_id == request_id).first())
        if approval is None:
            raise InvalidRunStateError("找不到与当前工具调用匹配的审批记录")
        try:
            payload = json.loads(approval.request_json or "{}")
        except json.JSONDecodeError as exc:
            raise InvalidRunStateError("审批请求内容损坏") from exc
        if (
            payload.get("run_id") != self._run_id
            or payload.get("call_id") != call.call_id
            or payload.get("tool") != call.name
            or int(payload.get("owner_user_id") or 0) != self._user.id
        ):
            raise InvalidRunStateError("审批请求与当前运行、调用或用户不匹配")
        return dict(payload)

    def _mark_approval(self, call: ToolCall, *, approve: bool, reason: str = "") -> None:
        request_id = _request_id(self._run_id, call.call_id)
        row = (
            self._db.query(ApprovalItem).filter(ApprovalItem.copilot_request_id == request_id).with_for_update().first()
        )
        if row is None:
            raise InvalidRunStateError("找不到与当前工具调用匹配的审批记录")
        self._approval_request(call, row=row)
        expected = "approved" if approve else "rejected"
        if row.status in {"approved", "rejected"}:
            if row.status != expected:
                raise InvalidRunStateError("该工具调用已作出相反审批决定")
            return
        if row.status != "pending":
            raise InvalidRunStateError(f"审批记录当前状态为 {row.status}")
        row.status = expected
        row.decision = "allow" if approve else "deny"
        row.decision_reason = reason or ("用户在 Agent 对话中批准" if approve else "用户在 Agent 对话中拒绝")
        row.decided_by = self._user.id
        row.decided_at = datetime.now(timezone.utc)
        self._db.commit()

    def _execute_admin_write(self, call: ToolCall) -> ToolExecutionResult:
        context = {"copilot_request_id": _request_id(self._run_id, call.call_id), "run_id": self._run_id}
        if call.name == "admin_set_user_role":
            result = admin_agent_tools.admin_set_user_role(
                self._db,
                self._user,
                int(call.arguments["user_id"]),
                str(call.arguments["role"]),
                context=context,
            )
        elif call.name == "admin_delete_user":
            result = admin_agent_tools.admin_delete_user(
                self._db,
                self._user,
                int(call.arguments["user_id"]),
                context=context,
            )
        else:
            result = admin_agent_tools.admin_toggle_agent(
                self._db,
                self._user,
                str(call.arguments["agent_code"]),
                bool(call.arguments["enable"]),
                context=context,
            )
        return self._agent_result(call, result)

    async def _describe_admin_capabilities(self, call: ToolCall) -> ToolExecutionResult:
        if not self._is_admin:
            return await self._failed_attempt(call, "仅管理员可查询后台能力契约")
        unknown = sorted(set(call.arguments) - {"page", "query"})
        if unknown:
            return await self._failed_attempt(call, f"能力查询不接受参数: {', '.join(unknown)}")
        page = str(call.arguments.get("page") or "")
        query = str(call.arguments.get("query") or "")

        async def discover() -> ToolExecutionResult:
            from app.main import app

            rows = describe_capabilities(app.openapi(), page=page, query=query)
            if not rows:
                return ToolExecutionResult.failure("没有找到匹配的管理能力")
            return ToolExecutionResult.success({"count": len(rows), "items": rows})

        return await self._execute_once(call, discover)

    async def _execute_admin_capability(
        self,
        call: ToolCall,
        *,
        approved: bool,
    ) -> ToolExecutionResult:
        if not self._is_admin:
            return await self._failed_attempt(call, "仅管理员可执行后台能力")
        unknown = sorted(set(call.arguments) - {"capability", "params"})
        if unknown:
            return await self._failed_attempt(call, f"管理能力工具不接受参数: {', '.join(unknown)}")
        capability = str(call.arguments.get("capability") or "")
        spec = CAPABILITY_BY_CODE.get(capability)
        if spec is None:
            return await self._failed_attempt(call, f"未注册的管理能力: {capability}")
        raw_params = call.arguments.get("params", {})
        if not isinstance(raw_params, Mapping):
            return await self._failed_attempt(call, "管理能力 params 必须是 JSON object")
        params = dict(raw_params)
        legacy_admin = str(getattr(self._user, "role", "")) in {"admin", "super_admin"}
        if (
            spec.permission
            and not legacy_admin
            and not rbac_service.check_permission(
                self._db,
                self._user.id,
                spec.permission,
            )
        ):
            return await self._failed_attempt(call, f"当前管理员缺少权限: {spec.permission}")

        from app.main import app

        try:
            admin_capability_service.prepare_request(spec, params, app.openapi())
        except admin_capability_service.AdminCapabilityError as exc:
            return await self._failed_attempt(call, str(exc))

        policy = tool_gateway.authorize(
            self._db,
            agent_code="manager",
            tool_code="admin_execute_capability",
            action=f"admin.{spec.code}",
            resource=spec.page,
            actor=self._user,
            context={"copilot_request_id": _request_id(self._run_id, call.call_id), "surface": self._surface},
        )
        if policy.decision == policy_engine.DENY:
            return await self._failed_attempt(call, f"策略阻断管理能力: {policy.reason}")

        if spec.risk != CAPABILITY_READ and not approved:
            return self._approval(
                call,
                danger=spec.risk == CAPABILITY_CRITICAL,
                operation=spec.description,
                impact=f"将通过真实业务 API 在 {spec.page} 执行「{spec.description}」",
                preview={
                    "capability": spec.code,
                    "page": spec.page,
                    "risk": spec.risk,
                    "params": _redact_event_value(params),
                },
            )
        if spec.risk != CAPABILITY_READ:
            self._mark_approval(call, approve=True)

        async def execute_capability() -> ToolExecutionResult:
            try:
                output = await admin_capability_service.execute_api(
                    self._user,
                    spec,
                    params,
                    request_id=_request_id(self._run_id, call.call_id),
                )
            except admin_capability_service.AdminCapabilityError as exc:
                return ToolExecutionResult.failure(str(exc))
            protected_output = await self._protect_sensitive_capability_output(
                call,
                capability,
                output,
            )
            return ToolExecutionResult.success(protected_output)

        return await self._execute_once(call, execute_capability)

    async def _protect_sensitive_capability_output(
        self,
        call: ToolCall,
        capability: str,
        output: Mapping[str, Any],
    ) -> dict[str, Any]:
        """一次性凭据仅发往当前管理员连接，不进入模型或持久化账本。"""
        protected = copy.deepcopy(dict(output))
        data = protected.get("data")
        if not isinstance(data, Mapping):
            return protected

        values: list[str] = []
        title = ""
        safe_data: dict[str, Any]
        if capability == "beta_codes.generate":
            raw_codes = data.get("codes")
            if isinstance(raw_codes, Sequence) and not isinstance(raw_codes, (str, bytes, bytearray)):
                values = [str(value) for value in raw_codes if isinstance(value, str) and value]
            title = "新生成的内测码"
            safe_data = {
                "generated_count": len(values),
                "items": copy.deepcopy(data.get("items")) if isinstance(data.get("items"), list) else [],
                "one_time_result": True,
            }
        elif capability == "users.reset_password":
            password = data.get("default_password")
            if isinstance(password, str) and password:
                values = [password]
            title = "重置后的临时密码"
            safe_data = {
                "password_reset": bool(values),
                "one_time_result": True,
            }
        else:
            return protected

        protected["data"] = safe_data
        if values:
            await _emit(
                self._event_sink,
                {
                    "type": "response.sensitive.result",
                    "run_id": self._run_id,
                    "call_id": call.call_id,
                    "capability": capability,
                    "title": title,
                    "notice": "仅当前页面会话显示，请立即妥善保存；刷新后无法恢复明文。",
                    "values": values,
                },
            )
        return protected

    async def _execute_operation(self, call: ToolCall, *, approved: bool) -> ToolExecutionResult:
        if not self._is_admin:
            return ToolExecutionResult.failure("仅管理员可执行运维工具")
        action = str(call.arguments.get("action") or "")
        if action not in ops_service.ACTION_RISKS:
            return ToolExecutionResult.failure(f"不支持的运维动作: {action}")
        if not agent_governance_service.is_runtime_enabled(self._db, "operations"):
            return ToolExecutionResult.failure("全服管理 Agent 当前已停用")
        if not rbac_service.check_permission(self._db, self._user.id, PermissionCode.SERVER_OPS_VIEW):
            return ToolExecutionResult.failure("当前管理员没有服务器运维查看权限")
        if action not in _OPS_READ_ONLY and not rbac_service.check_permission(
            self._db,
            self._user.id,
            PermissionCode.SERVER_OPS_EXECUTE,
        ):
            return ToolExecutionResult.failure("当前管理员没有服务器运维执行权限")
        if ops_service.ACTION_RISKS[action] == "critical" and not rbac_service.check_permission(
            self._db,
            self._user.id,
            PermissionCode.SERVER_OPS_CRITICAL,
        ):
            return ToolExecutionResult.failure("当前管理员没有服务器关键运维权限")
        try:
            args = ops_service.validate_action_params(action, dict(call.arguments.get("params") or {}))
        except ValueError as exc:
            return ToolExecutionResult.failure(str(exc))
        policy = tool_gateway.authorize(
            self._db,
            agent_code="operations",
            tool_code="admin_execute_operation",
            action=f"operations.{action}",
            resource="production",
            actor=self._user,
            context={"copilot_request_id": _request_id(self._run_id, call.call_id), "surface": self._surface},
        )
        if policy.decision == policy_engine.DENY:
            return ToolExecutionResult.failure(f"策略阻断运维动作: {policy.reason}")
        if action not in _OPS_READ_ONLY and not approved:
            return self._approval(
                call,
                danger=ops_service.ACTION_RISKS[action] == "critical",
                operation=f"执行全服运维动作 {action}",
                impact=f"将在生产主机执行白名单运维动作 {action}",
                preview={"action": action, "params": ops_service.audit_action_params(action, args)},
            )
        if action not in _OPS_READ_ONLY:
            self._mark_approval(call, approve=True)

        async def execute_operation() -> ToolExecutionResult:
            result = ops_service.execute(
                self._db,
                self._user,
                action=action,
                params=args,
                request_id=_request_id(self._run_id, call.call_id),
                source="responses_admin_agent",
            )
            if result.get("status") != "success":
                return ToolExecutionResult.failure(str(result.get("error") or "运维动作失败"))
            return ToolExecutionResult.success(result)

        return await self._execute_once(call, execute_operation)

    @staticmethod
    def _persisted_arguments(call: ToolCall) -> Dict[str, Any]:
        """运维写入内容和 SSH 公钥只保存摘要；其他工具保持原有参数。"""
        arguments = dict(call.arguments)
        if call.name == "admin_execute_capability":
            return dict(_redact_event_value(arguments))
        if call.name != "admin_execute_operation":
            return arguments
        action = str(arguments.get("action") or "")
        params = arguments.get("params") if isinstance(arguments.get("params"), dict) else {}
        arguments["params"] = ops_service.audit_action_params(action, dict(params))
        return arguments

    @staticmethod
    def _agent_result(call: ToolCall, result: Any) -> ToolExecutionResult:
        if getattr(result, "success", False):
            return ToolExecutionResult.success(getattr(result, "data", None))
        return ToolExecutionResult.failure(str(getattr(result, "error", "") or f"工具 {call.name} 执行失败"))


class AgentResponsesService:
    """构建并驱动一次用户隔离的 Agent Responses 运行。"""

    def __init__(self, db: Session, user: User, *, surface: str, session_key: str) -> None:
        if surface not in {"user", "admin"}:
            raise ValueError("surface 必须是 user 或 admin")
        if surface == "admin" and not _is_admin_actor(db, user):
            raise PermissionError("仅管理员可使用管理员 Agent")
        self._db = db
        self._user = user
        self._surface = surface
        self._session_key = session_key
        self._store = DatabaseCheckpointStore(
            db,
            user_id=user.id,
            surface=surface,
            session_key=session_key,
        )

    async def start(
        self,
        messages: Sequence[Mapping[str, Any]],
        *,
        run_id: str,
        event_sink: Optional[EventSink] = None,
    ) -> RuntimeResult:
        executor, runtime = await self._runtime(run_id, event_sink)
        tools = await executor.tool_schemas()
        result = await runtime.start(
            messages,
            instructions=_instructions(self._surface),
            tools=tools,
            run_id=run_id,
        )
        await self._emit_validated_admin_output(event_sink, result)
        return result

    async def resume(
        self,
        *,
        run_id: str,
        action: str,
        call_id: str = "",
        answer: str = "",
        confirmation: str = "",
        event_sink: Optional[EventSink] = None,
    ) -> RuntimeResult:
        executor, runtime = await self._runtime(run_id, event_sink)
        # 新 HTTP 请求恢复运行时需要重建 Skill/MCP 名称绑定；检查点中的工具
        # schema 仍由 runtime 原样复用，这里只恢复执行器映射。
        await executor.tool_schemas()
        if action == "approve":
            result = await runtime.approve(
                run_id,
                call_id or None,
                confirmation=confirmation,
            )
        elif action == "reject":
            result = await runtime.reject(run_id, call_id or None, reason=answer or "用户拒绝执行该操作")
        elif action == "answer":
            if not answer.strip():
                raise ValueError("回答不能为空")
            result = await runtime.answer(run_id, answer, call_id or None)
        else:
            raise ValueError("不支持的恢复动作")
        await self._emit_validated_admin_output(event_sink, result)
        return result

    async def _emit_validated_admin_output(
        self,
        event_sink: Optional[EventSink],
        result: RuntimeResult,
    ) -> None:
        if self._surface != "admin" or result.status != COMPLETED or not result.output_text:
            return
        await _emit(event_sink, {"type": "response.output_text.delta", "delta": result.output_text})

    async def _runtime(
        self,
        run_id: str,
        event_sink: Optional[EventSink],
    ) -> tuple[PrismToolExecutor, DeepSeekResponsesRuntime]:
        config = resolve_api_config(self._db, self._user.id)
        mcp = McpToolProvider()
        executor = PrismToolExecutor(
            self._db,
            self._user,
            surface=self._surface,
            run_id=run_id,
            mcp_provider=mcp,
            event_sink=event_sink,
        )
        transport_sink = (
            _buffer_admin_text_sink(event_sink)
            if self._surface == "admin"
            else event_sink
        )
        runtime = DeepSeekResponsesRuntime(
            transport=NativeResponsesTransport(config, transport_sink),
            tool_executor=executor,
            checkpoint_store=self._store,
            model=config.model or settings.deepseek_model,
            max_rounds=20,
            stream=True,
            context_window_tokens=settings.deepseek_context_window_tokens,
            max_output_tokens=settings.deepseek_max_output_tokens,
            compaction_threshold_tokens=settings.deepseek_compaction_threshold_tokens,
            keep_recent_tokens=settings.deepseek_compaction_keep_recent_tokens,
            completion_guard=self._validate_admin_completion if self._surface == "admin" else None,
        )
        return executor, runtime

    async def _validate_admin_completion(
        self,
        checkpoint: RunCheckpoint,
        output_text: str,
    ) -> Optional[str]:
        completed, successful = _ledger_admin_write_evidence(
            self._db,
            user_id=int(self._user.id),
            checkpoint=checkpoint,
        )
        return _admin_completion_guard(
            checkpoint,
            output_text,
            completed_writes=completed,
            successful_writes=successful,
        )


async def _emit(sink: Optional[EventSink], event: Mapping[str, Any]) -> None:
    if sink is None:
        return
    result = sink(copy.deepcopy(dict(event)))
    if inspect.isawaitable(result):
        await result


def _buffer_admin_text_sink(sink: Optional[EventSink]) -> EventSink:
    async def filtered(event: Mapping[str, Any]) -> None:
        if str(event.get("type") or "") == "response.output_text.delta":
            return
        await _emit(sink, event)

    return filtered


def _redact_sensitive_text(value: str) -> str:
    """脱敏以字符串形式封装的工具参数、请求头和密钥。"""
    stripped = value.strip()
    if stripped.startswith(("{", "[")):
        try:
            parsed = json.loads(stripped)
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, (Mapping, list)):
            return json.dumps(_redact_event_value(parsed), ensure_ascii=False, default=str)
    redacted = _PRIVATE_KEY_BLOCK.sub("[REDACTED PRIVATE KEY]", value)
    redacted = _SENSITIVE_TEXT_ASSIGNMENT.sub(
        lambda match: f"{match.group('prefix')}[REDACTED]",
        redacted,
    )
    redacted = _BEARER_SECRET.sub("Bearer [REDACTED]", redacted)
    return _API_KEY_SECRET.sub("[REDACTED API KEY]", redacted)


def _redact_event_value(value: Any, *, key: str = "", depth: int = 0) -> Any:
    """为 SSE 工具事件生成有界、递归脱敏的参数与摘要。"""
    if _SENSITIVE_EVENT_KEY.search(key):
        return "[REDACTED]"
    if depth >= 5:
        return "[TRUNCATED]"
    if isinstance(value, Mapping):
        items = list(value.items())
        redacted = {
            str(item_key): _redact_event_value(item_value, key=str(item_key), depth=depth + 1)
            for item_key, item_value in items[:50]
        }
        if len(items) > 50:
            redacted["_truncated_fields"] = len(items) - 50
        return redacted
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        values = list(value)
        redacted_values = [_redact_event_value(item, depth=depth + 1) for item in values[:20]]
        if len(values) > 20:
            redacted_values.append(f"[TRUNCATED {len(values) - 20} ITEMS]")
        return redacted_values
    if isinstance(value, str):
        safe_value = _redact_sensitive_text(value)
        if len(safe_value) <= 500:
            return safe_value
        return f"{safe_value[:300]}...[TRUNCATED {len(safe_value) - 300} CHARS]"
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return str(value)[:500]


def redact_agent_event_value(value: Any) -> Any:
    """向 API 层提供统一的有界递归脱敏，避免恢复接口绕过 SSE 保护。"""
    return _redact_event_value(value)


def _public_response_output(value: Any) -> list[dict[str, Any]]:
    """终态事件只暴露用户可见消息，排除 reasoning 和函数参数。"""
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        return []
    output: list[dict[str, Any]] = []
    for raw_item in list(value)[:100]:
        if not isinstance(raw_item, Mapping) or str(raw_item.get("type") or "") != "message":
            continue
        content: list[dict[str, Any]] = []
        raw_content = raw_item.get("content")
        if isinstance(raw_content, Sequence) and not isinstance(raw_content, (str, bytes, bytearray)):
            for raw_part in list(raw_content)[:100]:
                if not isinstance(raw_part, Mapping):
                    continue
                part_type = str(raw_part.get("type") or "")
                if part_type not in {"output_text", "refusal"}:
                    continue
                text = raw_part.get("text") if part_type == "output_text" else raw_part.get("refusal")
                if not isinstance(text, str):
                    continue
                key = "text" if part_type == "output_text" else "refusal"
                content.append({"type": part_type, key: text})
        output.append(
            {
                "id": str(raw_item.get("id") or ""),
                "type": "message",
                "status": str(raw_item.get("status") or "completed"),
                "role": str(raw_item.get("role") or "assistant"),
                "content": content,
            }
        )
    return output


def _summarize_tool_value(value: Any) -> str:
    sanitized = _redact_event_value(value)
    if isinstance(sanitized, str):
        return sanitized[:1000]
    try:
        return json.dumps(sanitized, ensure_ascii=False, default=str)[:1000]
    except (TypeError, ValueError):
        return str(sanitized)[:1000]


def _responses_url(base_url: str) -> str:
    value = base_url.rstrip("/")
    if value.endswith("/v1/responses"):
        return value
    if value.endswith("/v1"):
        return f"{value}/responses"
    return f"{value}/v1/responses"


def _decode_sse_event(event_name: str, data_lines: Sequence[str]) -> Optional[Mapping[str, Any]]:
    if not data_lines:
        return None
    raw = "\n".join(data_lines)
    if raw == "[DONE]":
        return None
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(value, Mapping):
        return None
    event = dict(value)
    if not event.get("type") and event_name:
        event["type"] = event_name
    return event


def _upstream_error(raw: str, status: int) -> str:
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        payload = {}
    error = payload.get("error") if isinstance(payload, Mapping) else None
    if isinstance(error, Mapping) and error.get("message"):
        return f"Responses 上游 HTTP {status}: {str(error['message'])[:500]}"
    return f"Responses 上游 HTTP {status}"


def _instructions(surface: str) -> str:
    identity = "Prism 管理员 Agent" if surface == "admin" else "Prism 代码审查 Agent"
    return (
        f"你是 {identity}。所有事实查询和操作必须使用已提供工具；不要编造工具结果，也不要声称未执行的动作已完成。"
        "根据每次工具返回结果自主判断下一步，可以连续调用多个工具。"
        "缺少真正阻断任务的信息时调用 ask_user，问题、候选项及其说明必须由你根据当前任务动态生成，不使用预设问题。"
        "涉及名称近义表达或自定义 Agent 能力时先调用 search_published_agents；候选不唯一时用 ask_user "
        "展示动态候选，确认后才能调用 invoke_published_agent。"
        "涉及用户批量操作时必须先查询真实用户。用户说序号、第几条或范围而未明确是用户 ID 时，"
        "不得猜测；必须用 ask_user 区分列表序号与用户 ID，得到精确 user_ids 后再调用批量工具。"
        "管理员界面任务必须先调用 admin_describe_capabilities 查询对应页面能力和精确参数，"
        "再调用 admin_execute_capability；不得猜测能力编码或参数。"
        "管理员处理 Agent 发布审批前必须先查询完整详情，展示修改前后内容、依赖、测试证据和风险，再申请执行决策。"
        "写操作由系统暂停并展示审批；用户点击批准后系统会把原调用结果自动交还给你，不要要求用户重复发送指令。"
        "使用中文直接给出结果，不使用预设套话，不输出空白行；代码块内部格式保持原样。"
    )


def _admin_completion_guard(
    checkpoint: RunCheckpoint,
    output_text: str,
    *,
    completed_writes: Optional[set[str]] = None,
    successful_writes: Optional[set[str]] = None,
) -> Optional[str]:
    """阻止管理 Agent 在没有成功写工具证据时声称变更已完成。"""

    requested_capabilities = _requested_admin_write_capabilities(checkpoint.transcript)
    claims_success = bool(output_text) and any(pattern.search(output_text) for pattern in _MUTATION_SUCCESS_PATTERNS)
    claimed_capabilities = {
        spec.code
        for spec in CAPABILITY_BY_CODE.values()
        if spec.risk != CAPABILITY_READ and spec.code.casefold() in output_text.casefold()
    }
    attempted_capabilities = set(_admin_write_calls(checkpoint.transcript).values())
    mutation_requested = _requests_admin_mutation(checkpoint.transcript)
    if not mutation_requested and not attempted_capabilities:
        return None
    required_capabilities = (
        claimed_capabilities
        or (requested_capabilities if mutation_requested else set())
        or attempted_capabilities
    )
    transcript_completed, transcript_successful = _transcript_admin_write_evidence(checkpoint.transcript)
    completed = transcript_completed if completed_writes is None else completed_writes
    successful = transcript_successful if successful_writes is None else successful_writes

    if claims_success and (required_capabilities or mutation_requested):
        if required_capabilities and required_capabilities.issubset(successful):
            return None
        missing = sorted(required_capabilities - successful)
        detail = f": {', '.join(missing)}" if missing else ""
        return f"回复声称管理写操作已完成，但当前运行缺少精确的成功写工具证据{detail}"

    if not requested_capabilities and not mutation_requested:
        return None
    if required_capabilities and required_capabilities.issubset(completed):
        return None
    missing = sorted(required_capabilities - completed)
    detail = f": {', '.join(missing)}" if missing else ""
    return f"管理写请求在没有精确工具执行证据时就结束了{detail}"


def _requested_admin_write_capabilities(
    transcript: Sequence[Mapping[str, Any]],
) -> set[str]:
    normalized = _latest_user_text(transcript).casefold()
    return {
        spec.code
        for spec in CAPABILITY_BY_CODE.values()
        if spec.risk != CAPABILITY_READ and spec.code.casefold() in normalized
    }


def _latest_user_text(transcript: Sequence[Mapping[str, Any]]) -> str:
    latest_user_text = ""
    for item in transcript:
        if str(item.get("role") or "") != "user":
            continue
        content = item.get("content")
        if isinstance(content, str):
            latest_user_text = content
        elif isinstance(content, Sequence) and not isinstance(content, (str, bytes, bytearray)):
            latest_user_text = " ".join(
                str(part.get("text") or "")
                for part in content
                if isinstance(part, Mapping)
            )
    return latest_user_text.strip()


def _requests_admin_mutation(transcript: Sequence[Mapping[str, Any]]) -> bool:
    text = _latest_user_text(transcript)
    if not text or _ADMIN_MUTATION_DISCUSSION.search(text):
        return False
    if _requested_admin_write_capabilities(transcript):
        return True
    return bool(_ADMIN_MUTATION_REQUEST.search(text))


def _admin_write_calls(transcript: Sequence[Mapping[str, Any]]) -> dict[str, str]:
    calls: dict[str, tuple[str, Mapping[str, Any]]] = {}
    for item in transcript:
        if str(item.get("type") or "") == "function_call":
            call_id = str(item.get("call_id") or "")
            name = str(item.get("name") or "")
            raw_arguments = item.get("arguments")
            try:
                arguments = json.loads(raw_arguments) if isinstance(raw_arguments, str) else raw_arguments
            except json.JSONDecodeError:
                arguments = {}
            if call_id and isinstance(arguments, Mapping):
                calls[call_id] = (name, arguments)
    return {
        call_id: write_code
        for call_id, (name, arguments) in calls.items()
        if (write_code := _admin_write_code(name, arguments))
    }


def _transcript_admin_write_evidence(
    transcript: Sequence[Mapping[str, Any]],
) -> tuple[set[str], set[str]]:
    calls = _admin_write_calls(transcript)
    completed: set[str] = set()
    successful: set[str] = set()
    for item in transcript:
        if str(item.get("type") or "") != "function_call_output":
            continue
        call_id = str(item.get("call_id") or "")
        write_code = calls.get(call_id)
        if not write_code:
            continue
        raw_output = item.get("output")
        try:
            output = json.loads(raw_output) if isinstance(raw_output, str) else raw_output
        except json.JSONDecodeError:
            output = {}
        if not isinstance(output, Mapping):
            continue
        completed.add(write_code)
        if str(output.get("status") or "") == "success":
            successful.add(write_code)
    return completed, successful


def _ledger_admin_write_evidence(
    db: Session,
    *,
    user_id: int,
    checkpoint: RunCheckpoint,
) -> tuple[set[str], set[str]]:
    call_codes = _admin_write_calls(checkpoint.transcript)
    if not call_codes:
        return set(), set()
    rows = (
        db.query(AgentToolExecution)
        .filter(
            AgentToolExecution.run_id == checkpoint.run_id,
            AgentToolExecution.user_id == user_id,
            AgentToolExecution.call_id.in_(tuple(call_codes)),
        )
        .all()
    )
    completed: set[str] = set()
    successful: set[str] = set()
    for row in rows:
        expected_code = call_codes.get(str(row.call_id))
        if not expected_code:
            continue
        try:
            arguments = json.loads(row.arguments_json or "{}")
        except json.JSONDecodeError:
            continue
        if not isinstance(arguments, Mapping):
            continue
        actual_code = _admin_write_code(str(row.tool_name or ""), arguments)
        if (
            actual_code != expected_code
            or row.request_id != _request_id(checkpoint.run_id, str(row.call_id))
            or row.status == "executing"
        ):
            continue
        completed.add(expected_code)
        if row.status == "success":
            successful.add(expected_code)
    return completed, successful


def _admin_write_code(name: str, arguments: Mapping[str, Any]) -> str:
    if name == "admin_execute_capability":
        spec = CAPABILITY_BY_CODE.get(str(arguments.get("capability") or ""))
        return spec.code if spec is not None and spec.risk != CAPABILITY_READ else ""
    if name == "admin_execute_operation":
        action = str(arguments.get("action") or "")
        return f"operations.{action}" if action and action not in _OPS_READ_ONLY else ""
    return name if name in _WRITE_TOOLS else ""


def _operations_tool_schema() -> Dict[str, Any]:
    return {
        "type": "function",
        "name": "admin_execute_operation",
        "description": "查询生产状态或执行宿主机白名单运维动作；有副作用的动作会先等待用户批准",
        "parameters": {
            "type": "object",
            "oneOf": [
                {
                    "type": "object",
                    "properties": {
                        "action": {"type": "string", "const": action},
                        "params": ops_service.ACTION_PARAM_SCHEMAS[action],
                    },
                    "required": ["action", "params"],
                    "additionalProperties": False,
                }
                for action in sorted(ops_service.ACTION_RISKS)
            ],
        },
    }


def _unique_tool_name(value: str, occupied: set[str]) -> str:
    base = _TOOL_NAME_SAFE.sub("_", value).strip("_-")[:56] or "tool"
    candidate = base
    suffix = 2
    while candidate in occupied:
        candidate = f"{base[:52]}_{suffix}"
        suffix += 1
    return candidate


def _request_id(run_id: str, call_id: str) -> str:
    return hashlib.sha256(f"responses:{run_id}:{call_id}".encode("utf-8")).hexdigest()


def terminal_event(result: RuntimeResult) -> Mapping[str, Any]:
    """把运行结果转成唯一终态事件，避免中间模型轮次提前结束前端。"""

    if result.status == COMPLETED:
        event_type = "response.completed"
    elif result.status == INCOMPLETE:
        event_type = "response.incomplete"
    else:
        event_type = "response.failed"
    return {
        "type": event_type,
        "response": {
            "id": result.run_id,
            "object": "response",
            "status": result.status,
            "output_text": result.output_text,
            "output": _public_response_output(result.response.get("output")),
            "error": redact_agent_event_value(result.error) if result.error else None,
            "rounds": result.rounds,
        },
    }


def is_paused(result: RuntimeResult) -> bool:
    return result.status in {WAITING_APPROVAL, WAITING_INPUT}


def is_terminal_status(status: str) -> bool:
    return status in {COMPLETED, INCOMPLETE, FAILED, MAX_ROUNDS_EXCEEDED}
