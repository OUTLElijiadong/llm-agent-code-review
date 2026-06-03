"""AI 提示词生成 API 路由 (v2.0 新增)

提供按问题 / 按任务生成可粘贴的外部 AI 修复提示词。
全部经 Orchestrator 委派给 AiPromptAgent 执行。
"""
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.agents import AgentContext
from app.agents.orchestrator import get_orchestrator
from app.ai.exceptions import AiServiceError
from app.core.database import get_db
from app.core.dependencies import get_current_user
from app.models.user import User
from app.schemas.ai_prompt import (
    AiPromptBundleOut,
    AiPromptIssueIn,
    AiPromptProjectIn,
    AiPromptTaskIn,
    AiPromptToolOut,
)
from app.schemas.common import Resp

router = APIRouter()


def _ctx(user: User) -> AgentContext:
    return AgentContext(user_id=user.id)


@router.get("/tools", response_model=Resp[list[AiPromptToolOut]])
def list_tools(_: User = Depends(get_current_user)):
    """支持的目标 AI 工具枚举(供前端下拉)"""
    orch = get_orchestrator()
    items = orch.ai_prompt.list_supported_tools()
    return Resp(data=[AiPromptToolOut(**i) for i in items])


@router.post("/issue", response_model=Resp[AiPromptBundleOut])
def generate_for_issue(
    payload: AiPromptIssueIn,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """单条问题生成 AI 提示词"""
    orch = get_orchestrator()
    orch.inject_db(db, user=user)
    result = orch.generate_ai_prompt_for_issue(
        issue_id=payload.issue_id,
        target_tool=payload.target_tool,
        use_llm=payload.use_llm,
        ctx=_ctx(user),
    )
    if not result.success:
        raise AiServiceError(result.error or "提示词生成失败", code=50210)
    return Resp(data=AiPromptBundleOut(**result.data))


@router.post("/task", response_model=Resp[AiPromptBundleOut])
def generate_for_task(
    payload: AiPromptTaskIn,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """审查任务下批量生成提示词"""
    orch = get_orchestrator()
    orch.inject_db(db, user=user)
    result = orch.generate_ai_prompt_for_task(
        task_id=payload.task_id,
        target_tool=payload.target_tool,
        severity_filter=payload.severity,
        use_llm=payload.use_llm,
        ctx=_ctx(user),
    )
    if not result.success:
        raise AiServiceError(result.error or "提示词生成失败", code=50210)
    return Resp(data=AiPromptBundleOut(**result.data))


@router.post("/project", response_model=Resp[AiPromptBundleOut])
def generate_for_project(
    payload: AiPromptProjectIn,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """项目级 AI 修复手册: 按严重度优先,取前 top_n 条问题生成提示词"""
    orch = get_orchestrator()
    orch.inject_db(db, user=user)
    result = orch.generate_ai_prompt_for_project(
        project_id=payload.project_id,
        target_tool=payload.target_tool,
        top_n=payload.top_n,
        use_llm=payload.use_llm,
        ctx=_ctx(user),
    )
    if not result.success:
        raise AiServiceError(result.error or "提示词生成失败", code=50210)
    return Resp(data=AiPromptBundleOut(**result.data))
