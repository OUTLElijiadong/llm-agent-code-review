"""Prism 普通用户与管理员共用的 Responses Agent 运行适配层。"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import hmac
import inspect
import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, AsyncIterator, Awaitable, Callable, Dict, Literal, Mapping, Optional, Sequence
from urllib.parse import urlencode

import httpx
from sqlalchemy import update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.agents import AgentContext
from app.agents.orchestrator import get_request_orchestrator
from app.agents.skills.registry import SkillRegistry
from app.agents.tool_contracts import (
    DownloadProjectSourceArguments,
    FixedToolArgumentError,
    FixedToolArguments,
    UpdateProjectArguments,
    get_fixed_tool_description,
    get_fixed_tool_names,
    get_fixed_tool_schema,
    is_fixed_tool,
    validate_fixed_tool_arguments,
)
from app.core.config import settings
from app.core.exceptions import AppError
from app.core.observability import observe_event
from app.core.permission_codes import PermissionCode
from app.models.agent_governance import ApprovalItem
from app.models.agent_response_run import AgentResponseRun, AgentToolExecution
from app.models.user import User
from app.services import (
    admin_agent_tools,
    admin_capability_service,
    agent_governance_service,
    agent_knowledge_service,
    code_file_service,
    ops_service,
    policy_engine,
    published_agent_tools,
    rbac_service,
    report_service,
    sandbox_service,
    strategy_learning_service,
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
    CANCELLED,
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

logger = logging.getLogger(__name__)

EventSink = Callable[[Mapping[str, Any]], Optional[Awaitable[None]]]
SessionValidator = Callable[[], bool]
_FULL_VALIDATION_TERMINAL_STATES = frozenset({"succeeded", "failed", "blocked", "stopped", "expired"})
_FULL_VALIDATION_POLL_SECONDS = 2.0


class AgentSessionExpiredError(RuntimeError):
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


class QueueRemoteProjectImportArguments(FixedToolArguments):
    """创建可恢复远程导入任务的参数。"""

    url: str
    project_name: str
    description: str = ""
    language: Optional[str] = None
    audit_mode: bool = False


class GetRemoteProjectImportArguments(FixedToolArguments):
    """查询可恢复远程导入任务的参数。"""

    task_id: str


_ADMIN_TOOL_PREFIX = "admin_"
_SUPER_ADMIN_FIXED_TOOLS = frozenset({"admin_system_status"})
_SECURITY_SCAN_FIXED_TOOLS = frozenset({
    "audit_security_for_file",
    "audit_security_for_task",
    "audit_security_for_project",
})
_PENTEST_FIXED_TOOLS = frozenset({
    "create_pentest_engagement",
    "start_pentest_engagement",
    "get_pentest_status",
})
_WRITE_TOOLS = {
    "create_project",
    "update_project",
    "delete_project",
    "queue_remote_project_import",
    "start_roundtable_discussion",
    "control_roundtable_discussion",
    "start_review",
    "trigger_evolution",
    "change_own_password",
    "admin_set_user_role",
    "admin_delete_user",
    "admin_delete_users",
    "admin_toggle_agent",
    "admin_decide_agent_release",
    "admin_execute_operation",
    "save_knowledge_note",
    "start_pentest_engagement",
}
_DANGER_TOOLS = {
    "delete_project",
    "admin_delete_user",
    "admin_delete_users",
    "admin_execute_operation",
}
_USER_CAPABILITY_NAMES = {
    "update_project",
    "queue_remote_project_import",
    "get_remote_project_import",
    "download_project_source",
    "download_report",
    "download_code_file",
    "start_roundtable_discussion",
    "get_roundtable_discussion",
    "control_roundtable_discussion",
}
_CN_MUTATION_VERB = (
    r"(?:创建|新增|添加|修改|调整|编辑|更改|更新|设置|启用|停用|禁用|下线|"
    r"删除|移除|重置|生成|撤销|发布|批准|驳回|拒绝|回滚|写入|保存|上传|导入|重建|"
    r"绑定|分配|激活|抓取|运行|试算|解决|记录|登记|覆盖|评测|触发|调用|测试|检查|同步|沉淀|应用|"
    r"备份|校验|验证|重启|重载|续期|维护|恢复|清理|安装|升级|卸载|锁定|解锁|"
    r"暂停|启动|停止|开放|关闭)"
)
# 只读意图：明确查询/查看/统计/巡检类请求不算写请求（防止“未解决告警”中的
# “解决”被 _ADMIN_MUTATION_REQUEST 误判为写命令）。
_ADMIN_READ_INTENT = re.compile(
    r"(?:查询|查看|获取|读取|列出|统计|搜索|查找|展示|显示|看看|查一下|"
    r"有多少|几个|多少条|多少|数量)"
)
# 强写动词（排除“解决/恢复/运行/记录/测试/验证/应用/调用/维护/检查”等
# 在只读语境也常出现的歧义词）：出现时即使有只读词也不短路，避免误放行
# “查询后删除”这类复合写指令。
_ADMIN_CLEAR_WRITE_VERB = re.compile(
    r"(?<![已未待不])(?:创建|新增|添加|删除|移除|修改|调整|编辑|更改|更新|设置|启用|停用|禁用|下线|"
    r"重置|生成|撤销|发布|批准|驳回|拒绝|回滚|写入|保存|上传|导入|绑定|分配|激活|触发|"
    r"试算|评测|重启|重载|续期|清理|安装|升级|卸载|锁定|解锁|暂停|启动|停止|开放|关闭|"
    r"处置|执行|操作)"
)

_MUTATION_SUCCESS_PATTERNS = (
    re.compile(
        r"(?:成功(?:地)?|已经完成|已完成|完成了).{0,24}"
        + _CN_MUTATION_VERB
    ),
    re.compile(
        _CN_MUTATION_VERB + r".{0,16}(?:已成功完成|成功完成|已完成|成功|完成了)"
    ),
    re.compile(
        r"(?:已|已经)(?:成功)?" + _CN_MUTATION_VERB
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
    # 通用完成声明（写请求语境下，未调用写工具却称“操作完成/已处理”同样拦截）。
    # 只读请求已被 _ADMIN_READ_INTENT 短路，不会走到这里的成功声明判断。
    re.compile(r"(?:操作|处理|任务|事项|全部|所有|批量)?(?:已)?(?:完成|结束|处理好|搞定)"),
    re.compile(r"(?:已处理|已操作|已执行|处理完成|操作完成|执行完成|已完成)"),
)
_MUTATION_FAILURE_PATTERNS = (
    re.compile(
        r"(?:未(?:能|成功|完成)?|没(?:有)?|无法|不能|不会|失败|未执行|未完成)"
        r".{0,24}" + _CN_MUTATION_VERB
    ),
    re.compile(
        _CN_MUTATION_VERB + r".{0,24}(?:失败|被拒绝|已取消|未执行|未完成|未成功|无法|不能)"
    ),
    re.compile(r"(?:用户|审批|策略|系统).{0,16}(?:拒绝|取消).{0,16}(?:执行|操作|请求)"),
    re.compile(r"(?:请求|操作|执行).{0,12}(?:被拒绝|已取消)"),
    re.compile(r"(?:已取消|不会).{0,12}(?:执行|操作|" + _CN_MUTATION_VERB + r")"),
    re.compile(r"\b(?:failed|rejected|cancelled|canceled|denied|not completed)\b", re.I),
)
_ADMIN_MUTATION_REQUEST = re.compile(
    r"^(?:请(?!问)|请帮|帮我|帮忙|麻烦|劳烦|给我|现在|立即|马上|需要|我要|"
    r"请通过|请在|把|将|直接|执行).{0,80}"
    + _CN_MUTATION_VERB
    + r"|^"
    + _CN_MUTATION_VERB
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


def surface_agent_identity(surface: str) -> tuple[str, str]:
    """控制面/体验面身份分离(单一事实源)。

    管理端 = 贾维斯(manager, 全局运维); 成员端 = 小菱(chat_assistant)。
    AgentEventBus 事件归属、状态文案统一取这里, 防止管理端运行被记到小菱名下。
    """
    if surface == "admin":
        return "manager", "贾维斯"
    return "chat_assistant", "小菱"


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
    r"one[_-]?time[_-]?(?:code|result)s?|"
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
            self._db.commit()
            return
        self._assert_owner(row)
        # 取消是不可覆盖终态:取消请求与原始驱动循环分属不同请求实例,
        # 驱动循环可能稍后回写 waiting_approval/waiting_input。用条件 UPDATE
        # 保证并发交错下取消仍最终获胜(先取消者先提交即赢)。
        if row.status == CANCELLED and checkpoint.status != CANCELLED:
            return
        payload = json.dumps(checkpoint.to_dict(), ensure_ascii=False, default=str)
        updated = (
            self._db.query(AgentResponseRun)
            .filter(
                AgentResponseRun.run_id == checkpoint.run_id,
                AgentResponseRun.status != CANCELLED,
            )
            .update(
                {
                    "status": checkpoint.status,
                    "checkpoint_json": payload,
                    "version": AgentResponseRun.version + 1,
                },
                synchronize_session=False,
            )
        )
        self._db.commit()
        if updated == 0:
            # 并发取消已先落库;保持 cancelled 终态,放弃本次回写。
            return

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
        claimed_at = datetime.now(timezone.utc)
        result = self._db.execute(
            update(AgentResponseRun)
            .where(
                AgentResponseRun.run_id == run_id,
                AgentResponseRun.user_id == self._user_id,
                AgentResponseRun.surface == self._surface,
                AgentResponseRun.session_key == self._session_key,
                AgentResponseRun.status == expected_status,
            )
            .values(
                status=claimed_status,
                version=AgentResponseRun.version + 1,
                update_time=claimed_at,
            )
            .execution_options(synchronize_session=False)
        )
        if result.rowcount != 1:
            self._db.rollback()
            return None
        row = (
            self._db.query(AgentResponseRun)
            .filter(
                AgentResponseRun.run_id == run_id,
                AgentResponseRun.user_id == self._user_id,
                AgentResponseRun.surface == self._surface,
                AgentResponseRun.session_key == self._session_key,
            )
            .populate_existing()
            .one()
        )
        try:
            checkpoint = RunCheckpoint.from_dict(json.loads(row.checkpoint_json))
        except (TypeError, json.JSONDecodeError) as exc:
            self._db.rollback()
            raise InvalidRunStateError(f"运行 {run_id} 的检查点损坏") from exc
        if expected_status in {WAITING_APPROVAL, WAITING_INPUT} and checkpoint.pending is None:
            self._db.rollback()
            return None
        if tool_call_id and (
            checkpoint.pending is None
            or checkpoint.pending.call.call_id != tool_call_id
        ):
            self._db.rollback()
            return None
        checkpoint.status = claimed_status
        row.checkpoint_json = json.dumps(checkpoint.to_dict(), ensure_ascii=False, default=str)
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
        session_key: str = "",
        event_sink: Optional[EventSink] = None,
        session_validator: Optional[SessionValidator] = None,
    ) -> None:
        self._db = db
        self._user = user
        self._surface = surface
        self._run_id = run_id
        self._session_key = session_key
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
            self._db, self._user.id, PermissionCode.CUSTOM_AGENT_INVOKE,
        )
        can_view_projects = legacy_admin or rbac_service.check_permission(
            self._db, self._user.id, PermissionCode.PROJECT_VIEW,
        )
        can_scan_security = legacy_admin or rbac_service.check_permission(
            self._db,
            self._user.id,
            PermissionCode.SECURITY_SCAN,
        )
        can_pentest = legacy_admin or rbac_service.check_permission(
            self._db,
            self._user.id,
            PermissionCode.PENTEST_VIEW,
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
            if name in _PENTEST_FIXED_TOOLS and not can_pentest:
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
        if self._surface == "user" and not can_view_projects:
            user_fixed_schemas = [
                schema for schema in user_fixed_schemas
                if schema["name"] != "download_project_source"
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
            tools.extend((
                user_discovery_tool_schema(),
                user_execution_tool_schema(available_specs),
            ))

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

    def _agent_context(self) -> AgentContext:
        return AgentContext(
            user_id=self._user.id,
            extra={
                "run_id": self._run_id,
                "trace_id": self._run_id,
                "surface": self._surface,
                "session_key": self._session_key,
            },
        )

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
                    "queue_remote_project_import": QueueRemoteProjectImportArguments,
                    "get_remote_project_import": GetRemoteProjectImportArguments,
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
                    if getattr(self._mcp, "is_managed", False)
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

        if call.name == "run_full_project_validation":
            return await self._execute_once(call, lambda: self._run_full_project_validation(call))

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
                        self._agent_context(),
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

        if call.name == "create_pentest_engagement":
            if self._surface == "admin":
                return ToolExecutionResult.failure(
                    "渗透测试是成员侧业务, 由小菱编排; 请在成员端或渗透测试页面操作"
                )
            return await self._execute_once(call, lambda: self._create_pentest_engagement(call))

        if call.name == "get_pentest_status":
            if self._surface == "admin":
                return ToolExecutionResult.failure(
                    "渗透测试是成员侧业务, 请在成员端查询进度"
                )
            return await self._execute_once(call, lambda: self._get_pentest_status(call))

        # start_pentest_engagement 是写工具: 走与圆桌启动相同的内联审批门,
        # 不能放在通用 _WRITE_TOOLS 审批门之前(否则 _WRITE_TOOLS 成员资格失效)。
        if call.name == "start_pentest_engagement":
            if self._surface == "admin":
                return ToolExecutionResult.failure(
                    "渗透测试是成员侧业务, 由小菱编排; 请在成员端启动"
                )
            if not approved:
                return self._approval(
                    call,
                    danger=False,
                    operation="启动渗透测试",
                    impact="将以当前登录用户身份启动七阶段渗透测试流水线(消耗模型调用, 可能发起沙箱探测)",
                )
            self._mark_approval(call, approve=True)
            return await self._execute_once(call, lambda: self._start_pentest_engagement(call))

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
                        ctx=self._agent_context(),
                    ),
                ),
            )

        if call.name == "recall_knowledge":
            return await self._execute_once(call, lambda: self._recall_knowledge(call))

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
            # 唯一超级管理员:高危任务仍需确认,普通写操作免审批直接执行
            is_danger = call.name in _DANGER_TOOLS
            if not (self._is_super_admin and not is_danger):
                return self._approval(
                    call,
                    danger=is_danger,
                    impact="将使用当前登录用户权限执行该平台写操作",
                )
        if call.name in _WRITE_TOOLS and not (
            self._is_super_admin and call.name not in _DANGER_TOOLS
        ):
            self._mark_approval(call, approve=True)

        if call.name in {"admin_set_user_role", "admin_delete_user", "admin_toggle_agent"}:
            return await self._execute_once(call, lambda: self._execute_admin_write(call))
        if call.name == "save_knowledge_note":
            return await self._execute_once(call, lambda: self._save_knowledge_note(call))
        if call.name in _SECURITY_SCAN_FIXED_TOOLS:
            # 审计类工具:同步转发事件总线里的 fullchain 阶段事件为 SSE 进度
            # (侦察→分析→验证→汇报),前端渲染成通俗角色阶段卡
            return await self._execute_audit_with_progress(call)
        if call.name in _USER_CAPABILITY_NAMES:
            return await self._execute_once(
                call,
                lambda: self._agent_result(
                    call,
                    getattr(self._orch, call.name)(
                        **call.arguments,
                        ctx=self._agent_context(),
                    ),
                ),
            )
        return await self._execute_once(
            call,
            lambda: self._agent_result(
                call,
                self._orch.invoke_tool(
                    call.name,
                    call.arguments,
                    self._agent_context(),
                ),
            ),
        )

    # fullchain 审计四阶段的通俗文案(DeepAudit 式角色叙事,前端直接展示)
    _AUDIT_PHASE_LABELS = {
        "recon": "侦察员正在梳理攻击面",
        "analysis": "分析师正在逐文件挖掘漏洞",
        "verification": "验证员正在复核高危发现",
        "report": "汇报员正在整理审计报告",
    }

    async def _execute_audit_with_progress(self, call: ToolCall) -> ToolExecutionResult:
        """执行安全审计工具,并把事件总线里的 fullchain 阶段事件转发为 SSE 进度。

        审计在 orchestrator 内同步执行(跑线程池),这里并发订阅事件总线,
        把带 phase 的 PROGRESS 事件翻译成 response.audit.progress 推给前端,
        让用户看到「侦察→分析→验证→汇报」的角色化阶段而非黑盒等待。
        """
        from app.agents import event_bus as agent_event_bus
        from app.agents.events import AgentEventType

        async def _relay_audit_progress() -> None:
            try:
                async for ev in agent_event_bus.AgentEventBus.instance().subscribe():
                    if ev.type != AgentEventType.PROGRESS:
                        continue
                    phase = str((ev.payload or {}).get("phase") or "")
                    if phase not in self._AUDIT_PHASE_LABELS:
                        continue
                    await _emit(
                        self._event_sink,
                        {
                            "type": "response.audit.progress",
                            "call_id": call.call_id,
                            "phase": phase,
                            "message": str(ev.message or ""),
                            "label": self._AUDIT_PHASE_LABELS[phase],
                        },
                    )
            except asyncio.CancelledError:
                pass
            except Exception:
                # 转发失败不影响审计本体
                pass

        relay = asyncio.create_task(_relay_audit_progress())
        try:
            # 审计在事件循环内同步执行(如需线程池须先解决 sqlite 会话亲和),
            # 事件转发任务在审计让出点(await)之间仍可推送阶段进度
            return await self._execute_once(
                call,
                lambda: self._agent_result(
                    call,
                    self._orch.invoke_tool(
                        call.name,
                        call.arguments,
                        self._agent_context(),
                    ),
                ),
            )
        finally:
            relay.cancel()

    async def _run_full_project_validation(self, call: ToolCall) -> ToolExecutionResult:
        """创建一次组合沙箱并在同一固定工具调用中等待其真实终态。"""
        created = self._agent_result(
            call,
            self._orch.run_full_project_validation(
                **call.arguments,
                ctx=self._agent_context(),
            ),
        )
        if created.status != "success":
            return created
        if not isinstance(created.output, Mapping):
            return ToolExecutionResult.failure("全量验证未返回结构化沙箱结果")
        public_id = str(created.output.get("public_id") or "")
        if not public_id:
            return ToolExecutionResult.failure("全量验证未返回沙箱编号")

        state = dict(created.output)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + int(settings.agent_full_validation_wait_seconds)
        while str(state.get("status") or "") not in _FULL_VALIDATION_TERMINAL_STATES:
            self._assert_session_active()
            if loop.time() >= deadline:
                return ToolExecutionResult.failure(
                    f"等待全量验证沙箱 {public_id} 进入终态超时，最后状态: {state.get('status') or 'unknown'}",
                    failure_kind="timeout",
                )
            await asyncio.sleep(_FULL_VALIDATION_POLL_SECONDS)
            # MySQL 默认 REPEATABLE READ；仅 expire ORM 对象仍会停留在首次
            # SELECT 的事务快照。占位账本已在执行前提交，这里可安全结束只读事务。
            self._db.rollback()
            self._db.expire_all()
            try:
                state = sandbox_service.get_environment(self._db, self._user, public_id)
            except Exception as exc:  # 查询失败也必须结束本次原子工具调用
                return ToolExecutionResult.failure(f"读取全量验证沙箱 {public_id} 失败: {exc}")

            latest_events = state.get("events") if isinstance(state, dict) else None
            latest_event = latest_events[-1] if isinstance(latest_events, list) and latest_events else {}
            if not isinstance(latest_event, dict):
                latest_event = {}
            try:
                await _emit(
                    self._event_sink,
                    {
                        "type": "response.sandbox.progress",
                        "environment_id": public_id,
                        "status": str(state.get("status") or ""),
                        "stage": str(latest_event.get("stage") or ""),
                        "message": str(latest_event.get("message") or ""),
                    },
                )
            except Exception:  # noqa: BLE001 - SSE 进度事件失败不阻断沙箱等待
                pass

        terminal = dict(state)
        terminal["terminal"] = True
        return ToolExecutionResult.success(terminal)

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

    def _knowledge_agent_code(self) -> str:
        return surface_agent_identity(self._surface)[0]

    def _recall_knowledge(self, call: ToolCall) -> ToolExecutionResult:
        """检索小菱的 RAG 知识笔记本(用户私有知识 + 当前 Agent 知识)。"""

        query = str(call.arguments["query"])
        top_k = int(call.arguments.get("top_k") or 5)
        hits = agent_knowledge_service.unified_retrieve(
            self._db,
            user_id=int(self._user.id),
            agent_code=self._knowledge_agent_code(),
            query=query,
            top_k=top_k,
        )
        # 运维教程仅唯一超级管理员可检索;普通管理员/普通用户即使命中也过滤掉
        if not (self._surface == "admin" and self._is_super_admin):
            hits = [hit for hit in hits if "运维教程" not in str(hit.get("title") or "")]
        if not hits:
            return ToolExecutionResult.failure("知识笔记本中没有检索到相关内容")
        return ToolExecutionResult.success({"count": len(hits), "hits": hits})

    def _save_knowledge_note(self, call: ToolCall) -> ToolExecutionResult:
        """把小菱的学习感悟/操作要点写入知识笔记本(写操作已先经审批)。"""

        row = agent_knowledge_service.add_document(
            self._db,
            agent_code=self._knowledge_agent_code(),
            title=str(call.arguments["title"]),
            content=str(call.arguments["content"]),
            source_type="manual",
            source_ref=f"response_run:{self._run_id}",
            risk_level="medium",
            confidence=float(call.arguments.get("confidence") or 0.8),
        )
        try:
            from app.services import audit_service

            audit_service.log(
                self._db, self._user, "agent_tool.knowledge_note_save",
                target_type="agent_knowledge_doc", target_id=str(getattr(row, "id", "")),
                detail="小菱侧写入知识笔记(经审批)",
            )
        except Exception:  # noqa: BLE001 - 审计失败不影响工具结果
            logger.debug("knowledge_note_save 审计落库失败")
        return ToolExecutionResult.success({
            "doc_id": row.id,
            "title": row.title,
            "status": row.status,
            "message": "已写入知识笔记本" if row.status == "active" else "已提交知识笔记本,等待审批激活",
        })

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
            origin_surface=self._surface,
            origin_session_key=self._session_key,
        )
        data = dict(response.data or {})
        session_id = str(data.get("session_id") or "")
        pending = take_pending(session_id)
        if not session_id or pending is None:
            return ToolExecutionResult.failure("圆桌讨论上下文创建失败")
        launch_pending_discussion(pending)
        open_query = urlencode({
            "discuss_session": session_id,
            "discuss_ws": str(data.get("ws_url") or ""),
            "discuss_agents": json.dumps(data.get("agents") or [], ensure_ascii=False),
            "discuss_file": str(data.get("file_name") or ""),
        })
        data.update({
            "started_by": "user_agent",
            "open_path": f"/agents?{open_query}",
            "open_url": f"/agents?{open_query}",
        })
        return ToolExecutionResult.success(data)

    def _create_pentest_engagement(self, call: ToolCall) -> ToolExecutionResult:
        """小菱安排渗透测试: 只创建草稿, 授权确认必须由用户在前端完成。"""

        from app.services import pentest_service

        if not rbac_service.check_permission(
            self._db, self._user.id, PermissionCode.PENTEST_START,
        ):
            return ToolExecutionResult.failure("当前账户没有发起渗透测试的权限(pentest:start)")
        try:
            data = pentest_service.create_engagement(
                self._db,
                self._user,
                {
                    "project_id": int(call.arguments["project_id"]),
                    "target_type": str(call.arguments.get("target_type") or "web"),
                    "task_name": str(call.arguments.get("task_name") or ""),
                    "notes": str(call.arguments.get("notes") or ""),
                },
            )
        except pentest_service.PentestError as exc:
            return ToolExecutionResult.failure(str(exc))
        except AppError as exc:
            return ToolExecutionResult.failure(str(exc))
        # 审计留痕与 REST 路径同源(agent 工具不能绕过审计链)
        try:
            from app.services import audit_service

            audit_service.log(
                self._db,
                self._user,
                "pentest_create",
                target_type="pentest_engagement",
                target_id=str(data["public_id"]),
                detail=f"小菱创建渗透测试委托({data.get('target_type_label', '')})",
            )
        except Exception:  # noqa: BLE001 - 审计失败不影响工具结果
            logger.debug("pentest_create 审计落库失败")
        return ToolExecutionResult.success(
            {
                "engagement_public_id": data["public_id"],
                "status": data["status"],
                "target_type_label": data["target_type_label"],
                "authorize_url": "/pentests",
                "rules_version": pentest_service.PENTEST_RULES_VERSION,
                "message": (
                    "委托草稿已创建。请在『渗透测试』页面完成授权规则签署与时间窗设定后启动;"
                    "未完成授权前不会执行任何主动测试动作。"
                ),
            }
        )

    def _start_pentest_engagement(self, call: ToolCall) -> ToolExecutionResult:
        """启动已授权委托;授权门未通过时返回授权入口而不是绕过。"""

        from app.services import pentest_service

        if not rbac_service.check_permission(
            self._db, self._user.id, PermissionCode.PENTEST_START,
        ):
            return ToolExecutionResult.failure("当前账户没有启动渗透测试的权限(pentest:start)")
        public_id = str(call.arguments.get("engagement_public_id") or "")
        try:
            data = pentest_service.start_engagement(self._db, self._user, public_id)
        except pentest_service.PentestError as exc:
            return ToolExecutionResult.failure(str(exc))
        except AppError as exc:
            return ToolExecutionResult.failure(str(exc))
        # 审计留痕与 REST 路径同源(agent 工具不能绕过审计链)
        try:
            from app.services import audit_service

            audit_service.log(
                self._db,
                self._user,
                "pentest_start",
                target_type="pentest_engagement",
                target_id=str(data["public_id"]),
                detail="小菱启动七阶段渗透测试流水线",
            )
        except Exception:  # noqa: BLE001 - 审计失败不影响工具结果
            logger.debug("pentest_start 审计落库失败")
        return ToolExecutionResult.success(
            {
                "engagement_public_id": data["public_id"],
                "status": data["status"],
                "phases": ["情报收集", "威胁建模", "漏洞探测", "受控验证", "后渗透推演", "报告输出"],
                "message": "七阶段流水线已启动, 可用 get_pentest_status 跟踪进度。",
            }
        )

    def _get_pentest_status(self, call: ToolCall) -> ToolExecutionResult:
        """按委托或项目查询渗透测试进度摘要。"""

        from app.models.pentest import PentestEngagement
        from app.services import pentest_service

        if not rbac_service.check_permission(
            self._db, self._user.id, PermissionCode.PENTEST_VIEW,
        ):
            return ToolExecutionResult.failure("当前账户没有查看渗透测试的权限(pentest:view)")
        public_id = str(call.arguments.get("engagement_public_id") or "")
        project_id = call.arguments.get("project_id")
        query = self._db.query(PentestEngagement).filter(PentestEngagement.user_id == int(self._user.id))
        if not public_id and project_id is None:
            return ToolExecutionResult.failure("请提供 engagement_public_id 或 project_id")
        if public_id:
            query = query.filter(PentestEngagement.public_id == public_id)
        else:
            query = query.filter(PentestEngagement.project_id == int(project_id))
        row = query.order_by(PentestEngagement.id.desc()).first()
        if row is None:
            return ToolExecutionResult.failure("未找到对应渗透测试委托")
        try:
            detail = pentest_service.get_engagement_detail(self._db, self._user, row.public_id)
        except pentest_service.PentestError as exc:
            return ToolExecutionResult.failure(str(exc))
        return ToolExecutionResult.success(
            {
                "engagement_public_id": detail["public_id"],
                "status": detail["status"],
                "target_type_label": detail["target_type_label"],
                "phases": [
                    {
                        "phase_label": phase["phase_label"],
                        "status": phase["status"],
                        "summary": phase["summary"],
                        "line_count": len(phase.get("lines") or []),
                    }
                    for phase in detail.get("phases") or []
                ],
                "finding_total": len(detail.get("findings") or []),
                "summary": detail.get("summary") or {},
                "report_task_id": detail.get("report_task_id"),
            }
        )

    def _get_roundtable_discussion(self, call: ToolCall) -> ToolExecutionResult:
        """按会话归属返回圆桌状态与已产生的发言。"""

        from app.agents.discussion_bus import DiscussionBus

        session_id = str(call.arguments["session_id"])
        session = DiscussionBus.instance().get_session(session_id)
        if session is None:
            return ToolExecutionResult.failure("圆桌讨论不存在或已过期")
        if int(session.owner_user_id) != int(self._user.id) and not _is_admin_actor(self._db, self._user):
            return ToolExecutionResult.failure("无权访问该圆桌讨论")
        return ToolExecutionResult.success({
            "session_id": session.session_id,
            "status": session.status,
            "file_name": session.file_name,
            "max_rounds": session.max_rounds,
            "report_task_id": session.report_task_id,
            "turn_count": len(session.turns),
            "turns": [turn.to_dict() for turn in session.turns[-100:]],
        })

    async def _control_roundtable_discussion(self, call: ToolCall) -> ToolExecutionResult:
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
            if action != "user_input":
                return ToolExecutionResult.failure("圆桌讨论已经结束，只能提交纠正意见发起续会")
            if not content:
                return ToolExecutionResult.failure("user_input 必须提供非空 content")
            return await self._continue_roundtable_discussion(session, content)
        if action == "user_input":
            if not content:
                return ToolExecutionResult.failure("user_input 必须提供非空 content")
            bus.publish_turn(session_id, DiscussionTurn(
                turn_id=-1,
                agent_code="user",
                agent_name="你",
                role="user",
                content=content,
            ))
            accepted = bus.send_user_input(session_id, content)
        else:
            accepted = bus.control_session(session_id, action)
        if not accepted:
            return ToolExecutionResult.failure("讨论尚未启动或控制器已结束")
        return ToolExecutionResult.success({
            "session_id": session_id,
            "action": action,
            "accepted": True,
            "status": session.status,
        })

    async def _continue_roundtable_discussion(
        self,
        session: Any,
        correction: str,
    ) -> ToolExecutionResult:
        """为已结束圆桌创建独立续会，保留原小菱回投上下文。"""

        from app.api.v1.discussion import start_discussion
        from app.api.v1.ws_discussion import launch_pending_discussion, take_pending

        project_id = int(getattr(session, "project_id", 0) or 0)
        file_id = int(getattr(session, "file_id", 0) or 0)
        review_type = str(getattr(session, "review_type", "") or "").strip()
        origin_surface = str(getattr(session, "origin_surface", "") or "").strip()
        origin_session_key = str(getattr(session, "origin_session_key", "") or "").strip()
        if project_id <= 0 or file_id <= 0 or not review_type:
            return ToolExecutionResult.failure("原圆桌缺少真实项目、文件或审查类型，无法安全创建续会")
        if origin_surface not in {"user", "admin"} or not origin_session_key:
            return ToolExecutionResult.failure("原圆桌缺少小菱回投上下文，无法安全创建续会")

        owner = self._db.get(User, int(session.owner_user_id or 0))
        if owner is None or int(getattr(owner, "status", 0) or 0) != 1:
            return ToolExecutionResult.failure("原圆桌所属用户不存在或已停用，无法创建续会")

        previous_summary = ""
        for turn in reversed(list(getattr(session, "turns", []) or [])):
            if str(getattr(turn, "agent_code", "")) == "orchestrator":
                previous_summary = str(getattr(turn, "content", "") or "").strip()
                if previous_summary:
                    break
        if not previous_summary:
            previous_summary = "上一轮未产生可读的主持人结论。"
        continuation_context = (
            "【上一轮结论】\n"
            f"{previous_summary[:4000]}\n\n"
            "【本次纠正要求】\n"
            f"{correction[:2000]}\n\n"
            "请基于当前项目源码重新独立审查，不得将上一轮结论当作既定事实。"
        )

        response = start_discussion(
            project_id=project_id,
            file_id=file_id,
            review_type=review_type,
            db=self._db,
            user=owner,
            origin_surface=origin_surface,
            origin_session_key=origin_session_key,
            continued_from_session_id=str(session.session_id),
            continuation_context=continuation_context,
        )
        data = dict(response.data or {})
        new_session_id = str(data.get("session_id") or "")
        pending = take_pending(new_session_id)
        if not new_session_id or pending is None:
            return ToolExecutionResult.failure("圆桌续会上下文创建失败")
        launch_pending_discussion(pending)
        return ToolExecutionResult.success({
            "session_id": new_session_id,
            "continued_from_session_id": str(session.session_id),
            "previous_report_task_id": int(getattr(session, "report_task_id", 0) or 0),
            "action": "user_input",
            "accepted": True,
            "status": "active",
            "started_by": "user_agent",
            "message": "已基于上一轮结论创建独立续会，完成后将回投原小菱会话。",
            "open_path": f"/agents?discuss_session={new_session_id}",
            "open_url": f"/agents?discuss_session={new_session_id}",
        })

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
        return await self._execute_once(
            call,
            lambda: ToolExecutionResult.failure(
                error,
                failure_kind=strategy_learning_service.classify_failure(error),
            ),
        )

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
        # 同步广播到全局 AgentEventBus: Agent 中心工位卡因此能看到助手
        # 「正在工作」; 按 surface 归属身份(管理端=贾维斯/manager, 成员端=小菱),
        # 事件按 user_id 隔离,只推给运行所属用户。
        if event_type == "response.tool.started":
            try:
                from app.agents.event_bus import emit_event
                from app.agents.events import AgentEventType

                agent_code, agent_display = surface_agent_identity(self._surface)
                emit_event(
                    AgentEventType.PROGRESS,
                    agent_code,
                    self._run_id,
                    message=f"{agent_display}正在执行工具: {call.name}",
                    user_id=int(self._user.id),
                )
            except Exception:  # noqa: BLE001 - 状态广播失败不影响工具执行
                logger.debug("助手工具事件广播失败", call_name=call.name)
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
        if call.name == "send_message":
            return str(call.arguments.get("send_to") or "orchestrator")
        if call.name == "admin_execute_operation":
            return "operations"
        if call.name == "invoke_published_agent":
            return str(call.arguments.get("agent_code") or "custom_agent")
        if call.name in self._skill_bindings:
            return self._skill_bindings[call.name].split(".", 1)[0]
        return surface_agent_identity(self._surface)[0]

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
            error = str(exc)
            result = ToolExecutionResult.failure(
                error,
                failure_kind=strategy_learning_service.classify_failure(error),
            )

        row.status = "success" if result.status == "success" else "failed"
        persisted_output = _redact_event_value(result.output)
        row.result_json = json.dumps(
            {
                "status": result.status,
                "output": persisted_output,
                "error": result.error,
                "failure_kind": result.failure_kind,
            },
            ensure_ascii=False,
            default=str,
        )
        row.error = result.error or None
        self._db.commit()
        try:
            nested_params = call.arguments.get("params")
            project_id = _to_int(call.arguments.get("project_id"))
            if project_id is None and isinstance(nested_params, Mapping):
                project_id = _to_int(nested_params.get("project_id"))
            strategy_learning_service.record_tool_outcome(
                self._db,
                owner_user_id=int(self._user.id),
                project_id=project_id,
                agent_code=self._tool_agent_code(call),
                tool_name=call.name,
                arguments=self._persisted_arguments(call),
                outcome="success" if result.status == "success" else "failure",
                failure_kind=result.failure_kind,
                summary=(
                    _summarize_tool_value(result.output)
                    if result.status == "success"
                    else str(result.error or "工具执行失败")
                ),
                evidence_ref=f"tool:{request_id}",
            )
            self._db.commit()
        except Exception:  # noqa: BLE001 - 学习失败不改变已经落账的工具真实结果
            self._db.rollback()
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
        return ToolExecutionResult.failure(
            str(payload.get("error") or row.error or "工具执行失败"),
            failure_kind=str(payload.get("failure_kind") or ""),
        )

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
                agent_code=surface_agent_identity(self._surface)[0],
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

        is_critical = spec.risk == CAPABILITY_CRITICAL
        if spec.risk != CAPABILITY_READ and not approved:
            # 唯一超级管理员:critical 高危仍需点击确认,中低风险写能力免审批
            if not (self._is_super_admin and not is_critical):
                return self._approval(
                    call,
                    danger=is_critical,
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
            # 明文码随一次性结果回到模型上下文:生成即用场景(管理员当场转交)
            # 需要完整码直接可见;one_time_codes 键落库时被脱敏为 [REDACTED],
            # 库里始终只有哈希,明文只存在于本次内存结果与 SSE 事件。
            safe_data = {
                "generated_count": len(values),
                "one_time_codes": values,
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
        error = str(getattr(result, "error", "") or f"工具 {call.name} 执行失败")
        return ToolExecutionResult.failure(
            error,
            failure_kind=str(
                getattr(result, "failure_kind", "")
                or strategy_learning_service.classify_failure(error)
            ),
        )


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
        self._is_super_admin = surface == "admin" and _is_super_admin_actor(db, user)
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
        instructions = _instructions(self._surface, self._user, self._is_super_admin)
        try:
            instructions += strategy_learning_service.build_strategy_context(
                self._db,
                owner_user_id=int(self._user.id),
                surface=self._surface,
                messages=messages,
            )
        except Exception:  # noqa: BLE001 - 记忆检索降级不能阻断小菱主链路
            self._db.rollback()
        result = await runtime.start(
            messages,
            instructions=instructions,
            tools=tools,
            run_id=run_id,
        )
        await self._emit_validated_output(event_sink, result)
        return result

    async def cancel(self, *, run_id: str, reason: str = "") -> RuntimeResult:
        """请求取消一次运行并把检查点收敛为 cancelled。

        用户可选填取消原因，随检查点持久化，用于历史回看与原因沉淀；
        有原因时额外累计一次带维度的事件计数。
        """
        _, runtime = await self._runtime(run_id, None)
        result = await runtime.cancel(run_id, reason=reason)
        observe_event("xiaoling_cancel", labels={"surface": self._surface})
        if reason.strip():
            observe_event("xiaoling_cancel_reasoned", labels={"surface": self._surface})
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
        elif action == "retry":
            result = await runtime.retry(run_id)
        else:
            raise ValueError("不支持的恢复动作")
        await self._emit_validated_output(event_sink, result)
        return result

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
        mcp = McpToolProvider()
        executor = PrismToolExecutor(
            self._db,
            self._user,
            surface=self._surface,
            run_id=run_id,
            mcp_provider=mcp,
            session_key=self._session_key,
            event_sink=event_sink,
        )
        # 工具事件必须先于结论文本到达用户端。DeepSeek 可能在工具调用前
        # 产生 output_text.delta，因此所有 surface 都先缓冲文本，完成后统一发出。
        transport_sink = _buffer_text_sink(event_sink)
        # 小菱的 user/admin 两个 surface 都属于总调度者,统一使用 orchestrator 模型;
        # 只有用户/全局自定义配置才覆盖该默认值。系统默认 config.model 是子 Agent 的
        # deepseek-v4-flash,不能在这里覆盖成 flash,否则模型分层永远不会生效。
        # AiCallLog 的 model_name 兜底值也复用该变量,因此模型分层调整会同步落到调用日志。
        fallback_model = (
            config.model
            if config.source in {"user", "global"}
            else settings.deepseek_orchestrator_model
        )
        agent_label = "manager" if self._surface == "admin" else "chat_assistant"

        def _write_ai_call_log(response: Mapping[str, Any]) -> None:
            """小菱每次 LLM 轮次落 AiCallLog,供 Agent 工作台按用户统计调用次数。

            模型名优先取上游 response.model,否则与 _runtime 的 fallback_model
            保持同步,确保日志记录的是总调度者模型而不是子 Agent 的 flash 默认值。
            """
            from app.models.ai_call_log import AiCallLog

            usage = response.get("usage") if isinstance(response.get("usage"), Mapping) else {}
            upstream_status = str(response.get("status") or "")
            error_value = response.get("error")
            log = AiCallLog(
                user_id=int(self._user.id),
                agent_label=agent_label,
                model_name=str(response.get("model") or fallback_model),
                prompt_tokens=_to_int(usage.get("input_tokens") or usage.get("prompt_tokens")),
                completion_tokens=_to_int(usage.get("output_tokens") or usage.get("completion_tokens")),
                total_tokens=_to_int(usage.get("total_tokens")),
                status="success" if upstream_status == COMPLETED else "failed",
                error_message=(str(error_value) if error_value else None)[:500] if error_value else None,
            )
            self._db.add(log)
            self._db.commit()

        runtime = DeepSeekResponsesRuntime(
            transport=NativeResponsesTransport(config, transport_sink),
            tool_executor=executor,
            checkpoint_store=self._store,
            model=fallback_model,
            fallback_model=settings.deepseek_model if settings.deepseek_orchestrator_fallback_to_flash else None,
            max_rounds=settings.agent_responses_max_rounds,
            stream=True,
            context_window_tokens=settings.deepseek_context_window_tokens,
            max_output_tokens=settings.deepseek_max_output_tokens,
            compaction_threshold_tokens=settings.deepseek_compaction_threshold_tokens,
            keep_recent_tokens=settings.deepseek_compaction_keep_recent_tokens,
            completion_guard=self._validate_admin_completion if self._surface == "admin" else None,
            on_round=_write_ai_call_log,
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
    # 工具生命周期由本地执行器产生；上游同名事件可能没有平台错误详情，
    # 透传会让前端先收到 error=null 的重复失败事件。
    upstream_tool_events = frozenset(
        {"response.tool.started", "response.tool.completed", "response.tool.failed"}
    )

    async def filtered(event: Mapping[str, Any]) -> None:
        if str(event.get("type") or "") == "response.output_text.delta":
            return
        if str(event.get("type") or "") in upstream_tool_events:
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


def _to_int(value: Any) -> Optional[int]:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _instructions(surface: str, user: Optional[User] = None, is_super_admin: bool = False) -> str:
    if surface == "admin":
        # 控制面/体验面分离(参照 Microsoft Copilot Control System 模式):
        # 管理端是独立的「贾维斯」全局运维身份, 与普通成员的小菱彻底分开;
        # 定位是全局运维(态势/健康/审批/治理/服务器), 不承担代码审计叙事。
        identity = "Prism 全局运维 Agent「贾维斯」"
        capability_instruction = (
            "管理员界面任务必须先调用 admin_describe_capabilities 查询对应页面能力和精确参数，"
            "再调用 admin_execute_capability；不得猜测能力编码或参数。"
            "收到来自 agent:monitor 的 status.update「JARVIS 运维简报」时,只依据简报 evidence 中的真实证据汇报:"
            "先给结论与优先级排序,再建议只读核验动作和需要管理员点击批准的处置动作;"
            "绝不未经批准自动执行写操作,也不要声称已修复或已处理。"
            "你是全局运维身份: 用户提到代码审查、安全审计、渗透测试等成员侧业务时,"
            "说明这些由成员侧的小菱负责,引导用户到对应业务页面或切换成员账号,"
            "不要主动替管理员发起项目安全审计。"
        )
        role_behavior = (
            "全局运维是你的核心职责：系统态势巡查(服务健康/安全态势/威胁信号/数据库健康)、"
            "用户与项目治理(账号/项目/Agent 发布审批, 审批前先查完整详情并展示修改前后内容、"
            "依赖、测试证据和风险)、平台运营数据聚合(统计、趋势、分布要聚合多来源只读能力后给结论表格)。"
            "批量处理与批量分析同样是核心能力：处理“所有/批量/全部/这些”类请求时，"
            "先用列表类能力查清完整候选(注意翻页,page_size 取大值并核对总数),"
            "再用 ask_user 展示统计口径与候选数量供确认,然后逐条或分页执行；"
            "完成后汇报成功/失败/跳过条数与原因。"
            "服务器运维(仅超级管理员可用)：实时获取服务器信息(状态/磁盘/进程/日志/证书)时，"
            "直接用 admin_execute_operation 的只读动作现查"
            "(status/host_inventory/journal_query/read_text_file/certificate_status)，不得编造；"
            "用户要求开放/关闭端口或服务时，用 admin_execute_operation 的 firewall_action"
            "(开放端口=operation add + target_type port + value 端口号，关闭用 remove)；"
            "重启服务用 restart_service，装/卸软件包用 package_action，"
            "改防火墙/服务/账号等写操作都会自动等待用户批准，批准后系统会把结果交还给你。"
            "管理员需要导入 GitHub 公开仓库时可以用 queue_remote_project_import 创建导入任务"
            "(queued/running 只报告真实 task_id 和进度,后续查询使用 get_remote_project_import),"
            "但导入后的安全审计属于成员侧业务,引导到项目页由小菱侧流程完成,你不代跑审计。"
        )
    else:
        identity = "Prism 代码审查 Agent「棱镜小助·小菱」"
        capability_instruction = (
            "普通用户页面任务必须先调用 user_describe_capabilities 查询对应页面能力和精确参数，"
            "再调用 user_execute_capability；不得猜测能力编码或参数。"
            "报告、二进制单文件和项目源码下载必须分别使用 download_report、"
            "download_code_file 和 download_project_source 固定工具。"
            "圆桌讨论必须使用 start_roundtable_discussion、get_roundtable_discussion "
            "和 control_roundtable_discussion 固定工具。start_roundtable_discussion 启动后立即结束本轮,"
            "告知用户等待期间可以继续对话;讨论结束后系统会把结论回投本会话,收到 task.result 后直接汇报共识、"
            "报告入口和是否满足要求;结论不满足时用 control_roundtable_discussion 的 user_input 追加纠正意见，"
            "系统会自动创建独立续会并沿用原回投会话，不得把旧会话伪装为运行中;"
            "不得编造讨论进度或结论。"
            "上传源码后用户要求自动完成部署和黑白盒审查时，必须立即调用 "
            "run_full_project_validation；沙箱测试和部署是页面发现协议的例外：白盒、黑盒和组合测试直接使用 "
            "run_project_tests，持续部署、关闭和续期分别使用 deploy_project_sandbox、"
            "close_sandbox 和 extend_sandbox，不要先通过 user_describe_capabilities 搜索这四项固定工具。"
            "run_full_project_validation 会在同一个固定工具调用内等待唯一沙箱进入 "
            "succeeded/failed/blocked/stopped/expired 终态并返回完整结果；工具返回后直接基于终态和报告给出结论，"
            "不得再调用 user_describe_capabilities、user_execute_capability 或重复创建沙箱。"
            "项目做过语法修复后会有源码修复副本：查询项目详情(项目列表能力)可拿到 source_revisions 列表，"
            "每个副本有 id/revision_no/修复文件清单；用户要求'用修复后的源码跑审计/用副本'时，"
            "把对应副本 id 传给 run_project_tests 或 deploy_project_sandbox 的 source_revision_id，"
            "不传则默认使用原始源码。"
            "用户直接发送 GitHub 公开仓库网址(https://github.com/{owner}/{repo} 或 /tree/<分支>)时："
            "调用 queue_remote_project_import(url=原始网址, project_name=仓库名, audit_mode=true) 创建可恢复导入任务；"
            "queued/running 只报告真实 task_id 和进度，不要忙轮询或声称已完成；后续查询使用 get_remote_project_import。"
            "任务 succeeded 后才调用 audit_security_for_project(project_id=返回的 project_id, scan_mode='static_full') "
            "执行整包安全审计，并基于工具返回的真实结果汇报；不要伪造下载或审计结果。"
        )
        role_behavior = (
            "你服务的对象主要是不会看文档的普通用户和审查员：回答要像带路人，"
            "先给结论,再给傻瓜式下一步,并把对应页面入口用站内链接标出来；"
            "审查员发起审查、处理问题、导出报告时,优先直接用工具替他完成,再引导到结果页面核对。"
            "审查结论必须引用本次工具返回的真实数据，不得凭印象作答。"
            "上传/全量验证完成后,基于真实结果按「下一步推荐协议」主动给出 2-3 个推荐动作并说明理由,"
            "让用户不必自己想接下来做什么:"
            "①验证全绿 → 推荐发起安全审计(说明能看到什么风险)+ 发起正式审查(拿评分和报告);"
            "②验证有失败阶段 → 先指出失败的阶段与大白话原因,推荐修复入口或让小菱生成 AI 修复提示;"
            "③审计已做过且有高危发现 → 推荐先看高危清单再决定是否修复重测;"
            "每次推荐都给出对应的站内链接,不堆砌术语。"
        )
    username = str(getattr(user, "username", "") or "未知用户")
    role = str(getattr(user, "role", "") or "unknown")
    role_label = {
        "super_admin": "超级管理员(唯一 admin,可执行服务器运维)",
        "admin": "管理员(可管理平台内容,不可执行服务器运维)",
        "user": "普通用户",
    }.get(role, role)
    identity_line = (
        f"你当前服务的登录用户是「{username}」(角色:{role_label})。"
        "所有操作均以该身份执行,不得假设或冒充其他身份;"
        "执行服务器运维、删除、改角色、批量删除、写知识等敏感操作前,"
        "先向用户复述当前身份并请用户确认。"
    )
    if surface == "admin" and not is_super_admin:
        identity_line += (
            "注意:你不是唯一超级管理员,没有服务器运维权限(admin_execute_operation/admin_system_status/"
            "外部 MCP/知识源写入/受限调度任务均不可用),也无法检索「运维教程」知识;"
            "用户请求这类操作时直接说明'仅超级管理员 admin 可执行',不要调用工具。"
        )
    return (
        f"你是 {identity}。{identity_line}"
        "系统按当前用户权限过滤了工具列表:没有提供给你的工具一律不可用,"
        "超级管理员(admin)拥有平台全部能力:管理能力注册表中的任何能力(含各页面增删改查)都可直接执行,不要以「页面未开放/权限不足」拒绝;查询类直接执行,写入/删除/高危类先给出说明并等待用户点击确认后执行。普通管理员仍按权限执行。"
        "用户请求超出权限时直接说明原因并拒绝,不要尝试调用也不会报'工具权限不足'错误。"
        "所有事实查询和操作必须使用已提供工具；不要编造工具结果，也不要声称未执行的动作已完成。"
        "根据每次工具返回结果自主判断下一步，可以连续调用多个工具。"
        "需要发现子 Agent、已发布 Agent 或同一账户其他会话时调用 list_agents；"
        "需要移交结论、同步进度或协调并行任务时调用 send_message，"
        "只能向 list_agents 返回的精确地址发送严格结构化消息，不得用自由文本伪造消息信封。"
        "跨会话自主协作协议(像真人同事并行干活一样):"
        "①你完成影响其他会话的改动后(如修改共享项目/产生新审查结论/改变团队计划),"
        "主动用 send_message 向相关会话移交结论摘要,不要等用户转述;"
        "②收到 mesh 消息任务(task.request)时,这是另一个会话在向你移交工作,执行后用"
        "send_message 回传结果;对方运行中会在工具间隙读到你的消息,空闲时会立即开新回合处理;"
        "③长任务(迁移/全量验证/团队作业)进行中,定期向发起会话 send_message 汇报进度;"
        "④send_message 的 subject 写人话结论,payload 写结构化交接内容(做了什么/关键数字/下一步谁做);"
        "需要同时拆解两个以上相互独立的局部任务时，使用 create_agent_team 创建真实可追踪的团队工作图，"
        "成员只能选择 list_agents 返回的可执行 Agent；"
        "list_agents 中 team_dispatch_state=team_governed 的受治理 Agent(test_verifier、sandbox_deployer、"
        "operations)即使 dispatch_state=approval_required 也可作为团队成员,团队调度会接管其审批,"
        "不要因此拒绝组队或追问用户；用 get_agent_team 查看实际状态和证据，"
        "失败节点需要改变方案后再用 retry_agent_team，用户要求停止时调用 cancel_agent_team。"
        "唯一超级管理员在管理小菱中创建服务器运维团队时，成员必须使用 agent:operations，"
        "任务 input 必须是 {action,params}，且 action 必须来自 list_agents 返回的 team_input_contract.action；"
        "不得用 monitor、security_sentinel 或 dashboard 代替服务器运维。团队只允许只读运维，"
        "重启、配置、防火墙等写操作必须由主小菱调用 admin_execute_operation 进入审批链。"
        "只读项目验收中，项目分析成员必须使用 operation=inspect_project，历史测试核验成员必须使用"
        " operation=inspect_existing_results；文件清单核对成员必须使用 agent:code_file_manager 且"
        " input 为 {operation:'list', project_id}，不得把 inspect_project 交给文件清单成员。"
        "用户要求“扫描+验证”并行时：扫描成员必须用 agent:security_sentinel，input={project_id}；"
        "验证成员必须用 agent:test_verifier，只读核验用 input={operation:'inspect_existing_results', project_id}，"
        "只有用户明确授权才可用 operation:'run_project_tests'；不得用 agent:dashboard 做扫描或验证，"
        "dashboard 只用于看板汇总且 input 必须带 operation（summary|risk_distribution|score_trend 之一）；"
        "不得把只读核验交给 run_project_tests 或 run_full_project_validation。"
        "只有用户明确要求实际运行测试时，才允许使用后两种执行操作。"
        "用户要求「全方位/完整/黑白盒一起」审计某个项目时，优先组建审计团队并行推进四视角:"
        "①侦察成员 agent:security_sentinel(input={project_id}, scan_mode='static_full')摸清技术栈与高危文件;"
        "②白盒审计成员 agent:code_reviewer(input={project_id, review_type:'security'})逐文件语义审计;"
        "③黑盒验证成员 agent:test_verifier(input={operation:'inspect_existing_results', project_id})核验既有测试证据,"
        "用户明确要求实测时才用 run_project_tests;④汇总成员 agent:dashboard(input={operation:'risk_distribution'})"
        "汇总风险分布。团队任务的 depends_on 必须表达真实依赖(白盒依赖侦察,汇总依赖全部),"
        "不得四个成员互不依赖地裸奔;团队完成后用大白话汇报(见汇报风格),不得堆砌工具术语。"
        "用户要求修改自己的密码时，先说明修改成功后需要重新登录，并用 ask_user 收集旧密码与新密码，"
        "然后调用 change_own_password；不得在回复中回显任何密码，也不得修改他人密码。"
        "团队工具自动绑定当前登录用户、surface、session 和 trace，不得在参数中伪造其他账户或会话。"
        "需要派发任务给子 Agent 或团队时：先核对任务目标、范围和验收口径，缺关键细节先用 ask_user 与用户对齐；"
        "派发完成后立即结束本轮，告知用户已派发、等待期间可以继续补充需求或询问进度；"
        "收到子 Agent 的 task.result 时严格按消息附带的【监督式复核协议】执行：不合格且未达上限就用 "
        "send_message 回发纠正并说明已纠正，合格或达上限就直接向用户汇报全链结论。"
        "缺少真正阻断任务的信息时调用 ask_user，问题、候选项及其说明必须由你根据当前任务动态生成，不使用预设问题。"
        "回复中的站内 markdown 链接必须使用真实路由；需要跳转时必须在回复末尾追加一行"
        ' <!--PRISM_NAVIGATE {"action":"navigate","route":"/真实路由","label":"页面名称"}-->，'
        "路由必须来自 recall_knowledge 检索到的页面指南，不得编造路由。"
        "涉及名称近义表达或自定义 Agent 能力时先调用 search_published_agents；候选不唯一时用 ask_user "
        "展示动态候选，确认后才能调用 invoke_published_agent。"
        "涉及用户批量操作时必须先查询真实用户。用户说序号、第几条或范围而未明确是用户 ID 时，"
        "不得猜测；必须用 ask_user 区分列表序号与用户 ID，得到精确 user_ids 后再调用批量工具。"
        f"{capability_instruction}"
        "知识笔记本是你的 RAG 教学库：回答前若问题可能参考既有教学手册、操作指南或你沉淀过的经验，"
        "先调用 recall_knowledge 检索最相关知识切片再作答；只有用户明确要求保存教程或知识笔记时，"
        "才调用 save_knowledge_note(写操作会先等待用户批准)。工具、审查和沙箱的成功/失败策略由系统"
        "自动按当前账户固化并供所有子 Agent 复用，不得为这类执行结果重复调用 save_knowledge_note。"
        f"{role_behavior}"
        "写操作由系统暂停并展示审批；用户点击批准后系统会把原调用结果自动交还给你，不要要求用户重复发送指令。"
        "涉及具体页面操作步骤时，先调用 recall_knowledge 检索对应页面指南。"
        "使用中文直接给出结果，不使用预设套话，不输出空白行；代码块内部格式保持原样。\n\n"
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
        # 未调用任何写工具时，只有“声称写操作已完成”才需要证据拦截；
        # 模型诚实输出“无需处理/没有待处理/失败/说明性结论”不应被吞掉，
        # 否则工具链中断或检查后无需写操作时前端永远收不到结论。
        if claims_success:
            return "管理写请求在没有精确工具执行证据时就结束了"
        return None

    evidence = (
        _transcript_admin_write_evidence(checkpoint.transcript)
        if write_evidence is None
        else dict(write_evidence)
    )
    missing_calls = sorted(call_id for call_id in attempted_calls if call_id not in evidence)
    mismatched_calls = sorted(
        call_id
        for call_id, expected_call in attempted_calls.items()
        if call_id in evidence and evidence[call_id][0] != expected_call.code
    )
    non_successful_calls = sorted(
        call_id
        for call_id in attempted_calls
        if call_id in evidence and evidence[call_id][1] != "success"
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
            latest_user_text = " ".join(
                str(part.get("text") or "")
                for part in content
                if isinstance(part, Mapping)
            )
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
    # 只读意图优先：明确查询/查看/统计类请求，无强写动词按只读处理；
    # 若同时含强写动词（如“查询后删除”）则按写请求处理，避免复合指令漏判。
    if _ADMIN_READ_INTENT.search(text):
        return bool(_ADMIN_CLEAR_WRITE_VERB.search(text))
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
            terminal_status = (
                "rejected"
                if status in {"rejected", "denied", "cancelled", "canceled"}
                else "failed"
            )
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
        "description": "查询生产状态或执行宿主机白名单运维动作(仅超级管理员可用)；有副作用的动作会先等待用户批准",
        "parameters": {
            "type": "object",
            "oneOf": [
                {
                    "type": "object",
                    "description": ops_service.ACTION_DESCRIPTIONS.get(action, ""),
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
            "name": "queue_remote_project_import",
            "description": "创建可恢复、可查询进度的公开 HTTPS/GitHub 项目导入任务；执行前需要用户批准",
            "parameters": QueueRemoteProjectImportArguments.model_json_schema(),
        },
        {
            "type": "function",
            "name": "get_remote_project_import",
            "description": "查询当前用户远程项目导入任务的进度、结果或具体失败原因",
            "parameters": GetRemoteProjectImportArguments.model_json_schema(),
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
            "description": (
                "暂停、恢复、停止运行中的圆桌讨论或提交用户发言；"
                "对已结束讨论提交 user_input 会自动创建独立续会；执行前需要批准"
            ),
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
