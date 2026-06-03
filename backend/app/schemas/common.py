"""
通用Schema: 统一响应结构和分页输出
"""
from typing import Generic, Optional, TypeVar

from pydantic import BaseModel

T = TypeVar("T")


class Resp(BaseModel, Generic[T]):
    """统一API响应结构"""
    code: int = 0
    message: str = "ok"
    data: Optional[T] = None


class PageOut(BaseModel, Generic[T]):
    """分页输出结构"""
    items: list[T]
    total: int
    page: int
    page_size: int
    pages: int
