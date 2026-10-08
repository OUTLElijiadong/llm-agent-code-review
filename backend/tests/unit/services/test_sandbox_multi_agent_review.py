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
    monkeypatch.setattr(sandbox_service, "_publish_sandbox_report", lambda *_args, **_kwargs: None)
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
    monkeypatch.setattr(sandbox_service, "_publish_sandbox_report", lambda *_args, **_kwargs: None)
    db = SimpleNamespace(commit=lambda: None)
    environment = SimpleNamespace(owner_id=1, project_id=1, public_id="sbx_usage_accounting_review")

    result = sandbox_service._run_test_review_report(
        db,
        environment,
        {"passed": False, "summary": "AI 动态用例未完成", "evidence": {}},
    )

    assert calls == 1
    assert result["artifact_id"] == 78
    assert len(persisted) == 1
    assert persisted[0]["content"] == b"DETERMINISTIC FALLBACK"


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
    monkeypatch.setattr(sandbox_service, "_publish_sandbox_report", lambda *_args, **_kwargs: None)
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


def test_reporter_wrapper_persists_thrown_nonretryable_failure(monkeypatch) -> None:
    events: list[tuple] = []
    conclusion = {"passed": True, "summary": "deterministic execution passed", "evidence": {}}

    def throw_billing(*_args, **_kwargs):
        error = RuntimeError("provider refused request")
        error.failure_kind = "billing_error"
        raise error

    monkeypatch.setattr(sandbox_service, "configure_subagent", throw_billing)
    monkeypatch.setattr(sandbox_service, "_append_event", lambda *args, **_kwargs: events.append(args))
    commits: list[bool] = []
    db = SimpleNamespace(commit=lambda: commits.append(True))
    environment = SimpleNamespace(owner_id=1, project_id=1, public_id="sbx_reporter_wrapper")

    result = sandbox_service._run_test_review_report(db, environment, conclusion)

    assert result is None
    assert conclusion["ai_model_stop_reason"]["failure_kind"] == "billing_error"
    assert events[-1][3] == "multi_agent_review"
    assert events[-1][5]["failure_kind"] == "billing_error"
    assert commits


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
