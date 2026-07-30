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
        content: dict = {"code": exc.code, "message": exc.message}
        # 生产环境收敛 detail/request_id 等调试细节,仅保留业务 code 与 message
        if not _is_prod():
            content["detail"] = exc.detail
            content["request_id"] = request_id
        return JSONResponse(
            status_code=exc.http_status,
            headers={"X-Request-Id": request_id},
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
        content: dict = {"code": 40002, "message": "参数校验失败"}
        if not _is_prod():
            content["detail"] = detail
            content["request_id"] = request_id
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
        content: dict = {"code": 50000, "message": "服务器内部错误"}
        if not _is_prod():
            content["detail"] = None
            content["request_id"] = request_id
        return JSONResponse(
            status_code=500,
            headers={"X-Request-Id": request_id},
            content=content,
        )
