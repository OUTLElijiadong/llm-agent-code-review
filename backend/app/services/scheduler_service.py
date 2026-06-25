"""Agent 治理调度服务。

本阶段持久化任务定义与运行记录，并预留 APScheduler 接入点。
"""
import json
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.core.exceptions import NotFoundError
from app.models.agent_governance import AgentJob, AgentJobRun
from app.services import agent_knowledge_service, agent_memory_service

_DEFAULT_JOBS = (
    ("daily_agent_knowledge_crawl", "crawl", "knowledge_distiller", "daily@02:00"),
    ("daily_agent_reflection", "reflection", "reflection", "daily@03:00"),
    ("daily_agent_evolution", "evolution", "evolution", "daily@04:00"),
)


def _utcnow() -> datetime:
    """获取 UTC 当前时间。

    Returns:
        datetime: 当前 UTC 时间。
    """
    return datetime.now(timezone.utc)


def ensure_default_jobs(db: Session) -> list[AgentJob]:
    """确保默认治理调度任务存在。

    Args:
        db: 数据库会话。

    Returns:
        list[AgentJob]: 当前任务列表。
    """
    for code, job_type, agent_code, schedule in _DEFAULT_JOBS:
        exists = db.query(AgentJob).filter(AgentJob.job_code == code).first()
        if exists:
            continue
        db.add(AgentJob(
            job_code=code,
            job_type=job_type,
            agent_code=agent_code,
            schedule=schedule,
            status="enabled",
            config_json=json.dumps({"scheduler": "apscheduler-ready"}, ensure_ascii=False),
        ))
    db.commit()
    return list_jobs(db)


def list_jobs(db: Session) -> list[AgentJob]:
    """查询治理调度任务。

    Args:
        db: 数据库会话。

    Returns:
        list[AgentJob]: 任务列表。
    """
    return db.query(AgentJob).order_by(AgentJob.id.asc()).all()


def update_job(db: Session, job_id: int, payload: dict) -> AgentJob:
    """更新治理调度任务配置。

    Args:
        db: 数据库会话。
        job_id: 任务 ID。
        payload: 更新字段。

    Returns:
        AgentJob: 更新后的任务。

    Raises:
        NotFoundError: 任务不存在。
    """
    job = db.get(AgentJob, job_id)
    if not job:
        raise NotFoundError("调度任务不存在", code=40400)
    if payload.get("schedule") is not None:
        job.schedule = payload["schedule"]
    if payload.get("status") is not None:
        job.status = payload["status"]
    if payload.get("config_json") is not None:
        job.config_json = json.dumps(payload["config_json"], ensure_ascii=False)
    db.commit()
    db.refresh(job)
    return job


def run_job(db: Session, job_id: int) -> AgentJobRun:
    """手动运行一个治理调度任务。

    Args:
        db: 数据库会话。
        job_id: 任务 ID。

    Returns:
        AgentJobRun: 任务运行记录。

    Raises:
        NotFoundError: 任务不存在。
    """
    job = db.get(AgentJob, job_id)
    if not job:
        raise NotFoundError("调度任务不存在", code=40400)
    run = AgentJobRun(job_id=job.id, status="running", started_at=_utcnow())
    db.add(run)
    db.commit()
    db.refresh(run)
    try:
        result = _execute_job(db, job)
        run.status = "success"
        run.result_json = json.dumps(result, ensure_ascii=False)
        run.finished_at = _utcnow()
        job.last_run_at = run.finished_at
    except Exception as exc:  # noqa: BLE001 - 调度失败需落库
        run.status = "failed"
        run.error = str(exc)
        run.finished_at = _utcnow()
    db.commit()
    db.refresh(run)
    return run


def _execute_job(db: Session, job: AgentJob) -> dict:
    """执行调度任务的本地动作。

    Args:
        db: 数据库会话。
        job: 调度任务。

    Returns:
        dict: 执行结果摘要。
    """
    if job.job_type == "crawl":
        return agent_knowledge_service.crawl_enabled_sources(db, agent_code=job.agent_code or "")
    if job.job_type == "reflection":
        item = agent_memory_service.add_memory(
            db,
            agent_code=job.agent_code or "reflection",
            title="每日反思任务",
            content="调度触发自我反思，已将本次反思沉淀到 Agent 独立记忆。",
            memory_type="reflection",
            weight=0.5,
            source_ref=f"job:{job.id}",
        )
        return {"memory_id": item.id}
    if job.job_type == "evolution":
        return {"message": "自进化任务已记录，详细执行由 evolution_service 承接"}
    return {"message": f"未知任务类型 {job.job_type}，已跳过"}
