"""审查发现的语义一致性判定（确定性规则，不调用模型）。

合并器（issue_merger）、协同聚合器（finding_aggregator）和存量检测脚本
（scripts/detect_mixed_issues.py）共用 claims_are_coherent，避免各处口径漂移。

背景：线上审查任务 #236 的两条记录（#3956 文档示例跨代码块引用变量、#3961 两个类的
命名及文档衔接）类别显示“安全漏洞”，来源明细里却混着“签名载荷可读性”“不可信反序列化
风险”这类另一主题的安全发现。代码里能复现的成因有三处：
1. 合并只看“同一段源码引文或同一调用点”，不看主题；文档类发现引用了 sink 所在行，
   就被并入该行的安全发现。
2. 合并后标题、描述、建议各取簇内最长的一条，类别和 CWE 又取自另一条，记录拼成
   “标题是 A、类别是 B”。
3. 解析层按子串推断 CWE，description、nodes 里的 des 会被推断成弱加密。
本模块给第 1、2 点提供判定；第 3 点在 result_parser 里修正。
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

# 模型常把类别标成这些值表示“安全类”。
SECURITY_ISSUE_TYPES = {
    "安全", "安全漏洞", "安全问题", "security", "security_issue",
    "security_vulnerability", "vulnerability", "漏洞",
}
SECURITY_LANGUAGE_RE = re.compile(
    r"漏洞|注入|越权|未授权|未认证|未验证|鉴权绕过|认证绕过|身份验证绕过|"
    r"访问控制|跨站|权限提升|权限逃逸|敏感信息泄露|信息泄露|数据泄露|"
    r"动态\s*SQL|(?:SQL|查询).{0,30}(?:拼接|拼入|插入|插值|字符串拼装)|"
    r"(?:用户|请求|外部|不可信).{0,30}(?:拼接|拼入|插入|插值|未经转义|未转义).{0,30}(?:SQL|查询)|"
    r"对象级(?:授权|权限|访问控制)|跨租户|跨账号|租户隔离|账号隔离|"
    r"任意文件|路径穿越|目录穿越|反序列化|远程代码执行|命令执行|代码执行|"
    r"硬编码凭据|凭据泄露|密钥泄露|密码泄露|"
    r"\b(?:SQLi|XSS|CSRF|SSRF|RCE|XXE)\b|"
    r"\b(?:IDOR|BOLA|injection|vulnerabilit(?:y|ies)|exploit|unauthori[sz]ed|unauthenticated|"
    r"privilege escalation|privilege bypass|auth(?:entication|orization)? bypass|"
    r"access control|object[- ]level (?:authorization|access control)|"
    r"\bdynamic\s+sql\b|"
    r"(?:user[- ]controlled|user[- ]input|untrusted|request[- ]provided).{0,80}(?:sql|query)|"
    r"(?:sql|query).{0,80}(?:interpolat\w*|concatenat\w*|without escaping|unescaped)|"
    r"broken object[- ]level authorization|cross[- ]tenant|tenant isolation|"
    r"path traversal|directory traversal|remote code execution|"
    r"command execution|command injection|code execution|deseriali[sz]ation|"
    r"credential exposure|credential leak|secret leak|information disclosure|data exposure)\b",
    re.IGNORECASE,
)

# 主题重叠的阈值。取值依据：手工构造 12 对“同一问题、措辞不同”（全部要求通过，实测共享权重
# 3 到 10、重叠比例 0.24 到 0.90）和 10 对“同一 sink、不同主题”（全部要求拆开，其中只有 2 对
# 会走到本步骤，实测权重 0 到 1、比例不超过 0.05，其余由安全内容差异直接拦下）。样本很少，
# 且没有用线上语料校准，正例最窄余量是权重 3、比例 0.24。取向是宁可多拆（多出一条独立记录）
# 也不误并（一条记录混入另一主题）。
TOPIC_MIN_SHARED_WEIGHT = 2
TOPIC_MIN_OVERLAP = 0.2

_CWE_RE = re.compile(r"(?:CWE)?[-_ ]?(\d+)", re.IGNORECASE)
_ASCII_WORD_RE = re.compile(r"[a-z][a-z0-9_]{2,}")
_CJK_RUN_RE = re.compile(r"[一-鿿]+")

# 几乎出现在所有发现里的词，共享它们不能说明是同一个主题。
_GENERIC_BIGRAMS = frozenset({
    "问题", "风险", "存在", "可能", "导致", "建议", "需要", "进行", "使用", "没有", "缺少",
    "缺失", "不够", "不是", "已经", "如果", "因此", "但是", "或者", "以及", "其中", "可以",
    "应该", "应当", "容易", "影响", "说明", "当前", "相关", "所有", "一个", "这个", "此处",
    "该处", "处理", "检测", "发现", "代码", "函数", "方法", "变量", "文件", "数据", "用户",
    # 安全类文本里到处出现，不区分具体是哪一类问题。
    "漏洞", "安全", "攻击", "击者",
})
_GENERIC_WORDS = frozenset({
    "the", "and", "for", "with", "not", "use", "are", "can", "may", "this", "that", "from",
    "into", "code", "data", "file", "user", "line", "var", "def", "return", "class", "value",
    "null", "none", "true", "false", "self", "args", "kwargs",
})


@dataclass(frozen=True)
class ClaimView:
    """判定一致性所需的最小字段，由调用方从 Issue 或聚合声明字典转换而来。"""

    issue_type: str = ""
    cwe: str = ""
    owasp: str = ""
    title: str = ""
    description: str = ""
    suggestion: str = ""
    exploit_scenario: str = ""


def view_from_mapping(raw: Mapping[str, Any]) -> ClaimView:
    """从聚合声明字典（finding_aggregator 的 claim、ReviewIssue 序列化行）构造 ClaimView。

    同一调用点或同一段引文不是“同一主题”的证据：文档类发现引用 sink 所在行时它们的锚点
    相同，所以锚点、行号、证据都不进入主题判定。
    """
    return ClaimView(
        issue_type=str(raw.get("issue_type") or ""),
        cwe=str(raw.get("cwe") or ""),
        owasp=str(raw.get("owasp") or ""),
        title=str(raw.get("title") or ""),
        description=str(raw.get("description") or ""),
        suggestion=str(raw.get("suggestion") or ""),
        exploit_scenario=str(raw.get("exploit_scenario") or ""),
    )


@dataclass(frozen=True)
class CoherenceVerdict:
    """一致性判定结果。reason 是稳定的机器可读码，供测试与检测脚本使用。"""

    ok: bool
    reason: str
    shared_weight: int = 0
    overlap: float = 0.0


def normalize_cwe(value: object) -> str:
    """统一成 CWE-数字；无法识别的非空文本原样截断，空值返回空串。"""
    text = str(value or "").strip().upper()
    if not text:
        return ""
    match = _CWE_RE.search(text)
    return f"CWE-{match.group(1)}" if match else text[:64]


def is_security_label(view: ClaimView) -> bool:
    return str(view.issue_type or "").strip().lower() in SECURITY_ISSUE_TYPES


def has_security_content(view: ClaimView) -> bool:
    """有明确的安全内容：CWE、OWASP，或标题、描述、建议、攻击场景里出现安全语义。

    只看内容，不看 issue_type 标签。模型会把文档示例这类与安全无关的观察也标成“安全漏洞”，
    标签不能证明内容是安全问题。
    """
    if normalize_cwe(view.cwe) or str(view.owasp or "").strip():
        return True
    text = " ".join(
        str(value or "")
        for value in (view.title, view.description, view.suggestion, view.exploit_scenario)
    )
    return bool(SECURITY_LANGUAGE_RE.search(text))


def _topic_tokens(text: str) -> frozenset[str]:
    # 主题只取标题和描述开头的 4000 字符：再往后基本是复述与示例代码，不影响主题判断。
    lowered = (text or "")[:4000].lower()
    tokens = {word for word in _ASCII_WORD_RE.findall(lowered) if word not in _GENERIC_WORDS}
    for run in _CJK_RUN_RE.findall(lowered):
        for index in range(len(run) - 1):
            gram = run[index:index + 2]
            if gram not in _GENERIC_BIGRAMS:
                tokens.add(gram)
    return frozenset(tokens)


def topic_overlap(left: ClaimView, right: ClaimView) -> tuple[int, float]:
    """两条发现标题与描述的共享特征词。

    返回 (共享权重, 重叠比例)。特征词是英文标识符（权重 2，例如 pickle、sql）和中文
    双字组（权重 1，例如“反序”“序列”）。重叠比例是共享词数除以较短一方的词数。
    """
    left_tokens = _topic_tokens(f"{left.title} {left.description}")
    right_tokens = _topic_tokens(f"{right.title} {right.description}")
    if not left_tokens or not right_tokens:
        return 0, 0.0
    shared = left_tokens & right_tokens
    weight = sum(2 if token.isascii() else 1 for token in shared)
    return weight, len(shared) / min(len(left_tokens), len(right_tokens))


def claims_are_coherent(
    left: ClaimView,
    right: ClaimView,
    *,
    cwe_conflict_splits: bool = True,
) -> CoherenceVerdict:
    """判断两条发现是否在讲同一个问题（只裁决主题，不看行号和证据）。

    判定顺序：
    1. 双方 CWE 相同：已有同一弱点身份，一致（same_cwe）。
    2. 双方 CWE 都有且不同：cwe_conflict_splits 为真时不一致（cwe_conflict），合并器用这个
       口径。聚合器按既有设计，把“同一问题、归类意见不一”的声明留在一起并标记
       classification_disagreement 交人工复核，所以传 False：不直接拆，继续走后面的步骤，
       只有主题重叠才保留在一起；CWE-502 与 CWE-345 这类互不相干的两条仍会被拆开。
    3. 一方有明确安全内容，另一方既没有安全内容、类别也不是安全类：不一致
       （security_vs_non_security）。文档、命名、注释类发现引用了 sink 所在行属于这种情形。
    4. 其余情形（至少一方没有 CWE，或 CWE 不同但调用方不直接拆；含双方都与安全无关的
       非安全类发现）：要求主题重叠，共享特征词权重不低于 TOPIC_MIN_SHARED_WEIGHT 且重叠
       比例不低于 TOPIC_MIN_OVERLAP，否则不一致（unrelated_topic）。非安全类同样要求，
       因为同一行引文下“函数命名过宽”和“docstring 缺参数说明”也不是同一个问题。

    已知取舍：特征词重叠是弱信号。同一问题措辞完全不同、又缺 CWE 时会被多拆成两条；
    两条不同问题恰好共用一个 API 名且措辞接近时可能仍被合并。
    """
    left_cwe, right_cwe = normalize_cwe(left.cwe), normalize_cwe(right.cwe)
    if left_cwe and right_cwe:
        if left_cwe == right_cwe:
            return CoherenceVerdict(True, "same_cwe")
        if cwe_conflict_splits:
            return CoherenceVerdict(False, "cwe_conflict")

    left_content, right_content = has_security_content(left), has_security_content(right)
    left_label, right_label = is_security_label(left), is_security_label(right)
    if left_content != right_content:
        label_of_weaker_side = right_label if left_content else left_label
        if not label_of_weaker_side:
            return CoherenceVerdict(False, "security_vs_non_security")

    weight, overlap = topic_overlap(left, right)
    if weight >= TOPIC_MIN_SHARED_WEIGHT and overlap >= TOPIC_MIN_OVERLAP:
        return CoherenceVerdict(True, "topic_overlap", weight, overlap)
    return CoherenceVerdict(False, "unrelated_topic", weight, overlap)


__all__ = [
    "SECURITY_ISSUE_TYPES",
    "SECURITY_LANGUAGE_RE",
    "TOPIC_MIN_OVERLAP",
    "TOPIC_MIN_SHARED_WEIGHT",
    "ClaimView",
    "CoherenceVerdict",
    "claims_are_coherent",
    "has_security_content",
    "is_security_label",
    "normalize_cwe",
    "topic_overlap",
]
