"""
个人知识库(RAG) API 路由

- /knowledge/docs   文档增删查(严格本人隔离)
- /knowledge/search 检索(联调/演示)
- /knowledge/sync   从平台数据同步语料
- /knowledge/stats  统计
- /knowledge/embedding-config  embedding 配置(管理员)
"""
from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.dependencies import get_current_user, require_super_admin
from app.models.user import User
from app.schemas.common import PageOut, Resp
from app.schemas.knowledge import (
    DocAddIn,
    DocOut,
    EmbeddingConfigIn,
    EmbeddingConfigOut,
    KbStatsOut,
    SearchHitOut,
    SearchIn,
    SyncResultOut,
)
from app.services import knowledge_service, system_config_service

router = APIRouter()


@router.get("/docs", response_model=Resp[PageOut[DocOut]])
def list_docs(
    source_type: str = Query(""),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """我的知识库文档列表"""
    result = knowledge_service.list_docs(db, user.id, source_type, page, page_size)
    return Resp(data=PageOut(**result))


@router.post("/docs", response_model=Resp[dict])
def add_doc(payload: DocAddIn, db: Session = Depends(get_db),
            user: User = Depends(get_current_user)):
    """手动添加一篇知识文档(切片+嵌入)"""
    doc = knowledge_service.add_document(
        db, user.id, payload.title, payload.content, source_type="upload")
    return Resp(data={"id": doc.id, "chunk_count": doc.chunk_count})


@router.delete("/docs/{doc_id}", response_model=Resp[None])
def delete_doc(doc_id: int, db: Session = Depends(get_db),
               user: User = Depends(get_current_user)):
    """删除我的知识文档"""
    knowledge_service.delete_doc(db, user.id, doc_id)
    return Resp(data=None)


@router.post("/search", response_model=Resp[list[SearchHitOut]])
def search(payload: SearchIn, db: Session = Depends(get_db),
           user: User = Depends(get_current_user)):
    """在我的知识库内检索(仅本人数据)"""
    hits = knowledge_service.retrieve(db, user.id, payload.query, payload.top_k)
    return Resp(data=[SearchHitOut(**h) for h in hits])


@router.post("/sync", response_model=Resp[SyncResultOut])
def sync(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """从平台数据(项目代码/已处理问题/论坛/反馈/工单)同步到我的知识库"""
    result = knowledge_service.sync_from_platform(db, user.id)
    return Resp(data=SyncResultOut(**result))


@router.get("/stats", response_model=Resp[KbStatsOut])
def stats(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """我的知识库统计"""
    return Resp(data=KbStatsOut(**knowledge_service.get_stats(db, user.id)))


@router.get("/embedding-config", response_model=Resp[EmbeddingConfigOut])
def get_embedding_config(db: Session = Depends(get_db), admin: User = Depends(require_super_admin)):
    """由唯一超级管理员查看 embedding 配置(不回显明文 Key)。"""
    return Resp(data=EmbeddingConfigOut(**system_config_service.get_embedding_config_public(db)))


@router.put("/embedding-config", response_model=Resp[EmbeddingConfigOut])
def update_embedding_config(payload: EmbeddingConfigIn, db: Session = Depends(get_db),
                            admin: User = Depends(require_super_admin)):
    """由唯一超级管理员更新 embedding 配置。"""
    data = system_config_service.update_embedding_config(
        db, base_url=payload.base_url, api_key=payload.api_key,
        model=payload.model, enabled=payload.enabled)
    return Resp(data=EmbeddingConfigOut(**data))
