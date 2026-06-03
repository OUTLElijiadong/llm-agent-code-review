"""
Prompt构建模块: 根据模板和参数构建审查Prompt
"""
from pathlib import Path
from typing import Tuple

PROMPT_PATH = Path(__file__).parent / "prompts" / "review.zh.md"

_SYSTEM = (
    "你是一名严谨的代码审查工程师。"
    "你必须严格按照用户消息中要求的 JSON 格式回答,只输出 JSON,不输出任何额外文字。"
)


def _load_template() -> str:
    """加载Prompt模板文件"""
    return PROMPT_PATH.read_text(encoding="utf-8")


def _format_rules(rules, language: str = "") -> str:
    """将规则列表格式化为Prompt中的规则段落,按分类和严重度组织

    Args:
        rules: 启用的规则ORM对象列表
        language: 当前审查文件的语言

    Returns:
        str: 格式化的规则描述文本
    """
    if not rules:
        return "(用户未启用任何规则,请按通用最佳实践审查)"

    by_type: dict[str, list] = {}
    for r in rules:
        by_type.setdefault(r.rule_type, []).append(r)

    type_labels = {
        "security": "🔴 安全检查",
        "correctness": "🐛 潜在Bug",
        "performance": "⚡ 性能优化",
        "robustness": "🛡️ 异常处理与健壮性",
        "maintainability": "🔧 可维护性",
        "style": "📝 代码风格",
        "documentation": "📖 文档与注释",
    }

    lang = (language or "").strip().lower()
    lang_filtered_count = sum(
        1 for r in rules if (
            getattr(r, "language", "*") in ("*", lang) or
            getattr(r, "language", "*") == "*"
        )
    )

    severity_order = {"严重": 1, "高": 2, "中": 3, "低": 4}

    lines = [
        f"## 当前文件语言: {language or '未知'}",
        f"## 审查规则: 共 {len(rules)} 条启用(含 {lang_filtered_count} 条适用当前语言)",
        "",
    ]

    counter = 0
    for rule_type, type_rules in by_type.items():
        label = type_labels.get(rule_type, rule_type)
        type_rules.sort(key=lambda r: severity_order.get(getattr(r, "severity", "中"), 3))
        lines.append(f"### {label}  ({len(type_rules)} 条)")
        for r in type_rules:
            counter += 1
            sev = getattr(r, "severity", "中")
            rule_lang = getattr(r, "language", "*")
            lang_tag = f"[{rule_lang}]" if rule_lang != "*" else ""
            lines.append(
                f"{counter}. [{sev}] {r.rule_name}{lang_tag} ({r.rule_code})\n"
                f"   {r.rule_content}",
            )
        lines.append("")

    lines.append("")
    lines.append(
        "请优先关注标记为[严重]和[高]的规则项,逐一扫描代码中是否存在违反项。"
        "如果没有发现问题,不要捏造。"
    )
    return "\n".join(lines)


def _format_experience(experiences) -> str:
    """将检索到的经验记忆格式化为 Prompt 段落(Agent 自进化 L1 注入)

    Args:
        experiences: ReviewExperience 列表(已按权重筛选)

    Returns:
        str: 经验参考段落;无经验时返回空串(模板里只留一个空行)
    """
    if not experiences:
        return ""
    lines = [
        "## 历史经验参考(本团队已确认的高频真实问题)",
        "下列为过往审查中被开发者确认并修复的高频问题。请重点核查是否存在同类问题;"
        "但仍以代码实际情况为准,不要为套用经验而捏造问题。",
        "",
    ]
    for i, e in enumerate(experiences, 1):
        sug = (getattr(e, "canonical_suggestion", "") or "").strip()
        sug_part = f" — 参考修复: {sug[:80]}" if sug else ""
        lines.append(
            f"{i}. [{getattr(e, 'issue_type', '')}] {getattr(e, 'title', '') or ''}"
            f"(历史确认 {getattr(e, 'accepted_count', 0)} 次){sug_part}",
        )
    lines.append("")
    return "\n".join(lines)


def build_prompt(*, language: str, file_name: str, code: str,
                 rules: list, line_offset: int = 0, agent_section: str = "",
                 experience_section: str = "") -> Tuple[str, str]:
    """构建审查用的System和User Prompt

    Args:
        language: 编程语言标识
        file_name: 文件名
        code: 代码内容
        rules: 启用规则列表
        line_offset: 行号偏移(分片时使用)
        agent_section: 当前审查代理画像说明
        experience_section: 历史经验参考段落(自进化注入,可空)

    Returns:
        tuple[str, str]: (system_prompt, user_prompt)
    """
    template = _load_template()
    if not agent_section:
        agent_section = "- 代理名称: 通用质量代理\n- 关注范围: 综合检查代码质量、安全、性能和可维护性。"
    user_prompt = (
        template
        .replace("{rules_section}", _format_rules(rules, language))
        .replace("{experience_section}", experience_section)
        .replace("{agent_section}", agent_section)
        .replace("{language}", language or "plaintext")
        .replace("{file_name}", file_name)
        .replace("{line_offset}", str(line_offset))
        .replace("{code_content}", code)
    )
    return _SYSTEM, user_prompt
