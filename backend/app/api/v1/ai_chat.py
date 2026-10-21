"""项目创建辅助：按文件夹名和文件名列表推断项目名、描述与主语言。

旧的 /ai/chat、/ai/detect-language、/ai/agents 已在 4.1.20 下线：对话与 Agent 调用
统一由小菱（/api/agent-responses）负责，其他 Agent 只作为小菱的子 Agent 运行。
"""
from typing import List, Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.agents import AgentContext
from app.agents.orchestrator import get_request_orchestrator
from app.core.database import get_db
from app.core.dependencies import get_current_user
from app.models.user import User
from app.schemas.common import Resp

router = APIRouter()


class FolderAnalyzeRequest(BaseModel):
    folder_name: str = Field(default="", max_length=500, description="文件夹名称")
    file_names: List[str] = Field(default_factory=list, max_length=2000, description="文件名列表")


class FolderAnalyzeResponse(BaseModel):
    project_name: str = Field(..., description="Agent建议的项目名称")
    description: str = Field(default="", description="Agent生成的项目描述")
    language: str = Field(..., description="检测到的主语言标识")
    language_name: str = Field(..., description="语言中文名")


def _ctx(user: User, trace_id: Optional[str] = None) -> AgentContext:
    extra = {"trace_id": trace_id} if trace_id else {}
    return AgentContext(user_id=user.id, extra=extra)


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
