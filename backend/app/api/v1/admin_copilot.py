"""管理员副驾驶 API。"""
from typing import Any, Dict, List, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.dependencies import require_admin
from app.models.user import User
from app.schemas.common import Resp
from app.services import admin_chat_history_service

router = APIRouter()


class AdminCopilotRequest(BaseModel):
    message: str = Field(default="", max_length=2000)
    session_id: str = Field(min_length=8, max_length=128)
    action_token: str = Field(default="", max_length=4096)
    decision: Literal["", "confirm", "cancel"] = ""
    confirmation_text: str = Field(default="", max_length=20)

    @field_validator("session_id")
    @classmethod
    def validate_session_id(cls, value: str) -> str:
        if not all(char.isalnum() or char in "-_" for char in value):
            raise ValueError("session_id 只能包含字母、数字、连字符和下划线")
        return value


class AdminCopilotMessage(BaseModel):
    type: Literal["text", "confirm", "danger_confirm", "report", "alert", "table"]
    title: Optional[str] = None
    content: Optional[str] = None
    operation: Optional[str] = None
    impact: Optional[str] = None
    consequence: Optional[str] = None
    action_token: Optional[str] = None
    status: Optional[str] = None
    summary: Optional[str] = None
    counts: Optional[Dict[str, int]] = None
    count_labels: Optional[Dict[str, str]] = None
    risks: Optional[List[str]] = None
    suggestions: Optional[List[str]] = None
    severity: Optional[str] = None
    description: Optional[str] = None
    suggestion: Optional[str] = None
    action_label: Optional[str] = None
    action_prompt: Optional[str] = None
    columns: Optional[List[str]] = None
    rows: Optional[List[Dict[str, Any]]] = None
    total: Optional[int] = None
    collapsed: Optional[bool] = None
    message_id: Optional[int] = None
    user_message_id: Optional[int] = None


@router.post("/chat", response_model=Resp[AdminCopilotMessage])
def chat(
    payload: AdminCopilotRequest,
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
):
    """拒绝旧模板协议，防止请求绕过 Responses Agent 工具循环。"""
    del payload, db, admin
    raise HTTPException(
        status_code=410,
        detail="旧管理副驾驶协议已停用，请使用 /api/agent-responses/stream",
    )


@router.get("/history", response_model=Resp[dict])
def history(
    session_id: str = Query(min_length=8, max_length=128),
    after_id: int = Query(default=0, ge=0),
    limit: int = Query(default=200, ge=1, le=500),
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
):
    """读取当前管理员自己的副驾驶历史，支持增量轮询。"""
    if not all(char.isalnum() or char in "-_" for char in session_id):
        raise HTTPException(status_code=422, detail="session_id 只能包含字母、数字、连字符和下划线")
    return Resp(data=admin_chat_history_service.list_history(
        db, admin, session_id, after_id=after_id, limit=limit,
    ))
