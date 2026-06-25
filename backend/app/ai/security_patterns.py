"""确定性敏感信息正则库 (v2.1)

为 SecuritySentinelAgent 提供"不依赖 LLM 也能确定命中"的硬编码秘钥扫描。
参考 TruffleHog / Gitleaks 公开规则集精简,精准覆盖中小型项目最常见的 10+ 类。

设计原则:
1. 召回优先于精确——硬编码秘钥一律 severity="严重"
2. evidence 字段必须截短并脱敏(前 4 + 后 4 字符,中间打码)
3. 不与 ai_prompt_agent._redact 互相依赖,保持各自完整
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List, Optional, Pattern


@dataclass(frozen=True)
class SecretPattern:
    """单条敏感信息正则"""

    name: str
    cwe: str
    owasp: str
    regex: Pattern[str]
    description: str
    min_length: int = 0


# 注: 各正则末尾允许 0 个或多个空白以容错复制粘贴,但不允许跨行匹配
_PATTERNS: tuple[SecretPattern, ...] = (
    SecretPattern(
        name="OpenAI API Key",
        cwe="CWE-798",
        owasp="A02:2021-Cryptographic Failures",
        regex=re.compile(r"sk-(?:proj-)?[A-Za-z0-9_\-]{20,}"),
        description="OpenAI 平台 API 密钥",
    ),
    SecretPattern(
        name="AWS Access Key",
        cwe="CWE-798",
        owasp="A07:2021-Identification and Authentication Failures",
        regex=re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
        description="AWS IAM 访问密钥 ID",
    ),
    SecretPattern(
        name="AWS Secret Access Key",
        cwe="CWE-798",
        owasp="A07:2021-Identification and Authentication Failures",
        regex=re.compile(
            r"""(?ix)
            (?:aws[_-]?(?:secret|sk))\s*[:=]\s*["']?
            ([A-Za-z0-9/+=]{40})
            ["']?
            """,
        ),
        description="AWS Secret Access Key",
    ),
    SecretPattern(
        name="GitHub Personal Token",
        cwe="CWE-798",
        owasp="A07:2021-Identification and Authentication Failures",
        regex=re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}\b"),
        description="GitHub Personal Access Token",
    ),
    SecretPattern(
        name="JWT Token",
        cwe="CWE-522",
        owasp="A02:2021-Cryptographic Failures",
        regex=re.compile(
            r"\beyJ[A-Za-z0-9_\-]{8,}\.eyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\b",
        ),
        description="JSON Web Token (含 header.payload.signature 三段)",
    ),
    SecretPattern(
        name="RSA Private Key",
        cwe="CWE-321",
        owasp="A02:2021-Cryptographic Failures",
        regex=re.compile(
            r"-----BEGIN (?:RSA |OPENSSH |EC |DSA |PGP )?PRIVATE KEY-----",
        ),
        description="非对称加密私钥 PEM 头部",
    ),
    SecretPattern(
        name="Hardcoded Password",
        cwe="CWE-259",
        owasp="A07:2021-Identification and Authentication Failures",
        regex=re.compile(
            r"""(?ix)
            # v3 补丁: 允许 _ 前缀,覆盖 DB_PASSWORD / USER_PASSWORD 等大写蛇形命名
            # 原 [^A-Za-z0-9_] 会拒绝 _ 前缀,导致 DB_PASSWORD = "xxx" 漏报
            (?:^|[^A-Za-z0-9])(?:password|passwd|pwd)\s*[:=]\s*
            ["']([^"'\s]{6,})["']
            """,
        ),
        description="硬编码明文密码赋值(含 DB_PASSWORD / USER_PASSWORD 等蛇形命名)",
    ),
    SecretPattern(
        name="Generic API Key",
        cwe="CWE-798",
        owasp="A02:2021-Cryptographic Failures",
        regex=re.compile(
            r"""(?ix)
            (?:^|[^A-Za-z0-9_])(?:api[_-]?key|access[_-]?token|secret[_-]?key)\s*[:=]\s*
            ["']([A-Za-z0-9_\-]{16,})["']
            """,
        ),
        description="通用 API Key/Access Token/Secret Key 硬编码",
    ),
    SecretPattern(
        name="Slack Bot Token",
        cwe="CWE-798",
        owasp="A07:2021-Identification and Authentication Failures",
        regex=re.compile(r"\bxox[baprs]-[A-Za-z0-9\-]{10,}\b"),
        description="Slack Bot / App Token",
    ),
    SecretPattern(
        name="Google API Key",
        cwe="CWE-798",
        owasp="A02:2021-Cryptographic Failures",
        regex=re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b"),
        description="Google API Key",
    ),
    SecretPattern(
        name="Stripe Secret Key",
        cwe="CWE-798",
        owasp="A02:2021-Cryptographic Failures",
        regex=re.compile(r"\b(?:sk|rk)_(?:test|live)_[A-Za-z0-9]{24,}\b"),
        description="Stripe 支付密钥",
    ),
    SecretPattern(
        name="Database URL with Credentials",
        cwe="CWE-522",
        owasp="A02:2021-Cryptographic Failures",
        regex=re.compile(
            r"\b(?:mysql|postgres(?:ql)?|mongodb)://[^\s:@]+:[^\s:@/]+@[^\s/]+",
        ),
        description="包含明文账号密码的数据库连接串",
    ),
    # ===== v2.1.1 扩展: 8 条新规则 =====
    SecretPattern(
        name="GitLab Personal Token",
        cwe="CWE-798",
        owasp="A07:2021-Identification and Authentication Failures",
        regex=re.compile(r"\bglpat-[A-Za-z0-9_\-]{20,}\b"),
        description="GitLab Personal Access Token",
    ),
    SecretPattern(
        name="Heroku API Key",
        cwe="CWE-798",
        owasp="A07:2021-Identification and Authentication Failures",
        regex=re.compile(
            r"""(?ix)
            heroku.{0,30}["']([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})["']
            """,
        ),
        description="Heroku API Key (UUID 形态)",
    ),
    SecretPattern(
        name="Discord Bot Token",
        cwe="CWE-798",
        owasp="A02:2021-Cryptographic Failures",
        regex=re.compile(
            r"\b[MN][A-Za-z0-9_-]{23}\.[A-Za-z0-9_-]{6}\.[A-Za-z0-9_-]{27}\b",
        ),
        description="Discord Bot Token",
    ),
    SecretPattern(
        name="Telegram Bot Token",
        cwe="CWE-798",
        owasp="A02:2021-Cryptographic Failures",
        regex=re.compile(r"\b\d{8,10}:[A-Za-z0-9_-]{35}\b"),
        description="Telegram Bot Token",
    ),
    SecretPattern(
        name="SendGrid API Key",
        cwe="CWE-798",
        owasp="A07:2021-Identification and Authentication Failures",
        regex=re.compile(r"\bSG\.[A-Za-z0-9_\-]{22}\.[A-Za-z0-9_\-]{43}\b"),
        description="SendGrid 邮件服务 API Key",
    ),
    SecretPattern(
        name="Twilio API Key",
        cwe="CWE-798",
        owasp="A07:2021-Identification and Authentication Failures",
        regex=re.compile(r"\bSK[a-z0-9]{32}\b"),
        description="Twilio API Key SID",
    ),
    SecretPattern(
        name="Mailgun API Key",
        cwe="CWE-798",
        owasp="A07:2021-Identification and Authentication Failures",
        regex=re.compile(r"\bkey-[a-z0-9]{32}\b"),
        description="Mailgun 邮件 API Key",
    ),
    SecretPattern(
        name="Square OAuth Secret",
        cwe="CWE-798",
        owasp="A07:2021-Identification and Authentication Failures",
        regex=re.compile(r"\bsq0(?:at|cs)p-[A-Za-z0-9_\-]{22,43}\b"),
        description="Square OAuth Access/Client Secret",
    ),
)


@dataclass
class SecretMatch:
    """单条匹配结果"""

    pattern_name: str
    cwe: str
    owasp: str
    description: str
    line_number: int
    matched_text: str
    evidence_redacted: str


def _redact_value(text: str, head: int = 4, tail: int = 4) -> str:
    """保留前 head + 后 tail 字符,中间打码"""
    if len(text) <= head + tail:
        return "*" * len(text)
    return f"{text[:head]}{'*' * max(4, len(text) - head - tail)}{text[-tail:]}"


def scan_secrets(content: str, max_per_pattern: int = 20) -> List[SecretMatch]:
    """扫描代码内容中的硬编码敏感信息

    Args:
        content: 待扫描代码文本
        max_per_pattern: 单条规则最多保留命中数,防止规模化文本污染结果

    Returns:
        list[SecretMatch]: 命中结果列表(可能为空)
    """
    if not content:
        return []

    matches: List[SecretMatch] = []
    lines = content.splitlines()
    for pattern in _PATTERNS:
        hits = 0
        for line_no, line_text in enumerate(lines, start=1):
            if hits >= max_per_pattern:
                break
            m = pattern.regex.search(line_text)
            if not m:
                continue
            # group(1) 可能是被引号包裹的实际值;若无分组则用整体匹配
            try:
                raw = m.group(1)
            except IndexError:
                raw = m.group(0)
            if not raw or len(raw) < pattern.min_length:
                continue
            matches.append(
                SecretMatch(
                    pattern_name=pattern.name,
                    cwe=pattern.cwe,
                    owasp=pattern.owasp,
                    description=pattern.description,
                    line_number=line_no,
                    matched_text=raw,
                    evidence_redacted=_redact_value(raw),
                ),
            )
            hits += 1
    return matches


def list_patterns() -> List[dict]:
    """列出所有已加载的正则规则(供检查清单接口使用)"""
    return [
        {
            "name": p.name,
            "cwe": p.cwe,
            "owasp": p.owasp,
            "description": p.description,
        }
        for p in _PATTERNS
    ]


def get_pattern(name: str) -> Optional[SecretPattern]:
    """按名称查询规则,供测试使用"""
    for p in _PATTERNS:
        if p.name == name:
            return p
    return None
