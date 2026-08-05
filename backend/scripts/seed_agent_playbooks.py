"""把操作知识库种入对应 Agent 的 AgentKnowledge(幂等,可重复执行)。

chat_assistant(普通用户聊天 Agent「小菱」) ← 用户操作手册 + 个人风格引导
manager(管理员管理 Agent)             ← 运维手册(接实时数据 + 固化流程)

用法:  docker exec -w /app cr_backend python /app/scripts/seed_agent_playbooks.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core.database import SessionLocal
from app.models.agent_governance import AgentKnowledgeChunk, AgentKnowledgeDoc
from app.services import agent_knowledge_service

PLAYBOOKS = {
    "chat_assistant": {
        "title": "普通用户操作知识库(小菱)",
        "file": "chat_assistant_playbook.md",
    },
    "manager": {
        "title": "管理员运维操作知识库",
        "file": "manager_ops_playbook.md",
    },
    "test_review": {
        "title": "沙箱测试审查知识库",
        "file": "sandbox_test_review_playbook.md",
    },
}

CONTENT_DIR = Path(__file__).resolve().parent.parent / "app" / "ai" / "agent_knowledge"


def _delete_existing(db, agent_code: str, title: str) -> None:
    docs = (
        db.query(AgentKnowledgeDoc)
        .filter(AgentKnowledgeDoc.agent_code == agent_code, AgentKnowledgeDoc.title == title)
        .all()
    )
    for doc in docs:
        db.query(AgentKnowledgeChunk).filter(AgentKnowledgeChunk.doc_id == doc.id).delete(
            synchronize_session=False
        )
        db.delete(doc)
    db.commit()


def seed() -> None:
    db = SessionLocal()
    try:
        for agent_code, meta in PLAYBOOKS.items():
            path = CONTENT_DIR / meta["file"]
            content = path.read_text(encoding="utf-8")
            _delete_existing(db, agent_code, meta["title"])
            doc = agent_knowledge_service.add_document(
                db,
                agent_code=agent_code,
                title=meta["title"],
                content=content,
                source_type="playbook",
                source_ref=meta["file"],
                risk_level="low",
                confidence=1.0,
            )
            if doc.status != "active":
                agent_knowledge_service.activate_document(db, doc.id)
            print(f"[seed] {agent_code}: doc#{doc.id} '{doc.title}' chunks={doc.chunk_count} status={doc.status}")
    finally:
        db.close()


if __name__ == "__main__":
    seed()
    print("[seed] 完成")
