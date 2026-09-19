"""共享的用户输入边界校验。"""
from __future__ import annotations

import json
import re
import unicodedata
from typing import Any

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


def validate_json_payload(
    value: Any,
    *,
    field_name: str,
    max_bytes: int = 65_536,
    max_depth: int = 12,
    max_items: int = 1_000,
) -> Any:
    """验证可变 JSON 输入的容量、深度和文本控制字符边界。

    该函数只做拒绝式校验，不重写合法字符串，避免破坏 Prompt、
    源码片段或结构化配置的原始语义。
    """

    if max_bytes < 1 or max_depth < 0 or max_items < 1:
        raise ValueError(f"{field_name} 校验边界配置无效")

    item_count = 0
    active_containers: set[int] = set()

    def _walk(node: Any, depth: int) -> None:
        nonlocal item_count
        if depth > max_depth:
            raise ValueError(f"{field_name} 嵌套层级超过 {max_depth}")
        if isinstance(node, str):
            # 复用统一控制字符规则，但保留原值不做 strip。
            normalize_plain_text(
                node,
                field_name=field_name,
                allow_empty=True,
                allow_newlines=True,
            )
            return
        if node is None or isinstance(node, (bool, int, float)):
            return
        if not isinstance(node, (dict, list)):
            raise ValueError(f"{field_name} 包含不可 JSON 序列化的值")

        identity = id(node)
        if identity in active_containers:
            raise ValueError(f"{field_name} 不允许循环引用")
        active_containers.add(identity)
        try:
            if isinstance(node, dict):
                item_count += len(node)
                if item_count > max_items:
                    raise ValueError(f"{field_name} 元素数超过 {max_items}")
                for key, child in node.items():
                    if not isinstance(key, str):
                        raise ValueError(f"{field_name} 的对象键必须是字符串")
                    normalize_plain_text(
                        key,
                        field_name=f"{field_name}键",
                        allow_empty=True,
                        allow_newlines=False,
                    )
                    _walk(child, depth + 1)
            else:
                item_count += len(node)
                if item_count > max_items:
                    raise ValueError(f"{field_name} 元素数超过 {max_items}")
                for child in node:
                    _walk(child, depth + 1)
        finally:
            active_containers.remove(identity)

    _walk(value, 0)
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} 必须是有限大小的合法 JSON") from exc
    if len(encoded) > max_bytes:
        raise ValueError(f"{field_name} 不得超过 {max_bytes} 字节")
    return value
