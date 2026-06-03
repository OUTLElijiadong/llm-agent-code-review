"""
日志配置模块 - 基于loguru
"""
import sys

from loguru import logger

from app.core.config import settings


def setup_logger():
    """配置loguru日志系统,按天滚动,保留30天"""
    logger.remove()
    logger.add(
        sys.stderr,
        level=settings.log_level,
        format=(
            "<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | <level>{level: <8}</level> | "
            "<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> | <level>{message}</level>"
        ),
        colorize=True,
    )
    logger.add(
        "logs/app_{time:YYYY-MM-DD}.log",
        level=settings.log_level,
        format="{time:YYYY-MM-DD HH:mm:ss.SSS} | {level} | {name}:{function}:{line} | {message}",
        rotation="50 MB",
        retention="30 days",
        encoding="utf-8",
        enqueue=True,
    )
