"""沙箱多 Agent 审查 + 线上治理行为的回归固化。

固化 2026-08-05 部署的关键行为，防未来回归：
1. 黑白盒测试后必须触发多 Agent 审查编排(白盒/黑盒/对抗复检/报告)。
2. Recon facts 必须经 PRISM_FACTS 日志回收并解析。
3. 隔离归档项目允许部署(仅 JWT 代理预览),40341 硬隔离不得加回。
4. php_doc_root 与 _dominant_archive_language 语言纠正必须保留。
5. test_review playbook 已登记 seed。
"""

from __future__ import annotations

import re
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from app.services import sandbox_service

# 支持两种定位:仓库内(tests/unit/services -> backend/) 与生产容器(/app)
_HERE = Path(__file__).resolve()
_CANDIDATES = [
    _HERE.parents[3],          # backend/ (tests/unit/services 上三级)
    _HERE.parents[2],          # backend/tests
    Path("/app"),              # 生产容器
]
_ROOT = next((p for p in _CANDIDATES if (p / "app" / "services" / "sandbox_service.py").exists()), _CANDIDATES[0])

SERVICE_PATH = _ROOT / "app" / "services" / "sandbox_service.py"
AGENT_PATH = _ROOT / "app" / "agents" / "test_review_reporter_agent.py"
PLAYBOOK_PATH = _ROOT / "app" / "ai" / "agent_knowledge" / "sandbox_test_review_playbook.md"
SEED_PATH = _ROOT / "scripts" / "seed_agent_playbooks.py"


def _successful_report_publication(report: str, published: list[str]) -> dict[str, Any]:
    """测试夹具显式模拟报告已持久化，避免把 None 误当成成功回执。"""
    published.append(report)
    return {"report_persisted": True, "artifact_id": 77}


def _complete_report_candidate(issue_section: str = "无问题。") -> str:
    """符合发布契约的最小七章节报告夹具。"""
    return f"""## 总体结论
总体结论候选。
## 白盒结果
白盒执行记录。
## 黑盒结果
黑盒探测记录。
## 问题清单
{issue_section}
## 攻击面待验证清单
无待验证项目。
## 下一步建议
按范围说明复核。
## 证据附录
参见结构化执行回执。
"""


@pytest.fixture(scope="module")
def service_src() -> str:
    return SERVICE_PATH.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def runner_src(service_src: str) -> str:
    m = re.search(r'_DEPLOY_VERIFY_RUNNER = r"""(.*?)"""', service_src, re.S)
    assert m, "_DEPLOY_VERIFY_RUNNER 常量缺失"
    return m.group(1)


# ── 1. 多 Agent 审查链路 ─────────────────────────────────────

def test_multi_agent_review_functions_exist(service_src: str) -> None:
    assert "def _run_test_review_report" in service_src
    assert "def _extract_prism_facts" in service_src
    assert "multi_agent_review" in service_src
    # 测试终态后必须调用审查编排
    assert "_run_test_review_report(db, environment, conclusion)" in service_src


def test_nonretryable_model_failure_uses_deterministic_review_report(monkeypatch) -> None:
    calls = 0
    events: list[tuple] = []

    class ReviewAgent:
        _api_key = "configured"

        def review(self, *_args, **_kwargs):
            nonlocal calls
            calls += 1
            pytest.fail("不可重试模型失败后不得再次请求多 Agent 审查")

    monkeypatch.setattr(sandbox_service, "configure_subagent", lambda *_args: ReviewAgent())
    monkeypatch.setattr(sandbox_service, "_append_event", lambda *args, **_kwargs: events.append(args))
    monkeypatch.setattr(
        sandbox_service,
        "_persist_browser_artifact",
        lambda *_args, **_kwargs: SimpleNamespace(id=77),
    )
    monkeypatch.setattr(
        sandbox_service,
        "_publish_sandbox_report",
        lambda *_args, **_kwargs: {"report_persisted": True, "artifact_id": 77},
    )
    db = SimpleNamespace(commit=lambda: None)
    environment = SimpleNamespace(owner_id=1, public_id="sbx_nonretryable_review")

    result = sandbox_service._run_test_review_report(
        db,
        environment,
        {
            "passed": False,
            "summary": "AI 动态用例未完成",
            "evidence": {},
            "ai_model_stop_reason": {
                "failure_kind": "usage_accounting_error",
                "message": "用量审计提交失败，后续 AI 阶段已停止",
            },
        },
    )

    assert result["artifact_id"] == 77
    assert calls == 0
    assert any(event[3] == "multi_agent_review" and "确定性报告" in event[4] for event in events)
    completion = next(event for event in events if event[2] == "complete")
    assert completion[4] == (
        "确定性测试报告已生成并写入报告中心；"
        "模型请求不可重试（usage_accounting_error）（角色 0/4）"
    )
    assert completion[5]["review_mode"] == "deterministic_fallback"
    assert completion[5]["ai_review_complete"] is False


def test_deterministic_failure_report_names_requested_mode() -> None:
    blackbox_report = sandbox_service._build_deterministic_test_report({
        "passed": False,
        "summary": "受授权远程黑盒探测异常，验证未完成",
        "evidence": {
            "agent_test_generation": {"status": "skipped", "mode": "blackbox"},
            "execution_failure": {"kind": "remote_probe_exception"},
        },
    })

    assert "黑盒执行未通过" in blackbox_report
    assert "反编译或白盒执行未通过" not in blackbox_report
    assert "受授权远程黑盒探测发生执行异常" in blackbox_report


def test_fact_gate_ignores_fenced_pseudo_h2_when_replacing_model_conclusion() -> None:
    from app.services.sandbox_report_summary import _markdown_h2_sections, _matches_report_section

    report = """## 总体结论
模型原始结论。
```markdown
## 黑盒结果
伪造的示例章节。
```
## 白盒结果
真实白盒回执。
## 黑盒结果
真实黑盒回执。
"""

    gated = sandbox_service._fact_gate_report(
        report,
        {"passed": True, "summary": "真实执行通过", "evidence": {}},
    )
    blackbox_sections = [
        section for section in _markdown_h2_sections(gated)
        if _matches_report_section(str(section["title"]), "黑盒结果")
    ]

    assert len(blackbox_sections) == 1
    assert "真实黑盒回执" in gated
    assert "伪造的示例章节" not in gated


@pytest.mark.parametrize(
    "placeholder",
    [
        "x", "TODO", "N/A", "占位符", "- 待填写", "-", "—", "...", "…",
        "[ ]", "[]", "- [ ]", "待复核。", "待人工验证。", "生成中…", "模型未给出。",
        "TODO: fill this section", "N/A - no data", "[填充]", "待填写具体结果",
    ],
)
def test_report_completeness_gate_rejects_placeholder_only_sections(placeholder: str) -> None:
    placeholder_report = "\n".join(
        f"## {section}\n{placeholder}"
        for section in sandbox_service._TEST_REVIEW_REQUIRED_SECTIONS
    )

    assert sandbox_service._has_complete_test_review_report(placeholder_report) is False


def test_report_completeness_gate_accepts_explicit_empty_finding_list() -> None:
    report = _complete_report_candidate("- 无问题。")

    assert sandbox_service._has_complete_test_review_report(report) is True


@pytest.mark.parametrize(
    (
        "reported_executed", "reported_total", "roles", "expected_complete",
        "expected_executed", "expected_total", "expect_manifest_note",
    ),
    [
        (4, 4, {
            "whitebox": {"ok": True}, "blackbox": {"ok": True},
            "verify": {"ok": True}, "report": {"ok": True},
        }, True, 4, 4, False),
        (5, 4, {
            "whitebox": {"ok": True}, "blackbox": {"ok": True},
            "verify": {"ok": True}, "report": {"ok": True},
        }, False, 4, 4, True),
        (3, 4, {
            "whitebox": {"ok": True}, "blackbox": {"ok": True},
            "verify": {"ok": False}, "report": {"ok": True},
        }, False, 3, 4, True),
        (3, 4, {
            "whitebox": {"ok": True}, "verify": {"ok": True}, "report": {"ok": True},
        }, False, 3, 4, True),
        (5, 5, {
            "whitebox": {"ok": True}, "blackbox": {"ok": True},
            "verify": {"ok": True}, "report": {"ok": True}, "unknown_role": {"ok": True},
        }, False, 5, 5, True),
        (8, 8, {
            "whitebox": {"ok": True}, "blackbox": {"ok": True},
            "verify": {"ok": True}, "report": {"ok": True},
            "unknown_1": {"ok": True}, "unknown_2": {"ok": True},
            "unknown_3": {"ok": True}, "unknown_4": {"ok": True},
        }, False, 8, 8, True),
    ],
)
def test_report_completion_requires_consistent_successful_role_manifest(
    monkeypatch, reported_executed: int, reported_total: int,
    roles: dict[str, dict[str, bool]], expected_complete: bool,
    expected_executed: int, expected_total: int, expect_manifest_note: bool,
) -> None:
    events: list[tuple] = []
    persisted: list[dict[str, Any]] = []
    published: list[str] = []
    candidate = _complete_report_candidate()
    data = {
        "report_md": candidate,
        "roles": roles,
        "roles_executed": reported_executed,
        "roles_total": reported_total,
    }

    class ReviewAgent:
        _api_key = "configured"

        def review(self, *_args, **_kwargs):
            return SimpleNamespace(success=True, data=data)

    monkeypatch.setattr(sandbox_service, "configure_subagent", lambda *_args: ReviewAgent())
    monkeypatch.setattr(sandbox_service, "_append_event", lambda *args, **_kwargs: events.append(args))
    monkeypatch.setattr(sandbox_service, "_execution_model_guard", lambda *_args: lambda: None)
    monkeypatch.setattr(
        sandbox_service,
        "_build_deterministic_test_report",
        lambda _conclusion: "DETERMINISTIC FALLBACK",
    )
    monkeypatch.setattr(
        sandbox_service,
        "_persist_browser_artifact",
        lambda _db, _environment, **kwargs: persisted.append(kwargs) or SimpleNamespace(id=88),
    )
    monkeypatch.setattr(
        sandbox_service,
        "_publish_sandbox_report",
        lambda _db, _environment, _conclusion, report, **_kwargs: _successful_report_publication(report, published),
    )

    summary = sandbox_service._run_test_review_report(
        SimpleNamespace(commit=lambda: None),
        SimpleNamespace(owner_id=1, project_id=1, public_id="sbx_role_manifest"),
        {"passed": False, "summary": "partial", "evidence": {}},
    )

    completion = next(event for event in events if event[2] == "complete")
    assert summary is not None
    assert summary["roles_executed"] == expected_executed
    assert summary["roles_total"] == expected_total
    assert summary["ai_review_complete"] is expected_complete
    assert summary["roles_manifest_valid"] is expected_complete
    assert summary["report_candidate_valid"] is True
    assert persisted == [], "报告附件应由报告中心事务统一写入，不得提前 flush"
    persisted_content = published[0]
    assert len(published) == 1
    if expected_complete:
        assert summary["review_mode"] == "multi_agent"
        assert completion[4] == "多 Agent 测试审查报告已生成并写入报告中心（角色 4/4）"
        assert "参见结构化执行回执。" in persisted_content
        assert "参见结构化执行回执。" in published[0]
    else:
        assert summary["review_mode"] == "deterministic_fallback"
        assert completion[4].startswith("确定性测试报告已生成并写入报告中心；")
        assert ("多 Agent 角色清单或执行结果未通过完整性校验" in completion[4]) is expect_manifest_note
        assert persisted_content == "DETERMINISTIC FALLBACK"
        assert published[0] == "DETERMINISTIC FALLBACK"


def test_unpersisted_complete_ai_report_is_replaced_by_persisted_deterministic_report(monkeypatch) -> None:
    published: list[tuple[str, dict[str, Any]]] = []
    publish_options: list[dict[str, Any]] = []
    events: list[tuple] = []
    roles = {
        "whitebox": {"ok": True},
        "blackbox": {"ok": True},
        "verify": {"ok": True},
        "report": {"ok": True},
    }

    class ReviewAgent:
        _api_key = "configured"

        def review(self, *_args, **_kwargs):
            return SimpleNamespace(
                success=True,
                data={
                    "report_md": _complete_report_candidate(),
                    "roles": roles,
                    "roles_executed": 4,
                    "roles_total": 4,
                },
            )

    monkeypatch.setattr(sandbox_service, "configure_subagent", lambda *_args: ReviewAgent())
    monkeypatch.setattr(sandbox_service, "_append_event", lambda *args, **_kwargs: events.append(args))
    monkeypatch.setattr(sandbox_service, "_execution_model_guard", lambda *_args: lambda: None)
    publish_attempts = 0

    def publish(_db, _environment, conclusion, report, **_kwargs):
        nonlocal publish_attempts
        publish_attempts += 1
        published.append((report, dict(conclusion)))
        publish_options.append(dict(_kwargs))
        if publish_attempts == 1:
            return {"report_persisted": False, "failure_code": "report_persistence_failed"}
        return {"report_persisted": True, "report_task_id": 124, "artifact_id": 90, "issues": 0}

    monkeypatch.setattr(sandbox_service, "_publish_sandbox_report", publish)
    conclusion = {"passed": True, "summary": "执行通过", "evidence": {}}
    summary = sandbox_service._run_test_review_report(
        SimpleNamespace(commit=lambda: None),
        SimpleNamespace(owner_id=1, project_id=1, public_id="sbx_ai_report_retry", test_mode="combined"),
        conclusion,
    )

    assert publish_attempts == 2
    assert publish_options[1]["_authoritative_ai_review"] is True
    assert "## 白盒结果" in published[0][0]
    assert "多 Agent AI 审查：未完成；原因：AI 报告未能写入报告中心；已改用确定性执行报告。" in published[1][0]
    assert published[1][1]["ai_review_status"] == "incomplete"
    assert summary["ai_review_generated"] is True
    assert summary["ai_review_complete"] is False
    assert summary["review_mode"] == "deterministic_fallback"
    assert summary["report_persisted"] is True
    assert summary["report_task_id"] == 124
    completion = next(event for event in events if event[2] == "complete")
    assert "确定性测试报告已生成并写入报告中心" in completion[4]


def test_empty_report_candidate_falls_back_with_explicit_reason(monkeypatch) -> None:
    events: list[tuple] = []
    persisted: list[dict[str, Any]] = []
    published: list[str] = []

    class ReviewAgent:
        _api_key = "configured"

        def review(self, *_args, **_kwargs):
            return SimpleNamespace(
                success=True,
                data={
                    "report_md": " \n ",
                    "roles": {
                        "whitebox": {"ok": True}, "blackbox": {"ok": True},
                        "verify": {"ok": True}, "report": {"ok": True},
                    },
                    "roles_executed": 4,
                    "roles_total": 4,
                },
            )

    monkeypatch.setattr(sandbox_service, "configure_subagent", lambda *_args: ReviewAgent())
    monkeypatch.setattr(sandbox_service, "_append_event", lambda *args, **_kwargs: events.append(args))
    monkeypatch.setattr(sandbox_service, "_execution_model_guard", lambda *_args: lambda: None)
    monkeypatch.setattr(
        sandbox_service,
        "_build_deterministic_test_report",
        lambda _conclusion: "DETERMINISTIC FALLBACK",
    )
    monkeypatch.setattr(
        sandbox_service,
        "_persist_browser_artifact",
        lambda _db, _environment, **kwargs: persisted.append(kwargs) or SimpleNamespace(id=89),
    )
    monkeypatch.setattr(
        sandbox_service,
        "_publish_sandbox_report",
        lambda _db, _environment, _conclusion, report, **_kwargs: _successful_report_publication(report, published),
    )

    summary = sandbox_service._run_test_review_report(
        SimpleNamespace(commit=lambda: None),
        SimpleNamespace(owner_id=1, project_id=1, public_id="sbx_empty_report_candidate"),
        {"passed": False, "summary": "partial", "evidence": {}},
    )

    completion = next(event for event in events if event[2] == "complete")
    assert summary is not None
    assert summary["roles_manifest_valid"] is True
    assert summary["report_candidate_valid"] is False
    assert summary["ai_review_complete"] is False
    assert "AI 报告为空或缺少必需章节" in completion[4]
    assert persisted == [], "确定性报告附件应由统一发布事务写入"
    assert published == ["DETERMINISTIC FALLBACK"]


def test_usage_accounting_error_preserves_deterministic_review_report(monkeypatch) -> None:
    from app.services.ai_usage_context import UsageAccountingError

    calls = 0
    persisted: list[dict[str, Any]] = []

    class ReviewAgent:
        _api_key = "configured"

        def review(self, *_args, **_kwargs):
            nonlocal calls
            calls += 1
            raise UsageAccountingError("audit write failed")

    monkeypatch.setattr(sandbox_service, "configure_subagent", lambda *_args: ReviewAgent())
    monkeypatch.setattr(sandbox_service, "_append_event", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(sandbox_service, "_execution_model_guard", lambda *_args: lambda: None)
    monkeypatch.setattr(
        sandbox_service,
        "_build_deterministic_test_report",
        lambda _conclusion: "DETERMINISTIC FALLBACK",
    )
    monkeypatch.setattr(
        sandbox_service,
        "_persist_browser_artifact",
        lambda _db, _environment, **kwargs: persisted.append(kwargs) or SimpleNamespace(id=78),
    )
    monkeypatch.setattr(
        sandbox_service,
        "_publish_sandbox_report",
        lambda *_args, **_kwargs: {"report_persisted": True, "artifact_id": 78},
    )
    db = SimpleNamespace(commit=lambda: None)
    environment = SimpleNamespace(owner_id=1, project_id=1, public_id="sbx_usage_accounting_review")

    result = sandbox_service._run_test_review_report(
        db,
        environment,
        {"passed": False, "summary": "AI 动态用例未完成", "evidence": {}},
    )

    assert calls == 1
    assert result["artifact_id"] == 78
    assert persisted == [], "报告附件必须和报告中心记录在同一事务中写入"


@pytest.mark.parametrize("failure_kind", ["billing_error", "usage_accounting_error"])
def test_reporter_nonretryable_result_is_committed_for_recovery(monkeypatch, failure_kind: str) -> None:
    events: list[tuple] = []
    commits: list[bool] = []

    class ReviewAgent:
        _api_key = "configured"

        def review(self, *_args, **_kwargs):
            return SimpleNamespace(
                success=False,
                error="model stage stopped",
                failure_kind=failure_kind,
            )

    monkeypatch.setattr(sandbox_service, "configure_subagent", lambda *_args: ReviewAgent())
    monkeypatch.setattr(sandbox_service, "_append_event", lambda *args, **_kwargs: events.append(args))
    monkeypatch.setattr(sandbox_service, "_execution_model_guard", lambda *_args: lambda: None)
    monkeypatch.setattr(sandbox_service, "_persist_browser_artifact", lambda *_args, **_kwargs: SimpleNamespace(id=79))
    monkeypatch.setattr(
        sandbox_service,
        "_publish_sandbox_report",
        lambda *_args, **_kwargs: {"report_persisted": True, "artifact_id": 79},
    )
    db = SimpleNamespace(commit=lambda: commits.append(True))

    result = sandbox_service._run_test_review_report(
        db,
        SimpleNamespace(owner_id=1, project_id=1, public_id="sbx_reporter_nonretryable"),
        {"passed": False, "summary": "partial", "evidence": {}},
    )

    failure_events = [
        event for event in events
        if event[3] == "multi_agent_review"
        and len(event) > 5
        and isinstance(event[5], dict)
        and event[5].get("failure_kind") == failure_kind
    ]
    assert result["artifact_id"] == 79
    assert failure_events
    assert commits, "the stop event must survive a worker/process restart"


def test_reporter_setup_failure_publishes_deterministic_fallback(monkeypatch) -> None:
    events: list[tuple] = []
    conclusion = {"passed": True, "summary": "deterministic execution passed", "evidence": {}}
    persisted: list[dict[str, Any]] = []
    published: list[str] = []

    def throw_billing(*_args, **_kwargs):
        error = RuntimeError("provider refused request")
        error.failure_kind = "billing_error"
        raise error

    monkeypatch.setattr(sandbox_service, "configure_subagent", throw_billing)
    monkeypatch.setattr(sandbox_service, "_append_event", lambda *args, **_kwargs: events.append(args))
    monkeypatch.setattr(
        sandbox_service,
        "_build_deterministic_test_report",
        lambda _conclusion: "DETERMINISTIC FALLBACK",
    )
    monkeypatch.setattr(
        sandbox_service,
        "_persist_browser_artifact",
        lambda _db, _environment, **kwargs: persisted.append(kwargs) or SimpleNamespace(id=90),
    )
    monkeypatch.setattr(
        sandbox_service,
        "_publish_sandbox_report",
        lambda _db, _environment, _conclusion, report, **_kwargs: _successful_report_publication(report, published),
    )
    commits: list[bool] = []
    db = SimpleNamespace(commit=lambda: commits.append(True))
    environment = SimpleNamespace(owner_id=1, project_id=1, public_id="sbx_reporter_wrapper")

    result = sandbox_service._run_test_review_report(db, environment, conclusion)

    assert result is not None
    assert result["review_mode"] == "deterministic_fallback"
    assert result["report_persisted"] is True
    assert persisted == [], "报告附件应在报告中心事务中统一创建"
    assert published == ["DETERMINISTIC FALLBACK"]
    assert conclusion["ai_model_stop_reason"]["failure_kind"] == "billing_error"
    assert any(event[3] == "multi_agent_review" and event[5].get("failure_kind") == "billing_error" for event in events)
    assert commits


def test_model_report_findings_are_separated_as_unverified_candidates(monkeypatch) -> None:
    report = _complete_report_candidate(
        "### [高] 虚构源码问题\n位置：invented.py:999\n证据：source_that_does_not_exist()"
    )
    events: list[tuple] = []
    published: list[str] = []

    class ReviewAgent:
        _api_key = "configured"

        def review(self, *_args, **_kwargs):
            return SimpleNamespace(
                success=True,
                data={
                    "report_md": report,
                    "roles": {
                        "whitebox": {"ok": True}, "blackbox": {"ok": True},
                        "verify": {"ok": True}, "report": {"ok": True},
                    },
                    "roles_executed": 4,
                    "roles_total": 4,
                },
            )

    monkeypatch.setattr(sandbox_service, "configure_subagent", lambda *_args: ReviewAgent())
    monkeypatch.setattr(sandbox_service, "_append_event", lambda *args, **_kwargs: events.append(args))
    monkeypatch.setattr(sandbox_service, "_execution_model_guard", lambda *_args: lambda: None)
    monkeypatch.setattr(
        sandbox_service,
        "_persist_browser_artifact",
        lambda *_args, **_kwargs: pytest.fail("报告附件不得提前写入"),
    )
    monkeypatch.setattr(
        sandbox_service,
        "_publish_sandbox_report",
        lambda _db, _environment, _conclusion, output, **_kwargs: _successful_report_publication(output, published),
    )

    summary = sandbox_service._run_test_review_report(
        SimpleNamespace(commit=lambda: None),
        SimpleNamespace(owner_id=1, project_id=1, public_id="sbx_unverified_finding"),
        {"passed": True, "summary": "deterministic execution passed", "evidence": {}},
    )

    assert summary["ai_review_complete"] is True
    assert summary["unverified_findings"] == 1
    assert "## 问题清单" in published[0]
    assert "## AI 候选发现（未核实）" in published[0]
    assert "invented.py:999" in published[0]
    assert published[0].index("## 问题清单") < published[0].index("## AI 候选发现（未核实）")
    assert "不得视为已确认漏洞" in published[0]
    from app.services.sandbox_report_summary import summarize_sandbox_report

    assert summarize_sandbox_report(published[0])["total"] == 0


def test_unparseable_report_candidate_is_not_counted_as_confirmed(monkeypatch) -> None:
    report = _complete_report_candidate("可能存在未经复现的注入问题，需复核。")
    separated, candidate_count = sandbox_service._separate_unverified_ai_findings(report)

    assert candidate_count is None
    assert "## AI 候选发现（未核实）" in separated
    assert "未经复现的注入问题" in separated
    assert "## 问题清单\n\n- 无问题。" in separated


def test_fenced_pseudo_issue_heading_does_not_hide_real_issue_list() -> None:
    from app.services.sandbox_report_summary import (
        _markdown_h2_sections,
        _matches_report_section,
        summarize_sandbox_report,
    )

    report = """## 总体结论
结果待复核。
## 白盒结果
白盒执行记录。
## 黑盒结果
黑盒输出示例：
```markdown
## 问题清单
### [低] 伪造示例，不是真实报告条目
```
## 4. 问题清单
### [高] SQL 注入候选
位置：app.py:21
证据：未经复现的模型候选
## 攻击面待验证清单
待人工复核。
## 下一步建议
执行复现。
## 证据附录
执行回执。
"""

    separated, candidate_count = sandbox_service._separate_unverified_ai_findings(report)

    sections = _markdown_h2_sections(separated)
    blackbox_section = next(item for item in sections if item["title"] == "黑盒结果")
    issue_section = next(item for item in sections if _matches_report_section(str(item["title"]), "问题清单"))
    candidate_section = next(
        item for item in sections if _matches_report_section(str(item["title"]), "AI 候选发现（未核实）")
    )
    blackbox = separated[int(blackbox_section["body_start"]):int(blackbox_section["end"])]
    issue_list = separated[int(issue_section["body_start"]):int(issue_section["end"])]
    candidates = separated[int(candidate_section["body_start"]):int(candidate_section["end"])]
    assert "## 问题清单" in blackbox
    assert "伪造示例" in blackbox
    assert "SQL 注入候选" not in issue_list
    assert "SQL 注入候选" in candidates
    assert candidate_count == 1
    assert summarize_sandbox_report(separated)["total"] == 0


def test_duplicate_issue_sections_merge_all_findings_into_unverified_candidates() -> None:
    from app.services.sandbox_report_summary import (
        _markdown_h2_sections,
        _matches_report_section,
        summarize_sandbox_report,
    )

    report = """## 总体结论
本次结论限于已保存的执行回执，未覆盖部分需补充验证。
## 白盒结果
执行记录。
## 黑盒结果
执行记录。
## 问题清单
- 暂无问题。
## 4. 问题清单
### [高] 第二清单候选
位置：second.py:21
证据：模型给出的未复现候选。
## 4、问题清单
### [中] 第三清单候选
位置：third.py:34
证据：另一条未复现候选。
## 攻击面待验证清单
身份校验失败后的横向访问路径尚未覆盖，需追加授权样本。
## 下一步建议
对上述路径执行人工复现并保存请求、响应和目标版本信息。
## 证据附录
模型输出。
"""

    separated, candidate_count = sandbox_service._separate_unverified_ai_findings(report)

    sections = _markdown_h2_sections(separated)
    issue_sections = [
        item for item in sections
        if _matches_report_section(str(item["title"]), "问题清单")
    ]
    candidate_sections = [
        item for item in sections
        if _matches_report_section(str(item["title"]), "AI 候选发现（未核实）")
    ]
    issue_body = separated[int(issue_sections[0]["body_start"]):int(issue_sections[0]["end"])]
    candidate_body = separated[int(candidate_sections[0]["body_start"]):int(candidate_sections[0]["end"])]

    assert len(issue_sections) == 1
    assert len(candidate_sections) == 1
    assert "- 无问题。" in issue_body
    assert "第二清单候选" in candidate_body
    assert "第三清单候选" in candidate_body
    assert candidate_count == 2
    assert summarize_sandbox_report(separated)["total"] == 0


def test_existing_candidate_sections_are_included_in_candidate_count_and_merge_idempotently() -> None:
    from app.services.sandbox_report_summary import _markdown_h2_sections, _matches_report_section

    report = """## 总体结论
待复核。
## 白盒结果
执行记录。
## 黑盒结果
执行记录。
## 问题清单
- 暂无问题。
## 4. 问题清单
### [高] 新候选
位置：fresh.py:8
## AI 候选发现（未核实）
以下条目未由确定性执行回执或人工复现确认，不得视为已确认漏洞：
### [中] 已有候选
位置：existing.py:12
## 攻击面待验证清单
待复核。
## 下一步建议
人工复现。
## 证据附录
模型输出。
"""

    separated, candidate_count = sandbox_service._separate_unverified_ai_findings(report)
    repeated, repeated_count = sandbox_service._separate_unverified_ai_findings(separated)
    sections = _markdown_h2_sections(repeated)
    candidate_sections = [
        item for item in sections
        if _matches_report_section(str(item["title"]), "AI 候选发现（未核实）")
    ]
    candidate_body = repeated[int(candidate_sections[0]["body_start"]):int(candidate_sections[0]["end"])]

    assert candidate_count == repeated_count == 2
    assert len(candidate_sections) == 1
    assert "新候选" in candidate_body
    assert "已有候选" in candidate_body


def test_duplicate_empty_issue_sections_are_canonicalized_without_candidates() -> None:
    from app.services.sandbox_report_summary import _markdown_h2_sections, _matches_report_section

    report = """## 总体结论
待复核。
## 白盒结果
执行记录。
## 黑盒结果
执行记录。
## 问题清单
- 暂无问题。
## 4. 问题清单
暂无漏洞
## 攻击面待验证清单
待复核。
## 下一步建议
人工复现。
## 证据附录
执行回执。
"""

    separated, candidate_count = sandbox_service._separate_unverified_ai_findings(report)
    issue_sections = [
        item for item in _markdown_h2_sections(separated)
        if _matches_report_section(str(item["title"]), "问题清单")
    ]

    assert candidate_count == 0
    assert len(issue_sections) == 1
    assert "AI 候选发现（未核实）" not in separated


def test_mixed_root_list_and_heading_candidates_are_both_counted_as_unverified() -> None:
    from app.services.sandbox_report_summary import summarize_sandbox_report

    report = _complete_report_candidate(
        "- [低] 列表候选A\n### [高] 标题候选B\n- 证据：未经复现的模型输出"
    )
    separated, candidate_count = sandbox_service._separate_unverified_ai_findings(report)

    assert candidate_count == 2
    assert summarize_sandbox_report(separated)["total"] == 0
    assert "列表候选A" in separated
    assert "标题候选B" in separated


def test_exact_duplicate_finding_in_issue_and_candidate_sections_is_rendered_once() -> None:
    from app.services.sandbox_report_summary import _markdown_h2_sections, _matches_report_section

    finding = "### [高] 相同候选\n位置：same.py:7\n证据：原始证据片段"
    report = _complete_report_candidate(finding) + "\n## AI 候选发现（未核实）\n" + finding + "\n"

    separated, candidate_count = sandbox_service._separate_unverified_ai_findings(report)
    candidate_section = next(
        item for item in _markdown_h2_sections(separated)
        if _matches_report_section(str(item["title"]), "AI 候选发现（未核实）")
    )
    candidate_body = separated[int(candidate_section["body_start"]):int(candidate_section["end"])]

    assert candidate_body.count("相同候选") == 1
    assert candidate_count == 1


def test_duplicate_fenced_pseudo_finding_headings_do_not_truncate_following_sections() -> None:
    from app.services.sandbox_report_summary import _markdown_h2_sections, _matches_report_section

    report = """## 总体结论
本次结论限于已保存的执行回执，未覆盖部分需补充验证。
## 白盒结果
执行记录。
## 黑盒结果
执行记录。
## 问题清单
来源一：模型引用的 Markdown 示例。
```markdown
### [低] 示例标题
示例正文。
```
## 4. 问题清单
来源二：重复的 Markdown 示例。
```markdown
### [低] 示例标题
示例正文。
```
## 攻击面待验证清单
身份校验失败后的横向访问路径尚未覆盖，需追加授权样本。
## 下一步建议
对上述路径执行人工复现并保存请求、响应和目标版本信息。
## 证据附录
执行回执。
"""

    separated, candidate_count = sandbox_service._separate_unverified_ai_findings(report)
    sections = _markdown_h2_sections(separated)
    section_titles = {str(item["title"]) for item in sections}

    assert candidate_count is None
    assert "### [低] 示例标题" in separated
    assert sum(line == "```markdown" for line in separated.splitlines()) == 2
    assert sum(line == "```" for line in separated.splitlines()) == 2
    assert _matches_report_section("攻击面待验证清单", "攻击面待验证清单")
    assert any(_matches_report_section(title, "攻击面待验证清单") for title in section_titles)
    assert any(_matches_report_section(title, "下一步建议") for title in section_titles)
    assert any(_matches_report_section(title, "证据附录") for title in section_titles)
    assert sandbox_service._has_complete_test_review_report(separated)


def test_candidate_deduplication_preserves_same_evidence_field_for_distinct_findings() -> None:
    from app.services.sandbox_report_summary import _markdown_h2_sections, _matches_report_section

    report = _complete_report_candidate(
        """### [中] 发现一
### 证据
同一段通用证据说明。
### [高] 发现二
### 证据
同一段通用证据说明。"""
    )

    separated, candidate_count = sandbox_service._separate_unverified_ai_findings(report)
    candidate_section = next(
        item for item in _markdown_h2_sections(separated)
        if _matches_report_section(str(item["title"]), "AI 候选发现（未核实）")
    )
    candidate_body = separated[int(candidate_section["body_start"]):int(candidate_section["end"])]

    assert candidate_count == 2
    assert candidate_body.count("### 证据") == 2
    assert candidate_body.count("同一段通用证据说明。") == 2


@pytest.mark.parametrize("empty_line", ["无问题。", "- 暂无问题。", "暂无漏洞"])
def test_empty_ai_issue_list_stays_zero_in_report_summary(empty_line: str) -> None:
    from app.services.sandbox_report_summary import summarize_sandbox_report

    separated, candidate_count = sandbox_service._separate_unverified_ai_findings(
        _complete_report_candidate(empty_line)
    )

    assert candidate_count == 0
    assert "AI 候选发现（未核实）" not in separated
    assert summarize_sandbox_report(separated)["total"] == 0


def test_missing_model_key_reason_is_saved_in_report_body_and_metadata(monkeypatch) -> None:
    published: list[tuple[str, dict[str, Any]]] = []

    class UnconfiguredReviewAgent:
        _api_key = ""

    monkeypatch.setattr(sandbox_service, "configure_subagent", lambda *_args: UnconfiguredReviewAgent())
    monkeypatch.setattr(sandbox_service, "_append_event", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        sandbox_service,
        "_persist_browser_artifact",
        lambda _db, _environment, **kwargs: SimpleNamespace(id=93, **kwargs),
    )

    def publish(_db, _environment, conclusion, report, **_kwargs):
        published.append((report, dict(conclusion)))
        return {"report_persisted": True, "report_task_id": 123, "issues": 0}

    monkeypatch.setattr(sandbox_service, "_publish_sandbox_report", publish)
    db = SimpleNamespace(commit=lambda: None)
    environment = SimpleNamespace(owner_id=1, project_id=1, public_id="sbx_missing_model_key", test_mode="combined")
    conclusion = {"passed": True, "summary": "deterministic execution passed", "evidence": {}}

    summary = sandbox_service._run_test_review_report(db, environment, conclusion)

    assert summary["ai_review_complete"] is False
    assert summary["report_persisted"] is True
    assert "多 Agent AI 审查：未完成" in published[0][0]
    assert "模型未配置" in published[0][0]
    assert "不代表 AI 审查已完成" in published[0][0]
    assert published[0][1]["ai_review_status"] == "incomplete"
    assert published[0][1]["ai_review_failure_reason"] == "模型未配置"


def test_authorization_loss_during_report_preserves_completed_runner_result(monkeypatch) -> None:
    from app.core.exceptions import ForbiddenError

    calls = 0
    fallback_calls: list[str] = []
    environment = SimpleNamespace(id=12, status="finalizing")
    conclusion = {"passed": True, "summary": "runner completed", "evidence": {"worker_result": {"exit_code": 0}}}

    def require_lease(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise ForbiddenError("权限已撤销")

    def fallback(_db, _environment, current_conclusion, *, failure_reason, failure_kind):
        fallback_calls.append(failure_reason)
        current_conclusion["ai_review_failure_reason"] = failure_reason
        current_conclusion["ai_review_status"] = "incomplete"
        return {"ai_review_complete": False, "report_persisted": True, "failure_kind": failure_kind}

    monkeypatch.setattr(sandbox_service, "_require_execution_lease", require_lease)
    monkeypatch.setattr(
        sandbox_service,
        "_run_test_review_report",
        lambda *_args: {"ai_review_complete": True, "report_persisted": True},
    )
    monkeypatch.setattr(sandbox_service, "_persist_deterministic_test_review_fallback", fallback)
    db = SimpleNamespace(rollback=lambda: None, refresh=lambda _environment: None)

    summary = sandbox_service._run_test_review_report_with_authorization_fallback(
        db, environment, conclusion, "lease-token"
    )

    assert summary["ai_review_complete"] is False
    assert conclusion["passed"] is True
    assert conclusion["summary"] == "runner completed"
    assert conclusion["evidence"]["worker_result"]["exit_code"] == 0
    assert conclusion["ai_review_status"] == "incomplete"
    assert fallback_calls == ["报告阶段执行授权已失效，AI 审查未完成"]


def test_incomplete_model_report_uses_deterministic_fallback(monkeypatch) -> None:
    events: list[tuple] = []
    published: list[str] = []

    class ReviewAgent:
        _api_key = "configured"

        def review(self, *_args, **_kwargs):
            return SimpleNamespace(
                success=True,
                data={
                    "report_md": "## 总体结论\n只有结论，没有七个章节。\n## 问题清单\nSQLi",
                    "roles": {
                        "whitebox": {"ok": True}, "blackbox": {"ok": True},
                        "verify": {"ok": True}, "report": {"ok": True},
                    },
                    "roles_executed": 4,
                    "roles_total": 4,
                },
            )

    monkeypatch.setattr(sandbox_service, "configure_subagent", lambda *_args: ReviewAgent())
    monkeypatch.setattr(sandbox_service, "_append_event", lambda *args, **_kwargs: events.append(args))
    monkeypatch.setattr(sandbox_service, "_execution_model_guard", lambda *_args: lambda: None)
    monkeypatch.setattr(sandbox_service, "_build_deterministic_test_report", lambda _c: "DETERMINISTIC FALLBACK")
    monkeypatch.setattr(
        sandbox_service,
        "_persist_browser_artifact",
        lambda *_args, **_kwargs: pytest.fail("报告附件不得提前写入"),
    )
    monkeypatch.setattr(
        sandbox_service,
        "_publish_sandbox_report",
        lambda _db, _environment, _conclusion, output, **_kwargs: _successful_report_publication(output, published),
    )

    summary = sandbox_service._run_test_review_report(
        SimpleNamespace(commit=lambda: None),
        SimpleNamespace(owner_id=1, project_id=1, public_id="sbx_incomplete_report"),
        {"passed": False, "summary": "partial", "evidence": {}},
    )

    assert summary["ai_review_complete"] is False
    assert summary["report_candidate_valid"] is False
    assert summary["ai_review_failure_reason"] == "AI 报告为空或缺少必需章节"
    assert published == ["DETERMINISTIC FALLBACK"]
    assert any(event[2] == "complete" and "缺少必需章节" in event[4] for event in events)


def test_review_report_artifact_type(service_src: str) -> None:
    assert 'artifact_type="review_report"' in service_src
    assert "sandbox-review-report-" in service_src


def test_recon_facts_recovery(service_src: str, runner_src: str) -> None:
    assert "PRISM_FACTS_BEGIN" in runner_src and "PRISM_FACTS_END" in runner_src
    assert "collect_facts" in runner_src and "emit_facts" in runner_src
    assert "recon_facts" in service_src
    # 日志回收解析正则
    assert re.search(r"PRISM_FACTS_BEGIN.*PRISM_FACTS_END", service_src, re.S)


def test_test_reviewer_agent_four_roles() -> None:
    src = AGENT_PATH.read_text(encoding="utf-8")
    for role in ("_WHITEBOX_PROMPT", "_BLACKBOX_PROMPT", "_VERIFY_PROMPT", "_REPORT_PROMPT"):
        assert role in src, f"{role} 缺失"
    # 对抗复检:无证据降级纪律
    assert "降级" in src or "downgrade" in src.lower()
    # redact 脱敏(密钥/Token/密码)
    assert "_redact" in src and re.search(r"token\|secret\|password|secret\|password\|token", src)


# ── 2. 隔离归档允许部署(40341 不得加回) ─────────────────────

def test_quarantine_40341_hard_block_removed(service_src: str) -> None:
    # 线上已移除 40341 硬隔离(仅 JWT 代理预览),严禁回归加回
    assert "40341" not in service_src, "40341 硬隔离被重新加回,违反隔离归档允许部署红线"
    assert "持续部署预览已禁用" not in service_src
    # 必须保留"允许部署但仅 JWT 代理预览"的注释语义
    assert "隔离归档允许部署" in service_src or "JWT" in service_src


# ── 3. 线上治理保留 ──────────────────────────────────────────

def test_php_doc_root_preserved(runner_src: str) -> None:
    assert "php_doc_root()" in runner_src
    assert "ROOT=$(php_doc_root)" in runner_src


def test_dominant_archive_language_preserved(service_src: str) -> None:
    assert "def _dominant_archive_language" in service_src
    assert "detect_language" in service_src


# ── 4. seed 登记 ─────────────────────────────────────────────

def test_test_review_playbook_seeded() -> None:
    assert PLAYBOOK_PATH.exists(), "sandbox_test_review_playbook.md 缺失"
    seed = SEED_PATH.read_text(encoding="utf-8")
    assert '"test_review"' in seed and "sandbox_test_review_playbook.md" in seed


# ── 5. facts 提取纯逻辑(不依赖 DB) ───────────────────────────

def test_extract_prism_facts_logic() -> None:
    # 只加载函数,绕过模块级重依赖
    src = SERVICE_PATH.read_text(encoding="utf-8")
    m = re.search(r"def _extract_prism_facts.*?(?=\ndef )", src, re.S)
    assert m
    ns: dict = {}
    exec("import json, re\nfrom typing import Any\n" + m.group(0), ns)
    fn = ns["_extract_prism_facts"]
    log = 'x\nPRISM_FACTS_BEGIN\n{"a":1,"endpoints":[{"path":"/api"}]}\n\nPRISM_FACTS_END\ny'
    assert fn(log) == {"a": 1, "endpoints": [{"path": "/api"}]}
    assert fn("no facts") is None
    assert fn("PRISM_FACTS_BEGIN\n{bad json}\nPRISM_FACTS_END") is None
