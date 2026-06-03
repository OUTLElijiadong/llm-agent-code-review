"""
全局异常处理器注册模块
"""
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from loguru import logger

from app.core.exceptions import AppError


def register_handlers(app: FastAPI):
    """向FastAPI应用注册全局异常处理器

    Args:
        app: FastAPI应用实例
    """

    @app.exception_handler(AppError)
    async def app_error_handler(req: Request, exc: AppError):
        """处理自定义AppError及其子类异常"""
        return JSONResponse(
            status_code=exc.http_status,
            content={
                "code": exc.code,
                "message": exc.message,
                "detail": exc.detail,
                "request_id": req.headers.get("X-Request-Id"),
            },
        )

    @app.exception_handler(RequestValidationError)
    async def validation_handler(req: Request, exc: RequestValidationError):
        """处理FastAPI自动参数校验失败异常"""
        return JSONResponse(
            status_code=400,
            content={
                "code": 40002,
                "message": "参数校验失败",
                "detail": exc.errors(),
                "request_id": req.headers.get("X-Request-Id"),
            },
        )

    @app.exception_handler(Exception)
    async def unhandled_handler(req: Request, exc: Exception):
        """处理所有未捕获异常"""
        logger.exception(exc)
        return JSONResponse(
            status_code=500,
            content={
                "code": 50000,
                "message": "服务器内部错误",
                "detail": None,
                "request_id": req.headers.get("X-Request-Id"),
            },
        )
