"""已发布自定义 Agent 的权限内搜索与统一调用服务。"""

from __future__ import annotations

import hashlib
from dataclasses import asdict
from typing import Any, Optional

from loguru import logger
from sqlalchemy.orm import Session

from app.ai.code_chunker import CodeChunk, chunk_code_with_context
from app.ai.deepseek_agent import (
    DeepSeekAgent,
    DeepSeekOutputTruncatedError,
    DeepSeekResponseError,
    _clamp_max_tokens,
)
from app.ai.multi_agent import format_agent_section
from app.ai.prompt_builder import build_prompt
from app.ai.result_parser import parse
from app.core.config import settings
from app.core.exceptions import ForbiddenError, NotFoundError, ValidationError
from app.core.permission_codes import PermissionCode
from app.models.user import User
from app.services import agent_studio_service, capability_catalog_service, rbac_service
from app.services.agent_model_service import resolve_subagent_config
from app.services.ai_usage_context import current_attribution, usage_context
from app.services.declarative_agent_runtime import DeclarativeReviewAgentFactory
from app.utils.api_resolver import resolve_api_config

_MODEL_INPUT_RESERVE_BYTES = 1024
_MAX_PUBLISHED_REVIEW_CHUNKS = 128
_MAX_TRUNCATION_SPLIT_DEPTH = 8
_MAX_LINE_OFFSET = 10_000_000


def _build_review_call(
    profile,
    *,
    language: str,
    file_name: str,
    chunk: CodeChunk,
    rules: list[dict[str, Any]],
    line_offset: int,
    experience: str,
    source_sha256: str,
) -> tuple[str, str]:
    """Render an exact source window with a stable origin and symbol context."""
    context = (
        f"源文件 SHA-256: {source_sha256}\n"
        "审查焦点: "
        f"原文件行: {chunk.start_line + line_offset + 1}-{chunk.end_line + line_offset}\n"
        f"分片 SHA-256: {hashlib.sha256(chunk.text.encode('utf-8')).hexdigest()}\n"
        f"{chunk.context}"
    )
    system_prompt, user_prompt = build_prompt(
        language=language,
        file_name=file_name,
        code=chunk.text,
        rules=rules,
        line_offset=line_offset + chunk.start_line,
        agent_section=format_agent_section(profile),
        experience_section=experience,
        context_section=context,
    )
    return (
        f"{profile.system_prompt.strip()}\n\n"
        "平台强制契约：只审查用户提供的代码，严格输出现有 Issue JSON 结构；"
        "每条 issue 的 evidence 必须逐字来自当前源码分片，"
        "line_number 必须能定位到该 evidence；不得编造文件、函数、依赖、调用路径、漏洞利用结果或覆盖范围。"
        "证据不足、路径未闭合或行号/evidence 无法核实时省略该 issue，并在 summary 中说明实际未覆盖范围；"
        "不得执行命令、访问网络、写文件或修改数据。\n\n"
        f"{system_prompt}",
        user_prompt,
    )


def _validate_issue_source_evidence(issue, chunk: CodeChunk) -> None:
    """Reject published-agent findings whose quote and chunk-local start line do not match."""
    evidence = str(getattr(issue, "evidence", "") or "")
    source = chunk.text.replace("\r\n", "\n").replace("\r", "\n")
    normalized_evidence = evidence.replace("\r\n", "\n").replace("\r", "\n")
    if not evidence.strip() or normalized_evidence not in source:
        raise DeepSeekResponseError("已发布 Agent 的问题证据不在当前源码分片中")
    raw_line_number = getattr(issue, "line_number", 0)
    if isinstance(raw_line_number, bool):
        raise DeepSeekResponseError("已发布 Agent 的问题行号不能是布尔值")
    try:
        line_number = int(raw_line_number or 0)
    except (TypeError, ValueError) as exc:
        raise DeepSeekResponseError("已发布 Agent 的问题行号非法") from exc
    if line_number <= 0:
        raise DeepSeekResponseError("已发布 Agent 的问题行号非法")
    # Check the complete quote's actual start line. Checking only its first line
    # lets repeated prefixes attach a multi-line quote to the wrong location.
    start = 0
    while True:
        start = source.find(normalized_evidence, start)
        if start < 0:
            break
        actual_line = source.count("\n", 0, start) + 1
        if actual_line == line_number:
            end_line = getattr(issue, "end_line", None)
            if end_line is not None:
                if isinstance(end_line, bool):
                    raise DeepSeekResponseError("已发布 Agent 的问题结束行不能是布尔值")
                try:
                    end_line = int(end_line)
                except (TypeError, ValueError) as exc:
                    raise DeepSeekResponseError("已发布 Agent 的问题结束行非法") from exc
                if end_line < line_number or end_line > len(source.splitlines()):
                    raise DeepSeekResponseError("已发布 Agent 的问题结束行超出当前焦点范围")
            return
        start += 1
    raise DeepSeekResponseError("已发布 Agent 的问题行号与逐字证据不匹配")


def _plan_complete_review(
    profile,
    *,
    code: str,
    language: str,
    file_name: str,
    rules: list[dict[str, Any]],
    line_offset: int,
    experience: str,
) -> tuple[str, list[tuple[CodeChunk, str, str]]]:
    """Plan every source window before any model call; reject unfit Skill context.

    UTF-8 bytes conservatively upper-bound a byte-tokenized prompt. Reserving
    the configured output ceiling also keeps output-length retries inside the
    same context window. The checked source windows must reconstruct the exact
    original input, including whitespace and very long individual lines.
    """
    input_bytes = (
        int(settings.deepseek_context_window_tokens)
        - _clamp_max_tokens(settings.deepseek_max_output_tokens)
        - _MODEL_INPUT_RESERVE_BYTES
    )
    if input_bytes <= 0:
        raise ValidationError("已发布 Agent 模型上下文预算不足，无法完整审查")
    source_sha256 = hashlib.sha256(code.encode("utf-8")).hexdigest()
    threshold = max(256, int(settings.deepseek_chunk_threshold or 32_000))
    while True:
        chunks = chunk_code_with_context(code, language, threshold=threshold)
        if len(chunks) > _MAX_PUBLISHED_REVIEW_CHUNKS:
            raise ValidationError("已发布 Agent 源码分片超过安全上限，无法证明完整审查")
        if "".join(chunk.text for chunk in chunks) != code:
            raise ValidationError("已发布 Agent 源码分片未覆盖完整原文，已停止审查")
        calls = [
            (
                chunk,
                *_build_review_call(
                    profile,
                    language=language,
                    file_name=file_name,
                    chunk=chunk,
                    rules=rules,
                    line_offset=line_offset,
                    experience=experience,
                    source_sha256=source_sha256,
                ),
            )
            for chunk in chunks
        ]
        if all(
            len(system.encode("utf-8")) + len(user.encode("utf-8")) <= input_bytes
            for _, system, user in calls
        ):
            return source_sha256, calls
        if threshold <= 256:
            raise ValidationError(
                "已发布 Agent 的已审批 Skill、规则或单个源码窗口超过模型上下文预算，"
                "无法完整审查；请精简配置或提高模型上下文容量"
            )
        threshold = max(256, threshold // 2)


def _sum_usage(values: list[dict], key: str) -> int | None:
    available = [
        value[key]
        for value in values
        if isinstance(value.get(key), int) and not isinstance(value[key], bool)
    ]
    return sum(available) if available else None


def _split_truncated_chunk(chunk: CodeChunk) -> list[CodeChunk]:
    """Split only at source-line boundaries and preserve the parent symbol context."""
    source_lines = chunk.text.splitlines(keepends=True)
    if len(source_lines) <= 1:
        return []
    midpoint = len(source_lines) // 2
    left_text = "".join(source_lines[:midpoint])
    right_text = "".join(source_lines[midpoint:])
    if not left_text or not right_text or left_text + right_text != chunk.text:
        return []
    shared = {
        "context": chunk.context,
        "context_fingerprint": chunk.context_fingerprint,
        "symbol_names": chunk.symbol_names,
        "diagnostics": chunk.diagnostics,
    }
    return [
        CodeChunk(
            text=left_text,
            start_line=chunk.start_line,
            end_line=chunk.start_line + midpoint,
            **shared,
        ),
        CodeChunk(
            text=right_text,
            start_line=chunk.start_line + midpoint,
            end_line=chunk.end_line,
            **shared,
        ),
    ]


def _require_invoke_permission(db: Session, user: User) -> None:
    if not rbac_service.check_permission(db, user.id, PermissionCode.CUSTOM_AGENT_INVOKE):
        raise ForbiddenError("当前用户没有调用已发布自定义 Agent 的权限", code=40300)


def search_published_agents(
    db: Session,
    user: User,
    *,
    query: str = "",
    limit: int = 8,
) -> dict[str, Any]:
    """返回候选而不替模型做语义决定；候选不唯一时由模型动态追问。"""
    _require_invoke_permission(db, user)
    catalog = agent_studio_service.list_catalog(db)
    capability_codes = [f"published_agent:{row['code']}" for row in catalog]
    persisted_aliases = capability_catalog_service.authorized_aliases(db, capability_codes)
    aliases_by_code = {
        row["code"]: persisted_aliases.get(f"published_agent:{row['code']}", [])
        for row in catalog
    }
    ranked = capability_catalog_service.rank_rows(
        db,
        catalog,
        query,
        aliases_by_code=aliases_by_code,
        user_id=user.id,
        limit=max(1, min(int(limit), 20)),
    )
    candidates = [
        {
            "id": row["id"],
            "code": row["code"],
            "name": row["name"],
            "description": row.get("description") or "",
            "version_number": row["version_number"],
            "release_id": row["release_id"],
            "skills": row.get("skills") or [],
            "score": row["score"],
            "match_reasons": row["match_reasons"],
        }
        for row in ranked
    ]
    exact_candidates = [item for item in candidates if item["score"] == 1.0 and item["match_reasons"] != ["catalog"]]
    if not candidates:
        selection_state = "no_candidates"
    elif len(exact_candidates) == 1:
        selection_state = "exact"
    else:
        selection_state = "ambiguous"
    return {
        "query": query,
        "selection_state": selection_state,
        "requires_clarification": selection_state == "ambiguous",
        "candidates": candidates,
        "total_catalog_size": len(catalog),
    }


def invoke_published_agent(
    db: Session,
    user: User,
    *,
    agent_code: str,
    code: str,
    language: str = "plaintext",
    file_name: str = "snippet.txt",
    rules: list[dict[str, Any]] | None = None,
    line_offset: int = 0,
    experience: str = "",
    release_id: Optional[int] = None,
    version_id: Optional[int] = None,
    package_checksum: str = "",
    template_checksum: str = "",
) -> dict[str, Any]:
    """通过与目录 API 相同的实现调用精确发布版本。"""
    _require_invoke_permission(db, user)
    if (
        isinstance(line_offset, bool)
        or not isinstance(line_offset, int)
        or not 0 <= line_offset <= _MAX_LINE_OFFSET
    ):
        raise ValidationError(f"line_offset 必须是 0 至 {_MAX_LINE_OFFSET} 的整数")
    if release_id is not None or version_id is not None:
        if release_id is None or version_id is None:
            raise NotFoundError("已发布 Agent 快照不完整", code=40400)
        definition = DeclarativeReviewAgentFactory.resolve_release(
            db,
            agent_code,
            release_id=int(release_id),
            version_id=int(version_id),
            package_checksum=package_checksum,
            template_checksum=template_checksum,
            user=user,
        )
    else:
        definition = DeclarativeReviewAgentFactory.resolve_published(db, agent_code, user=user)
    if definition is None:
        raise NotFoundError("已发布 Agent 不存在、已停用或快照校验失败", code=40400)
    profile = definition.to_profile()
    source_sha256, calls = _plan_complete_review(
        profile,
        code=code,
        language=language,
        file_name=file_name,
        rules=rules or [],
        line_offset=line_offset,
        experience=experience,
    )
    client = DeepSeekAgent(api_config=resolve_subagent_config(db, resolve_api_config(db, user.id)))

    def persist_failed_attempts(error_message: str) -> None:
        for failed_meta in failed_attempt_metas:
            client.log_deferred(
                db,
                user_id=user.id,
                meta=failed_meta,
                status="failed",
                error=error_message[:500],
            )
        db.commit()

    summaries: list[str] = []
    scores: list[tuple[int, int]] = []
    issues: list[dict[str, Any]] = []
    metas: list[dict] = []
    failed_attempt_metas: list[dict] = []
    completed: list[dict[str, Any]] = []
    pending = [
        (chunk, system_prompt, user_prompt, 0)
        for chunk, system_prompt, user_prompt in calls
    ]
    planned_chunks = len(pending)
    with usage_context(int(user.id), current_attribution(int(user.id)), db=db):
        while pending:
            chunk, system_prompt, user_prompt, split_depth = pending.pop(0)
            call_args = {
                "system_prompt": system_prompt,
                "user_prompt": user_prompt,
                "agent_label": profile.code,
                "temperature": profile.temperature,
            }
            try:
                raw, meta = client.call_raw(**call_args, max_tokens=profile.max_tokens)
            except DeepSeekOutputTruncatedError as first_error:
                if isinstance(getattr(first_error, "meta", None), dict):
                    failed_attempt_metas.append(first_error.meta)
                # Reasoning and JSON share the provider output allowance. A
                # partial JSON cannot be counted as reviewed source coverage.
                ceiling = _clamp_max_tokens(settings.deepseek_max_output_tokens)
                initial_budget = _clamp_max_tokens(profile.max_tokens)
                retry_budget = min(max(initial_budget * 4, 8192), ceiling)
                retry_error = None
                if retry_budget > initial_budget:
                    logger.warning(
                        f"[published_agent] {profile.code} 焦点范围 "
                        f"{line_offset + chunk.start_line + 1}-{line_offset + chunk.end_line} "
                        f"输出被截断，按 {initial_budget}→{retry_budget} 提高输出预算重试"
                    )
                    try:
                        raw, meta = client.call_raw(**call_args, max_tokens=retry_budget)
                    except DeepSeekOutputTruncatedError as exc:
                        if isinstance(getattr(exc, "meta", None), dict):
                            failed_attempt_metas.append(exc.meta)
                        retry_error = exc
                else:
                    retry_error = DeepSeekOutputTruncatedError(
                        "输出预算已达上限，需缩小源码分片",
                        finish_reason="length",
                    )
                if retry_error is not None:
                    children = _split_truncated_chunk(chunk)
                    if (
                        not children
                        or split_depth >= _MAX_TRUNCATION_SPLIT_DEPTH
                        or planned_chunks + len(children) - 1 > _MAX_PUBLISHED_REVIEW_CHUNKS
                    ):
                        error = DeepSeekOutputTruncatedError(
                            f"已发布 Agent 原文件第 {line_offset + chunk.start_line + 1}-"
                            f"{line_offset + chunk.end_line} 行输出仍被截断，审查覆盖不完整",
                            finish_reason="length",
                        )
                        error.attempt_metas = list(failed_attempt_metas)
                        persist_failed_attempts(str(error))
                        raise error from retry_error
                    new_calls = []
                    for child in children:
                        child_system, child_user = _build_review_call(
                            profile,
                            language=language,
                            file_name=file_name,
                            chunk=child,
                            rules=rules or [],
                            line_offset=line_offset,
                            experience=experience,
                            source_sha256=source_sha256,
                        )
                        new_calls.append((child, child_system, child_user, split_depth + 1))
                    planned_chunks += len(children) - 1
                    pending[0:0] = new_calls
                    continue
            try:
                result = parse(raw)
                if result.invalid_issue_count:
                    raise DeepSeekResponseError(
                        f"已发布 Agent 原文件第 {line_offset + chunk.start_line + 1}-"
                        f"{line_offset + chunk.end_line} 行有 "
                        f"{result.invalid_issue_count} 条问题未通过解析，无法声明完整审查"
                    )
                for issue in result.issues:
                    _validate_issue_source_evidence(issue, chunk)
            except DeepSeekResponseError as exc:
                failed_attempt_metas.append(meta)
                exc.attempt_metas = list(failed_attempt_metas)
                persist_failed_attempts(str(exc))
                raise
            summaries.append(result.summary)
            scores.append((int(result.score), max(1, len(chunk.text))))
            for issue in result.issues:
                item = asdict(issue)
                for field in ("line_number", "end_line"):
                    number = item.get(field)
                    if isinstance(number, int) and number > 0:
                        item[field] = number + line_offset + chunk.start_line
                issues.append(item)
            metas.append(meta)
            completed.append({
                "index": len(completed) + 1,
                "start_line": line_offset + chunk.start_line + 1,
                "end_line": line_offset + chunk.end_line,
                "source_chars": len(chunk.text),
                "source_sha256": hashlib.sha256(chunk.text.encode("utf-8")).hexdigest(),
            })
            client.log_deferred(db, user_id=user.id, meta=meta)
    persist_failed_attempts("模型输出被截断，后续通过源码范围恢复完成")
    if len(completed) == 1:
        summary = summaries[0]
    else:
        summary = "\n".join(
            f"分片 {index + 1}/{len(completed)}：{value}"
            for index, value in enumerate(summaries)
        )
    weight = sum(chars for _, chars in scores)
    return {
        "agent_code": profile.code,
        "release_id": profile.release_id,
        "version_id": profile.version_id,
        "summary": summary,
        "score": round(sum(score * chars for score, chars in scores) / weight),
        "issues": issues,
        "usage": {
            "prompt_tokens": _sum_usage(metas + failed_attempt_metas, "prompt_tokens"),
            "completion_tokens": _sum_usage(metas + failed_attempt_metas, "completion_tokens"),
            "total_tokens": _sum_usage(metas + failed_attempt_metas, "total_tokens"),
            "duration_ms": sum(int(meta.get("duration_ms") or 0) for meta in metas + failed_attempt_metas),
        },
        "coverage": {
            "status": "complete",
            "source_sha256": source_sha256,
            "total_chunks": planned_chunks,
            "completed_chunks": len(completed),
            "chunks": completed,
        },
    }
