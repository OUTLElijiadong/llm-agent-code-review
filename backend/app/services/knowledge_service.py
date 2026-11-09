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
import re
from typing import List, Optional

from loguru import logger
from sqlalchemy import Integer, cast, exists, func, not_, or_, select
from sqlalchemy.orm import Session

from app.core.exceptions import ForbiddenError, NotFoundError, PayloadTooLargeError
from app.core.pagination import Pagination
from app.models.knowledge_chunk import KnowledgeChunk
from app.models.knowledge_doc import KnowledgeDoc
from app.services import embedding_service

_CHUNK_SIZE = 700      # 目标切片字符数
_CHUNK_OVERLAP = 80    # 切片重叠,保留上下文
_MAX_DOCUMENT_CHARS = 2_000_000  # 与 DocAddIn 一致；超限明确拒绝，不静默丢尾
_EMBED_BATCH_SIZE = 32


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
    content = content or ""
    if len(content) > _MAX_DOCUMENT_CHARS:
        raise PayloadTooLargeError(f"知识文档最多 {_MAX_DOCUMENT_CHARS} 字，请拆分后入库")

    # Check before any external embedding request. On MySQL this lock remains
    # held until the document commit, serializing the write against project bind.
    if source_type in {"code", "issue"}:
        if not source_ref:
            raise ForbiddenError("项目知识来源缺少可验证的来源标识", code=40300)
        _lock_and_check_personal_project_source(db, source_type, source_ref, user_id)

    # 先完成全部切片的嵌入，确认数量和模型一致后才替换旧来源。
    # 远端嵌入可能独立记录用量；此时不持有知识文档写事务。
    pieces = _chunk_text(content)
    embedded: list[tuple[str, list[float], str]] = []
    model_tag = ""
    try:
        for offset in range(0, len(pieces), _EMBED_BATCH_SIZE):
            batch = pieces[offset:offset + _EMBED_BATCH_SIZE]
            vectors, tag = embedding_service.embed_texts(db, batch, user_id=user_id)
            if len(vectors) != len(batch) or not tag or (model_tag and tag != model_tag):
                raise RuntimeError("知识文档嵌入结果不完整或模型批次不一致，原文档保持不变")
            model_tag = tag
            embedded.extend((piece, vec, tag) for piece, vec in zip(batch, vectors))
    except BaseException:
        # The project row lock must not survive a failed upstream embedding.
        db.rollback()
        raise

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
    db.flush()
    for seq, (piece, vec, tag) in enumerate(embedded):
        db.add(KnowledgeChunk(
            doc_id=doc.id,
            user_id=user_id,
            seq=seq,
            content=piece,
            embedding=json.dumps(vec),
            embed_model=tag,
        ))
    doc.chunk_count = len(embedded)
    db.commit()
    db.refresh(doc)
    return doc


# ──────────────────────────────────────────────────────────
# 检索(RAG 核心)
# ──────────────────────────────────────────────────────────
# 低于此相似度的切片视为不相关，不再返回给页面或注入小菱上下文。
# 中文语义模型在无关段落之间也常有 0.5~0.6 的余弦，因此没有词面重合时要求更高的相似度；
# 哈希词袋降级向量的伪相似更明显，必须有词面重合。
_MIN_SEMANTIC_SCORE = 0.35
_STRONG_SEMANTIC_SCORE = 0.68
_MIN_SEMANTIC_OVERLAP = 0.2
_MIN_FALLBACK_SCORE = 0.25
_MIN_LEXICAL_OVERLAP = 0.34


def _query_terms(text: str) -> set[str]:
    """检索用词项：英文/数字词与中文二元组；查询只有单字时退化为单字。"""
    tokens = embedding_service._tokenize(text)
    terms = {tok for tok in tokens if len(tok) > 1}
    return terms or set(tokens)


def _lexical_overlap(query_terms: set[str], content: str) -> float:
    if not query_terms:
        return 0.0
    content_terms = set(embedding_service._tokenize(content))
    return len(query_terms & content_terms) / len(query_terms)


def personal_source_access_clause():
    """Hide old personal-KB copies whose source project now belongs to a company."""
    from app.models.code_file import CodeFile
    from app.models.enterprise_project import EnterpriseProject
    from app.models.review_issue import ReviewIssue
    from app.models.review_task import ReviewTask

    source = KnowledgeDoc.source_ref
    source_code_prefix = func.substr(source, 1, 5) == "file:"
    source_issue_prefix = func.substr(source, 1, 6) == "issue:"
    bound_code = exists(
        select(CodeFile.id)
        .join(EnterpriseProject, EnterpriseProject.project_id == CodeFile.project_id)
        .where(
            KnowledgeDoc.source_type == "code",
            source_code_prefix,
            cast(func.substr(source, 6), Integer) == CodeFile.id,
        )
    )
    bound_issue = exists(
        select(ReviewIssue.id)
        .join(ReviewTask, ReviewTask.id == ReviewIssue.task_id)
        .join(EnterpriseProject, EnterpriseProject.project_id == ReviewTask.project_id)
        .where(
            KnowledgeDoc.source_type == "issue",
            source_issue_prefix,
            cast(func.substr(source, 7), Integer) == ReviewIssue.id,
        )
    )
    return not_(or_(bound_code, bound_issue))


def _lock_and_check_personal_project_source(
    db: Session, source_type: str, source_ref: str, user_id: int,
) -> None:
    """Lock project before source rows, matching enterprise binding lock order."""
    from app.models.enterprise_project import EnterpriseProject
    from app.models.project import Project

    expected_prefix = {"code": "file", "issue": "issue"}.get(source_type)
    prefix, separator, raw_source_id = str(source_ref or "").partition(":")
    if (
        not expected_prefix or prefix != expected_prefix or separator != ":"
        or re.fullmatch(r"[1-9][0-9]*", raw_source_id) is None
    ):
        db.rollback()
        raise ForbiddenError("项目知识来源无法验证", code=40300)
    source_id = int(raw_source_id)
    if source_id <= 0:
        db.rollback()
        raise ForbiddenError("项目知识来源无法验证", code=40300)

    if source_type == "code":
        from app.models.code_file import CodeFile

        initial_project_id = db.query(CodeFile.project_id).filter(CodeFile.id == source_id).scalar()
    elif source_type == "issue":
        from app.models.review_issue import ReviewIssue
        from app.models.review_task import ReviewTask

        initial_task_id = db.query(ReviewIssue.task_id).filter(ReviewIssue.id == source_id).scalar()
        initial_project_id = (
            db.query(ReviewTask.project_id)
            .filter(ReviewTask.id == initial_task_id, ReviewTask.user_id == int(user_id))
            .scalar()
            if initial_task_id is not None else None
        )
    else:
        return

    if initial_project_id is None:
        db.rollback()
        raise ForbiddenError("项目知识来源已失效", code=40300)
    project_id = int(initial_project_id)
    project = (
        db.query(Project.id)
        .filter(Project.id == project_id, Project.user_id == int(user_id))
        .populate_existing()
        .with_for_update()
        .scalar()
    )
    if project is None:
        db.rollback()
        raise ForbiddenError("项目知识来源已失效", code=40300)

    if source_type == "code":
        current_source = (
            db.query(CodeFile.project_id)
            .filter(CodeFile.id == source_id)
            .populate_existing()
            .with_for_update()
            .scalar()
        )
        valid_source = current_source is not None and int(current_source) == project_id
    else:
        current_task = (
            db.query(ReviewTask)
            .filter(ReviewTask.id == initial_task_id, ReviewTask.user_id == int(user_id))
            .populate_existing()
            .with_for_update()
            .first()
        )
        current_issue = (
            db.query(ReviewIssue.task_id)
            .filter(ReviewIssue.id == source_id)
            .populate_existing()
            .with_for_update()
            .scalar()
        )
        valid_source = bool(
            current_task is not None
            and int(current_task.project_id) == project_id
            and current_issue is not None
            and int(current_issue) == int(initial_task_id)
        )
    if not valid_source:
        db.rollback()
        raise ForbiddenError("项目知识来源已失效", code=40300)

    bound = (
        db.query(EnterpriseProject.id)
        .filter_by(project_id=int(project_id))
        .populate_existing()
        .with_for_update()
        .first()
    )
    if bound is not None:
        # add_document owns its commit boundary; release the project lock before
        # the source-sync wrapper catches this expected rejection.
        db.rollback()
        raise ForbiddenError("企业项目来源不能复制到个人知识库", code=40300)


def retire_enterprise_project_sources(db: Session, project_id: int) -> tuple[int, int]:
    """Remove old personal-RAG copies when a project is bound to a company.

    The caller owns the transaction so the content is retired atomically with
    the new enterprise binding.
    """
    from app.models.code_file import CodeFile
    from app.models.review_issue import ReviewIssue
    from app.models.review_task import ReviewTask

    code_file_ids = [
        int(row[0])
        for row in db.query(CodeFile.id)
        .filter(CodeFile.project_id == int(project_id))
        .order_by(CodeFile.id)
        .populate_existing()
        .with_for_update()
        .all()
    ]
    issue_ids = [
        int(row[0])
        for row in db.query(ReviewIssue.id)
        .join(ReviewTask, ReviewTask.id == ReviewIssue.task_id)
        .filter(ReviewTask.project_id == int(project_id))
        .order_by(ReviewIssue.id)
        .populate_existing()
        .with_for_update()
        .all()
    ]
    # The binder may have waited for a personal sync that held the Project row
    # while it embedded and committed. Use a current locking read so this cleanup
    # sees that just-committed document even under MySQL REPEATABLE READ.
    doc_ids: set[int] = set()
    for source_type, prefix, source_ids in (
        ("code", "file:", code_file_ids),
        ("issue", "issue:", issue_ids),
    ):
        for offset in range(0, len(source_ids), 500):
            refs = [
                f"{prefix}{source_id}"
                for source_id in source_ids[offset:offset + 500]
            ]
            doc_ids.update(
                int(row[0])
                for row in db.query(KnowledgeDoc.id)
                .filter(
                    KnowledgeDoc.status == "active",
                    KnowledgeDoc.source_type == source_type,
                    KnowledgeDoc.source_ref.in_(refs),
                )
                .populate_existing()
                .with_for_update()
                .all()
            )
    doc_ids = sorted(doc_ids)
    if not doc_ids:
        return 0, 0
    deleted_chunks = db.query(KnowledgeChunk).filter(
        KnowledgeChunk.doc_id.in_(doc_ids),
    ).delete(synchronize_session=False)
    deleted_docs = db.query(KnowledgeDoc).filter(
        KnowledgeDoc.id.in_(doc_ids),
        KnowledgeDoc.status == "active",
    ).update({KnowledgeDoc.status: "deleted"}, synchronize_session=False)
    return int(deleted_docs or 0), int(deleted_chunks or 0)


def retrieve(db: Session, user_id: int, query: str, top_k: int = 5) -> List[dict]:
    """在用户私有知识库内检索最相关切片

    只比较同一嵌入模型产生的向量；模型不一致（历史切片、配置切换）或使用哈希降级时，
    按词面重合度判定相关性，避免把不相关文档以 60% 左右的“相似度”返回。

    Returns:
        list[dict]: { content, score, doc_id, title, source_type }
    """
    query = (query or "").strip()
    if not query:
        return []
    qvec, qtag = embedding_service.embed_one(db, query, user_id=user_id)
    terms = _query_terms(query)

    rows = (
        db.query(KnowledgeChunk, KnowledgeDoc.title, KnowledgeDoc.source_type)
        .join(KnowledgeDoc, KnowledgeChunk.doc_id == KnowledgeDoc.id)
        .filter(
            KnowledgeChunk.user_id == user_id,   # 切片与文档两侧均校验租户归属
            KnowledgeDoc.user_id == user_id,
            KnowledgeDoc.status == "active",
            personal_source_access_clause(),
        )
        .all()
    )
    scored = []
    for chunk, title, source_type in rows:
        overlap = _lexical_overlap(terms, f"{title or ''}\n{chunk.content or ''}")
        # 未记录模型的历史切片按维度比对（cosine 维度不一致时返回 -1）。
        same_model = bool(qvec) and (not chunk.embed_model or chunk.embed_model == qtag)
        if same_model and qtag != embedding_service.FALLBACK_TAG:
            score = embedding_service.cosine(qvec, embedding_service.parse_vector(chunk.embedding))
            if score < _MIN_SEMANTIC_SCORE or (
                score < _STRONG_SEMANTIC_SCORE and overlap < _MIN_SEMANTIC_OVERLAP
            ):
                continue
        elif same_model:
            score = embedding_service.cosine(qvec, embedding_service.parse_vector(chunk.embedding))
            if score < _MIN_FALLBACK_SCORE or overlap < _MIN_LEXICAL_OVERLAP:
                continue
        else:
            # 向量不可比：只按词面重合度返回，分数即重合比例。
            if overlap < _MIN_LEXICAL_OVERLAP:
                continue
            score = overlap
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
        personal_source_access_clause(),
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
        # R6/R7 修复:补齐 status/update_time,对齐 DocOut schema
        "status": d.status, "update_time": d.update_time,
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
        personal_source_access_clause(),
    ).all()
    by_source: dict = {}
    for d in docs:
        by_source[d.source_type] = by_source.get(d.source_type, 0) + 1
    chunk_total = (
        db.query(KnowledgeChunk)
        .join(KnowledgeDoc, KnowledgeDoc.id == KnowledgeChunk.doc_id)
        .filter(
            KnowledgeChunk.user_id == user_id,
            KnowledgeDoc.user_id == user_id,
            personal_source_access_clause(),
        )
        .count()
    )
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
    from app.models.enterprise_project import EnterpriseProject
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
                    Project.status != "deleted",
                    ~exists(select(EnterpriseProject.id).where(
                        EnterpriseProject.project_id == Project.id,
                    )))
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
                    ReviewIssue.status.in_(("fixed", "ignored")),
                    ~exists(select(EnterpriseProject.id).where(
                        EnterpriseProject.project_id == ReviewTask.project_id,
                    )))
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
