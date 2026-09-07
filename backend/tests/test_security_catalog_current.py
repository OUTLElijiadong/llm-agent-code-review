"""官方快照、扫描新标签和历史标签边界的回归验证。"""
from collections import Counter
import importlib.util
import json
from pathlib import Path
import re

from app.agents.security_sentinel_agent import SecuritySentinelAgent
from app.ai.result_parser import _infer_owasp_cwe, parse
from app.ai.security_patterns import list_patterns
from app.ai.security_static_rules import list_static_rules
from app.constants.security_catalog import (
    OWASP_CATEGORIES, catalog_metadata, get_cwe, owasp_for_cwe, owasp_reference,
)
from app.models.knowledge_chunk import KnowledgeChunk  # noqa: F401 -- register isolated tables
from app.models.knowledge_doc import KnowledgeDoc  # noqa: F401


def test_official_snapshot_counts_and_frontend_match():
    metadata = catalog_metadata()
    assert (metadata["owasp_version"], metadata["cwe_version"]) == ("2025", "4.20")
    assert metadata["cwe_weakness_count"] == 944
    mapped = [cwe for category in OWASP_CATEGORIES for cwe in category["cwe_refs"]]
    assert len(mapped) == len(set(mapped)) == 249
    assert Counter(get_cwe(cwe)["kind"] for cwe in mapped) == {"Weakness": 246, "Category": 3}
    frontend = Path(__file__).resolve().parents[2] / "frontend/src/views/security/security-catalog.json"
    assert json.loads(frontend.read_text())["categories"] == list(OWASP_CATEGORIES)


def test_rules_and_api_use_official_mapping():
    checklist = SecuritySentinelAgent().get_checklist()
    assert checklist["catalog_metadata"]["owasp_version"] == "2025"
    assert [row["owasp"] for row in checklist["owasp_top10"]] == [row["owasp"] for row in OWASP_CATEGORIES]
    for rule in list_patterns() + list_static_rules():
        assert get_cwe(rule["cwe"]), rule
        assert rule["owasp"] == owasp_for_cwe(rule["cwe"]), rule


def test_ssrf_injection_exception_and_secret_categories():
    assert owasp_for_cwe("CWE-918") == "A01:2025-Broken Access Control"
    assert owasp_for_cwe("CWE-89") == "A05:2025-Injection"
    assert owasp_for_cwe("CWE-798") == "A07:2025-Authentication Failures"
    assert owasp_for_cwe("CWE-636") == "A10:2025-Mishandling of Exceptional Conditions"
    assert owasp_for_cwe("CWE-999999") == ""
    assert _infer_owasp_cwe("SSRF", "")[0] == owasp_for_cwe("CWE-918")
    assert "Top10/2025/A05_2025-Injection/" in owasp_reference(owasp_for_cwe("CWE-89"))


def test_historical_model_result_label_is_not_rewritten():
    result = parse(json.dumps({"issues": [{"issue_type": "安全漏洞", "title": "SQL 注入", "owasp": "A03:2021-Injection", "cwe": "CWE-89"}]}))
    assert result.issues[0].owasp == "A03:2021-Injection"
    assert owasp_reference(result.issues[0].owasp) == "https://owasp.org/Top10/2021/"


def test_scan_prompt_contains_actual_current_categories():
    prompt = SecuritySentinelAgent()._build_audit_prompt("x = 1", "python", "test.py", 0)
    assert "A10:2025-Mishandling of Exceptional Conditions" in prompt
    assert "A03:2025-Software Supply Chain Failures" in prompt
    assert not re.search(r"A\d{2}:2021|Top10 2021", prompt)


def test_verified_cve_reference_set_and_prompt_loading():
    from app.ai.audit_knowledge_loader import _read, build_prompt_context
    data_path = Path(__file__).resolve().parents[1] / "app/constants/data/verified_advisories.json"
    data = json.loads(data_path.read_text())
    records = data["records"]
    assert len(records) == catalog_metadata()["cve_reference_count"] == 41
    assert len({row["id"] for row in records}) == 41
    assert len([row for row in records if row["id"].startswith("CVE-2026")]) == 20
    assert "CVE-2021-21381" not in {row["id"] for row in records}
    for row in records:
        assert re.fullmatch("[a-f0-9]{64}", row["source_sha256"])
        assert row["source_url"].startswith("https://")
        assert row["affected_summary"] and row["description"]
    assert "41 条" in _read("known_cves")
    assert "CVE-2026-48019" in build_prompt_context("recon")


def test_storage_inspection_is_read_only_and_detects_old_cve_copy(db):
    # 根目录和 backend 都有 scripts；全量测试可能先缓存根目录包。
    script_path = Path(__file__).resolve().parents[1] / "scripts/audit_security_knowledge_storage.py"
    spec = importlib.util.spec_from_file_location("security_storage_audit_under_test", script_path)
    assert spec is not None and spec.loader is not None
    audit = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(audit)
    from app.models.agent_governance import AgentKnowledgeChunk, AgentKnowledgeDoc
    doc = AgentKnowledgeDoc(agent_code="security_sentinel", title="内置漏洞参考", source_type="playbook", source_ref="known_cves.md", chunk_count=1)
    db.add(doc)
    db.flush()
    db.add(AgentKnowledgeChunk(doc_id=doc.id, agent_code="security_sentinel", seq=0, content="old CVE-2021-21381"))
    db.commit()
    before = db.query(AgentKnowledgeChunk).count()
    result = audit.inspect_storage(db)
    match = result["tables"]["agent_knowledge_doc"]["matches"][0]
    assert match["exact_builtin_source"] is True
    assert match["actual_chunk_count"] == 1
    assert "content" not in match
    assert not db.dirty and not db.deleted
    assert db.query(AgentKnowledgeChunk).count() == before
