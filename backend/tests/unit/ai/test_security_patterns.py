"""单元测试 (v2.1): 敏感信息正则库"""
from app.ai.security_patterns import (
    _redact_value,
    get_pattern,
    list_patterns,
    scan_secrets,
)


def test_redact_value_short_string_fully_masked():
    assert _redact_value("abc") == "***"
    assert _redact_value("12345678") == "********"


def test_redact_value_long_string_keeps_head_and_tail():
    out = _redact_value("sk-proj-abcdefghijklmnop")
    assert out.startswith("sk-p")
    assert out.endswith("mnop")
    assert "*" in out
    assert len(out) == 12


def test_scan_secrets_bounds_million_character_token_evidence():
    matches = scan_secrets("sk-" + ("A" * 1_000_000))

    target = next(match for match in matches if match.pattern_name == "OpenAI API Key")
    assert len(target.matched_text) < 600
    assert len(target.evidence_redacted) == 12


def test_scan_secrets_detects_openai_key():
    code = 'OPENAI_KEY = "sk-proj-AbCdEf1234567890XyZqWeRtYu"'
    matches = scan_secrets(code)
    names = {m.pattern_name for m in matches}
    assert "OpenAI API Key" in names


def test_scan_secrets_detects_aws_access_key():
    code = 'export AWS_ACCESS_KEY_ID=AKIAIOSFODNN7EXAMPLE'
    matches = scan_secrets(code)
    assert any(m.pattern_name == "AWS Access Key" for m in matches)


def test_scan_secrets_detects_rsa_private_key():
    code = (
        "-----BEGIN RSA PRIVATE KEY-----\n"
        "MIIEpAIBAAKCAQEA...\n"
        "-----END RSA PRIVATE KEY-----"
    )
    matches = scan_secrets(code)
    assert any(m.pattern_name == "RSA Private Key" for m in matches)


def test_scan_secrets_detects_jwt_token():
    code = (
        'TOKEN = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.'
        'eyJzdWIiOiIxMjMiLCJuYW1lIjoiSm9obiJ9.'
        'SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"'
    )
    matches = scan_secrets(code)
    assert any(m.pattern_name == "JWT Token" for m in matches)


def test_scan_secrets_detects_hardcoded_password():
    code = 'password = "Sup3rS3cret!"'
    matches = scan_secrets(code)
    assert any(m.pattern_name == "Hardcoded Password" for m in matches)


def test_scan_secrets_detects_db_password_underscore_prefix():
    """v3 补丁验证:DB_PASSWORD 大写蛇形命名应被命中(原正则 [^A-Za-z0-9_] 漏报)"""
    code = 'DB_PASSWORD = "mysecret123"'
    matches = scan_secrets(code)
    assert any(m.pattern_name == "Hardcoded Password" for m in matches)


def test_scan_secrets_detects_user_password_underscore_prefix():
    """v3 补丁验证:user_password 小写蛇形命名应被命中"""
    code = 'user_password = "anothersecret456"'
    matches = scan_secrets(code)
    assert any(m.pattern_name == "Hardcoded Password" for m in matches)


def test_scan_secrets_does_not_false_positive_on_mypassword():
    """mypassword 整体作为一个标识符,password 前面是字母 p,不应命中(避免误报)"""
    code = 'mypassword = "notmatched"'
    matches = scan_secrets(code)
    # mypassword 不应被识别为 Hardcoded Password(password 前是字母 p)
    assert not any(m.pattern_name == "Hardcoded Password" for m in matches)


def test_scan_secrets_detects_database_url():
    code = 'DB_URL = "postgresql://admin:s3cret@db.example.com:5432/app"'
    matches = scan_secrets(code)
    assert any(m.pattern_name == "Database URL with Credentials" for m in matches)


def test_scan_secrets_returns_line_number():
    code = "# header\n" + 'API_KEY = "sk-proj-abcdefghijklmnopqrstuvwx"\n'
    matches = scan_secrets(code)
    target = next(m for m in matches if m.pattern_name == "OpenAI API Key")
    assert target.line_number == 2


def test_scan_secrets_evidence_is_redacted():
    code = 'sk-proj-superSecretLongValueAbcDefGhi12345'
    matches = scan_secrets(code)
    assert matches, "should find at least one match"
    for m in matches:
        # evidence 一定不能等于原始匹配文本
        assert m.evidence_redacted != m.matched_text
        # 一定要包含掩码
        assert "*" in m.evidence_redacted


def test_scan_secrets_empty_input_returns_empty_list():
    assert scan_secrets("") == []
    assert scan_secrets("# only a comment\nx = 1") == []


def test_scan_secrets_does_not_false_positive_on_random_base64():
    """普通的 base64 不应被识别为 AWS / GitHub token"""
    code = 'data = "VGhpcyBpcyBhIHRlc3Qgc3RyaW5n"'
    matches = scan_secrets(code)
    # 允许命中 Generic API Key 之类(分组形式),但不应命中 AWS/GitHub
    forbidden = {"AWS Access Key", "GitHub Personal Token"}
    assert not any(m.pattern_name in forbidden for m in matches)


def test_list_patterns_returns_metadata():
    patterns = list_patterns()
    assert len(patterns) >= 10
    sample = patterns[0]
    assert "name" in sample and "cwe" in sample and "owasp" in sample


def test_get_pattern_by_name():
    p = get_pattern("OpenAI API Key")
    assert p is not None
    assert p.cwe == "CWE-798"
