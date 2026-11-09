"""Agent 知识库服务。"""
import ipaddress
import json
import math
import re
from html import unescape
from typing import List, Optional
from urllib.parse import parse_qs, quote, urljoin, urlparse

import httpx
from sqlalchemy import Integer, and_, cast, exists, false, func, not_, or_, select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.exceptions import ForbiddenError, NotFoundError, ValidationError
from app.models.agent_governance import AgentKnowledgeChunk, AgentKnowledgeDoc, AgentKnowledgeSource
from app.models.agent_response_run import AgentResponseRun
from app.models.code_file import CodeFile
from app.models.enterprise_project import EnterpriseProject
from app.models.project import Project
from app.services import approval_service, embedding_service, knowledge_service
from app.utils.public_http import PinnedPublicUrl, pin_public_http_url

_CHUNK_SIZE = 700
_CHUNK_OVERLAP = 80
_DEFAULT_USER_AGENT = "Prism-Agent-Governance/1.0"
_BLOCKED_HOSTS = {"localhost", "localhost.localdomain"}
_BLOCKED_HOST_SUFFIXES = (".localhost", ".local", ".localdomain", ".internal", ".lan", ".home", ".corp")
_EXTERNAL_SOURCE_TYPES = {"url", "official", "docs", "github"}


def chunk_text(content: str) -> List[str]:
    """将文本切分为知识切片。

    Args:
        content: 原始文本。

    Returns:
        List[str]: 切片列表。
    """
    content = (content or "").strip()
    if not content:
        return []
    if len(content) <= _CHUNK_SIZE:
        return [content]
    chunks: list[str] = []
    start = 0
    while start < len(content):
        chunks.append(content[start:start + _CHUNK_SIZE])
        start += _CHUNK_SIZE - _CHUNK_OVERLAP
    return chunks


def _document_access_clause(user_id: Optional[int], blocked_project_source_ids: set[int] | None = None):
    """限制私人笔记，并从全局 Agent 检索中排除企业项目源码。"""
    source = AgentKnowledgeDoc.source_ref
    shared = or_(source.is_(None), ~source.startswith("response_run:", autoescape=True))
    # 旧版项目来源使用 ``code_file:<id>``，通过文件归属动态识别已绑定项目。
    # 新版引用同时固化 project_id，项目文件被删除后仍能按企业绑定过滤。
    legacy_project_prefix = func.substr(source, 1, 10) == "code_file:"
    project_prefix = func.substr(source, 1, 8) == "project:"
    legacy_enterprise_project_source = exists(
        select(CodeFile.id)
        .join(EnterpriseProject, EnterpriseProject.project_id == CodeFile.project_id)
        .where(
            AgentKnowledgeDoc.source_type == "project",
            legacy_project_prefix,
            cast(func.substr(source, 11), Integer) == CodeFile.id,
        )
    )
    bound_project_reference = exists(
        select(EnterpriseProject.id).where(
            AgentKnowledgeDoc.source_type == "project",
            and_(
                project_prefix,
                cast(func.substr(source, 9), Integer) == EnterpriseProject.project_id,
            ),
        )
    )
    bound_project_source = (
        AgentKnowledgeDoc.source_id.in_(blocked_project_source_ids)
        if blocked_project_source_ids
        else false()
    )
    shared = and_(shared, not_(or_(legacy_enterprise_project_source, bound_project_reference, bound_project_source)))
    if user_id is None:
        return shared
    owned = exists(select(AgentResponseRun.id).where(
        AgentResponseRun.user_id == int(user_id),
        source.startswith("response_run:", autoescape=True),
        func.substr(source, 14) == AgentResponseRun.run_id,
    ))
    return or_(shared, owned)


def _configured_project_id(source: AgentKnowledgeSource, config: dict | None = None) -> int | None:
    """Resolve the project id used by a project source without trusting its type."""
    config = config if isinstance(config, dict) else {}
    try:
        project_id = int(config.get("project_id") or source.source_uri)
    except (TypeError, ValueError):
        return None
    return project_id if project_id > 0 else None


def _project_id_from_source_ref(db: Session, source_ref: str) -> int | None:
    """Resolve current and historical project-source references to their project."""
    value = str(source_ref or "")
    if value.startswith("project:"):
        parts = value.split(":")
        if len(parts) not in {2, 4} or (len(parts) == 4 and parts[2] != "code_file"):
            return None
        if re.fullmatch(r"[1-9][0-9]*", parts[1]) is None:
            return None
        try:
            project_id = int(parts[1])
        except (IndexError, TypeError, ValueError):
            return None
        if project_id <= 0:
            return None
        if len(parts) == 4:
            if re.fullmatch(r"[1-9][0-9]*", parts[3]) is None:
                return None
            try:
                file_id = int(parts[3])
            except (TypeError, ValueError):
                return None
            actual_project_id = db.query(CodeFile.project_id).filter(CodeFile.id == file_id).scalar()
            if actual_project_id is None or int(actual_project_id) != project_id:
                return None
        return project_id
    if value.startswith("code_file:"):
        raw_file_id = value.partition(":")[2]
        if re.fullmatch(r"[1-9][0-9]*", raw_file_id) is None:
            return None
        try:
            file_id = int(raw_file_id)
        except (TypeError, ValueError):
            return None
        return db.query(CodeFile.project_id).filter(CodeFile.id == file_id).scalar()
    return None


def _is_enterprise_project(db: Session, project_id: int | None) -> bool:
    if project_id is None:
        return False
    return db.query(EnterpriseProject.id).filter_by(project_id=int(project_id)).first() is not None


def _bound_project_source_ids(db: Session) -> set[int]:
    """Find global project-source rows whose configured project is enterprise-bound."""
    bound_project_ids = {int(row[0]) for row in db.query(EnterpriseProject.project_id).all()}
    if not bound_project_ids:
        return set()
    restricted_ids = set()
    sources = db.query(AgentKnowledgeSource).filter_by(source_type="project").all()
    for source in sources:
        try:
            config = json.loads(source.config_json or "{}")
        except (TypeError, json.JSONDecodeError):
            config = {}
        project_id = _configured_project_id(source, config)
        if project_id in bound_project_ids:
            restricted_ids.add(int(source.id))
    return restricted_ids


def _deactivate_bound_project_documents(
    db: Session,
    project_id: int,
    *,
    source_id: int | None = None,
) -> int:
    """Compatibility wrapper that retires documents and commits the cleanup."""
    return retire_bound_project_sources(db, project_id, source_id=source_id, commit=True)


def retire_bound_project_sources(
    db: Session,
    project_id: int,
    *,
    source_id: int | None = None,
    commit: bool = False,
) -> int:
    """Disable project crawlers and remove all global Agent copies for a bound project.

    The caller may include this cleanup in the project-binding transaction. Project,
    code-file, source, then document rows are locked in that order, matching source
    ingestion so a binding cannot commit while an old source is still being copied.
    """
    project_id = int(project_id)
    db.query(Project.id).filter(Project.id == project_id).populate_existing().with_for_update().first()

    # Build legacy refs from numeric file IDs in Python. Concatenating a SQL literal
    # with a cast string is not collation neutral on MySQL 8 (project schemas may use
    # utf8mb4_unicode_ci while the server default is utf8mb4_0900_ai_ci).
    code_file_ids = [
        int(row[0])
        for row in (
            db.query(CodeFile.id)
            .filter(CodeFile.project_id == project_id)
            .order_by(CodeFile.id)
            .populate_existing()
            .with_for_update()
            .all()
        )
    ]

    sources = (
        db.query(AgentKnowledgeSource)
        .filter(AgentKnowledgeSource.source_type == "project")
        .populate_existing()
        .with_for_update()
        .all()
    )
    matching_source_ids: set[int] = set()
    for row in sources:
        try:
            config = json.loads(row.config_json or "{}")
        except (TypeError, json.JSONDecodeError):
            config = {}
        if _configured_project_id(row, config) == project_id:
            row.enabled = 0
            row.whitelist = 0
            matching_source_ids.add(int(row.id))
    if source_id is not None:
        matching_source_ids.add(int(source_id))

    source = AgentKnowledgeDoc.source_ref
    references = [source == f"project:{project_id}", source.like(f"project:{project_id}:%")]
    references_type = AgentKnowledgeDoc.source_type == "project"
    if matching_source_ids:
        references.append(AgentKnowledgeDoc.source_id.in_(matching_source_ids))
    document_ids = {
        int(row[0])
        for row in (
            db.query(AgentKnowledgeDoc.id)
            .filter(references_type, or_(*references))
            .populate_existing()
            .with_for_update()
            .all()
        )
    }
    # Historical project documents used the unqualified `code_file:<id>` form.
    # Bounded IN queries avoid dialect-specific collation or parameter-limit issues.
    for offset in range(0, len(code_file_ids), 500):
        refs = [f"code_file:{file_id}" for file_id in code_file_ids[offset:offset + 500]]
        document_ids.update(
            int(row[0])
            for row in (
                db.query(AgentKnowledgeDoc.id)
                .filter(AgentKnowledgeDoc.source_type == "project", AgentKnowledgeDoc.source_ref.in_(refs))
                .populate_existing()
                .with_for_update()
                .all()
            )
        )
    documents = (
        db.query(AgentKnowledgeDoc)
        .filter(AgentKnowledgeDoc.id.in_(document_ids))
        .order_by(AgentKnowledgeDoc.id)
        .populate_existing()
        .with_for_update()
        .all()
        if document_ids else []
    )
    document_ids = [int(doc.id) for doc in documents]
    if document_ids:
        db.query(AgentKnowledgeChunk).filter(
            AgentKnowledgeChunk.doc_id.in_(document_ids),
        ).delete(synchronize_session=False)
        for doc in documents:
            doc.status = "deleted"
    if commit:
        db.commit()
    return len(document_ids)


def _lock_unbound_project_source(
    db: Session,
    source_ref: str,
    project_id: int,
    *,
    source_id: int | None = None,
) -> None:
    """Serialize project knowledge ingestion with binding and fail closed on stale refs."""
    project_id = int(project_id)
    project = (
        db.query(Project.id)
        .filter(Project.id == project_id)
        .populate_existing()
        .with_for_update()
        .one_or_none()
    )
    if project is None:
        raise ForbiddenError("无法确认项目来源", code=40300)

    value = str(source_ref or "")
    file_id = None
    if value.startswith("code_file:"):
        try:
            file_id = int(value.partition(":")[2])
        except (TypeError, ValueError):
            file_id = None
    elif value.startswith("project:"):
        tail = value.split(":", 2)
        if len(tail) == 3 and tail[2].startswith("code_file:"):
            try:
                file_id = int(tail[2].partition(":")[2])
            except (TypeError, ValueError):
                file_id = None
    if value and (value.startswith("code_file:") or ":code_file:" in value):
        if file_id is None or file_id <= 0:
            raise ForbiddenError("无法确认项目来源", code=40300)
        current_project_id = (
            db.query(CodeFile.project_id)
            .filter(CodeFile.id == file_id)
            .populate_existing()
            .with_for_update()
            .scalar()
        )
        if current_project_id is None or int(current_project_id) != project_id:
            raise ForbiddenError("无法确认项目来源", code=40300)

    if source_id is not None:
        source_row = (
            db.query(AgentKnowledgeSource)
            .filter_by(id=int(source_id), source_type="project")
            .populate_existing()
            .with_for_update()
            .one_or_none()
        )
        if source_row is None:
            raise ForbiddenError("无法确认项目来源", code=40300)
        try:
            config = json.loads(source_row.config_json or "{}")
        except (TypeError, json.JSONDecodeError):
            config = {}
        configured_project_id = _configured_project_id(source_row, config)
        if configured_project_id != project_id:
            raise ForbiddenError("无法确认项目来源", code=40300)

    binding = (
        db.query(EnterpriseProject.id)
        .filter_by(project_id=project_id)
        .populate_existing()
        .with_for_update()
        .first()
    )
    if binding is not None:
        raise ForbiddenError("企业绑定项目不能写入全局 Agent 知识库", code=40300)


def list_docs(
    db: Session, agent_code: str = "", limit: int = 100, *, user_id: Optional[int] = None,
) -> list[AgentKnowledgeDoc]:
    """查询 Agent 知识文档。

    Args:
        db: 数据库会话。
        agent_code: 可选 Agent 编码。
        limit: 最大返回条数。

    Returns:
        list[AgentKnowledgeDoc]: 知识文档列表。
    """
    q = db.query(AgentKnowledgeDoc).filter(
        AgentKnowledgeDoc.status != "deleted",
        _document_access_clause(user_id, _bound_project_source_ids(db)),
    )
    if agent_code:
        q = q.filter(AgentKnowledgeDoc.agent_code == agent_code)
    return q.order_by(AgentKnowledgeDoc.id.desc()).limit(limit).all()


def add_document(
    db: Session,
    *,
    agent_code: str,
    title: str,
    content: str,
    source_type: str = "manual",
    source_ref: str = "",
    risk_level: str = "low",
    confidence: float = 1.0,
    user_id: Optional[int] = None,
    require_api_embedding: bool = False,
    source_id: int | None = None,
) -> AgentKnowledgeDoc:
    """新增 Agent 知识文档并生成切片向量。

    Args:
        db: 数据库会话。
        agent_code: Agent 编码。
        title: 文档标题。
        content: 文档正文。
        source_type: 来源类型。
        source_ref: 来源引用。
        risk_level: 风险等级。
        confidence: 置信度。

    Returns:
        AgentKnowledgeDoc: 新增文档。
    """
    if (source_ref or "").startswith("response_run:"):
        run = db.query(AgentResponseRun).filter(
            AgentResponseRun.run_id == source_ref[len("response_run:"):],
            AgentResponseRun.user_id == user_id,
        ).first() if user_id is not None else None
        if run is None:
            raise ForbiddenError("私人知识来源不属于当前账号", code=40300)
    if source_type == "project":
        project_id = _project_id_from_source_ref(db, source_ref)
        if not source_ref and source_id is not None:
            source_row = db.query(AgentKnowledgeSource).filter_by(
                id=int(source_id), source_type="project",
            ).first()
            if source_row is not None:
                try:
                    source_config = json.loads(source_row.config_json or "{}")
                except (TypeError, json.JSONDecodeError):
                    source_config = {}
                project_id = _configured_project_id(source_row, source_config)
        if project_id is None:
            raise ForbiddenError("无法确认项目来源", code=40300)
        _lock_unbound_project_source(db, source_ref, project_id, source_id=source_id)
    pieces = chunk_text(content)
    if pieces or require_api_embedding:
        # 先完成嵌入和完整性校验，失败时不创建空文档；手册额外要求真实远端向量。
        try:
            vectors, tag = embedding_service.embed_texts(db, pieces, user_id=user_id)
        except BaseException:
            db.rollback()
            raise
        if (
            not pieces or not isinstance(tag, str) or not tag
            or (require_api_embedding and not tag.startswith("api:"))
            or not isinstance(vectors, (list, tuple)) or len(vectors) != len(pieces)
            or not vectors or not isinstance(vectors[0], (list, tuple)) or not vectors[0]
            or any(
                not isinstance(vector, (list, tuple)) or len(vector) != len(vectors[0])
                or any(not isinstance(value, (int, float)) or isinstance(value, bool)
                       or not math.isfinite(value) for value in vector)
                for vector in vectors
            )
        ):
            if require_api_embedding:
                raise RuntimeError("操作手册真实嵌入不可用或不完整，保留原手册，请恢复嵌入服务后重试")
            raise RuntimeError("知识嵌入不可用或不完整，未创建文档，请恢复嵌入服务后重试")
    status = "active" if risk_level in ("low", "medium") and confidence >= 0.6 else "pending_approval"
    doc = AgentKnowledgeDoc(
        source_id=source_id,
        agent_code=agent_code,
        source_type=source_type,
        source_ref=source_ref or None,
        title=title[:240],
        risk_level=risk_level,
        confidence=confidence,
        status=status,
        char_count=len(content or ""),
        chunk_count=0,
    )
    db.add(doc)
    # 仅取得文档主键，文档与全部切片在下方同一事务提交。
    db.flush()

    if pieces:
        for seq, piece in enumerate(pieces):
            vec = vectors[seq]
            db.add(AgentKnowledgeChunk(
                doc_id=doc.id,
                agent_code=agent_code,
                seq=seq,
                content=piece,
                embedding=json.dumps(vec),
                embed_model=tag,
            ))
        doc.chunk_count = len(pieces)
    db.commit()
    db.refresh(doc)
    if doc.status == "pending_approval":
        approval_service.create_or_auto_decide(
            db,
            title=f"{agent_code} 知识入库待审批: {doc.title}",
            action="knowledge.activate",
            resource=f"agent_knowledge_doc:{doc.id}",
            risk_level=risk_level,
            decision="escalate",
            reason="知识风险较高或置信度不足，需要管理员审批后生效",
            agent_code=agent_code,
            request={"doc_id": doc.id, "title": doc.title, "source_ref": doc.source_ref},
        )
    return doc


def activate_document(
    db: Session,
    doc_id: int,
    *,
    commit: bool = True,
    user_id: Optional[int] = None,
) -> AgentKnowledgeDoc:
    """将待审批 Agent 知识文档激活。

    Args:
        db: 数据库会话。
        doc_id: 知识文档 ID。

    Returns:
        AgentKnowledgeDoc: 激活后的知识文档。

    Raises:
        NotFoundError: 知识文档不存在。
    """
    doc = db.query(AgentKnowledgeDoc).filter(
        AgentKnowledgeDoc.id == doc_id,
        AgentKnowledgeDoc.status != "deleted",
        _document_access_clause(user_id, _bound_project_source_ids(db)),
    ).first()
    if not doc:
        raise NotFoundError("Agent 知识文档不存在", code=40400)
    doc.status = "active"
    if commit:
        db.commit()
        db.refresh(doc)
    return doc


def upsert_source(
    db: Session,
    *,
    agent_code: str,
    source_type: str,
    source_uri: str,
    whitelist: int = 1,
    enabled: int = 1,
    config: Optional[dict] = None,
    source_id: Optional[int] = None,
) -> AgentKnowledgeSource:
    """创建或更新 Agent 知识来源。

    Args:
        db: 数据库会话。
        agent_code: Agent 编码。
        source_type: 来源类型。
        source_uri: 来源地址或引用。
        whitelist: 是否白名单。
        enabled: 是否启用。
        config: 来源配置。
        source_id: 可选来源 ID；存在时更新。

    Returns:
        AgentKnowledgeSource: 来源记录。
    """
    source_type = (source_type or "").strip().lower()
    source_uri = (source_uri or "").strip()
    if source_type in _EXTERNAL_SOURCE_TYPES and not _validate_knowledge_url(source_uri):
        raise ValidationError("外部知识来源仅允许可解析的公网 HTTP(S) URL", code=40026)
    normalized_config = config if isinstance(config, dict) else {}
    if source_type == "project" and enabled == 1 and whitelist == 1:
        candidate = AgentKnowledgeSource(source_uri=source_uri)
        project_id = _configured_project_id(candidate, normalized_config)
        if _is_enterprise_project(db, project_id):
            raise ForbiddenError("企业绑定项目不能配置为全局 Agent 知识来源", code=40300)

    row = db.get(AgentKnowledgeSource, source_id) if source_id else None
    if row is not None and row.source_type == "project":
        try:
            previous_config = json.loads(row.config_json or "{}")
        except (TypeError, json.JSONDecodeError):
            previous_config = {}
        previous_project_id = _configured_project_id(row, previous_config)
        if _is_enterprise_project(db, previous_project_id):
            # Preserve provenance if a bound source is later repointed and its files disappear.
            _deactivate_bound_project_documents(
                db,
                previous_project_id,
                source_id=int(row.id),
            )
    if not row:
        row = AgentKnowledgeSource(agent_code=agent_code, source_type=source_type, source_uri=source_uri)
        db.add(row)
    row.agent_code = agent_code
    row.source_type = source_type
    row.source_uri = source_uri
    row.whitelist = whitelist
    row.enabled = enabled
    row.config_json = json.dumps(normalized_config, ensure_ascii=False)
    db.commit()
    db.refresh(row)
    return row


def list_sources(db: Session, agent_code: str = "") -> list[AgentKnowledgeSource]:
    """查询 Agent 知识来源。

    Args:
        db: 数据库会话。
        agent_code: 可选 Agent 编码。

    Returns:
        list[AgentKnowledgeSource]: 来源列表。
    """
    q = db.query(AgentKnowledgeSource)
    if agent_code:
        q = q.filter(AgentKnowledgeSource.agent_code == agent_code)
    return q.order_by(AgentKnowledgeSource.id.desc()).all()


def crawl_enabled_sources(db: Session, agent_code: str = "", *, user_id: Optional[int] = None) -> dict:
    """抓取启用的 Agent 知识来源并沉淀文档。

    Args:
        db: 数据库会话。
        agent_code: 可选 Agent 编码。

    Returns:
        dict: 抓取结果摘要。
    """
    q = db.query(AgentKnowledgeSource).filter(
        AgentKnowledgeSource.enabled == 1,
        AgentKnowledgeSource.whitelist == 1,
    )
    if agent_code:
        q = q.filter(AgentKnowledgeSource.agent_code == agent_code)
    sources = q.order_by(AgentKnowledgeSource.id.asc()).all()
    docs: list[dict] = []
    skipped: list[dict] = []
    for source in sources:
        project_id: int | None = None
        if source.source_type == "project":
            try:
                source_config = json.loads(source.config_json or "{}")
            except (TypeError, json.JSONDecodeError):
                source_config = {}
            project_id = _configured_project_id(source, source_config)
            if _is_enterprise_project(db, project_id):
                _deactivate_bound_project_documents(db, project_id, source_id=int(source.id))
                skipped.append(
                    {
                        "source_id": source.id,
                        "reason": "企业绑定项目禁止进入全局 Agent 知识库",
                    }
                )
                continue
        payload = _resolve_source_payload(db, source)
        if not payload:
            skipped.append({"source_id": source.id, "reason": "无可抓取内容"})
            continue
        for item in payload:
            if not str(item.get("content") or "").strip():
                skipped.append({"source_id": source.id, "reason": "抓取内容为空"})
                continue
            try:
                doc = add_document(
                    db,
                    agent_code=source.agent_code,
                    user_id=user_id,
                    title=item["title"],
                    content=item["content"],
                    source_type=source.source_type,
                    source_ref=item["source_ref"],
                    source_id=int(source.id),
                    risk_level=item.get("risk_level", "low"),
                    confidence=item.get("confidence", 0.9),
                )
            except ForbiddenError:
                db.rollback()
                if project_id is not None and _is_enterprise_project(db, project_id):
                    _deactivate_bound_project_documents(db, project_id, source_id=int(source.id))
                skipped.append({
                    "source_id": source.id,
                    "reason": "项目来源已绑定企业或无法验证，跳过全局知识入库",
                })
                break
            docs.append({"doc_id": doc.id, "source_id": source.id, "status": doc.status})
    return {"source_count": len(sources), "doc_count": len(docs), "docs": docs, "skipped": skipped}


def _resolve_source_payload(db: Session, source: AgentKnowledgeSource) -> list[dict]:
    """解析单个知识来源为可入库内容。

    Args:
        db: 数据库会话。
        source: 知识来源。

    Returns:
        list[dict]: 文档 payload 列表。
    """
    if source.source_type in _EXTERNAL_SOURCE_TYPES and not _validate_knowledge_url(source.source_uri):
        return []

    config = {}
    if source.config_json:
        try:
            config = json.loads(source.config_json)
        except json.JSONDecodeError:
            config = {}
    if source.source_type in {"manual", "inline"}:
        content = str(config.get("content") or source.source_uri)
        return [{
            "title": str(config.get("title") or f"{source.agent_code} 手动知识源"),
            "content": content,
            "source_ref": f"source:{source.id}",
            "risk_level": str(config.get("risk_level") or "low"),
            "confidence": float(config.get("confidence") or 0.9),
        }]
    if source.source_type == "project":
        return _project_source_payload(db, source, config)
    if source.source_type in {"url", "official", "docs"}:
        return _url_source_payload(source, config)
    if source.source_type == "github":
        return _github_source_payload(source, config)
    return []


def _project_source_payload(db: Session, source: AgentKnowledgeSource, config: dict) -> list[dict]:
    """从项目代码文件生成 Agent 知识 payload。

    Args:
        db: 数据库会话。
        source: 知识来源。
        config: 来源配置。

    Returns:
        list[dict]: 项目代码知识 payload。
    """
    project_id = _configured_project_id(source, config)
    if project_id is None:
        return []
    if _is_enterprise_project(db, project_id):
        raise ForbiddenError("企业绑定项目不能采集到全局 Agent 知识库", code=40300)
    project = db.get(Project, project_id)
    if not project:
        return []
    limit = int(config.get("file_limit") or 20)
    files = (
        db.query(CodeFile)
        .filter(CodeFile.project_id == project_id, CodeFile.status == "active")
        .order_by(CodeFile.id.asc())
        .limit(max(1, min(limit, 100)))
        .all()
    )
    payload: list[dict] = []
    for file in files:
        payload.append(
            {
                "title": f"{project.project_name}/{file.file_path or file.file_name}",
                "content": file.content,
                "source_ref": f"code_file:{file.id}",
                "risk_level": str(config.get("risk_level") or "low"),
                "confidence": float(config.get("confidence") or 0.9),
            }
        )
    return payload


def _url_source_payload(source: AgentKnowledgeSource, config: dict) -> list[dict]:
    """从白名单 URL 或官方文档来源抓取文本 payload。

    Args:
        source: 知识来源。
        config: 来源配置。

    Returns:
        list[dict]: URL 文档 payload。
    """
    body = _fetch_text_url(source.source_uri)
    content = _html_to_text(body) if _looks_like_html(body) else body.strip()
    if not content:
        return []
    return [{
        "title": str(config.get("title") or _title_from_text(content) or source.source_uri),
        "content": content,
        "source_ref": source.source_uri,
        "risk_level": str(config.get("risk_level") or "medium"),
        "confidence": float(config.get("confidence") or 0.78),
    }]


def _github_source_payload(source: AgentKnowledgeSource, config: dict) -> list[dict]:
    """从 GitHub issue/PR 白名单来源抓取知识 payload。

    Args:
        source: 知识来源。
        config: 来源配置。

    Returns:
        list[dict]: GitHub issue/PR 文档 payload。
    """
    owner, repo, item_kind, item_number = _parse_github_source(source.source_uri, config)
    if not owner or not repo:
        content = _fetch_text_url(source.source_uri)
        if not content:
            return []
        return [{
            "title": str(config.get("title") or source.source_uri),
            "content": _html_to_text(content) if _looks_like_html(content) else content,
            "source_ref": source.source_uri,
            "risk_level": str(config.get("risk_level") or "medium"),
            "confidence": float(config.get("confidence") or 0.72),
        }]

    if item_kind in {"issue", "pull"} and item_number:
        return [_github_issue_payload(owner, repo, item_number, config, source.source_uri)]

    limit = max(1, min(int(config.get("limit") or 10), 50))
    state = str(config.get("state") or "open")
    api_url = (
        f"https://api.github.com/repos/{quote(owner, safe='')}/{quote(repo, safe='')}/issues"
        f"?state={quote(state, safe='')}&per_page={limit}"
    )
    rows = _fetch_json_url(api_url)
    payload: list[dict] = []
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        number = int(row.get("number") or 0)
        if number <= 0:
            continue
        title = str(row.get("title") or f"{owner}/{repo}#{number}")
        body = str(row.get("body") or "")
        html_url = str(row.get("html_url") or source.source_uri)
        kind = "PR" if row.get("pull_request") else "Issue"
        payload.append({
            "title": f"{owner}/{repo} {kind} #{number}: {title}",
            "content": _format_github_item(row, body),
            "source_ref": html_url,
            "risk_level": str(config.get("risk_level") or "medium"),
            "confidence": float(config.get("confidence") or 0.76),
        })
    return payload


def _github_issue_payload(owner: str, repo: str, number: int, config: dict, source_uri: str) -> dict:
    """抓取单个 GitHub issue 或 PR payload。

    Args:
        owner: 仓库 owner。
        repo: 仓库名。
        number: issue/PR 编号。
        config: 来源配置。
        source_uri: 原始来源 URI。

    Returns:
        dict: GitHub 文档 payload。
    """
    api_url = f"https://api.github.com/repos/{quote(owner, safe='')}/{quote(repo, safe='')}/issues/{number}"
    row = _fetch_json_url(api_url)
    if not isinstance(row, dict):
        return {
            "title": source_uri,
            "content": "",
            "source_ref": source_uri,
            "risk_level": str(config.get("risk_level") or "medium"),
            "confidence": 0.0,
        }
    title = str(row.get("title") or f"{owner}/{repo}#{number}")
    body = str(row.get("body") or "")
    kind = "PR" if row.get("pull_request") else "Issue"
    return {
        "title": f"{owner}/{repo} {kind} #{number}: {title}",
        "content": _format_github_item(row, body),
        "source_ref": str(row.get("html_url") or source_uri),
        "risk_level": str(config.get("risk_level") or "medium"),
        "confidence": float(config.get("confidence") or 0.78),
    }


def _fetch_text_url(url: str) -> str:
    """抓取 HTTP 文本并限制大小。

    Args:
        url: 抓取 URL。

    Returns:
        str: 解码后的文本；抓取失败返回空字符串。
    """
    safe_url = _validate_knowledge_url(url)
    if not safe_url:
        return ""
    try:
        resp = _safe_http_get(safe_url)
        content = resp.content[:settings.agent_knowledge_fetch_max_bytes]
        return content.decode(resp.encoding or "utf-8", errors="replace")
    except httpx.HTTPError:
        return ""


def _fetch_json_url(url: str):
    """抓取 JSON API 响应并限制大小。

    Args:
        url: JSON API URL。

    Returns:
        Any: JSON 数据；抓取失败返回 None。
    """
    safe_url = _validate_knowledge_url(url)
    if not safe_url:
        return None
    try:
        resp = _safe_http_get(safe_url)
        raw = resp.content[:settings.agent_knowledge_fetch_max_bytes]
        return json.loads(raw.decode(resp.encoding or "utf-8", errors="replace"))
    except (httpx.HTTPError, json.JSONDecodeError):
        return None


def _safe_http_get(url: str) -> httpx.Response:
    """执行受控 HTTP GET，并对每次跳转重新做 URL 安全校验。

    Args:
        url: 已校验的初始 URL。

    Returns:
        httpx.Response: 最终 HTTP 响应。

    Raises:
        httpx.HTTPError: 请求失败、跳转不安全或跳转次数过多。
    """
    target = _pin_knowledge_url(url)
    if not target:
        raise httpx.HTTPError("unsafe knowledge URL")
    for _ in range(4):
        # 每一跳独立连接池，避免不同域名共用同一 IP 时复用错误 TLS 会话。
        with httpx.Client(
            timeout=settings.agent_knowledge_fetch_timeout,
            follow_redirects=False,
            trust_env=False,
        ) as client:
            resp = client.get(
                target.request_url,
                headers={**_fetch_headers(target.sni_hostname), "Host": target.host_header},
                extensions=target.request_extensions,
            )
            if resp.status_code in {301, 302, 303, 307, 308}:
                location = resp.headers.get("location", "")
                next_target = _pin_knowledge_url(urljoin(target.original_url, location))
                if not next_target:
                    raise httpx.HTTPError("unsafe redirect")
                target = next_target
                continue
            resp.raise_for_status()
            return resp
    raise httpx.HTTPError("too many redirects")


def _validate_knowledge_url(url: str) -> str:
    """校验知识抓取 URL，仅允许可解析的公网地址。

    Args:
        url: 原始 URL。

    Returns:
        str: 规范化 URL。
    """
    target = _pin_knowledge_url(url)
    return target.original_url if target else ""


def _pin_knowledge_url(url: str) -> Optional[PinnedPublicUrl]:
    """校验并固定知识抓取的公网连接目标。"""
    value = (url or "").strip()
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None
    if parsed.username or parsed.password or parsed.fragment:
        return None
    try:
        parsed.port
    except ValueError:
        return None
    host = (parsed.hostname or "").strip().lower()
    if not host:
        return None
    if host in _BLOCKED_HOSTS or host.endswith(_BLOCKED_HOST_SUFFIXES) or "." not in host:
        return None
    try:
        if _is_blocked_ip(host):
            return None
    except ValueError:
        pass
    try:
        return pin_public_http_url(value)
    except ValidationError:
        return None


def _is_blocked_ip(ip_text: str) -> bool:
    """判断 IP 是否为默认禁止抓取的非公网范围。

    Args:
        ip_text: IPv4 或 IPv6 字符串。

    Returns:
        bool: True 表示应阻止。
    """
    ip = ipaddress.ip_address(ip_text)
    return not ip.is_global


def _fetch_headers(hostname: str = "") -> dict:
    """构造知识抓取请求头。

    Returns:
        dict: HTTP 请求头。
    """
    headers = {
        "Accept": "application/vnd.github+json, text/html, text/plain, application/json;q=0.9, */*;q=0.8",
        "User-Agent": _DEFAULT_USER_AGENT,
    }
    if hostname.lower() == "api.github.com" and settings.agent_knowledge_github_token:
        headers["Authorization"] = f"Bearer {settings.agent_knowledge_github_token}"
        headers["X-GitHub-Api-Version"] = "2022-11-28"
    return headers


def _parse_github_source(source_uri: str, config: dict) -> tuple[Optional[str], Optional[str], str, Optional[int]]:
    """解析 GitHub 知识来源。

    Args:
        source_uri: 来源 URI。
        config: 来源配置。

    Returns:
        tuple[Optional[str], Optional[str], str, Optional[int]]: owner、repo、类型和编号。
    """
    owner = str(config.get("owner") or "").strip()
    repo = str(config.get("repo") or "").strip()
    item_kind = str(config.get("kind") or "").strip()
    item_number = _to_optional_int(config.get("number"))
    if owner and repo:
        return owner, repo, item_kind, item_number

    parsed = urlparse(source_uri)
    if parsed.netloc.lower() not in {"github.com", "www.github.com"}:
        return None, None, "", None
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) < 2:
        return None, None, "", None
    owner, repo = parts[0], parts[1]
    if len(parts) >= 4 and parts[2] in {"issues", "pull"}:
        return owner, repo, "pull" if parts[2] == "pull" else "issue", _to_optional_int(parts[3])
    query = parse_qs(parsed.query)
    if "issues" in query:
        return owner, repo, "issue", _to_optional_int(query["issues"][0])
    return owner, repo, item_kind, item_number


def _to_optional_int(value) -> Optional[int]:
    """将值转换为可选整数。

    Args:
        value: 任意输入值。

    Returns:
        Optional[int]: 转换后的整数。
    """
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _format_github_item(row: dict, body: str) -> str:
    """格式化 GitHub issue/PR 文本。

    Args:
        row: GitHub API 响应项。
        body: issue/PR 正文。

    Returns:
        str: 可蒸馏文本。
    """
    labels = ", ".join(label.get("name", "") for label in row.get("labels", []) if isinstance(label, dict))
    parts = [
        f"Title: {row.get('title') or ''}",
        f"State: {row.get('state') or ''}",
        f"Author: {(row.get('user') or {}).get('login') if isinstance(row.get('user'), dict) else ''}",
        f"Labels: {labels}",
        "",
        body,
    ]
    return "\n".join(parts).strip()


def _looks_like_html(content: str) -> bool:
    """判断文本是否近似 HTML。

    Args:
        content: 文本内容。

    Returns:
        bool: 是否像 HTML。
    """
    sample = content[:500].lower()
    return "<html" in sample or "<body" in sample or "<!doctype html" in sample


def _html_to_text(content: str) -> str:
    """从 HTML 中提取可读文本。

    Args:
        content: HTML 文本。

    Returns:
        str: 清洗后的纯文本。
    """
    text = re.sub(r"(?is)<(script|style|noscript).*?>.*?</\1>", " ", content)
    text = re.sub(r"(?is)<br\s*/?>", "\n", text)
    text = re.sub(r"(?is)</(p|div|section|article|h[1-6]|li|tr)>", "\n", text)
    text = re.sub(r"(?is)<[^>]+>", " ", text)
    text = unescape(text)
    lines = [re.sub(r"\s+", " ", line).strip() for line in text.splitlines()]
    return "\n".join(line for line in lines if line)


def _title_from_text(content: str) -> str:
    """从文本首行推断标题。

    Args:
        content: 文本内容。

    Returns:
        str: 标题。
    """
    for line in content.splitlines():
        line = line.strip()
        if line:
            return line[:120]
    return ""


def unified_retrieve(db: Session, *, user_id: int, agent_code: str, query: str, top_k: int = 5) -> list[dict]:
    """统一检索用户知识库与 Agent 知识库。

    Args:
        db: 数据库会话。
        user_id: 用户 ID。
        agent_code: Agent 编码。
        query: 检索文本。
        top_k: 最大返回条数。

    Returns:
        list[dict]: 检索命中结果。
    """
    hits = knowledge_service.retrieve(db, user_id, query, top_k)
    for item in hits:
        item["owner_type"] = "user"

    qvec, _ = embedding_service.embed_one(db, query, user_id=user_id)
    rows = (
        db.query(AgentKnowledgeChunk, AgentKnowledgeDoc.title, AgentKnowledgeDoc.source_type)
        .join(AgentKnowledgeDoc, AgentKnowledgeChunk.doc_id == AgentKnowledgeDoc.id)
        .filter(
            AgentKnowledgeChunk.agent_code == agent_code,
            AgentKnowledgeDoc.status == "active",
            _document_access_clause(user_id, _bound_project_source_ids(db)),
        )
        .all()
    )
    agent_hits: list[dict] = []
    for chunk, title, source_type in rows:
        score = embedding_service.cosine(qvec, embedding_service.parse_vector(chunk.embedding))
        if score <= 0:
            continue
        agent_hits.append({
            "content": chunk.content,
            "score": round(score, 4),
            "doc_id": chunk.doc_id,
            "title": title,
            "source_type": source_type,
            "owner_type": "agent",
        })
    merged = hits + agent_hits
    merged.sort(key=lambda x: x.get("score", 0), reverse=True)
    return merged[:top_k]
