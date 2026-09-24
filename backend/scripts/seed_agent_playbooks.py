"""把操作知识库种入对应 Agent 的 AgentKnowledge(幂等,可重复执行)。

chat_assistant(成员聊天 Agent「小菱」) ← 用户操作手册 + 角色说明
manager(管理员管理 Agent)             ← 运维手册(接实时数据 + 固化流程)

用法:  docker exec -w /app cr_backend python /app/scripts/seed_agent_playbooks.py \
           --only role_permission_guide.md --only chat_assistant_playbook.md
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core.database import SessionLocal
from app.models import load_all_models
from app.models.agent_governance import AgentKnowledgeChunk, AgentKnowledgeDoc
from app.services import agent_knowledge_service

# 每条一个目标 Agent;同一文档可写给多个 Agent(如角色权限说明书)。
PLAYBOOKS = [
    {"agent_code": "chat_assistant", "title": "普通用户操作知识库(小菱)", "file": "chat_assistant_playbook.md"},
    {"agent_code": "manager", "title": "管理员运维操作知识库", "file": "manager_ops_playbook.md"},
    {"agent_code": "test_review", "title": "沙箱测试审查知识库", "file": "sandbox_test_review_playbook.md"},
    {"agent_code": "chat_assistant", "title": "系统角色权限说明书(普通用户视角)", "file": "role_permission_guide.md"},
    {"agent_code": "manager", "title": "系统角色权限说明书(管理视角)", "file": "role_permission_guide.md"},
    {"agent_code": "manager", "title": "运维教程(仅超级管理员)", "file": "ops_tutorial_superadmin.md"},
]

CONTENT_DIR = Path(__file__).resolve().parent.parent / "app" / "ai" / "agent_knowledge"


def _delete_existing(db, agent_code: str, title: str, *, keep_doc_id: int | None = None) -> None:
    docs = (
        db.query(AgentKnowledgeDoc)
        .filter(AgentKnowledgeDoc.agent_code == agent_code, AgentKnowledgeDoc.title == title)
        .all()
    )
    for doc in docs:
        if doc.id == keep_doc_id:
            continue
        db.query(AgentKnowledgeChunk).filter(AgentKnowledgeChunk.doc_id == doc.id).delete(
            synchronize_session=False
        )
        db.delete(doc)
    db.commit()


def selected_playbooks(only_files: set[str] | None = None) -> list[dict[str, str]]:
    """只重建明确选中的手册，避免修改无关 Agent 知识。"""
    available = {entry["file"] for entry in PLAYBOOKS}
    unknown = (only_files or set()) - available
    if unknown:
        raise ValueError(f"未知手册: {', '.join(sorted(unknown))}")
    return [entry for entry in PLAYBOOKS if not only_files or entry["file"] in only_files]


def seed(only_files: set[str] | None = None) -> None:
    entries = selected_playbooks(only_files)
    # 独立脚本不会经过应用启动流程，先注册所有 ORM 映射供用量记账等关联表使用。
    load_all_models()
    db = SessionLocal()
    try:
        for entry in entries:
            agent_code = entry["agent_code"]
            meta = {"title": entry["title"], "file": entry["file"]}
            path = CONTENT_DIR / meta["file"]
            content = path.read_text(encoding="utf-8")
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
            # 新文档及向量已成功写入后，才移除旧版；嵌入失败时仍可检索旧版。
            _delete_existing(db, agent_code, meta["title"], keep_doc_id=doc.id)
            print(f"[seed] {agent_code}: doc#{doc.id} '{doc.title}' chunks={doc.chunk_count} status={doc.status}")
    finally:
        db.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="重建 Agent 操作手册知识切片")
    parser.add_argument("--only", action="append", choices=sorted({entry["file"] for entry in PLAYBOOKS}),
                        help="仅重建指定手册，可重复传入；省略时重建全部")
    args = parser.parse_args()
    seed(set(args.only) if args.only else None)
    print("[seed] 完成")
