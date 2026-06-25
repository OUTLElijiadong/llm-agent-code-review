"""单元测试 (T07): result_parser 模块 v3 增强

覆盖:
1. v3 新字段(cvss_score/cvss_vector/compliance_mapping/remediation)解析
2. 旧格式 JSON(无 v3 字段)向后兼容
3. CVSS 合法性校验与范围裁剪
4. 合规映射基于 CWE 自动反查
5. 安全类 issue 缺 cvss 时基于 severity 经验值补全
6. 空输入/非法 JSON/非对象 JSON 异常路径
7. 静态分析器 Finding 数据类 v3 字段填充
"""
import json

import pytest

from app.ai.result_parser import (
    Issue,
    ReviewResult,
    _build_compliance_mapping,
    _coerce_cvss_score,
    _coerce_cvss_vector,
    _coerce_remediation,
    _normalize_issue,
    parse,
)
from app.ai.static_analyzer import (
    Finding,
    _build_compliance_mapping as _static_build_compliance_mapping,
    _build_remediation as _static_build_remediation,
    _cwe_to_cvss_vector,
    _severity_to_cvss_score,
    scan,
)


# ============ CVSS 字段解析 ============

def test_coerce_cvss_score_normal():
    """合法数值正常解析,保留 1 位小数"""
    assert _coerce_cvss_score(9.8) == 9.8
    assert _coerce_cvss_score("7.5") == 7.5
    assert _coerce_cvss_score(8.76) == 8.8


def test_coerce_cvss_score_out_of_range_clamped():
    """超出 [0, 10] 范围的值被裁剪"""
    assert _coerce_cvss_score(-1.5) == 0.0
    assert _coerce_cvss_score(15.0) == 10.0
    assert _coerce_cvss_score(100) == 10.0


def test_coerce_cvss_score_invalid_returns_zero():
    """非法值返回默认 0.0"""
    assert _coerce_cvss_score(None) == 0.0
    assert _coerce_cvss_score("abc") == 0.0
    assert _coerce_cvss_score([]) == 0.0


def test_coerce_cvss_vector_normal():
    """合法 CVSS v3.1 向量正常返回"""
    v = "AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H"
    assert _coerce_cvss_vector(v) == v


def test_coerce_cvss_vector_with_prefix_stripped():
    """带 CVSS:3.1/ 前缀的向量被剥离前缀"""
    v = "CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H"
    expected = "AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H"
    assert _coerce_cvss_vector(v) == expected


def test_coerce_cvss_vector_missing_metrics_returns_empty():
    """缺失必备度量项的向量返回空字符串"""
    assert _coerce_cvss_vector("AV:N/AC:L") == ""
    assert _coerce_cvss_vector("not-a-vector") == ""


def test_coerce_cvss_vector_empty():
    """空值返回空字符串"""
    assert _coerce_cvss_vector("") == ""
    assert _coerce_cvss_vector(None) == ""


def test_coerce_remediation_normal():
    """合法文本正常返回"""
    text = "1. 改用参数化查询;2. 启用 WAF"
    assert _coerce_remediation(text) == text


def test_coerce_remediation_truncates_too_long():
    """超长文本被截断到 2000 字符"""
    long_text = "a" * 5000
    result = _coerce_remediation(long_text)
    assert len(result) == 2000


def test_coerce_remediation_empty():
    """空值返回空字符串"""
    assert _coerce_remediation("") == ""
    assert _coerce_remediation(None) == ""


# ============ 合规映射反查 ============

def test_build_compliance_mapping_known_cwe():
    """已知 CWE 返回非空合规映射,包含 4 个标准键"""
    mapping = _build_compliance_mapping("CWE-89")
    assert mapping, "CWE-89 应返回非空映射"
    assert "iso27001" in mapping
    assert "gdpr" in mapping
    assert "pci_dss" in mapping
    assert "hipaa" in mapping
    # CWE-89 (SQL 注入) 应命中 PCI-DSS 6.2.4 或 6.5.1
    assert len(mapping["pci_dss"]) > 0 or len(mapping["iso27001"]) > 0


def test_build_compliance_mapping_unknown_cwe_returns_empty():
    """未知 CWE 返回空字典(不抛异常)"""
    assert _build_compliance_mapping("CWE-9999") == {}


def test_build_compliance_mapping_empty_cwe_returns_empty():
    """空 CWE 返回空字典"""
    assert _build_compliance_mapping("") == {}
    assert _build_compliance_mapping(None) == {}


def test_build_compliance_mapping_case_insensitive():
    """CWE 编号大小写不敏感"""
    upper = _build_compliance_mapping("CWE-89")
    lower = _build_compliance_mapping("cwe-89")
    assert upper == lower


# ============ _normalize_issue v3 字段 ============

def test_normalize_issue_v3_full_fields():
    """完整 v3 字段的 issue 正常解析"""
    raw = {
        "line_number": 10,
        "end_line": 12,
        "issue_type": "安全漏洞",
        "severity": "严重",
        "title": "SQL 注入",
        "description": "字符串拼接 SQL",
        "suggestion": "改用参数化查询",
        "fixed_code": "cursor.execute(%s, (name,))",
        "owasp": "A03:2021-Injection",
        "cwe": "CWE-89",
        "evidence": "cursor.execute('SELECT...' + name)",
        "exploit_scenario": "攻击者注入 OR 1=1 拖库",
        "references": ["https://cwe.mitre.org/data/definitions/89.html"],
        "confidence": 0.9,
        "cvss_score": 9.8,
        "cvss_vector": "AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H",
        "remediation": "1. 参数化查询;2. 最小权限;3. WAF",
    }
    issue = _normalize_issue(raw)
    assert issue.cvss_score == 9.8
    assert issue.cvss_vector == "AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H"
    assert "1. 参数化查询" in issue.remediation
    # 合规映射应自动反查填充
    assert issue.compliance_mapping, "CWE-89 应触发合规反查"
    assert "iso27001" in issue.compliance_mapping


def test_normalize_issue_v2_only_backward_compatible():
    """v2 格式(无 v3 字段)仍能解析,v3 字段取默认值"""
    raw = {
        "line_number": 5,
        "issue_type": "安全漏洞",
        "severity": "高",
        "title": "XSS",
        "description": "未转义输出到 HTML",
        "suggestion": "使用 escape",
        "owasp": "A03:2021-Injection",
        "cwe": "CWE-79",
        "evidence": "innerHTML = userInput",
        "exploit_scenario": "攻击者注入脚本窃取 Cookie",
        "references": [],
        "confidence": 0.85,
    }
    issue = _normalize_issue(raw)
    # v2 字段正常
    assert issue.cwe == "CWE-79"
    assert issue.confidence == 0.85
    # v3 字段默认值 + severity 经验值补全
    assert issue.cvss_score == 7.5  # 高 -> 7.5
    assert issue.cvss_vector == ""  # LLM 未输出,默认空
    assert issue.remediation == ""
    # 合规映射仍基于 cwe 反查
    assert issue.compliance_mapping, "CWE-79 应触发合规反查"


def test_normalize_issue_security_missing_cvss_uses_severity():
    """安全类 issue 缺 cvss_score 时,基于 severity 给经验值"""
    raw = {
        "issue_type": "安全漏洞",
        "severity": "严重",
        "title": "RCE",
        "description": "命令注入",
        "cwe": "CWE-78",
    }
    issue = _normalize_issue(raw)
    assert issue.cvss_score == 9.5  # 严重 -> 9.5


def test_normalize_issue_non_security_cvss_zero():
    """非安全类 issue cvss_score 保持 0.0"""
    raw = {
        "issue_type": "代码规范",
        "severity": "低",
        "title": "命名不规范",
        "description": "变量名使用单字母",
    }
    issue = _normalize_issue(raw)
    assert issue.cvss_score == 0.0
    assert issue.compliance_mapping == {}


def test_normalize_issue_cvss_score_out_of_range_clamped():
    """cvss_score 超出 [0, 10] 被裁剪"""
    raw = {
        "issue_type": "安全漏洞",
        "severity": "严重",
        "cvss_score": 99.9,
        "cwe": "CWE-89",
    }
    issue = _normalize_issue(raw)
    assert issue.cvss_score == 10.0


def test_normalize_issue_cvss_vector_invalid_returns_empty():
    """非法 cvss_vector 返回空字符串"""
    raw = {
        "issue_type": "安全漏洞",
        "severity": "高",
        "cvss_vector": "not-a-valid-vector",
        "cwe": "CWE-89",
    }
    issue = _normalize_issue(raw)
    assert issue.cvss_vector == ""


# ============ parse() 顶层函数 ============

def test_parse_v3_full_json():
    """完整 v3 格式 JSON 正常解析"""
    text = json.dumps({
        "summary": "代码存在严重安全漏洞,需立即修复",
        "score": 45,
        "issues": [
            {
                "line_number": 10,
                "issue_type": "安全漏洞",
                "severity": "严重",
                "title": "SQL 注入",
                "description": "字符串拼接构造 SQL",
                "suggestion": "改用参数化查询",
                "owasp": "A03:2021-Injection",
                "cwe": "CWE-89",
                "evidence": "execute('SELECT...' + name)",
                "exploit_scenario": "攻击者注入 OR 1=1",
                "references": ["https://cwe.mitre.org/89"],
                "confidence": 0.95,
                "cvss_score": 9.8,
                "cvss_vector": "AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H",
                "remediation": "1. 参数化查询;2. 最小权限",
            },
        ],
    }, ensure_ascii=False)
    result = parse(text)
    assert isinstance(result, ReviewResult)
    assert result.score == 45
    assert len(result.issues) == 1
    issue = result.issues[0]
    assert issue.cvss_score == 9.8
    assert issue.compliance_mapping, "应自动反查合规映射"
    assert "iso27001" in issue.compliance_mapping


def test_parse_v2_only_backward_compatible():
    """v2 格式 JSON 仍能解析,v3 字段取默认值"""
    text = json.dumps({
        "summary": "代码审查完成",
        "score": 70,
        "issues": [
            {
                "line_number": 5,
                "issue_type": "安全漏洞",
                "severity": "中",
                "title": "弱加密",
                "description": "使用 MD5 哈希口令",
                "suggestion": "改用 bcrypt",
                "owasp": "A02:2021-Cryptographic Failures",
                "cwe": "CWE-327",
                "evidence": "hashlib.md5(pwd)",
                "exploit_scenario": "彩虹表破解",
                "references": [],
                "confidence": 0.9,
            },
        ],
    }, ensure_ascii=False)
    result = parse(text)
    assert len(result.issues) == 1
    issue = result.issues[0]
    # v2 字段正常
    assert issue.cwe == "CWE-327"
    # v3 字段:cvss_score 由 severity 经验值补全(中 -> 5.0)
    assert issue.cvss_score == 5.0
    # compliance_mapping 仍反查
    assert issue.compliance_mapping


def test_parse_legacy_v1_format():
    """v1 格式 JSON(无 v2/v3 字段)仍能解析,所有新字段默认值"""
    text = json.dumps({
        "summary": "代码审查完成",
        "score": 80,
        "issues": [
            {
                "line_number": 1,
                "issue_type": "代码规范",
                "severity": "低",
                "title": "缩进不一致",
                "description": "混合使用空格与 Tab",
                "suggestion": "统一使用 4 空格缩进",
            },
        ],
    }, ensure_ascii=False)
    result = parse(text)
    assert len(result.issues) == 1
    issue = result.issues[0]
    assert issue.owasp == ""
    assert issue.cwe == ""
    assert issue.cvss_score == 0.0
    assert issue.cvss_vector == ""
    assert issue.compliance_mapping == {}
    assert issue.remediation == ""


def test_parse_with_markdown_fence():
    """带 ```json 围栏的输出能正确提取"""
    text = '```json\n{"summary":"x","score":50,"issues":[]}\n```'
    result = parse(text)
    assert result.score == 50
    assert result.issues == []


def test_parse_empty_issues():
    """issues 为空数组时正常返回"""
    text = json.dumps({"summary": "代码无问题", "score": 95, "issues": []})
    result = parse(text)
    assert result.issues == []
    assert result.score == 95


def test_parse_empty_string_raises():
    """空字符串抛出 ResultParseError"""
    from app.ai.exceptions import ResultParseError
    with pytest.raises(ResultParseError):
        parse("")


def test_parse_invalid_json_raises():
    """非法 JSON 抛出 ResultParseError"""
    from app.ai.exceptions import ResultParseError
    with pytest.raises(ResultParseError):
        parse("not a json")


def test_parse_non_object_json_raises():
    """非对象 JSON(数组/字符串)抛出 ResultParseError"""
    from app.ai.exceptions import ResultParseError
    with pytest.raises(ResultParseError):
        parse("[1, 2, 3]")


def test_parse_score_clamped_to_0_100():
    """score 超出 [0, 100] 被裁剪"""
    text = json.dumps({"summary": "x", "score": 150, "issues": []})
    result = parse(text)
    assert result.score == 100

    text2 = json.dumps({"summary": "x", "score": -10, "issues": []})
    result2 = parse(text2)
    assert result2.score == 0


def test_parse_invalid_issue_type_falls_back_to_other():
    """非法 issue_type 回退到 '其他'"""
    text = json.dumps({
        "summary": "x",
        "score": 50,
        "issues": [{"issue_type": "非法类型", "severity": "中", "description": "x"}],
    })
    result = parse(text)
    assert result.issues[0].issue_type == "其他"


def test_parse_invalid_severity_falls_back_to_medium():
    """非法 severity 回退到 '中'"""
    text = json.dumps({
        "summary": "x",
        "score": 50,
        "issues": [{"issue_type": "其他", "severity": "极其严重", "description": "x"}],
    })
    result = parse(text)
    assert result.issues[0].severity == "中"


# ============ static_analyzer Finding v3 字段 ============

def test_severity_to_cvss_score_mapping():
    """severity 到 CVSS 分数经验映射正确"""
    assert _severity_to_cvss_score("严重") == 9.8
    assert _severity_to_cvss_score("高") == 7.5
    assert _severity_to_cvss_score("中") == 5.0
    assert _severity_to_cvss_score("低") == 2.5
    assert _severity_to_cvss_score("未知") == 5.0


def test_cwe_to_cvss_vector_known():
    """已知 CWE 返回预设 CVSS 向量"""
    v = _cwe_to_cvss_vector("CWE-89")
    assert v == "AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H"


def test_cwe_to_cvss_vector_case_insensitive():
    """CWE 编号大小写不敏感"""
    assert _cwe_to_cvss_vector("cwe-89") == _cwe_to_cvss_vector("CWE-89")


def test_cwe_to_cvss_vector_unknown_returns_empty():
    """未知 CWE 返回空字符串"""
    assert _cwe_to_cvss_vector("CWE-9999") == ""
    assert _cwe_to_cvss_vector("") == ""


def test_static_build_compliance_mapping_known_cwe():
    """静态分析模块的合规映射反查与 result_parser 一致"""
    m1 = _build_compliance_mapping("CWE-89")
    m2 = _static_build_compliance_mapping("CWE-89")
    assert m1 == m2


def test_static_build_remediation_with_known_rule():
    """已知规则 code 生成详细修复方案"""
    r = _static_build_remediation(
        "sql_string_concat",
        "改用参数化查询",
        "CWE-89",
    )
    assert "1." in r
    assert "参数化查询" in r
    assert "CWE-89" in r  # 合规备注


def test_static_build_remediation_with_unknown_rule_fallback():
    """未知规则 code 使用 suggestion 兜底生成"""
    r = _static_build_remediation(
        "unknown_rule_xyz",
        "立即修复此问题",
        "CWE-89",
    )
    assert "立即修复此问题" in r
    assert "1." in r


def test_scan_returns_findings_with_v3_fields():
    """scan() 返回的 Finding 含 v3 字段(cvss/compliance/remediation/static_rule_hits)"""
    # SQL f-string 拼接,应命中 sql_string_concat 规则
    content = (
        "import sqlite3\n"
        "def get_user(name):\n"
        "    conn = sqlite3.connect('db.sqlite')\n"
        "    cur = conn.cursor()\n"
        "    cur.execute(f\"SELECT * FROM users WHERE name='{name}'\")\n"
        "    return cur.fetchone()\n"
    )
    findings = scan(content=content, file_name="test.py", language="python")
    assert findings, "应至少命中 1 条静态规则"
    for f in findings:
        assert isinstance(f, Finding)
        # v2 字段
        assert f.cwe, "Finding 应填充 cwe"
        assert f.owasp, "Finding 应填充 owasp"
        assert f.evidence, "Finding 应填充 evidence"
        # v3 字段
        assert f.cvss_score > 0.0, "Finding 应填充 cvss_score"
        assert f.cvss_vector, "Finding 应填充 cvss_vector"
        assert f.compliance_mapping, "Finding 应填充 compliance_mapping"
        assert f.remediation, "Finding 应填充 remediation"
        assert f.static_rule_hits >= 1, "Finding 应填充 static_rule_hits"


def test_scan_empty_content_returns_empty_list():
    """空内容返回空列表"""
    assert scan(content="", file_name="test.py") == []
    assert scan(content="x", file_name="") == []


def test_finding_dataclass_v3_fields_default_values():
    """Finding 数据类 v3 字段默认值合理"""
    f = Finding()
    assert f.cvss_score == 0.0
    assert f.cvss_vector == ""
    assert f.compliance_mapping == {}
    assert f.remediation == ""
    assert f.static_rule_hits == 1


# ============ Issue 数据类 v3 字段 ============

def test_issue_dataclass_v3_fields_default_values():
    """Issue 数据类 v3 字段默认值合理"""
    issue = Issue()
    assert issue.cvss_score == 0.0
    assert issue.cvss_vector == ""
    assert issue.compliance_mapping == {}
    assert issue.remediation == ""


def test_issue_dataclass_v3_fields_assignable():
    """Issue 数据类 v3 字段可赋值"""
    issue = Issue(
        cvss_score=9.8,
        cvss_vector="AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H",
        compliance_mapping={"iso27001": ["A.8.28"]},
        remediation="立即修复",
    )
    assert issue.cvss_score == 9.8
    assert issue.cvss_vector == "AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H"
    assert issue.compliance_mapping == {"iso27001": ["A.8.28"]}
    assert issue.remediation == "立即修复"
