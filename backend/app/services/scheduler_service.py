"""Agent 治理调度服务。

本阶段持久化任务定义与运行记录，并预留 APScheduler 接入点。

v3.0 AgentSkill 升级新增:
- Skill 调度任务类型: skill_evolution / skill_proactive
- ensure_skill_jobs(): 注册 per-Agent 的 Skill 定时任务(每日 03:00 跑 evolution,
  每小时跑 proactive_check)
- _execute_skill_evolution / _execute_skill_proactive: 实际执行 Skill 的本地函数
"""
import json
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from sqlalchemy.orm import Session

from app.core.exceptions import NotFoundError
from app.models.agent_governance import AgentJob, AgentJobRun
from app.services import agent_knowledge_service, agent_memory_service

_DEFAULT_JOBS = (
    ("daily_agent_knowledge_crawl", "crawl", "knowledge_distiller", "daily@02:00"),
    ("daily_agent_reflection", "reflection", "reflection", "daily@03:00"),
    ("daily_agent_evolution", "evolution", "evolution", "daily@04:00"),
)

# v3.0 AgentSkill: per-Agent 进化任务(每日 03:00 跑 self_improve action=evolve)
# 14 个 Agent 各一条,与原 daily_agent_evolution 共存(后者保持兼容)
_SKILL_EVOLUTION_AGENTS = (
    "code_reviewer", "security_sentinel", "language_detector",
    "project_analyzer", "code_file_manager", "dashboard",
    "rule_manager", "reporter", "ai_prompt", "project_manager",
    "review_orchestrator", "chat_assistant", "orchestrator", "evolution",
)

# v3.0 AgentSkill: per-Agent 主动监测任务(每小时跑 proactive action=check_proactive)
# 同样 14 个 Agent,与 evolution 任务用不同 cron 表达式
_SKILL_PROACTIVE_AGENTS = _SKILL_EVOLUTION_AGENTS


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
    # v3.0 AgentSkill: 同步注册 per-Agent 的 Skill 调度任务
    try:
        ensure_skill_jobs(db)
    except Exception as exc:  # noqa: BLE001 - Skill 调度注册失败不阻断主流程
        from loguru import logger
        logger.warning(f"[scheduler_service] ensure_skill_jobs 失败(不影响主流程): {exc}")
    return list_jobs(db)


def ensure_skill_jobs(db: Session) -> list[AgentJob]:
    """注册 per-Agent 的 Skill 定时任务(v3.0 AgentSkill 升级)

    为每个 Agent 注册两条 cron 任务:
    1. 每日 03:00 跑 {agent}.self_improve action=evolve (自进化)
    2. 每小时跑 {agent}.proactive action=check_proactive (主动监测)

    Args:
        db: 数据库会话。

    Returns:
        list[AgentJob]: 当前已注册的 Skill 调度任务列表。
    """
    added: list[AgentJob] = []
    # 每日 03:00 跑 evolution
    for agent_code in _SKILL_EVOLUTION_AGENTS:
        job_code = f"daily_skill_evolution_{agent_code}"
        exists = db.query(AgentJob).filter(AgentJob.job_code == job_code).first()
        if exists:
            continue
        job = AgentJob(
            job_code=job_code,
            job_type="skill_evolution",
            agent_code=agent_code,
            schedule="daily@03:00",
            status="enabled",
            config_json=json.dumps({
                "scheduler": "apscheduler-ready",
                "skill_name": f"{agent_code}.self_improve",
                "action": "evolve",
                "trigger_type": "scheduled",
            }, ensure_ascii=False),
        )
        db.add(job)
        added.append(job)

    # 每小时跑 proactive(用 cron 表达式 hourly@*:00 表示)
    for agent_code in _SKILL_PROACTIVE_AGENTS:
        job_code = f"hourly_skill_proactive_{agent_code}"
        exists = db.query(AgentJob).filter(AgentJob.job_code == job_code).first()
        if exists:
            continue
        job = AgentJob(
            job_code=job_code,
            job_type="skill_proactive",
            agent_code=agent_code,
            schedule="hourly@*:00",
            status="enabled",
            config_json=json.dumps({
                "scheduler": "apscheduler-ready",
                "skill_name": f"{agent_code}.proactive",
                "action": "check_proactive",
                "trigger_type": "scheduled",
            }, ensure_ascii=False),
        )
        db.add(job)
        added.append(job)

    if added:
        db.commit()
        for j in added:
            db.refresh(j)
    return added


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
        run.result_json = json.dumps(result, ensure_ascii=False, default=str)
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
    # v3.0 AgentSkill: Skill 调度任务
    if job.job_type == "skill_evolution":
        return _execute_skill_evolution(db, job)
    if job.job_type == "skill_proactive":
        return _execute_skill_proactive(db, job)
    return {"message": f"未知任务类型 {job.job_type}，已跳过"}


def _execute_skill_evolution(db: Session, job: AgentJob) -> Dict[str, Any]:
    """执行 Skill 自进化调度任务(v3.0 AgentSkill 升级)

    通过 Orchestrator.invoke_skill 调用 {agent_name}.self_improve action=evolve,
    trigger_type="scheduled" 确保 agent_skill_record 正确归类。

    Args:
        db: 数据库会话。
        job: 调度任务(含 agent_code 与 config_json)。

    Returns:
        dict: 执行结果摘要, 含 success/effect/duration_ms/record_id 字段。
    """
    from loguru import logger

    from app.agents.base import AgentContext
    from app.agents.orchestrator import Orchestrator

    agent_name = job.agent_code or "evolution"
    skill_name = f"{agent_name}.self_improve"
    config = _parse_job_config(job.config_json)
    action = config.get("action", "evolve")

    logger.info(
        f"[scheduler_service] 触发定时自进化: agent={agent_name} "
        f"skill={skill_name} action={action}"
    )

    orch = Orchestrator(register=False)
    orch._db = db
    ctx = AgentContext(extra={"job_id": job.id, "job_code": job.job_code})
    try:
        result = orch.invoke_skill(
            agent_name=agent_name,
            skill_name=skill_name,
            params={"action": action},
            ctx=ctx,
            trigger_type="scheduled",
            trigger_source=f"scheduler:cron:{job.schedule}",
        )
    except Exception as exc:  # noqa: BLE001 - 调度异常需转成结构化返回
        logger.warning(
            f"[scheduler_service] 定时自进化异常: agent={agent_name} error={exc}"
        )
        return {
            "success": False,
            "error": str(exc),
            "agent_name": agent_name,
            "skill_name": skill_name,
        }

    data = result.data if isinstance(result.data, dict) else {}
    return {
        "success": result.success,
        "effect": data.get("effect", "?"),
        "duration_ms": data.get("duration_ms", result.duration_ms),
        "record_id": data.get("record_id"),
        "agent_name": agent_name,
        "skill_name": skill_name,
    }


def _execute_skill_proactive(db: Session, job: AgentJob) -> Dict[str, Any]:
    """执行 Skill 主动监测调度任务(v3.0 AgentSkill 升级)

    通过 Orchestrator.invoke_skill 调用 {agent_name}.proactive action=check_proactive,
    trigger_type="scheduled" 确保 agent_skill_record 正确归类。

    Args:
        db: 数据库会话。
        job: 调度任务(含 agent_code 与 config_json)。

    Returns:
        dict: 执行结果摘要, 含 success/effect/duration_ms/record_id 字段。
    """
    from loguru import logger

    from app.agents.base import AgentContext
    from app.agents.orchestrator import Orchestrator

    agent_name = job.agent_code or "orchestrator"
    skill_name = f"{agent_name}.proactive"
    config = _parse_job_config(job.config_json)
    action = config.get("action", "check_proactive")

    logger.info(
        f"[scheduler_service] 触发定时主动监测: agent={agent_name} "
        f"skill={skill_name} action={action}"
    )

    orch = Orchestrator(register=False)
    orch._db = db
    ctx = AgentContext(extra={"job_id": job.id, "job_code": job.job_code})
    try:
        result = orch.invoke_skill(
            agent_name=agent_name,
            skill_name=skill_name,
            params={"action": action},
            ctx=ctx,
            trigger_type="scheduled",
            trigger_source=f"scheduler:cron:{job.schedule}",
        )
    except Exception as exc:  # noqa: BLE001 - 调度异常需转成结构化返回
        logger.warning(
            f"[scheduler_service] 定时主动监测异常: agent={agent_name} error={exc}"
        )
        return {
            "success": False,
            "error": str(exc),
            "agent_name": agent_name,
            "skill_name": skill_name,
        }

    data = result.data if isinstance(result.data, dict) else {}
    return {
        "success": result.success,
        "effect": data.get("effect", "?"),
        "duration_ms": data.get("duration_ms", result.duration_ms),
        "record_id": data.get("record_id"),
        "agent_name": agent_name,
        "skill_name": skill_name,
    }


def _parse_job_config(config_json: Optional[str]) -> dict:
    """解析 AgentJob.config_json 字段

    Args:
        config_json: JSON 字符串(可能为 None 或非法 JSON)

    Returns:
        dict: 解析后的配置字典(失败时返回空 dict)
    """
    if not config_json:
        return {}
    try:
        result = json.loads(config_json)
        return result if isinstance(result, dict) else {}
    except (TypeError, ValueError):
        return {}
