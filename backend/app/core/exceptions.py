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


class AuthError(AppError):
    """鉴权异常: 未登录或token无效"""

    code = 40100
    http_status = 401


class ForbiddenError(AppError):
    """权限异常: 无访问权限"""

    code = 40300
    http_status = 403


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
