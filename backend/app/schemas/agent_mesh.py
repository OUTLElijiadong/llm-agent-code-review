"""小菱 Agent Mesh 的严格输入契约。"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.utils.input_validation import normalize_plain_text, validate_json_payload


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class AgentMeshContextIn(_StrictModel):
    team_id: Optional[int] = Field(default=None, gt=0)
    member_id: Optional[int] = Field(default=None, gt=0)
    source_revision_id: Optional[int] = Field(default=None, gt=0)
    task_id: Optional[int] = Field(default=None, gt=0)
    project_id: Optional[int] = Field(default=None, gt=0)
    file_id: Optional[int] = Field(default=None, gt=0)
    run_id: str = Field(default="", max_length=80)
    # 监督式调度闭环（T4）的可选监督元数据。
    # 仅作为信封透传字段；范围/依赖关系校验由 agent_mesh_service.send_message 负责，
    # 这里不引入新约束，避免破坏已有信封兼容性。
    supervision_objective: Optional[str] = Field(default=None, max_length=4000)
    supervision_round: Optional[int] = None
    supervision_max_rounds: Optional[int] = None
    supervision_correlation_id: Optional[str] = Field(default=None, max_length=160)

    @field_validator("supervision_objective", "supervision_correlation_id")
    @classmethod
    def validate_supervision_text(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        normalize_plain_text(value, field_name="Agent 监督上下文", allow_empty=True)
        return value


class AgentMeshDeliveryIn(_StrictModel):
    requires_ack: bool = True
    max_attempts: int = Field(default=3, ge=1, le=10)
    expires_at: Optional[datetime] = None


class AgentMeshMessageIn(_StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    idempotency_key: str = Field(min_length=1, max_length=160)
    trace_id: str = Field(default="", max_length=80)
    correlation_id: str = Field(default="", max_length=80)
    causation_id: str = Field(default="", max_length=80)
    sent_from: str = Field(default="", max_length=200)
    send_to: str = Field(min_length=1, max_length=200)
    message_type: Literal[
        "task.request",
        "task.result",
        "task.error",
        "status.update",
        "coordination",
        "notification",
    ]
    priority: Literal["low", "normal", "high", "critical"] = "normal"
    subject: str = Field(min_length=1, max_length=240)
    payload: Dict[str, Any]
    context: AgentMeshContextIn = Field(default_factory=AgentMeshContextIn)
    artifacts: List[Dict[str, Any]] = Field(default_factory=list, max_length=50)
    errors: List[Dict[str, Any]] = Field(default_factory=list, max_length=50)
    delivery: AgentMeshDeliveryIn = Field(default_factory=AgentMeshDeliveryIn)

    @field_validator("idempotency_key", "trace_id", "correlation_id", "causation_id")
    @classmethod
    def validate_identifier(cls, value: str) -> str:
        if value and not all(char.isalnum() or char in "-_.:" for char in value):
            raise ValueError("标识只能包含字母、数字、- _ . :")
        return value

    @field_validator("subject")
    @classmethod
    def validate_subject(cls, value: str) -> str:
        return normalize_plain_text(value, field_name="Agent 消息主题", allow_newlines=False)

    @model_validator(mode="after")
    def validate_dynamic_payloads(self) -> "AgentMeshMessageIn":
        validate_json_payload(
            self.payload,
            field_name="Agent 消息负载",
            max_bytes=262_144,
            max_depth=16,
            max_items=2_000,
        )
        validate_json_payload(
            self.artifacts,
            field_name="Agent 制品列表",
            max_bytes=131_072,
            max_depth=12,
            max_items=1_000,
        )
        validate_json_payload(
            self.errors,
            field_name="Agent 错误列表",
            max_bytes=65_536,
            max_depth=8,
            max_items=500,
        )
        return self


class AgentMeshHeartbeatIn(_StrictModel):
    surface: Literal["user", "admin"]
    session_id: str = Field(min_length=8, max_length=128)
    title: str = Field(default="新对话", min_length=1, max_length=200)
    active_run_id: str = Field(default="", max_length=80)
    active_run_status: str = Field(default="", max_length=32)

    @field_validator("session_id")
    @classmethod
    def validate_session_id(cls, value: str) -> str:
        if not all(char.isalnum() or char in "-_" for char in value):
            raise ValueError("session_id 只能包含字母、数字、连字符和下划线")
        return value


class AgentMeshAckIn(_StrictModel):
    status: Literal["acknowledged", "processing", "completed", "failed"]
    summary: str = Field(default="", max_length=2000)
    error: str = Field(default="", max_length=2000)
