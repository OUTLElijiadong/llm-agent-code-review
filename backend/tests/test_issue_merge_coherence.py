"""审查问题合并的语义一致性（线上任务 #236 的 #3956 / #3961 复现）。

线上现象：标题与描述讲文档或命名，类别却是“安全漏洞”，来源明细里混着另一条
安全发现。机理有两处：合并簇接纳了主题不同的成员；合并后的标题、描述、类别
又分别取自簇内不同成员。以下用例不调用 LLM，直接构造发现。
"""
from __future__ import annotations

import json

from app.ai.finding_aggregator import aggregate_agent_findings
from app.ai.result_parser import Issue, _infer_owasp_cwe
from app.ai.static_analyzer import Finding
from app.services.issue_merger import merge_findings_and_issues

SINK = "return pickle.loads(blob)"
CODE = (
    "import pickle\n\n\n"
    "def load_payload(blob):\n"
    '    """加载载荷。"""\n'
    f"    {SINK}\n"
)
DOC_TITLE = "两个类的命名及文档衔接"
SEC_TITLE = "不可信反序列化风险"


def _doc_issue(**overrides) -> Issue:
    values = dict(
        line_number=6, end_line=6, issue_type="命名规范", severity="低",
        title=DOC_TITLE, description="函数文档没有说明与调用方的衔接关系，命名不够清晰",
        suggestion="统一命名并补充文档", evidence=SINK, confidence=0.6,
        source="llm:maintainability",
    )
    values.update(overrides)
    return Issue(**values)


def _security_issue(**overrides) -> Issue:
    values = dict(
        line_number=6, end_line=6, issue_type="安全漏洞", severity="高",
        title=SEC_TITLE, description="pickle.loads 直接反序列化外部传入的数据",
        suggestion="改用 JSON 并校验签名", evidence=SINK, cwe="CWE-502",
        owasp="A08:2025-Software or Data Integrity Failures", confidence=0.9,
        exploit_scenario="攻击者构造恶意 pickle 执行任意代码",
        source="llm:security",
    )
    values.update(overrides)
    return Issue(**values)


def _static_deserialization() -> Finding:
    return Finding(
        line_number=6, end_line=6, issue_type="安全漏洞", severity="严重",
        title="不安全的反序列化 pickle.loads", description="使用 pickle.loads 反序列化不可信数据",
        suggestion="避免对不可信数据使用 pickle", cwe="CWE-502",
        owasp="A08:2025-Software or Data Integrity Failures", evidence=SINK,
        confidence=0.95, source="static",
    )


def _detail_titles(issue: Issue) -> set[str]:
    return {str(item.get("title") or "") for item in issue.source_details}


def _assert_single_topic(issue: Issue, *, security: bool) -> None:
    """合并结果的类别、CWE、标题、描述、来源明细必须指向同一个主题。"""
    titles = _detail_titles(issue)
    if security:
        assert issue.issue_type == "安全漏洞"
        assert issue.cwe == "CWE-502"
        assert "反序列化" in (issue.title or "")
        assert "命名" not in (issue.description or "")
        assert DOC_TITLE not in titles
    else:
        assert issue.issue_type == "命名规范"
        assert not issue.cwe
        assert issue.title == DOC_TITLE
        assert SEC_TITLE not in titles
        assert not any("反序列化" in title for title in titles)


def _by_topic(issues: list[Issue]) -> tuple[Issue, Issue]:
    """把恰好两条合并结果分成 (安全类, 文档命名类)。"""
    assert len(issues) == 2, [(item.issue_type, item.title) for item in issues]
    security = [item for item in issues if item.cwe or item.issue_type == "安全漏洞"]
    other = [item for item in issues if item not in security]
    assert len(security) == 1 and len(other) == 1
    return security[0], other[0]


# ===== 一、普通审查合并器：共享同一段源码引文的异主题发现 =====

def test_documentation_claim_quoting_the_sink_stays_separate_from_deserialization():
    """#3961 复现：命名/文档类发现引用了 sink 行，不得被并入反序列化发现。"""
    merged = merge_findings_and_issues(
        [], [_doc_issue(), _security_issue()], 1, code=CODE, language="python",
    )

    security, documentation = _by_topic(merged)
    _assert_single_topic(security, security=True)
    _assert_single_topic(documentation, security=False)


def test_static_security_finding_does_not_absorb_a_documentation_claim():
    """静态规则命中与 LLM 的注释完整性发现共享引文时，不得混成一条 hybrid。"""
    doc = _doc_issue(issue_type="注释完整性", title="载荷函数文档不完整",
                     description="函数缺少对返回值与异常的说明", suggestion="补全 docstring")
    merged = merge_findings_and_issues(
        [_static_deserialization()], [doc], 1, code=CODE, language="python",
    )

    security, documentation = _by_topic(merged)
    assert security.source == "static"
    assert security.cwe == "CWE-502" and security.issue_type == "安全漏洞"
    assert documentation.issue_type == "注释完整性"
    assert not documentation.cwe
    assert documentation.source == "llm"

def test_unevidenced_documentation_claim_with_overlapping_words_is_not_absorbed():
    """无证据时同一行标题词重叠（>=0.5）也不得把文档类并入带 CWE 的安全发现。"""
    documentation = Issue(
        line_number=6, issue_type="可维护性", severity="低", title="载荷解析函数缺少说明",
        description="载荷解析函数缺少说明文档", suggestion="补充说明", confidence=0.6,
        source="llm:maintainability",
    )
    integrity = Issue(
        line_number=6, issue_type="安全漏洞", severity="高", title="载荷解析函数缺少完整性校验",
        description="载荷解析函数缺少完整性校验可被篡改", suggestion="增加 HMAC 校验",
        cwe="CWE-345", confidence=0.6, source="llm:security",
    )
    merged = merge_findings_and_issues(
        [], [documentation, integrity], 1, code=CODE, language="python",
    )

    security, other = _by_topic(merged)
    assert security.cwe == "CWE-345" and security.title == integrity.title
    assert other.issue_type == "可维护性" and not other.cwe
    assert other.title == documentation.title
    assert _detail_titles(security) == {integrity.title}
    assert _detail_titles(other) == {documentation.title}


def test_roundtable_finalization_keeps_documentation_claim_apart_from_static_hit():
    """圆桌收尾（静态扫描 + 抽取问题）走同一合并器，同样不得混合。"""
    from app.ai.discussion_orchestrator import _normalize_discussion_issues

    issues = _normalize_discussion_issues(
        [_doc_issue(source="llm")], code=CODE, language="python",
        file_name="payload.py", file_id=1,
    )

    security, documentation = _by_topic(issues)
    assert security.cwe == "CWE-502" and security.issue_type == "安全漏洞"
    assert documentation.issue_type == "命名规范" and not documentation.cwe
    assert documentation.title == DOC_TITLE
    assert DOC_TITLE not in _detail_titles(security)


def test_merged_record_takes_narrative_fields_from_one_primary_claim():
    """同一 CWE 的真重复仍合并，但标题、描述、建议、类别、CWE 必须同属一条主发现。"""
    first = _security_issue(
        title="反序列化", source="llm:security", confidence=0.95,
        description="pickle.loads 直接反序列化外部输入，攻击者可构造恶意字节流在服务端执行任意代码",
        suggestion="换用 JSON",
    )
    second = _security_issue(
        title="不可信数据的反序列化风险(pickle)", source="llm:reliability", confidence=0.6,
        description="外部数据被反序列化", suggestion="改用 JSON 并对字节流做签名校验，拒绝未签名输入",
    )
    merged = merge_findings_and_issues([], [first, second], 1, code=CODE, language="python")

    assert len(merged) == 1
    assert len(merged[0].source_details) == 2
    record = (merged[0].title, merged[0].description, merged[0].suggestion)
    assert record in {
        (first.title, first.description, first.suggestion),
        (second.title, second.description, second.suggestion),
    }
    assert (merged[0].issue_type, merged[0].cwe) == ("安全漏洞", "CWE-502")

def test_label_only_security_claims_with_unrelated_topics_are_not_merged():
    """两条只贴了“安全漏洞”标签、正文互不相干的发现，同行同引文也不得合并。"""
    example = Issue(
        line_number=6, end_line=6, issue_type="安全漏洞", severity="低",
        title="文档示例跨代码块引用变量",
        description="文档里的示例引用了前一个代码块定义的变量，单独阅读时会误导读者",
        suggestion="让每个示例自包含", evidence=SINK, confidence=0.5,
        source="llm:maintainability",
    )
    payload = Issue(
        line_number=6, end_line=6, issue_type="安全漏洞", severity="中",
        title="签名载荷可读性", description="载荷签名后仍可被直接阅读，建议评估是否需要加密",
        suggestion="评估载荷加密", evidence=SINK, confidence=0.5, source="llm:security",
    )
    merged = merge_findings_and_issues([], [example, payload], 1, code=CODE, language="python")

    assert len(merged) == 2
    assert {item.title for item in merged} == {example.title, payload.title}
    assert all(len(item.source_details) == 1 for item in merged)


def test_forced_cluster_with_incoherent_members_is_split_by_the_post_merge_check(monkeypatch):
    """即使成簇环节放行了异主题成员，合并后的自检也要把它们拆回独立问题。"""
    from app.services import issue_merger

    monkeypatch.setattr(issue_merger, "_cluster_match_score", lambda cluster, candidate: 5.0)
    merged = merge_findings_and_issues(
        [], [_doc_issue(), _security_issue()], 1, code=CODE, language="python",
    )

    security, documentation = _by_topic(merged)
    _assert_single_topic(security, security=True)
    _assert_single_topic(documentation, security=False)


# ===== 二、解析层：子串关键词给无关发现盖上同一个伪 CWE =====

def test_cwe_inference_ignores_keywords_inside_ordinary_words():
    """“des”藏在 description/design/destination 里，不能把文档类发现推断成弱加密。"""
    text = "文档 description 中的示例与 design 不一致，destination 说明缺失"
    assert _infer_owasp_cwe("文档示例跨代码块引用变量", text) == ("", "")
    assert _infer_owasp_cwe("载荷签名后可读性差", "jwtlike 字段命名与 describe 不一致") == ("", "")


def test_cwe_inference_still_recognises_real_keyword_hits():
    assert _infer_owasp_cwe("使用 MD5 保存口令", "")[1] == "CWE-327"
    assert _infer_owasp_cwe("DES 加密已不安全", "")[1] == "CWE-327"
    assert _infer_owasp_cwe("hashlib.md5(password)", "")[1] == "CWE-327"
    assert _infer_owasp_cwe("存储型xss", "")[1] == "CWE-79"
    assert _infer_owasp_cwe("SSRF", "")[1] == "CWE-918"
    assert _infer_owasp_cwe("JWT 未校验签名", "")[1] == "CWE-522"
    assert _infer_owasp_cwe("SQL 注入风险", "存在 sql injection")[1] == "CWE-89"


def test_parse_does_not_stamp_a_crypto_cwe_on_a_documentation_claim():
    from app.ai.result_parser import parse

    payload = {"issues": [{
        "issue_type": "安全漏洞", "severity": "低", "title": "文档示例跨代码块引用变量",
        "description": "文档 description 中的示例引用了前一代码块的变量", "line_number": 6,
    }]}
    parsed = parse(json.dumps(payload, ensure_ascii=False))

    assert parsed.issues[0].cwe == ""
    assert parsed.issues[0].owasp == ""


# ===== 三、协同审查聚合器：第一层合并同样只看证据重叠 =====

def _claim(title: str, issue_type: str, cwe: str, description: str, severity: str = "高") -> dict:
    return {
        "title": title, "issue_type": issue_type, "severity": severity, "line_start": 6,
        "description": description, "evidence": SINK, "cwe": cwe, "confidence": 0.8,
    }


def test_aggregator_keeps_documentation_claim_apart_from_deserialization_claim():
    """协同路径 #3961 形态：两个 Agent 引用同一行 sink，主题却不同。"""
    result = aggregate_agent_findings(
        {
            "maintainability": [_claim(DOC_TITLE, "命名规范", "", "命名不清晰，文档缺少衔接说明", "低")],
            "security": [_claim(SEC_TITLE, "安全漏洞", "CWE-502", "pickle.loads 反序列化外部输入")],
        },
        {"maintainability": "可维护性代理", "security": "安全审查代理"},
        code=CODE, file_name="payload.py", chunk_id="chunk-0",
    )

    assert len(result.issues) == 2
    for issue in result.issues:
        titles = {detail["title"] for detail in issue["source_details"]}
        assert len(titles) == 1
        assert titles == {issue["title"]}
    by_title = {issue["title"]: issue for issue in result.issues}
    assert by_title[DOC_TITLE]["issue_type"] == "命名规范" and not by_title[DOC_TITLE]["cwe"]
    assert by_title[SEC_TITLE]["issue_type"] == "安全漏洞" and by_title[SEC_TITLE]["cwe"] == "CWE-502"
    # 输入声明全部保留，不因拆分丢失。
    assert result.coverage["input_claim_count"] == result.coverage["output_claim_count"] == 2


def test_aggregator_still_merges_the_same_problem_reported_by_two_agents():
    result = aggregate_agent_findings(
        {
            "security": [_claim("反序列化", "安全漏洞", "CWE-502", "pickle.loads 反序列化外部输入")],
            "reliability": [_claim("不可信数据反序列化", "安全漏洞", "CWE-502", "外部数据被反序列化", "中")],
        },
        {},
        code=CODE, file_name="payload.py", chunk_id="chunk-0",
    )

    assert len(result.issues) == 1
    assert result.issues[0]["confirmation_count"] == 2
    assert result.issues[0]["cwe"] == "CWE-502"


# ===== 四、防过度拆分：确实同一问题的多来源重复发现仍然合并 =====

def test_static_and_llm_reports_of_the_same_deserialization_still_merge_to_hybrid():
    merged = merge_findings_and_issues(
        [_static_deserialization()], [_security_issue()], 1, code=CODE, language="python",
    )

    assert len(merged) == 1
    assert merged[0].source == "hybrid"
    assert merged[0].confirmation_count == 2
    assert merged[0].static_rule_hits == 1
    assert (merged[0].issue_type, merged[0].cwe) == ("安全漏洞", "CWE-502")
    assert len(merged[0].source_details) == 2


def test_llm_security_claim_without_cwe_but_with_security_language_merges_with_static_hit():
    """模型没填 CWE，但标题明确讲“反序列化”，与静态命中是同一问题。"""
    llm = _security_issue(cwe="", owasp="", title="pickle 反序列化外部数据",
                          description="直接反序列化不可信输入，可导致远程代码执行")
    merged = merge_findings_and_issues(
        [_static_deserialization()], [llm], 1, code=CODE, language="python",
    )

    assert len(merged) == 1
    assert merged[0].source == "hybrid"
    assert merged[0].cwe == "CWE-502"


def test_mislabelled_issue_type_with_matching_security_language_still_merges():
    """类别标签贴成“潜在Bug”但正文就是 SQL 注入，仍与静态 SQL 注入命中合并。"""
    static = Finding(
        line_number=10, end_line=10, issue_type="安全漏洞", severity="高", title="SQL 注入",
        description="第 10 行检测到 SQL 注入", suggestion="使用参数化查询", cwe="CWE-89",
        owasp="A05:2025-Injection", evidence="cursor.execute(sql + uid)", confidence=0.95,
        source="static",
    )
    llm = Issue(
        line_number=10, issue_type="潜在Bug", severity="中", title="用户输入拼接进 SQL 导致注入",
        description="uid 直接拼接进查询语句", suggestion="改用参数化查询",
        evidence="cursor.execute(sql + uid)", confidence=0.7, source="llm:reliability",
    )
    merged = merge_findings_and_issues([static], [llm], 1)

    assert len(merged) == 1
    assert merged[0].source == "hybrid"
    assert merged[0].cwe == "CWE-89"


def test_two_non_security_agents_reporting_the_same_naming_problem_still_merge():
    first = _doc_issue(source="llm:general", confidence=0.7)
    second = _doc_issue(source="llm:maintainability", description="命名不够清晰，文档衔接缺失",
                        suggestion="补充文档", confidence=0.6)
    merged = merge_findings_and_issues([], [first, second], 1, code=CODE, language="python")

    assert len(merged) == 1
    assert merged[0].issue_type == "命名规范"
    assert merged[0].title == DOC_TITLE
    assert merged[0].confirmation_count == 2
    assert not merged[0].cwe


# ===== 五、端到端：经 _execute_review 落库后，问题记录各字段仍然同源 =====

def test_persisted_issues_from_a_full_review_never_mix_topics(tmp_path, monkeypatch):
    from datetime import datetime, timezone
    from unittest.mock import Mock
    from types import SimpleNamespace

    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from app.core.database import Base
    from app.models.code_file import CodeFile
    from app.models.code_version import CodeVersion
    from app.models.project import Project
    from app.models.review_issue import ReviewIssue
    from app.models.review_task import ReviewTask
    from app.models.user import User
    from app.ai.multi_agent import get_agent_profiles
    from app.services import review_service

    engine = create_engine(f"sqlite:///{tmp_path / 'coherence.sqlite'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    monkeypatch.setattr(review_service, "SessionLocal", sessions)
    monkeypatch.setattr(review_service, "_emit_review_event", Mock())
    monkeypatch.setattr(review_service, "static_scan_file", lambda code_file: [])
    monkeypatch.setattr(review_service, "_log_sequential_call", Mock())
    monkeypatch.setattr(review_service, "DeepSeekAgent", lambda **kwargs: SimpleNamespace(model="isolated"))
    profiles = get_agent_profiles("full")
    outputs = {
        "security": [{
            "issue_type": "安全漏洞", "severity": "高", "title": SEC_TITLE, "line_number": 6,
            "description": "pickle.loads 直接反序列化外部传入的数据", "suggestion": "改用 JSON",
            "evidence": SINK, "cwe": "CWE-502", "confidence": 0.9,
        }],
        "maintainability": [{
            "issue_type": "命名规范", "severity": "低", "title": DOC_TITLE, "line_number": 6,
            "description": "函数文档没有说明与调用方的衔接关系，命名不够清晰",
            "suggestion": "统一命名并补充文档", "evidence": SINK, "confidence": 0.6,
        }],
    }

    def analyze(profile, *args, **kwargs):
        return json.dumps({"issues": outputs.get(profile.code, [])}, ensure_ascii=False), {}

    monkeypatch.setattr(review_service, "_call_single_agent", analyze)
    with sessions() as database:
        user = User(username="coherence", password="x", role="admin", status=1)
        database.add(user)
        database.flush()
        project = Project(user_id=user.id, project_name="coherence", status="active", language="python")
        database.add(project)
        database.flush()
        code_file = CodeFile(
            project_id=project.id, file_name="payload.py", file_path="payload.py", content=CODE,
            language="python", status="active", size_bytes=len(CODE), line_count=CODE.count("\n"),
            version_no=1, is_binary=0,
        )
        database.add(code_file)
        database.flush()
        database.add(CodeVersion(
            file_id=code_file.id, version_no=1, content=CODE, create_time=datetime.now(timezone.utc),
        ))
        task = ReviewTask(
            user_id=user.id, project_id=project.id, status="running", review_type="full",
            total_files=1, execution_token="lease", start_time=datetime.now(timezone.utc),
        )
        database.add(task)
        database.commit()

        review_service._execute_review(
            database, Mock(), None, task, user, [code_file], [], profiles, "",
            execution_token="lease",
        )

    with sessions() as observer:
        rows = observer.query(ReviewIssue).filter_by(task_id=task.id).all()
        assert len(rows) == 2, [(row.issue_type, row.title) for row in rows]
        for row in rows:
            detail_titles = {detail["title"] for detail in (row.source_details or [])}
            assert detail_titles == {row.title}
            if row.title == DOC_TITLE:
                assert row.issue_type == "命名规范" and not row.cwe
            else:
                assert row.title == SEC_TITLE
                assert row.issue_type == "安全漏洞" and row.cwe == "CWE-502"
    engine.dispose()

