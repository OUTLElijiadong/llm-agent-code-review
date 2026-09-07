"""官方安全分类快照；新扫描使用当前映射，历史记录不在此迁移。"""
from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import re


_DATA_PATH = Path(__file__).with_name("data") / "security_catalog.json"
_CATALOG = json.loads(_DATA_PATH.read_text(encoding="utf-8"))
OWASP_VERSION = _CATALOG["metadata"]["owasp_version"]
OWASP_CATEGORIES = tuple(_CATALOG["categories"])
_BY_CODE = {category["code"]: category for category in OWASP_CATEGORIES}
_BY_CWE = {cwe: category for category in OWASP_CATEGORIES for cwe in category["cwe_refs"]}


def catalog_metadata() -> dict:
    return deepcopy(_CATALOG["metadata"])


def get_cwe(cwe: str) -> dict | None:
    entry = _CATALOG["cwe_entries"].get(str(cwe).upper())
    return deepcopy(entry) if entry else None


def owasp_for_cwe(cwe: str) -> str:
    """仅返回官网明确映射；不把未列出的 CWE 硬塞入一个分类。"""
    entry = _BY_CWE.get(str(cwe).upper())
    return entry["owasp"] if entry else ""


def owasp_reference(owasp: str) -> str:
    match = re.match(r"^(A\d{2}):(\d{4})(?:-|$)", str(owasp))
    if not match:
        return "https://owasp.org/Top10/"
    code, version = match.groups()
    if version == OWASP_VERSION and code in _BY_CODE:
        return _BY_CODE[code]["source_url"]
    # 历史报告链接指向该版本首页，绝不能悄悄跳到当前类别。
    return f"https://owasp.org/Top10/{version}/"


def owasp_prompt_context() -> str:
    return "OWASP Top 10:2025 Final 当前分类（不能沿用 2021 编号）：\n" + "\n".join(
        f"- {category['owasp']}（{category['name_zh']}）" for category in OWASP_CATEGORIES
    ) + "\nSSRF 属于 A01；A10 为异常条件处理不当。未知映射填空，不臆造 CWE 或 CVE。\n"
