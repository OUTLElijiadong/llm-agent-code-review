"""
审查问题API路由
"""
from typing import Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.dependencies import get_current_user
from app.core.permission_codes import PermissionCode
from app.core.rbac_dependency import require_permission
from app.models.user import User
from app.schemas.common import PageOut, Resp
from app.schemas.review import IssueBatchStatusIn, IssueListItemOut, IssueOut, IssueStatusIn
from app.services import issue_service

router = APIRouter()


@router.get("", response_model=Resp[PageOut[IssueListItemOut]],
            dependencies=[Depends(require_permission(PermissionCode.ISSUE_VIEW))])
def list_issues(
    project_id: Optional[int] = Query(None),
    task_id: Optional[int] = Query(None),
    severity: str = Query(""),
    issue_type: str = Query(""),
    status: str = Query(""),
    keyword: str = Query(""),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=1000),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """跨任务/项目分页查询审查问题(普通用户仅看自己,管理员看全部)"""
    result = issue_service.list_issues(
        db, user, project_id, task_id, severity, issue_type, status, keyword, page, page_size,
    )
    return Resp(data=PageOut(**result))


@router.get("/{issue_id}", response_model=Resp[IssueOut],
            dependencies=[Depends(require_permission(PermissionCode.ISSUE_VIEW))])
def get_issue(issue_id: int, db: Session = Depends(get_db),
              user: User = Depends(get_current_user)):
    """问题详情"""
    issue = issue_service.get_issue(db, user, issue_id)
    return Resp(data=IssueOut.model_validate(issue))


@router.put("/{issue_id}/status", response_model=Resp[None],
            dependencies=[Depends(require_permission(PermissionCode.ISSUE_HANDLE))])
def update_status(issue_id: int, payload: IssueStatusIn,
                  db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """更新问题状态(单条)"""
    issue_service.update_status(db, user, issue_id, payload.status)
    return Resp(data=None)


@router.post("/batch-status", response_model=Resp[None],
             dependencies=[Depends(require_permission(PermissionCode.ISSUE_BATCH))])
def batch_update_status(payload: IssueBatchStatusIn,
                        db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """批量更新问题状态"""
    issue_service.batch_update_status(db, user, payload.ids, payload.status)
    return Resp(data=None)
