from app.ai.multi_agent import get_agent_profiles
from app.services.review_service import _absolute_line, _build_summary, _issue_fingerprint


def test_absolute_line_keeps_file_level_issue_zero():
    """文件级问题的行号应保持为 0,不能被分片偏移污染"""
    assert _absolute_line(0, 200) == 0
    assert _absolute_line(None, 200) == 0


def test_absolute_line_adds_chunk_offset():
    """分片内相对行号应转换为原文件绝对行号"""
    assert _absolute_line(3, 200) == 203


def test_issue_fingerprint_deduplicates_same_issue():
    """同一文件同一行同类问题应生成相同去重指纹"""
    first = _issue_fingerprint(
        file_id=1,
        line_number=10,
        end_line=12,
        issue_type="安全漏洞",
        title="SQL 注入风险",
        description="字符串拼接构造 SQL,存在注入风险。",
    )
    second = _issue_fingerprint(
        file_id=1,
        line_number=10,
        end_line=12,
        issue_type="安全漏洞",
        title="SQL 注入风险",
        description="字符串拼接构造 SQL,存在注入风险。",
    )

    assert first == second


def test_build_summary_mentions_multi_agent():
    """任务摘要应能体现本次使用的代理组合"""
    summary = _build_summary(get_agent_profiles("full"), file_count=2, issue_count=5, score=82)

    assert "多 Agent 协同" in summary
    assert "2 个文件" in summary
    assert "5 个问题" in summary
