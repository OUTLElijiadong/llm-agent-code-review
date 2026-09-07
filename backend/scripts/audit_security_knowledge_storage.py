"""只读检查内置安全文件是否另有数据库知识副本，不输出知识正文或用户内容。"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import func, or_, text

from app.core.database import SessionLocal
from app.models.agent_governance import AgentKnowledgeChunk, AgentKnowledgeDoc, AgentKnowledgeSource
from app.models.knowledge_chunk import KnowledgeChunk
from app.models.knowledge_doc import KnowledgeDoc


TERMS = ("known_cves", "audit_knowledge", "CVE-2021-21381", "OWASP Top10 2021", "OWASP Top 10 2021")


def _hash(value: str) -> str:
    return hashlib.sha256((value or "").encode()).hexdigest()


def inspect_storage(db) -> dict:
    result = {"read_only": True, "terms": list(TERMS), "tables": {}}
    for doc_model, chunk_model, scope_field in (
        (AgentKnowledgeDoc, AgentKnowledgeChunk, "agent_code"),
        (KnowledgeDoc, KnowledgeChunk, "user_id"),
    ):
        chunk_matches = or_(*(chunk_model.content.ilike(f"%{term}%") for term in TERMS))
        matched_doc_ids = {row[0] for row in db.query(chunk_model.doc_id).filter(chunk_matches).distinct().all()}
        meta_matches = or_(*(column.ilike(f"%{term}%") for term in TERMS for column in (doc_model.title, doc_model.source_ref)))
        docs = db.query(doc_model).filter(or_(doc_model.id.in_(matched_doc_ids), meta_matches)).order_by(doc_model.id).all()
        details = []
        for doc in docs:
            chunks = db.query(chunk_model).filter(chunk_model.doc_id == doc.id).order_by(chunk_model.seq).all()
            owned = doc.source_type in {"builtin", "playbook"} and (doc.source_ref or "").replace("\\", "/") in {
                "known_cves.md", "audit_knowledge/known_cves.md", "app/ai/audit_knowledge/known_cves.md",
            }
            details.append({
                "doc_id": doc.id, "source_type": doc.source_type, "status": doc.status,
                "scope": getattr(doc, scope_field), "source_ref_sha256": _hash(doc.source_ref),
                "title_sha256": _hash(doc.title), "declared_chunk_count": doc.chunk_count,
                "actual_chunk_count": len(chunks), "exact_builtin_source": owned,
                "chunk_content_sha256": [_hash(chunk.content) for chunk in chunks],
                "matched_terms": [term for term in TERMS if term.lower() in ((doc.title or "") + (doc.source_ref or "") + "".join(chunk.content or "" for chunk in chunks)).lower()],
            })
        result["tables"][doc_model.__tablename__] = {
            "total_docs": db.query(func.count(doc_model.id)).scalar(),
            "total_chunks": db.query(func.count(chunk_model.id)).scalar(), "matches": details,
        }
    sources = db.query(AgentKnowledgeSource).filter(or_(*(
        column.ilike(f"%{term}%") for term in TERMS
        for column in (AgentKnowledgeSource.source_uri, AgentKnowledgeSource.config_json)
    ))).order_by(AgentKnowledgeSource.id).all()
    result["tables"]["agent_knowledge_source"] = {
        "total_sources": db.query(func.count(AgentKnowledgeSource.id)).scalar(),
        "matches": [{"source_id": row.id, "agent_code": row.agent_code, "source_type": row.source_type,
                     "enabled": row.enabled, "source_uri_sha256": _hash(row.source_uri)} for row in sources],
    }
    return result


def main() -> None:
    with SessionLocal() as db:
        if db.bind.dialect.name == "mysql":
            db.execute(text("SET TRANSACTION READ ONLY"))
        result = inspect_storage(db)
        db.rollback()
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
