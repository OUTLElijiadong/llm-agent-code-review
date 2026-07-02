"""
静态分析模块(双引擎之引擎1)

确定性漏洞规则 + 正则秘钥扫描,无 LLM 调用。
作为代码审查主流程的前置过滤引擎,先命中确定性问题再交由 LLM 深度审查。

设计要点:
1. 纯函数,无 LLM 调用,无 DB 写入,可独立测试
2. 复用 security_patterns.scan_secrets() 与 security_static_rules.apply_static_rules()
3. 输出标准化 Finding 数据类,字段对齐 result_parser.Issue 与 ReviewIssue 模型
4. 每个 Finding 强制填充 owasp/cwe/evidence/exploit_scenario/references/confidence/source
5. v3 扩展:同时填充 cvss_score/cvss_vector/compliance_mapping/remediation/static_rule_hits
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional

from app.ai.security_patterns import SecretMatch, scan_secrets
from app.ai.security_static_rules import StaticMatch, apply_static_rules
from app.constants.compliance import get_compliance_mapping

# === 严重度到 CVSS 基础分的经验映射(覆盖静态规则的常见漏洞类型) ===
_SEVERITY_TO_CVSS: Dict[str, float] = {
    "严重": 9.8,
    "高": 7.5,
    "中": 5.0,
    "低": 2.5,
}

# === CWE → CVSS 向量模板(基于该 CWE 的典型攻击路径预设) ===
_CWE_TO_CVSS_VECTOR: Dict[str, str] = {
    "CWE-89": "AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H",        # SQL 注入
    "CWE-78": "AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H",        # 命令注入
    "CWE-79": "AV:N/AC:L/PR:N/UI:R/S:U/C:H/I:L/A:N",        # XSS
    "CWE-918": "AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:L/A:N",       # SSRF
    "CWE-352": "AV:N/AC:L/PR:N/UI:R/S:U/C:H/I:H/A:N",       # CSRF
    "CWE-502": "AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H",       # 反序列化
    "CWE-22": "AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:N/A:N",        # 路径遍历
    "CWE-639": "AV:N/AC:L/PR:L/UI:N/S:U/C:H/I:H/A:N",       # 越权
    "CWE-798": "AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H",       # 硬编码凭据
    "CWE-327": "AV:N/AC:H/PR:N/UI:N/S:U/C:H/I:N/A:N",       # 弱加密
    "CWE-522": "AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:N",       # 弱口令保护
}


def _severity_to_cvss_score(severity: str) -> float:
    """根据严重度返回 CVSS 基础分经验值

    Args:
        severity: 严重/高/中/低

    Returns:
        float: 0.0-10.0 之间的浮点数;未知严重度返回 5.0
    """
    return _SEVERITY_TO_CVSS.get(severity, 5.0)


def _cwe_to_cvss_vector(cwe: str) -> str:
    """根据 CWE 编号返回预设的 CVSS 向量字符串

    Args:
        cwe: CWE 编号,如 CWE-89

    Returns:
        str: CVSS v3.1 向量字符串;未匹配返回空字符串
    """
    if not cwe:
        return ""
    return _CWE_TO_CVSS_VECTOR.get(cwe.upper(), "")


def _build_compliance_mapping(cwe: str) -> Dict[str, List[str]]:
    """基于 CWE 编号反查 4 大合规标准映射

    Args:
        cwe: CWE 编号,如 CWE-89

    Returns:
        Dict[str, List[str]]: 合规映射字典;未命中时返回空字典
    """
    if not cwe:
        return {}
    try:
        mapping = get_compliance_mapping(cwe)
        if not mapping:
            return {}
        result = {
            "iso27001": mapping.get("iso27001", []) or [],
            "gdpr": mapping.get("gdpr", []) or [],
            "pci_dss": mapping.get("pci_dss", []) or [],
            "hipaa": mapping.get("hipaa", []) or [],
        }
        if not any(result.values()):
            return {}
        return result
    except Exception:
        return {}


def _build_remediation(rule_code: str, suggestion: str, cwe: str) -> str:
    """根据规则 code 生成详细修复方案(50-500 字)

    Args:
        rule_code: 静态规则编号,如 sql_string_concat
        suggestion: 基础修复建议(来自静态规则)
        cwe: CWE 编号,用于补充合规要求

    Returns:
        str: 详细修复方案文本
    """
    detailed_steps: Dict[str, str] = {
        "weak_md5": (
            "1. 立即停用 MD5 用于口令或签名场景,改用 bcrypt/scrypt/Argon2 哈希口令;"
            "2. 数据完整性校验改用 SHA-256 或 SHA-3;"
            "3. 对历史 MD5 哈希强制下次登录时重置口令并迁移到新算法;"
            "4. 在安全策略中禁止 MD5 出现在新代码中,通过 CI 静态扫描拦截。"
        ),
        "weak_sha1": (
            "1. 立即停用 SHA-1 用于签名、证书、HMAC 场景,改用 SHA-256 或 SHA-3;"
            "2. 升级依赖的 TLS 证书到 SHA-256 签名;"
            "3. 对历史 SHA-1 签名数据制定迁移计划;"
            "4. 添加 CI 检查禁止新代码使用 SHA-1。"
        ),
        "weak_des": (
            "1. 立即停用 DES/3DES 加密,改用 AES-256-GCM 或 ChaCha20-Poly1305;"
            "2. 对历史 DES 加密数据制定解密并重新加密的迁移计划;"
            "3. 密钥管理使用 KMS 或 HSM,避免硬编码;"
            "4. 启用密钥轮换策略,周期性更新加密密钥。"
        ),
        "aes_ecb_mode": (
            "1. 立即改用 AES-256-GCM 或 AES-256-CBC + HMAC 模式;"
            "2. 使用密码学安全的 IV 生成方式(GCM 随机 nonce 或 CBC 随机 IV);"
            "3. 对相同明文产生的密文应不同,使用 HMAC 验证密文完整性;"
            "4. 推荐使用 cryptography.hazmat.primitives.ciphers.aead.AESGCM。"
        ),
        "ssl_verify_disabled": (
            "1. 立即恢复 SSL/TLS 证书校验(verify=True,check_hostname=True);"
            "2. 在测试环境配置自签名 CA,不要在生产禁用校验;"
            "3. 使用 certifi 提供 CA 信任库,定期更新;"
            "4. 添加 CI 检查禁止 verify=False 出现在新代码中。"
        ),
        "pickle_load": (
            "1. 立即停用 pickle 处理不可信数据,改用 JSON/MessagePack 等无副作用格式;"
            "2. 必须反序列化时使用 hmac 签名验证数据完整性;"
            "3. 限制可反序列化的类白名单(自定义 Unpickler.find_class);"
            "4. 内部 RPC 数据改用 gRPC + Protocol Buffers。"
        ),
        "eval_user_input": (
            "1. 立即停用 eval/exec 处理用户输入;"
            "2. 表达式求值改用 ast.literal_eval(仅支持字面量);"
            "3. 模板渲染使用沙箱化的 Jinja2/SandboxedEnvironment;"
            "4. 必须执行动态代码时使用独立进程 + seccomp 沙箱。"
        ),
        "insecure_random": (
            "1. 立即将 random 模块替换为 secrets 模块(Python 3.6+);"
            "2. token/密钥生成使用 secrets.token_urlsafe(32);"
            "3. UUID 生成使用 uuid.uuid4()(基于 os.urandom);"
            "4. 添加 CI 检查禁止 random.random() 出现在安全相关代码中。"
        ),
        "sql_string_concat": (
            "1. 立即将 SQL 字符串拼接改为参数化查询(占位符 %s 或 ?);"
            "2. 使用 ORM(SQLAlchemy/Django ORM)替代裸 SQL;"
            "3. 必须拼接 SQL 时使用 SQLAlchemy text() + bindparams;"
            "4. 数据库账号启用最小权限,禁止 DROP/ALTER;"
            "5. 部署 WAF SQL 注入规则作为纵深防御。"
        ),
        "xxe_processing": (
            "1. 立即禁用 XML 外部实体解析(defusedxml 或设置 resolve_entities=False);"
            "2. 使用 defusedxml.ElementTree 替换 xml.etree.ElementTree;"
            "3. 限制 XML 文件大小(≤1MB)与实体深度(≤10);"
            "4. 添加 CI 检查禁止直接 import xml.etree 出现在新代码中。"
        ),
        "open_redirect": (
            "1. 立即对跳转 URL 做白名单校验(只允许同域名或预设域名);"
            "2. 使用 urllib.parse.urlparse 解析后校验 netloc;"
            "3. 外部跳转使用中间确认页(类似 GitHub 的 'you are being redirected');"
            "4. 禁止直接 redirect(request.args.get('next'))。"
        ),
        "path_traversal_user_input": (
            "1. 立即对路径参数做规范化 pathlib.Path(p).resolve() 后校验是否在允许目录;"
            "2. 使用 os.path.realpath + os.path.commonpath 校验;"
            "3. 文件名白名单(只允许 [a-zA-Z0-9_-]+.ext);"
            "4. 禁止直接拼接用户输入到文件系统路径。"
        ),
        "log_injection": (
            "1. 立即对日志输入做换行符过滤或转义(replace('\\n', '\\\\n'));"
            "2. 使用结构化日志(JSON logging)避免文本注入;"
            "3. 日志输出前对 control character 做 strip;"
            "4. 关键日志添加 request_id 用于审计追踪。"
        ),
        "cookie_no_httponly": (
            "1. 立即设置 Cookie httponly=True 防止 JS 读取;"
            "2. 同时设置 secure=True(仅 HTTPS 传输);"
            "3. 设置 samesite='Lax' 或 'Strict' 防御 CSRF;"
            "4. 会话 Cookie 还应设置 max_age 与 idle 超时。"
        ),
        "cookie_no_secure": (
            "1. 立即设置 Cookie secure=True 仅在 HTTPS 传输;"
            "2. 启用 HSTS 头强制 HTTPS(max-age=31536000);"
            "3. 在反向代理层(Nginx)强制 HTTP 跳转 HTTPS;"
            "4. Cookie 同时设置 httponly + samesite。"
        ),
        "missing_hsts": (
            "1. 立即在 HTTPS 响应头添加 Strict-Transport-Security: max-age=31536000; includeSubDomains; preload;"
            "2. 在 Nginx 配置 add_header Strict-Transport-Security ...;"
            "3. 申请加入 HSTS Preload List(https://hstspreload.org);"
            "4. 同时启用 HTTPS 跳转与 HSTS。"
        ),
        "missing_csp": (
            "1. 立即添加 Content-Security-Policy: default-src 'self'; script-src 'self' 'nonce-xxx';"
            "2. 禁用内联脚本(使用 nonce 或 hash);"
            "3. 禁用内联样式(使用 nonce 或 hash);"
            "4. 限制 img/font/connect-src 到必要域名;"
            "5. 添加 report-uri 收集 CSP 违规报告。"
        ),
        "missing_x_frame_options": (
            "1. 立即添加 X-Frame-Options: DENY 或 SAMEORIGIN;"
            "2. 现代浏览器使用 CSP frame-ancestors 'self' 替代;"
            "3. 同时设置 X-Frame-Options 与 CSP frame-ancestors 兼容老浏览器;"
            "4. 关键操作页(转账/删除)强制 DENY。"
        ),
        "missing_x_content_type_options": (
            "1. 立即添加 X-Content-Type-Options: nosniff 防止 MIME 嗅探;"
            "2. 在 Nginx add_header X-Content-Type-Options nosniff always;"
            "3. 同时确保所有响应有正确的 Content-Type;"
            "4. 静态资源(图片/PDF)也需设置。"
        ),
        "cors_wildcard_with_credentials": (
            "1. 立即将 Access-Control-Allow-Origin: * 替换为具体白名单域名;"
            "2. 使用 Origin 反射(只反射白名单内域名);"
            "3. Access-Control-Allow-Credentials: true 时必须指定具体 origin;"
            "4. 限制 Access-Control-Allow-Methods 到必要方法;"
            "5. 添加 Vary: Origin 头。"
        ),
    }
    steps = detailed_steps.get(rule_code, "")
    if not steps:
        # 兜底:基于 suggestion 生成
        steps = f"1. {suggestion};2. 添加单元测试覆盖该场景;3. 在 CI 中加入静态扫描规则。"
    compliance_note = ""
    if cwe:
        compliance_note = f"\n\n本问题对应 {cwe},可能违反 ISO 27001 / GDPR / PCI-DSS / HIPAA 相关条款,需评估合规风险。"
    return f"{steps}{compliance_note}"


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
        cvss_score: CVSS v3.1 基础分 0.0-10.0(基于 severity 经验值)
        cvss_vector: CVSS v3.1 向量字符串(基于规则类型固定模板)
        compliance_mapping: 合规映射字典,由 cwe 反查填充
        remediation: 详细修复方案文本
        static_rule_hits: 静态规则命中次数(本 Finding 至少为 1)
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
    # v3 新增 CVSS / 合规映射 / 修复方案 / 命中统计
    cvss_score: float = 0.0
    cvss_vector: str = ""
    compliance_mapping: Dict[str, List[str]] = field(default_factory=dict)
    remediation: str = ""
    static_rule_hits: int = 1


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
        cvss_score=_severity_to_cvss_score("严重"),
        cvss_vector=_cwe_to_cvss_vector(m.cwe),
        compliance_mapping=_build_compliance_mapping(m.cwe),
        remediation=_build_remediation(
            f"hardcoded_{pattern_name.lower()}",
            f"立即从代码中移除该 {pattern_name},改从环境变量或密钥管理服务读取",
            m.cwe,
        ),
        static_rule_hits=1,
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
        cvss_score=_severity_to_cvss_score(m.severity),
        cvss_vector=_cwe_to_cvss_vector(m.cwe),
        compliance_mapping=_build_compliance_mapping(m.cwe),
        remediation=_build_remediation(m.rule_code, m.fix_suggestion, m.cwe),
        static_rule_hits=1,
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
