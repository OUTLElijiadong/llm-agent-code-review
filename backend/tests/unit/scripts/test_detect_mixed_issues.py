"""存量混合问题只读检测脚本：用临时 SQLite 库验证信号、只读保证与命令行入口。"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from app.core.database import Base
from app.models.review_issue import ReviewIssue

SCRIPT_PATH = Path(__file__).resolve().parents[3] / "scripts" / "detect_mixed_issues.py"
SPEC = importlib.util.spec_from_file_location("detect_mixed_issues_under_test", SCRIPT_PATH)
assert SPEC is not None and SPEC.loader is not None
detector = importlib.util.module_from_spec(SPEC)
# 脚本里用了 dataclass，动态加载时需先登记模块，dataclass 才能解析类型注解。
sys.modules[SPEC.name] = detector
SPEC.loader.exec_module(detector)

DOC_TITLE = "两个类的命名及文档衔接"
SEC_TITLE = "不可信反序列化风险"


def _row(**overrides) -> dict:
    values = {
        "issue_type": "命名规范", "title": DOC_TITLE,
        "description": "函数文档没有说明与调用方的衔接关系，命名不够清晰",
        "suggestion": "统一命名并补充文档", "exploit_scenario": "", "cwe": "", "owasp": "",
        "source_details": None, "aggregation_json": None,
    }
    values.update(overrides)
    return values


def _codes(row: dict) -> set[str]:
    return {signal.code for signal in detector.detect_row(row)}


# ===== 行级检测 =====

def test_clean_records_produce_no_signal():
    assert _codes(_row()) == set()
    security = _row(
        issue_type="安全漏洞", title=SEC_TITLE, cwe="CWE-502",
        description="pickle.loads 直接反序列化外部传入的数据",
        source_details=[{"source": "llm:security", "title": SEC_TITLE},
                        {"source": "static", "title": "不安全的反序列化 pickle.loads"}],
    )
    assert _codes(security) == set()


def test_security_label_without_any_security_content_is_flagged():
    """#3956 / #3961 形态：标题与描述讲文档，类别却是安全漏洞，且没有 CWE。"""
    row = _row(issue_type="安全漏洞", title="文档示例跨代码块引用变量",
               description="文档里的示例引用了前一个代码块定义的变量")
    assert _codes(row) == {detector.LABEL_WITHOUT_SECURITY_CONTENT}


def test_security_label_with_cwe_or_security_language_is_not_flagged():
    assert _codes(_row(issue_type="安全漏洞", cwe="CWE-502")) == set()
    assert _codes(_row(issue_type="安全漏洞", title="SQL 注入", description="拼接用户输入")) == set()


def test_unrelated_source_title_is_flagged_but_matching_source_is_not():
    row = _row(
        issue_type="安全漏洞", title=SEC_TITLE, cwe="CWE-502",
        description="pickle.loads 直接反序列化外部传入的数据",
        source_details=[{"source": "llm:a", "title": SEC_TITLE},
                        {"source": "llm:b", "title": DOC_TITLE}],
    )
    signals = detector.detect_row(row)
    assert [signal.code for signal in signals] == [detector.SOURCE_TITLE_MISMATCH]
    assert DOC_TITLE in signals[0].message


def test_incoherent_claims_in_aggregation_snapshot_are_flagged():
    claims = [
        {"title": DOC_TITLE, "issue_type": "命名规范", "cwe": "", "description": "命名不清晰，文档缺少衔接说明"},
        {"title": SEC_TITLE, "issue_type": "安全漏洞", "cwe": "CWE-502",
         "description": "pickle.loads 反序列化外部输入"},
    ]
    row = _row(issue_type="安全漏洞", title=SEC_TITLE, cwe="CWE-502",
               description="pickle.loads 反序列化外部输入", aggregation_json={"claims": claims})
    signals = detector.detect_row(row)
    assert [signal.code for signal in signals] == [detector.CLAIMS_INCOHERENT]
    assert DOC_TITLE in signals[0].message and SEC_TITLE in signals[0].message


def test_claim_pairs_with_cwe_disagreement_but_related_topic_are_not_flagged():
    """聚合器把“归类意见不一”的声明留给人工复核，检测脚本不把它当混合。"""
    claims = [
        {"title": "反序列化外部数据", "issue_type": "安全漏洞", "cwe": "CWE-502",
         "description": "pickle.loads 反序列化外部输入"},
        {"title": "不可信数据的反序列化", "issue_type": "安全漏洞", "cwe": "CWE-94",
         "description": "外部数据被 pickle 反序列化"},
    ]
    row = _row(issue_type="安全漏洞", title="反序列化外部数据", cwe="CWE-502",
               description="pickle.loads 反序列化外部输入", aggregation_json=json.dumps({"claims": claims}))
    assert _codes(row) == set()


def test_malformed_json_columns_are_ignored():
    row = _row(source_details="not json", aggregation_json="{broken")
    assert _codes(row) == set()
    assert _codes(_row(source_details=[1, "x", {"title": None}], aggregation_json={"claims": "x"})) == set()


def test_reported_pairs_are_capped_per_code():
    # 六条词汇完全不重叠的声明共有 15 个不一致配对，只报告前几个。
    topics = [
        ("zebra", "pasture"), ("walnut", "orchard"), ("quartz", "granite"),
        ("ember", "kindling"), ("falcon", "plumage"), ("onyx", "obsidian"),
    ]
    claims = [{"title": title, "issue_type": "命名规范", "description": detail} for title, detail in topics]
    signals = detector.detect_row(_row(aggregation_json={"claims": claims}))
    assert len(signals) == detector.MAX_REPORTED_PER_CODE
    assert {signal.code for signal in signals} == {detector.CLAIMS_INCOHERENT}


# ===== 库级扫描：临时 SQLite =====

@pytest.fixture
def database_url(tmp_path):
    url = f"sqlite:///{tmp_path / 'mixed.sqlite'}"
    engine = create_engine(url)
    Base.metadata.create_all(engine)
    with sessionmaker(bind=engine)() as session:
        session.add_all([
            ReviewIssue(task_id=236, file_name="a.py", line_number=6, issue_type="安全漏洞", severity="低",
                        title="文档示例跨代码块引用变量", description="文档里的示例引用了前一个代码块定义的变量"),
            ReviewIssue(task_id=236, file_name="a.py", line_number=9, issue_type="命名规范", severity="低",
                        title=DOC_TITLE, description="命名不够清晰"),
            ReviewIssue(task_id=236, file_name="b.py", line_number=3, issue_type="安全漏洞", severity="高",
                        title=SEC_TITLE, description="pickle.loads 反序列化外部输入", cwe="CWE-502",
                        source_details=[{"source": "llm:a", "title": SEC_TITLE},
                                        {"source": "llm:b", "title": DOC_TITLE}]),
            ReviewIssue(task_id=300, file_name="c.py", line_number=1, issue_type="安全漏洞", severity="高",
                        title="SQL 注入", description="拼接用户输入", cwe="CWE-89"),
        ])
        session.commit()
    engine.dispose()
    return url


def _scan(url: str, **kwargs):
    engine = detector.build_read_only_engine(url)
    try:
        with Session(engine) as session:
            return detector.scan_issues(session, **kwargs)
    finally:
        engine.dispose()


def test_scan_reports_exactly_the_suspected_rows(database_url):
    result = _scan(database_url)

    assert result.scanned == 4
    assert [(item.task_id, item.file_name, item.line_number) for item in result.suspects] == [
        (236, "a.py", 6), (236, "b.py", 3),
    ]
    assert result.by_code == {
        detector.LABEL_WITHOUT_SECURITY_CONTENT: 1, detector.SOURCE_TITLE_MISMATCH: 1,
    }


def test_scan_filters_by_task_since_id_and_limit(database_url):
    assert _scan(database_url, task_ids=[300]).scanned == 1
    assert _scan(database_url, task_ids=[300]).suspects == []
    assert _scan(database_url, since_id=3).scanned == 2
    assert _scan(database_url, limit=2).scanned == 2
    assert _scan(database_url, batch_size=1).scanned == 4


def test_scan_never_writes_and_rejects_write_statements(database_url):
    before = Path(database_url.removeprefix("sqlite:///")).read_bytes()
    _scan(database_url)
    assert Path(database_url.removeprefix("sqlite:///")).read_bytes() == before

    engine = detector.build_read_only_engine(database_url)
    try:
        with engine.connect() as connection:
            with pytest.raises(RuntimeError, match="只读脚本拒绝执行 DELETE"):
                connection.execute(text("DELETE FROM review_issue"))
            with pytest.raises(RuntimeError, match="只读脚本拒绝执行 UPDATE"):
                connection.execute(text("UPDATE review_issue SET title = 'x'"))
    finally:
        engine.dispose()
    assert Path(database_url.removeprefix("sqlite:///")).read_bytes() == before


def test_sqlite_connection_is_in_query_only_mode(database_url):
    engine = detector.build_read_only_engine(database_url)
    try:
        with engine.connect() as connection:
            assert connection.execute(text("PRAGMA query_only")).scalar() == 1
    finally:
        engine.dispose()


def test_unknown_dialect_is_refused_instead_of_running_without_a_read_only_guard(monkeypatch):
    class _Dialect:
        name = "firebird"

    class _Engine:
        dialect = _Dialect()
        disposed = False

        def dispose(self):
            self.disposed = True

    engine = _Engine()
    monkeypatch.setattr(detector, "create_engine", lambda *args, **kwargs: engine)

    with pytest.raises(RuntimeError, match="无法保证只读"):
        detector.build_read_only_engine("firebird://user:pass@host/db")
    assert engine.disposed


# ===== 命令行入口 =====

def test_cli_text_and_json_output(database_url, capsys):
    assert detector.main(["--database-url", database_url, "--task-id", "236"]) == 0
    text_output = capsys.readouterr().out
    assert "扫描 3 条，疑似混合 2 条" in text_output
    assert "label_without_security_content" in text_output and "source_title_mismatch" in text_output

    assert detector.main(["--database-url", database_url, "--format", "json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["scanned"] == 4 and payload["suspected"] == 2
    assert {item["task_id"] for item in payload["suspects"]} == {236}


def test_cli_output_truncates_titles_and_never_prints_description(tmp_path, capsys):
    url = f"sqlite:///{tmp_path / 'long.sqlite'}"
    engine = create_engine(url)
    Base.metadata.create_all(engine)
    secret = "绝不应出现在输出里的描述正文" * 5
    with sessionmaker(bind=engine)() as session:
        session.add(ReviewIssue(
            task_id=1, file_name="x.py", line_number=1, issue_type="安全漏洞", severity="低",
            title="很长的标题" * 30, description=secret, evidence="敏感证据代码片段", fixed_code="敏感修复代码",
        ))
        session.commit()
    engine.dispose()

    assert detector.main(["--database-url", url, "--format", "json"]) == 0
    output = capsys.readouterr().out
    assert secret not in output and "敏感证据代码片段" not in output and "敏感修复代码" not in output
    payload = json.loads(output)
    assert len(payload["suspects"][0]["title"]) <= detector.RECORD_TITLE_PREVIEW_CHARS
