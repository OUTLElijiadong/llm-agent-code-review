"""服务端时间口径：库内一律是不带时区的 UTC，对外统一标成 UTC，人读文档统一显示北京时间。

背景（审查任务 #231 事故）：
- 数据库 DateTime 列不带时区，写入用 ``datetime.now(timezone.utc)`` / ``datetime.utcnow()``，值是 UTC。
- pydantic 对 naive datetime 输出不带 ``Z`` 的 ISO 字符串，浏览器 ``new Date()`` 把它当本地时间，
  各页面又各自补 ``Z`` 或不补，同一任务在不同页面差 8 小时。

约定：
- 机器可读（API、JSON 导出、SSE/WebSocket）：UTC，以 ``Z`` 结尾，见 :func:`iso_utc`。
- 人读（HTML/PDF/Word/Markdown 导出、报告正文）：北京时间并显式标注“（UTC+8）”，见 :func:`format_beijing`。
- 不改数据库存储，不改业务计算里的 ``utcnow()`` 语义；本模块只在“输出”这一刻解释 naive 值。

北京时间用固定偏移 +08:00：中国自 1991 年起不使用夏令时，且避免对系统 tzdata 的运行时依赖。
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any, Optional

from pydantic import BaseModel

UTC = timezone.utc
BEIJING = timezone(timedelta(hours=8), "Asia/Shanghai")
BEIJING_LABEL = "（UTC+8）"


def parse_server_time(value: Any) -> Optional[datetime]:
    """把服务端时间解析为带 UTC 偏移的 datetime；无法解析返回 None。

    兼容历史上出现过的全部写法：``2026-10-22T08:20:00``、``...Z``、``...+00:00``、
    空格分隔的 ``2026-10-22 08:20:00``、带其他偏移的 ``...+08:00``。不带偏移的一律按 UTC 解释。

    Args:
        value: datetime 或 ISO 字符串。

    Returns:
        Optional[datetime]: 带 tzinfo 的 UTC 时刻；空值或无法解析时为 None。
    """
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        if text[-1] in "zZ":
            text = text[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None
    else:
        return None
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def iso_utc(value: Any) -> Any:
    """输出 UTC 时刻的 ISO 字符串（以 ``Z`` 结尾）；已带标记的值幂等。

    - naive datetime 按 UTC 解释，墙上时间不变，只补上 UTC 标记。
    - 其他偏移先换算到 UTC。
    - ``date``（不是时刻）和无法解析的字符串原样返回，None 返回 None。
    """
    if value is None:
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value.isoformat()
    parsed = parse_server_time(value)
    if parsed is None:
        return value
    return parsed.isoformat().replace("+00:00", "Z")


def iso_beijing(value: Any) -> Optional[str]:
    """输出带 ``+08:00`` 偏移的北京时间 ISO 字符串；空值或无法解析返回 None。"""
    parsed = parse_server_time(value)
    if parsed is None:
        return None
    return parsed.astimezone(BEIJING).isoformat()


def format_beijing(value: Any, *, label: bool = True) -> str:
    """人读用的北京时间：``2026-10-22 16:20:00（UTC+8）``。

    Args:
        value: datetime 或 ISO 字符串，naive 按 UTC 解释。
        label: 是否追加“（UTC+8）”标注，导出文档默认必须带。

    Returns:
        str: 空值为 ``-``；无法解析的文本原样返回，避免丢信息。
    """
    if value is None or (isinstance(value, str) and not value.strip()):
        return "-"
    parsed = parse_server_time(value)
    if parsed is None:
        return str(value)
    text = parsed.astimezone(BEIJING).strftime("%Y-%m-%d %H:%M:%S")
    return f"{text}{BEIJING_LABEL}" if label else text


def localize_fields(record: Any, fields: Any) -> Any:
    """返回 ``record`` 的副本，其中指定字段改为北京时间人读文本；其他字段一律不动。

    只改名单内且有值的字段：空值保持原样，由模板/导出器自己的占位逻辑显示“-”。
    供 HTML/PDF/Word 导出使用；JSON 导出不调用，保持机器可读的 UTC ``Z``。
    """
    if not isinstance(record, dict):
        return record
    localized = dict(record)
    for name in fields:
        value = localized.get(name)
        if value is None or (isinstance(value, str) and not value.strip()):
            continue
        localized[name] = format_beijing(value)
    return localized


def tag_naive_utc(value: Any) -> Any:
    """递归把 naive datetime 标成 UTC，其余偏移换算到 UTC；字符串和日期保持不动。

    供 JSON 输出前调用。遇到 dict/list/tuple/pydantic 模型时按需复制，未发现需要改的值时返回原对象，
    调用方手里的 ORM 对象、缓存字典和模型不会被改写。
    """
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value if value.utcoffset() == timedelta(0) else value.astimezone(UTC)
    if isinstance(value, dict):
        changed: Optional[dict] = None
        for key, item in value.items():
            new_item = tag_naive_utc(item)
            if new_item is not item:
                if changed is None:
                    changed = dict(value)
                changed[key] = new_item
        return value if changed is None else changed
    if isinstance(value, (list, tuple)):
        items = [tag_naive_utc(item) for item in value]
        if all(new is old for new, old in zip(items, value)):
            return value
        return items if isinstance(value, list) else tuple(items)
    if isinstance(value, BaseModel):
        updates = {}
        for name in type(value).model_fields:
            item = getattr(value, name, None)
            new_item = tag_naive_utc(item)
            if new_item is not item:
                updates[name] = new_item
        return value.model_copy(update=updates) if updates else value
    return value


def json_default(value: Any) -> Any:
    """``json.dumps(default=...)`` 用：datetime 输出 UTC ``Z``，日期输出日期，其余转文本。

    替代历史上的 ``default=str``（它会把 naive datetime 写成不带标记且用空格分隔的文本）。
    """
    if isinstance(value, datetime):
        return iso_utc(value)
    if isinstance(value, date):
        return value.isoformat()
    return str(value)
