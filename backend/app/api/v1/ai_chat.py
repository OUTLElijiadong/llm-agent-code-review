"""
Agent 助手聊天 + 语言检测 + 项目分析 API 路由
全部通过 Orchestrator 主调度 Agent 委派给专业子 Agent 执行
"""
from typing import List, Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.agents import AgentContext
from app.agents.orchestrator import get_orchestrator, get_request_orchestrator
from app.core.database import get_db
from app.core.dependencies import get_current_user
from app.models.user import User
from app.schemas.common import Resp

router = APIRouter()


class ChatMessage(BaseModel):
    role: str = Field(default="user", description="角色: user/assistant")
    content: str = Field(..., description="消息内容")


class ChatRequest(BaseModel):
    messages: List[ChatMessage] = Field(..., description="历史消息列表")
    stream: bool = Field(default=False, description="是否流式输出")
    trace_id: Optional[str] = Field(
        default=None,
        description="v2.0: 客户端可携带 trace_id 关联 SSE 事件流",
    )


class ClarifyQuestionOut(BaseModel):
    key: str
    label: str
    type: str
    hint: str = ""
    required: bool = True


class ClarifyOut(BaseModel):
    clarify_id: str
    intent: str
    questions: List[ClarifyQuestionOut]


class ChatResponse(BaseModel):
    content: str = Field(..., description="Agent回复内容")
    model: str = Field(..., description="实际使用的模型名称")
    trace_id: Optional[str] = Field(default=None, description="v2.0 调用链 id")
    clarify: Optional[ClarifyOut] = Field(
        default=None,
        description="v2.0 Agent 主动追问;前端识别后渲染追问表单",
    )


class LanguageDetectRequest(BaseModel):
    project_name: str = Field(..., description="项目名称")
    description: str = Field(default="", description="项目描述")


class LanguageDetectResponse(BaseModel):
    language: str = Field(..., description="检测到的语言标识")
    language_name: str = Field(..., description="语言中文名称")
    confidence: str = Field(default="high", description="置信度 high/medium/low")
    reason: str = Field(default="", description="判断理由")


class FolderAnalyzeRequest(BaseModel):
    folder_name: str = Field(default="", description="文件夹名称")
    file_names: List[str] = Field(default_factory=list, description="文件名列表")


class FolderAnalyzeResponse(BaseModel):
    project_name: str = Field(..., description="Agent建议的项目名称")
    description: str = Field(default="", description="Agent生成的项目描述")
    language: str = Field(..., description="检测到的主语言标识")
    language_name: str = Field(..., description="语言中文名")


def _ctx(user: User, trace_id: Optional[str] = None) -> AgentContext:
    extra = {"trace_id": trace_id} if trace_id else {}
    return AgentContext(user_id=user.id, extra=extra)


@router.post("/chat", response_model=Resp[ChatResponse])
def agent_chat(
    payload: ChatRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Agent 聊天接口 — 通过对话调控所有 Agent"""
    orch = get_request_orchestrator(db, user=current_user)
    messages = [{"role": m.role, "content": m.content} for m in payload.messages]
    ctx = _ctx(current_user, payload.trace_id)
    result = orch.chat(messages, ctx=ctx)
    if not result.success:
        from app.ai.exceptions import AiServiceError
        raise AiServiceError(result.error or "Agent调用失败", code=50201)
    trace_id = (ctx.extra or {}).get("trace_id")
    # 区分 v1.0 普通字符串响应 vs v2.0 Clarify 字典响应
    if isinstance(result.data, dict):
        return Resp(data=ChatResponse(
            content=result.data.get("content", ""),
            model=result.model,
            trace_id=trace_id,
            clarify=result.data.get("clarify"),
        ))
    return Resp(data=ChatResponse(
        content=result.data,
        model=result.model,
        trace_id=trace_id,
    ))


@router.post("/detect-language", response_model=Resp[LanguageDetectResponse])
def agent_detect_language(
    payload: LanguageDetectRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Agent 智能识别项目语言"""
    orch = get_request_orchestrator(db, user=current_user)
    result = orch.detect_language(
        payload.project_name, payload.description, ctx=_ctx(current_user))
    if not result.success:
        from app.ai.exceptions import AiServiceError
        raise AiServiceError(result.error or "Agent调用失败", code=50201)
    return Resp(data=LanguageDetectResponse(**result.data))


@router.post("/analyze-folder", response_model=Resp[FolderAnalyzeResponse])
def agent_analyze_folder(
    payload: FolderAnalyzeRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Agent 智能分析文件夹"""
    orch = get_request_orchestrator(db, user=current_user)
    result = orch.analyze_project(
        payload.folder_name, payload.file_names, ctx=_ctx(current_user))
    if not result.success:
        from app.ai.exceptions import AiServiceError
        raise AiServiceError(result.error or "Agent调用失败", code=50201)
    return Resp(data=FolderAnalyzeResponse(**result.data))


@router.get("/agents", response_model=Resp[dict])
def list_agents(current_user: User = Depends(get_current_user)):
    """列出所有已注册的 Agent 及其描述"""
    orch = get_orchestrator()
    return Resp(data=orch.list_agents())
