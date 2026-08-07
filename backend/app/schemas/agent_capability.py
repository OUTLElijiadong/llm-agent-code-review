"""MCP、能力搜索和代码沙箱 API Schema。"""

from datetime import datetime
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field, HttpUrl, field_validator


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


class SandboxCreateIn(BaseModel):
    project_id: int = Field(gt=0)
    purpose: Literal["test", "deploy"]
    language: Literal["python", "node", "java", "go", "php"]
    test_mode: Literal["whitebox", "blackbox", "combined", "deploy"] = "whitebox"
    db_type: Literal["none", "sqlite"] = "none"
    worker_code: str = Field(default="", max_length=80)
    ttl_hours: int = Field(default=72, ge=1, le=168)
    remote_target_url: Optional[HttpUrl] = None
    remote_target_authorized: bool = False

    @field_validator("remote_target_authorized")
    @classmethod
    def authorization_is_explicit(cls, value: bool) -> bool:
        return bool(value)


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


class SandboxEnvironmentOut(BaseModel):
    public_id: str
    project_id: int
    owner_id: int
    worker_code: Optional[str] = None
    agent_code: str
    purpose: str
    language: str
    test_mode: str
    status: str
    runtime: str
    source_sha256: str
    preview_path: Optional[str] = None
    remote_target_url: Optional[str] = None
    expires_at: datetime
    started_at: Optional[datetime] = None
    stopped_at: Optional[datetime] = None
    result: dict[str, Any] = Field(default_factory=dict)
    error: Optional[str] = None
    events: list[SandboxEventOut] = Field(default_factory=list)
    artifacts: list[SandboxArtifactOut] = Field(default_factory=list)
