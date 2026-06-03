"""经验记忆服务 — Agent 自进化 L1 在线经验

- harvest(): 从已决问题(fixed/ignored)中按「语言+类型+归一化标题」聚类,
  幂等地重算并写入 review_experience(每次按窗口重算计数,重复运行不会翻倍)。
- retrieve(): 审查前按当前语言检索高权重经验,供 PromptBuilder 注入。
- decay_weight()/make_fingerprint(): 纯函数,便于单测。

设计要点:
- 权重带时间衰减,过期经验自然淘汰(治理分布漂移)。
- 注入只取 Top-K 且 weight≥阈值,控制 token 预算、不放大噪声。
"""
import hashlib
import re
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy.orm import Session

from app.models.code_file import CodeFile
from app.models.review_experience import ReviewExperience
from app.models.review_issue import ReviewIssue

# 默认参数
DEFAULT_HALFLIFE_DAYS = 30
DEFAULT_LAMBDA = 1.0          # 假阳性对权重的惩罚系数
DEFAULT_MIN_WEIGHT = 0.5      # 低于此权重不注入
DEFAULT_TOP_K = 3

_NORM_RE = re.compile(r"[\d\W_]+", re.UNICODE)
_DECIDED = ("fixed", "ignored")


def _norm_title(title: str) -> str:
    """归一化标题:去数字/标点/空白并小写,使同类问题聚到同一指纹"""
    t = (title or "").strip().lower()
    return _NORM_RE.sub("", t)


def make_fingerprint(language: str, issue_type: str, title: str) -> str:
    """生成经验聚类指纹(语言 + 问题类型 + 归一化标题)"""
    lang = (language or "*").strip().lower() or "*"
    key = f"{lang}|{issue_type or '其他'}|{_norm_title(title)}"
    return hashlib.sha1(key.encode("utf-8")).hexdigest()


def decay_weight(
    accepted: int,
    rejected: int,
    last_seen: Optional[datetime],
    now: Optional[datetime] = None,
    halflife_days: int = DEFAULT_HALFLIFE_DAYS,
    lam: float = DEFAULT_LAMBDA,
) -> float:
    """计算带时间衰减的经验权重

    weight = (accepted - lam*rejected) * 0.5 ** (age_days / halflife)

    Args:
        accepted: 被采纳(fixed)次数
        rejected: 被忽略(ignored)次数
        last_seen: 最近出现时间(UTC, aware);None 视为无衰减
        now: 当前时间(UTC),默认取系统时间(便于测试注入)
        halflife_days: 半衰期天数
        lam: 假阳性惩罚系数

    Returns:
        float: 权重(可能为负,代表净噪声)
    """
    base = accepted - lam * rejected
    if last_seen is None:
        return float(base)
    now = now or datetime.now(timezone.utc)
    # 兼容 naive 时间(SQLite 取出的可能无 tzinfo)
    if last_seen.tzinfo is None:
        last_seen = last_seen.replace(tzinfo=timezone.utc)
    age_days = max(0.0, (now - last_seen).total_seconds() / 86400.0)
    factor = 0.5 ** (age_days / halflife_days) if halflife_days > 0 else 1.0
    return float(base * factor)


def harvest(
    db: Session,
    window_days: int = 90,
    halflife_days: int = DEFAULT_HALFLIFE_DAYS,
    now: Optional[datetime] = None,
) -> dict:
    """从已决问题沉淀经验(幂等)

    每次按时间窗重算各指纹的 fixed/ignored 计数并 upsert,
    因此重复运行不会重复累加。

    Returns:
        dict: {"clusters": 写入/更新的经验条数, "scanned": 扫描的已决问题数}
    """
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=window_days)

    rows = (
        db.query(ReviewIssue, CodeFile.language)
        .outerjoin(CodeFile, CodeFile.id == ReviewIssue.file_id)
        .filter(
            ReviewIssue.status.in_(_DECIDED),
            ReviewIssue.handled_at.isnot(None),
            ReviewIssue.handled_at >= cutoff,
        )
        .all()
    )

    clusters: dict[str, dict] = {}
    for issue, language in rows:
        lang = (language or "*").strip().lower() or "*"
        fp = make_fingerprint(lang, issue.issue_type, issue.title or "")
        c = clusters.setdefault(fp, {
            "language": lang,
            "issue_type": issue.issue_type or "其他",
            "title": issue.title or "",
            "accepted": 0, "rejected": 0,
            "suggestion": "", "code_pattern": "",
            "last_seen": None,
        })
        if issue.status == "fixed":
            c["accepted"] += 1
            # 取被采纳案例的建议/修复作为优质范本
            if not c["suggestion"] and issue.suggestion:
                c["suggestion"] = issue.suggestion
            if not c["code_pattern"] and issue.fixed_code:
                c["code_pattern"] = _desensitize(issue.fixed_code)
        elif issue.status == "ignored":
            c["rejected"] += 1
        if issue.handled_at and (c["last_seen"] is None or issue.handled_at > c["last_seen"]):
            c["last_seen"] = issue.handled_at

    written = 0
    for fp, c in clusters.items():
        weight = decay_weight(c["accepted"], c["rejected"], c["last_seen"],
                              now=now, halflife_days=halflife_days)
        exp = (
            db.query(ReviewExperience)
            .filter(ReviewExperience.fingerprint == fp)
            .first()
        )
        if exp is None:
            exp = ReviewExperience(fingerprint=fp)
            db.add(exp)
        exp.language = c["language"]
        exp.issue_type = c["issue_type"]
        exp.title = (c["title"] or "")[:200]
        exp.canonical_suggestion = c["suggestion"] or None
        exp.code_pattern = c["code_pattern"] or None
        exp.accepted_count = c["accepted"]
        exp.rejected_count = c["rejected"]
        exp.weight = weight
        exp.last_seen = c["last_seen"]
        written += 1

    db.commit()
    return {"clusters": written, "scanned": len(rows)}


def retrieve(
    db: Session,
    language: str = "",
    top_k: int = DEFAULT_TOP_K,
    min_weight: float = DEFAULT_MIN_WEIGHT,
) -> list[ReviewExperience]:
    """检索可注入的高权重经验(本团队已确认的高频真实问题)

    仅返回净采纳(accepted≥1)且权重达标的经验,按权重降序取 Top-K,
    避免把噪声或过期经验注入 Prompt。

    Args:
        db: 数据库会话
        language: 当前审查语言(空=不限语言)
        top_k: 最多返回条数(token 预算)
        min_weight: 权重下限

    Returns:
        list[ReviewExperience]: 命中的经验列表
    """
    lang = (language or "").strip().lower()
    q = db.query(ReviewExperience).filter(
        ReviewExperience.accepted_count >= 1,
        ReviewExperience.weight >= min_weight,
    )
    if lang:
        q = q.filter(
            (ReviewExperience.language == "*") | (ReviewExperience.language == lang),
        )
    return q.order_by(ReviewExperience.weight.desc()).limit(max(1, top_k)).all()


def _desensitize(snippet: str, max_len: int = 400) -> str:
    """对入库代码片段做轻量脱敏:截断 + 去除明显的字符串字面量内容

    不做完整 AST 解析(超出本期范围),仅把双/单引号内容替换为占位,
    降低把密钥/路径等敏感字面量沉淀进经验库的风险。
    """
    if not snippet:
        return ""
    s = re.sub(r"([\"'])(?:\\.|(?!\1).)*\1", r"\1***\1", snippet)
    return s[:max_len]
