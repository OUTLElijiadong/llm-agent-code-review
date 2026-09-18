"""共享的用户输入边界校验。"""
from __future__ import annotations

import re
import unicodedata

_RULE_CODE = re.compile(r"^[a-z][a-z0-9_.-]{0,49}$")
_USERNAME = re.compile(r"^[\w\u4e00-\u9fff][\w\u4e00-\u9fff._-]{2,49}$", re.UNICODE)


def normalize_plain_text(
    value: str,
    *,
    field_name: str,
    allow_empty: bool = False,
    allow_newlines: bool = True,
) -> str:
    """去除首尾空白，并拒绝 NUL/C0/C1 控制字符。"""
    normalized = value.strip()
    if not normalized and not allow_empty:
        raise ValueError(f"{field_name} 不能为空")
    allowed_controls = {"\n", "\r", "\t"} if allow_newlines else set()
    for char in normalized:
        if unicodedata.category(char) == "Cc" and char not in allowed_controls:
            raise ValueError(f"{field_name} 包含不允许的控制字符")
    return normalized


def normalize_username(value: str, *, strict: bool = False) -> str:
    """注册使用稳定标识符；登录只做安全规范化以兼容历史账号。"""
    normalized = normalize_plain_text(value, field_name="用户名", allow_newlines=False)
    if strict and not _USERNAME.fullmatch(normalized):
        raise ValueError("用户名只能使用中文、字母、数字、下划线、点或短横线")
    return normalized


def normalize_rule_code(value: str) -> str:
    """把自定义规则编码限制为稳定、不可解释为路径的机器标识。"""
    normalized = normalize_plain_text(value, field_name="规则编码", allow_newlines=False)
    if not _RULE_CODE.fullmatch(normalized):
        raise ValueError("规则编码必须以小写字母开头，仅允许小写字母、数字、点、下划线和短横线")
    return normalized
