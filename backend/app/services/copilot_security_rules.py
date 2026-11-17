"""小菱安全中心：规则库与预设。

本模块只放"数据"：提示注入特征规则、敏感信息脱敏字典、动作风险词表、
策略默认值与三档预设。真正的判定与落库在 ``copilot_security_service``。

设计约束：
- 规则必须可解释：每条命中都要带 code / 类别 / 说明 / 分值，便于前端展示与审计复盘。
- 匹配前先做归一化（NFKC + 去零宽/RTL + 小写），否则全角、零宽字符可以绕过。
- 分值累加封顶 100，阈值判定在 service 层做，规则本身不决定"拦不拦"。
"""

from __future__ import annotations

import ipaddress
import re
import unicodedata
from dataclasses import dataclass
from typing import Iterable, Optional, Union

# ── 分值口径：单条规则 5~45 分，累加封顶 100；阈值在策略里配。
_SCORE_CAP = 100
# 零宽字符与双向控制符，常被用来把"忽略指令"拆开躲检索。
_INVISIBLE_CHARS = (
    "\u200b\u200c\u200d\u200e\u200f\u202a\u202b\u202c\u202d\u202e"
    "\u2060\u2061\u2062\u2063\u2064\ufeff"
    # Unicode Tag 区段（U+E0000–U+E007F）：肉眼不可见，可承载整段隐藏指令
    + "".join(chr(code) for code in range(0xE0000, 0xE0080))
)

# 常见同形字折叠：西里尔/希腊字母冒充拉丁字母，用来绕过英文规则。
_HOMOGLYPHS = str.maketrans({
    "а": "a", "е": "e", "о": "o", "р": "p", "с": "c", "у": "y", "х": "x", "і": "i",
    "ј": "j", "ѕ": "s", "н": "h", "в": "b", "к": "k", "м": "m", "т": "t",
    "ο": "o", "α": "a", "ε": "e", "ι": "i", "κ": "k", "ν": "v", "ρ": "p", "τ": "t", "υ": "u",
})
_INVISIBLE_TABLE = {ord(ch): None for ch in _INVISIBLE_CHARS}


# ▎一条提示注入特征规则：编码、类别、分值、正则与人类可读说明。
@dataclass(frozen=True)
class InjectionRule:
    """提示注入/越权诱导特征规则。"""

    code: str
    category: str
    score: int
    pattern: re.Pattern[str]
    description: str


# ▎归一化结果：清洗后的文本 + 归一化过程中发现的混淆信号。
@dataclass(frozen=True)
class NormalizedText:
    """规范化后的文本与混淆证据。"""

    text: str
    invisibles_removed: int = 0
    fullwidth_changed: bool = False
    rtl_control: bool = False


# ▎归一化输入文本：NFKC 折叠全角/同形字符，剥离零宽与 RTL 控制符，压平空白长度上限。
def normalize_text(raw: str) -> NormalizedText:
    """把输入折叠成可稳定匹配的形态。

    Args:
        raw: 原始输入文本。

    Returns:
        NormalizedText: 折叠后文本（小写）与混淆证据。
    """
    source = raw or ""
    invisibles = sum(1 for ch in source if ch in _INVISIBLE_CHARS)
    rtl = any(ch in source for ch in "\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069")
    stripped = source.translate(_INVISIBLE_TABLE)
    folded = unicodedata.normalize("NFKC", stripped).translate(_HOMOGLYPHS)
    return NormalizedText(
        text=folded.casefold(),
        invisibles_removed=invisibles,
        fullwidth_changed=(folded != stripped),
        rtl_control=rtl,
    )


def _rule(code: str, category: str, score: int, pattern: str, description: str) -> InjectionRule:
    return InjectionRule(
        code=code,
        category=category,
        score=score,
        pattern=re.compile(pattern, re.IGNORECASE | re.DOTALL),
        description=description,
    )


# ▎提示注入特征规则表。
# ▎分值口径：直接改写系统指令 / 索取系统提示 / 索取凭据 = 高危(30~45)；
# ▎辅助性话术(角色扮演、权威冒充、混淆) = 中低危(5~20)，靠累加触发阈值。
INJECTION_RULES: tuple[InjectionRule, ...] = (
    # ── 指令覆盖 ──
    _rule(
        "inj.override.zh", "指令覆盖", 40,
        r"(忽略|无视|忘记|忘掉|跳过|不要遵守|不再遵守|取消|作废)\s*(掉)?\s*"
        r"(?:(?:之前|以上|上面|前面|先前|刚才|上述|前述|前面提到|所有|全部|系统|开发者|你(?:的)?|原本)\s*(?:的)?\s*){0,3}"
        r"(指令|指示|要求|提示词?|规则|约束|设定|限制|安全策略)",
        "试图让模型丢弃系统/开发者指令",
    ),
    _rule(
        "inj.override.en", "指令覆盖", 40,
        r"\b(ignore|disregard|forget|override|bypass|discard)\b[^\n]{0,48}?"
        r"\b(previous|prior|above|earlier|former|system|developer|all)\b[^\n]{0,24}?"
        r"\b(instruction|prompt|rule|direction|guideline|constraint)s?\b",
        "英文指令覆盖话术",
    ),
    _rule(
        "inj.override.newtask", "指令覆盖", 30,
        r"(从现在开始|从现在起|从此刻起|接下来|下面开始)[^\n]{0,10}?"
        r"(你(要|必须|只需要|只能)|你的新?(角色|任务|目标|身份)是)",
        "重设模型任务边界",
    ),
    # ── 角色劫持 / 越狱 ──
    _rule(
        "inj.roleplay.jailbreak", "角色劫持", 35,
        r"(扮演|假装你是|假装自己是|充当|切换成?|进入)\s*"
        r"(DAN|dan|开发者模式|开发模式|上帝模式|越狱模式|无限制模式|不受限制|无审查|无过滤)",
        "越狱/无限制模式诱导",
    ),
    _rule(
        "inj.roleplay.en", "角色劫持", 30,
        r"\b(jailbreak|do anything now|developer mode|god mode|dan mode|unrestricted mode|"
        r"no restrictions? mode|act as an? (unfiltered|uncensored))\b",
        "英文越狱关键词",
    ),
    _rule(
        "inj.roleplay.persona", "角色劫持", 15,
        r"(你的(真实)?身份是|你其实是|你不是小菱|你不是助手|你不再受)[^\n]{0,24}",
        "否定助手身份",
    ),
    # ── 系统提示窃取 ──
    _rule(
        "inj.exfil.systemprompt.zh", "系统提示窃取", 40,
        r"(输出|显示|打印|重复|复述|告诉我|泄露|给出|贴出|翻译)[^\n]{0,18}?"
        r"(系统提示词?|系统指令|初始指令|预设指令|隐藏指令|你的提示词|你的设定|你的系统消息)",
        "索取系统提示词",
    ),
    _rule(
        "inj.exfil.systemprompt.en", "系统提示窃取", 40,
        r"\b(reveal|print|repeat|show|dump|leak|output|translate)\b[^\n]{0,32}?"
        r"\b(system prompt|initial instruction|hidden prompt|your instruction|system message)s?\b",
        "英文索取系统提示词",
    ),
    _rule(
        "inj.exfil.systemprompt.ask", "系统提示窃取", 30,
        r"(系统提示词?|系统指令|初始指令|隐藏指令|你的(设定|人格|角色卡))[^\n]{0,14}?"
        r"(是什么|有哪些|内容是|能(不能)?(说|给|告诉|发)|发我|给我看|贴一下|原文)",
        "探询系统提示词内容",
    ),
    # ── 凭据外带 ──
    _rule(
        "inj.exfil.credential.zh", "凭据外带", 45,
        r"(把|将|帮我)[^\n]{0,24}?"
        r"(密钥|私钥|api[\s_-]?key|token|令牌|密码|口令|凭据|凭证|\.env|环境变量|数据库密码|"
        r"管理员密码|用户密码|哈希)[^\n]{0,24}?"
        r"(发|发送|上传|提交|回传|粘贴|写到|传到|打包|post|upload|send|外带)",
        "诱导把凭据带出平台",
    ),
    _rule(
        "inj.exfil.credential.en", "凭据外带", 45,
        r"\b(send|post|upload|exfiltrate|paste|leak)\b[^\n]{0,32}?"
        r"\b(api[ _-]?key|secret|token|password|credential|private key|\.env)\b[^\n]{0,32}?"
        r"\b(to|into)\b\s*(https?://|webhook|pastebin|gist|邮箱|email)",
        "英文凭据外带话术",
    ),
    _rule(
        "inj.exfil.webhook", "凭据外带", 40,
        r"(https?://)?(discord(app)?\.com/api/webhooks|hooks\.slack\.com/services|"
        r"oapi\.dingtalk\.com/robot/send|qyapi\.weixin\.qq\.com/cgi-bin/webhook|"
        r"api\.telegram\.org/bot|pastebin\.com|gist\.githubusercontent)",
        "出现外部回传通道地址",
    ),
    # ── 工具滥用 / 破坏性操作 ──
    _rule(
        "inj.tool.shell", "工具滥用", 30,
        r"(执行|运行|调用|跑一下|帮我跑)[^\n]{0,16}?"
        r"(shell|bash|zsh|sh\b|powershell|cmd|终端|命令行|脚本|系统命令)",
        "诱导执行系统命令",
    ),
    _rule(
        "inj.tool.destructive", "工具滥用", 40,
        r"(rm\s+-rf\s+/|rm\s+-rf\s+~|chmod\s+777|mkfs\.|dd\s+if=|shutdown\s+-[hf]|"
        r"format\s+c:|drop\s+database|truncate\s+table|delete\s+from\s+users|"
        r":\(\)\s*\{\s*:\|:&\s*\};:|>\s*/dev/sda)",
        "出现破坏性命令特征",
    ),
    _rule(
        "inj.tool.sabotage", "工具滥用", 35,
        r"(关闭|禁用|停用|删除|清空|绕过|跳过|关掉)[^\n]{0,14}?"
        r"(审计|审批|日志|安全监控|风控|限流|鉴权|权限校验|策略引擎|告警)",
        "诱导关闭安全与审计机制",
    ),
    # ── 批量数据索取 ──
    _rule(
        "inj.data.bulk", "批量数据索取", 25,
        r"(导出|列出|dump|全量|批量)[^\n]{0,16}?"
        r"(所有用户|全部用户|用户表|账号列表|手机号|身份证|密码哈希|私钥|会话令牌)",
        "索取敏感批量数据",
    ),
    # ── 结构伪装 ──
    _rule(
        "inj.inject.tokens", "结构伪装", 30,
        r"(<\|im_start\|>|<\|im_end\|>|<\|system\|>|<\|endoftext\|>|###\s*(system|assistant)\s*:?|"
        r"\[\s*system\s*\]|<<sys>>|</?system>)",
        "伪造对话结构标记",
    ),
    _rule(
        "inj.inject.authority", "结构伪装", 20,
        r"(我是|作为|以)(平台)?(超级?管理员|开发者|运维人员|root|developer)[^\n]{0,16}?"
        r"(授权|允许|要求|命令|批准)[^\n]{0,16}?"
        r"(你)?(执行|关闭|删除|提升权限|绕过|跳过)",
        "冒充管理员下达高危指令",
    ),
    # ── 通过 markdown/图片外带 ──
    _rule(
        "inj.exfil.markdown", "凭据外带", 30,
        r"!\[[^\]\n]{0,80}\]\(\s*https?://[^)\s]{0,300}?[?&](data|q|text|payload|token|content|key|secret|d|c)=",
        "Markdown 图片夹带参数外传",
    ),
    _rule(
        "inj.exfil.markdown_any", "凭据外带", 20,
        r"!\[[^\]\n]{0,80}\]\(\s*https?://(?!(?:localhost|127\.0\.0\.1|\S*\.internal))[^)\s]{0,200}\?[^)\s]{0,200}\)",
        "Markdown 图片指向外站并携带查询串（任意参数名）",
    ),
    _rule(
        "inj.exfil.link_any", "凭据外带", 15,
        r"\[[^\]\n]{0,60}\]\(\s*https?://(?!(?:localhost|127\.0\.0\.1|\S*\.internal))[^)\s]{0,200}"
        r"[?&](?:data|q|text|payload|token|secret|content)=",
        "Markdown 链接夹带参数外传",
    ),
)

# 混淆信号：不是单条规则命中，而是归一化阶段的证据，按固定分值计入。
_OBFUSCATION_SCORE = {"invisibles": 20, "rtl": 25, "base64_blob": 15}
# 一段连续 base64 的最小长度；短串是正常哈希/ID，不作为证据。
_BASE64_BLOB = re.compile(r"[A-Za-z0-9+/]{160,}={0,2}")
_BASE64_HINT = re.compile(r"(忽略|指令|系统提示|ignore|instruction|prompt|password|token|secret)", re.IGNORECASE)


def _decode_base64_hint(blob: str) -> bool:
    """判断疑似 base64 的长串是否是"带指令语义"的载荷。"""
    import base64

    for candidate in (blob, blob + "=" * (-len(blob) % 4)):
        try:
            decoded = base64.b64decode(candidate, validate=False).decode("utf-8", "ignore")
        except (ValueError, TypeError):
            continue
        if _BASE64_HINT.search(decoded):
            return True
    return False


# ▎对一段文本跑完整注入检测：归一化 → 规则匹配 → 混淆证据 → 汇总分值。
def scan_injection(raw: str) -> dict:
    """扫描提示注入/越权诱导。

    Args:
        raw: 待检文本。

    Returns:
        dict: ``{"score": int, "blocked_hint": bool, "hits": [{code,category,score,description,evidence}],
        "obfuscation": {...}, "normalized_chars": int}``。是否阻断由策略阈值决定。
    """
    normalized = normalize_text(raw or "")
    text = normalized.text
    hits: list[dict] = []
    for rule in INJECTION_RULES:
        match = rule.pattern.search(text)
        if match is None:
            continue
        evidence = match.group(0).strip().replace("\n", " ")
        hits.append({
            "code": rule.code,
            "category": rule.category,
            "score": rule.score,
            "description": rule.description,
            "evidence": evidence[:120],
        })

    obfuscation: dict = {}
    if normalized.invisibles_removed:
        obfuscation["invisibles"] = normalized.invisibles_removed
        hits.append({
            "code": "obf.invisible", "category": "混淆规避",
            "score": _OBFUSCATION_SCORE["invisibles"],
            "description": "夹带零宽/不可见字符拆分关键词",
            "evidence": f"移除 {normalized.invisibles_removed} 个不可见字符",
        })
    if normalized.rtl_control:
        obfuscation["rtl_control"] = True
        hits.append({
            "code": "obf.rtl", "category": "混淆规避",
            "score": _OBFUSCATION_SCORE["rtl"],
            "description": "使用双向控制符制造显示/匹配错位",
            "evidence": "检测到 RTL/嵌入控制符",
        })
    for blob in _BASE64_BLOB.findall(raw or ""):
        if _decode_base64_hint(blob):
            obfuscation["base64_blob"] = len(obfuscation.get("base64_blob", "")) or len(blob)
            hits.append({
                "code": "obf.base64", "category": "混淆规避",
                "score": _OBFUSCATION_SCORE["base64_blob"],
                "description": "长 base64 串解码后含指令语义",
                "evidence": blob[:48] + "…",
            })
            break

    # 同一类别重复命中只取最高分一次，再加其余的一半，避免长文本刷分。
    seen: set[str] = set()
    total = 0.0
    for item in sorted(hits, key=lambda h: int(h["score"]), reverse=True):
        category = str(item["category"])
        raw_score = float(item["score"])
        total += raw_score if category not in seen else raw_score * 0.5
        seen.add(category)
    return {
        "score": min(_SCORE_CAP, int(round(total))),
        "hits": hits,
        "obfuscation": obfuscation,
        "normalized_chars": len(text),
    }


# ── 脱敏 ──────────────────────────────────────────────────────────────────

# ▎一条脱敏规则：命中后按 ``replacement`` 模板替换。
@dataclass(frozen=True)
class RedactionRule:
    """敏感信息脱敏规则。"""

    kind: str
    pattern: re.Pattern[str]
    replacement: str
    description: str


def _redact(kind: str, pattern: str, replacement: str, description: str, flags: int = 0) -> RedactionRule:
    return RedactionRule(
        kind=kind,
        pattern=re.compile(pattern, flags),
        replacement=replacement,
        description=description,
    )


# ▎敏感信息脱敏字典。
# ▎顺序有意义：长结构（私钥块、连接串）先跑，避免被「password=」这类短规则切碎。
REDACTION_RULES: tuple[RedactionRule, ...] = (
    _redact(
        "private_key", r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?-----END [A-Z0-9 ]*PRIVATE KEY-----",
        "«REDACTED:private_key»", "PEM 私钥块", re.DOTALL,
    ),
    _redact(
        "jwt", r"\beyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\b",
        "«REDACTED:jwt»", "JWT 令牌",
    ),
    _redact(
        "ds_key", r"\bsk-[A-Za-z0-9_\-]{16,}\b", "«REDACTED:api_key»", "OpenAI/DeepSeek 风格 API Key",
    ),
    _redact(
        "aws_key", r"\b(AKIA|ASIA)[0-9A-Z]{16}\b", "«REDACTED:aws_key»", "AWS Access Key",
    ),
    _redact(
        "db_uri",
        r"\b(?:mysql|mariadb|postgres(?:ql)?|mongodb(?:\+srv)?|redis|amqp)(?:\+\w+)?://"
        r"[^\s:/@]+:[^\s@/]+@[^\s]+",
        "«REDACTED:db_uri»", "带口令的数据库连接串",
    ),
    _redact(
        "webhook",
        r"https?://(?:oapi\.dingtalk\.com/robot/send\?access_token=|qyapi\.weixin\.qq\.com/cgi-bin/webhook/send\?key=|"
        r"hooks\.slack\.com/services/|discord(?:app)?\.com/api/webhooks/)[A-Za-z0-9_\-/]{8,}",
        "«REDACTED:webhook»", "带 token 的机器人/Webhook 地址",
    ),
    _redact(
        "secret_assignment",
        r"(?i)\b([A-Za-z0-9_.\-]{0,24}(?:api[_-]?key|secret|token|passw(?:or)?d|passwd|pwd|"
        r"access[_-]?key|private[_-]?key))['\"]?\s*[:=]\s*['\"]?([^\s'\"，,;]{6,})['\"]?",
        r"\1=«REDACTED:secret»", "键值对里的口令/密钥",
    ),
    _redact(
        "id_card", r"(?<!\d)(?:[1-9]\d{5}(?:19|20)\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])\d{3}[\dXx])(?!\d)",
        "«REDACTED:id_card»", "身份证号",
    ),
    _redact(
        "phone_cn", r"(?<!\d)1[3-9]\d{9}(?!\d)", "«REDACTED:phone»", "中国大陆手机号",
    ),
    _redact(
        "internal_ip",
        r"\b(?:10\.\d{1,3}\.\d{1,3}\.\d{1,3}|192\.168\.\d{1,3}\.\d{1,3}|"
        r"172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3})\b",
        "«REDACTED:internal_ip»", "内网 IP",
    ),
    _redact(
        "email", r"\b[\w.+-]{1,64}@(?!example\.)[\w-]{1,63}(?:\.[\w-]{2,63}){1,4}\b",
        "«REDACTED:email»", "邮箱地址",
    ),
)


def redact_text(raw: str, *, kinds: Optional[Iterable[str]] = None) -> tuple[str, list[dict]]:
    """按字典脱敏文本。

    Args:
        raw: 原始文本。
        kinds: 只应用这些 kind；为空表示全部应用。

    Returns:
        tuple: (脱敏后文本, [{kind, count, description}])
    """
    if not raw:
        return raw or "", []
    allowed = {str(item) for item in kinds} if kinds else None
    text = raw
    applied: list[dict] = []
    for rule in REDACTION_RULES:
        if allowed is not None and rule.kind not in allowed:
            continue
        text, count = rule.pattern.subn(rule.replacement, text)
        if count:
            applied.append({"kind": rule.kind, "count": count, "description": rule.description})
    return text, applied


# ── 动作风险词表 ──────────────────────────────────────────────────────────

# 系统命令/进程/数据库执行类动作。
SHELL_TOKENS: tuple[str, ...] = (
    "shell", "exec", "execute", "command", "cmd", "bash", "sh", "powershell", "ssh", "script",
    "pty", "sql", "query", "db", "database", "kill", "terminate", "restart", "systemctl",
    "docker", "kubectl", "cron", "crontab", "sudo", "spawn", "process",
)
# 写类动作（改变平台数据或状态）。
WRITE_TOKENS: tuple[str, ...] = (
    "write", "create", "update", "delete", "remove", "drop", "truncate", "patch", "apply",
    "set", "insert", "upsert", "grant", "revoke", "reset", "rollback", "import", "deploy",
    "publish", "enable", "disable", "toggle", "archive", "restore",
    # 复核补洞：批准/决定/启停/编辑/身份类动词此前全部漏判
    "decide", "approve", "reject", "close", "extend", "trigger", "start", "stop", "cancel",
    "run", "invoke", "invite", "add", "adduser", "rotate", "change", "modify", "edit", "save",
    "upload", "send", "post", "submit", "bind", "assign", "activate", "unlock", "lock",
    "impersonate", "suspend", "merge", "move", "rename", "commit", "push", "checkout",
)
# 生产配置/权限/凭据类资源，改动影响面最大。
PRODUCTION_TOKENS: tuple[str, ...] = (
    "production", "prod_config", "system_config", "systemconfig", "llm_config", "api_config",
    "policy", "permission", "role", "rbac", "secret", "credential", "webhook", "mcp",
    "sandbox:deploy", "user:password", "jwt", "env", "dotenv", "release", "deploy", "gateway",
    "billing", "refund", "enterprise", "pentest", "governance", "studio",
)

# 外带通道：能把数据带出平台的工具/动作（HTTP、邮件、上传、浏览器导航）。
EGRESS_TOKENS: tuple[str, ...] = (
    "curl", "wget", "http", "https", "request", "fetch", "egress", "webhook", "email", "mail",
    "smtp", "upload", "export", "download", "browser", "navigate", "screenshot", "proxy",
    "https_proxy", "dns", "socket", "netcat", "nc",
)

# 明确只读、可安全放行的工具编码（工具闸门的"未声明工具"白名单）。
READ_ONLY_TOOL_CODES: frozenset[str] = frozenset({
    "list_projects", "get_project_detail", "list_review_tasks", "list_review_issues",
    "list_code_files", "list_rules", "list_reports", "dashboard_summary", "detect_language",
    "list_agent_skills", "admin_list_users", "admin_list_roles", "admin_governance_overview",
    "admin_list_agents", "admin_list_approvals", "admin_list_agent_release_approvals",
    "admin_system_status", "list_alerts", "recall_knowledge", "ask_user",
})


# 词元分割：``admin_set_user_role`` → {admin,set,user,role}，避免 ``settings`` 命中 ``set``。
_WORD_SPLIT = re.compile(r"[^a-z0-9]+")


# ▎把一次工具调用的动作/工具/资源归一成风险标签集合。
def classify_action(action: str, tool_code: str = "", resource: str = "") -> set[str]:
    """识别动作的风险类别。

    判定分两种粒度：单词词元做精确匹配（``set`` 不命中 ``settings``），
    含 ``_``/``:``/空格的复合词做子串匹配（``system_config`` 能命中 ``system_config:update``）。

    Args:
        action: 动作编码。
        tool_code: 工具编码。
        resource: 资源编码。

    Returns:
        set[str]: 可能包含 ``shell`` / ``write`` / ``production``。
    """
    haystack = " ".join(part for part in (action, tool_code, resource) if part).casefold()
    if not haystack:
        return set()
    words = {word for word in _WORD_SPLIT.split(haystack) if word}

    def _hit(tokens: tuple[str, ...]) -> bool:
        for token in tokens:
            if token in words:
                return True
            if any(sep in token for sep in ("_", ":", " ")) and token in haystack:
                return True
        return False

    flags: set[str] = set()
    if _hit(SHELL_TOKENS):
        flags.add("shell")
    if _hit(WRITE_TOKENS):
        flags.add("write")
    if _hit(PRODUCTION_TOKENS):
        flags.add("production")
    if _hit(EGRESS_TOKENS):
        flags.add("egress")
    return flags


# ── 策略默认值与预设 ──────────────────────────────────────────────────────

# 默认策略：检测、脱敏、审计全开，但**不做任何硬拦截**（等价 monitor 口径）。
# 理由：小菱的工具在网关里以 ``chat_assistant`` / ``operations`` 等编码出现，
# 默认就把生产配置或 shell 一刀切会在升级当天掐断既有管理流程。
# 真正收紧靠 preset（strict / max），本次上线已按 max 落库。
DEFAULT_POLICY: dict = {
    "enabled": True,
    "mode": "balanced",
    "kill_switch": False,
    "kill_switch_reason": "",
    # 受管控的 Agent 编码：小菱在网关上会以这些身份出现（surface_agent_identity 返回
    # chat_assistant；运维动作是 operations；调度是 orchestrator）。
    "governed_agents": ["admin_copilot", "chat_assistant", "manager", "operations", "orchestrator"],
    # 会话级守卫只作用于这些面；admin 面 = 管理员小菱。
    "session_guard_surfaces": ["admin"],
    # 会话级守卫强制的面（IP/超时/长度）；与 session_guard_surfaces 取交集。
    "ip_allowlist": [],
    "trust_proxy_headers": False,
    # 只有 peer 落在可信代理网段内时才采信 X-Forwarded-For，并从右往左取第一个不可信地址。
    "trusted_proxies": [],
    "bind_session_to_ip": False,
    "session_idle_timeout_seconds": 0,
    "session_absolute_timeout_seconds": 0,
    "max_message_chars": 0,
    # 注入检测
    "injection_scan_enabled": True,
    "injection_block_threshold": 60,
    "injection_warn_threshold": 30,
    "injection_blocked_surfaces": ["admin"],
    # 脱敏
    "redaction_enabled": True,
    "redact_in_audit": True,
    "redaction_kinds": [],
    # 工具闸门
    "tool_allowlist": [],
    "tool_denylist": [],
    "deny_shell": False,
    "deny_write_actions": False,
    "deny_production_config": False,
    "deny_egress": False,
    "require_approval_for_writes": False,
    "dangerous_tools_require_approval": False,
    # 名字里看不出风险、也不在只读白名单里的工具：严格/最顶级预设下升级审批而不是直接放行。
    "escalate_unknown_tools": False,
    # 工具输出（源码、网页、团队结果）回流给模型前做注入扫描。
    "scan_tool_output": True,
    # 安全运营工具：小菱要能自动化攻防，就得让这些工具有序穿过"禁止改生产配置/禁止外带"的
    # 一刀切。默认只放行只读研判类；封禁/解封/启动渗透属于状态变更，要放行需管理员显式加进来。
    "security_ops_tools": [
        "secops.threat_digest",
        "secops.surface_audit",
        "secops.block_candidates",
        "secops.baseline_check",
        "secops.patrol_status",
    ],
    "max_tool_calls_per_turn": 0,
    "max_tool_calls_per_hour": 0,
    # 审计与告警
    "audit_events_enabled": True,
    "alert_on_block": True,
    "step_up_window_seconds": 0,
}

# 三档预设：只覆盖差异项，其余沿用 DEFAULT_POLICY。
PRESETS: dict[str, dict] = {
    # 观察模式：只记账不拦。
    "monitor": {
        "mode": "monitor",
        "ip_allowlist": [],
        "bind_session_to_ip": False,
        "session_idle_timeout_seconds": 0,
        "session_absolute_timeout_seconds": 0,
        "injection_scan_enabled": True,
        "injection_block_threshold": 101,
        "injection_warn_threshold": 30,
        "redaction_enabled": True,
        "redaction_kinds": [],
        "tool_allowlist": [],
        "deny_shell": False,
        "deny_write_actions": False,
        "deny_production_config": False,
        "deny_egress": False,
        "require_approval_for_writes": False,
        "dangerous_tools_require_approval": False,
        "escalate_unknown_tools": False,
        "scan_tool_output": True,
        "max_tool_calls_per_turn": 0,
        "max_tool_calls_per_hour": 0,
        "audit_events_enabled": True,
        "alert_on_block": False,
    },
    # 严格：拦注入、封 shell 与生产配置、写操作强制审批、有预算上限。
    "strict": {
        "mode": "strict",
        "ip_allowlist": [],
        "bind_session_to_ip": True,
        "session_idle_timeout_seconds": 3600,
        "session_absolute_timeout_seconds": 43200,
        "max_message_chars": 40000,
        "injection_scan_enabled": True,
        "injection_block_threshold": 50,
        "injection_warn_threshold": 25,
        "injection_blocked_surfaces": ["admin", "user"],
        "redaction_enabled": True,
        "redact_in_audit": True,
        "redaction_kinds": [],
        "tool_allowlist": [],
        "tool_denylist": [],
        "deny_shell": True,
        "deny_write_actions": False,
        "deny_production_config": True,
        "deny_egress": False,
        # 名字看不出风险又不在只读白名单的工具：升级审批，而不是静默放行。
        "escalate_unknown_tools": True,
        "scan_tool_output": True,
        "require_approval_for_writes": True,
        "dangerous_tools_require_approval": True,
        "max_tool_calls_per_turn": 40,
        "max_tool_calls_per_hour": 240,
        "audit_events_enabled": True,
        "alert_on_block": True,
        "step_up_window_seconds": 900,
    },
    # 最顶级：最严口径。IP 白名单仍需管理员按实际来源填，这里不预置，避免一键锁死。
    "max": {
        "mode": "max",
        "ip_allowlist": [],
        "trust_proxy_headers": False,
        "bind_session_to_ip": True,
        "session_idle_timeout_seconds": 1800,
        "session_absolute_timeout_seconds": 28800,
        "max_message_chars": 20000,
        "injection_scan_enabled": True,
        "injection_block_threshold": 40,
        "injection_warn_threshold": 20,
        "injection_blocked_surfaces": ["admin", "user"],
        "redaction_enabled": True,
        "redact_in_audit": True,
        "redaction_kinds": [],
        "tool_allowlist": [],
        "tool_denylist": [],
        "deny_shell": True,
        "deny_write_actions": False,
        "deny_production_config": True,
        "deny_egress": False,
        "escalate_unknown_tools": True,
        "scan_tool_output": True,
        "require_approval_for_writes": True,
        "dangerous_tools_require_approval": True,
        "max_tool_calls_per_turn": 24,
        "max_tool_calls_per_hour": 120,
        "audit_events_enabled": True,
        "alert_on_block": True,
        "step_up_window_seconds": 600,
    },
}

# 预设顺序与人类可读名称，供前端下拉直接把 PRESETS 渲染出来。
PRESET_LABELS: tuple[tuple[str, str], ...] = (
    ("monitor", "观察模式（只记录不拦截）"),
    ("balanced", "平衡模式（默认）"),
    ("strict", "严格模式"),
    ("max", "最顶级（管理小菱推荐）"),
)


# ▎把预设键解析成完整策略字典：未知键回落到默认策略。
def resolve_preset(name: str) -> dict:
    """返回某档预设的完整策略。

    Args:
        name: ``monitor`` / ``balanced`` / ``strict`` / ``max``。

    Returns:
        dict: 完整策略；未知名称返回默认策略副本。
    """
    policy = dict(DEFAULT_POLICY)
    if name != "balanced":
        policy.update(PRESETS.get(name, {}))
    policy["preset"] = name if name in PRESETS or name == "balanced" else "balanced"
    return policy


# ▎解析并校验 CIDR/单 IP 白名单。
def parse_networks(
    entries: Iterable[str],
) -> list[Union[ipaddress.IPv4Network, ipaddress.IPv6Network]]:
    """把字符串列表解析成网络对象，非法项抛出 ValueError。

    Args:
        entries: IP 或 CIDR 字符串列表。

    Returns:
        list: ipaddress 网络对象。
    """
    networks: list[Union[ipaddress.IPv4Network, ipaddress.IPv6Network]] = []
    for raw in entries or []:
        value = str(raw or "").strip()
        if not value:
            continue
        networks.append(ipaddress.ip_network(value, strict=False))
    return networks


# ▎判断来访 IP 是否命中白名单；白名单为空时视为不限制。
def ip_allowed(client_ip: str, entries: Iterable[str]) -> bool:
    """校验来源 IP。

    Args:
        client_ip: 请求来源 IP。
        entries: 白名单条目。

    Returns:
        bool: 允许返回 True；白名单为空返回 True。
    """
    networks = list(entries or [])
    if not networks:
        return True
    try:
        address = ipaddress.ip_address(str(client_ip or "").strip())
    except ValueError:
        return False
    # 双栈部署里 ::ffff:10.1.2.3 与 10.1.2.3 是同一来源，先归一。
    mapped = getattr(address, "ipv4_mapped", None)
    if mapped is not None:
        address = mapped
    parsed = parse_networks(networks)
    return any(address in network for network in parsed)


# 白名单里需要留出本机回环，否则一键最顶级会把管理员自己锁在外面。
LOOPBACK_NETWORKS: tuple[str, ...] = ("127.0.0.1/32", "::1/128")
