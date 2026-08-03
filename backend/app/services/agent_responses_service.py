"""Prism 普通用户与管理员共用的 Responses Agent 运行适配层。"""

from __future__ import annotations

import copy
import hashlib
import hmac
import inspect
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, AsyncIterator, Awaitable, Callable, Dict, Literal, Mapping, Optional, Sequence
from urllib.parse import urlencode

import httpx
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.agents import AgentContext
from app.agents.orchestrator import get_request_orchestrator
from app.agents.skills.registry import SkillRegistry
from app.agents.tool_contracts import (
    DownloadProjectSourceArguments,
    FixedToolArgumentError,
    FixedToolArguments,
    ImportRemoteProjectArguments,
    UpdateProjectArguments,
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
    code_file_service,
    ops_service,
    policy_engine,
    published_agent_tools,
    rbac_service,
    report_service,
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
    FatalToolExecutionError,
    InvalidRunStateError,
    RunCheckpoint,
    RuntimeResult,
    ToolCall,
    ToolExecutionResult,
)
from app.services.mcp_tool_provider import McpToolProvider
from app.services.user_capability_registry import (
    CAPABILITY_BY_CODE as USER_CAPABILITY_BY_CODE,
)
from app.services.user_capability_registry import (
    CRITICAL as USER_CAPABILITY_CRITICAL,
)
from app.services.user_capability_registry import (
    READ as USER_CAPABILITY_READ,
)
from app.services.user_capability_registry import USER_CAPABILITIES
from app.services.user_capability_registry import (
    describe_capabilities as describe_user_capabilities,
)
from app.services.user_capability_registry import (
    discovery_tool_schema as user_discovery_tool_schema,
)
from app.services.user_capability_registry import (
    execution_tool_schema as user_execution_tool_schema,
)
from app.utils.api_resolver import ApiConfig, resolve_api_config

EventSink = Callable[[Mapping[str, Any]], Optional[Awaitable[None]]]
SessionValidator = Callable[[], bool]


class AgentSessionExpiredError(FatalToolExecutionError):
    """当前 Responses 运行所属的登录版本已被更新登录取代。"""


@dataclass(frozen=True)
class _AdminWriteCall:
    """管理员写调用的不可变身份；证据必须绑定到完整调用签名。"""

    code: str
    tool_name: str
    arguments_json: str
    invalid: bool = False


class DownloadReportArguments(FixedToolArguments):
    """报告下载入口的固定参数契约。"""

    task_id: int
    format: Literal["json", "html", "pdf", "word"] = "html"
    template_type: Literal["simple", "detailed", "compliance"] = "detailed"


class DownloadCodeFileArguments(FixedToolArguments):
    """二进制代码文件下载入口的固定参数契约。"""

    file_id: int


class StartRoundtableDiscussionArguments(FixedToolArguments):
    """用户 Agent 启动圆桌讨论的固定参数。"""

    project_id: int
    file_id: int
    review_type: str = "full"


class GetRoundtableDiscussionArguments(FixedToolArguments):
    """读取当前用户圆桌讨论状态的固定参数。"""

    session_id: str


class ControlRoundtableDiscussionArguments(FixedToolArguments):
    """控制当前用户圆桌讨论的固定参数。"""

    session_id: str
    action: Literal["pause", "resume", "stop", "user_input"]
    content: str = ""


_ADMIN_TOOL_PREFIX = "admin_"
_SUPER_ADMIN_FIXED_TOOLS = frozenset({"admin_system_status"})
_SECURITY_SCAN_FIXED_TOOLS = frozenset({
    "audit_security_for_file",
    "audit_security_for_task",
    "audit_security_for_project",
})
_WRITE_TOOLS = {
    "create_project",
    "update_project",
    "delete_project",
    "import_remote_project",
    "start_roundtable_discussion",
    "control_roundtable_discussion",
    "start_review",
    "audit_security_for_file",
    "audit_security_for_task",
    "audit_security_for_project",
    "run_project_tests",
    "deploy_project_sandbox",
    "close_sandbox",
    "extend_sandbox",
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
_USER_CAPABILITY_NAMES = {
    "update_project",
    "import_remote_project",
    "download_project_source",
    "download_report",
    "download_code_file",
    "start_roundtable_discussion",
    "get_roundtable_discussion",
    "control_roundtable_discussion",
    "run_project_tests",
    "deploy_project_sandbox",
    "close_sandbox",
    "extend_sandbox",
}
_CN_MUTATION_VERB = (
    r"(?:创建|新增|添加|修改|调整|编辑|更改|更新|设置|启用|停用|禁用|下线|"
    r"删除|移除|重置|生成|撤销|发布|批准|驳回|拒绝|回滚|写入|保存|上传|导入|"
    r"绑定|分配|激活|抓取|运行|试算|解决|记录|登记|覆盖|评测|触发|调用|测试|检查|同步|沉淀|应用|"
    r"备份|校验|验证|重启|重载|续期|维护|恢复|清理|安装|升级|卸载|锁定|解锁|"
    r"暂停|启动|停止|开放|关闭)"
)
_MUTATION_SUCCESS_PATTERNS = (
    re.compile(r"(?:成功(?:地)?|已经完成|已完成|完成了).{0,24}" + _CN_MUTATION_VERB),
    re.compile(_CN_MUTATION_VERB + r".{0,16}(?:已成功完成|成功完成|已完成|成功|完成了)"),
    re.compile(r"(?:已|已经)(?:成功)?" + _CN_MUTATION_VERB),
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
_MUTATION_FAILURE_PATTERNS = (
    re.compile(
        r"(?:未(?:能|成功|完成)?|没(?:有)?|无法|不能|不会|失败|未执行|未完成)"
        r".{0,24}" + _CN_MUTATION_VERB
    ),
    re.compile(_CN_MUTATION_VERB + r".{0,24}(?:失败|被拒绝|已取消|未执行|未完成|未成功|无法|不能)"),
    re.compile(r"(?:用户|审批|策略|系统).{0,16}(?:拒绝|取消).{0,16}(?:执行|操作|请求)"),
    re.compile(r"(?:请求|操作|执行).{0,12}(?:被拒绝|已取消)"),
    re.compile(r"(?:已取消|不会).{0,12}(?:执行|操作|" + _CN_MUTATION_VERB + r")"),
    re.compile(r"\b(?:failed|rejected|cancelled|canceled|denied|not completed)\b", re.I),
)
_ADMIN_MUTATION_REQUEST = re.compile(
    r"^(?:请(?!问)|请帮|帮我|帮忙|麻烦|劳烦|给我|现在|立即|马上|需要|我要|"
    r"请通过|请在|把|将|直接|执行).{0,80}" + _CN_MUTATION_VERB + r"|^" + _CN_MUTATION_VERB
)
_ADMIN_MUTATION_DISCUSSION = re.compile(
    r"(?:^\s*(?:请说明|请告诉我|请介绍|请解释|请问|是否|能否|可否|可以吗|怎么|如何|为什么|是什么|什么意思|"
    r"原理|说明|解释|介绍|告诉我|文档|教程|用途)"
    r"|(?:的参数|参数和|成功后|失败后|之后|以后).{0,40}"
    r"(?:是什么|什么意思|怎么|如何|是否|能否|可以|恢复))"
)
_ADMIN_POLITE_MUTATION_REQUEST = re.compile(
    r"(?:能否帮我|可否帮我|可以帮我|是否可以帮我|请帮我|帮我|麻烦|劳烦)"
    r".{0,80}" + _CN_MUTATION_VERB
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


def _is_super_admin_actor(db: Session, user: User) -> bool:
    if str(getattr(user, "username", "")) != "admin" or str(getattr(user, "role", "")) != "super_admin":
        return False
    return rbac_service.is_super_admin_user(db, int(user.id))


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
        from app.utils.api_resolver import validate_ai_base_url
        from app.utils.public_http import pin_public_http_url

        base_url = validate_ai_base_url(
            self._config.base_url,
            resolve_host=True,
            allow_private=False,
        )
        target = pin_public_http_url(_responses_url(base_url))
        headers = {
            "Authorization": f"Bearer {self._config.api_key}",
            "Accept": "text/event-stream",
            "Content-Type": "application/json",
            "Accept-Encoding": "identity",
            "Host": target.host_header,
        }
        timeout = httpx.Timeout(float(settings.deepseek_timeout), read=float(settings.deepseek_timeout))
        async with httpx.AsyncClient(timeout=timeout, trust_env=False) as client:
            async with client.stream(
                "POST",
                target.request_url,
                headers=headers,
                json=dict(payload),
                extensions=target.request_extensions,
            ) as response:
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
        session_validator: Optional[SessionValidator] = None,
    ) -> None:
        self._db = db
        self._user = user
        self._surface = surface
        self._run_id = run_id
        self._is_admin = surface == "admin" and _is_admin_actor(db, user)
        self._is_super_admin = surface == "admin" and _is_super_admin_actor(db, user)
        self._mcp = mcp_provider
        self._event_sink = event_sink
        self._session_validator = session_validator
        self._orch = get_request_orchestrator(db, user=user)
        self._skill_bindings: Dict[str, str] = {}

    async def tool_schemas(self) -> list[Dict[str, Any]]:
        self._assert_session_active()
        tools: list[Dict[str, Any]] = []
        is_admin = self._surface == "admin" and self._is_admin
        legacy_admin = str(getattr(self._user, "role", "")) in {"admin", "super_admin"}
        can_invoke_published = legacy_admin or rbac_service.check_permission(
            self._db,
            self._user.id,
            PermissionCode.CUSTOM_AGENT_INVOKE,
        )
        can_view_projects = legacy_admin or rbac_service.check_permission(
            self._db,
            self._user.id,
            PermissionCode.PROJECT_VIEW,
        )
        can_download_files = legacy_admin or rbac_service.check_permission(
            self._db,
            self._user.id,
            PermissionCode.FILE_DOWNLOAD,
        )
        can_scan_security = legacy_admin or rbac_service.check_permission(
            self._db,
            self._user.id,
            PermissionCode.SECURITY_SCAN,
        )
        can_configure_agents = self._has_permission(PermissionCode.AGENT_CONFIGURE)
        for name in get_fixed_tool_names():
            if name.startswith(_ADMIN_TOOL_PREFIX) and not is_admin:
                continue
            if name in _SUPER_ADMIN_FIXED_TOOLS and not self._is_super_admin:
                continue
            if name == "trigger_evolution" and not can_configure_agents:
                continue
            if name in _SECURITY_SCAN_FIXED_TOOLS and not can_scan_security:
                continue
            if (
                self._surface == "user"
                and name in {"search_published_agents", "invoke_published_agent"}
                and not can_invoke_published
            ):
                continue
            tools.append(
                {
                    "type": "function",
                    "name": name,
                    "description": get_fixed_tool_description(name),
                    "parameters": get_fixed_tool_schema(name),
                }
            )

        # 用户端项目页的写入/源码能力不是任意 HTTP 代理，而是固定的、
        # 请求级用户隔离能力。它们单独注册，避免破坏旧固定工具契约。
        user_fixed_schemas = _user_capability_schemas()
        if self._surface == "user" and not (can_view_projects and can_download_files):
            user_fixed_schemas = [
                schema for schema in user_fixed_schemas if schema["name"] != "download_project_source"
            ]
        tools.extend(user_fixed_schemas)

        if self._surface == "user":
            available_specs = [
                spec
                for spec in USER_CAPABILITIES
                if not spec.permission
                or legacy_admin
                or rbac_service.check_permission(self._db, self._user.id, spec.permission)
            ]
            tools.extend(
                (
                    user_discovery_tool_schema(),
                    user_execution_tool_schema(available_specs),
                )
            )

        if is_admin:
            available_admin_specs = [
                spec
                for spec in CAPABILITY_BY_CODE.values()
                if not spec.permission or self._has_permission(spec.permission)
            ]
            tools.extend(
                (
                    discovery_tool_schema(),
                    execution_tool_schema(available_admin_specs),
                )
            )

        for schema in SkillRegistry.instance().list_tools(invocable_only=True):
            function = schema.get("function") if isinstance(schema, Mapping) else None
            if not isinstance(function, Mapping):
                continue
            original = str(function.get("name") or "")
            if not original:
                continue
            if not can_configure_agents:
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

        if is_admin and self._has_permission(PermissionCode.SERVER_OPS_VIEW):
            if agent_governance_service.is_runtime_enabled(self._db, "operations"):
                tools.append(_operations_tool_schema())
        # MCP 是外部动态能力，无法仅凭工具名证明它不会触达宿主机或其他
        # 基础设施；因此只向唯一超级管理员发现，普通管理员按最小权限失败关闭。
        if self._is_super_admin or bool(
            getattr(self._mcp, "supports_user_scoped_managed_tools", False)
        ):
            tools.extend(await self._mcp.discover())
        return tools

    def _assert_session_active(self) -> None:
        if self._session_validator is not None and not self._session_validator():
            raise AgentSessionExpiredError("账号已在另一台设备登录，当前设备已下线")

    def _has_permission(self, permission_code: str) -> bool:
        """普通管理员继续管理程序内容，服务器权限只认唯一 admin 超管。"""

        if permission_code.startswith(rbac_service.SERVER_OPS_PERMISSION_PREFIX):
            return self._is_super_admin
        if str(getattr(self._user, "role", "")) in {"admin", "super_admin"}:
            return True
        return rbac_service.check_permission(self._db, self._user.id, permission_code)

    async def execute(self, call: ToolCall, *, approved: bool = False) -> ToolExecutionResult:
        self._assert_session_active()
        if call.name.startswith(_ADMIN_TOOL_PREFIX) and not self._is_admin:
            return await self._failed_attempt(call, "当前用户没有管理员工具权限")
        if call.name in _SUPER_ADMIN_FIXED_TOOLS and not self._is_super_admin:
            return await self._failed_attempt(call, "仅超级管理员 admin 可使用服务器工具")
        is_managed_mcp = bool(
            getattr(self._mcp, "is_managed_tool", lambda _tool_name: False)(call.name)
        )
        if call.name.startswith("mcp_") and not self._is_super_admin and not is_managed_mcp:
            return await self._failed_attempt(call, "仅超级管理员 admin 可使用外部 MCP 工具")
        if call.name == "trigger_evolution" and not self._has_permission(PermissionCode.AGENT_CONFIGURE):
            return await self._failed_attempt(call, "当前用户缺少 Agent 配置权限")
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
        elif call.name in _USER_CAPABILITY_NAMES:
            try:
                model_by_name = {
                    "update_project": UpdateProjectArguments,
                    "import_remote_project": ImportRemoteProjectArguments,
                    "download_project_source": DownloadProjectSourceArguments,
                    "download_report": DownloadReportArguments,
                    "download_code_file": DownloadCodeFileArguments,
                    "start_roundtable_discussion": StartRoundtableDiscussionArguments,
                    "get_roundtable_discussion": GetRoundtableDiscussionArguments,
                    "control_roundtable_discussion": ControlRoundtableDiscussionArguments,
                }[call.name]
                arguments = model_by_name.model_validate(call.arguments).model_dump(exclude_unset=True)
            except Exception as exc:
                return await self._failed_attempt(call, f"工具 {call.name} 参数校验失败: {exc}")
            call = ToolCall(
                call_id=call.call_id,
                name=call.name,
                arguments=arguments,
                raw_arguments=call.raw_arguments,
                parse_error=call.parse_error,
            )
        if self._mcp.has_tool(call.name):
            requires_approval = getattr(
                self._mcp,
                "requires_approval",
                lambda _tool_name: True,
            )(call.name)
            if requires_approval and not approved:
                impact = (
                    "将使用当前登录用户权限执行项目内 MCP 能力"
                    if is_managed_mcp
                    else "将向已配置的 MCP Server 发送本次工具参数"
                )
                return self._approval(call, danger=False, impact=impact)
            if approved:
                self._mark_approval(call, approve=True)
            return await self._execute_once(
                call,
                lambda: self._mcp.call(call.name, call.arguments),
            )

        if call.name == "admin_describe_capabilities":
            return await self._describe_admin_capabilities(call)

        if call.name == "admin_execute_capability":
            return await self._execute_admin_capability(call, approved=approved)

        if call.name == "user_describe_capabilities":
            return await self._describe_user_capabilities(call)

        if call.name == "user_execute_capability":
            return await self._execute_user_capability(call, approved=approved)

        if call.name in self._skill_bindings:
            if not self._has_permission(PermissionCode.AGENT_CONFIGURE):
                return await self._failed_attempt(call, "当前用户缺少 Agent 配置权限")
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

        if call.name == "download_report":
            return await self._execute_once(call, lambda: self._download_report(call))

        if call.name == "download_code_file":
            return await self._execute_once(call, lambda: self._download_code_file(call))

        if call.name == "get_roundtable_discussion":
            return await self._execute_once(call, lambda: self._get_roundtable_discussion(call))

        if call.name in {"start_roundtable_discussion", "control_roundtable_discussion"}:
            if not approved:
                operation = "启动圆桌讨论" if call.name == "start_roundtable_discussion" else "控制圆桌讨论"
                return self._approval(
                    call,
                    danger=False,
                    operation=operation,
                    impact="将以当前登录用户身份变更圆桌讨论运行状态",
                )
            self._mark_approval(call, approve=True)
            if call.name == "start_roundtable_discussion":
                return await self._execute_once(call, lambda: self._start_roundtable_discussion(call))
            return await self._execute_once(call, lambda: self._control_roundtable_discussion(call))

        if call.name in _USER_CAPABILITY_NAMES:
            if call.name in _WRITE_TOOLS and not approved:
                return self._approval(
                    call,
                    danger=False,
                    impact="将使用当前登录用户权限执行项目写操作",
                )
            if call.name in _WRITE_TOOLS:
                self._mark_approval(call, approve=True)
            return await self._execute_once(
                call,
                lambda: self._agent_result(
                    call,
                    getattr(self._orch, call.name)(
                        **call.arguments,
                        ctx=AgentContext(user_id=self._user.id, extra={"run_id": self._run_id}),
                    ),
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

    def _download_report(self, call: ToolCall) -> ToolExecutionResult:
        """校验格式权限与报告归属后，返回固定同源下载入口。"""

        task_id = int(call.arguments["task_id"])
        export_format = str(call.arguments.get("format") or "html")
        template_type = str(call.arguments.get("template_type") or "detailed")
        permission = {
            "json": PermissionCode.REPORT_EXPORT_JSON,
            "html": PermissionCode.REPORT_EXPORT_HTML,
            "pdf": PermissionCode.REPORT_EXPORT_PDF,
            "word": PermissionCode.REPORT_EXPORT_WORD,
        }[export_format]
        if not rbac_service.check_permission(self._db, self._user.id, permission):
            return ToolExecutionResult.failure(f"当前用户缺少权限: {permission}")
        try:
            report_service.get_report_detail(self._db, self._user, task_id)
        except Exception as exc:  # 与真实报告路由保持归属/存在性语义
            return ToolExecutionResult.failure(str(exc))
        query = urlencode({"format": export_format, "template_type": template_type})
        path = f"/api/reports/tasks/{task_id}/export?{query}"
        extension = "docx" if export_format == "word" else export_format
        return ToolExecutionResult.success(
            {
                "task_id": task_id,
                "format": export_format,
                "template_type": template_type,
                "file_name": f"review_report_{task_id}.{extension}",
                "download_path": path,
                "download_url": path,
                "authentication": "current_user",
            }
        )

    def _download_code_file(self, call: ToolCall) -> ToolExecutionResult:
        """校验文件下载权限与项目归属后，返回固定二进制下载入口。"""

        file_id = int(call.arguments["file_id"])
        if not rbac_service.check_permission(self._db, self._user.id, PermissionCode.FILE_DOWNLOAD):
            return ToolExecutionResult.failure(f"当前用户缺少权限: {PermissionCode.FILE_DOWNLOAD}")
        try:
            code_file = code_file_service.get_file(self._db, self._user, file_id)
        except Exception as exc:  # 与真实下载路由保持归属/存在性语义
            return ToolExecutionResult.failure(str(exc))
        if int(getattr(code_file, "is_binary", 0) or 0) != 1:
            return ToolExecutionResult.failure("当前单文件下载路由仅支持二进制文件；文本源码请下载项目源码 ZIP")
        path = f"/api/code-files/{file_id}/download"
        return ToolExecutionResult.success(
            {
                "file_id": file_id,
                "file_name": str(getattr(code_file, "file_name", "") or ""),
                "download_path": path,
                "download_url": path,
                "authentication": "current_user",
            }
        )

    async def _start_roundtable_discussion(self, call: ToolCall) -> ToolExecutionResult:
        """复用用户 REST 预检后，在后台启动同一套圆桌编排器。"""

        from app.api.v1.discussion import start_discussion
        from app.api.v1.ws_discussion import launch_pending_discussion, take_pending

        response = start_discussion(
            project_id=int(call.arguments["project_id"]),
            file_id=int(call.arguments["file_id"]),
            review_type=str(call.arguments.get("review_type") or "full"),
            db=self._db,
            user=self._user,
        )
        data = dict(response.data or {})
        session_id = str(data.get("session_id") or "")
        pending = take_pending(session_id)
        if not session_id or pending is None:
            return ToolExecutionResult.failure("圆桌讨论上下文创建失败")
        launch_pending_discussion(pending)
        open_query = urlencode(
            {
                "discuss_session": session_id,
                "discuss_ws": str(data.get("ws_url") or ""),
                "discuss_agents": json.dumps(data.get("agents") or [], ensure_ascii=False),
                "discuss_file": str(data.get("file_name") or ""),
            }
        )
        data.update(
            {
                "started_by": "user_agent",
                "open_path": f"/agents?{open_query}",
                "open_url": f"/agents?{open_query}",
            }
        )
        return ToolExecutionResult.success(data)

    def _get_roundtable_discussion(self, call: ToolCall) -> ToolExecutionResult:
        """按会话归属返回圆桌状态与已产生的发言。"""

        from app.agents.discussion_bus import DiscussionBus

        session_id = str(call.arguments["session_id"])
        session = DiscussionBus.instance().get_session(session_id)
        if session is None:
            return ToolExecutionResult.failure("圆桌讨论不存在或已过期")
        if int(session.owner_user_id) != int(self._user.id) and not _is_admin_actor(self._db, self._user):
            return ToolExecutionResult.failure("无权访问该圆桌讨论")
        return ToolExecutionResult.success(
            {
                "session_id": session.session_id,
                "status": session.status,
                "file_name": session.file_name,
                "max_rounds": session.max_rounds,
                "report_task_id": session.report_task_id,
                "turn_count": len(session.turns),
                "turns": [turn.to_dict() for turn in session.turns[-100:]],
            }
        )

    def _control_roundtable_discussion(self, call: ToolCall) -> ToolExecutionResult:
        """按会话归属发送暂停、恢复、停止或用户发言。"""

        from app.agents.discussion_bus import DiscussionBus
        from app.agents.events import DiscussionTurn

        session_id = str(call.arguments["session_id"])
        action = str(call.arguments["action"])
        content = str(call.arguments.get("content") or "").strip()
        bus = DiscussionBus.instance()
        session = bus.get_session(session_id)
        if session is None:
            return ToolExecutionResult.failure("圆桌讨论不存在或已过期")
        if int(session.owner_user_id) != int(self._user.id) and not _is_admin_actor(self._db, self._user):
            return ToolExecutionResult.failure("无权控制该圆桌讨论")
        if session.status == "concluded":
            return ToolExecutionResult.failure("圆桌讨论已经结束")
        if action == "user_input":
            if not content:
                return ToolExecutionResult.failure("user_input 必须提供非空 content")
            bus.publish_turn(
                session_id,
                DiscussionTurn(
                    turn_id=-1,
                    agent_code="user",
                    agent_name="你",
                    role="user",
                    content=content,
                ),
            )
            accepted = bus.send_user_input(session_id, content)
        else:
            accepted = bus.control_session(session_id, action)
        if not accepted:
            return ToolExecutionResult.failure("讨论尚未启动或控制器已结束")
        return ToolExecutionResult.success(
            {
                "session_id": session_id,
                "action": action,
                "accepted": True,
                "status": session.status,
            }
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
            if not _execution_row_matches_call(
                row,
                call,
                request_id=request_id,
                run_id=self._run_id,
                user_id=int(self._user.id),
            ):
                recorded = ToolExecutionResult.failure(
                    "相同运行和调用标识对应的工具或参数不一致，已阻止复用既有执行结果"
                )
                await self._emit_tool_result(call, recorded, cached=True)
                return recorded
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
            if not _execution_row_matches_call(
                existing,
                call,
                request_id=request_id,
                run_id=self._run_id,
                user_id=int(self._user.id),
            ):
                recorded = ToolExecutionResult.failure(
                    "相同运行和调用标识对应的工具或参数不一致，已阻止复用既有执行结果"
                )
                await self._emit_tool_result(call, recorded, cached=True)
                return recorded
            recorded = self._recorded_execution(existing)
            await self._emit_tool_result(call, recorded, cached=True)
            return recorded

        try:
            # 登录版本可能在占位账本提交后变化；真正触发任何外部副作用前
            # 必须再次校验，旧设备不能利用校验与执行之间的窗口。
            self._assert_session_active()
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

            specs = [
                spec
                for spec in CAPABILITY_BY_CODE.values()
                if not spec.permission or self._has_permission(spec.permission)
            ]
            rows = describe_capabilities(app.openapi(), page=page, query=query, specs=specs)
            if not rows:
                return ToolExecutionResult.failure("没有找到匹配的管理能力")
            return ToolExecutionResult.success({"count": len(rows), "items": rows})

        return await self._execute_once(call, discover)

    async def _describe_user_capabilities(self, call: ToolCall) -> ToolExecutionResult:
        if self._surface != "user":
            return await self._failed_attempt(call, "普通用户页面能力只能在用户 Agent 中查询")
        unknown = sorted(set(call.arguments) - {"page", "query"})
        if unknown:
            return await self._failed_attempt(call, f"能力查询不接受参数: {', '.join(unknown)}")
        page = str(call.arguments.get("page") or "")
        query = str(call.arguments.get("query") or "")

        async def discover() -> ToolExecutionResult:
            from app.main import app

            rows = describe_user_capabilities(app.openapi(), page=page, query=query)
            legacy_admin = str(getattr(self._user, "role", "")) in {"admin", "super_admin"}
            available = [
                row
                for row in rows
                if row["permission"] == "route_enforced"
                or legacy_admin
                or rbac_service.check_permission(
                    self._db,
                    self._user.id,
                    str(row["permission"]),
                )
            ]
            if not available:
                return ToolExecutionResult.failure("当前用户没有匹配的可用页面能力")
            return ToolExecutionResult.success({"count": len(available), "items": available})

        return await self._execute_once(call, discover)

    async def _execute_user_capability(
        self,
        call: ToolCall,
        *,
        approved: bool,
    ) -> ToolExecutionResult:
        if self._surface != "user":
            return await self._failed_attempt(call, "普通用户页面能力只能在用户 Agent 中执行")
        unknown = sorted(set(call.arguments) - {"capability", "params"})
        if unknown:
            return await self._failed_attempt(call, f"用户能力工具不接受参数: {', '.join(unknown)}")
        capability = str(call.arguments.get("capability") or "")
        spec = USER_CAPABILITY_BY_CODE.get(capability)
        if spec is None:
            return await self._failed_attempt(call, f"未注册的用户页面能力: {capability}")
        raw_params = call.arguments.get("params", {})
        if not isinstance(raw_params, Mapping):
            return await self._failed_attempt(call, "用户能力 params 必须是 JSON object")
        params = dict(raw_params)

        legacy_admin = str(getattr(self._user, "role", "")) in {"admin", "super_admin"}
        if (
            spec.permission
            and not legacy_admin
            and not rbac_service.check_permission(self._db, self._user.id, spec.permission)
        ):
            return await self._failed_attempt(call, f"当前用户缺少权限: {spec.permission}")

        from app.main import app

        try:
            admin_capability_service.prepare_request(spec, params, app.openapi())  # type: ignore[arg-type]
        except admin_capability_service.AdminCapabilityError as exc:
            return await self._failed_attempt(call, str(exc))

        policy = tool_gateway.authorize(
            self._db,
            agent_code="chat_assistant",
            tool_code="user_execute_capability",
            action=f"user.{spec.code}",
            resource=spec.page,
            actor=self._user,
            context={"copilot_request_id": _request_id(self._run_id, call.call_id), "surface": self._surface},
        )
        if policy.decision == policy_engine.DENY:
            return await self._failed_attempt(call, f"策略阻断用户页面能力: {policy.reason}")

        if spec.risk != USER_CAPABILITY_READ and not approved:
            return self._approval(
                call,
                danger=spec.risk == USER_CAPABILITY_CRITICAL,
                operation=spec.description,
                impact=f"将以当前登录用户身份在 {spec.page} 执行「{spec.description}」",
                preview={
                    "capability": spec.code,
                    "page": spec.page,
                    "risk": spec.risk,
                    "params": _redact_event_value(params),
                },
            )
        if spec.risk != USER_CAPABILITY_READ:
            self._mark_approval(call, approve=True)

        async def execute_capability() -> ToolExecutionResult:
            try:
                output = await admin_capability_service.execute_api(
                    self._user,
                    spec,  # type: ignore[arg-type]
                    params,
                    request_id=_request_id(self._run_id, call.call_id),
                )
            except admin_capability_service.AdminCapabilityError as exc:
                return ToolExecutionResult.failure(str(exc))
            return ToolExecutionResult.success(output)

        return await self._execute_once(call, execute_capability)

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
        if spec.permission and not self._has_permission(spec.permission):
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
            password = data.get("temporary_password")
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
        if not self._is_super_admin:
            return ToolExecutionResult.failure("仅超级管理员 admin 可执行运维工具")
        action = str(call.arguments.get("action") or "")
        if action not in ops_service.ACTION_RISKS:
            return ToolExecutionResult.failure(f"不支持的运维动作: {action}")
        if not agent_governance_service.is_runtime_enabled(self._db, "operations"):
            return ToolExecutionResult.failure("全服管理 Agent 当前已停用")
        if not self._has_permission(PermissionCode.SERVER_OPS_VIEW):
            return ToolExecutionResult.failure("当前管理员没有服务器运维查看权限")
        if action not in _OPS_READ_ONLY and not self._has_permission(PermissionCode.SERVER_OPS_EXECUTE):
            return ToolExecutionResult.failure("当前管理员没有服务器运维执行权限")
        if ops_service.ACTION_RISKS[action] == "critical" and not self._has_permission(
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
        return _persisted_tool_arguments(call.name, call.arguments)

    @staticmethod
    def _agent_result(call: ToolCall, result: Any) -> ToolExecutionResult:
        if getattr(result, "success", False):
            return ToolExecutionResult.success(getattr(result, "data", None))
        return ToolExecutionResult.failure(str(getattr(result, "error", "") or f"工具 {call.name} 执行失败"))


class AgentResponsesService:
    """构建并驱动一次用户隔离的 Agent Responses 运行。"""

    def __init__(
        self,
        db: Session,
        user: User,
        *,
        surface: str,
        session_key: str,
        session_validator: Optional[SessionValidator] = None,
    ) -> None:
        if surface not in {"user", "admin"}:
            raise ValueError("surface 必须是 user 或 admin")
        if surface == "admin" and not _is_admin_actor(db, user):
            raise PermissionError("仅管理员可使用管理员 Agent")
        self._db = db
        self._user = user
        self._surface = surface
        self._session_key = session_key
        self._session_validator = session_validator
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
        await self._emit_validated_output(event_sink, result)
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
        await self._emit_validated_output(event_sink, result)
        return result

    async def cancel(self, run_id: str) -> None:
        """把因登录版本失效而取消的运行持久化为终态。"""

        checkpoint = await self._store.load(run_id)
        if checkpoint is None:
            return
        checkpoint.status = "cancelled"
        checkpoint.pending = None
        await self._store.save(checkpoint)

    async def _emit_validated_output(
        self,
        event_sink: Optional[EventSink],
        result: RuntimeResult,
    ) -> None:
        if result.status != COMPLETED or not result.output_text:
            return
        await _emit(event_sink, {"type": "response.output_text.delta", "delta": result.output_text})

    async def _runtime(
        self,
        run_id: str,
        event_sink: Optional[EventSink],
    ) -> tuple[PrismToolExecutor, DeepSeekResponsesRuntime]:
        config = resolve_api_config(self._db, self._user.id)
        mcp = McpToolProvider(
            db=self._db,
            agent_code="manager" if self._surface == "admin" else "chat_assistant",
            user=self._user,
        )
        executor = PrismToolExecutor(
            self._db,
            self._user,
            surface=self._surface,
            run_id=run_id,
            mcp_provider=mcp,
            event_sink=event_sink,
            session_validator=self._session_validator,
        )
        # 工具事件必须先于结论文本到达用户端。DeepSeek 可能在工具调用前
        # 产生 output_text.delta，因此所有 surface 都先缓冲文本，完成后统一发出。
        transport_sink = _buffer_text_sink(event_sink)
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
        transcript_evidence = _transcript_admin_write_evidence(checkpoint.transcript)
        ledger_evidence = _ledger_admin_write_evidence(
            self._db,
            user_id=int(self._user.id),
            checkpoint=checkpoint,
        )
        # 成功必须由持久化审计账本证明；回传文本只能证明失败或拒绝。
        evidence = dict(ledger_evidence)
        for call_id, terminal in transcript_evidence.items():
            if terminal[1] != "success" and call_id not in evidence:
                evidence[call_id] = terminal
        return _admin_completion_guard(
            checkpoint,
            output_text,
            write_evidence=evidence,
        )


async def _emit(sink: Optional[EventSink], event: Mapping[str, Any]) -> None:
    if sink is None:
        return
    result = sink(copy.deepcopy(dict(event)))
    if inspect.isawaitable(result):
        await result


def _buffer_text_sink(sink: Optional[EventSink]) -> EventSink:
    async def filtered(event: Mapping[str, Any]) -> None:
        if str(event.get("type") or "") == "response.output_text.delta":
            return
        await _emit(sink, event)

    return filtered


# 兼容既有单元测试和外部导入；语义已扩展为所有 surface。
_buffer_admin_text_sink = _buffer_text_sink


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
    return _redact_sensitive_text_unbounded(value)


def _redact_sensitive_text_unbounded(value: str) -> str:
    """脱敏用户可见长文本，但不套用工具事件的长度上限。"""
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


def redact_agent_output_text(value: str, *, limit: int = 100_000) -> str:
    """恢复用户可见的模型文本，保留长输出并继续清除敏感信息。"""
    return _redact_sensitive_text_unbounded(value)[:limit]


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
    from app.services.page_guide_service import admin_guide_block, user_guide_block

    if surface == "admin":
        identity = "Prism 管理员 Agent「小菱」"
        capability_instruction = (
            "管理员界面任务必须先调用 admin_describe_capabilities 查询对应页面能力和精确参数，"
            "再调用 admin_execute_capability；不得猜测能力编码或参数。"
        )
        role_behavior = (
            "管理员处理 Agent 发布审批前必须先查询完整详情，展示修改前后内容、依赖、测试证据和风险，再申请执行决策。"
            "面向批量处理与批量分析时先说明影响范围再执行。"
        )
        guide_block = admin_guide_block()
    else:
        identity = "Prism 棱镜小助「小菱」"
        capability_instruction = (
            "普通用户页面任务必须先调用 user_describe_capabilities 查询对应页面能力和精确参数，"
            "再调用 user_execute_capability；不得猜测能力编码或参数。"
            "报告、二进制单文件和项目源码下载必须分别使用 download_report、"
            "download_code_file 和 download_project_source 固定工具。"
            "圆桌讨论必须使用 start_roundtable_discussion、get_roundtable_discussion "
            "和 control_roundtable_discussion 固定工具。"
        )
        role_behavior = "审查结论必须引用本次工具返回的真实数据，不得凭印象作答。"
        guide_block = user_guide_block()
    return (
        f"你是 {identity}。所有事实查询和操作必须使用已提供工具；不要编造工具结果，也不要声称未执行的动作已完成。"
        "根据每次工具返回结果自主判断下一步，可以连续调用多个工具。"
        "缺少真正阻断任务的信息时调用 ask_user，问题、候选项及其说明必须由你根据当前任务动态生成，不使用预设问题。"
        "涉及名称近义表达或自定义 Agent 能力时先调用 search_published_agents；候选不唯一时用 ask_user "
        "展示动态候选，确认后才能调用 invoke_published_agent。"
        "涉及用户批量操作时必须先查询真实用户。用户说序号、第几条或范围而未明确是用户 ID 时，"
        "不得猜测；必须用 ask_user 区分列表序号与用户 ID，得到精确 user_ids 后再调用批量工具。"
        f"{capability_instruction}"
        f"{role_behavior}"
        "写操作由系统暂停并展示审批；用户点击批准后系统会把原调用结果自动交还给你，不要要求用户重复发送指令。"
        "使用中文直接给出结果，不使用预设套话，不输出空白行；代码块内部格式保持原样。\n\n"
        f"{guide_block}"
    )


def _admin_completion_guard(
    checkpoint: RunCheckpoint,
    output_text: str,
    *,
    write_evidence: Optional[Mapping[str, tuple[str, str]]] = None,
) -> Optional[str]:
    """要求当前运行的每个写调用都有逐调用终态证据。"""

    requested_capabilities = _requested_admin_write_capabilities(checkpoint.transcript)
    claims_success = _claims_mutation_success(output_text)
    claims_failure = _claims_mutation_failure(output_text)
    attempted_calls = _admin_write_calls(checkpoint.transcript)
    attempted_capabilities = {call.code for call in attempted_calls.values()}
    mutation_requested = _requests_admin_mutation(checkpoint.transcript)
    if not mutation_requested and not attempted_calls:
        return None
    if not attempted_calls:
        return "管理写请求在没有精确工具执行证据时就结束了"

    evidence = (
        _transcript_admin_write_evidence(checkpoint.transcript) if write_evidence is None else dict(write_evidence)
    )
    missing_calls = sorted(call_id for call_id in attempted_calls if call_id not in evidence)
    mismatched_calls = sorted(
        call_id
        for call_id, expected_call in attempted_calls.items()
        if call_id in evidence and evidence[call_id][0] != expected_call.code
    )
    non_successful_calls = sorted(
        call_id for call_id in attempted_calls if call_id in evidence and evidence[call_id][1] != "success"
    )
    missing_capabilities = sorted(requested_capabilities - attempted_capabilities)
    details = missing_calls + mismatched_calls + missing_capabilities
    if claims_success:
        if not details and not non_successful_calls:
            return None
        labels = details + [f"{call_id}(未成功)" for call_id in non_successful_calls]
        return f"回复声称管理写操作已完成，但当前运行缺少逐调用成功证据: {', '.join(sorted(set(labels)))}"

    # 只有与真实失败工具证据对应的失败陈述才可结束；泛化的“已处理/操作完成”不能掩盖失败。
    if claims_failure:
        if not details and non_successful_calls:
            return None
        labels = details + (["缺少失败工具证据"] if not non_successful_calls else [])
        return f"回复声称管理写操作失败，但当前运行缺少逐调用失败证据: {', '.join(sorted(set(labels)))}"
    if not details and not non_successful_calls:
        return None
    labels = details + [f"{call_id}(未成功)" for call_id in non_successful_calls]
    return f"管理写请求在没有精确工具执行证据时就结束了: {', '.join(sorted(set(labels)))}"


def _claims_mutation_success(output_text: str) -> bool:
    if not output_text:
        return False
    return not _claims_mutation_failure(output_text) and any(
        pattern.search(output_text) for pattern in _MUTATION_SUCCESS_PATTERNS
    )


def _claims_mutation_failure(output_text: str) -> bool:
    return bool(output_text) and any(pattern.search(output_text) for pattern in _MUTATION_FAILURE_PATTERNS)


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
            latest_user_text = " ".join(str(part.get("text") or "") for part in content if isinstance(part, Mapping))
    return latest_user_text.strip()


def _requests_admin_mutation(transcript: Sequence[Mapping[str, Any]]) -> bool:
    text = _latest_user_text(transcript)
    if not text:
        return False
    # “能否/可否/麻烦”是礼貌祈使句，不应被疑问句规则当成纯讨论。
    if _ADMIN_POLITE_MUTATION_REQUEST.search(text):
        return True
    if _ADMIN_MUTATION_DISCUSSION.search(text):
        return False
    if _ADMIN_MUTATION_REQUEST.search(text):
        return True
    # 能力码本身不是执行授权；只有同时出现明确动作词时才算写命令。
    return bool(
        _requested_admin_write_capabilities(transcript)
        and re.search(
            _CN_MUTATION_VERB,
            text,
        )
    )


def _admin_write_calls(transcript: Sequence[Mapping[str, Any]]) -> dict[str, _AdminWriteCall]:
    calls: dict[str, _AdminWriteCall] = {}
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
                write_code = _admin_write_code(name, arguments)
                if not write_code:
                    continue
                candidate = _AdminWriteCall(
                    code=write_code,
                    tool_name=name,
                    arguments_json=_canonical_json(_persisted_tool_arguments(name, arguments)),
                )
                previous = calls.get(call_id)
                if previous is None:
                    calls[call_id] = candidate
                elif (
                    previous.code != candidate.code
                    or previous.tool_name != candidate.tool_name
                    or previous.arguments_json != candidate.arguments_json
                ):
                    # 同一 call_id 出现不同参数时，任何一条结果都不能证明当前调用成功。
                    calls[call_id] = _AdminWriteCall(
                        code=previous.code,
                        tool_name=previous.tool_name,
                        arguments_json="",
                        invalid=True,
                    )
    return calls


def _transcript_admin_write_evidence(
    transcript: Sequence[Mapping[str, Any]],
) -> dict[str, tuple[str, str]]:
    calls = _admin_write_calls(transcript)
    evidence: dict[str, tuple[str, str]] = {}
    for item in transcript:
        if str(item.get("type") or "") != "function_call_output":
            continue
        call_id = str(item.get("call_id") or "")
        write_call = calls.get(call_id)
        if not write_call or write_call.invalid:
            continue
        raw_output = item.get("output")
        try:
            output = json.loads(raw_output) if isinstance(raw_output, str) else raw_output
        except json.JSONDecodeError:
            output = {}
        if not isinstance(output, Mapping):
            continue
        status = str(output.get("status") or "").casefold()
        if status == "success":
            evidence[call_id] = (write_call.code, "success")
        elif status in {"error", "failed", "rejected", "denied", "cancelled", "canceled"}:
            terminal_status = "rejected" if status in {"rejected", "denied", "cancelled", "canceled"} else "failed"
            evidence[call_id] = (write_call.code, terminal_status)
    return evidence


def _ledger_admin_write_evidence(
    db: Session,
    *,
    user_id: int,
    checkpoint: RunCheckpoint,
) -> dict[str, tuple[str, str]]:
    call_codes = _admin_write_calls(checkpoint.transcript)
    if not call_codes:
        return {}
    rows = (
        db.query(AgentToolExecution)
        .filter(
            AgentToolExecution.run_id == checkpoint.run_id,
            AgentToolExecution.user_id == user_id,
            AgentToolExecution.call_id.in_(tuple(call_codes)),
        )
        .all()
    )
    evidence: dict[str, tuple[str, str]] = {}
    for row in rows:
        expected_call = call_codes.get(str(row.call_id))
        if not expected_call or expected_call.invalid:
            continue
        if (
            row.tool_name != expected_call.tool_name
            or _canonical_json_text(row.arguments_json) != expected_call.arguments_json
            or row.request_id != _request_id(checkpoint.run_id, str(row.call_id))
            or row.status == "executing"
        ):
            continue
        evidence[str(row.call_id)] = (
            expected_call.code,
            "success" if row.status == "success" else "failed",
        )
    return evidence


def _admin_write_code(name: str, arguments: Mapping[str, Any]) -> str:
    if name == "admin_execute_capability":
        spec = CAPABILITY_BY_CODE.get(str(arguments.get("capability") or ""))
        return spec.code if spec is not None and spec.risk != CAPABILITY_READ else ""
    if name == "admin_execute_operation":
        action = str(arguments.get("action") or "")
        return f"operations.{action}" if action and action not in _OPS_READ_ONLY else ""
    return name if name in _WRITE_TOOLS else ""


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def _canonical_json_text(value: Any) -> str:
    try:
        parsed = json.loads(value or "{}") if isinstance(value, str) else value
    except (TypeError, json.JSONDecodeError):
        return ""
    if not isinstance(parsed, Mapping):
        return ""
    return _canonical_json(parsed)


def _argument_fingerprint(value: Any) -> str:
    digest = hmac.new(
        str(settings.jwt_secret).encode("utf-8"),
        f"agent-tool-argument-v1\0{_canonical_json(value)}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return f"[REDACTED]:hmac-sha256:{digest}"


def _audit_persisted_value(value: Any, *, key: str = "") -> Any:
    """完整保留参数身份；敏感值只持久化服务端 HMAC 指纹。"""

    if _SENSITIVE_EVENT_KEY.search(key):
        return _argument_fingerprint(value)
    if isinstance(value, Mapping):
        return {
            str(item_key): _audit_persisted_value(item_value, key=str(item_key))
            for item_key, item_value in value.items()
        }
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_audit_persisted_value(item) for item in value]
    if isinstance(value, str):
        redacted = _redact_sensitive_text(value)
        if redacted != value:
            return f"{redacted} {_argument_fingerprint(value)}"
        return value
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return str(value)


def _persisted_tool_arguments(name: str, arguments: Mapping[str, Any]) -> Dict[str, Any]:
    """返回与执行账本相同的脱敏参数身份。"""
    persisted = dict(arguments)
    if name == "admin_execute_operation":
        action = str(persisted.get("action") or "")
        params = persisted.get("params") if isinstance(persisted.get("params"), dict) else {}
        try:
            persisted["params"] = ops_service.audit_action_params(action, dict(params))
        except (KeyError, TypeError, ValueError):
            persisted["params"] = dict(params)
    audited = _audit_persisted_value(persisted)
    return dict(audited) if isinstance(audited, Mapping) else {}


def _execution_row_matches_call(
    row: AgentToolExecution,
    call: ToolCall,
    *,
    request_id: str,
    run_id: str,
    user_id: int,
) -> bool:
    expected = _canonical_json(_persisted_tool_arguments(call.name, call.arguments))
    return (
        row.request_id == request_id
        and row.run_id == run_id
        and str(row.call_id) == call.call_id
        and int(row.user_id) == user_id
        and row.tool_name == call.name
        and _canonical_json_text(row.arguments_json) == expected
    )


def _operations_tool_schema() -> Dict[str, Any]:
    return {
        "type": "function",
        "name": "admin_execute_operation",
        "description": (
            "仅超级管理员可用的结构化服务器运维工具。查询路径或服务前必须先调用 host_inventory；"
            "后续只能使用清单返回的真实绝对路径和单元名，禁止猜测路径、绕过软链接限制或对安全拒绝换路径重试。"
            "有副作用的动作会先等待用户批准。"
        ),
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


def _user_capability_schemas() -> list[Dict[str, Any]]:
    """用户页面对应的固定项目能力契约。"""
    return [
        {
            "type": "function",
            "name": "update_project",
            "description": "编辑当前用户有权限项目的名称、描述、语言或状态",
            "parameters": UpdateProjectArguments.model_json_schema(),
        },
        {
            "type": "function",
            "name": "import_remote_project",
            "description": "导入公开 HTTPS 源码归档并创建项目；执行前需要用户批准",
            "parameters": ImportRemoteProjectArguments.model_json_schema(),
        },
        {
            "type": "function",
            "name": "download_project_source",
            "description": "生成当前用户可访问项目的完整源码 ZIP 下载地址",
            "parameters": DownloadProjectSourceArguments.model_json_schema(),
        },
        {
            "type": "function",
            "name": "download_report",
            "description": "校验当前用户权限后，生成 JSON、HTML、PDF 或 Word 报告的同源下载地址",
            "parameters": DownloadReportArguments.model_json_schema(),
        },
        {
            "type": "function",
            "name": "download_code_file",
            "description": "校验当前用户权限后，生成二进制代码文件的同源下载地址",
            "parameters": DownloadCodeFileArguments.model_json_schema(),
        },
        {
            "type": "function",
            "name": "start_roundtable_discussion",
            "description": "为当前用户可访问的单个源码文件启动多 Agent 圆桌讨论；执行前需要批准",
            "parameters": StartRoundtableDiscussionArguments.model_json_schema(),
        },
        {
            "type": "function",
            "name": "get_roundtable_discussion",
            "description": "查询当前用户圆桌讨论的状态、发言与沉淀报告任务",
            "parameters": GetRoundtableDiscussionArguments.model_json_schema(),
        },
        {
            "type": "function",
            "name": "control_roundtable_discussion",
            "description": "暂停、恢复、停止圆桌讨论或提交用户发言；执行前需要批准",
            "parameters": ControlRoundtableDiscussionArguments.model_json_schema(),
        },
    ]


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
