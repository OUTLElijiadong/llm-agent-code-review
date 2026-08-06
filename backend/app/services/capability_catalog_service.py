"""在授权结果内统一检索 Agent、Skill、页面、MCP 与沙箱能力。"""

from __future__ import annotations

import re
import unicodedata
from difflib import SequenceMatcher
from typing import Any, Iterable, Mapping, Sequence

from sqlalchemy.orm import Session

from app.agents.contracts import CONTRACTS
from app.core.permission_codes import PermissionCode
from app.models.agent_capability import AgentCapabilityAlias, AgentMcpBinding, McpServer, McpTool
from app.models.user import User
from app.services import agent_studio_service, embedding_service, rbac_service
from app.services.admin_capability_registry import ADMIN_CAPABILITIES
from app.services.managed_mcp_adapter import is_live_managed_kind, managed_tool_permissions
from app.services.user_capability_registry import USER_CAPABILITIES

_SEPARATORS = re.compile(r"[^0-9a-z\u3400-\u9fff]+")


def normalize_text(value: Any) -> str:
    return _SEPARATORS.sub("", unicodedata.normalize("NFKC", str(value or "")).casefold())


def _ngrams(value: str, size: int = 2) -> set[str]:
    if not value:
        return set()
    if len(value) <= size:
        return {value}
    return {value[index:index + size] for index in range(len(value) - size + 1)}


def _allowed(db: Session, user: User, permission: str | None) -> bool:
    return not permission or rbac_service.check_permission(db, user.id, permission)


def _is_super_admin(db: Session, user: User) -> bool:
    return rbac_service.is_super_admin_user(db, user.id)


def _is_admin(db: Session, user: User) -> bool:
    return rbac_service.is_admin_user(db, user.id)


def _builtin_rows(db: Session, user: User) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if not _allowed(db, user, PermissionCode.AGENT_VIEW):
        return rows
    for contract in CONTRACTS.values():
        if contract.code == "operations" and not _is_super_admin(db, user):
            continue
        rows.append(
            {
                "code": f"agent:{contract.code}",
                "name": contract.name,
                "description": contract.mission,
                "source": "builtin_agent",
                "agent_code": contract.code,
                "requires_approval": contract.code == "operations",
            }
        )
        if not _allowed(db, user, PermissionCode.AGENT_CONFIGURE):
            continue
        for skill in contract.skills:
            rows.append(
                {
                    "code": f"skill:{contract.code}:{skill.code}",
                    "name": skill.name,
                    "description": f"{skill.purpose}；{skill.usage_rule}",
                    "source": "builtin_skill",
                    "agent_code": contract.code,
                    "requires_approval": False,
                }
            )
    return rows


def _user_capability_rows(db: Session, user: User) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for spec in USER_CAPABILITIES:
        if not _allowed(db, user, spec.permission):
            continue
        rows.append(
            {
                "code": f"user_api:{spec.code}",
                "name": spec.code,
                "description": spec.description,
                "source": "user_api",
                "agent_code": "chat_assistant",
                "requires_approval": spec.risk != "read",
            }
        )
    return rows


def _admin_capability_rows(db: Session, user: User) -> list[dict[str, Any]]:
    if not _is_admin(db, user):
        return []
    rows: list[dict[str, Any]] = []
    for spec in ADMIN_CAPABILITIES:
        if not _allowed(db, user, spec.permission):
            continue
        rows.append(
            {
                "code": f"admin_api:{spec.code}",
                "name": spec.code,
                "description": spec.description,
                "source": "admin_api",
                "agent_code": "manager",
                "requires_approval": spec.risk != "read",
            }
        )
    return rows


def _published_rows(db: Session, user: User) -> list[dict[str, Any]]:
    if not _allowed(db, user, PermissionCode.CUSTOM_AGENT_INVOKE):
        return []
    return [
        {
            "code": f"published_agent:{item['code']}",
            "name": item["name"],
            "description": item.get("description") or "",
            "source": "published_agent",
            "agent_code": item["code"],
            "requires_approval": False,
            "published_agent": item,
        }
        for item in agent_studio_service.list_catalog(db)
    ]


def _sandbox_rows(db: Session, user: User) -> list[dict[str, Any]]:
    if user.status != 1 or not _allowed(db, user, PermissionCode.PROJECT_VIEW):
        return []
    return [
        {
            "code": "sandbox:create_test",
            "name": "创建代码测试沙箱",
            "description": "对有权项目执行白盒、黑盒或组合测试",
            "source": "sandbox",
            "agent_code": "test_verifier",
            "requires_approval": False,
        },
        {
            "code": "sandbox:create_deploy",
            "name": "创建持续部署沙箱",
            "description": "在隔离节点启动项目并保留受控预览",
            "source": "sandbox",
            "agent_code": "sandbox_deployer",
            "requires_approval": True,
        },
        {
            "code": "sandbox:close",
            "name": "关闭沙箱",
            "description": "停止本人或有权项目的测试、部署环境",
            "source": "sandbox",
            "agent_code": "sandbox_deployer",
            "requires_approval": True,
        },
        {
            "code": "sandbox:extend",
            "name": "延长沙箱保留时间",
            "description": "在服务器约束的最长存活时间内为有权环境续期",
            "source": "sandbox",
            "agent_code": "sandbox_deployer",
            "requires_approval": True,
        },
    ]


def _mcp_rows(db: Session, user: User) -> list[dict[str, Any]]:
    # 外部 MCP 可能触及服务器，仅唯一 superadmin 可发现；内部 managed
    # 工具在应用权限边界内执行，按与 Responses 执行面相同的 RBAC 条件暴露。
    is_super_admin = _is_super_admin(db, user)
    rows = (
        db.query(AgentMcpBinding, McpTool, McpServer)
        .join(McpTool, McpTool.id == AgentMcpBinding.tool_id)
        .join(McpServer, McpServer.id == McpTool.server_id)
        .filter(
            AgentMcpBinding.enabled == 1,
            AgentMcpBinding.permission != "deny",
            AgentMcpBinding.bound_schema_sha256 == McpTool.schema_sha256,
            McpTool.enabled == 1,
            McpServer.enabled == 1,
            McpServer.status == "healthy",
        )
        .all()
    )
    result: list[dict[str, Any]] = []
    for binding, tool, server in rows:
        if server.transport == "managed":
            managed_kind = str(server.managed_kind or "")
            if not is_live_managed_kind(managed_kind):
                continue
            required_permissions = managed_tool_permissions(managed_kind, tool.tool_name)
            if not all(_allowed(db, user, permission) for permission in required_permissions):
                continue
        elif server.transport != "streamable_http" or not is_super_admin:
            continue
        result.append(
            {
                "code": f"mcp:{server.code}:{tool.tool_name}",
                "name": tool.display_name,
                "description": tool.description or "",
                "source": "mcp",
                "agent_code": binding.agent_code,
                "requires_approval": bool(
                    binding.requires_approval or binding.permission == "escalate"
                ),
            }
        )
    return result


def authorized_catalog(db: Session, user: User) -> list[dict[str, Any]]:
    """只生成当前用户已获授权的候选，后续搜索不得扩大集合。"""

    if user.status != 1:
        return []
    groups = (
        _builtin_rows(db, user),
        _user_capability_rows(db, user),
        _admin_capability_rows(db, user),
        _published_rows(db, user),
        _sandbox_rows(db, user),
        _mcp_rows(db, user),
    )
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in (item for group in groups for item in group):
        if row["code"] in seen:
            continue
        result.append(row)
        seen.add(row["code"])
    return result


def rank_rows(
    db: Session,
    rows: Sequence[Mapping[str, Any]],
    query: str,
    *,
    aliases_by_code: Mapping[str, Sequence[tuple[str, float]]] | None = None,
    limit: int = 10,
) -> list[dict[str, Any]]:
    """对已授权候选做别名、词法和 embedding 重排。"""

    aliases_by_code = aliases_by_code or {}
    query_normalized = normalize_text(query)
    prepared: list[dict[str, Any]] = []
    documents: list[str] = []
    lexical_scores: list[float] = []
    reasons_by_row: list[list[str]] = []
    for raw_row in rows:
        row = dict(raw_row)
        aliases = list(aliases_by_code.get(str(row.get("code") or ""), ()))
        fields = [row.get("code"), row.get("name"), row.get("description"), *(a for a, _ in aliases)]
        normalized_fields = [normalize_text(field) for field in fields if field]
        reasons: list[str] = []
        exact = 0.0
        contains = 0.0
        alias_exact = 0.0
        if query_normalized:
            if query_normalized in normalized_fields:
                exact = 1.0
                reasons.append("exact")
            if any(query_normalized in field or field in query_normalized for field in normalized_fields):
                contains = 0.92
                reasons.append("contains")
            alias_exact = max(
                (
                    min(1.0, float(weight))
                    for value, weight in aliases
                    if query_normalized == normalize_text(value)
                ),
                default=0.0,
            )
            if alias_exact:
                reasons.append("alias")
        sequence = max(
            (SequenceMatcher(None, query_normalized, field).ratio() for field in normalized_fields),
            default=0.0,
        )
        qgrams = _ngrams(query_normalized)
        gram = 0.0
        for field in normalized_fields:
            fgrams = _ngrams(field)
            union = qgrams | fgrams
            gram = max(gram, len(qgrams & fgrams) / len(union) if union else 0.0)
        lexical = min(1.0, max(exact, contains, sequence * 0.8, gram * 0.85, alias_exact))
        if not query_normalized:
            lexical = 1.0
            reasons = ["catalog"]
        elif not reasons and lexical:
            reasons = ["fuzzy"]
        row["aliases"] = [value for value, _ in aliases]
        prepared.append(row)
        lexical_scores.append(lexical)
        reasons_by_row.append(reasons)
        documents.append(" ".join(str(field) for field in fields if field))

    if not prepared:
        return []
    if query_normalized:
        vectors, _ = embedding_service.embed_texts(db, [query, *documents])
        query_vector = vectors[0] if vectors else []
        doc_vectors = vectors[1:] if len(vectors) > 1 else [[] for _ in documents]
    else:
        query_vector = []
        doc_vectors = [[] for _ in documents]
    ranked: list[dict[str, Any]] = []
    for row, lexical, vector, reasons in zip(
        prepared,
        lexical_scores,
        doc_vectors,
        reasons_by_row,
    ):
        semantic = max(0.0, embedding_service.cosine(query_vector, vector))
        score = lexical if lexical >= 0.9 else lexical * 0.7 + semantic * 0.3
        ranked.append({**row, "score": round(score, 4), "match_reasons": reasons})
    ranked.sort(key=lambda item: (-item["score"], str(item["code"])))
    threshold = 0.2 if query_normalized else 0.0
    return [item for item in ranked if item["score"] >= threshold][: max(1, min(limit, 20))]


def authorized_aliases(
    db: Session,
    capability_codes: Iterable[str],
) -> dict[str, list[tuple[str, float]]]:
    """只读取调用方已经完成授权过滤的能力编码别名。"""

    codes = list(dict.fromkeys(capability_codes))
    if not codes:
        return {}
    # 别名查询本身也仅限已授权编码，避免未授权元数据进入候选。
    persisted_codes = [code for code in codes if len(code) <= 255]
    rows = (
        db.query(AgentCapabilityAlias)
        .filter(
            AgentCapabilityAlias.enabled == 1,
            AgentCapabilityAlias.capability_code.in_(persisted_codes),
        )
        .all()
    ) if persisted_codes else []
    result: dict[str, list[tuple[str, float]]] = {}
    for row in rows:
        result.setdefault(row.capability_code, []).append((row.alias, float(row.weight)))
    return result


def search_capabilities(
    db: Session,
    user: User,
    query: str,
    limit: int = 5,
) -> list[dict[str, Any]]:
    rows = authorized_catalog(db, user)
    aliases = authorized_aliases(db, [row["code"] for row in rows])
    return rank_rows(db, rows, query, aliases_by_code=aliases, limit=limit)
