"""富文本/用户输入消毒工具。

对用户可控的富文本字段(论坛内容、项目名、用户昵称等)在写入前做白名单过滤,
防止存储型 XSS。优先使用 bleach 白名单;无 bleach 时退化为纯文本转义(最严格)。
"""
from __future__ import annotations

try:
    import bleach  # type: ignore

    _HAS_BLEACH = True
except Exception:  # pragma: no cover - bleach 未安装的环境
    _HAS_BLEACH = False

# 论坛/富文本允许的标签与属性(最小安全集)
_ALLOWED_TAGS = [
    "p", "br", "strong", "em", "b", "i", "u", "code", "pre",
    "ul", "ol", "li", "blockquote", "h1", "h2", "h3", "h4", "a", "span",
]
_ALLOWED_ATTRS = {"a": ["href", "title", "target", "rel"], "span": []}
_ALLOWED_PROTOCOLS = ["http", "https", "mailto"]


def sanitize_text(value: str | None) -> str:
    """清洗纯文本字段(项目名/昵称/标题等):剥离一切 HTML,保留可读文本。

    Args:
        value: 原始输入。

    Returns:
        str: 去除全部标签后的安全文本。
    """
    if not value:
        return ""
    text = str(value)
    if _HAS_BLEACH:
        # 不允许任何标签,返回纯文本
        return bleach.clean(text, tags=[], attributes={}, strip=True).strip()
    # 无 bleach 时退化为转义 <>&" 防注入
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .strip()
    )


def sanitize_rich_text(value: str | None) -> str:
    """清洗富文本字段(论坛正文/回复):白名单保留安全排版标签,剔除脚本与危险属性。

    Args:
        value: 原始富文本。

    Returns:
        str: 白名单过滤后的安全 HTML。
    """
    if not value:
        return ""
    text = str(value)
    if _HAS_BLEACH:
        return bleach.clean(
            text,
            tags=_ALLOWED_TAGS,
            attributes=_ALLOWED_ATTRS,
            protocols=_ALLOWED_PROTOCOLS,
            strip=True,
        ).strip()
    # 无 bleach 时最保守:全部转义,不保留任何标签
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .strip()
    )
