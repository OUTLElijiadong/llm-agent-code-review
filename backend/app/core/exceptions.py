"""
自定义应用异常类
"""
from typing import Any, Optional


class AppError(Exception):
    """应用基础异常类,包含业务错误码与HTTP状态码"""

    code: int = 50000
    http_status: int = 500

    def __init__(self, message: str, *, code: Optional[int] = None, detail: Any = None):
        self.message = message
        self.code = code or self.code
        self.detail = detail

    def __str__(self) -> str:
        """异常跨任务/SSE 边界时保留可操作的业务原因。"""
        return self.message


class AuthError(AppError):
    """鉴权异常: 未登录或token无效"""

    code = 40100
    http_status = 401


class ForbiddenError(AppError):
    """权限异常: 无访问权限"""

    code = 40300
    http_status = 403


class PermissionError(ForbiddenError):
    """RBAC 权限拒绝异常: 当前用户无指定操作权限

    用于 RBAC 体系中 require_permission 依赖校验失败时抛出。
    继承 ForbiddenError 以复用 403 状态码体系,同时提供独立错误码
    便于前端区分"通用无权访问"与"RBAC 权限不足"。

    Attributes:
        error_code: 字符串错误码,固定为 "PERMISSION_DENIED"
    """

    code = 40303
    error_code = "PERMISSION_DENIED"

    def __init__(self, message: str = "无操作权限", *, code: Optional[int] = None, detail: Any = None):
        super().__init__(message, code=code, detail=detail)


class NotFoundError(AppError):
    """资源不存在异常"""

    code = 40400
    http_status = 404


class ConflictError(AppError):
    """资源冲突异常"""

    code = 40901
    http_status = 409


class ValidationError(AppError):
    """参数校验异常"""

    code = 40001
    http_status = 400


class BadRequestError(ValidationError):
    """坏请求异常:客户端请求参数或语义错误(HTTP 400)

    继承 ValidationError,code=40000,用于 project_member_service 等模块
    区分"通用参数校验失败"与"业务语义坏请求"(如已是项目成员)。
    """

    code = 40000


class ExternalServiceError(AppError):
    """外部服务异常"""

    code = 50201
    http_status = 502


class ServiceUnavailableError(AppError):
    """服务因维护或容量保护暂时不可用，调用方可稍后重试。"""

    code = 50301
    http_status = 503


class TooManyRequestsError(AppError):
    """请求在短时间窗口内超过安全阈值。"""

    code = 42900
    http_status = 429

    def __init__(
        self,
        message: str = "请求过于频繁,请稍后再试",
        *,
        retry_after: int = 1,
        code: Optional[int] = None,
        detail: Any = None,
    ) -> None:
        super().__init__(message, code=code, detail=detail)
        self.retry_after = max(1, int(retry_after))
