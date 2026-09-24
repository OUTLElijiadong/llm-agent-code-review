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
    chunk_index: int,
    total_chunks: int,
) -> tuple[str, str]:
    """Render an exact source window with a stable origin and symbol context."""
    context = (
        f"源文件 SHA-256: {source_sha256}\n"
        f"审查分片: {chunk_index + 1}/{total_chunks}; "
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
        "不得执行命令、访问网络、写文件或修改数据。\n\n"
        f"{system_prompt}",
        user_prompt,
    )


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
                    chunk_index=index,
                    total_chunks=len(chunks),
                ),
            )
            for index, chunk in enumerate(chunks)
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
    summaries: list[str] = []
    scores: list[tuple[int, int]] = []
    issues: list[dict[str, Any]] = []
    metas: list[dict] = []
    completed: list[dict[str, Any]] = []
    with usage_context(int(user.id), current_attribution(int(user.id)), db=db):
        for index, (chunk, system_prompt, user_prompt) in enumerate(calls):
            call_args = {
                "system_prompt": system_prompt,
                "user_prompt": user_prompt,
                "agent_label": profile.code,
                "temperature": profile.temperature,
            }
            try:
                raw, meta = client.call_raw(**call_args, max_tokens=profile.max_tokens)
            except DeepSeekOutputTruncatedError:
                # Reasoning and JSON share the provider output allowance. A
                # partial JSON cannot be counted as reviewed source coverage.
                ceiling = _clamp_max_tokens(settings.deepseek_max_output_tokens)
                initial_budget = _clamp_max_tokens(profile.max_tokens)
                retry_budget = min(max(initial_budget * 4, 8192), ceiling)
                if retry_budget <= initial_budget:
                    raise
                logger.warning(
                    f"[published_agent] {profile.code} 分片 {index + 1}/{len(calls)} "
                    f"输出被截断，按 {initial_budget}→{retry_budget} 提高输出预算重试"
                )
                raw, meta = client.call_raw(**call_args, max_tokens=retry_budget)
            result = parse(raw)
            if result.invalid_issue_count:
                raise DeepSeekResponseError(
                    f"已发布 Agent 分片 {index + 1}/{len(calls)} 有 "
                    f"{result.invalid_issue_count} 条问题未通过解析，无法声明完整审查"
                )
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
                "index": index + 1,
                "start_line": line_offset + chunk.start_line + 1,
                "end_line": line_offset + chunk.end_line,
                "source_chars": len(chunk.text),
                "source_sha256": hashlib.sha256(chunk.text.encode("utf-8")).hexdigest(),
            })
            client.log_deferred(db, user_id=user.id, meta=meta)
    db.commit()
    if len(calls) == 1:
        summary = summaries[0]
    else:
        summary = "\n".join(
            f"分片 {index + 1}/{len(calls)}：{value}"
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
            "prompt_tokens": _sum_usage(metas, "prompt_tokens"),
            "completion_tokens": _sum_usage(metas, "completion_tokens"),
            "total_tokens": _sum_usage(metas, "total_tokens"),
            "duration_ms": sum(int(meta.get("duration_ms") or 0) for meta in metas),
        },
        "coverage": {
            "status": "complete",
            "source_sha256": source_sha256,
            "total_chunks": len(calls),
            "completed_chunks": len(completed),
            "chunks": completed,
        },
    }
