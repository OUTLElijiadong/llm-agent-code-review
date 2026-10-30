"""只读检测存量审查问题里疑似“标题、描述与类别、来源互相混合”的记录。

背景：线上审查任务 #236 的 #3956、#3961 标题和描述讲文档或命名，类别却显示“安全漏洞”，
来源明细里混着另一条安全发现。合并逻辑已修正（见 app/ai/finding_coherence.py），历史记录
不做迁移；本脚本只用来评估规模，方便决定是否对受影响的任务重新审查。

只读保证：
- 只执行 SELECT；引擎层在每条新连接上打开数据库只读模式（SQLite 为 query_only，
  MySQL 为 READ ONLY 事务），并拦截任何非 SELECT/PRAGMA/SET/SHOW 语句。
- 不提交、不调用模型、不修改代码文件；输出只含记录编号、任务编号、文件名、行号、
  截断后的标题与原因，不输出描述全文、证据或源码。

检测口径与合并器共用 claims_are_coherent，但存量行只保留部分字段，所以结果是“疑似”：
- label_without_security_content：类别是安全类，却没有 CWE/OWASP，标题、描述、建议里也
  没有安全语义。可能是混合，也可能是模型贴错标签。
- claims_incoherent：aggregation_json 里保存了完整声明（协同审查路径），其中两条声明主题
  不一致。精度最高，与现在聚合器的判定一致。
- source_title_mismatch：source_details 里有标题与记录标题、描述主题不一致。来源明细没有
  保存描述和类别，只能拿标题比，并假定它与记录同类别（偏宽松，宁可漏报）。
已知盲区：旧解析层按子串推断出的伪 CWE（description 里的 des 被推成 CWE-327）无法从存量行
精确还原，不单独检测；只有伴随上述信号时才会被带出来。

用法（在能连生产库的环境里运行；本脚本不会写库，但仍建议先在备份副本上试跑）：
    python scripts/detect_mixed_issues.py --task-id 236
    python scripts/detect_mixed_issues.py --since-id 3900 --limit 5000 --format json
    python scripts/detect_mixed_issues.py --database-url sqlite:////tmp/copy.db
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import create_engine, event, select  # noqa: E402
from sqlalchemy.engine import Engine  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app.ai.finding_coherence import (  # noqa: E402
    ClaimView,
    claims_are_coherent,
    has_security_content,
    is_security_label,
    view_from_mapping,
)
from app.core.config import settings  # noqa: E402
from app.models.review_issue import ReviewIssue  # noqa: E402

LABEL_WITHOUT_SECURITY_CONTENT = "label_without_security_content"
CLAIMS_INCOHERENT = "claims_incoherent"
SOURCE_TITLE_MISMATCH = "source_title_mismatch"

# 一条记录最多报告多少个不一致的配对 / 来源标题；一条记录混了很多条时只列前几条，避免输出过长。
MAX_REPORTED_PER_CODE = 3
# 聚合快照里最多两两比较多少条声明，限制 O(n²) 的开销。
MAX_CLAIMS_COMPARED = 20
TITLE_PREVIEW_CHARS = 40
RECORD_TITLE_PREVIEW_CHARS = 60
_ALLOWED_STATEMENTS = frozenset({"SELECT", "PRAGMA", "SET", "SHOW"})


@dataclass(frozen=True)
class Signal:
    code: str
    message: str


@dataclass(frozen=True)
class Suspect:
    issue_id: int
    task_id: int | None
    file_name: str | None
    line_number: int | None
    issue_type: str
    title: str
    signals: tuple[Signal, ...]


@dataclass
class ScanResult:
    scanned: int = 0
    suspects: list[Suspect] = field(default_factory=list)

    @property
    def by_code(self) -> dict[str, int]:
        counter: Counter[str] = Counter(
            signal.code for suspect in self.suspects for signal in suspect.signals
        )
        return dict(sorted(counter.items()))


def _preview(text: object, limit: int) -> str:
    value = " ".join(str(text or "").split())
    return value if len(value) <= limit else value[: limit - 1] + "…"


def _as_json(value: object) -> object:
    if isinstance(value, (str, bytes)):
        try:
            return json.loads(value)
        except (TypeError, ValueError):
            return None
    return value


def _claims_snapshot(aggregation_json: object) -> list[Mapping[str, Any]]:
    data = _as_json(aggregation_json)
    claims = data.get("claims") if isinstance(data, dict) else None
    if not isinstance(claims, list):
        return []
    return [item for item in claims if isinstance(item, dict)][:MAX_CLAIMS_COMPARED]


def _source_titles(source_details: object) -> list[str]:
    data = _as_json(source_details)
    if not isinstance(data, list):
        return []
    titles: list[str] = []
    for item in data:
        title = str(item.get("title") or "").strip() if isinstance(item, dict) else ""
        if title and title not in titles:
            titles.append(title)
    return titles


def detect_row(row: Mapping[str, Any]) -> list[Signal]:
    """检测一条 review_issue 行，返回疑似混合的信号；没有信号表示未发现问题。"""
    record = ClaimView(
        issue_type=str(row.get("issue_type") or ""),
        cwe=str(row.get("cwe") or ""),
        owasp=str(row.get("owasp") or ""),
        title=str(row.get("title") or ""),
        description=str(row.get("description") or ""),
        suggestion=str(row.get("suggestion") or ""),
        exploit_scenario=str(row.get("exploit_scenario") or ""),
    )
    signals: list[Signal] = []

    if is_security_label(record) and not has_security_content(record):
        signals.append(Signal(
            LABEL_WITHOUT_SECURITY_CONTENT,
            "类别为安全类，但没有 CWE/OWASP，标题、描述、建议里也没有安全语义",
        ))

    flagged_titles: set[str] = set()
    claims = _claims_snapshot(row.get("aggregation_json"))
    reported = 0
    for left_index, left in enumerate(claims):
        for right in claims[left_index + 1:]:
            if reported >= MAX_REPORTED_PER_CODE:
                break
            verdict = claims_are_coherent(
                view_from_mapping(left), view_from_mapping(right), cwe_conflict_splits=False,
            )
            if verdict.ok:
                continue
            left_title, right_title = str(left.get("title") or ""), str(right.get("title") or "")
            flagged_titles.update({left_title, right_title})
            signals.append(Signal(
                CLAIMS_INCOHERENT,
                f"聚合快照里的声明「{_preview(left_title, TITLE_PREVIEW_CHARS)}」与"
                f"「{_preview(right_title, TITLE_PREVIEW_CHARS)}」主题不一致（{verdict.reason}）",
            ))
            reported += 1

    reported = 0
    for title in _source_titles(row.get("source_details")):
        if reported >= MAX_REPORTED_PER_CODE:
            break
        if title == record.title.strip() or title in flagged_titles:
            continue
        # 来源明细没有保存描述和类别：假定来源与记录同类别，只拿标题比主题，偏宽松。
        verdict = claims_are_coherent(
            record, ClaimView(issue_type=record.issue_type, title=title),
        )
        if verdict.ok:
            continue
        signals.append(Signal(
            SOURCE_TITLE_MISMATCH,
            f"来源明细标题「{_preview(title, TITLE_PREVIEW_CHARS)}」与记录主题不一致（{verdict.reason}）",
        ))
        reported += 1
    return signals


_COLUMNS = (
    ReviewIssue.id, ReviewIssue.task_id, ReviewIssue.file_name, ReviewIssue.line_number,
    ReviewIssue.issue_type, ReviewIssue.title, ReviewIssue.description, ReviewIssue.suggestion,
    ReviewIssue.exploit_scenario, ReviewIssue.cwe, ReviewIssue.owasp,
    ReviewIssue.source_details, ReviewIssue.aggregation_json,
)


def _iter_rows(
    session: Session,
    *,
    task_ids: Sequence[int] | None,
    since_id: int | None,
    batch_size: int,
) -> Iterator[Mapping[str, Any]]:
    """按主键分页只读遍历；只取检测需要的列，不加载 fixed_code 和证据全文。"""
    last_id = max(0, int(since_id) - 1) if since_id else 0
    while True:
        statement = select(*_COLUMNS).where(ReviewIssue.id > last_id)
        if task_ids:
            statement = statement.where(ReviewIssue.task_id.in_(list(task_ids)))
        rows = session.execute(statement.order_by(ReviewIssue.id).limit(batch_size)).mappings().all()
        if not rows:
            return
        yield from rows
        last_id = int(rows[-1]["id"])


def scan_issues(
    session: Session,
    *,
    task_ids: Sequence[int] | None = None,
    since_id: int | None = None,
    limit: int | None = None,
    batch_size: int = 1000,
) -> ScanResult:
    result = ScanResult()
    for row in _iter_rows(session, task_ids=task_ids, since_id=since_id, batch_size=batch_size):
        if limit is not None and result.scanned >= limit:
            break
        result.scanned += 1
        signals = detect_row(row)
        if signals:
            result.suspects.append(Suspect(
                issue_id=int(row["id"]),
                task_id=row["task_id"],
                file_name=row["file_name"],
                line_number=row["line_number"],
                issue_type=str(row["issue_type"] or ""),
                title=_preview(row["title"], RECORD_TITLE_PREVIEW_CHARS),
                signals=tuple(signals),
            ))
    return result


def build_read_only_engine(url: str) -> Engine:
    """创建只读引擎：每条新连接打开数据库只读模式，并拒绝非查询语句。"""
    kwargs: dict[str, Any] = {"pool_pre_ping": True}
    if not url.startswith("sqlite"):
        kwargs["connect_args"] = {"connect_timeout": 10, "read_timeout": 120}
    engine = create_engine(url, **kwargs)
    dialect = engine.dialect.name
    read_only_sql = {
        "sqlite": "PRAGMA query_only = ON",
        "mysql": "SET SESSION TRANSACTION READ ONLY",
        "mariadb": "SET SESSION TRANSACTION READ ONLY",
        "postgresql": "SET default_transaction_read_only = on",
    }.get(dialect)
    if read_only_sql is None:
        engine.dispose()
        raise RuntimeError(f"不支持的数据库方言 {dialect}：无法保证只读，拒绝运行")

    @event.listens_for(engine, "connect")
    def _enter_read_only(dbapi_connection, _record) -> None:  # noqa: ANN001
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute(read_only_sql)
        finally:
            cursor.close()

    @event.listens_for(engine, "before_cursor_execute")
    def _select_only(_conn, _cursor, statement, _parameters, _context, _executemany) -> None:  # noqa: ANN001
        words = str(statement).split(None, 1)
        head = words[0].upper() if words else ""
        if head not in _ALLOWED_STATEMENTS:
            raise RuntimeError(f"只读脚本拒绝执行 {head or '空'} 语句")

    return engine


def render_text(result: ScanResult) -> str:
    lines: list[str] = []
    for suspect in result.suspects:
        place = f"{suspect.file_name or '-'}:{suspect.line_number or 0}"
        lines.append(
            f"issue={suspect.issue_id} task={suspect.task_id} {place} "
            f"[{suspect.issue_type}] {suspect.title}"
        )
        lines.extend(f"  - {signal.code}: {signal.message}" for signal in suspect.signals)
    codes = "，".join(f"{code}={count}" for code, count in result.by_code.items()) or "无"
    lines.append(f"扫描 {result.scanned} 条，疑似混合 {len(result.suspects)} 条（信号：{codes}）")
    return "\n".join(lines)


def render_json(result: ScanResult) -> str:
    return json.dumps({
        "scanned": result.scanned,
        "suspected": len(result.suspects),
        "by_code": result.by_code,
        "suspects": [
            {
                "issue_id": suspect.issue_id,
                "task_id": suspect.task_id,
                "file_name": suspect.file_name,
                "line_number": suspect.line_number,
                "issue_type": suspect.issue_type,
                "title": suspect.title,
                "signals": [{"code": signal.code, "message": signal.message} for signal in suspect.signals],
            }
            for suspect in result.suspects
        ],
    }, ensure_ascii=False, indent=2)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--task-id", type=int, action="append", help="只检测该审查任务，可重复传入")
    parser.add_argument("--since-id", type=int, help="只检测 review_issue.id 不小于该值的记录")
    parser.add_argument("--limit", type=int, help="最多检测多少条记录")
    parser.add_argument("--batch-size", type=int, default=1000, help="每批读取行数，默认 1000")
    parser.add_argument("--format", choices=("text", "json"), default="text")
    parser.add_argument("--database-url", help="默认读取应用配置的数据库；可指向备份副本")
    args = parser.parse_args(argv)

    engine = build_read_only_engine(args.database_url or settings.db_url)
    try:
        with Session(engine) as session:
            result = scan_issues(
                session, task_ids=args.task_id, since_id=args.since_id,
                limit=args.limit, batch_size=max(1, args.batch_size),
            )
            session.rollback()
    finally:
        engine.dispose()
    print(render_json(result) if args.format == "json" else render_text(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
