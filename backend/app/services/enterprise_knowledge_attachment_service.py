"""企业附件知识：隔离存储、受限抽取、本地向量检索和可撤权下载。"""

from __future__ import annotations

import hashlib
import json
import multiprocessing
import re
import zipfile
from io import BytesIO
from pathlib import PurePosixPath

from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.exceptions import ConflictError, NotFoundError, PayloadTooLargeError, ValidationError
from app.core.request_size_limit import ENTERPRISE_KNOWLEDGE_ATTACHMENT_MAX_BYTES
from app.models.enterprise_knowledge import EnterpriseKnowledgeAttachment, EnterpriseKnowledgeAttachmentChunk
from app.models.enterprise_org import Enterprise
from app.services import embedding_service
from app.services import enterprise_knowledge_service as entries
from app.services import enterprise_org_service as org

MAX_ATTACHMENT_BYTES = min(int(settings.max_upload_size), ENTERPRISE_KNOWLEDGE_ATTACHMENT_MAX_BYTES)
MAX_EXTRACTED_CHARS = 500_000
MAX_CHUNKS = 1_500
MAX_ENTERPRISE_ACTIVE_CHUNKS = 10_000
MAX_DOCX_ENTRIES = 512
MAX_DOCX_UNCOMPRESSED_BYTES = 24 * 1024 * 1024
MAX_PDF_PAGES = 120
MAX_PDF_PAGE_STREAM_BYTES = 512 * 1024
_ALLOWED_EXTENSIONS = {".txt", ".md", ".docx", ".pdf"}
_MIME_BY_EXTENSION = {
    ".txt": "text/plain",
    ".md": "text/markdown",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".pdf": "application/pdf",
}
_CHUNK_SIZE = 700
_CHUNK_OVERLAP = 80


def _require_current_member(db: Session, enterprise_id: int, actor_id: int) -> None:
    # A shared current read keeps attachment readers concurrent while ordering
    # them against company suspension/member-removal writers, which take the
    # same company row exclusively before changing membership.
    company = (db.query(Enterprise).filter_by(id=enterprise_id)
               .populate_existing().with_for_update(read=True).first())
    if company is None or company.status not in {"active", "suspended"}:
        raise NotFoundError("企业不存在")
    bind = db.get_bind()
    if bind.dialect.name == "mysql":
        # The request Session may already have an RR snapshot from authentication.
        # Read subscription/refund entitlement in a fresh, non-locking transaction;
        # taking refund/order locks here would invert the refund -> order -> company
        # writer lock order while this transaction already holds the company share lock.
        billing_bind = getattr(bind, "engine", bind)
        with Session(bind=billing_bind, autoflush=False) as billing_db:
            org.require_enterprise_member(
                db, enterprise_id, actor_id, lock=True, billing_db=billing_db,
            )
        return
    org.require_enterprise_member(db, enterprise_id, actor_id, lock=True)


def _safe_filename(filename: str) -> tuple[str, str]:
    normalized = str(filename or "").replace("\\", "/")
    name = PurePosixPath(normalized).name.strip()
    if not name or len(name) > 200 or any(ord(char) < 32 or ord(char) == 127 for char in name):
        raise ValidationError("附件文件名无效")
    extension = PurePosixPath(name).suffix.lower()
    if extension not in _ALLOWED_EXTENSIONS:
        raise ValidationError("企业知识附件仅支持 TXT、Markdown、DOCX 和可提取文字的 PDF")
    return name, extension


def _extract_text_file(raw: bytes) -> str:
    if b"\x00" in raw:
        raise ValidationError("文本附件包含二进制内容")
    try:
        text = raw.decode("utf-8-sig", errors="strict")
    except UnicodeDecodeError as exc:
        raise ValidationError("文本附件必须使用 UTF-8 编码") from exc
    try:
        return _validate_extracted_text(text)
    except ValueError as exc:
        if str(exc) == "extracted_text_too_large":
            raise ValidationError("附件提取文字超过 50 万字符上限") from exc
        raise ValidationError("文本附件没有可检索内容") from exc


def _set_parser_limits() -> None:
    """在独立解析进程限制 CPU/地址空间，拒绝解析器资源消耗攻击。"""
    try:
        import resource

        resource.setrlimit(resource.RLIMIT_CPU, (8, 9))
        address_limit = 768 * 1024 * 1024
        resource.setrlimit(resource.RLIMIT_AS, (address_limit, address_limit))
    except (ImportError, OSError, ValueError):
        # 生产容器为 Unix；不支持 resource 的平台仍受上传字节、页数和墙钟限制。
        return


def _validate_extracted_text(text: str) -> str:
    normalized = (text or "").replace("\x00", "").strip()
    if len(normalized) > MAX_EXTRACTED_CHARS:
        raise ValueError("extracted_text_too_large")
    if not normalized:
        raise ValueError("no_extractable_text")
    return normalized


def _docx_worker(raw: bytes, connection) -> None:
    try:
        _set_parser_limits()
        with zipfile.ZipFile(BytesIO(raw)) as archive:
            members = archive.infolist()
            if len(members) > MAX_DOCX_ENTRIES:
                raise ValueError("docx_too_many_parts")
            total_size = sum(item.file_size for item in members)
            if total_size > MAX_DOCX_UNCOMPRESSED_BYTES:
                raise ValueError("docx_expanded_size_too_large")
            if any(
                item.file_size > MAX_DOCX_UNCOMPRESSED_BYTES
                or (item.compress_size == 0 and item.file_size > 0)
                or (item.compress_size > 0 and item.file_size / item.compress_size > 150)
                or item.filename.startswith(("/", "\\"))
                or ".." in PurePosixPath(item.filename).parts
                for item in members
            ):
                raise ValueError("docx_part_invalid")
            names = {item.filename for item in members}
            if "[Content_Types].xml" not in names or "word/document.xml" not in names:
                raise ValueError("docx_structure_invalid")
            if any(name.lower().endswith("vbaproject.bin") for name in names):
                raise ValueError("docx_macro_not_supported")

        from docx import Document

        document = Document(BytesIO(raw))
        parts = [paragraph.text for paragraph in document.paragraphs if paragraph.text]
        for table in document.tables:
            for row in table.rows:
                parts.append(" | ".join(cell.text for cell in row.cells))
        connection.send(("ok", _validate_extracted_text("\n".join(parts))))
    except BaseException as exc:  # worker boundary: parent gets a safe error code only
        code = str(exc) if isinstance(exc, ValueError) else "docx_parse_failed"
        try:
            connection.send(("error", code[:80]))
        except (BrokenPipeError, EOFError, OSError):
            pass
    finally:
        connection.close()


def _pdf_worker(raw: bytes, connection) -> None:
    try:
        _set_parser_limits()
        from pypdf import PdfReader

        reader = PdfReader(BytesIO(raw), strict=True)
        if reader.is_encrypted:
            raise ValueError("pdf_encrypted")
        if len(reader.pages) > MAX_PDF_PAGES:
            raise ValueError("pdf_too_many_pages")
        pages: list[str] = []
        total_chars = 0
        for page in reader.pages:
            contents = page.get_contents()
            if contents is not None and len(contents.get_data()) > MAX_PDF_PAGE_STREAM_BYTES:
                raise ValueError("pdf_page_stream_too_large")
            text = page.extract_text() or ""
            total_chars += len(text)
            if total_chars > MAX_EXTRACTED_CHARS:
                raise ValueError("extracted_text_too_large")
            pages.append(text)
        connection.send(("ok", _validate_extracted_text("\n".join(pages))))
    except BaseException as exc:  # worker boundary: parent gets a safe error code only
        code = str(exc) if isinstance(exc, ValueError) else "pdf_parse_failed"
        try:
            connection.send(("error", code[:80]))
        except (BrokenPipeError, EOFError, OSError):
            pass
    finally:
        connection.close()


def _parse_in_limited_process(worker, raw: bytes, *, kind: str) -> str:
    context = multiprocessing.get_context("spawn")
    parent, child = context.Pipe(duplex=False)
    process = context.Process(target=worker, args=(raw, child), name=f"enterprise-{kind}-parser")
    try:
        try:
            process.start()
        except (OSError, RuntimeError) as exc:
            raise ValidationError("附件解析器暂不可用，请稍后重试") from exc
        child.close()
        if not parent.poll(12):
            raise ValidationError("附件解析超时，请拆分文档后重试")
        try:
            status, value = parent.recv()
        except (EOFError, OSError) as exc:
            raise ValidationError("附件解析进程异常退出，未保存附件") from exc
        if status != "ok":
            messages = {
                "no_extractable_text": "未提取到可检索文字；暂不支持扫描件 OCR",
                "extracted_text_too_large": "附件提取文字超过 50 万字符上限",
                "pdf_encrypted": "加密 PDF 暂不支持，请上传未加密版本",
                "pdf_too_many_pages": f"PDF 页数超过 {MAX_PDF_PAGES} 页上限",
                "pdf_page_stream_too_large": "PDF 页面内容过大，已拒绝解析",
                "docx_too_many_parts": "DOCX 内部文件数量过多，已拒绝解析",
                "docx_expanded_size_too_large": "DOCX 解压后超过 24 MB 上限",
                "docx_part_invalid": "DOCX 内部文件结构无效或压缩比异常",
                "docx_structure_invalid": "DOCX 文件结构无效",
                "docx_macro_not_supported": "带宏的 Word 文件不支持上传",
            }
            raise ValidationError(messages.get(value, "附件格式无效或解析失败，未保存附件"))
        return str(value)
    finally:
        parent.close()
        child.close()
        if process.pid is not None:
            process.join(timeout=0.5)
            if process.is_alive():
                process.terminate()
                process.join(timeout=1)


def _extract_text(raw: bytes, extension: str) -> str:
    if extension in {".txt", ".md"}:
        return _extract_text_file(raw)
    if extension == ".docx":
        return _parse_in_limited_process(_docx_worker, raw, kind="docx")
    if extension == ".pdf":
        if not raw.startswith(b"%PDF-"):
            raise ValidationError("PDF 文件头无效")
        return _parse_in_limited_process(_pdf_worker, raw, kind="pdf")
    raise ValidationError("附件格式不支持")


def _scan_upload(filename: str, raw: bytes) -> None:
    from app.services.code_file_service import _enforce_scan_result
    from app.utils.malware_scanner import get_scanner

    result = get_scanner().scan(raw, filename)
    try:
        _enforce_scan_result(result, filename)
    except ValueError as exc:
        raise ValidationError("附件未通过恶意内容扫描，已拒绝保存") from exc


def _chunks(text: str) -> list[str]:
    rows: list[str] = []
    start = 0
    while start < len(text) and len(rows) < MAX_CHUNKS:
        rows.append(text[start:start + _CHUNK_SIZE])
        start += _CHUNK_SIZE - _CHUNK_OVERLAP
    if start < len(text):
        raise ValidationError("附件切片数量超过上限，请缩小文档后重试")
    return rows


def create_attachment(
    db: Session,
    enterprise_id: int,
    actor_id: int,
    filename: str,
    raw: bytes,
    *,
    client_request_id: str,
) -> EnterpriseKnowledgeAttachment:
    name, extension = _safe_filename(filename)
    if not isinstance(raw, bytes) or not raw:
        raise ValidationError("附件不能为空")
    if len(raw) > MAX_ATTACHMENT_BYTES:
        raise PayloadTooLargeError(
            f"附件超过 {MAX_ATTACHMENT_BYTES // (1024 * 1024)} MB 上限",
            detail={"max_bytes": MAX_ATTACHMENT_BYTES},
        )
    if not isinstance(client_request_id, str) or not 1 <= len(client_request_id) <= 80:
        raise ValidationError("请求标识无效")

    digest = hashlib.sha256(raw).hexdigest()
    create_hash = hashlib.sha256(json.dumps([name, digest], ensure_ascii=False).encode("utf-8")).hexdigest()
    org.require_enterprise_admin(db, enterprise_id, actor_id)
    existing = db.query(EnterpriseKnowledgeAttachment).filter_by(
        enterprise_id=enterprise_id,
        author_id=actor_id,
        client_request_id=client_request_id,
    ).first()
    if existing is not None:
        # The initial lookup is only an optimization. An RR snapshot may have
        # outlived a removal/deletion, so reauthorize and perform a current row
        # read before returning an idempotent result.
        entries._admin(db, enterprise_id, actor_id)
        existing = (db.query(EnterpriseKnowledgeAttachment)
                    .filter_by(enterprise_id=enterprise_id, author_id=actor_id,
                               client_request_id=client_request_id)
                    .populate_existing().with_for_update().first())
        if existing is None:
            raise ConflictError("请求标识已用于其他内容或已删除的附件")
        if existing.create_hash != create_hash or existing.status != "active":
            raise ConflictError("请求标识已用于其他内容或已删除的附件")
        return existing

    _scan_upload(name, raw)
    extracted = _extract_text(raw, extension)
    pieces = _chunks(extracted)
    vectors, model_tag = embedding_service.embed_local_texts(pieces)
    # Parsing and malware scanning can take seconds. Re-acquire the company and
    # current admin membership only after the CPU-bound work, then serialize the
    # final idempotency check and insert with other enterprise writes.
    entries._admin(db, enterprise_id, actor_id)
    existing = (
        db.query(EnterpriseKnowledgeAttachment)
        .filter_by(
            enterprise_id=enterprise_id,
            author_id=actor_id,
            client_request_id=client_request_id,
        )
        .populate_existing()
        .with_for_update()
        .first()
    )
    if existing is not None:
        if existing.create_hash != create_hash or existing.status != "active":
            raise ConflictError("请求标识已用于其他内容或已删除的附件")
        return existing
    active_attachment_counts = (
        db.query(EnterpriseKnowledgeAttachment.chunk_count)
        .filter_by(enterprise_id=enterprise_id, status="active")
        .populate_existing()
        .with_for_update()
        .all()
    )
    active_chunk_count = sum(int(row.chunk_count or 0) for row in active_attachment_counts)
    if active_chunk_count + len(pieces) > MAX_ENTERPRISE_ACTIVE_CHUNKS:
        raise PayloadTooLargeError(
            f"企业知识附件检索库最多 {MAX_ENTERPRISE_ACTIVE_CHUNKS} 个有效片段；请先清理旧附件",
            detail={"max_active_chunks": MAX_ENTERPRISE_ACTIVE_CHUNKS},
        )
    attachment = EnterpriseKnowledgeAttachment(
        enterprise_id=enterprise_id,
        author_id=actor_id,
        updated_by=actor_id,
        client_request_id=client_request_id,
        create_hash=create_hash,
        file_name=name,
        file_ext=extension,
        mime=_MIME_BY_EXTENSION[extension],
        file_size=len(raw),
        chunk_count=len(pieces),
        source_sha256=digest,
        original_data=raw,
        extracted_text=extracted,
        version=1,
        status="active",
    )
    db.add(attachment)
    db.flush()
    db.add_all([
        EnterpriseKnowledgeAttachmentChunk(
            enterprise_id=enterprise_id,
            attachment_id=attachment.id,
            seq=index,
            content=piece,
            embedding=json.dumps(vector, separators=(",", ":")),
            embed_model=model_tag,
        )
        for index, (piece, vector) in enumerate(zip(pieces, vectors, strict=True))
    ])
    org._event(
        db, enterprise_id, actor_id, "knowledge_attachment_created",
        detail={"attachment_id": attachment.id, "sha256": digest},
    )
    db.flush()
    return attachment


def _active_attachment(db: Session, enterprise_id: int, attachment_id: int, *, lock: bool = False):
    query = db.query(EnterpriseKnowledgeAttachment).filter_by(
        id=attachment_id,
        enterprise_id=enterprise_id,
        status="active",
    )
    row = (query.populate_existing().with_for_update() if lock else query).first()
    if row is None:
        raise NotFoundError("企业知识附件不存在")
    return row


def list_attachments(db: Session, enterprise_id: int, actor_id: int, *, before_id=None, limit=30):
    # The authentication dependency may establish a MySQL RR snapshot before
    # this service runs; use a current read so a committed removal takes effect.
    _require_current_member(db, enterprise_id, actor_id)
    query = (db.query(EnterpriseKnowledgeAttachment)
             .populate_existing()
             .with_for_update()
             .filter_by(enterprise_id=enterprise_id, status="active"))
    if before_id is not None:
        query = query.filter(EnterpriseKnowledgeAttachment.id < before_id)
    return query.order_by(EnterpriseKnowledgeAttachment.id.desc()).limit(limit).all()


def get_attachment(db: Session, enterprise_id: int, actor_id: int, attachment_id: int):
    _require_current_member(db, enterprise_id, actor_id)
    return _active_attachment(db, enterprise_id, attachment_id, lock=True)


def delete_attachment(
    db: Session,
    enterprise_id: int,
    actor_id: int,
    attachment_id: int,
    *,
    expected_version: int,
) -> None:
    entries._admin(db, enterprise_id, actor_id)
    row = _active_attachment(db, enterprise_id, attachment_id, lock=True)
    if type(expected_version) is not int or expected_version != row.version:
        raise ConflictError("附件已被修改，请刷新后重新核对")
    db.query(EnterpriseKnowledgeAttachmentChunk).filter_by(
        enterprise_id=enterprise_id,
        attachment_id=attachment_id,
    ).delete(synchronize_session=False)
    row.status = "deleted"
    row.version += 1
    row.updated_by = actor_id
    row.original_data = None
    row.extracted_text = None
    row.chunk_count = 0
    org._event(db, enterprise_id, actor_id, "knowledge_attachment_deleted", detail={"attachment_id": row.id})
    db.flush()


def _query_terms(value: str) -> set[str]:
    normalized = value.casefold()
    terms = set(re.findall(r"[a-z0-9_]+", normalized))
    cjk = re.findall(r"[\u4e00-\u9fff]", normalized)
    terms.update(cjk)
    terms.update(cjk[index] + cjk[index + 1] for index in range(len(cjk) - 1))
    return {term for term in terms if term}


def search_attachments(db: Session, enterprise_id: int, actor_id: int, query: str, *, limit: int = 3) -> list[dict]:
    _require_current_member(db, enterprise_id, actor_id)
    query = (query or "").strip()
    if not query or len(query) > 100:
        raise ValidationError("搜索词需为 1 至 100 个字符")
    limit = max(1, min(int(limit), 10))
    query_vector, query_tag = embedding_service.embed_local_texts([query])
    terms = _query_terms(query)
    rows = (
        db.query(
            EnterpriseKnowledgeAttachmentChunk.content,
            EnterpriseKnowledgeAttachmentChunk.embedding,
            EnterpriseKnowledgeAttachmentChunk.embed_model,
            EnterpriseKnowledgeAttachment.id,
            EnterpriseKnowledgeAttachment.file_name,
            EnterpriseKnowledgeAttachment.version,
        )
        .join(
            EnterpriseKnowledgeAttachment,
            EnterpriseKnowledgeAttachment.id == EnterpriseKnowledgeAttachmentChunk.attachment_id,
        )
        .filter(
            EnterpriseKnowledgeAttachmentChunk.enterprise_id == enterprise_id,
            EnterpriseKnowledgeAttachment.enterprise_id == enterprise_id,
            EnterpriseKnowledgeAttachment.status == "active",
        )
        .order_by(EnterpriseKnowledgeAttachmentChunk.id.asc())
        # Current locking read prevents an RR snapshot created by auth middleware
        # from returning an attachment after its deletion transaction committed.
        .populate_existing()
        .with_for_update()
        .yield_per(500)
    )
    best: dict[int, dict] = {}
    query_items = query_vector[0]
    for content, raw_vector, embed_model, attachment_id, file_name, version in rows:
        candidate = embedding_service.parse_vector(raw_vector)
        similarity = embedding_service.cosine(query_items, candidate) if embed_model == query_tag else 0.0
        lowered = str(content or "").casefold()
        lexical = sum(1 for term in terms if term in lowered) / max(len(terms), 1)
        score = 0.75 * max(similarity, 0.0) + 0.25 * lexical
        previous = best.get(int(attachment_id))
        if score >= 0.08 and (previous is None or score > previous["score"]):
            best[int(attachment_id)] = {
                "attachment_id": int(attachment_id),
                "file_name": str(file_name),
                "version": int(version),
                "excerpt": str(content)[:1400],
                "score": score,
                "search_mode": "local_hash_vector_and_terms",
            }
    return sorted(best.values(), key=lambda item: (-item["score"], item["attachment_id"]))[:limit]
