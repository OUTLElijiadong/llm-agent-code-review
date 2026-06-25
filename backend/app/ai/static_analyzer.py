"""
静态分析模块(双引擎之引擎1)

确定性漏洞规则 + 正则秘钥扫描,无 LLM 调用。
作为代码审查主流程的前置过滤引擎,先命中确定性问题再交由 LLM 深度审查。

设计要点:
1. 纯函数,无 LLM 调用,无 DB 写入,可独立测试
2. 复用 security_patterns.scan_secrets() 与 security_static_rules.apply_static_rules()
3. 输出标准化 Finding 数据类,字段对齐 result_parser.Issue 与 ReviewIssue 模型
4. 每个 Finding 强制填充 owasp/cwe/evidence/exploit_scenario/references/confidence/source
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional

from app.ai.security_patterns import SecretMatch, scan_secrets
from app.ai.security_static_rules import StaticMatch, apply_static_rules


@dataclass
class Finding:
    """标准化漏洞发现数据类

    与 result_parser.Issue 字段对齐,可直接转换为 ReviewIssue ORM 对象。

    Attributes:
        line_number: 问题起始行号(1-based;0 表示文件级)
        end_line: 问题结束行号(可等于 line_number)
        issue_type: 问题类型枚举(此处固定为"安全漏洞")
        severity: 严重/高/中/低
        title: 问题标题(≤30 字)
        description: 中文描述(30-200 字)
        suggestion: 中文修复建议(30-200 字)
        fixed_code: 修复代码片段(可空)
        owasp: OWASP 编号,如 A03:2021-Injection
        cwe: CWE 编号,如 CWE-89
        evidence: 漏洞证据代码片段(1-3 行,从源码直接复制)
        exploit_scenario: 攻击场景说明(30-200 字)
        references: 参考链接列表
        confidence: 置信度 0.0-1.0(静态/regex 命中 ≥0.95)
        source: 发现来源(static/regex/llm)
    """
    line_number: int = 0
    end_line: Optional[int] = None
    issue_type: str = "安全漏洞"
    severity: str = "中"
    title: str = ""
    description: str = ""
    suggestion: str = ""
    fixed_code: str = ""
    owasp: str = ""
    cwe: str = ""
    evidence: str = ""
    exploit_scenario: str = ""
    references: List[str] = field(default_factory=list)
    confidence: float = 0.95
    source: str = "static"


def scan(*, content: str, file_name: str, language: Optional[str] = None) -> List[Finding]:
    """对单个文件应用静态规则 + 正则秘钥扫描

    Args:
        content: 文件文本内容(UTF-8)
        file_name: 文件名(含扩展名),决定哪些规则适用
        language: 可选语言标识(未提供则由 file_name 推断)

    Returns:
        List[Finding]: 标准化漏洞发现列表(可能为空)

    Note:
        纯函数,不调用 LLM,不写数据库。失败时返回空列表,不抛异常。
    """
    if not content or not file_name:
        return []

    findings: List[Finding] = []

    # === 引擎 1a: 正则秘钥扫描 ===
    try:
        for m in scan_secrets(content):
            findings.append(_secret_to_finding(m))
    except Exception:
        # 静态分析失败不阻断主流程,LLM 审查照常进行
        pass

    # === 引擎 1b: 静态语义规则 ===
    try:
        for m in apply_static_rules(content, file_name):
            findings.append(_static_to_finding(m))
    except Exception:
        pass

    return findings


def scan_file(code_file) -> List[Finding]:
    """对 CodeFile ORM 对象应用静态扫描(便利方法)

    Args:
        code_file: CodeFile ORM 对象(需有 content/file_name/language 属性)

    Returns:
        List[Finding]: 标准化漏洞发现列表
    """
    if code_file is None:
        return []
    # binary 文件不参与静态扫描
    if getattr(code_file, "is_binary", 0) == 1:
        return []
    return scan(
        content=code_file.content or "",
        file_name=code_file.file_name or "",
        language=getattr(code_file, "language", None),
    )


# ============ 内部转换函数 ============

def _secret_to_finding(m: SecretMatch) -> Finding:
    """将正则秘钥扫描结果转换为 Finding

    Args:
        m: SecretMatch 命中对象

    Returns:
        Finding: 标准化漏洞发现
    """
    pattern_name = m.pattern_name
    return Finding(
        line_number=m.line_number,
        end_line=m.line_number,
        issue_type="安全漏洞",
        severity="严重",
        title=f"硬编码 {pattern_name}",
        description=(
            f"第 {m.line_number} 行检测到 {m.description}。"
            f"硬编码凭据一旦随代码或日志泄露,攻击者可直接复用该凭据访问对应服务,"
            f"造成数据泄露、资源盗用或权限提升。"
        ),
        suggestion=(
            "请立即从代码中移除该凭据,改从环境变量、密钥管理服务(如 AWS Secrets Manager、"
            "HashiCorp Vault)或 Kubernetes Secret 读取;同时应轮换已泄露的凭据。"
        ),
        fixed_code="",
        owasp=m.owasp,
        cwe=m.cwe,
        evidence=m.evidence_redacted,
        exploit_scenario=(
            f"攻击者获取代码仓库访问权(如代码推送、备份泄露、日志记录)后,"
            f"可直接提取该 {pattern_name} 并在公网服务上重放,绕过所有认证。"
        ),
        references=_build_references(m.cwe, m.owasp),
        confidence=0.99,
        source="regex",
    )


def _static_to_finding(m: StaticMatch) -> Finding:
    """将静态语义规则命中结果转换为 Finding

    Args:
        m: StaticMatch 命中对象

    Returns:
        Finding: 标准化漏洞发现
    """
    return Finding(
        line_number=m.line_number,
        end_line=m.line_number,
        issue_type="安全漏洞",
        severity=m.severity,
        title=m.rule_name,
        description=m.description,
        suggestion=m.fix_suggestion,
        fixed_code="",
        owasp=m.owasp,
        cwe=m.cwe,
        evidence=m.evidence_line,
        exploit_scenario=_build_exploit_scenario(m),
        references=_build_references(m.cwe, m.owasp),
        confidence=0.95,
        source="static",
    )


def _build_exploit_scenario(m: StaticMatch) -> str:
    """根据规则 code 生成攻击场景说明

    Args:
        m: StaticMatch 命中对象

    Returns:
        str: 30-200 字攻击场景描述
    """
    scenarios = {
        "weak_md5": "攻击者可对泄露的 MD5 哈希使用彩虹表或碰撞攻击,在分钟级还原原密码。",
        "weak_sha1": "攻击者可构造碰撞文档绕过基于 SHA-1 的签名校验。",
        "weak_des": "攻击者可在数小时内穷举破解 56 位 DES 密钥,解密全部密文。",
        "aes_ecb_mode": "攻击者通过密文块重复模式可推断明文结构,泄露加密数据规律。",
        "ssl_verify_disabled": "中间人攻击者可伪造证书拦截加密通信,窃取凭据或注入恶意数据。",
        "pickle_load": "攻击者构造恶意 pickle 数据,反序列化时执行任意代码,直接 RCE。",
        "eval_user_input": "攻击者通过输入注入 Python/JS 代码,服务端执行后可读写文件、命令执行。",
        "insecure_random": "攻击者可预测随机数序列,伪造 token 或绕过 CSRF/会话防护。",
        "sql_string_concat": "攻击者通过参数注入 ' OR 1=1 -- 绕过认证或拖库。",
        "xxe_processing": "攻击者构造含外部实体的 XML,读取服务器任意文件或发起 SSRF。",
        "open_redirect": "攻击者构造恶意跳转链接进行钓鱼,诱骗用户访问仿冒站点。",
        "path_traversal_user_input": "攻击者通过 ../ 序列读取服务器任意文件(如 /etc/passwd)。",
        "log_injection": "攻击者注入换行符伪造日志条目,污染审计追踪或注入终端控制字符。",
        "cookie_no_httponly": "XSS 攻击者可通过 document.cookie 窃取该 Cookie,劫持会话。",
        "cookie_no_secure": "中间人可在 HTTP 降级时嗅探该 Cookie,劫持会话。",
        "missing_hsts": "中间人可降级 HTTPS 到 HTTP,在首次访问时劫持连接。",
        "missing_csp": "XSS 攻击者可加载外部恶意脚本,放大 XSS 影响面。",
        "missing_x_frame_options": "攻击者用 iframe 嵌入本页面,诱骗用户点击(点击劫持)。",
        "missing_x_content_type_options": "浏览器可能将文本响应识别为可执行脚本,放大 XSS。",
        "cors_wildcard_with_credentials": "任何外部站点可携带用户凭据发起跨域请求,绕过同源策略。",
    }
    return scenarios.get(m.rule_code, m.description)


def _build_references(cwe: str, owasp: str) -> List[str]:
    """根据 CWE/OWASP 编号生成参考链接

    Args:
        cwe: CWE 编号,如 CWE-89
        owasp: OWASP 编号,如 A03:2021-Injection

    Returns:
        List[str]: 参考链接 URL 列表
    """
    refs: List[str] = []
    if cwe:
        cwe_num = cwe.replace("CWE-", "")
        refs.append(f"https://cwe.mitre.org/data/definitions/{cwe_num}.html")
    if owasp:
        refs.append("https://owasp.org/Top10/")
    return refs
