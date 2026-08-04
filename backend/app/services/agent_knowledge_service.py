"""Agent 知识库服务。"""
import ipaddress
import json
import re
from html import unescape
from typing import List, Optional
from urllib.parse import parse_qs, quote, urljoin, urlparse

import httpx
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.exceptions import NotFoundError, ValidationError
from app.models.agent_governance import AgentKnowledgeChunk, AgentKnowledgeDoc, AgentKnowledgeSource
from app.models.code_file import CodeFile
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


def list_docs(db: Session, agent_code: str = "", limit: int = 100) -> list[AgentKnowledgeDoc]:
    """查询 Agent 知识文档。

    Args:
        db: 数据库会话。
        agent_code: 可选 Agent 编码。
        limit: 最大返回条数。

    Returns:
        list[AgentKnowledgeDoc]: 知识文档列表。
    """
    q = db.query(AgentKnowledgeDoc).filter(AgentKnowledgeDoc.status != "deleted")
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
    status = "active" if risk_level in ("low", "medium") and confidence >= 0.6 else "pending_approval"
    doc = AgentKnowledgeDoc(
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
    db.commit()
    db.refresh(doc)

    pieces = chunk_text(content)
    if pieces:
        vectors, tag = embedding_service.embed_texts(db, pieces)
        for seq, piece in enumerate(pieces):
            vec = vectors[seq] if seq < len(vectors) else []
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
    doc = db.get(AgentKnowledgeDoc, doc_id)
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

    row = db.get(AgentKnowledgeSource, source_id) if source_id else None
    if not row:
        row = AgentKnowledgeSource(agent_code=agent_code, source_type=source_type, source_uri=source_uri)
        db.add(row)
    row.agent_code = agent_code
    row.source_type = source_type
    row.source_uri = source_uri
    row.whitelist = whitelist
    row.enabled = enabled
    row.config_json = json.dumps(config or {}, ensure_ascii=False)
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


def crawl_enabled_sources(db: Session, agent_code: str = "") -> dict:
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
        payload = _resolve_source_payload(db, source)
        if not payload:
            skipped.append({"source_id": source.id, "reason": "无可抓取内容"})
            continue
        for item in payload:
            if not str(item.get("content") or "").strip():
                skipped.append({"source_id": source.id, "reason": "抓取内容为空"})
                continue
            doc = add_document(
                db,
                agent_code=source.agent_code,
                title=item["title"],
                content=item["content"],
                source_type=source.source_type,
                source_ref=item["source_ref"],
                risk_level=item.get("risk_level", "low"),
                confidence=item.get("confidence", 0.9),
            )
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
    try:
        project_id = int(config.get("project_id") or source.source_uri)
    except (TypeError, ValueError):
        return []
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
        payload.append({
            "title": f"{project.project_name}/{file.file_path or file.file_name}",
            "content": file.content,
            "source_ref": f"code_file:{file.id}",
            "risk_level": str(config.get("risk_level") or "low"),
            "confidence": float(config.get("confidence") or 0.9),
        })
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
    """统一检索 Agent 操作知识库与用户个人知识库。

    Agent 操作手册与个人知识**分开各取 top_k 后合并**:语义检索里个人代码/历史
    chunk 常与"怎么用"类查询更相似,若混排打分会把操作手册挤出 top_k。分开配额
    保证操作手册必出现,个人知识作为补充。

    Returns:
        list[dict]: 检索命中结果(agent 手册在前,按各自相关度)。
    """
    # Agent 操作手册
    agent_hits: list[dict] = []
    qvec, _ = embedding_service.embed_one(db, query)
    if qvec:
        rows = (
            db.query(AgentKnowledgeChunk, AgentKnowledgeDoc.title, AgentKnowledgeDoc.source_type)
            .join(AgentKnowledgeDoc, AgentKnowledgeChunk.doc_id == AgentKnowledgeDoc.id)
            .filter(
                AgentKnowledgeChunk.agent_code == agent_code,
                AgentKnowledgeDoc.status == "active",
            )
            .all()
        )
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
        agent_hits.sort(key=lambda x: x["score"], reverse=True)

    # 个人知识(user_id 隔离),只对操作手册未覆盖的补充
    personal: list[dict] = []
    if user_id:
        personal = knowledge_service.retrieve(db, user_id, query, top_k)
        for item in personal:
            item["owner_type"] = "user"

    return agent_hits[:top_k] + personal[:top_k]
