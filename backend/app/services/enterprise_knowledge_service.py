"""企业文本知识条目：字面检索仅在所属企业中进行，不调用外部模型。"""

import hashlib
import json

from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.core.exceptions import ConflictError, NotFoundError, ValidationError
from app.models.enterprise_knowledge import EnterpriseKnowledgeEntry
from app.services import enterprise_org_service as org

MAX_CONTENT = 50000


def _validate(title: str, content: str) -> tuple[str, str]:
    title, content = (title or "").strip(), (content or "").strip()
    if not title or len(title) > 200 or not content or len(content) > MAX_CONTENT:
        raise ValidationError("知识标题或正文无效")
    return title, content


def _admin(db, enterprise_id, actor_id):
    # 与成员变更使用相同企业行锁顺序，避免撤权和管理写入相互穿越。
    org._company(db, enterprise_id, lock=True)
    org.require_enterprise_admin(db, enterprise_id, actor_id, lock=True)


def _entry(db, enterprise_id, entry_id, *, lock=False):
    q = db.query(EnterpriseKnowledgeEntry).filter_by(id=entry_id, enterprise_id=enterprise_id, status="active")
    row = (q.populate_existing().with_for_update() if lock else q).first()
    if row is None:
        raise NotFoundError("企业知识条目不存在")
    return row


def create_entry(db: Session, enterprise_id: int, actor_id: int, title: str, content: str, *, client_request_id: str):
    _admin(db, enterprise_id, actor_id)
    title, content = _validate(title, content)
    if not isinstance(client_request_id, str) or not 1 <= len(client_request_id) <= 80:
        raise ValidationError("请求标识无效")
    digest = hashlib.sha256(json.dumps([title, content], ensure_ascii=False).encode("utf-8")).hexdigest()
    existing = (
        db.query(EnterpriseKnowledgeEntry)
        .filter_by(enterprise_id=enterprise_id, author_id=actor_id, client_request_id=client_request_id)
        .first()
    )
    if existing:
        if existing.create_hash != digest or existing.status != "active":
            raise ConflictError("请求标识已用于其他内容或已删除的条目")
        return existing
    row = EnterpriseKnowledgeEntry(
        enterprise_id=enterprise_id,
        author_id=actor_id,
        updated_by=actor_id,
        client_request_id=client_request_id,
        create_hash=digest,
        title=title,
        content=content,
        status="active",
    )
    db.add(row)
    db.flush()
    org._event(db, enterprise_id, actor_id, "knowledge_created", detail={"entry_id": row.id})
    return row


def update_entry(db: Session, enterprise_id: int, actor_id: int, entry_id: int, title: str, content: str,
                 *, expected_version: int):
    _admin(db, enterprise_id, actor_id)
    row = _entry(db, enterprise_id, entry_id, lock=True)
    if type(expected_version) is not int or expected_version != row.version:
        raise ConflictError("知识条目已被修改，请刷新后重新核对")
    row.title, row.content = _validate(title, content)
    row.version += 1
    row.updated_by = actor_id
    org._event(db, enterprise_id, actor_id, "knowledge_updated", detail={"entry_id": row.id})
    db.flush()
    return row


def delete_entry(db: Session, enterprise_id: int, actor_id: int, entry_id: int, *, expected_version: int):
    _admin(db, enterprise_id, actor_id)
    row = _entry(db, enterprise_id, entry_id, lock=True)
    if type(expected_version) is not int or expected_version != row.version:
        raise ConflictError("知识条目已被修改，请刷新后重新核对")
    row.status = "deleted"
    row.version += 1
    row.updated_by = actor_id
    org._event(db, enterprise_id, actor_id, "knowledge_deleted", detail={"entry_id": row.id})
    db.flush()


def get_entry(db: Session, enterprise_id: int, actor_id: int, entry_id: int):
    org.require_enterprise_member(db, enterprise_id, actor_id)
    return _entry(db, enterprise_id, entry_id)


def list_entries(db: Session, enterprise_id: int, actor_id: int, *, before_id=None, limit=30):
    org.require_enterprise_member(db, enterprise_id, actor_id)
    q = db.query(EnterpriseKnowledgeEntry).filter_by(enterprise_id=enterprise_id, status="active")
    if before_id is not None:
        q = q.filter(EnterpriseKnowledgeEntry.id < before_id)
    return q.order_by(EnterpriseKnowledgeEntry.id.desc()).limit(limit).all()


def search_entries(db: Session, enterprise_id: int, actor_id: int, query: str, *, before_id=None, limit=30):
    org.require_enterprise_member(db, enterprise_id, actor_id)
    query = (query or "").strip()
    if not query or len(query) > 100:
        raise ValidationError("搜索词需为 1 至 100 个字符")
    q = db.query(EnterpriseKnowledgeEntry).filter(
        EnterpriseKnowledgeEntry.enterprise_id == enterprise_id,
        EnterpriseKnowledgeEntry.status == "active",
        or_(
            EnterpriseKnowledgeEntry.title.contains(query, autoescape=True),
            EnterpriseKnowledgeEntry.content.contains(query, autoescape=True),
        ),
    )
    if before_id is not None:
        q = q.filter(EnterpriseKnowledgeEntry.id < before_id)
    return q.order_by(EnterpriseKnowledgeEntry.id.desc()).limit(limit).all()
