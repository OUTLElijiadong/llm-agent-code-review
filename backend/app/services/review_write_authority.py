"""Current project authorization for mutations of review tasks and issues."""

from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.core.exceptions import ConflictError, NotFoundError
from app.models.review_issue import ReviewIssue
from app.models.review_task import ReviewTask
from app.models.user import User
from app.services import project_member_service


def _lock_conflict(db: Session, exc: OperationalError) -> None:
    code = getattr(exc.orig, "args", (None,))[0]
    if db.get_bind().dialect.name == "mysql" and code in (1205, 1213, 3572):
        db.rollback()
        raise ConflictError("项目权限或审查任务正在变更，请稍后重试", code=40901) from exc
    raise exc


def lock_task_for_write(db: Session, user: User, task_id: int, *, allow_deleted: bool = False) -> ReviewTask:
    """Keep authorization and the task's project scope locked until commit."""
    original = db.get(ReviewTask, task_id)
    if not original:
        raise NotFoundError("审查任务不存在", code=40400)
    project_id = original.project_id
    try:
        project_member_service.require_project_access(db, project_id, user, need_write=True, lock=True, nowait=True)
        current = (db.query(ReviewTask).filter_by(id=task_id).populate_existing()
                   .with_for_update(nowait=True).one_or_none())
        if current is None or current.project_id != project_id or (current.status == "deleted" and not allow_deleted):
            raise NotFoundError("审查任务不存在", code=40400)
        return current
    except OperationalError as exc:
        _lock_conflict(db, exc)


def lock_issue_for_write(db: Session, user: User, issue_id: int) -> ReviewIssue:
    """Recheck both the task and issue scope after current project authorization."""
    original = db.get(ReviewIssue, issue_id)
    if not original:
        raise NotFoundError("问题不存在", code=40400)
    task_id = original.task_id
    lock_task_for_write(db, user, task_id)
    try:
        current = (db.query(ReviewIssue).filter_by(id=issue_id).populate_existing()
                   .with_for_update(nowait=True).one_or_none())
        if current is None or current.task_id != task_id:
            raise NotFoundError("问题不存在", code=40400)
        return current
    except OperationalError as exc:
        _lock_conflict(db, exc)
