"""已发布自定义 Agent 的权限内搜索与统一调用服务。"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import asdict
from difflib import SequenceMatcher
from typing import Any, Mapping

from sqlalchemy.orm import Session

from app.ai.deepseek_agent import DeepSeekAgent
from app.ai.multi_agent import format_agent_section
from app.ai.prompt_builder import build_prompt
from app.ai.result_parser import parse
from app.core.exceptions import ForbiddenError, NotFoundError
from app.core.permission_codes import PermissionCode
from app.models.user import User
from app.services import agent_studio_service, rbac_service
from app.services.declarative_agent_runtime import DeclarativeReviewAgentFactory
from app.utils.api_resolver import resolve_api_config

_SEARCH_SEPARATORS = re.compile(r"[^0-9a-z\u3400-\u9fff]+")


def _require_invoke_permission(db: Session, user: User) -> None:
    if not rbac_service.check_permission(db, user.id, PermissionCode.CUSTOM_AGENT_INVOKE):
        raise ForbiddenError("当前用户没有调用已发布自定义 Agent 的权限", code=40300)


def _normalize_search_text(value: Any) -> str:
    normalized = unicodedata.normalize("NFKC", str(value or "")).casefold()
    return _SEARCH_SEPARATORS.sub("", normalized)


def _ngrams(value: str, size: int = 2) -> set[str]:
    if not value:
        return set()
    if len(value) <= size:
        return {value}
    return {value[index:index + size] for index in range(len(value) - size + 1)}


def _similarity(query: str, row: Mapping[str, Any]) -> tuple[float, list[str]]:
    fields = {
        "code": _normalize_search_text(row.get("code")),
        "name": _normalize_search_text(row.get("name")),
        "description": _normalize_search_text(row.get("description")),
    }
    if not query:
        return 1.0, ["catalog"]
    exact = [field for field, value in fields.items() if query == value]
    if exact:
        return 1.0, [f"exact_{field}" for field in exact]
    contains = [field for field, value in fields.items() if query in value or value in query]
    sequence_score = max(
        (SequenceMatcher(None, query, value).ratio() for value in fields.values() if value),
        default=0.0,
    )
    query_grams = _ngrams(query)
    gram_score = 0.0
    for value in fields.values():
        value_grams = _ngrams(value)
        union = query_grams | value_grams
        if union:
            gram_score = max(gram_score, len(query_grams & value_grams) / len(union))
    score = max(sequence_score, gram_score, 0.9 if contains else 0.0)
    reasons = [f"contains_{field}" for field in contains]
    if not reasons and score > 0:
        reasons = ["fuzzy"]
    return round(score, 4), reasons


def search_published_agents(
    db: Session,
    user: User,
    *,
    query: str = "",
    limit: int = 8,
) -> dict[str, Any]:
    """返回候选而不替模型做语义决定；候选不唯一时由模型动态追问。"""
    _require_invoke_permission(db, user)
    normalized_query = _normalize_search_text(query)
    ranked: list[dict[str, Any]] = []
    for row in agent_studio_service.list_catalog(db):
        score, reasons = _similarity(normalized_query, row)
        ranked.append(
            {
                "id": row["id"],
                "code": row["code"],
                "name": row["name"],
                "description": row.get("description") or "",
                "version_number": row["version_number"],
                "release_id": row["release_id"],
                "skills": row.get("skills") or [],
                "score": score,
                "match_reasons": reasons,
            }
        )
    ranked.sort(key=lambda item: (-item["score"], item["code"]))
    candidates = ranked[: max(1, min(int(limit), 20))]
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
        "total_catalog_size": len(ranked),
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
) -> dict[str, Any]:
    """通过与目录 API 相同的实现调用精确发布版本。"""
    _require_invoke_permission(db, user)
    definition = DeclarativeReviewAgentFactory.resolve_published(db, agent_code, user=user)
    if definition is None:
        raise NotFoundError("已发布 Agent 不存在或已停用", code=40400)
    profile = definition.to_profile()
    system_prompt, user_prompt = build_prompt(
        language=language,
        file_name=file_name,
        code=code,
        rules=rules or [],
        line_offset=line_offset,
        agent_section=format_agent_section(profile),
        experience_section=experience,
    )
    system_prompt = (
        f"{profile.system_prompt.strip()}\n\n"
        "平台强制契约：只审查用户提供的代码，严格输出现有 Issue JSON 结构；"
        "不得执行命令、访问网络、写文件或修改数据。\n\n"
        f"{system_prompt}"
    )
    client = DeepSeekAgent(api_config=resolve_api_config(db, user.id))
    raw, meta = client.call_raw(
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        agent_label=profile.code,
        temperature=profile.temperature,
        max_tokens=profile.max_tokens,
    )
    result = parse(raw)
    client.log_deferred(db, user_id=user.id, meta=meta)
    db.commit()
    return {
        "agent_code": profile.code,
        "release_id": profile.release_id,
        "version_id": profile.version_id,
        "summary": result.summary,
        "score": result.score,
        "issues": [asdict(issue) for issue in result.issues],
        "usage": {
            "prompt_tokens": meta.get("prompt_tokens", 0),
            "completion_tokens": meta.get("completion_tokens", 0),
            "total_tokens": meta.get("total_tokens", 0),
            "duration_ms": meta.get("duration_ms", 0),
        },
    }
