"""
审查任务API路由
"""
from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.dependencies import get_current_user
from app.core.permission_codes import PermissionCode
from app.core.rbac_dependency import require_permission
from app.models.user import User
from app.schemas.common import PageOut, Resp
from app.schemas.review import IssueOut, ReviewStartIn, TaskDetailOut, TaskOut
from app.services import review_service

router = APIRouter()


@router.post("/start", response_model=Resp[dict],
             dependencies=[Depends(require_permission(PermissionCode.REVIEW_START))])
def start(payload: ReviewStartIn, db: Session = Depends(get_db),
          user: User = Depends(get_current_user)):
    """启动代码审查(异步):立即返回 running 任务,前端轮询任务详情查看进度/结果"""
    task = review_service.start(db, user=user, payload=payload)
    return Resp(data={"task_id": task.id, "status": task.status})


@router.get("/tasks", response_model=Resp[PageOut[TaskOut]],
            dependencies=[Depends(require_permission(PermissionCode.REVIEW_VIEW))])
def list_tasks(
    project_id: int = Query(None),
    status: str = Query(""),
    start: str = Query(""),
    end: str = Query(""),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """审查任务列表"""
    result = review_service.list_tasks(db, user, project_id, status, start, end, page, page_size)
    return Resp(data=PageOut(**result))


@router.get("/tasks/{task_id}", response_model=Resp[TaskDetailOut],
            dependencies=[Depends(require_permission(PermissionCode.REVIEW_VIEW))])
def get_task(task_id: int, db: Session = Depends(get_db),
             user: User = Depends(get_current_user)):
    """审查任务详情"""
    data = review_service.get_task_detail(db, user, task_id)
    return Resp(data=TaskDetailOut(**data))


@router.get("/tasks/{task_id}/issues", response_model=Resp[PageOut[IssueOut]],
            dependencies=[Depends(require_permission(PermissionCode.ISSUE_VIEW))])
def list_task_issues(
    task_id: int,
    file_id: int = Query(None),
    severity: str = Query(""),
    issue_type: str = Query(""),
    status: str = Query(""),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=100),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """审查任务问题列表"""
    result = review_service.list_task_issues(db, user, task_id, file_id, severity, issue_type, status, page, page_size)
    return Resp(data=PageOut(**result))


@router.delete("/tasks/{task_id}", response_model=Resp[None],
               dependencies=[Depends(require_permission(PermissionCode.REVIEW_CANCEL))])
def delete_task(task_id: int, db: Session = Depends(get_db),
                user: User = Depends(get_current_user)):
    """删除审查任务"""
    review_service.delete_task(db, user, task_id)
    return Resp(data=None)


@router.post("/tasks/{task_id}/cancel", response_model=Resp[None],
             dependencies=[Depends(require_permission(PermissionCode.REVIEW_CANCEL))])
def cancel_task(task_id: int, db: Session = Depends(get_db),
                user: User = Depends(get_current_user)):
    """取消正在运行的审查任务"""
    review_service.cancel_task(db, user, task_id)
    return Resp(data=None)
