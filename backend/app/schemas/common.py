"""
通用Schema: 统一响应结构和分页输出
"""
from typing import Any, Generic, Optional, TypeVar

from pydantic import BaseModel, ConfigDict, SerializerFunctionWrapHandler, field_serializer

from app.core.server_time import tag_naive_utc

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

    @field_serializer("data", mode="wrap", when_used="json")
    def _tag_naive_datetimes_as_utc(self, value: Any, handler: SerializerFunctionWrapHandler) -> Any:
        """JSON 输出前把载荷里的 naive datetime 标成 UTC（输出 ``...Z``）。

        数据库时间列不带时区、值是 UTC；pydantic 默认把它输出成不带标记的 ISO 字符串，
        浏览器会按本地时区解读，导致同一时刻在不同页面差 8 小时（任务 #231 事故）。
        统一在信封出口处理，覆盖类型化模型、``Resp[dict]`` 里直接放 datetime 的服务返回值和分页包装；
        只影响 JSON 输出，python 模式下的对象与调用方手里的 ORM 对象保持不变。
        """
        return handler(tag_naive_utc(value))


class PageOut(BaseModel, Generic[T]):
    """分页输出结构"""
    items: list[T]
    total: int
    page: int
    page_size: int
    pages: int
