"""上下文压缩中的确定性约束原文提取。"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence

_DIRECTIVE_MARKER = re.compile(
    r"必须|不得(?!不)|禁止|严禁|不允许|不可以|不能(?:够)?|不可|不准|不要|请勿|请不要|"
    r"不应(?:当)?|仅当前|仅限|仅可|只能|只允许|只许|"
    r"仅(?=.{0,64}(?:可|能|允许|限|才|当前))|只有|最多|至多|至少|不超过|"
    r"不得少于|不少于|不低于|不高于|上限|下限|更正|纠正|修正|改为|改成|调整为|"
    r"更新为|替换为|改回|以.{0,20}为准|必须覆盖|需要覆盖|全覆盖|覆盖所有|覆盖范围",
)
_ENGLISH_DIRECTIVE_MARKER = re.compile(
    r"\b(?:must(?:\s+not)?|shall(?:\s+not)?|should\s+not|never|only|forbidden|"
    r"disallow(?:ed)?|required|do\s+not|cannot|can['’]t|change\s+to|changed\s+to|"
    r"updated\s+to|set\s+to|at\s+most|at\s+least|no\s+more\s+than|"
    r"no\s+less\s+than|up\s+to|maximum|minimum|exactly)\b",
    re.IGNORECASE,
)
_GOAL_MARKER = re.compile(r"确保|\bensure\b", re.IGNORECASE)
_ROLE_PERMISSION_MARKER = re.compile(
    r"(?:超级管理员|管理员|审查员|审计员|普通用户|普通账号|用户角色|角色)"
    r"[^。！？!?；;\r\n]{0,24}"
    r"(?:才能|才可|才可以|有权|无权|不该|不应|不得|不能|不允许|不可|仅可|仅能|可以|可)"
    r"[^。！？!?；;\r\n]{0,24}"
    r"(?:审批|批准|发布|创建|修改|删除|管理|操作)"
    r"|(?:审批|批准|发布|创建|修改|删除|管理|操作)"
    r"[^。！？!?；;\r\n]{0,32}"
    r"(?:仅限|只有|仅由|由)"
    r"[^。！？!?；;\r\n]{0,12}"
    r"(?:超级管理员|管理员|审查员|审计员|普通用户|普通账号|用户角色|角色)"
)
_ACCOUNT_SCOPE_MARKER = re.compile(
    r"(?:当前|本|不同|各|多个|跨)?账号"
    r"[^。！？!?；;\r\n]{0,40}"
    r"(?:只(?:能)?(?:看|查看|读取|访问)|仅(?:能)?(?:看|查看|读取|访问)|"
    r"(?:互相|相互)?隔离|不互通|彼此独立|各自独立|"
    r"(?:只|仅)(?:能)?(?:查看|读取|访问)?自己的[^。！？!?；;\r\n]{0,12}(?:历史|聊天记录|会话记录))"
)
_TIME_DURATION_MARKER = re.compile(r"\d+(?:\.\d+)?\s*(?:秒|分钟|小时|天)")
_CONVERSATION_MARKER = re.compile(r"追问|禁言|对话|会话")
_RISK_DECISION_MARKER = re.compile(
    r"(?:低风险|中风险|高风险|低危|中危|高危)"
    r"[^。！？!?；;\r\n]{0,24}"
    r"(?:自动通过|自动批准|自动审批|自动执行|自动拒绝|自动驳回|"
    r"需要询问|需询问|必须询问|需要确认|需确认|必须确认|人工审批)"
)
_ADMIN_APPROVAL_PERMISSION_MARKER = re.compile(
    r"(?:只|仅)(?:给|授予|允许|能|可)?(?:超级管理员|管理员|审查员|审计员)"
    r"[^。！？!?；;\r\n]{0,12}(?:审批|批准|发布|创建|管理)"
    r"[^。！？!?；;\r\n]{0,8}(?:权限|权|资格)"
    r"|(?:审批|批准|发布|创建)[^。！？!?；;\r\n]{0,12}(?:权限|权|资格)"
    r"[^。！？!?；;\r\n]{0,16}(?:仅限|只给|只授予|仅由)"
    r"[^。！？!?；;\r\n]{0,12}(?:超级管理员|管理员|审查员|审计员)"
)
_CONFIRMATION_GATE_MARKER = re.compile(
    r"(?:用户|超级管理员|管理员|审查员|审计员|负责人|审批人|申请人)"
    r"[^。！？!?；;\r\n]{0,16}(?:确认|审批|批准)"
    r"[^。！？!?；;\r\n]{0,12}(?:后|以后|之后|通过后)"
    r"[^。！？!?；;\r\n]{0,12}(?:才|再|方可|才能)?"
    r"[^。！？!?；;\r\n]{0,12}(?:发布|创建|启用|执行|生效|删除|修改)"
)
_COUNT_WITH_UNIT = re.compile(
    r"(?:并发(?:人数)?|团队(?:人数|成员数|规模)|(?:人数|数量|次数|线程数|并发数))"
    r"[^。！？!?;；\r\n]{0,24}\d+(?:\.\d+)?\s*(?:人|个|位|名|次|条|项|个文件|个 Agent)",
    re.IGNORECASE,
)
_NUMERIC_CONSTRAINT = re.compile(
    r"(?:达到|不少于|不低于|不超过|至多|最多|至少|上限|下限|设为|设置为|调整为|改为|"
    r"限定为|限制为|改到|提高到|降低到)\s*[<>≤≥]?\s*\d+(?:\.\d+)?\s*"
    r"(?:%|％|百分比|项|个|人|次|秒|分钟|小时|天|页|文件|token|tokens|字节|KB|MB|GB)?"
    r"|(?:人数|数量|次数|并发|预算|额度|时长|期限|上限|下限|团队规模|team\s+size|"
    r"concurrency)[^。！？!?;；\r\n]{0,64}(?:设为|设置为|调整为|改为|限制为|限定为|"
    r"is|to|at|=|:|：)\s*\d+(?:\.\d+)?",
    re.IGNORECASE,
)
_SENTENCE_BOUNDARY = re.compile(r"[。！？!?；;\r\n]+")
_MAX_PROTECTED_FACT_CHARS = 2_000
_MAX_LEDGER_STATEMENT_CHARS = 1_200
_MAX_UNMARKED_MESSAGE_LEDGER_CHARS = 128


def extract_user_fact_ledger(text: str) -> list[str]:
    """保留短用户陈述原文，覆盖不带约束关键词的普通业务事实。

    较长消息按句界拆开，只收纳能完整放入账本的语句；超长无句界内容仍由
    语义压缩器处理，并由 extract_protected_facts 另行保留可识别的约束。
    账本超出上下文预算时由调用方拒绝发送，不截断或静默丢弃。
    """
    content = text.strip()
    if not content:
        return []
    if len(content) <= _MAX_UNMARKED_MESSAGE_LEDGER_CHARS:
        return [content]
    if not _SENTENCE_BOUNDARY.search(content):
        return []
    statements = re.split(r"(?<=[。！？!?；;\r\n])", content)
    return [
        statement.strip()
        for statement in statements
        if statement.strip() and len(statement.strip()) <= _MAX_LEDGER_STATEMENT_CHARS
    ]


_QUERY_STOPWORDS = frozenset({
    "帮我", "请问", "一下", "这个", "那个", "如何", "怎么", "什么", "哪个", "是否",
    "可以", "继续", "说明", "内容", "情况", "事情", "问题", "处理", "进行", "相关",
    "背景", "历史", "审查", "项目", "保留", "所有", "证据", "填充", "讨论",
})


def _context_query_terms(text: str) -> set[str]:
    terms = {
        term.casefold()
        for term in re.findall(r"[A-Za-z0-9_]{2,}", text)
        if term.casefold() not in _QUERY_STOPWORDS
        and not re.fullmatch(r"(.)\1{2,}", term.casefold())
    }
    for sequence in re.findall(r"[\u3400-\u9fff]{2,}", text):
        terms.update(sequence[index:index + 2] for index in range(len(sequence) - 1))
    return terms - _QUERY_STOPWORDS


def retrieve_relevant_user_facts(
    transcript: Sequence[Mapping[str, object]],
    source_indices: Sequence[int],
    query: str,
    *,
    max_sources: int = 4,
) -> list[tuple[int, str]]:
    """按当前用户问题从被压缩原文中检索匹配句段，保留来源索引。"""
    query_terms = _context_query_terms(query)
    if not query_terms or max_sources <= 0:
        return []
    query_patterns = [re.compile(re.escape(term), re.IGNORECASE) for term in query_terms]

    candidates: list[tuple[int, int, str]] = []
    for index in source_indices:
        if index < 0 or index >= len(transcript):
            continue
        item = transcript[index]
        if str(item.get("role") or "").casefold() != "user":
            continue
        raw_content = item.get("content")
        if isinstance(raw_content, str):
            content = raw_content
        elif isinstance(raw_content, list):
            content = "\n".join(
                str(part.get("text") or part.get("input_text") or "")
                for part in raw_content if isinstance(part, Mapping)
            )
        else:
            continue
        best: tuple[int, int, int, str] | None = None
        for statement in re.split(r"(?<=[。！？!?；;\r\n])", content):
            statement = statement.strip()
            if not statement:
                continue
            candidate = _matching_fact_excerpt(statement, query_patterns)
            if candidate is not None and (
                best is None
                or (-candidate[0], candidate[1], -candidate[2]) < (-best[0], best[1], -best[2])
            ):
                best = candidate
        if best:
            candidates.append((best[0], index, best[3]))

    selected = sorted(candidates, key=lambda item: (item[0], item[1]), reverse=True)[:max_sources]
    return [(index, text) for _, index, text in sorted(selected, key=lambda item: item[1])]


def _matching_fact_excerpt(
    statement: str, query_patterns: Sequence[re.Pattern[str]],
) -> tuple[int, int, int, str] | None:
    """只对连续原文窗口打分，避免分散词项把摘录中心移到无关内容。"""
    context_chars = 160
    # 匹配窗口加两侧上下文和两个省略标记，仍不超过原摘录上限。
    window_chars = _MAX_LEDGER_STATEMENT_CHARS - context_chars * 2 - 2
    is_long = len(statement) > _MAX_LEDGER_STATEMENT_CHARS
    starts = range(0, len(statement), window_chars // 2) if is_long else (0,)
    best: tuple[int, int, int, str] | None = None
    for offset in starts:
        window = statement[offset:offset + window_chars] if is_long else statement
        # 查询的英文词项已经 casefold；直接在原文上匹配以保持字符坐标。
        # ß/İ 等字符经 casefold 会改变长度，不能用转换后的坐标切原文。
        matches = [match for pattern in query_patterns if (match := pattern.search(window)) is not None]
        score = len(matches)
        # 一次偶然词面碰撞（例如“历史 46”）不足以判定相关。
        if score < 3:
            continue
        first = offset + min(match.start() for match in matches)
        last = offset + max(match.end() for match in matches)
        if is_long:
            start, end = max(0, first - context_chars), min(len(statement), last + context_chars)
            excerpt = ("…" if start else "") + statement[start:end] + ("…" if end < len(statement) else "")
        else:
            excerpt = statement
        candidate = (score, last - first, first, excerpt)
        # 相同覆盖优先更密集的原文，再保留较后的陈述；不合并或改写事实。
        if best is None or (-score, last - first, -first) < (-best[0], best[1], -best[2]):
            best = candidate
    return best


def _bounded_fact_excerpt(sentence: str, markers: list[re.Match[str]]) -> str:
    """长句保留头尾及每个约束标记附近原文，显式标出中间省略。"""
    if len(sentence) <= _MAX_PROTECTED_FACT_CHARS:
        return sentence
    length = len(sentence)
    edge = 320
    spans = [(0, edge), (length - edge, length)]
    interior = sorted(
        (match.start(), match.end()) for match in markers
        if edge <= match.start() < length - edge
    )
    for start, end in interior:
        spans.append((max(edge, start - 64), min(length - edge, end + 96)))
    merged: list[tuple[int, int]] = []
    for start, end in sorted(spans):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    excerpts = []
    previous_end = 0
    for start, end in merged:
        if start > previous_end:
            excerpts.append(
                f"【原文过长，摘录/中段省略 {start - previous_end} 字符；"
                "完整原文仍在原始输入】"
            )
        excerpts.append(sentence[start:end])
        previous_end = end
    return "".join(excerpts)


def extract_protected_facts(text: str, *, include_goals: bool = True) -> list[str]:
    """提取含权限、否定、数量约束或后续更正的短原文片段。

    这是一个保守的词面保护层，不是自然语言完整性证明。它不会替代来源
    校验或模型语义摘要；无法放进最终预算时，调用方必须显式失败。
    """
    facts: list[str] = []
    seen: set[str] = set()
    start = 0
    for boundary in [*_SENTENCE_BOUNDARY.finditer(text), None]:
        end = boundary.start() if boundary is not None else len(text)
        sentence = text[start:end]
        sentence_start = start
        start = boundary.end() if boundary is not None else len(text)
        normalized = sentence.strip()
        if not normalized:
            continue
        leading_trim = len(sentence) - len(sentence.lstrip())
        absolute_start = sentence_start + leading_trim
        matches = [*_DIRECTIVE_MARKER.finditer(normalized), *_ENGLISH_DIRECTIVE_MARKER.finditer(normalized)]
        matches.extend(_ROLE_PERMISSION_MARKER.finditer(normalized))
        matches.extend(_ACCOUNT_SCOPE_MARKER.finditer(normalized))
        matches.extend(_RISK_DECISION_MARKER.finditer(normalized))
        matches.extend(_ADMIN_APPROVAL_PERMISSION_MARKER.finditer(normalized))
        matches.extend(_CONFIRMATION_GATE_MARKER.finditer(normalized))
        if include_goals:
            matches.extend(_GOAL_MARKER.finditer(normalized))
        numeric_match = _NUMERIC_CONSTRAINT.search(normalized) or _COUNT_WITH_UNIT.search(normalized)
        time_match = _TIME_DURATION_MARKER.search(normalized)
        conversation_match = _CONVERSATION_MARKER.search(normalized) if time_match else None
        has_numeric_constraint = numeric_match is not None
        has_conversation_time_window = conversation_match is not None
        if not matches and not has_numeric_constraint and not has_conversation_time_window:
            continue
        # 短句保留完整原文；极长无句界文本显式摘录，完整输入仍走原有历史链路。
        # “not only”是英语固定搭配，单独的 only 不应误作范围限制。
        has_directive = False
        for match in matches:
            if match.group(0).casefold() == "only":
                prefix = text[:absolute_start + match.start()]
                if re.search(r"\bnot(?:\s|[,;:])+$", prefix, re.IGNORECASE):
                    continue
            has_directive = True
            break
        if has_directive or has_numeric_constraint or has_conversation_time_window:
            if normalized not in seen:
                anchors = [*matches]
                anchors.extend(match for match in (numeric_match, time_match, conversation_match) if match)
                facts.append(_bounded_fact_excerpt(normalized, anchors))
                seen.add(normalized)
    return facts
