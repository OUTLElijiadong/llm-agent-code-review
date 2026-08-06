"""静态语义安全规则 (v2.1.1)

正则秘钥扫描只能识别"字面 secret"。本模块负责"形态识别":
- 弱加密 API 调用 (MD5/SHA1/DES/ECB/不安全随机数)
- 危险函数 (pickle.loads/eval(用户输入))
- HTTP 安全头缺失 (HSTS/CSP/X-Frame-Options 等,仅扫描配置文件)

不依赖 LLM,确定性命中即输出 finding,severity 已预设。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List, Optional, Pattern, Tuple


@dataclass(frozen=True)
class StaticRule:
    """单条静态规则"""

    code: str
    name: str
    cwe: str
    owasp: str
    severity: str
    description: str
    fix_suggestion: str
    pattern: Pattern[str]
    file_extensions: Tuple[str, ...]
    # 配置文件规则: 要求文件先出现某些标记才检查 "缺失"
    require_presence_of: Optional[Pattern[str]] = None


# ============ 弱加密 / 危险 API 规则 ============

_WEAK_CRYPTO_RULES: Tuple[StaticRule, ...] = (
    StaticRule(
        code="weak_md5",
        name="弱加密算法 MD5",
        cwe="CWE-327",
        owasp="A02:2021-Cryptographic Failures",
        severity="高",
        description="使用 MD5 做哈希,该算法已被破解,不应再用于密码/签名/完整性校验。",
        fix_suggestion="改用 SHA-256 / SHA-3 / BLAKE2;密码哈希应使用 bcrypt/argon2/scrypt。",
        pattern=re.compile(
            r"""(?ix)
            \b(?:hashlib\.md5\s*\(
              | MessageDigest\.getInstance\s*\(\s*["']MD5["']
              | CryptoJS\.MD5\s*\(
              | createHash\s*\(\s*["']md5["']
              | md5\s*\(\s*(?:password|passwd|pwd|secret)
            )
            """,
        ),
        file_extensions=(".py", ".java", ".js", ".ts", ".jsx", ".tsx", ".go", ".rb"),
    ),
    StaticRule(
        code="weak_sha1",
        name="弱加密算法 SHA-1",
        cwe="CWE-327",
        owasp="A02:2021-Cryptographic Failures",
        severity="中",
        description="SHA-1 已被实际碰撞,不应再用于数字签名或证书。",
        fix_suggestion="改用 SHA-256 或更强算法;HMAC 应用 SHA-256 及以上。",
        pattern=re.compile(
            r"""(?ix)
            \b(?:hashlib\.sha1\s*\(
              | MessageDigest\.getInstance\s*\(\s*["']SHA-?1["']
              | CryptoJS\.SHA1\s*\(
              | createHash\s*\(\s*["']sha1["']
            )
            """,
        ),
        file_extensions=(".py", ".java", ".js", ".ts", ".go", ".rb"),
    ),
    StaticRule(
        code="weak_des",
        name="DES 加密算法",
        cwe="CWE-327",
        owasp="A02:2021-Cryptographic Failures",
        severity="严重",
        description="DES 密钥长度仅 56 bit,可在数小时内被穷举破解。",
        fix_suggestion="改用 AES-GCM 或 ChaCha20-Poly1305 等现代加密算法。",
        pattern=re.compile(
            r"""(?ix)
            \b(?:from\s+Crypto\.Cipher\s+import\s+DES
              | Cipher\.getInstance\s*\(\s*["']DES(?:/|["'])
              | DES\.new\s*\(
            )
            """,
        ),
        file_extensions=(".py", ".java"),
    ),
    StaticRule(
        code="aes_ecb_mode",
        name="AES 使用 ECB 模式",
        cwe="CWE-327",
        owasp="A02:2021-Cryptographic Failures",
        severity="严重",
        description="ECB 模式相同明文块产生相同密文块,会泄露数据规律。",
        fix_suggestion="改用 AES-GCM 或 AES-CBC + 随机 IV,不要使用 ECB。",
        pattern=re.compile(
            r"""(?ix)
            \b(?:AES\.new\s*\([^)]*AES\.MODE_ECB
              | ["']AES/ECB/
              | Cipher\.getInstance\s*\(\s*["']AES/ECB
            )
            """,
        ),
        file_extensions=(".py", ".java", ".go", ".js", ".ts"),
    ),
    StaticRule(
        code="ssl_verify_disabled",
        name="SSL 证书校验被禁用",
        cwe="CWE-295",
        owasp="A02:2021-Cryptographic Failures",
        severity="严重",
        description="禁用 SSL/TLS 证书校验等同于明文传输,中间人可以任意篡改。",
        fix_suggestion="启用证书校验;若是内部 CA,导入 CA 证书而不是禁用校验。",
        pattern=re.compile(
            r"""(?ix)
            (?:verify\s*=\s*False
              | rejectUnauthorized\s*:\s*false
              | InsecureRequestWarning
              | InsecureSkipVerify\s*:\s*true
            )
            """,
        ),
        file_extensions=(".py", ".js", ".ts", ".go"),
    ),
    StaticRule(
        code="pickle_load",
        name="不安全的反序列化 pickle.loads",
        cwe="CWE-502",
        owasp="A08:2021-Software and Data Integrity Failures",
        severity="严重",
        description="pickle.loads 可执行任意构造代码,加载不可信数据等于 RCE。",
        fix_suggestion="改用 JSON 或加签名校验;若必须用 pickle,只加载本地可信数据。",
        pattern=re.compile(
            r"""(?ix)
            \b(?:c?[Pp]ickle\.loads?\s*\(
              | yaml\.load\s*\(\s*[^),]+\s*\)   # 不带 Loader= 的 yaml.load
            )
            """,
        ),
        file_extensions=(".py",),
    ),
    StaticRule(
        code="eval_user_input",
        name="eval 用于动态执行",
        cwe="CWE-95",
        owasp="A03:2021-Injection",
        severity="严重",
        description="eval / exec 直接执行字符串,若拼接外部输入将导致代码执行漏洞。",
        fix_suggestion="移除 eval;用 ast.literal_eval (Python) 或参数化运算替代。",
        pattern=re.compile(
            r"""(?mx)
            ^\s*(?:eval|exec)\s*\(
            """,
        ),
        file_extensions=(".py", ".js", ".ts"),
    ),
    StaticRule(
        code="insecure_random",
        name="安全场景使用非加密随机数",
        cwe="CWE-330",
        owasp="A02:2021-Cryptographic Failures",
        severity="高",
        description="random.random / Math.random 是伪随机,不可用于 token/密码/密钥生成。",
        fix_suggestion="Python 用 secrets 模块;Node 用 crypto.randomBytes;Java 用 SecureRandom。",
        pattern=re.compile(
            r"""(?ix)
            (?:token|password|secret|api[_-]?key|nonce|salt|otp)
            .{0,30}
            (?:random\.random\s*\(
              | Math\.random\s*\(
              | new\s+Random\s*\(
            )
            """,
        ),
        file_extensions=(".py", ".js", ".ts", ".java"),
    ),
    # ============ v2.1.1 扩展规则 ============
    StaticRule(
        code="sql_string_concat",
        name="SQL 语句字符串拼接",
        cwe="CWE-89",
        owasp="A03:2021-Injection",
        severity="严重",
        description="使用字符串拼接 / f-string / 格式化 / + 拼接构造 SQL 语句,存在 SQL 注入风险。",
        fix_suggestion="改用参数化查询: cursor.execute(sql, params) 或 ORM 查询构造器。",
        pattern=re.compile(
            r"""(?ix)
            (?:cursor\.execute\s*\(\s*f["']
              | cursor\.execute\s*\(\s*["'].*?\%.*?["']\s*%
              | cursor\.execute\s*\(\s*["'].*?\{.*?\}.*?["']\s*\.format
              | execute\s*\(\s*f["']
              | query\s*=\s*f["'].*?\b(?:SELECT|INSERT|UPDATE|DELETE|WHERE)\b
              # v3 补丁: 覆盖 + 拼接形式(要求拼接字符串含 SQL 关键字,避免误报)
              # 分支 A: execute("SELECT..." + var) 形式
              | (?:cursor\.execute|execute)\s*\(\s*
                ["']\s*(?:SELECT|INSERT|UPDATE|DELETE|REPLACE|CREATE|ALTER|DROP)\b
                [^"']*["']\s*\+
              # 分支 B: query = "SELECT..." + var 形式
              | \b(?:query|sql|stmt|q)\s*=\s*
                ["']\s*(?:SELECT|INSERT|UPDATE|DELETE|REPLACE|CREATE|ALTER|DROP)\b
                [^"']*["']\s*\+
              # 分支 C: query = var + "...WHERE..." 形式(变量在前)
              | \b(?:query|sql|stmt|q)\s*=\s*\w+\s*\+\s*
                ["'][^"']*\b(?:WHERE|VALUES|SET|AND|OR|FROM|JOIN)\b
            )
            """,
        ),
        file_extensions=(".py",),
    ),
    StaticRule(
        code="xxe_processing",
        name="XML 外部实体注入风险 (XXE)",
        cwe="CWE-611",
        owasp="A05:2021-Security Misconfiguration",
        severity="严重",
        description="使用标准 XML 解析器(lxml/etree/xml.sax/DocumentBuilder)但未明确禁用外部实体。",
        fix_suggestion="禁用 DTD 和外部实体;Python 用 defusedxml 替代标准库;"
                        "Java 设置 FEATURE_DISALLOW_DOCTYPE_DECL 为 true。",
        pattern=re.compile(
            r"""(?ix)
            \b(?:etree\.(?:parse|fromstring|XMLParser)\s*\(
              | DocumentBuilderFactory
              | SAXParserFactory
              | xml\.sax\.parse\s*\(
              | lxml\.etree\.parse\s*\(
            )
            """,
        ),
        file_extensions=(".py", ".java", ".js", ".ts"),
    ),
    StaticRule(
        code="open_redirect",
        name="开放重定向",
        cwe="CWE-601",
        owasp="A01:2021-Broken Access Control",
        severity="高",
        description="重定向目标 URL 直接取自用户输入,未做白名单校验,可被用于钓鱼攻击。",
        fix_suggestion="对 redirect URL 做白名单校验(仅允许自身域名);或使用相对路径重定向。",
        pattern=re.compile(
            r"""(?ix)
            \b(?:redirect\s*\(\s*(?:request\.(?:args|form|query|GET|POST)|params|input)
              | HttpResponseRedirect\s*\(\s*(?:request\.(?:args|GET|POST))
              | res\.redirect\s*\(\s*(?:req\.(?:query|body|params))
              | header\s*\(\s*["']Location["']\s*,\s*\$
            )
            """,
        ),
        file_extensions=(".py", ".php", ".js", ".ts", ".rb", ".go"),
    ),
    StaticRule(
        code="path_traversal_user_input",
        name="路径遍历使用用户输入",
        cwe="CWE-22",
        owasp="A01:2021-Broken Access Control",
        severity="严重",
        description="文件路径拼接了用户输入(如 ../ ),攻击者可读取任意文件。",
        fix_suggestion="用 os.path.realpath 规范路径 + 白名单限制可访问目录;禁止 .. 出现在路径中。",
        pattern=re.compile(
            r"""(?ix)
            (?:os\.path\.join\s*\([^)]*(?:request\.|req\.|params|input|args|GET|POST|user)
              | open\s*\([^)]*(?:request\.|req\.|params|input|args|GET|POST|user)
              | \w+\s*=\s*(?:request\.(?:args|form|GET|POST)\[)
              .*?(?:open|read|write|delete|unlink|os\.(?:remove|unlink|stat))
            )
            """,
        ),
        file_extensions=(".py", ".rb", ".php", ".go"),
    ),
    StaticRule(
        code="log_injection",
        name="日志注入风险",
        cwe="CWE-117",
        owasp="A09:2021-Security Logging and Monitoring Failures",
        severity="中",
        description="用户输入直接写入日志,可能注入换行伪造日志条目或注入控制字符污染终端。",
        fix_suggestion="对用户输入做换行/回车转义;使用结构化日志(JSON),不依赖文本解析。",
        pattern=re.compile(
            r"""(?ix)
            logger\.(?:info|warn|error|debug|critical)\s*\(
            .*?(?:user|input|request\.(?:args|form|GET|POST|json|data)|params)
            """,
        ),
        file_extensions=(".py", ".js", ".ts", ".java"),
    ),
)


# ============ Cookie 安全标志缺失规则 ============
# 仅在代码文件中搜索 set_cookie / Set-Cookie 调用,
# 先确认存在 cookie 操作(require_presence_of 命中),
# 再检查特定安全标志是否缺失(pattern 未命中 → 报警)

_COOKIE_SET_PRESENCE = re.compile(
    r"""(?ix)
    (?:\.set_cookie\s*\(
      | response\.set_cookie
      | cookies\.set\s*\(
      | Set-Cookie\s*:\s*
      | header\s*\(\s*["']Set-Cookie
    )
    """,
)

_COOKIE_RULES: tuple[StaticRule, ...] = (
    StaticRule(
        code="cookie_no_httponly",
        name="Cookie 缺少 HttpOnly 标志",
        cwe="CWE-1004",
        owasp="A05:2021-Security Misconfiguration",
        severity="高",
        description="设置 Cookie 时未指定 HttpOnly,JavaScript 可读取该 Cookie,放大 XSS 危害。",
        fix_suggestion="添加 httponly=True / HttpOnly 标志;敏感 Cookie(如 session token)必须设置。",
        pattern=re.compile(r"(?i)\b(?:httponly|HttpOnly)\b"),
        file_extensions=(".py", ".js", ".ts", ".java", ".go", ".rb", ".php"),
        require_presence_of=_COOKIE_SET_PRESENCE,
    ),
    StaticRule(
        code="cookie_no_secure",
        name="Cookie 缺少 Secure 标志",
        cwe="CWE-614",
        owasp="A05:2021-Security Misconfiguration",
        severity="高",
        description="设置 Cookie 时未指定 Secure,在 HTTPS 连接下该 Cookie 可能通过 HTTP 明文传输。",
        fix_suggestion="添加 secure=True / Secure 标志;生产环境所有 Cookie 必须设置 Secure。",
        pattern=re.compile(
            r"""(?ix)
            \b(?:secure\s*=\s*True
              | Secure\b
              | secure\s*:\s*true
            )
            """,
        ),
        file_extensions=(".py", ".js", ".ts", ".java", ".go", ".rb", ".php"),
        require_presence_of=_COOKIE_SET_PRESENCE,
    ),
)


# ============ HTTP 安全头规则 ============
# 仅在配置文件类型上跑;且文件必须至少包含一次 HTTP header 声明,
# 才检查"是否缺失"——避免对普通 yaml 误报

_HEADER_PRESENCE_RE = re.compile(
    r"(?i)\b(?:add_header|Header\s+set|server_tokens|listen\s+\d+|location\s+/)",
)

_HTTP_HEADER_RULES: Tuple[StaticRule, ...] = (
    StaticRule(
        code="missing_hsts",
        name="缺失 HSTS 安全头",
        cwe="CWE-319",
        owasp="A02:2021-Cryptographic Failures",
        severity="高",
        description="未配置 Strict-Transport-Security,浏览器可被降级到 HTTP 中间人攻击。",
        fix_suggestion=(
            "添加: add_header Strict-Transport-Security \"max-age=31536000; includeSubDomains\";"
        ),
        # 命中条件: 文件**没有** Strict-Transport-Security 字符串。
        # 用反向匹配设计: 用一个"必然不出现"的负匹配模式占位,实际判断在 apply_rules 里
        pattern=re.compile(r"Strict-Transport-Security", re.IGNORECASE),
        file_extensions=(".conf", ".nginx", ".cnf"),
        require_presence_of=_HEADER_PRESENCE_RE,
    ),
    StaticRule(
        code="missing_csp",
        name="缺失 Content-Security-Policy",
        cwe="CWE-693",
        owasp="A05:2021-Security Misconfiguration",
        severity="高",
        description="未配置 CSP,无法限制脚本来源,放大 XSS 影响面。",
        fix_suggestion=(
            "添加: add_header Content-Security-Policy \"default-src 'self'\"; 根据业务调整"
        ),
        pattern=re.compile(r"Content-Security-Policy", re.IGNORECASE),
        file_extensions=(".conf", ".nginx", ".cnf"),
        require_presence_of=_HEADER_PRESENCE_RE,
    ),
    StaticRule(
        code="missing_x_frame_options",
        name="缺失 X-Frame-Options",
        cwe="CWE-1021",
        owasp="A05:2021-Security Misconfiguration",
        severity="中",
        description="未配置 X-Frame-Options,页面可被任意网站 iframe 嵌入,导致点击劫持。",
        fix_suggestion="添加: add_header X-Frame-Options SAMEORIGIN;",
        pattern=re.compile(r"X-Frame-Options", re.IGNORECASE),
        file_extensions=(".conf", ".nginx", ".cnf"),
        require_presence_of=_HEADER_PRESENCE_RE,
    ),
    StaticRule(
        code="missing_x_content_type_options",
        name="缺失 X-Content-Type-Options",
        cwe="CWE-693",
        owasp="A05:2021-Security Misconfiguration",
        severity="中",
        description="未配置 X-Content-Type-Options,浏览器可能根据内容猜测 MIME,放大 XSS。",
        fix_suggestion="添加: add_header X-Content-Type-Options nosniff;",
        pattern=re.compile(r"X-Content-Type-Options", re.IGNORECASE),
        file_extensions=(".conf", ".nginx", ".cnf"),
        require_presence_of=_HEADER_PRESENCE_RE,
    ),
    StaticRule(
        code="cors_wildcard_with_credentials",
        name="CORS 配置错误: 通配符 + 携带凭据",
        cwe="CWE-942",
        owasp="A05:2021-Security Misconfiguration",
        severity="严重",
        description="同时设置 Access-Control-Allow-Origin: * 与 Allow-Credentials: "
                      "true 违反 CORS 规范,任何站点可携带凭据请求。",
        fix_suggestion="将 Origin 限定为白名单具体域名,不要同时使用通配符与凭据携带。",
        pattern=re.compile(
            r"(?is)Access-Control-Allow-Origin\s*[:\s]+\*"
            r".{0,400}Access-Control-Allow-Credentials\s*[:\s]+true",
        ),
        file_extensions=(".conf", ".nginx", ".cnf", ".yml", ".yaml", ".py", ".js", ".ts"),
    ),
)


@dataclass
class StaticMatch:
    """单条静态规则命中结果"""

    rule_code: str
    rule_name: str
    cwe: str
    owasp: str
    severity: str
    description: str
    fix_suggestion: str
    line_number: int
    evidence_line: str


def _file_extension(file_name: str) -> str:
    if "." not in file_name:
        return ""
    return "." + file_name.rsplit(".", 1)[-1].lower()


def apply_static_rules(content: str, file_name: str) -> List[StaticMatch]:
    """对单文件应用所有静态规则

    Args:
        content: 文件内容
        file_name: 文件名(含扩展名),决定哪些规则适用

    Returns:
        list[StaticMatch]: 命中结果
    """
    if not content:
        return []
    ext = _file_extension(file_name)
    matches: List[StaticMatch] = []
    lines = content.splitlines()

    # ===== 弱加密 / 危险 API: 直接搜命中 =====
    for rule in _WEAK_CRYPTO_RULES:
        if ext and ext not in rule.file_extensions:
            continue
        # 单文件每条规则最多保留 10 处,避免规模化样本污染
        hits = 0
        for line_no, line_text in enumerate(lines, start=1):
            if hits >= 10:
                break
            if rule.pattern.search(line_text):
                matches.append(_make_match(rule, line_no, line_text))
                hits += 1

    # ===== Cookie 安全标志规则: 检查"应有但缺失" =====
    for rule in _COOKIE_RULES:
        if ext and ext not in rule.file_extensions:
            continue
        if rule.require_presence_of is None:
            continue
        if not rule.require_presence_of.search(content):
            continue
        # 文件存在 cookie 设置操作,但未出现目标安全标志 → 缺失
        if not rule.pattern.search(content):
            matches.append(_make_match(rule, 0, "(整文件级 · 未发现该安全标志)"))

    # ===== HTTP 头规则: 检查"应有但缺失" =====
    for rule in _HTTP_HEADER_RULES:
        if ext and ext not in rule.file_extensions:
            continue
        # cors_wildcard 规则反过来: 跨行搜索"同时出现",命中即报警
        if rule.code == "cors_wildcard_with_credentials":
            m = rule.pattern.search(content)
            if m:
                # 找到匹配片段所在行
                line_no = content[: m.start()].count("\n") + 1
                snippet = lines[line_no - 1] if 1 <= line_no <= len(lines) else ""
                matches.append(_make_match(rule, line_no, snippet))
            continue
        # 缺失类: 必须先证明这是 web 配置(require_presence_of 命中)
        if rule.require_presence_of is None:
            continue
        if not rule.require_presence_of.search(content):
            continue
        # 文件是 web 配置,但未出现目标 header → 缺失
        if not rule.pattern.search(content):
            matches.append(_make_match(rule, 0, "(整文件级 · 未发现该响应头声明)"))

    return matches


def _make_match(rule: StaticRule, line_no: int, line_text: str) -> StaticMatch:
    return StaticMatch(
        rule_code=rule.code,
        rule_name=rule.name,
        cwe=rule.cwe,
        owasp=rule.owasp,
        severity=rule.severity,
        description=rule.description,
        fix_suggestion=rule.fix_suggestion,
        line_number=line_no,
        evidence_line=line_text.strip()[:200],
    )


def list_static_rules() -> List[dict]:
    """列出所有静态规则(供检查清单接口使用)"""
    out: List[dict] = []
    for rule in _WEAK_CRYPTO_RULES + _COOKIE_RULES + _HTTP_HEADER_RULES:
        out.append({
            "code": rule.code,
            "name": rule.name,
            "owasp": rule.owasp,
            "cwe": rule.cwe,
            "description": rule.description,
            "severity": rule.severity,
            "applies_to": list(rule.file_extensions),
        })
    return out
