"""
AI调用日志服务模块(管理员)
"""
from sqlalchemy.orm import Session

from app.core.exceptions import NotFoundError
from app.core.pagination import Pagination
from app.models.agent_response_run import AgentResponseRun
from app.models.ai_call_log import AiCallLog
from app.models.code_file import CodeFile
from app.models.project import Project
from app.models.review_task import ReviewTask
from app.models.user import User
from app.services.ai_usage_context import ATTRIBUTION_FIELDS


def list_logs(db: Session, task_id: int = None, user_id: int = None, status: str = "",
              start_date: str = "", end_date: str = "", page: int = 1, page_size: int = 20,
              *, root_agent_run_id: int = None, agent_team_id: int = None,
              agent_team_task_id: int = None, tool_execution_id: int = None) -> dict:
    """查询AI调用日志列表

    Args:
        db: 数据库会话
        task_id: 任务ID过滤
        user_id: 用户ID过滤
        status: 状态过滤
        start_date: 开始日期
        end_date: 结束日期
        page: 页码
        page_size: 每页数量

    Returns:
        dict: 分页响应
    """
    q = db.query(AiCallLog)
    for field, value in (("root_agent_run_id", root_agent_run_id), ("agent_team_id", agent_team_id),
                         ("agent_team_task_id", agent_team_task_id), ("tool_execution_id", tool_execution_id)):
        if value is not None:
            q = q.filter(getattr(AiCallLog, field) == value)
    if task_id:
        q = q.filter(AiCallLog.task_id == task_id)
    if user_id:
        q = q.filter(AiCallLog.user_id == user_id)
    if status:
        q = q.filter(AiCallLog.status == status)
    if start_date:
        q = q.filter(AiCallLog.create_time >= start_date)
    if end_date:
        q = q.filter(AiCallLog.create_time <= end_date + " 23:59:59")

    total = q.count()
    pagination = Pagination(page, page_size, total)
    rows = q.order_by(AiCallLog.create_time.desc()).offset(pagination.offset).limit(pagination.page_size).all()
    items = [_to_traceable_dict(db, row, include_detail=False) for row in rows]
    return pagination.to_dict(items)


def get_log_detail(db: Session, log_id: int, *, actor: User | None = None) -> dict:
    """获取AI调用日志详情

    Args:
        db: 数据库会话
        log_id: 日志ID

    Returns:
        dict: 带追溯字段的日志详情

    Raises:
        NotFoundError: 日志不存在
    """
    log = db.get(AiCallLog, log_id)
    if not log:
        raise NotFoundError("日志不存在", code=40400)
    return _to_traceable_dict(db, log, include_detail=True, actor_id=actor.id if actor else None)


def _to_traceable_dict(
    db: Session, log: AiCallLog, include_detail: bool = False, *, actor_id: int | None = None,
) -> dict:
    """构造带任务/项目/文件/用户快照的 AI 调用日志响应。

    Args:
        db: 数据库会话
        log: AI 调用日志 ORM 对象
        include_detail: 是否包含 prompt/response 等大字段

    Returns:
        dict: 可直接序列化给前端展示的追溯日志
    """
    task = db.get(ReviewTask, log.task_id) if log.task_id else None
    code_file = db.get(CodeFile, log.file_id) if log.file_id else None
    user = db.get(User, log.user_id) if log.user_id else None
    project_id = code_file.project_id if code_file else (task.project_id if task else None)
    project = db.get(Project, project_id) if project_id else None

    root_run = db.get(AgentResponseRun, log.root_agent_run_id) if log.root_agent_run_id else None
    token_values = (log.prompt_tokens, log.completion_tokens, log.total_tokens)
    data = {
        **{field: getattr(log, field) for field in ATTRIBUTION_FIELDS},
        "root_agent_run_key": root_run.run_id if root_run else None,
        "usage_state": ("unknown" if all(value is None for value in token_values)
                        else "reported" if all(value is not None for value in token_values) else "partial"),
        "id": log.id,
        "task_id": log.task_id,
        "task_name": task.task_name if task else None,
        "project_id": project_id,
        "project_name": project.project_name if project else None,
        "user_id": log.user_id,
        "user_name": user.username if user else None,
        "file_id": log.file_id,
        "file_name": code_file.file_name if code_file else None,
        "chunk_index": log.chunk_index,
        "model_name": log.model_name,
        "agent_label": log.agent_label,
        "prompt_tokens": log.prompt_tokens,
        "completion_tokens": log.completion_tokens,
        "total_tokens": log.total_tokens,
        "duration_ms": log.duration_ms,
        "status": log.status,
        "create_time": log.create_time,
    }
    if include_detail:
        # 管理员可以核对全局用量，但不能通过运行派生的模型账本读取他人对话。
        # 历史记录可能没有 agent_label；没有正式审查任务归属时也不能默认公开原文。
        private_call = (not log.task_id or log.agent_label in {"chat_assistant", "manager", "admin_copilot"}
                        or any(getattr(log, field, None) is not None for field in ATTRIBUTION_FIELDS))
        redact_content = bool(private_call and (actor_id is None or actor_id != log.user_id))
        data.update({
            "prompt": None if redact_content else log.prompt,
            "response": None if redact_content else log.response,
            "error_message": None if redact_content else log.error_message,
            "content_redacted": redact_content,
        })
    return data
