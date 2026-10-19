"""MCP、能力搜索和代码沙箱 API Schema。"""

import re
import urllib.parse
from datetime import datetime
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, StrictInt, StrictStr, field_validator, model_validator


class McpServerUpsertIn(BaseModel):
    code: str = Field(pattern=r"^[a-z][a-z0-9_-]{1,79}$")
    name: str = Field(min_length=1, max_length=160)
    description: str = Field(default="", max_length=2000)
    transport: Literal["streamable_http", "managed"] = "streamable_http"
    url: str = Field(default="", max_length=500)
    auth_type: Literal["none", "bearer", "headers", "oauth_required"] = "none"
    headers: Optional[dict[str, str]] = None
    managed_kind: Optional[Literal["prism-code", "prism-sandbox", "playwright"]] = None
    enabled: bool = False
    credential_required: bool = False

    @field_validator("headers")
    @classmethod
    def validate_headers(cls, value: Optional[dict[str, str]]) -> Optional[dict[str, str]]:
        if value is None:
            return None
        if len(value) > 32:
            raise ValueError("MCP headers 不能超过 32 项")
        for key, item in value.items():
            if not str(key).strip() or len(str(key)) > 100 or len(str(item)) > 4000:
                raise ValueError("MCP header 名称或值超出限制")
        return value


class McpToolUpdateIn(BaseModel):
    display_name: Optional[str] = Field(default=None, min_length=1, max_length=200)
    description: Optional[str] = Field(default=None, max_length=4000)
    risk_level: Optional[Literal["low", "medium", "high", "critical"]] = None
    enabled: Optional[bool] = None


class McpBindingIn(BaseModel):
    agent_code: str = Field(min_length=1, max_length=80)
    tool_id: int = Field(gt=0)
    permission: Literal["allow", "deny", "escalate"] = "allow"
    requires_approval: bool = False
    enabled: bool = True


class CapabilityAliasIn(BaseModel):
    capability_code: str = Field(min_length=1, max_length=255)
    alias: str = Field(min_length=1, max_length=160)
    locale: str = Field(default="zh-CN", max_length=20)
    weight: float = Field(default=1.0, ge=0.1, le=3.0)
    enabled: bool = True


class CapabilitySearchOut(BaseModel):
    code: str
    name: str
    description: str
    source: str
    score: float
    aliases: list[str] = Field(default_factory=list)
    agent_code: Optional[str] = None
    requires_approval: bool = False


class SandboxWorkerUpsertIn(BaseModel):
    code: str = Field(pattern=r"^[a-z][a-z0-9_-]{1,79}$")
    name: str = Field(min_length=1, max_length=160)
    worker_type: Literal["local", "managed", "production_fallback"]
    transport: Literal["unix", "https"]
    endpoint: str = Field(min_length=1, max_length=500)
    token: str = Field(default="", max_length=500)
    supported_languages: list[Literal["python", "node", "java", "go", "php"]]
    supported_modes: list[Literal["whitebox", "blackbox", "combined", "deploy"]]
    runtime: str = Field(default="runsc", max_length=50)
    max_concurrency: int = Field(default=1, ge=1, le=8)
    priority: int = Field(default=50, ge=0, le=1000)
    enabled: bool = False


class SandboxHttpRequestScope(BaseModel):
    """单条本地黑盒 HTTP 白名单规则；路径和查询参数均按精确值匹配。"""

    model_config = ConfigDict(extra="forbid")

    method: Literal["GET"] = "GET"
    path: str = Field(min_length=1, max_length=512)
    query: dict[StrictStr, StrictStr] = Field(default_factory=dict)
    expected_statuses: list[StrictInt] = Field(default_factory=list, max_length=20)

    @field_validator("path")
    @classmethod
    def validate_exact_path(cls, value: str) -> str:
        path = str(value or "")
        if (
            not re.fullmatch(r"/(?!/)[A-Za-z0-9._~!$&+,;=@/-]*", path)
            or "//" in path
        ):
            raise ValueError("路径必须是精确的本地 URL 路径，不能含通配符、查询串、片段或双重编码")
        if "\\" in path or any(segment in {".", ".."} for segment in path.split("/")):
            raise ValueError("路径不能包含反斜杠或目录穿越段")
        return path

    @field_validator("query")
    @classmethod
    def validate_exact_query(cls, value: dict[str, str]) -> dict[str, str]:
        if not isinstance(value, dict) or len(value) > 10:
            raise ValueError("查询参数最多允许 10 个精确键值对")
        normalized: dict[str, str] = {}
        for raw_key, raw_value in value.items():
            if not isinstance(raw_key, str) or not isinstance(raw_value, str):
                raise ValueError("查询参数名称和值必须是字符串")
            key = raw_key
            item = raw_value
            if not re.fullmatch(r"[A-Za-z0-9_.~-]{1,80}", key) or not re.fullmatch(r"[A-Za-z0-9_.~-]{0,512}", item):
                raise ValueError("查询参数名称或值超出白名单限制")
            if item in {".", ".."}:
                raise ValueError("查询参数值不能是目录穿越段")
            normalized[key] = item
        if len(urllib.parse.urlencode(normalized).encode("ascii")) > 2048:
            raise ValueError("编码后的查询参数总长度不能超过 2048 字节")
        return normalized

    @field_validator("expected_statuses")
    @classmethod
    def validate_expected_statuses(cls, value: list[int]) -> list[int]:
        if len(value) > 20 or any(type(code) is not int or not 100 <= code <= 599 for code in value):
            raise ValueError("预期 HTTP 状态码必须在 100–599 之间")
        if len(set(value)) != len(value):
            raise ValueError("预期 HTTP 状态码不能重复")
        return value


class SandboxCreateIn(BaseModel):
    project_id: int = Field(gt=0)
    purpose: Literal["test", "deploy"]
    language: Literal["python", "node", "java", "go", "php"]
    test_mode: Literal["whitebox", "blackbox", "combined", "deploy"] = "whitebox"
    db_type: Literal["none", "sqlite", "mysql"] = "none"
    worker_code: str = Field(default="", max_length=80)
    source_revision_id: Optional[int] = Field(default=None, description="使用项目源码修复副本(不传则用原始源码)")
    ttl_hours: int = Field(default=72, ge=1, le=168)
    remote_target_url: Optional[HttpUrl] = None
    remote_target_authorized: bool = False
    remote_target_approval_token: Optional[str] = Field(default=None, max_length=120)
    http_scope: Optional[list[SandboxHttpRequestScope]] = Field(default=None, max_length=20)

    @model_validator(mode="after")
    def validate_http_scope(self) -> "SandboxCreateIn":
        local_bounded_blackbox = (
            self.purpose == "test"
            and self.remote_target_url is None
            and self.test_mode == "blackbox"
        )
        if not local_bounded_blackbox:
            if self.http_scope is not None:
                raise ValueError("HTTP 白名单仅适用于本地黑盒测试")
            return self
        if not self.http_scope:
            raise ValueError("本地黑盒测试必须提供精确 HTTP 路径白名单")
        if self.db_type == "mysql":
            raise ValueError("精确 HTTP 白名单模式不能连接非 loopback 测试数据库网络")
        targets = [(item.path, tuple(sorted(item.query.items()))) for item in self.http_scope]
        if len(set(targets)) != len(targets):
            raise ValueError("HTTP 白名单不能包含重复请求目标")
        if ("/", ()) not in targets:
            raise ValueError("HTTP 白名单必须包含不带查询参数的 GET / 探活条目")
        return self

    @field_validator("remote_target_authorized")
    @classmethod
    def authorization_is_explicit(cls, value: bool) -> bool:
        return bool(value)


class SandboxRemoteTargetAuthorizationIn(BaseModel):
    project_id: int = Field(gt=0)
    remote_target_url: HttpUrl
    test_mode: Literal["blackbox", "combined"]
    confirmed: bool


class SandboxRemoteTargetAuthorizationOut(BaseModel):
    approval_token: str
    expires_at: datetime


class SandboxExtendIn(BaseModel):
    hours: int = Field(ge=1, le=168)


class SandboxEventOut(BaseModel):
    id: int
    event_type: str
    stage: str
    message: str
    payload: dict[str, Any] = Field(default_factory=dict)
    create_time: datetime


class SandboxArtifactOut(BaseModel):
    id: int
    artifact_type: str
    file_name: str
    mime_type: str
    byte_size: int
    sha256: str
    download_allowed: bool = False


class SandboxEnvironmentOut(BaseModel):
    public_id: str
    project_id: int
    owner_id: int
    can_execute: bool = False
    can_stop: bool = False
    can_preview: bool = False
    worker_code: Optional[str] = None
    agent_code: str
    purpose: str
    language: str
    test_mode: str
    status: str
    runtime: str
    source_sha256: str
    source_revision_id: Optional[int] = None
    source_revision_no: Optional[int] = None
    source_revision_sha256: Optional[str] = None
    execution_source_sha256: Optional[str] = None
    execution_round: int = 0
    worker_request_id: Optional[str] = None
    syntax_repair_revisions: list[dict[str, Any]] = Field(default_factory=list)
    preview_path: Optional[str] = None
    remote_target_url: Optional[str] = None
    remote_target_approval_id: Optional[int] = None
    expires_at: datetime
    started_at: Optional[datetime] = None
    stopped_at: Optional[datetime] = None
    result: dict[str, Any] = Field(default_factory=dict)
    error: Optional[str] = None
    events: list[SandboxEventOut] = Field(default_factory=list)
    artifacts: list[SandboxArtifactOut] = Field(default_factory=list)
