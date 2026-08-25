"""单元测试:静态分析模块(static_analyzer)

验证双引擎之引擎1(静态规则 + 正则秘钥扫描)对漏洞样本的命中能力。
不调用 LLM,纯函数测试。
"""
from __future__ import annotations

from pathlib import Path

from app.ai.security_static_rules import apply_static_rules
from app.ai.static_analyzer import Finding, scan, scan_file
from app.models.code_file import CodeFile

# 漏洞样本目录
VULN_SAMPLES_DIR = Path(__file__).parents[2] / "fixtures" / "vuln_samples"


def _read_sample(name: str) -> str:
    """读取漏洞样本文件内容

    Args:
        name: 样本文件名

    Returns:
        str: 文件内容
    """
    return (VULN_SAMPLES_DIR / name).read_text(encoding="utf-8")


def _make_code_file(file_name: str, content: str, is_binary: int = 0) -> CodeFile:
    """构造内存 CodeFile 对象

    Args:
        file_name: 文件名
        content: 文本内容
        is_binary: 是否二进制

    Returns:
        CodeFile: 未持久化的 ORM 对象
    """
    return CodeFile(
        id=1,
        project_id=1,
        file_name=file_name,
        file_path=file_name,
        language="python",
        content=content,
        size_bytes=len(content),
        line_count=content.count("\n") + 1,
        version_no=1,
        status="active",
        is_binary=is_binary,
    )


# ============ scan() 基础行为 ============

class TestScanBasic:
    """scan() 基础行为测试"""

    def test_scan_empty_content_returns_empty(self):
        """空内容应返回空列表"""
        assert scan(content="", file_name="x.py") == []

    def test_scan_empty_filename_returns_empty(self):
        """空文件名应返回空列表"""
        assert scan(content="x = 1", file_name="") == []

    def test_scan_normal_code_returns_empty(self):
        """正常代码不应触发任何规则"""
        code = "x = 1\ny = 2\nprint(x + y)\n"
        assert scan(content=code, file_name="normal.py") == []

    def test_scan_returns_finding_instances(self):
        """命中时应返回 Finding 数据类实例"""
        code = 'password = "SuperSecret123!"\n'
        findings = scan(content=code, file_name="secrets.py")
        assert len(findings) >= 1
        assert isinstance(findings[0], Finding)


# ============ 正则秘钥扫描 ============

class TestSecretScanning:
    """正则秘钥扫描测试"""

    def test_hardcoded_aws_key_detected(self):
        """硬编码 AWS Access Key 应被命中"""
        code = 'AWS_KEY = "AKIAIOSFODNN7EXAMPLE"\n'
        findings = scan(content=code, file_name="config.py")
        aws_findings = [f for f in findings if "AWS" in f.title or "AKIA" in f.evidence]
        assert len(aws_findings) >= 1
        assert aws_findings[0].cwe == "CWE-798"
        assert aws_findings[0].source == "regex"
        assert aws_findings[0].confidence >= 0.99

    def test_hardcoded_password_detected(self):
        """硬编码密码应被命中"""
        code = 'password = "SuperSecret123!"\n'
        findings = scan(content=code, file_name="config.py")
        pwd_findings = [f for f in findings if "密码" in f.title or "Password" in f.title]
        assert len(pwd_findings) >= 1

    def test_database_url_with_credentials_detected(self):
        """含明文凭据的数据库连接串应被命中"""
        code = 'DATABASE_URL = "postgresql://admin:admin123@10.0.0.1:5432/db"\n'
        findings = scan(content=code, file_name="config.py")
        db_findings = [f for f in findings if "数据库" in f.title or "Database" in f.title]
        assert len(db_findings) >= 1

    def test_secret_evidence_is_redacted(self):
        """命中秘钥的 evidence 字段应被脱敏"""
        code = 'AWS_KEY = "AKIAIOSFODNN7EXAMPLE"\n'
        findings = scan(content=code, file_name="config.py")
        for f in findings:
            if "AWS" in f.title or "AKIA" in f.evidence:
                assert "AKIAIOSFODNN7EXAMPLE" not in f.evidence


# ============ 静态语义规则 ============

class TestStaticRules:
    """静态语义规则测试"""

    def test_sql_injection_fstring_detected(self):
        """SQL f-string 拼接应被命中"""
        code = 'query = f"SELECT * FROM users WHERE id={user_id}"\n'
        findings = scan(content=code, file_name="sqli.py")
        sql_findings = [f for f in findings if f.cwe == "CWE-89"]
        assert len(sql_findings) >= 1
        assert sql_findings[0].source == "static"

    def test_sql_injection_plus_concat_execute_detected(self):
        """v3 补丁验证:cursor.execute("SELECT..." + var) + 拼接应被命中"""
        code = 'cursor.execute("SELECT * FROM users WHERE id=" + user_id)\n'
        findings = scan(content=code, file_name="sqli_plus.py")
        sql_findings = [f for f in findings if f.cwe == "CWE-89"]
        assert len(sql_findings) >= 1
        assert sql_findings[0].source == "static"

    def test_sql_injection_plus_concat_query_assignment_detected(self):
        """v3 补丁验证:query = "SELECT..." + var 赋值拼接应被命中"""
        code = 'query = "SELECT * FROM users" + condition\n'
        findings = scan(content=code, file_name="sqli_plus2.py")
        sql_findings = [f for f in findings if f.cwe == "CWE-89"]
        assert len(sql_findings) >= 1

    def test_sql_injection_plus_concat_var_first_detected(self):
        """v3 补丁验证:query = var + " WHERE..." 变量在前的拼接应被命中"""
        code = 'sql = base_query + " WHERE id=1"\n'
        findings = scan(content=code, file_name="sqli_plus3.py")
        sql_findings = [f for f in findings if f.cwe == "CWE-89"]
        assert len(sql_findings) >= 1

    def test_sql_injection_plus_concat_no_false_positive_on_non_sql(self):
        """v3 补丁验证:普通字符串 + 拼接(无 SQL 关键字)不应命中 SQL 规则"""
        code = 'msg = "Hello " + name\n'
        findings = scan(content=code, file_name="normal.py")
        sql_findings = [f for f in findings if f.cwe == "CWE-89"]
        assert len(sql_findings) == 0

    def test_pickle_loads_detected(self):
        """pickle.loads 应被命中"""
        code = "import pickle\npickle.loads(data)\n"
        findings = scan(content=code, file_name="deser.py")
        pickle_findings = [f for f in findings if f.cwe == "CWE-502"]
        assert len(pickle_findings) >= 1
        assert pickle_findings[0].severity == "严重"

    def test_eval_user_input_detected(self):
        """eval 调用应被命中"""
        code = "eval(user_input)\n"
        findings = scan(content=code, file_name="eval.py")
        eval_findings = [f for f in findings if f.cwe == "CWE-95"]
        assert len(eval_findings) >= 1

    def test_weak_md5_detected(self):
        """hashlib.md5 应被命中"""
        code = "import hashlib\nhashlib.md5(password.encode())\n"
        findings = scan(content=code, file_name="crypto.py")
        md5_findings = [f for f in findings if f.cwe == "CWE-327" and "MD5" in f.title]
        assert len(md5_findings) >= 1


# ============ scan_file() 便利方法 ============

class TestScanFile:
    """scan_file() ORM 便利方法测试"""

    def test_scan_file_binary_returns_empty(self):
        """二进制文件应跳过静态扫描"""
        f = _make_code_file("x.png", "", is_binary=1)
        assert scan_file(f) == []

    def test_scan_file_none_returns_empty(self):
        """None 输入应返回空列表"""
        assert scan_file(None) == []

    def test_scan_file_works_with_orm(self):
        """CodeFile ORM 对象应正常工作"""
        f = _make_code_file("secrets.py", 'password = "SuperSecret123!"\n')
        findings = scan_file(f)
        assert len(findings) >= 1


# ============ 漏洞样本集成测试 ============

class TestVulnSamples:
    """对 7 个漏洞样本的集成测试"""

    def test_sqli_sample_has_findings(self):
        """SQL 注入样本应被静态规则命中"""
        findings = scan(content=_read_sample("sqli_python.py"), file_name="sqli_python.py")
        assert len(findings) >= 1
        assert any(f.cwe == "CWE-89" for f in findings)

    def test_hardcoded_secrets_sample_has_multiple_findings(self):
        """硬编码密钥样本应命中至少 3 处"""
        findings = scan(
            content=_read_sample("hardcoded_secrets.py"),
            file_name="hardcoded_secrets.py",
        )
        assert len(findings) >= 3

    def test_path_traversal_sample_has_findings(self):
        """路径遍历样本应被静态规则命中"""
        findings = scan(
            content=_read_sample("path_traversal_python.py"),
            file_name="path_traversal_python.py",
        )
        assert len(findings) >= 1

    def test_deserialization_sample_has_findings(self):
        """反序列化样本应被静态规则命中"""
        findings = scan(
            content=_read_sample("deserialization_python.py"),
            file_name="deserialization_python.py",
        )
        assert any(f.cwe == "CWE-502" for f in findings)

    def test_total_findings_across_samples_at_least_5(self):
        """所有样本合计应命中至少 5 个漏洞"""
        samples = [
            "sqli_python.py",
            "xss_javascript.js",
            "hardcoded_secrets.py",
            "path_traversal_python.py",
            "deserialization_python.py",
            "ssrf_python.py",
            "command_injection_python.py",
        ]
        total = 0
        for name in samples:
            findings = scan(content=_read_sample(name), file_name=name)
            total += len(findings)
        assert total >= 5, f"静态分析仅命中 {total} 个漏洞,期望至少 5 个"


# ============ Finding 字段完整性 ============

class TestFindingFields:
    """Finding 数据类字段完整性测试"""

    def test_finding_has_all_required_fields(self):
        """每个 Finding 应填充所有必需字段"""
        code = 'password = "SuperSecret123!"\n'
        findings = scan(content=code, file_name="config.py")
        for f in findings:
            assert f.line_number > 0
            assert f.severity in ("严重", "高", "中", "低")
            assert f.title
            assert f.description
            assert f.suggestion
            assert f.owasp
            assert f.cwe
            assert f.evidence
            assert f.exploit_scenario
            assert isinstance(f.references, list)
            assert f.confidence > 0
            assert f.source in ("static", "regex", "llm")

    def test_finding_references_contain_cwe_url(self):
        """references 应包含 CWE 链接"""
        code = "import pickle\npickle.loads(data)\n"
        findings = scan(content=code, file_name="deser.py")
        pickle_findings = [f for f in findings if f.cwe == "CWE-502"]
        assert len(pickle_findings) >= 1
        cwe_url = "https://cwe.mitre.org/data/definitions/502.html"
        assert cwe_url in pickle_findings[0].references


class TestParamRoutingRules:
    """参数路由规则(用户输入直连危险操作,源自渗透工作流参数路由表)回归。"""

    def test_orderby_raw_concat_hits(self):
        hits = apply_static_rules('db.execute("SELECT * FROM t ORDER BY " + sortField)', "a.py")
        assert any(m.rule_code == "orderby_raw_concat" for m in hits)

    def test_user_url_fetch_hits(self):
        hits = apply_static_rules("resp = requests.get(url)", "a.py")
        assert any(m.rule_code == "user_url_fetch" for m in hits)

    def test_path_param_file_read_hits(self):
        hits = apply_static_rules("data = open(file_path).read()", "a.py")
        assert any(m.rule_code == "path_param_file_read" for m in hits)

    def test_sensitive_to_log_hits(self):
        hits = apply_static_rules('logger.info(f"login token={token}")', "a.py")
        assert any(m.rule_code == "sensitive_to_log" for m in hits)

    def test_role_from_request_hits(self):
        hits = apply_static_rules('role = request.json["role"]', "a.py")
        assert any(m.rule_code == "role_from_request" for m in hits)

    def test_password_plaintext_compare_hits(self):
        hits = apply_static_rules("if password == userInput: login()", "a.py")
        assert any(m.rule_code == "password_plaintext_compare" for m in hits)

    def test_clean_code_no_false_positive(self):
        clean = "import os\n\n\ndef compute(value: int) -> int:\n    return value * 2\n"
        assert apply_static_rules(clean, "clean.py") == []
