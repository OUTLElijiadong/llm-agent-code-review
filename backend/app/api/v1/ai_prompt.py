"""AI 提示词生成 API 路由 (v2.0 新增)

提供按问题 / 按任务生成可粘贴的外部 AI 修复提示词。
全部经 Orchestrator 委派给 AiPromptAgent 执行。
"""
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.agents import AgentContext
from app.agents.orchestrator import get_orchestrator, get_request_orchestrator
from app.ai.exceptions import AiServiceError
from app.core.database import get_db
from app.core.dependencies import get_current_user
from app.core.exceptions import NotFoundError
from app.core.permission_codes import PermissionCode
from app.core.rbac_dependency import require_permission
from app.models.review_issue import ReviewIssue
from app.models.review_task import ReviewTask
from app.models.user import User
from app.schemas.ai_prompt import (
    AiPromptBundleOut,
    AiPromptIssueIn,
    AiPromptProjectIn,
    AiPromptTaskIn,
    AiPromptToolOut,
)
from app.schemas.common import Resp
from app.services.project_member_service import require_project_access, require_project_execution

router = APIRouter()


def _ctx(user: User) -> AgentContext:
    return AgentContext(user_id=user.id)


def _require_source_access(db: Session, user: User, source: str, identifier: int, use_llm: bool) -> None:
    """Validate real source scope before orchestration; auth errors retain their 403/404 contract."""
    project_id = identifier
    if source == "issue":
        issue = db.get(ReviewIssue, identifier)
        task = db.get(ReviewTask, issue.task_id) if issue is not None else None
    elif source == "task":
        task = db.get(ReviewTask, identifier)
    else:
        task = None
    if source in {"issue", "task"}:
        if task is None or task.status == "deleted":
            raise NotFoundError("提示词来源不存在或不可见", code=40400)
        project_id = int(task.project_id)
    if use_llm:
        require_project_execution(db, project_id, user)
    else:
        require_project_access(db, project_id, user)


@router.get("/tools", response_model=Resp[list[AiPromptToolOut]])
def list_tools(_: User = Depends(get_current_user)):
    """支持的目标 AI 工具枚举(供前端下拉)"""
    orch = get_orchestrator()
    items = orch.ai_prompt.list_supported_tools()
    return Resp(data=[AiPromptToolOut(**i) for i in items])


@router.post("/issue", response_model=Resp[AiPromptBundleOut],
             dependencies=[Depends(require_permission(PermissionCode.ISSUE_VIEW))])
def generate_for_issue(
    payload: AiPromptIssueIn,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """单条问题生成 AI 提示词"""
    _require_source_access(db, user, "issue", payload.issue_id, payload.use_llm)
    orch = get_request_orchestrator(db, user=user)
    result = orch.generate_ai_prompt_for_issue(
        issue_id=payload.issue_id,
        target_tool=payload.target_tool,
        use_llm=payload.use_llm,
        ctx=_ctx(user),
    )
    if not result.success:
        raise AiServiceError(result.error or "提示词生成失败", code=50210)
    return Resp(data=AiPromptBundleOut(**result.data))


@router.post("/task", response_model=Resp[AiPromptBundleOut],
             dependencies=[Depends(require_permission(PermissionCode.REVIEW_VIEW))])
def generate_for_task(
    payload: AiPromptTaskIn,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """审查任务下批量生成提示词"""
    _require_source_access(db, user, "task", payload.task_id, payload.use_llm)
    orch = get_request_orchestrator(db, user=user)
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


@router.post("/project", response_model=Resp[AiPromptBundleOut],
             dependencies=[Depends(require_permission(PermissionCode.PROJECT_VIEW))])
def generate_for_project(
    payload: AiPromptProjectIn,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """项目级 AI 修复手册: 按严重度优先,取前 top_n 条问题生成提示词"""
    _require_source_access(db, user, "project", payload.project_id, payload.use_llm)
    orch = get_request_orchestrator(db, user=user)
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
