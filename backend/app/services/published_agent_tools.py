"""已发布自定义 Agent 的权限内搜索与统一调用服务。"""

from __future__ import annotations

from dataclasses import asdict
from typing import Any, Optional

from sqlalchemy.orm import Session

from app.ai.deepseek_agent import DeepSeekAgent
from app.ai.multi_agent import format_agent_section
from app.ai.prompt_builder import build_prompt
from app.ai.result_parser import parse
from app.core.exceptions import ForbiddenError, NotFoundError
from app.core.permission_codes import PermissionCode
from app.models.user import User
from app.services import agent_studio_service, capability_catalog_service, rbac_service
from app.services.declarative_agent_runtime import DeclarativeReviewAgentFactory
from app.utils.api_resolver import resolve_api_config


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
