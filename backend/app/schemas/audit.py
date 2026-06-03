"""
操作审计相关 Pydantic Schema
"""
from datetime import datetime
from typing import Optional

from pydantic import BaseModel


class AuditLogOut(BaseModel):
    """审计日志列表项"""
    id: int
    actor_id: Optional[int] = None
    actor_name: Optional[str] = None
    action: str
    target_type: Optional[str] = None
    target_id: Optional[str] = None
    detail: Optional[str] = None
    status: str
    ip: Optional[str] = None
    create_time: datetime

    model_config = {"from_attributes": True}
