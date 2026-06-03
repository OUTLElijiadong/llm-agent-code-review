/**
 * OWASP Top 10 · 2021 中文详解
 * 用于前端「安全中心」知识展示,与后端 SecuritySentinel checklist 对齐
 */

export interface OwaspCodeExample {
  language: string
  code: string
}

export interface OwaspDoc {
  code: string                   // 'A01' ~ 'A10'
  name_en: string
  name_zh: string
  definition: string             // 定义,30-150 字
  harm: string                   // 危害,30-150 字
  bad_example: OwaspCodeExample
  good_example: OwaspCodeExample
  prevention: string[]           // 防范要点
  cwe_refs: string[]             // 关联 CWE
  prism_coverage: string         // 棱镜如何检测
}

export const OWASP_TOP10: OwaspDoc[] = [
  {
    code: 'A01',
    name_en: 'Broken Access Control',
    name_zh: '失效的访问控制',
    definition:
      '应用未正确限制用户对资源或操作的访问权限。包括水平越权(访问同级别其他用户的数据)、' +
      '垂直越权(普通用户执行管理员操作)、IDOR(基于不可枚举的 ID 直接访问)、强制浏览等。',
    harm:
      '攻击者可绕过授权机制查看、修改、删除他人数据;在权限提升后可执行管理员级操作,' +
      '可能导致数据全量泄露、订单篡改、账号接管。',
    bad_example: {
      language: 'python',
      code:
        '@app.get("/orders/{order_id}")\n' +
        'def get_order(order_id: int):\n' +
        '    # ❌ 仅按 ID 查询,不校验当前用户是否为订单归属人\n' +
        '    return db.query(Order).get(order_id)',
    },
    good_example: {
      language: 'python',
      code:
        '@app.get("/orders/{order_id}")\n' +
        'def get_order(order_id: int, user = Depends(current_user)):\n' +
        '    order = db.query(Order).get(order_id)\n' +
        '    if order is None or order.user_id != user.id:\n' +
        '        raise HTTPException(404)  # 既不存在也不归属当前用户,统一返回 404\n' +
        '    return order',
    },
    prevention: [
      '默认拒绝(Deny by Default),所有资源访问都要显式授权',
      '将权限检查实现为统一中间件/装饰器,不要散落在业务逻辑里',
      '不返回敏感的资源标识(如自增 ID),改用 UUID 等不可枚举值',
      '管理员操作单独路由 + 独立的 require_admin 依赖',
      '为关键接口加 audit log,事后可追溯',
    ],
    cwe_refs: ['CWE-22', 'CWE-284', 'CWE-285', 'CWE-352', 'CWE-639'],
    prism_coverage:
      '棱镜检测路径遍历、IDOR 关键词,关联 CWE-22/CWE-639。建议结合人工 review 关键接口。',
  },
  {
    code: 'A02',
    name_en: 'Cryptographic Failures',
    name_zh: '加密失败 / 敏感数据泄露',
    definition:
      '与加密相关的失败,包括明文传输、弱加密算法(MD5/SHA1/DES)、密钥硬编码、' +
      '随机数不安全、证书校验缺失等。常导致敏感数据(密码、令牌、PII)泄露。',
    harm:
      '密码被破解,会话被劫持,中间人攻击可实施;合规风险(GDPR / 等保 2.0 / PCI DSS)。',
    bad_example: {
      language: 'python',
      code:
        'import hashlib\n' +
        '# ❌ MD5 已被破解,且无 salt,字典攻击毫秒级\n' +
        'hashed = hashlib.md5(password.encode()).hexdigest()\n\n' +
        '# ❌ 硬编码密钥\n' +
        'API_KEY = "sk-proj-AbCdEf1234567890..."',
    },
    good_example: {
      language: 'python',
      code:
        'import os\n' +
        'from passlib.hash import argon2\n\n' +
        '# ✅ argon2 / bcrypt / scrypt 都是带 salt 的慢哈希\n' +
        'hashed = argon2.hash(password)\n\n' +
        '# ✅ 从环境变量读取\n' +
        'API_KEY = os.environ["OPENAI_API_KEY"]',
    },
    prevention: [
      '密码用 bcrypt / argon2 / scrypt 哈希,带 salt + 慢算法',
      '禁用 MD5 / SHA1 / DES,加密用 AES-GCM 或 ChaCha20-Poly1305',
      '密钥从环境变量 / 密钥管理服务读取,绝不硬编码',
      'token / nonce / salt 用 secrets / crypto.randomBytes,不要用 Math.random',
      '强制 HTTPS,启用 HSTS;requests 库必须 verify=True',
    ],
    cwe_refs: ['CWE-261', 'CWE-296', 'CWE-310', 'CWE-321', 'CWE-326', 'CWE-327', 'CWE-798'],
    prism_coverage:
      '棱镜内置 12 条 MD5/SHA1/DES/ECB/SSL 校验关闭等静态规则,加 20+ 类硬编码秘钥正则。',
  },
  {
    code: 'A03',
    name_en: 'Injection',
    name_zh: '注入',
    definition:
      '应用将不可信数据直接拼接到查询/命令/代码中,导致解释器执行恶意指令。' +
      '包括 SQL 注入、Command 注入、LDAP 注入、XPath 注入、模板注入、NoSQL 注入。',
    harm:
      '数据库被脱库、删表,服务器被植入后门,跨账号查询/篡改数据。这是 OWASP Top 10 中最常见且危害最大的类别之一。',
    bad_example: {
      language: 'python',
      code:
        '# ❌ 字符串拼接 SQL,用户名 admin\' OR \'1\'=\'1 即可绕过登录\n' +
        'sql = f"SELECT * FROM user WHERE name=\'{name}\' AND pwd=\'{pwd}\'"\n' +
        'cursor.execute(sql)\n\n' +
        '# ❌ Command 注入\n' +
        'os.system(f"ping {host}")  # host="example.com; rm -rf /"',
    },
    good_example: {
      language: 'python',
      code:
        '# ✅ 参数化查询,驱动负责转义\n' +
        'cursor.execute(\n' +
        '    "SELECT * FROM user WHERE name=%s AND pwd=%s",\n' +
        '    (name, pwd),\n' +
        ')\n\n' +
        '# ✅ subprocess 不走 shell,参数化传入\n' +
        'subprocess.run(["ping", "-c", "3", host], check=True)',
    },
    prevention: [
      '所有 SQL 用参数化查询 / ORM 抽象,绝不字符串拼接',
      '系统命令用 subprocess.run([list], shell=False) 形式',
      '模板渲染默认自动转义(Jinja2 / Vue / React 都默认转义)',
      '对用户输入做白名单校验(类型 / 长度 / 字符集)',
      '最小权限 DB 账号,禁止应用层 DBA 权限',
    ],
    cwe_refs: ['CWE-77', 'CWE-78', 'CWE-79', 'CWE-89', 'CWE-91', 'CWE-94', 'CWE-95'],
    prism_coverage:
      '棱镜会识别字符串拼接构造 SQL、eval()/exec() 高危调用,并标记 CWE-89/CWE-95。',
  },
  {
    code: 'A04',
    name_en: 'Insecure Design',
    name_zh: '不安全的设计',
    definition:
      '从架构和设计层面缺少必要的安全控制,与"实现缺陷"不同 — 即使代码无 bug,' +
      '设计本身也无法满足安全需求。例如忘记设计速率限制、忘记设计资金扣减的原子性、' +
      '忘记设计敏感操作的二次验证。',
    harm:
      '业务逻辑漏洞,如订单价格篡改、积分套利、批量短信轰炸、并发竞争超卖。' +
      '这类问题无法被静态扫描器或 WAF 识别。',
    bad_example: {
      language: 'python',
      code:
        '# ❌ 提现接口未校验余额扣减原子性,并发可超额提现\n' +
        '@app.post("/withdraw")\n' +
        'def withdraw(amount: int, user = Depends(current_user)):\n' +
        '    balance = get_balance(user.id)\n' +
        '    if balance >= amount:\n' +
        '        deduct(user.id, amount)\n' +
        '        return {"ok": True}',
    },
    good_example: {
      language: 'python',
      code:
        '@app.post("/withdraw")\n' +
        'def withdraw(amount: int, user = Depends(current_user)):\n' +
        '    # ✅ 数据库行锁 + 单事务,杜绝竞态\n' +
        '    with db.begin():\n' +
        '        bal = db.execute(\n' +
        '            "SELECT balance FROM account WHERE user_id=%s FOR UPDATE",\n' +
        '            (user.id,),\n' +
        '        ).scalar()\n' +
        '        if bal < amount:\n' +
        '            raise HTTPException(400, "余额不足")\n' +
        '        db.execute("UPDATE account SET balance=balance-%s WHERE user_id=%s",\n' +
        '                   (amount, user.id))',
    },
    prevention: [
      '威胁建模阶段:列出所有"用户输入→关键资源"路径,逐个判定信任边界',
      '关键业务流程要有"安全用户故事"(滥用场景描述)',
      '敏感操作设计速率限制(rate limit) + 二次验证',
      '金额/库存类操作必须事务化,DB 行锁或乐观锁',
      '安全要求写进 PRD,不是实施阶段补丁',
    ],
    cwe_refs: ['CWE-209', 'CWE-256', 'CWE-501', 'CWE-522'],
    prism_coverage:
      '设计层问题难以纯静态扫描发现。棱镜会识别可执行的实现层缺陷,设计层建议结合 /plan-ceo-review。',
  },
  {
    code: 'A05',
    name_en: 'Security Misconfiguration',
    name_zh: '安全配置错误',
    definition:
      '应用 / 服务器 / 中间件 / 框架 / 云服务 等任意层级的配置不当。包括默认凭据、' +
      '不必要服务开放、Verbose 错误信息、未禁用 directory listing、过时依赖等。',
    harm:
      '后台管理界面外网可访问、Nginx 暴露版本号、错误堆栈泄露代码路径,均可成为攻击突破口。',
    bad_example: {
      language: 'conf',
      code:
        '# ❌ Nginx 默认配置,无 CSP / HSTS / X-Frame-Options\n' +
        'server {\n' +
        '    listen 80;\n' +
        '    server_name example.com;\n' +
        '    location / {\n' +
        '        proxy_pass http://app:3000;\n' +
        '    }\n' +
        '}',
    },
    good_example: {
      language: 'conf',
      code:
        'server {\n' +
        '    listen 443 ssl http2;\n' +
        '    server_name example.com;\n' +
        '    server_tokens off;\n' +
        '    # ✅ 全套安全响应头\n' +
        '    add_header Strict-Transport-Security "max-age=31536000; includeSubDomains" always;\n' +
        '    add_header X-Frame-Options SAMEORIGIN always;\n' +
        '    add_header X-Content-Type-Options nosniff always;\n' +
        '    add_header Content-Security-Policy "default-src \'self\'" always;\n' +
        '    location / {\n' +
        '        proxy_pass http://app:3000;\n' +
        '    }\n' +
        '}',
    },
    prevention: [
      '生产环境关闭 DEBUG / Verbose 错误页',
      'Nginx / Apache 配置完整的 HTTP 安全响应头',
      '默认凭据(admin/admin)上线前必须修改',
      '关闭 directory listing,不必要的服务/端口不要暴露',
      '统一基础镜像 + IaC(Terraform/Ansible),消除配置漂移',
    ],
    cwe_refs: ['CWE-2', 'CWE-11', 'CWE-13', 'CWE-15', 'CWE-16', 'CWE-260', 'CWE-525', 'CWE-1021'],
    prism_coverage:
      '棱镜在 .conf/.nginx/.cnf 文件上检测 HSTS/CSP/X-Frame-Options/CORS 通配符等 5 类常见配置错误。',
  },
  {
    code: 'A06',
    name_en: 'Vulnerable and Outdated Components',
    name_zh: '易受攻击和过时的组件',
    definition:
      '使用了已知存在漏洞的依赖库、框架版本、操作系统、运行时。常因升级滞后或不知道依赖关系导致。',
    harm:
      '攻击者可以用公开 EXP 直接命中(如 log4shell、struts2 漏洞)。供应链攻击成为近年来高频路径。',
    bad_example: {
      language: 'text',
      code:
        '# ❌ requirements.txt 锁定老版本且不更新\n' +
        'django==2.0.0          # CVE-2019-3498 等已知漏洞\n' +
        'requests==2.6.0        # SSL 校验问题\n' +
        'pillow==4.0.0          # 多个图像解析漏洞',
    },
    good_example: {
      language: 'text',
      code:
        '# ✅ 用工具锁定版本 + 定期更新\n' +
        'django>=4.2,<5.0\n' +
        'requests>=2.31\n' +
        'pillow>=10.0\n\n' +
        '# 配合 pip-audit / safety / Dependabot 自动检查 CVE',
    },
    prevention: [
      '建立 SBOM(软件物料清单),清楚每个依赖的版本和来源',
      '接入 Dependabot / Renovate / pip-audit / npm audit 自动扫描',
      '订阅安全公告邮件(Django Security / NPM Advisory 等)',
      '生产环境只允许从可信源拉取依赖(私有镜像源)',
      '废弃的库及时移除,不要保留"以防万一"',
    ],
    cwe_refs: ['CWE-937', 'CWE-1035', 'CWE-1104'],
    prism_coverage:
      '本版本棱镜暂不接入 CVE 数据库(列入 v2.2 路线图)。建议同时使用 pip-audit / npm audit。',
  },
  {
    code: 'A07',
    name_en: 'Identification and Authentication Failures',
    name_zh: '身份识别和身份验证失败',
    definition:
      '与身份认证、会话管理相关的缺陷。包括弱密码策略、暴力破解无限速、会话固定、' +
      'JWT 算法降级、密码恢复流程被绕过等。',
    harm:
      '账号被批量爆破,凭证填充攻击成功,会话被劫持后导致账号接管。',
    bad_example: {
      language: 'python',
      code:
        '# ❌ 登录无失败次数限制 + 弱口令策略\n' +
        '@app.post("/login")\n' +
        'def login(name: str, pwd: str):\n' +
        '    user = db.query(User).filter_by(name=name).first()\n' +
        '    if user and user.password == pwd:  # 明文比较\n' +
        '        return create_token(user.id)\n' +
        '    return {"error": "用户名或密码错误"}',
    },
    good_example: {
      language: 'python',
      code:
        'from passlib.hash import argon2\n' +
        'from app.rate_limit import rate_limit\n\n' +
        '@app.post("/login")\n' +
        '@rate_limit(key=lambda r: r.client.host, max=5, window=60)\n' +
        'def login(name: str, pwd: str):\n' +
        '    user = db.query(User).filter_by(name=name).first()\n' +
        '    # ✅ 哈希校验 + 通用错误消息(不暴露用户名是否存在)\n' +
        '    if user and argon2.verify(pwd, user.password_hash):\n' +
        '        return create_token(user.id)\n' +
        '    raise HTTPException(401, "账号或密码错误")',
    },
    prevention: [
      '密码哈希:argon2 / bcrypt / scrypt + salt',
      '登录失败次数限制(单 IP / 单账号)+ 验证码 / 邮件二次确认',
      '强密码策略 + 弱密码字典拦截',
      'JWT 强制指定 algorithm,不接受 None / 用户传入 alg',
      '敏感操作需要重新认证(re-authentication)',
    ],
    cwe_refs: ['CWE-259', 'CWE-287', 'CWE-384', 'CWE-521', 'CWE-522', 'CWE-798'],
    prism_coverage:
      '棱镜识别明文密码比较、弱口令、Cookie 不带 HttpOnly/Secure 等模式,关联 CWE-287/CWE-521。',
  },
  {
    code: 'A08',
    name_en: 'Software and Data Integrity Failures',
    name_zh: '软件和数据完整性失败',
    definition:
      '应用接受来源不可信的代码、更新、数据。包括不安全反序列化(pickle/yaml.load)、' +
      'CI/CD 中无签名校验、自动更新无完整性验证。',
    harm:
      '不安全反序列化可直接 RCE;供应链投毒(SolarWinds 事件即此类);开发流程可被植入后门。',
    bad_example: {
      language: 'python',
      code:
        'import pickle\n' +
        '# ❌ 反序列化任意可控数据,等于 RCE\n' +
        '@app.post("/import")\n' +
        'def import_state(data: bytes):\n' +
        '    state = pickle.loads(data)\n' +
        '    return apply(state)',
    },
    good_example: {
      language: 'python',
      code:
        'import json\n' +
        'from app.crypto import verify_signature\n\n' +
        '@app.post("/import")\n' +
        'def import_state(data: bytes, signature: str):\n' +
        '    # ✅ JSON 安全反序列化 + HMAC 签名校验\n' +
        '    if not verify_signature(data, signature):\n' +
        '        raise HTTPException(403)\n' +
        '    state = json.loads(data)\n' +
        '    return apply(state)',
    },
    prevention: [
      '避免 pickle / yaml.load 处理不可信数据,改用 JSON',
      'CI/CD 流程中所有制品要有签名(provenance)',
      'npm install --ignore-scripts(防止依赖包安装时执行任意脚本)',
      'Docker 镜像锁定 sha256 digest,不用 :latest',
      '对自动更新增加完整性校验(checksum / signature)',
    ],
    cwe_refs: ['CWE-345', 'CWE-353', 'CWE-426', 'CWE-494', 'CWE-502', 'CWE-565', 'CWE-784'],
    prism_coverage:
      '棱镜识别 pickle.loads / yaml.load 无 Loader / eval / exec 等高危反序列化调用,关联 CWE-502。',
  },
  {
    code: 'A09',
    name_en: 'Security Logging and Monitoring Failures',
    name_zh: '安全日志和监控失败',
    definition:
      '关键安全事件未被记录、未被告警、未被持久化。导致入侵无法及时发现,事后无法溯源。',
    harm:
      '攻击窗口从小时级延长到月级。根据 Verizon DBIR 报告,平均被入侵后的发现时间是 200+ 天。',
    bad_example: {
      language: 'python',
      code:
        '@app.post("/login")\n' +
        'def login(name: str, pwd: str):\n' +
        '    if check(name, pwd):\n' +
        '        return create_token(name)\n' +
        '    # ❌ 失败既不记日志也不告警\n' +
        '    return {"error": "fail"}',
    },
    good_example: {
      language: 'python',
      code:
        'from loguru import logger\n' +
        'from app.audit import audit_log\n\n' +
        '@app.post("/login")\n' +
        'def login(name: str, pwd: str, req: Request):\n' +
        '    ok = check(name, pwd)\n' +
        '    # ✅ 成功 / 失败都记录,IP + UA + 时间\n' +
        '    audit_log(\n' +
        '        action="login",\n' +
        '        result="success" if ok else "failure",\n' +
        '        username=name,\n' +
        '        ip=req.client.host,\n' +
        '        ua=req.headers.get("User-Agent"),\n' +
        '    )\n' +
        '    if ok:\n' +
        '        return create_token(name)\n' +
        '    raise HTTPException(401)',
    },
    prevention: [
      '所有登录 / 提权 / 资金 / 密码修改事件都要审计日志',
      '日志包含: 时间、用户、IP、UA、操作、结果、影响资源',
      '日志统一汇集(ELK / Loki),设置异常告警规则',
      '保留期限至少 90 天,关键系统 1 年以上',
      '定期演练事件响应,确保告警链路有效',
    ],
    cwe_refs: ['CWE-117', 'CWE-223', 'CWE-532', 'CWE-778'],
    prism_coverage:
      '棱镜识别明显的"缺审计"模式(关键操作无日志)。完整可观测性建议结合 audit_log 模块。',
  },
  {
    code: 'A10',
    name_en: 'Server-Side Request Forgery (SSRF)',
    name_zh: '服务端请求伪造',
    definition:
      '服务器代用户访问任意 URL,但未校验目标。攻击者通过提交内网 IP / 元数据接口 URL,' +
      '让服务器代为请求,获取内网信息或攻击内网服务。',
    harm:
      '云服务器元数据接口(如 169.254.169.254)被读取,泄露临时凭证;内网管理界面被探测;' +
      'Redis / Memcached 未授权访问被利用。',
    bad_example: {
      language: 'python',
      code:
        'import requests\n' +
        '# ❌ 用户输入直接作为请求 URL\n' +
        '@app.get("/preview")\n' +
        'def preview(url: str):\n' +
        '    return requests.get(url).text\n' +
        '# 攻击: ?url=http://169.254.169.254/latest/meta-data/',
    },
    good_example: {
      language: 'python',
      code:
        'import requests\n' +
        'from urllib.parse import urlparse\n' +
        'import ipaddress\n' +
        'import socket\n\n' +
        'ALLOWED_HOSTS = {"www.example.com", "cdn.example.com"}\n\n' +
        '@app.get("/preview")\n' +
        'def preview(url: str):\n' +
        '    parsed = urlparse(url)\n' +
        '    # ✅ 1. 协议白名单\n' +
        '    if parsed.scheme not in ("http", "https"):\n' +
        '        raise HTTPException(400, "scheme")\n' +
        '    # ✅ 2. host 白名单\n' +
        '    if parsed.hostname not in ALLOWED_HOSTS:\n' +
        '        raise HTTPException(400, "host")\n' +
        '    # ✅ 3. 解析 IP,拒绝内网/loopback\n' +
        '    ip = ipaddress.ip_address(socket.gethostbyname(parsed.hostname))\n' +
        '    if ip.is_private or ip.is_loopback or ip.is_link_local:\n' +
        '        raise HTTPException(400, "private network")\n' +
        '    return requests.get(url, timeout=5, allow_redirects=False).text',
    },
    prevention: [
      'URL 协议白名单(只允许 http/https)',
      'host 白名单 + IP 范围校验(拒绝 127/8、10/8、172.16/12、192.168/16、169.254/16)',
      '禁用或限制 HTTP 重定向(避免重定向到内网)',
      '云环境对元数据接口加 IMDSv2 强制认证',
      '内网服务自身也要鉴权,不依赖网络隔离作为唯一安全边界',
    ],
    cwe_refs: ['CWE-918'],
    prism_coverage:
      '棱镜识别 requests.get(user_input)、urllib.urlopen 等模式,关联 CWE-918。',
  },
]


export function getOwaspByCode(code: string): OwaspDoc | undefined {
  return OWASP_TOP10.find((d) => d.code === code)
}
