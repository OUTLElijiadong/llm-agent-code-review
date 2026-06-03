"""
AI日志模块Pydantic Schema
"""
from datetime import datetime, timezone
from typing import Optional

from pydantic import BaseModel, field_serializer


def _serialize_dt(dt: Optional[datetime]) -> Optional[str]:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()


class AiLogOut(BaseModel):
    """AI日志列表项"""
    id: int
    task_id: Optional[int] = None
    task_name: Optional[str] = None
    project_id: Optional[int] = None
    project_name: Optional[str] = None
    user_id: Optional[int] = None
    user_name: Optional[str] = None
    file_id: Optional[int] = None
    file_name: Optional[str] = None
    chunk_index: Optional[int] = None
    model_name: str
    prompt_tokens: Optional[int] = None
    completion_tokens: Optional[int] = None
    total_tokens: Optional[int] = None
    duration_ms: Optional[int] = None
    status: str
    create_time: datetime

    model_config = {"from_attributes": True}

    _ser_create = field_serializer("create_time")(_serialize_dt)


class AiLogDetailOut(BaseModel):
    """AI日志详情(含Prompt和Response)"""
    id: int
    task_id: Optional[int] = None
    task_name: Optional[str] = None
    project_id: Optional[int] = None
    project_name: Optional[str] = None
    user_id: Optional[int] = None
    user_name: Optional[str] = None
    file_id: Optional[int] = None
    file_name: Optional[str] = None
    chunk_index: Optional[int] = None
    model_name: str
    prompt_tokens: Optional[int] = None
    completion_tokens: Optional[int] = None
    total_tokens: Optional[int] = None
    prompt: Optional[str] = None
    response: Optional[str] = None
    status: str
    error_message: Optional[str] = None
    duration_ms: Optional[int] = None
    create_time: datetime

    model_config = {"from_attributes": True}

    _ser_create = field_serializer("create_time")(_serialize_dt)
