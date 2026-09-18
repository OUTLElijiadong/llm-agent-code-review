from app.ai.multi_agent import get_agent_profiles
from app.ai.static_analyzer import Finding
from app.models.review_rule import ReviewRule
from app.services.review_service import (
    _absolute_line,
    _build_summary,
    _finding_fingerprint,
    _freeze_rules,
    _rules_from_snapshot,
)


def test_rule_snapshot_freezes_content_and_detects_later_mutation():
    rule = ReviewRule(
        id=7,
        rule_code="sql",
        rule_name="SQL",
        rule_type="security",
        rule_content="使用参数化查询",
        language="python",
        severity="高",
        enabled=1,
        is_builtin=0,
    )
    snapshot = _freeze_rules([rule])
    rule.rule_content = "排队后被修改的内容"

    restored = _rules_from_snapshot(snapshot)

    assert restored[0].rule_content == "使用参数化查询"
    assert restored[0].severity == "高"
    assert len(snapshot[0]["sha256"]) == 64


def test_empty_rule_snapshot_is_valid_and_distinct_from_legacy_snapshot():
    """新任务可合法冻结为空规则集，不能在执行时回查后来启用的规则。"""
    assert _rules_from_snapshot([]) == []
    assert _rules_from_snapshot(None) is None
    assert _rules_from_snapshot([{"code": "legacy-only"}]) is None


def test_absolute_line_keeps_file_level_issue_zero():
    """文件级问题的行号应保持为 0,不能被分片偏移污染"""
    assert _absolute_line(0, 200) == 0
    assert _absolute_line(None, 200) == 0


def test_absolute_line_adds_chunk_offset():
    """分片内相对行号应转换为原文件绝对行号"""
    assert _absolute_line(3, 200) == 203


def test_finding_fingerprint_deduplicates_same_issue():
    """同一文件同一行同类问题应生成相同去重指纹"""
    first_finding = Finding(
        line_number=10,
        end_line=12,
        issue_type="安全漏洞",
        title="SQL 注入风险",
        description="字符串拼接构造 SQL,存在注入风险。",
    )
    second_finding = Finding(
        line_number=10,
        end_line=12,
        issue_type="安全漏洞",
        title="SQL 注入风险",
        description="字符串拼接构造 SQL,存在注入风险。",
    )

    first = _finding_fingerprint(file_id=1, finding=first_finding)
    second = _finding_fingerprint(file_id=1, finding=second_finding)

    assert first == second


def test_finding_fingerprint_differs_by_file():
    """不同文件的同问题应生成不同指纹"""
    finding = Finding(
        line_number=10,
        end_line=12,
        issue_type="安全漏洞",
        title="SQL 注入风险",
        description="字符串拼接构造 SQL,存在注入风险。",
    )

    assert _finding_fingerprint(file_id=1, finding=finding) != _finding_fingerprint(file_id=2, finding=finding)


def test_build_summary_mentions_multi_agent():
    """任务摘要应能体现本次使用的代理组合"""
    summary = _build_summary(get_agent_profiles("full"), file_count=2, issue_count=5, score=82)

    assert "多 Agent 协同" in summary
    assert "2 个文件" in summary
    assert "5 个问题" in summary
