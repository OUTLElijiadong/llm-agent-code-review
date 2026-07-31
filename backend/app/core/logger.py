"""日志配置模块：基于 Loguru 输出 request id 与滚动文件。"""
import sys

from loguru import logger

from app.core.config import settings


def setup_logger() -> None:
    """配置控制台与文件日志，并为非请求日志提供 request id 默认值。

    Returns:
        None: 直接更新 Loguru 全局处理器。
    """
    logger.remove()
    logger.configure(extra={"request_id": "-"})
    logger.add(
        sys.stderr,
        level=settings.log_level,
        format=(
            "<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | "
            "<level>{level: <8}</level> | request_id={extra[request_id]} | "
            "<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> | "
            "<level>{message}</level>"
        ),
        colorize=True,
    )
    logger.add(
        "logs/app_{time:YYYY-MM-DD}.log",
        level=settings.log_level,
        format=(
            "{time:YYYY-MM-DD HH:mm:ss.SSS} | {level} | "
            "request_id={extra[request_id]} | {name}:{function}:{line} | {message}"
        ),
        rotation="50 MB",
        retention="30 days",
        encoding="utf-8",
        enqueue=True,
    )
