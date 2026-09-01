"""
AI模块自定义异常
"""
from app.core.exceptions import AppError


class AiServiceError(AppError):
    """DeepSeek API调用异常"""
    code = 50201
    http_status = 502
    retryable = True
    next_action = "请稍后重试；若持续失败，请提供请求编号给管理员"


class ResultParseError(AppError):
    """AI返回结果解析异常"""
    code = 50202
    http_status = 502
    retryable = True
    next_action = "请重新发起任务；若持续失败，请提供请求编号给管理员"
