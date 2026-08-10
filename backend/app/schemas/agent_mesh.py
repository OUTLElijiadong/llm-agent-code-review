"""小菱 Agent Mesh 的严格输入契约。"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class AgentMeshContextIn(_StrictModel):
    task_id: Optional[int] = Field(default=None, gt=0)
    project_id: Optional[int] = Field(default=None, gt=0)
    file_id: Optional[int] = Field(default=None, gt=0)
    run_id: str = Field(default="", max_length=80)


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
