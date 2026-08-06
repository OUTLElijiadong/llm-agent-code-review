"""PHP 全链路审计知识库加载器 (v3.3 新增)

迁移自 yunmengya/PHP_AUDIT_SKILLS 的 shared/ 知识库, 按 L1/L2/L3 分层注入策略
(见 references/agent_injection_framework.md) 控制每个 Agent 的 prompt token 用量:

  L1 必注入(所有审计 Agent): 反幻觉铁律 + 证据契约摘要 + 误报模式摘要
  L2 按角色注入:            sink 定义 / PHP 特性模式 / 攻击链 / 二阶漏洞 / CVE / 框架模式
  L3 按需引用:              data_contracts(大) 等, 只给路径+一行摘要, Agent 需要时自取

设计红线(对应用户痛点「假警报多」):
  知识库的价值在于**约束模型不乱报**, 不是堆料。所以 L1 只取反幻觉与误报模式的
  「规则清单」(去掉冗长示例), 把宝贵的 token 预算留给真正的代码证据。
"""
from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path
from typing import Dict

_KNOWLEDGE_DIR = Path(__file__).resolve().parent / "audit_knowledge"


@lru_cache(maxsize=64)
def _read(name: str) -> str:
    path = _KNOWLEDGE_DIR / f"{name}.md"
    if not path.exists():
        return ""
    try:
        return path.read_text(encoding="utf-8")
    except Exception:
        return ""


def _extract_rules(text: str, max_chars: int) -> str:
    """从 markdown 知识文件中抽取规则条目(标题 + 首行), 压缩体积.

    反幻觉/误报模式这类文件, 规则的名字与一句话判定条件才是关键,
    大段示例对模型反而是噪音。这里按 `##`/`###` 标题切片, 每片只留标题和
    「**Check**/**Pattern**/条件」这类判定句, 控制总长度。
    """
    if len(text) <= max_chars:
        return text
    out: list[str] = []
    budget = max_chars
    # 按二级/三级标题分片
    parts = re.split(r"(?m)^(?=#{2,3}\s)", text)
    for part in parts:
        if budget <= 0:
            break
        lines = part.splitlines()
        header = lines[0] if lines else ""
        # 取标题 + 含判定关键词的行(Check/Pattern/Condition/规则/判定/FP Condition)
        keep = [header]
        for ln in lines[1:]:
            s = ln.strip()
            if not s:
                continue
            if re.search(
                r"\*\*(Check|Pattern|FP Condition|Not FP|规则|判定|条件|原则)\*\*|"
                r"^\s*[-*]\s*\*\*|MUST|必须|禁止|不得|一律",
                s,
            ):
                keep.append(ln)
            if sum(len(k) for k in keep) > 600:  # 单条规则最多保留 600 字符
                break
        chunk = "\n".join(keep).strip()
        if not chunk:
            continue
        if len(chunk) > budget:
            chunk = chunk[:budget]
        out.append(chunk)
        budget -= len(chunk)
    return "\n".join(out).strip()


# ---- L1: 全 Agent 必注入(高度压缩的铁律) ----

def l1_core_rules(max_chars: int = 3500) -> str:
    """反幻觉铁律 + 误报红线, 所有审计/验证 Agent 必注入."""
    anti = _extract_rules(_read("anti_hallucination"), max_chars * 2 // 3)
    fp = _extract_rules(_read("false_positive_patterns"), max_chars // 3)
    return (
        "## 反幻觉与证据铁律(违反即被 QC 驳回)\n" + anti
        + "\n\n## 已知误报模式(命中即降级/排除)\n" + fp
    ).strip()


# ---- L2: 按角色注入 ----

_L2_MAP: Dict[str, str] = {
    "sink": "sink_definitions",
    "php_patterns": "php_specific_patterns",
    "attack_chains": "attack_chains",
    "second_order": "second_order",
    "cves": "known_cves",
    "frameworks": "framework_patterns",
    "severity": "severity_rating",
    "evidence": "evidence_contract",
}


def l2_for_role(role: str, max_chars: int = 6000) -> str:
    """按 Agent 角色注入对应领域知识.

    role: recon / analysis / verification / report
    """
    role_files = {
        "recon": ["sink", "frameworks", "cves"],
        "analysis": ["sink", "php_patterns", "second_order", "attack_chains", "frameworks"],
        "verification": ["evidence", "php_patterns", "attack_chains", "severity"],
        "report": ["severity", "evidence"],
    }.get(role, ["sink"])

    per = max_chars // max(1, len(role_files))
    sections = []
    for key in role_files:
        name = _L2_MAP.get(key)
        if not name:
            continue
        body = _extract_rules(_read(name), per)
        if body:
            sections.append(f"### 知识库·{name}\n{body}")
    return "\n\n".join(sections).strip()


# ---- L3: 按需引用(只给索引) ----

def l3_index() -> str:
    return (
        "可按需引用的完整知识库文件(需要时阅读, 不占当前上下文):\n"
        "- audit_knowledge/data_contracts.md — 跨 Agent 数据契约 JSON Schema\n"
        "- audit_knowledge/known_cves.md — PHP 生态高频 CVE 速查\n"
        "- audit_knowledge/attack_chains.md — 多步攻击链模式库"
    )


def build_prompt_context(role: str, l1_chars: int = 3500, l2_chars: int = 6000) -> str:
    """组装某角色 Agent 的完整知识上下文(L1 + L2 + L3 索引)."""
    parts = [l1_core_rules(l1_chars), l2_for_role(role, l2_chars)]
    return "\n\n".join(p for p in parts if p)
