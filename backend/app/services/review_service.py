"""
审查服务模块: 核心业务编排
整合项目/文件/规则/AI模块,完成代码审查全流程

v2.1.2: 多 Agent 协同审查 — 三阶段流水线
    阶段1 并行感知: 所有专项代理用独立线程并行审查同一分片
    阶段2 交叉复审: 首席架构师代理对比合并各代理发现
    阶段3 共识统合: 仲裁官将交叉复审结果转化为最终问题清单
"""
import concurrent.futures
import json as json_lib
import time
from datetime import datetime, timezone
from typing import Optional

from loguru import logger
from sqlalchemy.orm import Session

from app.agents.event_bus import AgentEventBus
from app.agents.events import AgentEvent, AgentEventType
from app.ai.code_chunker import chunk_code
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
from app.ai.result_parser import parse
from app.ai.scoring import compute_score
from app.core.config import settings
from app.core.exceptions import ForbiddenError, NotFoundError, ValidationError
from app.core.pagination import Pagination
from app.models.code_file import CodeFile
from app.models.project import Project
from app.models.review_issue import ReviewIssue
from app.models.review_task import ReviewTask
from app.models.review_task_file import ReviewTaskFile
from app.models.user import User
from app.schemas.review import ReviewStartIn
from app.services.rule_service import get_enabled_rules

# 多Agent审查画像 → 注册中心BaseAgent code 的映射
_PROFILE_TO_AGENT_CODE: dict[str, str] = {
    "general": "code_reviewer",
    "security": "security_sentinel",
    "reliability": "code_reviewer",
    "performance": "code_reviewer",
    "maintainability": "code_reviewer",
}

_COLLAB_PARALLEL_THREADS = 4


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
    """启动代码审查任务(同步执行)

    Args:
        db: 数据库会话
        user: 当前用户
        payload: 审查启动请求

    Returns:
        ReviewTask: 完成后的审查任务

    Raises:
        NotFoundError: 项目或文件不存在
        ValidationError: 文件数量不合法
    """
    project = db.get(Project, payload.project_id)
    if not project:
        raise NotFoundError("项目不存在", code=40400)
    if project.user_id != user.id and user.role != "admin":
        raise ForbiddenError("无访问权限", code=40300)
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

    # Agent 自进化 L1: 检索本语言历史经验注入 Prompt;失败则降级为不注入,审查照常进行
    experience_section = ""
    try:
        from app.services import experience_service
        exps = experience_service.retrieve(db, language=project_lang)
        experience_section = _format_experience(exps)
        if exps:
            logger.info(f"[evolution] 注入 {len(exps)} 条历史经验到本次审查 (lang={project_lang or '*'})")
    except Exception as e:
        logger.warning(f"[evolution] 经验检索失败,降级为不注入: {e}")
        experience_section = ""

    review_type = payload.review_type or "standard"
    profiles = get_agent_profiles(review_type)
    agent = DeepSeekAgent()

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
    )
    db.add(task)
    _safe_commit(db)
    db.refresh(task)
    db.add_all(ReviewTaskFile(task_id=task.id, file_id=f.id) for f in files)
    _safe_commit(db)

    t0 = time.time()
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
            _check_cancelled(db, task)
            file_issues = _review_one_file(db, agent, task, code_file, rules, user, profiles,
                                           experience_section=experience_section)
            all_issues.extend(file_issues)
            task.processed_files += 1
            _safe_commit(db)
            db.refresh(task)
            for ac in set(agent_codes):
                _emit_review_event(AgentEventType.PROGRESS, task, user,
                                   f"文件 {task.processed_files}/{task.total_files}: {code_file.file_name} 审查完成,"
                                   f"累计 {len(all_issues)} 个问题",
                                   agent_code=ac)

        sev_count = {"严重": 0, "高": 0, "中": 0, "低": 0}
        for it in all_issues:
            sev_count[it.severity] = sev_count.get(it.severity, 0) + 1
        task.total_issues = len(all_issues)
        task.severe_issues = sev_count["严重"]
        task.high_issues = sev_count["高"]
        task.medium_issues = sev_count["中"]
        task.low_issues = sev_count["低"]
        task.score = compute_score(sev_count)
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
    except Exception as e:
        logger.exception(e)
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
        raise

    db.refresh(task)
    return task


def _review_one_file(db: Session, agent: DeepSeekAgent, task: ReviewTask,
                     code_file: CodeFile, rules: list, user: User,
                     profiles: tuple[ReviewAgentProfile, ...],
                     experience_section: str = "") -> list[ReviewIssue]:
    """审查单个文件: 分片 → 多代理协同审查 → 入库

    单代理模式(quick/standard): 直接串行审查,无协同开销。
    多代理模式(security/performance/full): 三阶段协同流水线 —

      阶段1 · 并行感知 — 所有代理用独立线程并行审查同一分片
      阶段2 · 交叉复审 — 首席架构师 Agent 对比、去重、升级各代理发现
      阶段3 · 共识统合 — 仲裁官将交叉复审结果转化为最终问题清单
    """
    chunks = chunk_code(code_file.content, code_file.language,
                        threshold=settings.deepseek_chunk_threshold)
    issues_acc: list[ReviewIssue] = []
    seen: set[tuple] = set()

    use_collab = len(profiles) >= 2

    for idx, chunk in enumerate(chunks):
        if use_collab:
            collab_issues = _review_chunk_collaborative(
                db, agent, task, code_file, rules, user,
                profiles, idx, chunk, experience_section=experience_section,
            )
        else:
            collab_issues = _review_chunk_sequential(
                db, agent, task, code_file, rules, user,
                profiles, idx, chunk, experience_section=experience_section,
            )

        for it in collab_issues:
            fingerprint = _issue_fingerprint(
                file_id=code_file.id,
                line_number=it.get("line_number", 0),
                end_line=it.get("end_line"),
                issue_type=it.get("issue_type", ""),
                title=it.get("title", ""),
                description=it.get("description", ""),
            )
            if fingerprint in seen:
                continue
            seen.add(fingerprint)
            issues_acc.append(ReviewIssue(
                task_id=task.id,
                file_id=code_file.id,
                file_name=code_file.file_name,
                line_number=it.get("line_number", 0),
                end_line=it.get("end_line"),
                issue_type=it.get("issue_type", ""),
                severity=it.get("severity", "中"),
                title=it.get("title", ""),
                description=it.get("description", ""),
                suggestion=it.get("suggestion", ""),
                fixed_code=it.get("fixed_code", ""),
                status="unfixed",
            ))

    if issues_acc:
        db.add_all(issues_acc)
        db.commit()
    return issues_acc


# ═══════════════ 单代理模式 (quick/standard) ═══════════════

def _review_chunk_sequential(
    db: Session, agent: DeepSeekAgent, task: ReviewTask,
    code_file: CodeFile, rules: list, user: User,
    profiles: tuple[ReviewAgentProfile, ...], chunk_idx: int,
    chunk, experience_section: str = "",
) -> list[dict]:
    """单代理串行审查 — 保留原有语义,无协同开销"""
    issues: list[dict] = []
    for agent_idx, profile in enumerate(profiles):
        system_prompt, user_prompt = build_prompt(
            language=code_file.language,
            file_name=code_file.file_name,
            code=chunk.text,
            rules=rules,
            line_offset=chunk.start_line,
            agent_section=format_agent_section(profile),
            experience_section=experience_section,
        )
        target_agent = _PROFILE_TO_AGENT_CODE.get(profile.code, profile.code)
        try:
            _emit_review_event(AgentEventType.THINKING, task, user,
                               f"[{profile.name}] 正在审查 {code_file.file_name}",
                               agent_code=target_agent)
            raw, _meta = agent.chat(
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                task_id=task.id,
                user_id=user.id,
                file_id=code_file.id,
                chunk_index=chunk_idx * 100 + agent_idx,
                db=db,
                agent_label=profile.code,
            )
            result = parse(raw)
            _emit_review_event(AgentEventType.COMPLETE, task, user,
                               f"[{profile.name}] 审查 {code_file.file_name} 完成,"
                               f"发现 {len(result.issues)} 个问题",
                               agent_code=target_agent)
        except Exception as e:
            logger.warning(f"文件 {code_file.file_name} chunk {chunk_idx} {profile.code} 失败: {e}")
            continue

        for it in result.issues:
            issues.append({
                "issue_type": it.issue_type,
                "severity": it.severity,
                "title": it.title,
                "line_number": _absolute_line(it.line_number, chunk.start_line),
                "end_line": _absolute_line(it.end_line, chunk.start_line) if it.end_line else None,
                "description": it.description or "",
                "suggestion": it.suggestion or "",
                "fixed_code": it.fixed_code or "",
            })
    return issues


# ═══════════════ 多 Agent 协同三阶段 ═══════════════

def _review_chunk_collaborative(
    db: Session, shared_agent: DeepSeekAgent, task: ReviewTask,
    code_file: CodeFile, rules: list, user: User,
    profiles: tuple[ReviewAgentProfile, ...], chunk_idx: int,
    chunk, experience_section: str = "",
) -> list[dict]:
    """多 Agent 协同审查 — 三阶段流水线

    阶段1: 并行感知 — ThreadPoolExecutor 并行调用各 Agent
    阶段2: 交叉复审 — 首席架构师对比合并发现
    阶段3: 共识统合 — 仲裁官输出最终问题清单
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
            )
            future_map[future] = profile

        for future, profile in future_map.items():
            agent_names[profile.code] = profile.name
            target = _PROFILE_TO_AGENT_CODE.get(profile.code, profile.code)
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
                logger.warning(f"并行审查 {profile.code} 失败: {e}")
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
    return final_issues


# ═══════════════ 协同辅助函数 ═══════════════

def _call_single_agent(
    profile: ReviewAgentProfile,
    code: str,
    language: str,
    file_name: str,
    rules: list,
    line_offset: int,
    experience_section: str = "",
) -> tuple:
    """单次代理并行调用 — 通过 DeepSeekAgent.call_raw() 统一入口

    每个线程独立创建 DeepSeekAgent,通过统一的 call_raw() 调用 DeepSeek API。
    返回 (raw_response_text, meta_dict), meta 供主线程事后 log_deferred() 补写 AiCallLog。
    """
    agent = DeepSeekAgent()
    system_prompt, user_prompt = build_prompt(
        language=language,
        file_name=file_name,
        code=code,
        rules=rules,
        line_offset=line_offset,
        agent_section=format_agent_section(profile),
        experience_section=experience_section,
    )
    return agent.call_raw(
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        agent_label=profile.code,
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
                f"      描述={it.get('description','')[:120]}",
            )
        parts.append("")
    return "\n".join(parts)


def _fallback_cross_review(
    findings: dict[str, list[dict]],
    names: dict[str, str],
) -> list[dict]:
    """降级方案: 交叉复审 LLM 失败时,直接按规则合并"""
    results: list[dict] = []
    seen_titles: set[str] = set()
    for code, items in findings.items():
        name = names.get(code, code)
        for it in items:
            title = it.get("title", "")
            if title in seen_titles:
                continue
            seen_titles.add(title)
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
                "confidence": 0.8,
                "owasp": "",
                "cwe": "",
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


def _issue_fingerprint(*, file_id: int, line_number: int, end_line: Optional[int],
                       issue_type: str, title: Optional[str], description: str) -> tuple:
    """生成问题去重指纹

    Args:
        file_id: 文件ID。
        line_number: 问题起始行号。
        end_line: 问题结束行号。
        issue_type: 问题类型。
        title: 问题标题。
        description: 问题描述。

    Returns:
        tuple: 可放入 set 的稳定去重键。
    """
    return (
        file_id,
        line_number,
        end_line or 0,
        issue_type,
        (title or "").strip()[:80],
        (description or "").strip()[:120],
    )


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
    """查询审查任务列表

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
    q = db.query(ReviewTask).filter(ReviewTask.status != "deleted")
    if user.role != "admin":
        q = q.filter(ReviewTask.user_id == user.id)
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

    items = []
    for row in rows:
        project = db.get(Project, row.project_id)
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

    Args:
        db: 数据库会话
        user: 当前用户
        task_id: 任务ID

    Returns:
        dict: 任务详情含关联项目
    """
    task = db.get(ReviewTask, task_id)
    if not task:
        raise NotFoundError("审查任务不存在", code=40400)
    if task.user_id != user.id and user.role != "admin":
        raise NotFoundError("审查任务不存在", code=40400)
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
        "files": _task_file_summaries(db, task.id),
    }


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
    """查询审查任务的问题列表

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
    """
    task = db.get(ReviewTask, task_id)
    if not task:
        raise NotFoundError("审查任务不存在", code=40400)
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
    """软删除审查任务"""
    task = db.get(ReviewTask, task_id)
    if not task:
        raise NotFoundError("审查任务不存在", code=40400)
    if task.user_id != user.id and user.role != "admin":
        raise ForbiddenError("无权限删除此任务", code=40300)
    task.status = "deleted"
    db.commit()


def cancel_task(db: Session, user: User, task_id: int) -> None:
    """取消正在运行的审查任务

    Args:
        db: 数据库会话
        user: 当前用户
        task_id: 任务ID

    Raises:
        NotFoundError: 任务不存在
        ForbiddenError: 无操作权限
        ValidationError: 任务不在运行中
    """
    task = db.get(ReviewTask, task_id)
    if not task:
        raise NotFoundError("审查任务不存在", code=40400)
    if task.user_id != user.id and user.role != "admin":
        raise ForbiddenError("无权限取消此任务", code=40300)
    if task.status != "running":
        raise ValidationError("只能取消运行中的任务", code=40001)
    task.status = "cancelled"
    db.commit()


class TaskCancelledError(Exception):
    """任务已被取消的内部信号"""


def _check_cancelled(db: Session, task: ReviewTask) -> None:
    """检查任务是否被取消,若已取消则抛出中断信号

    Args:
        db: 数据库会话
        task: 当前审查任务

    Raises:
        TaskCancelledError: 任务已被用户取消
    """
    db.refresh(task)
    if task.status == "cancelled":
        raise TaskCancelledError("审查任务已被用户取消")


def _emit_review_event(
    type_: AgentEventType,
    task: ReviewTask,
    user: User,
    message: str,
    agent_code: str = "review_orchestrator",
) -> None:
    """向 EventBus 发布审查过程中的 Agent 事件,供 SSE 实时同步至前端 Agent 办公室

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
        ))
    except Exception:
        pass
