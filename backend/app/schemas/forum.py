"""
开发者论坛 Pydantic Schema
"""
from datetime import datetime
from typing import List, Optional

from pydantic import BaseModel, Field


class PostIn(BaseModel):
    """发帖"""
    title: str = Field(min_length=1, max_length=200)
    content: str = Field(min_length=1)
    category: str = Field(default="qa", pattern="^(qa|tech|share|announce|other)$")


class PostUpdateIn(BaseModel):
    """编辑帖子"""
    title: Optional[str] = Field(default=None, min_length=1, max_length=200)
    content: Optional[str] = Field(default=None, min_length=1)
    category: Optional[str] = Field(default=None, pattern="^(qa|tech|share|announce|other)$")


class PinIn(BaseModel):
    pinned: bool = True


class ReplyIn(BaseModel):
    content: str = Field(min_length=1)


class AssistIn(BaseModel):
    """论坛发帖助手(RAG)"""
    title: str = Field(default="", max_length=200)
    draft: str = Field(min_length=1)


class PostListItemOut(BaseModel):
    """论坛帖子列表项

    R7 修复(2026-06-25):补齐 status 字段,对齐 ForumPost ORM。
    """
    id: int
    user_id: int
    author_name: str = ""
    category: str
    title: str
    view_count: int = 0
    reply_count: int = 0
    is_pinned: bool = False
    status: str = "normal"
    create_time: datetime
    update_time: datetime

    model_config = {"from_attributes": True}


class ReplyOut(BaseModel):
    """论坛回复项

    R6 修复(2026-06-25):补齐 update_time,对齐 ForumReply ORM。
    """
    id: int
    post_id: int
    user_id: int
    author_name: str = ""
    content: str
    create_time: datetime
    # R6 修复:补齐 update_time,对齐 ForumReply ORM
    update_time: Optional[datetime] = None

    model_config = {"from_attributes": True}


class PostDetailOut(PostListItemOut):
    content: str
    replies: List[ReplyOut] = []
