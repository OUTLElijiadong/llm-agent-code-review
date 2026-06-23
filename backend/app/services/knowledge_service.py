"""
个人知识库服务(RAG)

职责:
- 入库: 文本切片 → 嵌入 → 写入 knowledge_doc / knowledge_chunk
- 检索: 取该用户全部切片,Python 内余弦相似度,返回 top_k(严格 user_id 隔离)
- 同步: 从平台自有数据(项目代码/已处理问题/论坛/反馈/工单)聚合语料入库
- 管理: 列表 / 删除 / 统计

隔离红线: 任何查询都强制带 user_id 过滤;即使管理员也不得读取他人切片内容。
"""
import json
from typing import List, Optional

from loguru import logger
from sqlalchemy.orm import Session

from app.core.exceptions import ForbiddenError, NotFoundError
from app.core.pagination import Pagination
from app.models.knowledge_chunk import KnowledgeChunk
from app.models.knowledge_doc import KnowledgeDoc
from app.services import embedding_service

_CHUNK_SIZE = 700      # 目标切片字符数
_CHUNK_OVERLAP = 80    # 切片重叠,保留上下文
_MAX_SYNC_CHARS = 20000  # 单条来源最大入库字符,防超大文件拖垮


# ──────────────────────────────────────────────────────────
# 切片
# ──────────────────────────────────────────────────────────
def _chunk_text(content: str) -> List[str]:
    content = (content or "").strip()
    if not content:
        return []
    if len(content) <= _CHUNK_SIZE:
        return [content]

    # 优先按段落聚合,超长再按字符窗口切
    paras = [p.strip() for p in content.split("\n\n") if p.strip()]
    chunks: List[str] = []
    buf = ""
    for p in paras:
        if len(buf) + len(p) + 2 <= _CHUNK_SIZE:
            buf = f"{buf}\n\n{p}" if buf else p
        else:
            if buf:
                chunks.append(buf)
            if len(p) <= _CHUNK_SIZE:
                buf = p
            else:
                # 段落本身超长 → 字符窗口切片(带重叠)
                start = 0
                while start < len(p):
                    chunks.append(p[start:start + _CHUNK_SIZE])
                    start += _CHUNK_SIZE - _CHUNK_OVERLAP
                buf = ""
    if buf:
        chunks.append(buf)
    return chunks


# ──────────────────────────────────────────────────────────
# 入库
# ──────────────────────────────────────────────────────────
def add_document(
    db: Session,
    user_id: int,
    title: str,
    content: str,
    source_type: str = "upload",
    source_ref: Optional[str] = None,
    replace_existing: bool = True,
) -> KnowledgeDoc:
    """把一段文本作为一篇文档纳入用户知识库(切片+嵌入)

    若 source_ref 已存在且 replace_existing,则先软删旧文档与其切片再重建,
    实现幂等同步。
    """
    content = (content or "")[:_MAX_SYNC_CHARS]
    if source_ref and replace_existing:
        olds = db.query(KnowledgeDoc).filter(
            KnowledgeDoc.user_id == user_id,
            KnowledgeDoc.source_ref == source_ref,
            KnowledgeDoc.status == "active",
        ).all()
        for old in olds:
            db.query(KnowledgeChunk).filter(KnowledgeChunk.doc_id == old.id).delete()
            old.status = "deleted"

    doc = KnowledgeDoc(
        user_id=user_id,
        source_type=source_type,
        source_ref=source_ref,
        title=title[:200] or "未命名文档",
        char_count=len(content),
        chunk_count=0,
        status="active",
    )
    db.add(doc)
    db.commit()
    db.refresh(doc)

    pieces = _chunk_text(content)
    if pieces:
        vectors, tag = embedding_service.embed_texts(db, pieces)
        for seq, (piece, vec) in enumerate(zip(pieces, vectors)):
            db.add(KnowledgeChunk(
                doc_id=doc.id,
                user_id=user_id,
                seq=seq,
                content=piece,
                embedding=json.dumps(vec),
                embed_model=tag,
            ))
        doc.chunk_count = len(pieces)
    db.commit()
    db.refresh(doc)
    return doc


# ──────────────────────────────────────────────────────────
# 检索(RAG 核心)
# ──────────────────────────────────────────────────────────
def retrieve(db: Session, user_id: int, query: str, top_k: int = 5) -> List[dict]:
    """在用户私有知识库内检索最相关切片

    Returns:
        list[dict]: { content, score, doc_id, title, source_type }
    """
    query = (query or "").strip()
    if not query:
        return []
    qvec, _ = embedding_service.embed_one(db, query)
    if not qvec:
        return []

    rows = (
        db.query(KnowledgeChunk, KnowledgeDoc.title, KnowledgeDoc.source_type)
        .join(KnowledgeDoc, KnowledgeChunk.doc_id == KnowledgeDoc.id)
        .filter(
            KnowledgeChunk.user_id == user_id,   # 隔离红线
            KnowledgeDoc.status == "active",
        )
        .all()
    )
    scored = []
    for chunk, title, source_type in rows:
        vec = embedding_service.parse_vector(chunk.embedding)
        score = embedding_service.cosine(qvec, vec)
        if score <= 0:
            continue
        scored.append({
            "content": chunk.content,
            "score": round(score, 4),
            "doc_id": chunk.doc_id,
            "title": title,
            "source_type": source_type,
        })
    scored.sort(key=lambda x: x["score"], reverse=True)
    return scored[:top_k]


# ──────────────────────────────────────────────────────────
# 管理
# ──────────────────────────────────────────────────────────
def list_docs(db: Session, user_id: int, source_type: str = "",
              page: int = 1, page_size: int = 20) -> dict:
    q = db.query(KnowledgeDoc).filter(
        KnowledgeDoc.user_id == user_id,
        KnowledgeDoc.status == "active",
    )
    if source_type:
        q = q.filter(KnowledgeDoc.source_type == source_type)
    total = q.count()
    pg = Pagination(page, page_size, total)
    rows = q.order_by(KnowledgeDoc.create_time.desc()).offset(pg.offset).limit(pg.page_size).all()
    items = [{
        "id": d.id, "title": d.title, "source_type": d.source_type,
        "source_ref": d.source_ref, "char_count": d.char_count,
        "chunk_count": d.chunk_count, "create_time": d.create_time,
    } for d in rows]
    return pg.to_dict(items)


def delete_doc(db: Session, user_id: int, doc_id: int) -> None:
    doc = db.get(KnowledgeDoc, doc_id)
    if not doc or doc.status == "deleted":
        raise NotFoundError("文档不存在", code=40400)
    if doc.user_id != user_id:                   # 隔离红线:连管理员也不放行
        raise ForbiddenError("无权操作他人知识库", code=40300)
    db.query(KnowledgeChunk).filter(KnowledgeChunk.doc_id == doc.id).delete()
    doc.status = "deleted"
    db.commit()


def get_stats(db: Session, user_id: int) -> dict:
    docs = db.query(KnowledgeDoc).filter(
        KnowledgeDoc.user_id == user_id, KnowledgeDoc.status == "active",
    ).all()
    by_source: dict = {}
    for d in docs:
        by_source[d.source_type] = by_source.get(d.source_type, 0) + 1
    chunk_total = db.query(KnowledgeChunk).filter(KnowledgeChunk.user_id == user_id).count()
    return {
        "doc_total": len(docs),
        "chunk_total": chunk_total,
        "by_source": by_source,
        "remote_embedding": embedding_service.is_remote_enabled(db),
    }


# ──────────────────────────────────────────────────────────
# 从平台数据同步(项目代码/已处理问题/论坛/反馈/工单)
# ──────────────────────────────────────────────────────────
def sync_from_platform(db: Session, user_id: int) -> dict:
    """把用户的平台数据聚合进个人知识库(幂等)

    Returns:
        dict: 各来源新增/更新条数
    """
    from app.models.code_file import CodeFile
    from app.models.forum_post import ForumPost
    from app.models.maintenance_ticket import MaintenanceTicket
    from app.models.project import Project
    from app.models.review_issue import ReviewIssue
    from app.models.review_task import ReviewTask
    from app.models.user_feedback import UserFeedback

    counts = {"code": 0, "issue": 0, "forum": 0, "feedback": 0, "ticket": 0}

    def _safe(fn, label):
        try:
            return fn()
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[knowledge.sync] {label} 同步失败,跳过: {e}")
            return 0

    # 1) 项目代码文件
    def _sync_code():
        n = 0
        files = (
            db.query(CodeFile)
            .join(Project, CodeFile.project_id == Project.id)
            .filter(Project.user_id == user_id, CodeFile.status == "active",
                    Project.status != "deleted")
            .limit(200)
            .all()
        )
        for f in files:
            title = f.file_path or f.file_name
            add_document(db, user_id, f"代码: {title}", f.content or "",
                         source_type="code", source_ref=f"file:{f.id}")
            n += 1
        return n
    counts["code"] = _safe(_sync_code, "code")

    # 2) 已处理的审查问题(沉淀个人经验)
    def _sync_issues():
        n = 0
        rows = (
            db.query(ReviewIssue)
            .join(ReviewTask, ReviewIssue.task_id == ReviewTask.id)
            .filter(ReviewTask.user_id == user_id,
                    ReviewIssue.status.in_(("fixed", "ignored")))
            .order_by(ReviewIssue.handled_at.desc())
            .limit(300)
            .all()
        )
        for it in rows:
            body = (
                f"问题类型: {it.issue_type}\n严重度: {it.severity}\n"
                f"标题: {it.title or ''}\n描述: {it.description or ''}\n"
                f"建议: {it.suggestion or ''}\n处理结果: {it.status}"
            )
            add_document(db, user_id, f"审查问题#{it.id}: {it.title or it.issue_type}",
                         body, source_type="issue", source_ref=f"issue:{it.id}")
            n += 1
        return n
    counts["issue"] = _safe(_sync_issues, "issue")

    # 3) 论坛发帖
    def _sync_forum():
        n = 0
        posts = db.query(ForumPost).filter(
            ForumPost.user_id == user_id, ForumPost.status == "normal",
        ).limit(200).all()
        for p in posts:
            add_document(db, user_id, f"论坛: {p.title}",
                         f"{p.title}\n\n{p.content}",
                         source_type="forum", source_ref=f"post:{p.id}")
            n += 1
        return n
    counts["forum"] = _safe(_sync_forum, "forum")

    # 4) 反馈
    def _sync_feedback():
        n = 0
        rows = db.query(UserFeedback).filter(UserFeedback.user_id == user_id).limit(200).all()
        for fb in rows:
            body = f"反馈类型: {fb.feedback_type}\n内容: {fb.content}"
            if fb.admin_reply:
                body += f"\n管理员回复: {fb.admin_reply}"
            add_document(db, user_id, f"反馈#{fb.id}", body,
                         source_type="feedback", source_ref=f"feedback:{fb.id}")
            n += 1
        return n
    counts["feedback"] = _safe(_sync_feedback, "feedback")

    # 5) 维修工单
    def _sync_tickets():
        n = 0
        rows = db.query(MaintenanceTicket).filter(
            MaintenanceTicket.user_id == user_id,
        ).limit(200).all()
        for t in rows:
            body = (f"工单: {t.title}\n分类: {t.category}\n描述: {t.description}\n"
                    f"状态: {t.status}")
            if t.admin_reply:
                body += f"\n处理: {t.admin_reply}"
            add_document(db, user_id, f"工单#{t.id}: {t.title}", body,
                         source_type="ticket", source_ref=f"ticket:{t.id}")
            n += 1
        return n
    counts["ticket"] = _safe(_sync_tickets, "ticket")

    counts["total"] = sum(counts.values())
    return counts
