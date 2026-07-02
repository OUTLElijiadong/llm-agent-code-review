"""单元测试 (v3.1): 安全审计漏洞点定位兜底。

覆盖用户反馈「审计结果只有描述、没有漏洞点」:小模型常漏给 line_start,
此时用 evidence 原文在代码里反查行号,保证每条 finding 都能定位。
"""
from app.agents.security_sentinel_agent import SecuritySentinelAgent

_CODE = (
    "def login(req):\n"
    "    user = req.get('user')\n"
    "    sql = \"SELECT * FROM users WHERE name='\" + user + \"'\"\n"
    "    cur.execute(sql)\n"
    "    return cur.fetchall()\n"
)


def test_locate_exact_line():
    assert SecuritySentinelAgent._locate_evidence_line(_CODE, "cur.execute(sql)") == 4


def test_locate_picks_longest_evidence_line():
    ev = "noise\n    sql = \"SELECT * FROM users WHERE name='\" + user + \"'\"\nmore"
    assert SecuritySentinelAgent._locate_evidence_line(_CODE, ev) == 3


def test_locate_strips_backticks():
    assert SecuritySentinelAgent._locate_evidence_line(_CODE, "`cur.execute(sql)`") == 4


def test_locate_missing_returns_zero():
    assert SecuritySentinelAgent._locate_evidence_line(_CODE, "no such code here") == 0
    assert SecuritySentinelAgent._locate_evidence_line(_CODE, "") == 0


def test_normalize_backfills_line_from_evidence():
    """LLM 漏给 line_start,但给了 evidence → 归一化后 line_number 被回填。"""
    agent = SecuritySentinelAgent()

    class _Stub:
        file_path = "a.py"
        file_name = "a.py"
        id = 1

    raw = {
        "title": "SQL 注入",
        "severity": "严重",
        "owasp": "A03:2021-Injection",
        "cwe": "CWE-89",
        "evidence": "cur.execute(sql)",
        # 故意不给 line_start / line_end
    }
    norm = agent._normalize_finding(raw, _Stub(), line_offset=0, code=_CODE)
    assert norm is not None
    assert norm["line_number"] == 4
    assert norm["lines"] == "L4"
    assert norm["evidence"] == "cur.execute(sql)"
