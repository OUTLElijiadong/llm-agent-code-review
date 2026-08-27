"""
审查服务模块: 核心业务编排
整合项目/文件/规则/AI模块,完成代码审查全流程

v2.2(2026-06-25): Agent 集成 + 双引擎漏洞识别
    - 引擎1: 静态规则前置过滤(正则秘钥 + 静态语义规则,确定性命中,无 LLM 调用)
    - 引擎2: LLM 深度审查(通过 BaseAgent.call() 调用真实 Agent,统一事件总线/AiCallLog)
    - 多 Agent 协同审查保留三阶段流水线(并行感知 → 交叉复审 → 共识统合)
"""
import concurrent.futures
import json as json_lib
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import List, Optional

from loguru import logger
from sqlalchemy.orm import Session

from app.agents.base import AgentContext, BaseAgent
from app.agents.event_bus import AgentEventBus
from app.agents.events import AgentEvent, AgentEventType
from app.agents.registry import AgentRegistry
from app.ai.code_chunker import chunk_code
from app.ai.cvss import normalize_cvss
from app.ai.deepseek_agent import DeepSeekAgent
from app.ai.multi_agent import (
    COLLAB_CONSENSUS_SYSTEM,
    COLLAB_CONSENSUS_USER,
    COLLAB_CROSS_REVIEW_SYSTEM,
    COLLAB_CROSS_REVIEW_USER,
    ReviewAgentProfile,
    build_agent_summary,
    format_agent_section,
    get_agent_profiles,
    get_model_label,
)
from app.ai.prompt_builder import _format_experience, build_prompt
from app.ai.result_parser import Issue, normalize_severity, parse
from app.ai.scoring import SCORING_VERSION, compute_score, compute_score_breakdown, score_risk_level
from app.ai.static_analyzer import Finding
from app.ai.static_analyzer import scan_file as static_scan_file
from app.core.config import settings
from app.core.database import SessionLocal
from app.core.exceptions import NotFoundError, ValidationError
from app.core.pagination import Pagination
from app.models.code_file import CodeFile
from app.models.custom_agent import CustomAgent, CustomAgentVersion, ReviewTaskAgentRelease
from app.models.project import Project
from app.models.review_issue import ReviewIssue
from app.models.review_task import ReviewTask
from app.models.review_task_file import ReviewTaskFile
from app.models.user import User
from app.schemas.review import ReviewStartIn
from app.services.issue_merger import finding_to_issue, merge_findings_and_issues
from app.services.rule_service import get_enabled_rules

# 多 Agent 审查画像 → 注册中心 BaseAgent name 的映射(v2.2 真实调用 Agent)
_PROFILE_TO_AGENT_CODE: dict[str, str] = {
    "general": "code_reviewer",
    "security": "security_sentinel",
    "reliability": "code_reviewer",
    "performance": "code_reviewer",
    "maintainability": "code_reviewer",
}

_COLLAB_PARALLEL_THREADS = 4

# 全局审查并发上限:限制同时进行的后台审查任务数,防止 2C2G 机器上线程/内存放大
# 及撞 LLM 侧限流。超出上限的任务会在后台线程内排队等待(任务状态先入库为 running)。
_REVIEW_SEMAPHORE = threading.BoundedSemaphore(max(1, settings.review_max_concurrency))
_RECOVERING_REVIEW_TASK_IDS: set[int] = set()
_RECOVERY_LOCK = threading.Lock()


def _enabled_review_profiles(
    db: Session,
    profiles: tuple[ReviewAgentProfile, ...],
) -> tuple[ReviewAgentProfile, ...]:
    """过滤被治理后台停用的真实审查 Agent。"""
    from app.services import agent_governance_service

    return tuple(
        profile
        for profile in profiles
        if agent_governance_service.is_runtime_enabled(
            db,
            _PROFILE_TO_AGENT_CODE.get(profile.code, profile.code),
        )
    )


def _safe_commit(db: Session, task: Optional[ReviewTask] = None) -> None:
    """安全提交 — 在长时间审查中防止 MySQL 连接断开导致 Session 脏状态

    如果 session 处于 PendingRollbackError 状态,先回滚清脏再重试。
    """
    from sqlalchemy.exc import PendingRollbackError

    try:
        if task is not None:
            db.add(task)
        db.commit()
    except PendingRollbackError:
        logger.warning("[safe_commit] 检测到脏 session,执行 rollback 后重试")
        db.rollback()
        if task is not None:
            db.add(task)
        db.commit()


def start(db: Session, user: User, payload: ReviewStartIn) -> ReviewTask:
    """创建审查任务并提交后台异步执行 —— 立即返回 status=running 的任务。

    校验(项目归属/文件存在/数量上限)在请求线程内同步完成,非法请求立刻返回 4xx;
    真正耗时的多 Agent 审查流水线在后台守护线程中以独立 DB Session 运行,
    请求不再被 LLM 调用阻塞。前端通过轮询 GET /review/tasks/{id} 获取进度与最终结果。

    Args:
        db: 数据库会话
        user: 当前用户
        payload: 审查启动请求

    Returns:
        ReviewTask: 已入库、状态为 running 的审查任务

    Raises:
        NotFoundError: 项目或文件不存在
        ValidationError: 文件数量不合法
    """
    project = db.get(Project, payload.project_id)
    if not project:
        raise NotFoundError("项目不存在", code=40400)
    # v2.4: 改用 project_member 关系校验(owner/admin/reviewer 都可启动审查)
    from app.services.project_member_service import require_project_access
    require_project_access(db, project.id, user, need_write=False)
    if not 1 <= len(payload.file_ids) <= 500:
        raise ValidationError("file_ids 需为 1-500 个", code=40001)

    files = (
        db.query(CodeFile)
        .filter(
            CodeFile.id.in_(payload.file_ids),
            CodeFile.project_id == project.id,
            CodeFile.status == "active",
        )
        .all()
    )
    if len(files) != len(payload.file_ids):
        raise NotFoundError("部分文件不存在", code=40400)

    project_lang = (project.language or "").strip().lower()
    rules = get_enabled_rules(db, user.id, language=project_lang)
    review_type = payload.review_type or "standard"
    profiles = get_agent_profiles(review_type)
    from app.utils.api_resolver import resolve_api_config
    _api_cfg = resolve_api_config(db, user.id)
    agent = DeepSeekAgent(api_config=_api_cfg)

    task = ReviewTask(
        user_id=user.id,
        project_id=project.id,
        task_name=payload.task_name or f"{project.project_name}-审查",
        review_type=review_type,
        status="running",
        total_files=len(files),
        model_name=get_model_label(agent.model, profiles),
        rules_snapshot=[{"code": r.rule_code, "name": r.rule_name} for r in rules],
        start_time=datetime.now(timezone.utc),
        execution_token=uuid.uuid4().hex,
    )
    db.add(task)
    _safe_commit(db)
    db.refresh(task)
    db.add_all(ReviewTaskFile(task_id=task.id, file_id=f.id) for f in files)
    _safe_commit(db)

    threading.Thread(
        target=_run_review_task,
        args=(task.id, user.id, str(task.execution_token)),
        name=f"review-task-{task.id}",
        daemon=True,
    ).start()
    logger.info(
        f"审查任务 #{task.id} 已提交后台执行 (type={review_type}, files={len(files)})",
    )
    return task


def resume_interrupted_tasks(task_refs: list[tuple[int, int, str]]) -> int:
    """重新派发上个进程遗留的审查任务。

    每个任务先删除不完整问题并重置统计，再复用原审查入口。进程再次中断时，
    任务仍保持 running，下一次启动会继续恢复。
    """
    dispatched = 0
    for task_id, user_id, previous_token in task_refs:
        with _RECOVERY_LOCK:
            if task_id in _RECOVERING_REVIEW_TASK_IDS:
                continue
            _RECOVERING_REVIEW_TASK_IDS.add(task_id)
        threading.Thread(
            target=_resume_interrupted_task,
            args=(task_id, user_id, previous_token),
            name=f"review-recovery-{task_id}",
            daemon=True,
        ).start()
        dispatched += 1
    if dispatched:
        logger.warning("[recovery] 已派发 {} 个孤儿审查任务继续执行", dispatched)
    return dispatched


def _resume_interrupted_task(task_id: int, user_id: int, previous_token: str) -> None:
    """CAS 换发执行租约、清理部分产物后，通过原有后台入口完整重跑。"""
    db = SessionLocal()
    next_token = uuid.uuid4().hex
    try:
        claimed = (
            db.query(ReviewTask)
            .filter(
                ReviewTask.id == task_id,
                ReviewTask.status == "running",
                ReviewTask.execution_token == previous_token,
            )
            .update(
                {"execution_token": next_token},
                synchronize_session=False,
            )
        )
        if claimed != 1:
            db.rollback()
            with _RECOVERY_LOCK:
                _RECOVERING_REVIEW_TASK_IDS.discard(task_id)
            return
        task = db.query(ReviewTask).filter(ReviewTask.id == task_id).one()
        db.query(ReviewIssue).filter(ReviewIssue.task_id == task_id).delete(
            synchronize_session=False,
        )
        task.processed_files = 0
        task.total_issues = 0
        task.severe_issues = 0
        task.high_issues = 0
        task.medium_issues = 0
        task.low_issues = 0
        task.score = 0
        task.summary = None
        task.end_time = None
        task.duration_ms = 0
        task.error_message = None
        task.start_time = datetime.now(timezone.utc)
        _safe_commit(db, task)
    except Exception as exc:  # noqa: BLE001 - 恢复准备失败必须落库并隔离
        db.rollback()
        task = db.get(ReviewTask, task_id)
        if task is not None:
            task.status = "failed"
            task.error_message = f"重启后自动恢复失败: {exc}"[:500]
            task.end_time = datetime.now(timezone.utc)
            _safe_commit(db, task)
        logger.exception("[recovery] 审查任务 #{} 恢复准备失败: {}", task_id, exc)
        with _RECOVERY_LOCK:
            _RECOVERING_REVIEW_TASK_IDS.discard(task_id)
        return
    finally:
        db.close()

    try:
        _run_review_task(task_id, user_id, next_token)
    finally:
        with _RECOVERY_LOCK:
            _RECOVERING_REVIEW_TASK_IDS.discard(task_id)


def _run_review_task(task_id: int, user_id: int, execution_token: Optional[str] = None) -> None:
    """后台线程入口: 用独立 DB Session 执行完整审查流水线。

    线程没有调用方,任何异常都在此被吞掉(失败状态已由 _execute_review 落库),
    避免未捕获异常导致线程静默崩溃而任务永远停留在 running。

    Args:
        task_id: 审查任务 ID(已在请求线程中入库)
        user_id: 发起用户 ID
    """
    _REVIEW_SEMAPHORE.acquire()
    db = SessionLocal()
    try:
        task = db.get(ReviewTask, task_id)
        user = db.get(User, user_id)
        if not task or not user:
            logger.error(f"[review] 后台任务 #{task_id} 找不到 task/user,放弃执行")
            return
        active_token = (
            execution_token
            if execution_token is not None
            else str(getattr(task, "execution_token", "") or "")
        )
        if str(getattr(task, "execution_token", "") or "") != active_token:
            logger.info("[review] 后台任务 #{} 的执行租约已失效，跳过旧 Worker", task_id)
            return

        project = db.get(Project, task.project_id)
        project_lang = (project.language or "").strip().lower() if project else ""

        rules = get_enabled_rules(db, user.id, language=project_lang)

        # Agent 自进化 L1: 检索本语言历史经验注入 Prompt;失败则降级为不注入,审查照常进行
        experience_section = ""
        try:
            from app.services import experience_service
            exps = experience_service.retrieve(db, language=project_lang)
            experience_section = _format_experience(exps)
            if exps:
                logger.info(
                    f"[evolution] 注入 {len(exps)} 条历史经验到本次审查 "
                    f"(lang={project_lang or '*'})",
                )
        except Exception as e:
            logger.warning(f"[evolution] 经验检索失败,降级为不注入: {e}")
            experience_section = ""

        # 个性化:把用户画像偏好注入审查(追加到经验段);失败降级为不注入
        try:
            from app.services import personalization_service
            persona_section = personalization_service.build_review_context(
                db, user.id, language=project_lang)
            if persona_section:
                experience_section = (f"{experience_section}\n\n{persona_section}").strip()
                logger.info(f"[personalization] 已注入用户画像到审查 (user={user.id})")
        except Exception as e:
            logger.warning(f"[personalization] 审查画像注入失败,降级: {e}")

        profiles = _enabled_review_profiles(db, get_agent_profiles(task.review_type))
        from app.services.declarative_agent_runtime import DeclarativeReviewAgentFactory

        custom_profiles = (
            DeclarativeReviewAgentFactory.snapshot_profiles(db, task.id, user=user)
            if isinstance(db, Session)
            else ()
        )
        profiles = profiles + custom_profiles
        if isinstance(db, Session):
            db.commit()
        if not profiles:
            _check_cancelled(db, task, active_token, lock=True)
            task.status = "failed"
            task.error_message = "本次审查所需 Agent 均已停用，任务未执行"
            task.end_time = datetime.now(timezone.utc)
            task.duration_ms = int((task.end_time - task.start_time).total_seconds() * 1000) if task.start_time else 0
            _safe_commit(db, task)
            _emit_review_event(
                AgentEventType.FAILED,
                task,
                user,
                task.error_message,
                agent_code="review_orchestrator",
            )
            return
        # 使用解析后的 API 配置(用户自定义 > 管理员全局配置 > 系统默认 DeepSeek)
        from app.utils.api_resolver import resolve_api_config
        api_config = resolve_api_config(db, user.id)
        # 协同审查仍需 DeepSeekAgent(三阶段流水线需要 system_prompt/user_prompt 灵活传入)
        collab_agent = DeepSeekAgent(api_config=api_config)
        files = (
            db.query(CodeFile)
            .join(ReviewTaskFile, ReviewTaskFile.file_id == CodeFile.id)
            .filter(ReviewTaskFile.task_id == task.id)
            .order_by(ReviewTaskFile.id.asc())
            .all()
        )

        _execute_review(
            db,
            collab_agent,
            api_config,
            task,
            user,
            files,
            rules,
            profiles,
            experience_section,
            execution_token=active_token,
        )
    except Exception as e:
        logger.exception(e)
    finally:
        db.close()
        _REVIEW_SEMAPHORE.release()


def _get_agent_for_profile(profile_code: str) -> Optional[BaseAgent]:
    """根据审查画像 code 从 AgentRegistry 获取真实注册的 BaseAgent

    Args:
        profile_code: 审查画像 code(general/security/reliability/performance/maintainability)

    Returns:
        Optional[BaseAgent]: 注册中心中的真实 Agent;未找到返回 None
    """
    agent_name = _PROFILE_TO_AGENT_CODE.get(profile_code, "code_reviewer")
    return AgentRegistry.instance().get(agent_name)


def _finding_to_review_issue(task_id: int, code_file: CodeFile, finding: Finding) -> ReviewIssue:
    """将 Finding 转换为 ReviewIssue ORM 对象(填充所有 v2/v3 字段)

    Args:
        task_id: 审查任务 ID
        code_file: 代码文件 ORM 对象
        finding: 标准化漏洞发现

    Returns:
        ReviewIssue: 已填充所有字段的 ORM 对象(未加入 session)
    """
    cvss_score, cvss_vector, cvss_version, cvss_source = normalize_cvss(
        finding.cvss_score,
        finding.cvss_vector,
    )
    return ReviewIssue(
        task_id=task_id,
        file_id=code_file.id,
        file_name=code_file.file_name,
        line_number=finding.line_number,
        end_line=finding.end_line,
        issue_type=finding.issue_type,
        severity=finding.severity,
        title=finding.title or "",
        description=finding.description,
        suggestion=finding.suggestion,
        fixed_code=finding.fixed_code,
        status="unfixed",
        # v2 新增漏洞元数据字段
        owasp=finding.owasp,
        cwe=finding.cwe,
        evidence=finding.evidence,
        exploit_scenario=finding.exploit_scenario,
        references_json=finding.references if finding.references else None,
        confidence=finding.confidence,
        source=finding.source,
        # v3 新增 CVSS / 合规映射 / 修复方案 / 静态命中统计
        cvss_score=cvss_score,
        cvss_vector=cvss_vector,
        cvss_version=cvss_version,
        cvss_source=cvss_source,
        compliance_mapping=finding.compliance_mapping if finding.compliance_mapping else None,
        remediation=finding.remediation,
        static_rule_hits=finding.static_rule_hits,
        source_details=finding.source_details if finding.source_details else None,
        confirmation_count=finding.confirmation_count,
        finding_fingerprint=finding.finding_fingerprint or None,
    )


def _issue_to_review_issue(task_id: int, code_file: CodeFile, issue: Issue) -> ReviewIssue:
    """将合并后的 Issue 转换为 ReviewIssue ORM 对象(填充全量 v2/v3 字段)

    T08 双引擎合并后的 Issue 携带 source(static/llm/hybrid)和 static_rule_hits,
    此函数将其 1:1 映射到 ReviewIssue ORM,确保 v3 字段全部持久化。

    Args:
        task_id: 审查任务 ID
        code_file: 代码文件 ORM 对象
        issue: 合并去重后的问题对象(来自 issue_merger)

    Returns:
        ReviewIssue: 已填充全量 v2/v3 字段的 ORM 对象(未加入 session)
    """
    cvss_score, cvss_vector, cvss_version, cvss_source = normalize_cvss(
        issue.cvss_score,
        issue.cvss_vector,
    )
    return ReviewIssue(
        task_id=task_id,
        file_id=code_file.id,
        file_name=code_file.file_name,
        line_number=issue.line_number,
        end_line=issue.end_line,
        issue_type=issue.issue_type,
        severity=issue.severity,
        title=issue.title or "",
        description=issue.description,
        suggestion=issue.suggestion,
        fixed_code=issue.fixed_code,
        status="unfixed",
        # v2 漏洞元数据
        owasp=issue.owasp,
        cwe=issue.cwe,
        evidence=issue.evidence,
        exploit_scenario=issue.exploit_scenario,
        references_json=issue.references if issue.references else None,
        confidence=issue.confidence,
        source=issue.source,
        # v3 CVSS / 合规映射 / 修复方案 / 静态命中统计
        cvss_score=cvss_score,
        cvss_vector=cvss_vector,
        cvss_version=cvss_version,
        cvss_source=cvss_source,
        compliance_mapping=issue.compliance_mapping if issue.compliance_mapping else None,
        remediation=issue.remediation,
        static_rule_hits=issue.static_rule_hits,
        source_details=issue.source_details if issue.source_details else None,
        confirmation_count=issue.confirmation_count,
        finding_fingerprint=issue.finding_fingerprint or None,
    )


def _execute_review(
    db: Session, collab_agent: DeepSeekAgent, api_config, task: ReviewTask, user: User,
    files: list, rules: list, profiles: tuple[ReviewAgentProfile, ...],
    experience_section: str,
    execution_token: Optional[str] = None,
) -> None:
    """执行审查主循环并将统计结果落库。

    v2.2 双引擎:静态规则前置过滤 + LLM 深度审查(通过 BaseAgent.call() 调用真实 Agent)。
    逐文件审查、累计问题、刷新进度,最终汇总评分与摘要;
    捕获取消信号与异常,分别落库为 cancelled / failed 状态,不向上抛出。
    """
    t0 = time.time()
    review_type = task.review_type
    agent_codes = [_PROFILE_TO_AGENT_CODE.get(p.code, p.code) for p in profiles]
    _emit_review_event(AgentEventType.DISPATCH, task, user,
                       f"审查任务 #{task.id} 启动,类型={review_type},文件={len(files)},代理={len(profiles)}")
    for ac in set(agent_codes):
        _emit_review_event(AgentEventType.DISPATCH, task, user,
                           f"审查任务 #{task.id} 启动",
                           agent_code=ac)
    try:
        all_issues: list[ReviewIssue] = []
        for code_file in files:
            _check_cancelled(db, task, execution_token)
            file_issues = _review_one_file(db, collab_agent, api_config, task, code_file, rules, user, profiles,
                                           experience_section=experience_section,
                                           execution_token=execution_token)
            all_issues.extend(file_issues)
            _check_cancelled(db, task, execution_token, lock=True)
            task.processed_files += 1
            _safe_commit(db)
            db.refresh(task)
            for ac in set(agent_codes):
                _emit_review_event(AgentEventType.PROGRESS, task, user,
                                   f"文件 {task.processed_files}/{task.total_files}: {code_file.file_name} 审查完成,"
                                   f"累计 {len(all_issues)} 个问题",
                                   agent_code=ac)

        _check_cancelled(db, task, execution_token, lock=True)
        sev_count = {"严重": 0, "高": 0, "中": 0, "低": 0}
        for it in all_issues:
            # 归一化:LLM 偶发返回四类之外的 severity 时归入「中」,
            # 保证四级计数之和恒等于 total_issues,评分与统计口径自洽。
            key = it.severity if it.severity in sev_count else "中"
            sev_count[key] += 1
        task.total_issues = len(all_issues)
        task.severe_issues = sev_count["严重"]
        task.high_issues = sev_count["高"]
        task.medium_issues = sev_count["中"]
        task.low_issues = sev_count["低"]
        # 保留 compute_score 作为旧调用方/测试的兼容入口；生产实现与
        # breakdown 使用同一公式，因此两者正常情况下始终一致。
        score = int(compute_score(sev_count))
        score_breakdown = compute_score_breakdown(sev_count)
        if score != score_breakdown["score"]:
            # 外部兼容调用方可能暂时覆盖 compute_score；仍保持接口返回的
            # 最终分数与风险等级自洽，而不丢失扣分明细。
            score_breakdown = {
                **score_breakdown,
                "score": score,
                "risk_level": score_risk_level(score),
            }
        task.score = score
        task.score_version = SCORING_VERSION
        task.score_breakdown = score_breakdown
        task.summary = _build_summary(profiles, len(files), len(all_issues), task.score)
        task.status = "success"
        task.end_time = datetime.now(timezone.utc)
        task.duration_ms = int((time.time() - t0) * 1000)
        _safe_commit(db)
        _emit_review_event(AgentEventType.COMPLETE, task, user,
                           f"审查任务 #{task.id} 完成,评分={task.score},问题={len(all_issues)},"
                           f"耗时={task.duration_ms}ms")
        for ac in set(agent_codes):
            _emit_review_event(AgentEventType.COMPLETE, task, user,
                               f"审查任务 #{task.id} 完成",
                               agent_code=ac)
    except TaskCancelledError:
        task.status = "cancelled"
        task.end_time = datetime.now(timezone.utc)
        task.duration_ms = int((time.time() - t0) * 1000)
        _safe_commit(db)
        logger.info(f"审查任务 #{task.id} 已被用户取消")
        for ac in set(agent_codes):
            _emit_review_event(AgentEventType.FAILED, task, user,
                               f"审查任务 #{task.id} 已取消", agent_code=ac)
    except TaskSupersededError:
        if hasattr(db, "rollback"):
            db.rollback()
        logger.info("[review] 审查任务 #{} 的旧执行者已停止写入", task.id)
    except Exception as e:
        logger.exception(e)
        if hasattr(db, "rollback"):
            db.rollback()
        try:
            _check_cancelled(db, task, execution_token, lock=True)
        except (TaskCancelledError, TaskSupersededError):
            if hasattr(db, "rollback"):
                db.rollback()
            logger.info("[review] 审查任务 #{} 已取消或租约被接管，不覆盖新状态", task.id)
            return
        task.status = "failed"
        task.error_message = str(e)[:500]
        task.end_time = datetime.now(timezone.utc)
        task.duration_ms = int((time.time() - t0) * 1000)
        _safe_commit(db)
        _emit_review_event(AgentEventType.FAILED, task, user,
                           f"审查任务 #{task.id} 失败: {task.error_message}")
        for ac in set(agent_codes):
            _emit_review_event(AgentEventType.FAILED, task, user,
                               f"审查任务 #{task.id} 失败", agent_code=ac)


def _review_one_file(db: Session, collab_agent: DeepSeekAgent, api_config,
                     task: ReviewTask, code_file: CodeFile, rules: list, user: User,
                     profiles: tuple[ReviewAgentProfile, ...],
                     experience_section: str = "",
                     execution_token: Optional[str] = None) -> list[ReviewIssue]:
    """审查单个文件:双引擎 — 静态规则前置过滤 + LLM 深度审查 → 合并去重 → 入库

    T08 v3 双引擎流程:
      引擎1(静态规则前置过滤):对整个文件应用 scan_secrets + apply_static_rules
            → 生成 List[Finding](确定性命中,无 LLM 调用)
      引擎2(LLM 深度审查):
        - 单代理模式(quick/standard):分片 → 通过 BaseAgent.call() 调用真实 Agent
        - 多代理模式(security/performance/full):分片 → 三阶段协同流水线
            → 生成 List[Finding]
      合并去重:静态 Findings + LLM Findings(转 Issue)→ issue_merger 合并去重
            → List[Issue](source=static/llm/hybrid, static_rule_hits 已填充)
      持久化:用 _issue_to_review_issue() 将合并后 Issue 写入 ReviewIssue(全量 v3 字段)

    Args:
        db: 数据库会话
        collab_agent: 协同审查用的 DeepSeekAgent(三阶段流水线需要 system/user prompt 灵活传入)
        api_config: 用户解析后的 API 配置(用户自定义 > 管理员全局 > 系统默认)
        task: 当前审查任务
        code_file: 待审查代码文件 ORM 对象
        rules: 启用规则列表
        user: 当前用户
        profiles: 审查代理画像组合
        experience_section: 历史经验段落(自进化注入)

    Returns:
        list[ReviewIssue]: 该文件产生的问题列表(已入库)
    """
    # binary 文件直接跳过(LLM 无法审查二进制内容)
    if getattr(code_file, "is_binary", 0) == 1:
        logger.info(f"[review] 文件 {code_file.file_name} 为二进制,跳过审查")
        return []

    # ===== 引擎1:静态规则前置过滤(确定性命中,无 LLM 调用)=====
    static_findings: List[Finding] = []
    try:
        static_findings = static_scan_file(code_file)
        if static_findings:
            _emit_review_event(
                AgentEventType.PROGRESS, task, user,
                f"静态规则引擎:文件 {code_file.file_name} 命中 {len(static_findings)} 条确定性问题",
                agent_code="static_analyzer",
            )
    except Exception as e:
        logger.warning(f"[review] 静态规则引擎执行失败,跳过: {e}")

    # ===== 引擎2:LLM 深度审查(分片 + 多代理协同)=====
    llm_findings: List[Finding] = []
    chunks = chunk_code(code_file.content, code_file.language,
                        threshold=settings.deepseek_chunk_threshold)
    # 自定义 Agent 也必须进入协同执行器；即使内置画像被治理停用，单个自定义
    # Agent 仍应获得一次独立调用，而不是被静态 Agent 注册表路径吞掉。
    use_collab = len(profiles) >= 2 or any(profile.is_custom for profile in profiles)

    for idx, chunk in enumerate(chunks):
        if use_collab:
            chunk_findings = _review_chunk_collaborative(
                db, collab_agent, api_config, task, code_file, rules, user,
                profiles, idx, chunk, experience_section=experience_section,
            )
        else:
            chunk_findings = _review_chunk_sequential(
                db, api_config, task, code_file, rules, user,
                profiles, idx, chunk, experience_section=experience_section,
            )
        llm_findings.extend(chunk_findings)

    # ===== 双引擎合并去重(T08 核心)=====
    # 将 LLM Findings 转换为 Issues(source="llm"),与静态 Findings 合并
    llm_issues: List[Issue] = [finding_to_issue(f) for f in llm_findings]
    merged_issues: List[Issue] = merge_findings_and_issues(
        static_findings,
        llm_issues,
        code_file.id,
        code=code_file.content or "",
        language=code_file.language,
    )

    # ===== v3 字段持久化 =====
    issues_acc: list[ReviewIssue] = [
        _issue_to_review_issue(task.id, code_file, issue) for issue in merged_issues
    ]
    if issues_acc:
        _check_cancelled(db, task, execution_token, lock=True)
        db.add_all(issues_acc)
        db.commit()
    return issues_acc


def _finding_fingerprint(file_id: int, finding: Finding) -> tuple:
    """生成 Finding 的去重指纹(用于跨引擎去重)

    Args:
        file_id: 文件 ID
        finding: 标准化漏洞发现

    Returns:
        tuple: 可放入 set 的稳定去重键
    """
    return (
        file_id,
        finding.line_number,
        finding.end_line or 0,
        finding.issue_type,
        (finding.title or "").strip()[:80],
        (finding.description or "").strip()[:120],
    )


# ═══════════════ 单代理模式 (quick/standard) ═══════════════

def _review_chunk_sequential(
    db: Session, api_config, task: ReviewTask,
    code_file: CodeFile, rules: list, user: User,
    profiles: tuple[ReviewAgentProfile, ...], chunk_idx: int,
    chunk, experience_section: str = "",
) -> List[Finding]:
    """单代理串行审查(双引擎之引擎2:LLM 深度审查)

    v2.2 改造:
    - 通过 AgentRegistry 获取真实 Agent(code_reviewer / security_sentinel)
    - 通过 BaseAgent.call() 调用 LLM(替代 DeepSeekAgent.chat()),统一事件总线/AiCallLog 归因
    - 返回 List[Finding](替代 list[dict]),由 _review_one_file 统一入库

    Args:
        db: 数据库会话(用于失败时日志归因,不直接写 AiCallLog)
        api_config: 用户解析后的 API 配置
        task: 当前审查任务
        code_file: 待审查代码文件
        rules: 启用规则列表
        user: 当前用户
        profiles: 审查代理画像组合(单代理模式下只取第一个)
        chunk_idx: 分片索引
        chunk: 代码分片对象
        experience_section: 历史经验段落

    Returns:
        List[Finding]: 标准化漏洞发现列表(可能为空)
    """
    findings: List[Finding] = []

    for agent_idx, profile in enumerate(profiles):
        # 从注册中心获取真实 Agent
        agent = _get_agent_for_profile(profile.code)
        if agent is None:
            logger.warning(f"[review] 未找到 profile={profile.code} 对应的 Agent,跳过")
            continue

        target_agent = _PROFILE_TO_AGENT_CODE.get(profile.code, profile.code)
        ctx = AgentContext(
            user_id=user.id,
            task_id=task.id,
            project_id=task.project_id,
            file_id=code_file.id,
            extra={"trace_id": f"review_{task.id}_f{code_file.id}_c{chunk_idx}_{profile.code}"},
        )

        _emit_review_event(
            AgentEventType.THINKING, task, user,
            f"[{profile.name}] 正在审查 {code_file.file_name}",
            agent_code=target_agent,
        )

        try:
            # 通过真实 Agent 调用 LLM(安全画像用 SecuritySentinelAgent,其他用 CodeReviewerAgent)
            if profile.code == "security" and hasattr(agent, "scan_file_for_review"):
                result = agent.scan_file_for_review(
                    code=chunk.text,
                    language=code_file.language or "plaintext",
                    file_name=code_file.file_name,
                    line_offset=chunk.start_line,
                    experience_section=experience_section,
                    api_config=api_config,
                    ctx=ctx,
                )
            elif hasattr(agent, "execute_review"):
                result = agent.execute_review(
                    code=chunk.text,
                    rules=rules,
                    language=code_file.language or "plaintext",
                    file_name=code_file.file_name,
                    line_offset=chunk.start_line,
                    experience_section=experience_section,
                    agent_section=format_agent_section(profile),
                    api_config=api_config,
                    ctx=ctx,
                )
            else:
                logger.warning(f"[review] Agent {target_agent} 不支持 execute_review/scan_file_for_review,跳过")
                continue

            if not result.success:
                logger.warning(f"[review] {profile.code} 审查失败: {result.error}")
                _emit_review_event(
                    AgentEventType.FAILED, task, user,
                    f"[{profile.name}] 审查失败: {result.error}",
                    agent_code=target_agent,
                )
                # 补写 AiCallLog(失败):BaseAgent.call() 不写日志,这里补写实现 Agent 归因
                _log_sequential_call(
                    db, task, user, code_file, chunk_idx, agent_idx,
                    target_agent, result, status="failed",
                    error=(result.error or "")[:500],
                    agent=agent,
                )
                continue

            chunk_findings: List[Finding] = []
            if isinstance(result.data, dict):
                chunk_findings = result.data.get("issues", []) or []
            findings.extend(chunk_findings)

            # 补写 AiCallLog(成功):BaseAgent.call() 不写日志,这里补写实现 Agent 归因
            _log_sequential_call(
                db, task, user, code_file, chunk_idx, agent_idx,
                target_agent, result, status="success",
                agent=agent,
            )

            _emit_review_event(
                AgentEventType.COMPLETE, task, user,
                f"[{profile.name}] 审查 {code_file.file_name} 完成,"
                f"发现 {len(chunk_findings)} 个问题",
                agent_code=target_agent,
            )
        except Exception as e:
            logger.warning(f"文件 {code_file.file_name} chunk {chunk_idx} {profile.code} 失败: {e}")
            _emit_review_event(
                AgentEventType.FAILED, task, user,
                f"[{profile.name}] 审查异常: {e}",
                agent_code=target_agent,
            )
            # 异常时也补写一条失败日志(若有 result)
            try:
                _log_sequential_call(
                    db, task, user, code_file, chunk_idx, agent_idx,
                    target_agent, None, status="failed",
                    error=str(e)[:500],
                    agent=agent,
                )
            except Exception as log_err:
                logger.debug(f"[review] 补写异常日志失败: {log_err}")
            continue

    return findings


def _log_sequential_call(
    db: Session,
    task: ReviewTask,
    user: User,
    code_file: CodeFile,
    chunk_idx: int,
    agent_idx: int,
    agent_label: str,
    result: Optional["object"],
    status: str = "success",
    error: Optional[str] = None,
    agent: Optional[BaseAgent] = None,
) -> None:
    """补写顺序模式(BaseAgent.call 路径)的 AiCallLog

    T08 v3 改造:
    - 优先使用 agent._log_call() 写入 AiCallLog,agent_label 自动填充为 agent.name(AC6)
    - agent 为 None 时降级到 DeepSeekAgent.log_deferred()(兼容旧路径)

    Args:
        db: 数据库会话
        task: 当前审查任务
        user: 当前用户
        code_file: 待审查代码文件
        chunk_idx: 分片索引
        agent_idx: Agent 索引(同一分片内多 Agent 时的序号)
        agent_label: Agent 标识码(真实 Agent name,降级路径用)
        result: AgentResult 对象(异常时可为 None)
        status: 日志状态(success/failed)
        error: 错误信息(失败时)
        agent: 真实 BaseAgent 对象(优先路径,为 None 时降级)

    Returns:
        None
    """
    # 优先路径:通过 BaseAgent._log_call() 写入,agent_label 自动填充为 self.name
    if agent is not None and hasattr(agent, "_log_call"):
        try:
            agent._log_call(
                db,
                task_id=task.id,
                user_id=user.id,
                file_id=code_file.id,
                chunk_index=chunk_idx * 100 + agent_idx,
                result=result,
                status=status,
                error=error,
            )
            return
        except Exception as e:
            logger.debug(f"[review] agent._log_call 失败,降级到 log_deferred: {e}")

    # 降级路径:DeepSeekAgent.log_deferred()(兼容 agent 未传入的场景)
    tokens_dict = {}
    if result is not None and getattr(result, "tokens", None):
        tokens_dict = result.tokens if isinstance(result.tokens, dict) else {}

    meta = {
        "agent_label": agent_label,
        "model_name": (getattr(result, "model", None) if result else None) or "",
        "model_tag": (getattr(result, "model", None) if result else None) or "",
        "user_prompt": "",  # BaseAgent.call 路径不暴露 prompt,留空
        "response": "" if status != "success" else "",
        "prompt_tokens": tokens_dict.get("prompt", 0) if tokens_dict else 0,
        "completion_tokens": tokens_dict.get("completion", 0) if tokens_dict else 0,
        "total_tokens": tokens_dict.get("total", 0) if tokens_dict else 0,
        "duration_ms": (getattr(result, "duration_ms", None) if result else None) or 0,
        "create_time": datetime.now(timezone.utc),
    }

    try:
        DeepSeekAgent.log_deferred(
            db,
            task_id=task.id,
            user_id=user.id,
            file_id=code_file.id,
            chunk_index=chunk_idx * 100 + agent_idx,
            meta=meta,
            status=status,
            error=error,
        )
    except Exception as e:
        logger.debug(f"[review] 补写 AiCallLog 失败(agent={agent_label}): {e}")


# ═══════════════ 多 Agent 协同三阶段 ═══════════════

def _review_chunk_collaborative(
    db: Session, shared_agent: DeepSeekAgent, api_config, task: ReviewTask,
    code_file: CodeFile, rules: list, user: User,
    profiles: tuple[ReviewAgentProfile, ...], chunk_idx: int,
    chunk, experience_section: str = "",
) -> List[Finding]:
    """多 Agent 协同审查 — 三阶段流水线

    阶段1: 并行感知 — ThreadPoolExecutor 并行调用各 Agent(v2.2: agent_label 使用真实 Agent name)
    阶段2: 交叉复审 — 首席架构师对比合并发现
    阶段3: 共识统合 — 仲裁官输出最终问题清单

    v2.2 改造:
    - agent_label 从 profile.code 改为真实 Agent name(_PROFILE_TO_AGENT_CODE 映射)
    - 最终返回 List[Finding](替代 list[dict]),由 _review_one_file 统一入库
    - 阶段1仍然使用 DeepSeekAgent.call_raw()(线程内独立 Agent 实例,避免单例竞态)

    Args:
        db: 数据库会话
        shared_agent: 共享 DeepSeekAgent(阶段2/3 用,主线程同步调用)
        api_config: 用户解析后的 API 配置
        task: 当前审查任务
        code_file: 待审查代码文件
        rules: 启用规则列表
        user: 当前用户
        profiles: 审查代理画像组合(多代理模式)
        chunk_idx: 分片索引
        chunk: 代码分片对象
        experience_section: 历史经验段落

    Returns:
        List[Finding]: 标准化漏洞发现列表(可能为空)
    """
    t0 = time.time()
    file_name = code_file.file_name
    language = code_file.language or "plaintext"
    line_offset = chunk.start_line

    for profile in profiles:
        target = _PROFILE_TO_AGENT_CODE.get(profile.code, profile.code)
        _emit_review_event(AgentEventType.DISPATCH, task, user,
                           f"[{profile.name}] 并行感知阶段启动",
                           agent_code=target)

    # ===== 阶段1: 并行感知 =====
    agent_findings: dict[str, list[dict]] = {}
    agent_names: dict[str, str] = {}
    deferred_logs: list[dict] = []

    with concurrent.futures.ThreadPoolExecutor(
        max_workers=min(_COLLAB_PARALLEL_THREADS, len(profiles)),
    ) as pool:
        future_map: dict[concurrent.futures.Future, ReviewAgentProfile] = {}
        for profile in profiles:
            future = pool.submit(
                _call_single_agent,
                profile, chunk.text, language, file_name, rules, line_offset,
                experience_section,
                api_config,
            )
            future_map[future] = profile

        for future, profile in future_map.items():
            # v2.2: agent_label 使用真实 Agent name
            agent_label = _PROFILE_TO_AGENT_CODE.get(profile.code, profile.code)
            agent_names[profile.code] = profile.name
            target = agent_label
            agent_meta = None
            try:
                raw, agent_meta = future.result(timeout=180)
                parsed = parse(raw)
                agent_findings[profile.code] = [
                    {
                        "title": it.title,
                        "issue_type": it.issue_type,
                        "severity": it.severity,
                        "line_start": _absolute_line(it.line_number, line_offset),
                        "line_end": (
                            _absolute_line(it.end_line, line_offset)
                            if it.end_line else None
                        ),
                        "description": it.description or "",
                        "suggestion": it.suggestion or "",
                        # v2.2: 保留新字段供阶段2/3 传递
                        "owasp": getattr(it, "owasp", "") or "",
                        "cwe": getattr(it, "cwe", "") or "",
                        "evidence": getattr(it, "evidence", "") or "",
                        "exploit_scenario": getattr(it, "exploit_scenario", "") or "",
                        "references": list(getattr(it, "references", []) or []),
                        "confidence": float(getattr(it, "confidence", 0.8) or 0.8),
                        "cvss_score": getattr(it, "cvss_score", None),
                        "cvss_vector": getattr(it, "cvss_vector", None),
                        "remediation": getattr(it, "remediation", "") or "",
                        "source": f"llm:{profile.code}",
                    }
                    for it in parsed.issues
                ]
                _emit_review_event(AgentEventType.PROGRESS, task, user,
                                   f"[{profile.name}] 发现 {len(parsed.issues)} 个问题",
                                   agent_code=target)
                if agent_meta:
                    deferred_logs.append({
                        "meta": agent_meta,
                        "status": "success",
                        "chunk_index": chunk_idx * 100 + list(profiles).index(profile),
                    })
            except Exception as e:
                logger.warning(f"并行审查 {profile.code}({agent_label}) 失败: {e}")
                agent_findings[profile.code] = []
                _emit_review_event(AgentEventType.FAILED, task, user,
                                   f"[{profile.name}] 审查失败: {e}",
                                   agent_code=target)
                if agent_meta:
                    deferred_logs.append({
                        "meta": agent_meta,
                        "status": "failed",
                        "error": str(e)[:500],
                        "chunk_index": chunk_idx * 100 + list(profiles).index(profile),
                    })

    # 补写并行调用日志到 AiCallLog
    for log_info in deferred_logs:
        try:
            DeepSeekAgent.log_deferred(
                db,
                task_id=task.id,
                user_id=user.id,
                file_id=code_file.id,
                chunk_index=log_info["chunk_index"],
                meta=log_info["meta"],
                status=log_info.get("status", "success"),
                error=log_info.get("error"),
            )
        except Exception as e:
            logger.debug(f"补写 AiCallLog 失败: {e}")

    total_raw = sum(len(v) for v in agent_findings.values())
    logger.info(
        f"[collab] 阶段1完成 {file_name} chunk={chunk_idx} "
        f"agents={len(profiles)} raw_findings={total_raw} "
        f"elapsed={int((time.time()-t0)*1000)}ms",
    )

    # 零发现快速路径
    if total_raw == 0:
        for profile in profiles:
            target = _PROFILE_TO_AGENT_CODE.get(profile.code, profile.code)
            _emit_review_event(AgentEventType.COMPLETE, task, user,
                               f"[{profile.name}] 未发现新问题",
                               agent_code=target)
        return []

    # ===== 阶段2: 交叉复审 =====
    _emit_review_event(AgentEventType.THINKING, task, user,
                       "首席架构师 Agent 开始交叉复审各代理发现",
                       agent_code="orchestrator")

    findings_text = _build_findings_text(agent_findings, agent_names)
    cross_prompt = COLLAB_CROSS_REVIEW_USER.format(
        language=language,
        file_name=file_name,
        line_offset=line_offset,
        agent_findings=findings_text,
    )

    try:
        cross_raw, _ = shared_agent.chat(
            system_prompt=COLLAB_CROSS_REVIEW_SYSTEM,
            user_prompt=cross_prompt,
            task_id=task.id,
            user_id=user.id,
            file_id=code_file.id,
            chunk_index=chunk_idx * 100 + 90,
            db=db,
            agent_label="cross_review",
        )
        cross_data = json_lib.loads(cross_raw)
        cross_review = cross_data.get("cross_review", [])
        cross_summary = cross_data.get("summary", {})
        logger.info(
            f"[collab] 阶段2完成 confirmed={cross_summary.get('confirmed',0)} "
            f"escalated={cross_summary.get('escalated',0)} "
            f"merged={cross_summary.get('merged',0)} "
            f"disputed={cross_summary.get('disputed',0)}",
        )
    except Exception as e:
        logger.warning(f"交叉复审失败,降级为直接合并: {e}")
        cross_review = _fallback_cross_review(agent_findings, agent_names)
        cross_summary = {"confirmed": len(cross_review), "disputed": 0,
                         "escalated": 0, "merged": 0}

    # ===== 阶段3: 共识统合 =====
    _emit_review_event(AgentEventType.THINKING, task, user,
                       "仲裁官 Agent 开始共识统合",
                       agent_code="orchestrator")

    consensus_prompt = COLLAB_CONSENSUS_USER.format(
        cross_review_json=json_lib.dumps(
            {"cross_review": cross_review, "summary": cross_summary},
            ensure_ascii=False, indent=2,
        ),
    )

    try:
        cons_raw, _ = shared_agent.chat(
            system_prompt=COLLAB_CONSENSUS_SYSTEM,
            user_prompt=consensus_prompt,
            task_id=task.id,
            user_id=user.id,
            file_id=code_file.id,
            chunk_index=chunk_idx * 100 + 95,
            db=db,
            agent_label="consensus",
        )
        cons_data = json_lib.loads(cons_raw)
        final_issues = cons_data.get("issues", [])
        discarded = cons_data.get("discarded", [])
        logger.info(
            f"[collab] 阶段3完成 final={len(final_issues)} "
            f"discarded={len(discarded)}",
        )
    except Exception as e:
        logger.warning(f"共识统合失败,降级为直接使用交叉复审结果: {e}")
        final_issues = _fallback_consensus(cross_review)

    # 事件广播: 通知各 Agent 协同完成
    for profile in profiles:
        target = _PROFILE_TO_AGENT_CODE.get(profile.code, profile.code)
        _emit_review_event(AgentEventType.COMPLETE, task, user,
                           f"[{profile.name}] 协同审查完成,"
                           f"最终确认 {len(final_issues)} 个问题",
                           agent_code=target)

    elapsed = int((time.time() - t0) * 1000)
    logger.info(
        f"[collab] {file_name} chunk={chunk_idx} 三阶段完成 "
        f"raw={total_raw} → cross={len(cross_review)} → final={len(final_issues)} "
        f"elapsed={elapsed}ms",
    )
    # v2.2: 将最终问题列表转换为 List[Finding](统一数据结构,便于入库)
    return [_final_issue_to_finding(it) for it in final_issues]


def _final_issue_to_finding(item: dict) -> Finding:
    """将多代理协同最终问题(dict)转换为 Finding(用于统一入库)

    Args:
        item: 阶段3 共识统合返回的问题 dict,字段对齐 COLLAB_CONSENSUS_USER 输出 schema

    Returns:
        Finding: 标准化漏洞发现
    """
    cvss_score, cvss_vector, cvss_version, cvss_source = normalize_cvss(
        item.get("cvss_score"),
        item.get("cvss_vector"),
    )
    try:
        confidence = max(0.0, min(1.0, float(item.get("confidence", 0.8))))
    except (TypeError, ValueError):
        confidence = 0.8

    source_details = _collaborative_source_details(item, confidence)
    compliance_mapping = item.get("compliance_mapping")
    if not isinstance(compliance_mapping, dict):
        compliance_mapping = {}

    return Finding(
        line_number=int(item.get("line_number", 0) or 0),
        end_line=int(item["end_line"]) if item.get("end_line") else None,
        issue_type=item.get("issue_type", "") or "",
        severity=normalize_severity(item.get("severity")),
        title=item.get("title", "") or "",
        description=item.get("description", "") or "",
        suggestion=item.get("suggestion", "") or "",
        fixed_code=item.get("fixed_code", "") or "",
        owasp=item.get("owasp", "") or "",
        cwe=item.get("cwe", "") or "",
        evidence=item.get("evidence", "") or "",
        exploit_scenario=item.get("exploit_scenario", "") or "",
        references=item.get("references", []) if isinstance(item.get("references"), list) else [],
        confidence=confidence,
        source="llm_collab",
        cvss_score=cvss_score,
        cvss_vector=cvss_vector,
        cvss_version=cvss_version,
        cvss_source=cvss_source,
        compliance_mapping=compliance_mapping,
        remediation=item.get("remediation", "") or "",
        static_rule_hits=0,
        source_details=source_details,
        confirmation_count=max(
            len(source_details),
            _safe_positive_int(item.get("confirmation_count")),
            _safe_positive_int(item.get("cross_agent_count")),
            1,
        ),
    )


def _collaborative_source_details(item: dict, confidence: float) -> list[dict]:
    target_count = max(
        _safe_positive_int(item.get("confirmation_count")),
        _safe_positive_int(item.get("cross_agent_count")),
        1,
    )
    raw_details = item.get("source_details")
    if isinstance(raw_details, list):
        details = [dict(detail) for detail in raw_details if isinstance(detail, dict)]
    else:
        details = []

    names = item.get("cross_agent_names")
    if isinstance(names, list):
        for name in names:
            if len(details) >= target_count:
                break
            details.append(_collaborative_source_detail(item, confidence, name))

    while len(details) < target_count:
        ordinal = len(details) + 1
        source = item.get("source") or "llm_collab"
        if ordinal > 1:
            source = f"{source}:{ordinal}"
        details.append(_collaborative_source_detail(item, confidence, source))

    return details


def _collaborative_source_detail(
    item: dict,
    confidence: float,
    source: object,
) -> dict:
    return {
        "source": str(source or "llm_collab")[:80],
        "confidence": confidence,
        "evidence": str(item.get("evidence") or "")[:2000],
        "line_number": int(item.get("line_number", 0) or 0),
        "title": str(item.get("title") or "")[:200],
    }


def _safe_positive_int(value: object) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


# ═══════════════ 协同辅助函数 ═══════════════

def _call_single_agent(
    profile: ReviewAgentProfile,
    code: str,
    language: str,
    file_name: str,
    rules: list,
    line_offset: int,
    experience_section: str = "",
    api_config = None,
) -> tuple:
    """单次代理并行调用 — 通过 DeepSeekAgent.call_raw() 统一入口

    每个线程独立创建 DeepSeekAgent,通过统一的 call_raw() 调用 DeepSeek API。
    返回 (raw_response_text, meta_dict), meta 供主线程事后 log_deferred() 补写 AiCallLog。

    v2.2 改造:
    - agent_label 从 profile.code 改为真实 Agent name(_PROFILE_TO_AGENT_CODE 映射)
    - api_config 注入用户自定义 API 配置

    Args:
        profile: 审查代理画像
        code: 代码内容(单分片)
        language: 编程语言标识
        file_name: 文件名(含扩展名)
        rules: 启用规则列表
        line_offset: 行号偏移(分片时使用)
        experience_section: 历史经验段落
        api_config: 用户解析后的 API 配置

    Returns:
        tuple: (raw_response_text, meta_dict)
    """
    agent = DeepSeekAgent(api_config=api_config)
    system_prompt, user_prompt = build_prompt(
        language=language,
        file_name=file_name,
        code=code,
        rules=rules,
        line_offset=line_offset,
        agent_section=format_agent_section(profile),
        experience_section=experience_section,
    )
    if profile.is_custom and profile.system_prompt:
        system_prompt = (
            f"{profile.system_prompt.strip()}\n\n"
            "平台强制契约：只审查用户提供的代码，严格输出现有 Issue JSON 结构；"
            "不得执行命令、访问网络、写文件或修改数据。\n\n"
            f"{system_prompt}"
        )
    # v2.2: agent_label 使用真实 Agent name,便于 AiCallLog 归因到具体 Agent
    agent_label = _PROFILE_TO_AGENT_CODE.get(profile.code, profile.code)
    return agent.call_raw(
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        agent_label=agent_label,
        temperature=profile.temperature,
        max_tokens=profile.max_tokens,
    )


def _build_findings_text(
    findings: dict[str, list[dict]],
    names: dict[str, str],
) -> str:
    """把各代理发现格式化为 LLM 可读文本"""
    parts: list[str] = []
    for code, items in findings.items():
        name = names.get(code, code)
        parts.append(f"### {name} ({code}) — 发现 {len(items)} 条")
        if not items:
            parts.append("  (未发现严重问题)")
            continue
        for i, it in enumerate(items, 1):
            lines = (
                f"L{it.get('line_start',0)}-L{it.get('line_end',0)}"
                if it.get("line_end") and it["line_end"] != it.get("line_start")
                else f"L{it.get('line_start',0)}"
            )
            parts.append(
                f"  [{i}] [{it.get('severity','中')}] {it.get('title','')}\n"
                f"      位置={lines} 类型={it.get('issue_type','')}\n"
                f"      描述={it.get('description','')[:120]}\n"
                f"      证据={it.get('evidence','')[:200]}\n"
                f"      来源={it.get('source', f'llm:{code}')} "
                f"置信度={it.get('confidence', 0.8)} "
                f"CVSS={it.get('cvss_score')} {it.get('cvss_vector') or ''}",
            )
        parts.append("")
    return "\n".join(parts)


def _fallback_cross_review(
    findings: dict[str, list[dict]],
    names: dict[str, str],
) -> list[dict]:
    """降级方案: 交叉复审 LLM 失败时,直接按规则合并"""
    results: list[dict] = []
    for code, items in findings.items():
        name = names.get(code, code)
        for it in items:
            title = it.get("title", "")
            results.append({
                "verdict": "confirmed",
                "original_titles": [title],
                "final_title": title,
                "category": it.get("issue_type", ""),
                "explanation": f"来自{name}的独立发现(交叉复审降级)",
                "severity": it.get("severity", "中"),
                "severity_reason": "",
                "line_start": it.get("line_start", 0),
                "line_end": it.get("line_end"),
                "confidence": it.get("confidence", 0.8),
                "owasp": it.get("owasp", ""),
                "cwe": it.get("cwe", ""),
                "evidence": it.get("evidence", ""),
                "exploit_scenario": it.get("exploit_scenario", ""),
                "references": it.get("references", []),
                "cvss_score": it.get("cvss_score"),
                "cvss_vector": it.get("cvss_vector"),
                "remediation": it.get("remediation", ""),
                "source_details": [{
                    "source": it.get("source", f"llm:{code}"),
                    "confidence": it.get("confidence", 0.8),
                    "evidence": it.get("evidence", ""),
                    "line_number": it.get("line_start", 0),
                    "title": title,
                }],
            })
    return results


def _fallback_consensus(cross_review: list[dict]) -> list[dict]:
    """降级方案: 共识统合 LLM 失败时,直接使用交叉复审结果"""
    return [
        {
            "issue_type": cr.get("category", ""),
            "severity": cr.get("severity", "中"),
            "title": cr.get("final_title", ""),
            "line_number": cr.get("line_start", 0),
            "end_line": cr.get("line_end"),
            "description": cr.get("explanation", ""),
            "suggestion": "",
            "fixed_code": "",
            "confidence": cr.get("confidence", 0.8),
            "cross_agent_count": len(cr.get("original_titles", [])),
            "cross_agent_names": [],
            "owasp": cr.get("owasp", ""),
            "cwe": cr.get("cwe", ""),
            "evidence": cr.get("evidence", ""),
            "exploit_scenario": cr.get("exploit_scenario", ""),
            "references": cr.get("references", []),
            "cvss_score": cr.get("cvss_score"),
            "cvss_vector": cr.get("cvss_vector"),
            "remediation": cr.get("remediation", ""),
            "source_details": cr.get("source_details", []),
        }
        for cr in cross_review
    ]


def _absolute_line(line_number: Optional[int], chunk_start_line: int) -> int:
    """将分片内相对行号转换为原文件绝对行号

    Args:
        line_number: LLM 返回的分片内行号,0 或 None 表示文件级问题。
        chunk_start_line: 代码分片在原文件中的 0-based 起始行。

    Returns:
        int: 原文件内 1-based 行号;文件级问题保持为 0。
    """
    if not line_number:
        return 0
    return int(line_number) + chunk_start_line


def _build_summary(profiles: tuple[ReviewAgentProfile, ...], file_count: int,
                   issue_count: int, score: int) -> str:
    """生成审查任务总体摘要

    Args:
        profiles: 本次审查使用的代理画像组合。
        file_count: 本次审查文件数量。
        issue_count: 本次发现的问题数量。
        score: 综合评分。

    Returns:
        str: 可展示在任务详情和报告中的中文摘要。
    """
    agent_summary = build_agent_summary(profiles)
    return f"本次采用{agent_summary}完成 {file_count} 个文件审查,发现 {issue_count} 个问题,综合评分 {score}。"


def list_tasks(db: Session, user: User, project_id: int = None, status: str = "",
               start_date: str = "", end_date: str = "", page: int = 1, page_size: int = 20) -> dict:
    """查询审查任务列表(基于 project_member 关系)

    可见范围:
        - admin: 全部任务
        - 非 admin: 可见项目(owner ∪ member)下的任务

    Args:
        db: 数据库会话
        user: 当前用户
        project_id: 项目ID过滤
        status: 状态过滤
        start_date: 开始日期
        end_date: 结束日期
        page: 页码
        page_size: 每页数量

    Returns:
        dict: 分页响应
    """
    from app.services.project_member_service import get_visible_project_ids
    visible_ids, _ = get_visible_project_ids(db, user)
    q = db.query(ReviewTask).filter(
        ReviewTask.status != "deleted",
        ReviewTask.project_id.in_(visible_ids),
    )
    if project_id:
        q = q.filter(ReviewTask.project_id == project_id)
    if status:
        q = q.filter(ReviewTask.status == status)
    if start_date:
        q = q.filter(ReviewTask.create_time >= start_date)
    if end_date:
        q = q.filter(ReviewTask.create_time <= end_date + " 23:59:59")

    total = q.count()
    pagination = Pagination(page, page_size, total)
    rows = q.order_by(ReviewTask.create_time.desc()).offset(pagination.offset).limit(pagination.page_size).all()

    # 批量取项目名,避免逐行 db.get(Project) 造成 N+1 查询
    project_ids = {row.project_id for row in rows}
    projects = {
        p.id: p
        for p in db.query(Project).filter(Project.id.in_(project_ids)).all()
    } if project_ids else {}

    items = []
    for row in rows:
        project = projects.get(row.project_id)
        items.append({
            "id": row.id, "task_name": row.task_name,
            "project_id": row.project_id,
            "project_name": project.project_name if project else "",
            "review_type": row.review_type, "status": row.status,
            "total_files": row.total_files, "total_issues": row.total_issues,
            "severe_issues": row.severe_issues, "high_issues": row.high_issues,
            "medium_issues": row.medium_issues, "low_issues": row.low_issues,
            "score": row.score, "duration_ms": row.duration_ms,
            "create_time": row.create_time,
        })
    return pagination.to_dict(items)


def get_task_detail(db: Session, user: User, task_id: int) -> dict:
    """获取审查任务详情

    可见性:基于 project_member 关系,reviewer 可见同项目任务,
    非成员访问返回 404(防枚举)。

    Args:
        db: 数据库会话
        user: 当前用户
        task_id: 任务ID

    Returns:
        dict: 任务详情含关联项目

    Raises:
        NotFoundError: 任务不存在或无访问权限
    """
    from app.services.project_member_service import require_project_access
    task = db.get(ReviewTask, task_id)
    if not task or task.status == "deleted":
        raise NotFoundError("审查任务不存在", code=40400)
    # v2.4: 用 project_member 关系校验,reviewer 可读同项目任务
    require_project_access(db, task.project_id, user, need_write=False)
    project = db.get(Project, task.project_id)
    return {
        "id": task.id, "task_name": task.task_name,
        "project_id": task.project_id,
        "project_name": project.project_name if project else "",
        "review_type": task.review_type, "status": task.status,
        "total_files": task.total_files, "processed_files": task.processed_files,
        "total_issues": task.total_issues,
        "severe_issues": task.severe_issues, "high_issues": task.high_issues,
        "medium_issues": task.medium_issues, "low_issues": task.low_issues,
        "score": task.score, "summary": task.summary,
        "model_name": task.model_name, "duration_ms": task.duration_ms,
        "start_time": task.start_time, "end_time": task.end_time,
        "create_time": task.create_time,
        # R4 修复:任务失败时返回错误原因,对齐 TaskDetailOut schema
        "error_message": task.error_message,
        "files": _task_file_summaries(db, task.id),
        "agent_releases": _task_agent_release_summaries(db, task.id),
    }


def _task_agent_release_summaries(db: Session, task_id: int) -> list[dict]:
    """返回任务启动时冻结的自定义 Agent 版本，供报告归因与复现。"""
    rows = (
        db.query(ReviewTaskAgentRelease, CustomAgent, CustomAgentVersion)
        .join(CustomAgentVersion, CustomAgentVersion.id == ReviewTaskAgentRelease.agent_version_id)
        .join(CustomAgent, CustomAgent.id == CustomAgentVersion.agent_id)
        .filter(ReviewTaskAgentRelease.task_id == task_id)
        .order_by(ReviewTaskAgentRelease.id.asc())
        .all()
    )
    return [{
        "release_id": snapshot.release_id,
        "agent_code": agent.code,
        "agent_name": agent.name,
        "agent_version_id": version.id,
        "agent_version": version.version_number,
        "status": snapshot.status,
    } for snapshot, agent, version in rows]


def _task_file_summaries(db: Session, task_id: int) -> list[dict]:
    """查询审查任务关联文件,旧任务没有关联表记录时按问题文件回退。

    Args:
        db: 数据库会话
        task_id: 审查任务 ID

    Returns:
        list[dict]: 任务关联文件列表
    """
    files = db.query(CodeFile).join(
        ReviewTaskFile,
        ReviewTaskFile.file_id == CodeFile.id,
    ).filter(
        ReviewTaskFile.task_id == task_id,
    ).order_by(
        ReviewTaskFile.id.asc(),
    ).all()

    if not files:
        file_ids = [
            row.file_id for row in db.query(ReviewIssue.file_id)
            .filter(ReviewIssue.task_id == task_id, ReviewIssue.file_id.isnot(None))
            .distinct()
            .all()
        ]
        if file_ids:
            files = db.query(CodeFile).filter(CodeFile.id.in_(file_ids)).order_by(CodeFile.id.asc()).all()

    return [{
        "file_id": item.id,
        "project_id": item.project_id,
        "file_name": item.file_name,
        "file_path": item.file_path,
        "language": item.language,
        "line_count": item.line_count,
        "version_no": item.version_no,
    } for item in files]


def list_task_issues(db: Session, user: User, task_id: int, file_id: int = None,
                     severity: str = "", issue_type: str = "", status: str = "",
                     page: int = 1, page_size: int = 50) -> dict:
    """查询审查任务的问题列表(基于 project_member 关系)

    可见性:reviewer 可见同项目任务的问题,非成员返回 404。

    Args:
        db: 数据库会话
        user: 当前用户
        task_id: 任务ID
        file_id: 文件ID过滤
        severity: 严重程度过滤
        issue_type: 问题类型过滤
        status: 状态过滤
        page: 页码
        page_size: 每页数量

    Returns:
        dict: 分页响应

    Raises:
        NotFoundError: 任务不存在或无访问权限
    """
    from app.services.project_member_service import require_project_access
    task = db.get(ReviewTask, task_id)
    if not task or task.status == "deleted":
        raise NotFoundError("审查任务不存在", code=40400)
    # v2.4: 用 project_member 关系校验,reviewer 可读同项目任务的问题
    require_project_access(db, task.project_id, user, need_write=False)
    q = db.query(ReviewIssue).filter(ReviewIssue.task_id == task_id)
    if file_id:
        q = q.filter(ReviewIssue.file_id == file_id)
    if severity:
        q = q.filter(ReviewIssue.severity == severity)
    if issue_type:
        q = q.filter(ReviewIssue.issue_type == issue_type)
    if status:
        if status != "all":
            q = q.filter(ReviewIssue.status == status)
    else:
        q = q.filter(ReviewIssue.status.in_(["unfixed", "pending_review"]))

    total = q.count()
    pagination = Pagination(page, page_size, total)
    items = q.order_by(ReviewIssue.severity.desc(), ReviewIssue.id.asc()).offset(
        pagination.offset).limit(pagination.page_size).all()
    return pagination.to_dict(items)


def delete_task(db: Session, user: User, task_id: int) -> None:
    """软删除审查任务(需 owner/admin 权限)

    Args:
        db: 数据库会话
        user: 当前用户
        task_id: 任务ID

    Raises:
        NotFoundError: 任务不存在或无访问权限
        ForbiddenError: 仅 owner/admin 可删除
    """
    from app.services.project_member_service import require_project_access
    task = db.get(ReviewTask, task_id)
    if not task:
        raise NotFoundError("审查任务不存在", code=40400)
    # v2.4: 删除任务视为写操作,仅 owner/admin 可执行
    require_project_access(db, task.project_id, user, need_write=True)
    task.status = "deleted"
    db.commit()


def cancel_task(db: Session, user: User, task_id: int) -> None:
    """取消正在运行的审查任务(需 owner/admin 权限)

    Args:
        db: 数据库会话
        user: 当前用户
        task_id: 任务ID

    Raises:
        NotFoundError: 任务不存在或无访问权限
        ForbiddenError: 仅 owner/admin 可取消
        ValidationError: 任务不在运行中
    """
    from app.services.project_member_service import require_project_access
    task = db.get(ReviewTask, task_id)
    if not task:
        raise NotFoundError("审查任务不存在", code=40400)
    # v2.4: 取消任务视为写操作,仅 owner/admin 可执行
    require_project_access(db, task.project_id, user, need_write=True)
    if task.status != "running":
        raise ValidationError("只能取消运行中的任务", code=40001)
    task.status = "cancelled"
    db.commit()


class TaskCancelledError(Exception):
    """任务已被取消的内部信号"""


class TaskSupersededError(Exception):
    """当前 Worker 的数据库执行租约已被恢复调度器接管。"""


def _check_cancelled(
    db: Session,
    task: ReviewTask,
    execution_token: Optional[str] = None,
    *,
    lock: bool = False,
) -> None:
    """检查任务是否被取消,若已取消则抛出中断信号

    Args:
        db: 数据库会话
        task: 当前审查任务

    Raises:
        TaskCancelledError: 任务已被用户取消
    """
    if lock:
        task = (
            db.query(ReviewTask)
            .filter(ReviewTask.id == task.id)
            .with_for_update()
            .populate_existing()
            .one()
        )
    else:
        db.refresh(task)
    if task.status == "cancelled":
        raise TaskCancelledError("审查任务已被用户取消")
    if execution_token is not None and str(task.execution_token or "") != execution_token:
        raise TaskSupersededError("审查任务执行租约已被新的 Worker 接管")


def _emit_review_event(
    type_: AgentEventType,
    task: ReviewTask,
    user: User,
    message: str,
    agent_code: str = "review_orchestrator",
) -> None:
    """向 EventBus 发布审查过程中的 Agent 事件,供 SSE 实时同步至前端 Agent 办公室

    v2.4: AgentEvent.user_id 显式传入 user.id,使 SSE 端点可按用户隔离。

    Args:
        type_: 事件类型(DISPATCH/THINKING/PROGRESS/COMPLETE/FAILED)
        task: 当前审查任务
        user: 当前用户
        message: 事件描述文案
        agent_code: 代理标识,默认为 review_orchestrator
    """
    try:
        trace_id = f"review_{task.id}_{int(time.time() * 1000)}"
        AgentEventBus.instance().publish(AgentEvent(
            type=type_,
            agent=agent_code,
            trace_id=trace_id,
            message=message,
            payload={"task_id": task.id, "user_id": user.id},
            user_id=user.id,
        ))
    except Exception:
        pass
