"""全局异常处理器注册模块。"""
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from loguru import logger

from app.core.config import settings
from app.core.exceptions import AppError
from app.core.observability import get_request_id


def _is_prod() -> bool:
    """判断是否为生产环境(用于收敛对外的调试细节)。"""
    return settings.app_env.lower() not in ("dev", "test")


def register_handlers(app: FastAPI) -> None:
    """向 FastAPI 应用注册统一异常响应处理器。

    Args:
        app: FastAPI 应用实例。

    Returns:
        None: 处理器直接注册到应用。
    """

    @app.exception_handler(AppError)
    async def app_error_handler(req: Request, exc: AppError) -> JSONResponse:
        """处理项目自定义业务异常。"""
        request_id = get_request_id(req)
        content: dict = {
            "code": exc.code,
            "message": exc.message,
            "request_id": request_id,
            "retryable": exc.retryable,
            "next_action": exc.next_action,
        }
        headers = {"X-Request-Id": request_id}
        retry_after = getattr(exc, "retry_after", None)
        if retry_after is not None:
            headers["Retry-After"] = str(max(1, int(retry_after)))
        # request_id 是用户与值班人员关联日志的恢复入口；生产仅收敛内部 detail。
        if not _is_prod():
            content["detail"] = exc.detail
        return JSONResponse(
            status_code=exc.http_status,
            headers=headers,
            content=content,
        )

    @app.exception_handler(RequestValidationError)
    async def validation_handler(
        req: Request,
        exc: RequestValidationError,
    ) -> JSONResponse:
        """处理 FastAPI 请求参数校验异常。"""
        request_id = get_request_id(req)
        # 生产环境不再回显 exc.errors() 的 loc(字段路径)等调试细节
        detail = exc.errors() if not _is_prod() else None
        content: dict = {
            "code": 40002,
            "message": "参数校验失败",
            "request_id": request_id,
            "retryable": False,
            "next_action": "请检查输入后重新提交",
        }
        if not _is_prod():
            content["detail"] = detail
        return JSONResponse(
            status_code=400,
            headers={"X-Request-Id": request_id},
            content=content,
        )

    @app.exception_handler(Exception)
    async def unhandled_handler(req: Request, exc: Exception) -> JSONResponse:
        """处理所有未捕获异常并返回不泄露内部细节的响应。"""
        logger.exception(exc)
        request_id = get_request_id(req)
        content: dict = {
            "code": 50000,
            "message": "服务器内部错误",
            "request_id": request_id,
            # 未知错误可能发生在副作用提交之后，禁止暗示客户端盲目重放。
            "retryable": False,
            "next_action": "请先刷新状态；若仍失败，请提供请求编号给管理员",
        }
        if not _is_prod():
            content["detail"] = None
        return JSONResponse(
            status_code=500,
            headers={"X-Request-Id": request_id},
            content=content,
        )
