"""
通用Schema: 统一响应结构和分页输出
"""
from typing import Generic, Optional, TypeVar

from pydantic import BaseModel, ConfigDict

T = TypeVar("T")


class StrictInputModel(BaseModel):
    """内部业务 API 请求的默认输入边界。

    只拒绝未声明字段，不开启 Pydantic 的全局 strict 类型模式，
    以保留现有 HTTP JSON 客户端的合法类型转换。
    """

    model_config = ConfigDict(extra="forbid")


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
