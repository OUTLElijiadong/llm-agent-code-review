"""Knowledge excerpts disclose which source sections were omitted by their budgets."""

import hashlib

from app.ai import audit_knowledge_loader as loader


def test_rule_extraction_reports_partial_and_omitted_sections():
    source = "## 第一条\n必须检查 " + "A" * 100 + "\n## 第二条\n必须检查 B\n"
    coverage = []

    excerpt = loader._extract_rules(source, 40, coverage=coverage, source_name="rules.md")

    assert excerpt
    assert coverage == [{
        "source": "rules.md",
        "sha256": hashlib.sha256(source.encode("utf-8")).hexdigest(),
        "included": 1,
        "total": 2,
        "partial": 1,
        "omitted": 1,
    }]


def test_prompt_context_discloses_missing_knowledge_sections(monkeypatch):
    text = "\n".join(f"## 规则{i}\n必须检查规则{i}。" for i in range(20))
    monkeypatch.setattr(loader, "_read", lambda _name: text)

    context = loader.build_prompt_context("verification", l1_chars=120, l2_chars=160)

    assert "知识来源覆盖" in context
    assert "anti_hallucination.md" in context
    assert "evidence_contract.md" in context
    assert hashlib.sha256(text.encode("utf-8")).hexdigest() in context
    assert "省略段=" in context
    assert "未注入的段落不构成本次模型已读取的规则" in context


def test_full_source_is_reported_without_omissions():
    coverage = []
    source = "## 唯一规则\n必须核验来源。"

    assert loader._extract_rules(source, 100, coverage=coverage, source_name="one.md") == source
    assert coverage[0]["included"] == coverage[0]["total"] == 1
    assert coverage[0]["partial"] == coverage[0]["omitted"] == 0


def test_rule_excerpt_marks_section_partial_when_examples_are_dropped():
    coverage = []
    source = "\n".join(
        f"## 规则{i}\n必须核验来源。\n此处仅为长篇示例，不是判定规则。"
        for i in range(10)
    )

    loader._extract_rules(source, 100, coverage=coverage, source_name="examples.md")

    assert coverage[0]["partial"] > 0
