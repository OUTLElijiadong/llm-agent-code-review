"""PHP Web 项目攻击面与污点静态分析器 (v3.2 新增)

设计动机(源自 Graph Engineering / 结构主义审计方法论):
  传统逐文件扫描把每个 PHP 文件当作孤立实体,丢掉了「用户输入(source)→
  危险函数(sink)」这一安全审计最关键的**关系结构**。本模块在调 LLM 之前,
  用确定性静态分析把这份结构**免费**抽取出来,作为共享事实注入审计黑板,
  让 LLM 把精力集中在「判断这条污点链是否可利用」而非「大海捞针找 sink」。

对应文章的两个核心动作:
  - 共时性结构: 攻击面地图(路由/入口/sink/过滤器的关联拓扑)
  - Information:  把 PHP 超全局输入、危险函数、消毒器显式建模为可推理的事实

覆盖 PHP 生态典型的注入面(泛微 / iWebShop / 织梦 / Discuz 等 CMS 高发点):
  - 超全局 source: $_GET/$_POST/$_REQUEST/$_COOKIE/$_FILES/$_SERVER ...
  - SQL sink:     mysql_query / mysqli_query / $db->query / ->execute(拼接) ...
  - 命令 sink:    eval / system / exec / shell_exec / passthru / popen / 反引号
  - 文件 sink:    file_put_contents / fwrite / fopen(w) / unlink / include/require(变量)
  - 反序列化:     unserialize / maybe_unserialize
  - XSS sink:     echo/print 直接输出超全局
  - 消毒器:       intval / (int) / addslashes / htmlspecialchars / mysqli_real_escape_string ...
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List, Tuple

# ---------------------------------------------------------------------------
# Source(污点源)
# ---------------------------------------------------------------------------
_SUPERGLOBAL_RE = re.compile(
    r"\$_(?:GET|POST|REQUEST|COOKIE|FILES|SERVER|ENV|SESSION)\b"
)

# 各 sink 类别对应的危险函数正则(命中且参数中含变量/超全局才记为污点候选)
_SQL_SINK_RE = re.compile(
    r"\b(?:mysql_query|mysqli_query|mysql_unbuffered_query|pg_query|sqlite_query"
    r"|mssql_query|oci_execute)\s*\("
    r"|->\s*(?:query|exec|execute|prepare|getAll|getRow|getOne|getCol|getAssoc"
    r"|select|delete|update|insert|replace)\s*\(",
    re.IGNORECASE,
)
_CMD_SINK_RE = re.compile(
    r"\b(?:eval|assert|system|exec|shell_exec|passthru|popen|proc_open"
    r"|pcntl_exec|create_function|call_user_func(?:_array)?|preg_replace\s*\(\s*['\"].*e['\"]"
    r"|mb_ereg_replace|ob_start)\s*\("
    r"|`[^`\n]*\$[^`\n]*`",  # 反引号命令执行且含变量
    re.IGNORECASE,
)
_FILE_WRITE_RE = re.compile(
    r"\b(?:file_put_contents|fwrite|fputs|move_uploaded_file|copy|rename"
    r"|unlink|rmdir|mkdir|chmod|touch|symlink|link)\s*\(",
    re.IGNORECASE,
)
_FILE_READ_RE = re.compile(
    r"\b(?:file_get_contents|readfile|fpassthru|highlight_file|show_source"
    r"|parse_ini_file|file\s*\()\s*\(?"
    r"|\b(?:file_get_contents|readfile|highlight_file|show_source)\s*\(",
    re.IGNORECASE,
)
_INCLUDE_RE = re.compile(
    r"\b(?:include|include_once|require|require_once)\s*\(?\s*[\$@]",
    re.IGNORECASE,
)
_DESERIALIZE_RE = re.compile(
    r"\b(?:unserialize|maybe_unserialize|yaml_parse|wddx_deserialize)\s*\(",
    re.IGNORECASE,
)
_XSS_SINK_RE = re.compile(
    r"\b(?:echo|print|printf|sprintf|die|exit)\b\s*[\(]?\s*\$_(?:GET|POST|REQUEST|COOKIE|SERVER)",
    re.IGNORECASE,
)
_SSRF_RE = re.compile(
    r"\b(?:curl_init|fsockopen|pfsockopen|stream_socket_client|file_get_contents"
    r"|fopen|get_headers|parse_url)\s*\(",
    re.IGNORECASE,
)

# 消毒器(命中则降低该链的可疑度)
_SANITIZER_RE = re.compile(
    r"\b(?:intval|floatval|abs|ceil|floor|addslashes|stripslashes|htmlspecialchars"
    r"|htmlentities|mysqli_real_escape_string|mysql_real_escape_string|pg_escape_string"
    r"|sqlite_escape_string|escapeshellarg|escapeshellcmd|basename|realpath|pathinfo"
    r"|filter_var|filter_input|preg_quote|urlencode|rawurlencode|json_encode"
    r"|I\s*\(|is_numeric|ctype_digit)\s*\("
    r"|\(\s*(?:int|float|double|bool)\s*\)\s*\$",
    re.IGNORECASE,
)

# 文件上传白名单校验线索(出现则上传点风险降低)
_UPLOAD_GUARD_RE = re.compile(
    r"\b(?:getimagesize|exif_imagetype|finfo_file|mime_content_type"
    r"|pathinfo\s*\([^)]*PATHINFO_EXTENSION|in_array\s*\(\s*\w*ext|allow(?:ed)?_?(?:type|ext))"
    r"|\.(?:jpg|jpeg|png|gif)\b",
    re.IGNORECASE,
)

# 鉴权线索: session 校验 / 登录判断 / 权限函数
_AUTH_RE = re.compile(
    r"\b(?:checkLogin|checklogin|isLogin|check_admin|checkAdmin|checkPurview"
    r"|check_power|checkAuth|require_login|is_admin|check_right|checkPermission"
    r"|adminLogined|CheckPurview|_SESSION\s*\[\s*['\"](?:admin|uid|user_id|isadmin))"
    r"|\$_(?:SESSION)\s*\[",
    re.IGNORECASE,
)


@dataclass
class SinkHit:
    """一次危险函数命中"""
    line: int
    category: str          # sqli / rce / file_write / file_read / lfi / unserialize / xss / ssrf
    func: str              # 命中的函数名(截断)
    snippet: str           # 该行代码(截断)
    tainted: bool          # 同行或邻近行是否出现超全局 source
    sanitized: bool        # 该行是否出现消毒器
    var: str = ""          # 流入 sink 的变量名(若可识别)


@dataclass
class FileProfile:
    """单文件的攻击面画像"""
    file_path: str
    sources: List[int] = field(default_factory=list)        # 出现超全局的行
    sinks: List[SinkHit] = field(default_factory=list)
    sanitizer_lines: List[int] = field(default_factory=list)
    has_auth: bool = False
    has_upload_guard: bool = False
    superglobal_count: int = 0

    @property
    def tainted_sinks(self) -> List[SinkHit]:
        return [s for s in self.sinks if s.tainted and not s.sanitized]

    @property
    def risk_score(self) -> int:
        """用于排序的确定性风险分(越高越可疑)"""
        score = 0
        weight = {
            "rce": 10, "sqli": 8, "unserialize": 9, "lfi": 8,
            "file_write": 6, "file_read": 5, "xss": 5, "ssrf": 6,
        }
        for s in self.sinks:
            base = weight.get(s.category, 3)
            if s.tainted:
                base *= 2
            if s.sanitized:
                base //= 2
            score += base
        if self.superglobal_count and self.sinks:
            score += min(self.superglobal_count, 5)
        if not self.has_auth:
            score += 2  # 无鉴权线索的文件优先
        return score


# 类别 → (正则, 中文风险名, 默认严重度, CWE, OWASP)
_CATEGORY_META: Dict[str, Tuple[re.Pattern, str, str, str, str]] = {
    "rce": (_CMD_SINK_RE, "命令/代码执行", "严重", "CWE-78", "A03:2021-Injection"),
    "sqli": (_SQL_SINK_RE, "SQL 注入", "高", "CWE-89", "A03:2021-Injection"),
    "unserialize": (_DESERIALIZE_RE, "不安全的反序列化", "高", "CWE-502",
                    "A08:2021-Software and Data Integrity Failures"),
    "lfi": (_INCLUDE_RE, "本地文件包含/任意文件加载", "高", "CWE-22",
            "A01:2021-Broken Access Control"),
    "file_write": (_FILE_WRITE_RE, "任意文件写入/删除", "高", "CWE-73",
                   "A01:2021-Broken Access Control"),
    "file_read": (_FILE_READ_RE, "任意文件读取/路径遍历", "中", "CWE-22",
                  "A01:2021-Broken Access Control"),
    "xss": (_XSS_SINK_RE, "反射型 XSS", "中", "CWE-79", "A03:2021-Injection"),
    "ssrf": (_SSRF_RE, "服务端请求伪造", "中", "CWE-918",
             "A10:2021-Server-Side Request Forgery"),
}


def _detect_var_at_sink(line: str) -> str:
    """尽力从 sink 行提取流入的变量名($xxx),用于跨行污点追踪"""
    m = re.search(r"\(\s*\$?(\w+)", line)
    if m:
        return "$" + m.group(1)
    m = re.search(r"(\$\w+)\s*\.\s*", line)  # 字符串拼接 $a . "..."
    return m.group(1) if m else ""


def profile_php_file(file_path: str, content: str) -> FileProfile:
    """对单个 PHP 文件做攻击面画像(纯正则,无 LLM 成本)"""
    prof = FileProfile(file_path=file_path)
    if not content:
        return prof
    lines = content.splitlines()
    # 变量赋值行映射: $var = ...$_GET... → 记录 var 被污染
    tainted_vars: Dict[str, int] = {}

    for idx, raw in enumerate(lines, start=1):
        line = raw.strip()
        if not line or line.startswith(("//", "*", "/*", "#")):
            continue

        has_source = bool(_SUPERGLOBAL_RE.search(line))
        if has_source:
            prof.sources.append(idx)
            prof.superglobal_count += len(_SUPERGLOBAL_RE.findall(line))
            # 记录被超全局污染的变量:  $x = $_GET['a'];  $x = $_POST['b'] . "sql";
            assign = re.match(r"(\$\w+)\s*=\s*(.+)", line)
            if assign:
                tainted_vars[assign.group(1)] = idx

        if _SANITIZER_RE.search(line):
            prof.sanitizer_lines.append(idx)
        if not prof.has_auth and _AUTH_RE.search(line):
            prof.has_auth = True
        if not prof.has_upload_guard and _UPLOAD_GUARD_RE.search(line):
            prof.has_upload_guard = True

        # 传播: 若本行用了已知被污染的变量,视为污点延续
        line_tainted_vars = {v for v in tainted_vars if v in line}

        for category, (regex, _cn, _sev, _cwe, _owasp) in _CATEGORY_META.items():
            m = regex.search(line)
            if not m:
                continue
            # 判断污点: 同行直接含超全局,或 sink 参数引用了被污染变量
            var = _detect_var_at_sink(line)
            tainted = has_source or (var and var in tainted_vars) or bool(
                line_tainted_vars and var in line_tainted_vars
            )
            sanitized = bool(_SANITIZER_RE.search(line))
            prof.sinks.append(SinkHit(
                line=idx,
                category=category,
                func=(m.group(0)[:40] if m else category),
                snippet=line[:200],
                tainted=tainted,
                sanitized=sanitized,
                var=var,
            ))
    return prof


def category_meta(category: str) -> Tuple[str, str, str, str]:
    """返回 (中文风险名, 默认严重度, CWE, OWASP)"""
    meta = _CATEGORY_META.get(category)
    if not meta:
        return ("危险接收点", "中", "", "")
    return meta[1], meta[2], meta[3], meta[4]


# ---------------------------------------------------------------------------
# 项目级攻击面地图
# ---------------------------------------------------------------------------

@dataclass
class AttackSurface:
    """项目级攻击面地图(共时性结构:某一时刻全项目的 source→sink 拓扑)"""
    file_profiles: List[FileProfile] = field(default_factory=list)

    @property
    def ranked_files(self) -> List[FileProfile]:
        """按确定性风险分降序,供扫描优先级排序(替代文件名关键词启发式)"""
        return sorted(self.file_profiles, key=lambda p: p.risk_score, reverse=True)

    @property
    def hot_sinks(self) -> List[Tuple[FileProfile, SinkHit]]:
        """所有「被污染且未消毒」的高危 sink,按文件风险分排序"""
        out: List[Tuple[FileProfile, SinkHit]] = []
        for prof in self.ranked_files:
            for s in prof.tainted_sinks:
                out.append((prof, s))
        return out

    def to_blackboard_facts(self, limit: int = 40) -> List[str]:
        """把攻击面压缩成可读的事实行,注入审计黑板(渐进式加载的顶层摘要).

        当 sink 数量庞大(如泛微数千个)时,按类别分组并给出代表样本,
        避免被单一高噪类别(如反射型 XSS)淹没,保证 RCE/SQLi/LFI 等高价值
        类别一定进入 LLM 视野。
        """
        hot = self.hot_sinks
        if not hot:
            return []
        # 类别优先级(高价值漏洞排前)
        prio = {"rce": 0, "unserialize": 1, "sqli": 2, "lfi": 3,
                "file_write": 4, "ssrf": 5, "file_read": 6, "xss": 7}
        by_cat: Dict[str, List[Tuple[FileProfile, SinkHit]]] = {}
        for prof, sink in hot:
            by_cat.setdefault(sink.category, []).append((prof, sink))

        facts: List[str] = []
        total = len(hot)
        facts.append(
            f"全项目共 {total} 处污点 sink,分布: "
            + ", ".join(f"{category_meta(c)[0]}×{len(v)}" for c, v in
                        sorted(by_cat.items(), key=lambda kv: prio.get(kv[0], 9)))
        )
        # 每个类别按优先级取代表样本,直到达到 limit
        per_cat = max(2, limit // max(1, len(by_cat)))
        for cat in sorted(by_cat, key=lambda c: prio.get(c, 9)):
            cn, sev, cwe, _ = category_meta(cat)
            for prof, sink in by_cat[cat][:per_cat]:
                if len(facts) >= limit:
                    break
                facts.append(
                    f"[{sev}][{cn}] {prof.file_path}:L{sink.line} "
                    f"`{sink.snippet[:80]}`"
                    + ("" if prof.has_auth else " (无鉴权线索)")
                )
        return facts[:limit]
