"""统一安全与审查规则目录。

目录只投影既有规则源，不改变它们的写入与执行职责：

* ``review_rule`` 是注入 LLM 审查提示的可编辑规则；
* ``security_static_rules`` 与 ``security_patterns`` 是平台内置确定性扫描器；
* CodeQL 是可选外部执行器，由 CLI/CI 环境提供。

CNVD/CNNVD 仅作为漏洞情报与中文分类来源，不能被标记为源码规则执行器。
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from functools import lru_cache
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from app.ai.security_patterns import list_patterns
from app.ai.security_static_rules import list_static_rules
from app.models.user import User
from app.services.rule_service import list_rules

CODEQL_QUERY_HELP_URL = "https://codeql.github.com/codeql-query-help/"
CODEQL_SUITE_URL = "https://docs.github.com/en/code-security/concepts/code-scanning/codeql/codeql-query-suites"
OWASP_REVIEW_URL = "https://cheatsheetseries.owasp.org/cheatsheets/Secure_Code_Review_Cheat_Sheet.html"
CNVD_URL = "https://www.cnvd.org.cn/"
CNNVD_URL = "https://www.cnnvd.org.cn/"


def _codeql_executable() -> str | None:
    """返回配置或 PATH 中的 CodeQL CLI，且拒绝目录和不可执行文件。"""
    configured = (os.getenv("CODEQL_CLI_PATH") or "").strip()
    candidate = configured or shutil.which("codeql") or ""
    if not candidate:
        return None
    resolved = Path(candidate).expanduser().resolve()
    if not resolved.is_file() or not os.access(resolved, os.X_OK):
        return None
    return str(resolved)


def _codeql_version(executable: str | None) -> str:
    """有界读取 CLI 版本；失败时只返回未知，不阻断目录其他来源。"""
    if not executable:
        return ""
    try:
        result = subprocess.run(
            [executable, "version", "--format=json"],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
        payload = json.loads(result.stdout)
        return str(payload.get("version") or "")
    except (OSError, subprocess.SubprocessError, ValueError, TypeError):
        return ""


@lru_cache(maxsize=1)
def codeql_capability() -> dict[str, Any]:
    """缓存 CodeQL 探测；目录请求不重复启动外部进程。"""
    executable = _codeql_executable()
    version = _codeql_version(executable)
    cli_detected = bool(executable and version)
    return {
        "code": "github_codeql",
        "name": "GitHub CodeQL",
        "kind": "external_sast",
        # 产品 API 尚未接入 database create/analyze，不能把 CLI 存在标成可执行。
        "executable": False,
        "status": "cli_detected" if cli_detected else "ci_configured",
        "version": version,
        "suites": ["default", "security-extended"],
        "languages": ["python", "javascript-typescript"],
        "source_url": CODEQL_QUERY_HELP_URL,
        "documentation_url": CODEQL_SUITE_URL,
        "status_message": (
            "检测到 CodeQL CLI；产品扫描尚未接入，仓库提交由 GitHub Actions 执行。"
            if cli_detected
            else "仓库 CI 已配置 CodeQL；当前产品运行环境未配置 CLI，且未表示扫描通过。"
        ),
    }


def _review_rule_items(db: Session, user: User) -> list[dict[str, Any]]:
    return [
        {
            "id": f"review:{rule.id}",
            "code": rule.rule_code,
            "name": rule.rule_name,
            "category": rule.rule_type,
            "language": rule.language or "*",
            "severity": rule.severity or "中",
            "origin": "review_rule",
            "executor": "deepseek_prompt",
            "executable": bool(rule.enabled),
            "enabled": bool(rule.enabled),
            "builtin": bool(rule.is_builtin),
            "cwe": "",
            "owasp": "",
            "description": rule.rule_content,
            "source_url": "",
        }
        for rule in list_rules(db, user.id)
    ]


def _static_rule_items() -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for rule in list_static_rules():
        cwe = rule.get("cwe") or ""
        cwe_number = cwe.replace("CWE-", "")
        items.append({
            "id": f"static:{rule['code']}",
            "code": rule["code"],
            "name": rule["name"],
            "category": "security",
            "language": ",".join(rule.get("applies_to") or ["*"]),
            "severity": rule.get("severity") or "中",
            "origin": "platform_static",
            "executor": "static_analyzer",
            "executable": True,
            "enabled": True,
            "builtin": True,
            "cwe": cwe,
            "owasp": rule.get("owasp") or "",
            "description": rule.get("description") or "",
            "source_url": (
                f"https://cwe.mitre.org/data/definitions/{cwe_number}.html"
                if cwe_number.isdigit() else OWASP_REVIEW_URL
            ),
        })
    return items


def _secret_rule_items() -> list[dict[str, Any]]:
    return [
        {
            "id": "secret:" + hashlib.sha256(pattern["name"].encode("utf-8")).hexdigest()[:16],
            "code": pattern["name"].replace(" ", "_").lower(),
            "name": pattern["name"],
            "category": "secret",
            "language": "*",
            "severity": "严重",
            "origin": "platform_secret",
            "executor": "secret_scanner",
            "executable": True,
            "enabled": True,
            "builtin": True,
            "cwe": pattern.get("cwe") or "",
            "owasp": pattern.get("owasp") or "",
            "description": pattern.get("description") or "",
            "source_url": OWASP_REVIEW_URL,
        }
        for pattern in list_patterns()
    ]


def build_catalog(db: Session, user: User, *, include_review_rules: bool = False) -> dict[str, Any]:
    """构建统一规则目录及来源/执行器摘要。"""
    items = _static_rule_items() + _secret_rule_items()
    if include_review_rules:
        items = _review_rule_items(db, user) + items
    counts: dict[str, int] = {}
    for item in items:
        counts[item["origin"]] = counts.get(item["origin"], 0) + 1
    codeql = codeql_capability()
    engines = [
        {
            "code": "platform_static",
            "name": "平台确定性静态引擎",
            "kind": "builtin_sast",
            "executable": True,
            "status": "ready",
            "version": "v3",
            "suites": ["static", "secret"],
            "languages": ["multi-language"],
            "source_url": OWASP_REVIEW_URL,
            "documentation_url": OWASP_REVIEW_URL,
            "status_message": "平台静态规则和敏感信息规则可直接执行。",
        },
        codeql,
    ]
    return {
        "items": items,
        "counts": counts,
        "engines": engines,
        "sources": [
            {"code": "owasp", "name": "OWASP 安全代码审查", "role": "review_method", "url": OWASP_REVIEW_URL},
            {"code": "codeql", "name": "GitHub CodeQL 查询套件", "role": "executable_rules", "url": CODEQL_SUITE_URL},
            {"code": "cnvd", "name": "国家信息安全漏洞共享平台", "role": "vulnerability_intelligence", "url": CNVD_URL},
            {"code": "cnnvd", "name": "国家信息安全漏洞库", "role": "vulnerability_intelligence", "url": CNNVD_URL},
        ],
        "mapping_note": "CNVD/CNNVD 仅用于漏洞情报与中文分类；代码规则以 CWE/OWASP 映射，不虚构漏洞实例编号。",
    }
