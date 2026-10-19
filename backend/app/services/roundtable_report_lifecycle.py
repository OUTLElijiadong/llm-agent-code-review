"""圆桌报告的终态转换，供 REST、WebSocket 与编排收尾共用。"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.models.review_task import ReviewTask

_CANCELLED_SUMMARY = (
    "执行状态：已取消\n"
    "结论：圆桌讨论被停止，未形成完整审查结论。\n"
    "已验证内容：仅限停止前已完成并保留的发言和执行记录。\n"
    "未完成范围：尚未完成的专家轮次、问题抽取与主持人总结。\n"
    "结果说明：未报告不代表没有问题，不能将本任务视为审查通过。"
)


def cancel_running_discussion_report(
    db: Session,
    *,
    task_id: int,
    owner_user_id: int,
) -> str:
    """条件取消仍运行的圆桌报告，返回 cancelled、terminal 或 missing。

    行锁与报告收尾中的 `ReviewTask ... FOR UPDATE` 串行化两个终态竞争：
    停止先提交则收尾只能观察到 cancelled；收尾先提交则停止观察到已完成
    状态，不再把圆桌会话错误改写为 cancelled。
    """
    if int(task_id or 0) <= 0:
        return "missing"

    report = (
        db.query(ReviewTask)
        .filter(
            ReviewTask.id == int(task_id),
            ReviewTask.user_id == int(owner_user_id),
            ReviewTask.review_type == "discuss",
        )
        .with_for_update()
        .populate_existing()
        .one_or_none()
    )
    if report is None:
        return "missing"
    if report.status == "cancelled":
        return "cancelled"
    if report.status != "running":
        return "terminal"

    report.status = "cancelled"
    report.summary = _CANCELLED_SUMMARY
    report.coverage = {
        **(report.coverage or {}),
        "stage": "cancelled",
        "score_available": False,
    }
    report.end_time = datetime.now(timezone.utc)
    db.commit()
    return "cancelled"
