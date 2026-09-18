"""统一安全规则目录的来源、执行状态和用户隔离回归。"""
from __future__ import annotations

import json
from types import SimpleNamespace

from app.models.review_rule import ReviewRule
from app.services import security_rule_catalog_service as service


def test_catalog_combines_existing_sources_without_cross_user_leak(db, monkeypatch):
    owner = SimpleNamespace(id=11)
    db.add_all([
        ReviewRule(
            user_id=None, rule_code="builtin", rule_name="内置", rule_type="security",
            rule_content="检查参数化查询", language="python", severity="高",
            enabled=1, is_builtin=1, sort_order=1,
        ),
        ReviewRule(
            user_id=11, rule_code="mine", rule_name="我的", rule_type="style",
            rule_content="检查命名", language="*", severity="低",
            enabled=1, is_builtin=0, sort_order=2,
        ),
        ReviewRule(
            user_id=12, rule_code="foreign", rule_name="他人", rule_type="security",
            rule_content="不可见", language="*", severity="中",
            enabled=1, is_builtin=0, sort_order=3,
        ),
    ])
    db.commit()
    monkeypatch.setattr(service, "_codeql_executable", lambda: None)
    service.codeql_capability.cache_clear()

    catalog = service.build_catalog(db, owner, include_review_rules=True)

    codes = {item["code"] for item in catalog["items"]}
    assert {"builtin", "mine"}.issubset(codes)
    assert "foreign" not in codes
    assert catalog["counts"]["review_rule"] == 2
    assert catalog["counts"]["platform_static"] > 0
    assert catalog["counts"]["platform_secret"] > 0
    codeql = next(item for item in catalog["engines"] if item["code"] == "github_codeql")
    assert codeql["status"] == "ci_configured"
    assert codeql["executable"] is False
    assert "未表示扫描通过" in codeql["status_message"]
    assert "仅用于漏洞情报" in catalog["mapping_note"]


def test_codeql_version_is_read_with_argument_array(monkeypatch, tmp_path):
    binary = tmp_path / "codeql"
    binary.write_text("", encoding="utf-8")
    binary.chmod(0o755)
    calls = []

    def fake_run(args, **kwargs):
        calls.append((args, kwargs))
        return SimpleNamespace(stdout=json.dumps({"version": "2.27.0"}))

    monkeypatch.setenv("CODEQL_CLI_PATH", str(binary))
    monkeypatch.setattr(service.subprocess, "run", fake_run)
    service.codeql_capability.cache_clear()

    capability = service.codeql_capability()

    assert capability["status"] == "cli_detected"
    assert capability["executable"] is False
    assert capability["version"] == "2.27.0"
    assert calls[0][0] == [str(binary.resolve()), "version", "--format=json"]
    assert calls[0][1]["timeout"] == 10
    assert "shell" not in calls[0][1]


def test_catalog_without_rule_permission_omits_prompt_rule_content(db, monkeypatch):
    owner = SimpleNamespace(id=11)
    db.add(ReviewRule(
        user_id=11,
        rule_code="private_prompt",
        rule_name="私有提示规则",
        rule_type="security",
        rule_content="仅 rule:view 可读",
        language="*",
        severity="中",
        enabled=1,
        is_builtin=0,
        sort_order=1,
    ))
    db.commit()
    monkeypatch.setattr(service, "_codeql_executable", lambda: None)
    service.codeql_capability.cache_clear()

    catalog = service.build_catalog(db, owner, include_review_rules=False)

    assert "review_rule" not in catalog["counts"]
    assert all(item["code"] != "private_prompt" for item in catalog["items"])
