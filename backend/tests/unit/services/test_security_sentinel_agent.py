"""单元测试 (v2.1): SecuritySentinelAgent 核心行为

不发起任何真实 LLM 调用,使用 monkeypatch 替换 call_json。
"""
import hashlib
import io
import json
import time
import zipfile
from unittest.mock import MagicMock

import pytest

from app.agents.base import AgentResult
from app.agents.events import AgentEventType
from app.agents.security_sentinel_agent import (
    SecuritySentinelAgent,
    _AuditChunkResult,
    _ProjectAuditPart,
    _SemanticAuditBudget,
)
from app.models.code_file import CodeFile
from app.models.project import Project
from app.models.project_member import ProjectMember
from app.models.project_source_archive import ProjectSourceArchive
from app.models.review_issue import ReviewIssue
from app.models.review_task import ReviewTask
from app.models.user import User
from app.services import project_source_service


def _make_user(role="admin", uid=1):
    return User(id=uid, role=role, status=1, username="tester")


def _make_file(file_id=1, project_id=1, content="x = 1",
               file_name="foo.py", language="python"):
    return CodeFile(
        id=file_id, project_id=project_id, file_name=file_name,
        file_path=file_name, language=language, content=content,
        line_count=content.count("\n") + 1, size_bytes=len(content),
        status="active",
    )


def _make_project(project_id=1, user_id=1, name="demo"):
    return Project(
        id=project_id, user_id=user_id, project_name=name,
        language="python", status="active",
    )


def _make_task(task_id=1, user_id=1, project_id=1):
    return ReviewTask(
        id=task_id, user_id=user_id, project_id=project_id,
        task_name="t1", review_type="security", status="success",
        total_files=1, processed_files=1,
    )


def _patch_project_source(monkeypatch, files):
    """让项目扫描测试使用与被扫描内容完全一致的真实 ZIP 证据。"""
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for file in files:
            archive.writestr(file.file_path or file.file_name, file.content or "")
    archive_bytes = out.getvalue()
    monkeypatch.setattr(
        project_source_service,
        "begin_source_archive_audit",
        lambda *_args, **_kwargs: False,
    )
    monkeypatch.setattr(
        project_source_service,
        "load_project_source_files",
        lambda *_args, **_kwargs: files,
    )
    monkeypatch.setattr(
        project_source_service,
        "build_source_archive",
        lambda *_args, **_kwargs: (archive_bytes, "project.zip"),
    )
    monkeypatch.setattr(
        project_source_service,
        "touch_source_archive_audit",
        lambda *_args, **_kwargs: False,
    )
    monkeypatch.setattr(
        project_source_service,
        "finish_source_archive_audit",
        lambda *_args, **_kwargs: False,
    )


def _persist_quarantined_project(db, *, username: str):
    owner = User(
        username=username,
        password="x",
        email=f"{username}@example.test",
        role="user",
        status=1,
    )
    db.add(owner)
    db.flush()
    project = Project(
        user_id=owner.id,
        project_name=f"{username}-project",
        language="php",
        status="active",
    )
    db.add(project)
    db.flush()
    members = {"src/main.php": b"<?php echo $_GET['name'];\n"}
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path, content in members.items():
            archive.writestr(path, content)
    raw = output.getvalue()
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        infos = archive.infolist()
    row = ProjectSourceArchive(
        project_id=project.id,
        owner_id=owner.id,
        original_filename="original-source.zip",
        media_type="application/zip",
        archive_sha256=hashlib.sha256(raw).hexdigest(),
        compressed_size=len(raw),
        expanded_size=sum(info.file_size for info in infos),
        file_count=len(infos),
        max_member_size=max(info.file_size for info in infos),
        max_compression_ratio=max(info.file_size / max(1, info.compress_size) for info in infos),
        storage_status="active",
        malware_status="clean",
        audit_status="not_started",
        threat_count=0,
        scan_summary_json="{}",
        archive_blob=raw,
    )
    db.add(row)
    db.commit()
    return owner, project, row, raw


def _patch_successful_static_full_scan(agent, monkeypatch) -> None:
    monkeypatch.setattr(agent, "_emit", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(agent, "_regex_findings", lambda _file: [])
    monkeypatch.setattr(agent, "_static_findings", lambda _file: [])
    monkeypatch.setattr(agent, "_extract_api_endpoints", lambda _file: [])
    monkeypatch.setattr(
        agent,
        "_llm_project_audit_batch",
        lambda _parts, ctx=None, budget=None: _AuditChunkResult(),
    )


# ---------- 元数据与 checklist ----------


def test_agent_metadata_fields_are_set():
    agent = SecuritySentinelAgent()
    assert agent.name == "security_sentinel"
    assert agent.category == "security"
    assert agent.icon == "security_sentinel"
    assert "OWASP Top10" in agent.skills


def test_get_checklist_contains_owasp_and_secret_patterns():
    agent = SecuritySentinelAgent()
    data = agent.get_checklist()
    assert len(data["owasp_top10"]) == 10
    assert any(item["code"] == "A03" for item in data["owasp_top10"])
    assert len(data["secret_patterns"]) >= 10


# ---------- _ensure_db / 鉴权 ----------


def test_scan_file_without_db_returns_error():
    agent = SecuritySentinelAgent()
    result = agent.scan_file(file_id=1)
    assert result.success is False
    assert "DB" in (result.error or "")


def test_scan_file_authz_rejects_non_owner():
    agent = SecuritySentinelAgent()
    db = MagicMock()
    file = _make_file(file_id=1, project_id=99)
    project = _make_project(project_id=99, user_id=999)
    db.get.side_effect = lambda model, _id: {
        CodeFile: file,
        Project: project,
    }.get(model)
    member_query = MagicMock()
    member_query.filter.return_value.first.return_value = None
    db.query.return_value = member_query
    user = _make_user(role="user", uid=42)
    agent.inject(db, user=user)
    result = agent.scan_file(file_id=1)
    assert result.success is False
    assert "无权" in result.error


# ---------- 单文件正则路径 ----------


def test_scan_file_regex_secret_detected_without_llm(monkeypatch):
    """正则路径独立工作: 即使 LLM 失败也能返回 finding"""
    agent = SecuritySentinelAgent()

    db = MagicMock()
    content = 'OPENAI_KEY = "sk-proj-AbCdEf1234567890XyZqWeRtYu"\n'
    file = _make_file(content=content)
    db.get.return_value = file
    agent.inject(db, user=_make_user())

    # mock LLM 失败
    monkeypatch.setattr(
        agent, "call_json",
        lambda *a, **kw: AgentResult(success=False, error="mocked"),
    )

    result = agent.scan_file(file_id=1, scan_depth="standard")
    assert result.success is True
    findings = result.data["findings"]
    assert any(f["source"] == "regex" for f in findings)
    secret = next(f for f in findings if f["source"] == "regex")
    assert secret["severity"] == "严重"
    assert secret["cwe"] == "CWE-798"
    # evidence 必须脱敏
    assert "sk-proj-AbCdEf1234567890XyZqWeRtYu" not in secret["evidence"]


def test_scan_file_llm_finding_integrates_offset(monkeypatch):
    """LLM 返回相对行号,应正确换算 (单 chunk 内 offset=0)"""
    agent = SecuritySentinelAgent()

    db = MagicMock()
    file = _make_file(content="def f():\n    return 1\n")
    db.get.return_value = file
    agent.inject(db, user=_make_user())

    monkeypatch.setattr(agent, "call_json", lambda *a, **kw: AgentResult(
        success=True,
        data={
            "findings": [{
                "title": "SQL 注入",
                "category": "注入",
                "owasp": "A03:2021-Injection",
                "cwe": "CWE-89",
                "severity": "高",
                "line_start": 2,
                "line_end": 2,
                "evidence": "query = f\"...\"",
                "exploit_scenario": "字符串拼接构造 SQL",
                "fix_suggestion": "参数化查询",
                "references": ["https://owasp.org/x"],
                "confidence": 0.9,
            }],
            "entry_points": [],
            "dangerous_sinks": [],
        },
    ))

    result = agent.scan_file(file_id=1)
    assert result.success is True
    findings = [f for f in result.data["findings"] if f["source"] == "llm"]
    assert len(findings) == 1
    assert findings[0]["owasp"] == "A03:2021-Injection"
    assert findings[0]["cwe"] == "CWE-89"
    assert findings[0]["line_number"] == 2


def test_scan_file_invalid_depth_returns_error():
    agent = SecuritySentinelAgent()
    db = MagicMock()
    agent.inject(db, user=_make_user())
    result = agent.scan_file(file_id=1, scan_depth="ultra")
    assert result.success is False
    assert "scan_depth" in result.error


def test_scan_project_full_batches_multiple_files_in_one_semantic_request(monkeypatch):
    """full 模式应把同一项目的小文件合并审计，而不是逐文件调用模型。"""
    agent = SecuritySentinelAgent()
    db = MagicMock()
    project = _make_project()
    files = [
        _make_file(file_id=index, content=f"def f{index}():\n    return {index}\n", file_name=f"src/f{index}.py")
        for index in range(1, 4)
    ]
    db.get.return_value = project
    query = MagicMock()
    query.filter.return_value.all.return_value = files
    db.query.return_value = query
    agent.inject(db, user=_make_user())
    _patch_project_source(monkeypatch, files)
    monkeypatch.setattr(agent, "_regex_findings", lambda _file: [])
    monkeypatch.setattr(agent, "_static_findings", lambda _file: [])
    monkeypatch.setattr(agent, "_extract_api_endpoints", lambda _file: [])
    batches = []

    def fake_batch(parts, ctx=None, budget=None):
        batches.append(parts)
        return _AuditChunkResult()

    monkeypatch.setattr(agent, "_llm_project_audit_batch", fake_batch)

    result = agent.scan_project(project.id, trace_dataflow=False)

    assert result.success is True
    assert len(batches) == 1
    assert {part.file.id for part in batches[0]} == {1, 2, 3}
    assert result.data["compliance"]["semantic_file_count"] == 3
    assert result.data["compliance"]["semantic_batch_count"] == 1
    assert result.data["compliance"]["semantic_complete"] is True


@pytest.mark.parametrize(
    "payload",
    [
        [],
        {},
        {"findings": "bad", "entry_points": [], "dangerous_sinks": []},
        {"findings": ["bad"], "entry_points": [], "dangerous_sinks": []},
        {
            "findings": [{"file_path": "src/app.py"}],
            "entry_points": [],
            "dangerous_sinks": [],
        },
    ],
)
def test_project_audit_batch_rejects_invalid_json_schema(monkeypatch, payload):
    agent = SecuritySentinelAgent()
    file = _make_file(content="print('ok')\n", file_name="src/app.py")
    parts = list(agent._semantic_parts_for_file(file, len(file.content)))
    monkeypatch.setattr(
        agent,
        "call_json",
        lambda *_args, **_kwargs: AgentResult(success=True, data=payload),
    )

    result = agent._llm_project_audit_batch(parts, ctx=None)

    assert result.success is False
    assert result.failure_kind == "invalid_schema"


def test_project_audit_batch_accepts_explicit_empty_lists(monkeypatch):
    agent = SecuritySentinelAgent()
    file = _make_file(content="print('ok')\n", file_name="src/app.py")
    parts = list(agent._semantic_parts_for_file(file, len(file.content)))
    call_kwargs = []

    def return_empty_result(*_args, **kwargs):
        call_kwargs.append(kwargs)
        return AgentResult(
            success=True,
            data={
                "output_limited": False,
                "findings": [],
                "entry_points": [],
                "dangerous_sinks": [],
            },
        )

    monkeypatch.setattr(agent, "call_json", return_empty_result)

    result = agent._llm_project_audit_batch(parts, ctx=None)

    assert result.success is True
    assert result.findings == []
    assert call_kwargs[0]["thinking"] is False


def test_project_audit_batch_marks_explicit_or_saturated_output_for_split(monkeypatch):
    agent = SecuritySentinelAgent()
    file = _make_file(content="print('ok')\n", file_name="src/app.py")
    parts = list(agent._semantic_parts_for_file(file, len(file.content)))
    captured_prompts = []

    def explicit_limited(prompt, *args, **kwargs):
        captured_prompts.append(prompt)
        return AgentResult(
            success=True,
            data={
                "output_limited": True,
                "findings": [],
                "entry_points": [],
                "dangerous_sinks": [],
            },
        )

    monkeypatch.setattr(agent, "call_json", explicit_limited)
    result = agent._llm_project_audit_batch(parts, ctx=None)
    assert result.success is False
    assert result.failure_kind == "output_limited"
    assert '"output_limited":false' in captured_prompts[0]

    monkeypatch.setattr(
        agent,
        "call_json",
        lambda *_args, **_kwargs: AgentResult(
            success=True,
            data={
                "output_limited": False,
                "findings": [],
                "entry_points": [
                    {"file_path": "src/app.py", "name": "entry", "line": 1}
                    for _ in range(20)
                ],
                "dangerous_sinks": [],
            },
        ),
    )
    saturated = agent._llm_project_audit_batch(parts, ctx=None)
    assert saturated.success is False
    assert saturated.failure_kind == "output_limited"

    monkeypatch.setattr(
        agent,
        "call_json",
        lambda *_args, **_kwargs: AgentResult(
            success=True,
            data={
                "output_limited": False,
                "findings": [],
                "entry_points": [
                    {"file_path": "src/app.py", "name": "entry", "line": 1}
                    for _ in range(21)
                ],
                "dangerous_sinks": [],
            },
        ),
    )
    over_saturated = agent._llm_project_audit_batch(parts, ctx=None)
    assert over_saturated.success is False
    assert over_saturated.failure_kind == "output_limited"


def test_project_audit_batch_rejects_lines_and_evidence_outside_current_leaf(monkeypatch):
    agent = SecuritySentinelAgent()
    file = _make_file(
        content="first = 1\nsecond = danger()\nthird = 3\n",
        file_name="src/app.py",
    )
    scoped_part = _ProjectAuditPart(file=file, text="second = danger()\n", start_line=1)

    def payload(
        *, line_start, line_end, evidence, entry_line=2,
        entry_name="danger", entry_evidence="second = danger()",
    ):
        return {
            "output_limited": False,
            "findings": [{
                "file_path": "src/app.py",
                "title": "危险调用",
                "severity": "高",
                "line_start": line_start,
                "line_end": line_end,
                "evidence": evidence,
            }],
            "entry_points": [{
                "file_path": "src/app.py",
                "name": entry_name,
                "line": entry_line,
                "evidence": entry_evidence,
            }],
            "dangerous_sinks": [],
        }

    for invalid in (
        payload(line_start=1, line_end=1, evidence="first = 1"),
        payload(line_start=2, line_end=2, evidence="third = 3"),
        payload(
            line_start=2,
            line_end=2,
            evidence="second = danger()",
            entry_evidence="missing_entry()",
        ),
    ):
        monkeypatch.setattr(
            agent,
            "call_json",
            lambda *_args, _payload=invalid, **_kwargs: AgentResult(
                success=True,
                data=_payload,
            ),
        )
        result = agent._llm_project_audit_batch([scoped_part], ctx=None)
        assert result.success is False
        assert result.failure_kind == "invalid_item"
        assert result.invalid_item_count == 1

    monkeypatch.setattr(
        agent,
        "call_json",
        lambda *_args, **_kwargs: AgentResult(
            success=True,
            data=payload(
                line_start=2,
                line_end=2,
                evidence="second = danger()",
            ),
        ),
    )
    valid = agent._llm_project_audit_batch([scoped_part], ctx=None)
    assert valid.success is True
    assert len(valid.findings) == 1
    assert valid.findings[0]["line_number"] == 2
    assert valid.entry_points[0]["line"] == 2

    full_part = _ProjectAuditPart(file=file, text=file.content, start_line=0)
    monkeypatch.setattr(
        agent,
        "call_json",
        lambda *_args, **_kwargs: AgentResult(
            success=True,
            data=payload(
                line_start=1,
                line_end=1,
                evidence="second = danger()",
            ),
        ),
    )
    mismatched_line = agent._llm_project_audit_batch([full_part], ctx=None)
    assert mismatched_line.success is True
    assert mismatched_line.findings[0]["line_number"] == 2


def test_project_audit_batch_recovers_model_line_drift_from_exact_evidence(monkeypatch):
    agent = SecuritySentinelAgent()
    file = _make_file(
        content=(
            "<?php\r\n"
            "function update($input) {\r\n"
            "    $sql = \"UPDATE users SET name='\".$input.\"'\";\r\n"
            "    return exequery($connection, $sql);\r\n"
            "}\r\n"
        ),
        file_name="api/archive.php",
    )
    part = _ProjectAuditPart(file=file, text=file.content, start_line=0)
    evidence = "$sql = \"UPDATE users SET name='\".$input.\"'\";"
    monkeypatch.setattr(
        agent,
        "call_json",
        lambda *_args, **_kwargs: AgentResult(
            success=True,
            data={
                "output_limited": False,
                "findings": [{
                    "file_path": "api/archive.php",
                    "title": "SQL 注入",
                    "severity": "高",
                    "line_start": 99,
                    "line_end": 99,
                    "evidence": evidence,
                }],
                "entry_points": [{
                    "file_path": "api/archive.php",
                    "name": "update",
                    "line": 77,
                    "evidence": "function update($input) {",
                }],
                "dangerous_sinks": [{
                    "file_path": "api/archive.php",
                    "name": "exequery",
                    "line": 88,
                    "evidence": "return exequery($connection, $sql);",
                }],
            },
        ),
    )

    result = agent._llm_project_audit_batch([part], ctx=None)

    assert result.success is True
    assert result.findings[0]["line_number"] == 3
    assert result.findings[0]["end_line"] == 3
    assert result.entry_points[0]["line"] == 2
    assert result.dangerous_sinks[0]["line"] == 4


def test_project_audit_batch_accepts_lf_evidence_for_crlf_multiline_source(monkeypatch):
    """模型 JSON 常把 CRLF 证据序列化成 LF，但证据仍必须逐字匹配源码。"""
    agent = SecuritySentinelAgent()
    file = _make_file(
        content=(
            "<?php\r\n"
            "function handle($input) {\r\n"
            "    system($_GET['cmd']);\r\n"
            "}\r\n"
        ),
        file_name="api/handler.php",
        language="php",
    )
    part = _ProjectAuditPart(file=file, text=file.content, start_line=0)
    monkeypatch.setattr(
        agent,
        "call_json",
        lambda *_args, **_kwargs: AgentResult(
            success=True,
            data={
                "output_limited": False,
                "findings": [{
                    "file_path": "api/handler.php",
                    "title": "命令执行",
                    "severity": "严重",
                    "line_start": 99,
                    "line_end": 99,
                    "evidence": "function handle($input) {\n    system($_GET['cmd']);",
                }],
                "entry_points": [{
                    "file_path": "api/handler.php",
                    "name": "handle",
                    "line": 77,
                    "evidence": "function handle($input) {\n    system($_GET['cmd']);",
                }],
                "dangerous_sinks": [{
                    "file_path": "api/handler.php",
                    "name": "system",
                    "line": 88,
                    "evidence": "    system($_GET['cmd']);\n}",
                    "sink_type": "exec",
                }],
            },
        ),
    )

    result = agent._llm_project_audit_batch([part], ctx=None)

    assert result.success is True
    assert result.findings[0]["line_number"] == 2
    assert result.findings[0]["end_line"] == 3
    assert result.entry_points[0]["line"] == 2
    assert result.dangerous_sinks[0]["line"] == 3


def test_project_audit_batch_does_not_join_non_contiguous_source_parts(monkeypatch):
    """证据不能借助当前请求中同文件但不连续的叶片拼接伪造。"""
    agent = SecuritySentinelAgent()
    file = _make_file(
        content="first();\r\nmissing();\r\nthird();\r\n",
        file_name="src/gap.php",
        language="php",
    )
    parts = [
        _ProjectAuditPart(file=file, text="first();\r\n", start_line=0),
        _ProjectAuditPart(file=file, text="third();\r\n", start_line=2),
    ]
    monkeypatch.setattr(
        agent,
        "call_json",
        lambda *_args, **_kwargs: AgentResult(
            success=True,
            data={
                "output_limited": False,
                "findings": [{
                    "file_path": "src/gap.php",
                    "title": "伪造问题",
                    "severity": "高",
                    "line_start": 1,
                    "line_end": 2,
                    "evidence": "first();\nthird();",
                }],
                "entry_points": [],
                "dangerous_sinks": [],
            },
        ),
    )

    result = agent._llm_project_audit_batch(parts, ctx=None)

    assert result.success is False
    assert result.failure_kind == "invalid_item"
    assert result.findings == []
    assert result.invalid_item_count == 1


def test_project_audit_batch_rejects_evidence_that_cannot_be_relocated(monkeypatch):
    agent = SecuritySentinelAgent()
    file = _make_file(content="safe();\n", file_name="src/safe.php")
    part = _ProjectAuditPart(file=file, text=file.content, start_line=0)
    monkeypatch.setattr(
        agent,
        "call_json",
        lambda *_args, **_kwargs: AgentResult(
            success=True,
            data={
                "output_limited": False,
                "findings": [{
                    "file_path": "src/safe.php",
                    "title": "伪造问题",
                    "severity": "高",
                    "line_start": 1,
                    "line_end": 1,
                    "evidence": "system($_GET['cmd']);",
                }],
                "entry_points": [],
                "dangerous_sinks": [],
            },
        ),
    )

    result = agent._llm_project_audit_batch([part], ctx=None)

    assert result.success is False
    assert result.failure_kind == "invalid_item"
    assert result.findings == []
    assert result.invalid_item_count == 1


def test_adaptive_project_audit_repairs_invalid_item_before_splitting(monkeypatch):
    agent = SecuritySentinelAgent()
    file = _make_file(content="safe();\n", file_name="src/safe.php")
    part = _ProjectAuditPart(file=file, text=file.content, start_line=0)
    responses = [
        {
            "output_limited": False,
            "findings": [{
                "file_path": "src/safe.php",
                "title": "伪造问题",
                "severity": "高",
                "line_start": 1,
                "line_end": 1,
                "evidence": "missing();",
            }],
            "entry_points": [],
            "dangerous_sinks": [],
        },
        {
            "output_limited": False,
            "findings": [],
            "entry_points": [],
            "dangerous_sinks": [],
        },
    ]
    prompts = []

    def return_response(prompt, *args, **kwargs):
        prompts.append(prompt)
        return AgentResult(success=True, data=responses.pop(0))

    monkeypatch.setattr(agent, "call_json", return_response)
    budget = _SemanticAuditBudget(max_requests=4, deadline=time.monotonic() + 30)
    result = agent._audit_project_batch_resilient([part], ctx=None, budget=budget)

    assert result.request_count == 2
    assert result.split_count == 0
    assert len(result.leaves) == 1
    assert result.leaves[0][1].success is True
    assert prompts[1].find("契约修复") >= 0


def test_adaptive_split_preserves_unicode_crlf_long_lines_and_line_offsets(monkeypatch):
    agent = SecuritySentinelAgent()
    files = [
        _make_file(file_id=1, content="alpha\r\n中文\n尾行\n", file_name="src/a.php"),
        _make_file(file_id=2, content="长" * 9_000, file_name="src/b.php"),
    ]
    original_parts = [
        _ProjectAuditPart(files[0], files[0].content, 0),
        _ProjectAuditPart(files[1], files[1].content, 0),
    ]
    terminal_parts = []

    def recursively_split(parts):
        if sum(len(part.text) for part in parts) <= 700:
            terminal_parts.extend(parts)
            return
        split = agent._split_project_audit_parts(parts)
        assert split is not None
        recursively_split(split[0])
        recursively_split(split[1])

    recursively_split(original_parts)
    reconstructed = {file.id: "" for file in files}
    next_line = {file.id: 0 for file in files}
    for part in terminal_parts:
        assert part.start_line == next_line[part.file.id]
        reconstructed[part.file.id] += part.text
        next_line[part.file.id] += part.text.count("\n")
    assert reconstructed == {file.id: file.content for file in files}


def test_adaptive_split_counts_parent_and_children_without_duplicate_chars(monkeypatch):
    agent = SecuritySentinelAgent()
    file = _make_file(content=("line\n" * 1_600), file_name="src/app.php")
    parts = [_ProjectAuditPart(file, file.content, 0)]

    monkeypatch.setattr(
        agent,
        "_llm_project_audit_batch",
        lambda batch, ctx=None, budget=None: _AuditChunkResult(
            success=sum(len(part.text) for part in batch) <= 4_000,
            failure_kind=(
                "" if sum(len(part.text) for part in batch) <= 4_000 else "output_truncated"
            ),
        ),
    )
    budget = _SemanticAuditBudget(max_requests=10, deadline=10**12)
    result = agent._audit_project_batch_resilient(parts, ctx=None, budget=budget)

    assert result.request_count == 3
    assert result.split_count == 1
    assert all(chunk.success for _, chunk in result.leaves)
    assert "".join(part.text for leaf, _chunk in result.leaves for part in leaf) == file.content


@pytest.mark.parametrize("failure_kind", ["invalid_schema", "invalid_json"])
def test_adaptive_split_recovers_contract_failure_without_duplicate_ledger(
    monkeypatch,
    failure_kind,
):
    agent = SecuritySentinelAgent()
    file = _make_file(content=("line\n" * 1_600), file_name="src/contract.php")
    parts = [_ProjectAuditPart(file, file.content, 0)]
    requested_chars = []

    def audit_batch(batch, ctx=None, budget=None):
        source_chars = sum(len(part.text) for part in batch)
        requested_chars.append(source_chars)
        return _AuditChunkResult(
            success=source_chars <= 4_000,
            error="contract invalid" if source_chars > 4_000 else "",
            failure_kind=failure_kind if source_chars > 4_000 else "",
        )

    monkeypatch.setattr(agent, "_llm_project_audit_batch", audit_batch)
    budget = _SemanticAuditBudget(max_requests=10, deadline=10**12)

    result = agent._audit_project_batch_resilient(parts, ctx=None, budget=budget)

    assert requested_chars == [len(file.content), len(file.content) // 2, len(file.content) // 2]
    assert result.request_count == budget.request_count == len(requested_chars) == 3
    assert result.split_count == 1
    assert all(chunk.success for _, chunk in result.leaves)
    terminal_chars = sum(
        len(part.text)
        for leaf_parts, _chunk in result.leaves
        for part in leaf_parts
    )
    assert terminal_chars == len(file.content)
    assert "".join(
        part.text
        for leaf_parts, _chunk in result.leaves
        for part in leaf_parts
    ) == file.content


def test_adaptive_split_stops_when_shared_request_budget_is_exhausted(monkeypatch):
    agent = SecuritySentinelAgent()
    file = _make_file(content="x" * 6_000, file_name="src/app.php")
    parts = [_ProjectAuditPart(file, file.content, 0)]

    monkeypatch.setattr(
        agent,
        "_llm_project_audit_batch",
        lambda batch, ctx=None, budget=None: _AuditChunkResult(
            success=sum(len(part.text) for part in batch) <= 3_000,
            failure_kind=(
                "" if sum(len(part.text) for part in batch) <= 3_000 else "output_limited"
            ),
        ),
    )
    budget = _SemanticAuditBudget(max_requests=2, deadline=10**12)
    result = agent._audit_project_batch_resilient(parts, ctx=None, budget=budget)

    assert result.request_count == 2
    assert budget.request_count == 2
    assert len(result.leaves) == 2
    assert result.leaves[0][1].success is True
    assert result.leaves[1][1].failure_kind == "semantic_budget_exhausted"
    assert "".join(part.text for leaf, _chunk in result.leaves for part in leaf) == file.content


def test_quarantined_scan_success_persists_result_bound_to_original_zip_sha(db, monkeypatch):
    owner, project, archive_row, raw = _persist_quarantined_project(
        db,
        username="archive_audit_success",
    )
    expected_sha256 = hashlib.sha256(raw).hexdigest()
    agent = SecuritySentinelAgent()
    agent.inject(db, user=owner)
    _patch_successful_static_full_scan(agent, monkeypatch)

    result = agent.scan_project(
        project.id,
        trace_dataflow=False,
        scan_mode="static_full",
    )

    assert result.success is True
    assert result.data["source_archive_sha256"] == expected_sha256
    assert result.data["source_archive_filename"] == "original-source.zip"
    assert result.data["compliance"]["semantic_bounded_total_chars"] == 32_000
    assert result.data["compliance"]["semantic_bounded_per_file_chars"] == 8_000
    assert result.data["compliance"]["semantic_bounded_max_files"] == 12
    assert result.data["compliance"]["semantic_candidate_source_chars"] > 0
    assert result.data["compliance"]["semantic_planned_file_count"] == 1
    assert result.data["compliance"]["archive_text_file_count"] == 1
    assert result.data["compliance"]["archive_semantic_complete"] is True
    assert result.data["compliance"]["semantic_verified_file_count"] == 1
    assert result.data["compliance"]["semantic_request_headroom"] == 255
    assert expected_sha256 == archive_row.archive_sha256
    db.expire_all()
    stored = db.query(ProjectSourceArchive).filter_by(project_id=project.id).one()
    stored_payload = json.loads(stored.audit_result_json)
    assert stored.audit_status == "succeeded"
    assert stored.audit_completed_at is not None
    assert stored_payload["source_archive_sha256"] == expected_sha256
    assert stored_payload["source_archive_filename"] == "original-source.zip"
    persisted = project_source_service.get_source_archive_audit_result(db, owner, project.id)
    assert persisted["status"] == "succeeded"
    assert persisted["result"]["source_archive_sha256"] == expected_sha256


def test_quarantined_scan_static_exception_persists_failed_terminal_state(db, monkeypatch):
    owner, project, _archive_row, raw = _persist_quarantined_project(
        db,
        username="archive_audit_failure",
    )
    expected_sha256 = hashlib.sha256(raw).hexdigest()
    agent = SecuritySentinelAgent()
    agent.inject(db, user=owner)
    monkeypatch.setattr(agent, "_emit", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(agent, "_regex_findings", lambda _file: [])

    def fail_static_analysis(_file):
        raise RuntimeError("deterministic static failure")

    monkeypatch.setattr(agent, "_static_findings", fail_static_analysis)
    monkeypatch.setattr(agent, "_extract_api_endpoints", lambda _file: [])

    result = agent.scan_project(
        project.id,
        trace_dataflow=False,
        scan_mode="static_full",
    )

    assert result.success is False
    assert result.error == "项目白盒审计执行失败"
    db.expire_all()
    stored = db.query(ProjectSourceArchive).filter_by(project_id=project.id).one()
    stored_payload = json.loads(stored.audit_result_json)
    assert stored.audit_status == "failed"
    assert stored.audit_status != "running"
    assert stored.audit_completed_at is not None
    assert stored_payload["error"] == "项目白盒审计执行失败"
    assert stored_payload["source_archive_sha256"] == expected_sha256


def test_project_reviewer_can_run_read_only_quarantined_scan_project(db, monkeypatch):
    owner, project, _archive_row, _raw = _persist_quarantined_project(
        db,
        username="archive_audit_owner",
    )
    reviewer = User(
        username="archive_audit_reviewer",
        password="x",
        email="archive_audit_reviewer@example.test",
        role="user",
        status=1,
    )
    db.add(reviewer)
    db.flush()
    db.add(
        ProjectMember(
            project_id=project.id,
            user_id=reviewer.id,
            role_in_project="reviewer",
        )
    )
    db.commit()
    assert owner.id != reviewer.id
    agent = SecuritySentinelAgent()
    agent.inject(db, user=reviewer)
    _patch_successful_static_full_scan(agent, monkeypatch)

    result = agent.scan_project(
        project.id,
        trace_dataflow=False,
        scan_mode="static_full",
    )

    assert result.success is True
    assert result.data["file_count"] == 1
    db.expire_all()
    stored = db.query(ProjectSourceArchive).filter_by(project_id=project.id).one()
    assert stored.audit_status == "succeeded"


def test_project_audit_batches_full_stream_every_character_without_legacy_budgets():
    """full 模式必须流式覆盖超过旧单文件和总字符预算的全部源码。"""
    agent = SecuritySentinelAgent()
    files = [
        _make_file(
            file_id=index,
            content=(f"file-{index}:" + (chr(64 + index) * 250_000)),
            file_name=f"src/f{index:02d}.php",
            language="php",
        )
        for index in range(1, 12)
    ]

    batches = agent._project_audit_batches(files)
    assert iter(batches) is batches
    reconstructed: dict[int, list[str]] = {file.id: [] for file in files}
    batch_count = 0
    for batch in batches:
        batch_count += 1
        for part in batch:
            reconstructed[part.file.id].append(part.text)

    assert batch_count > 10
    assert set(reconstructed) == {file.id for file in files}
    for file in files:
        assert "".join(reconstructed[file.id]) == file.content


def test_project_audit_batches_bounded_scope_matches_config(monkeypatch):
    """有界模式必须跨多个风险文件组批，并严格遵守总量与单文件预算。"""
    from app.core.config import settings

    agent = SecuritySentinelAgent()
    files = [
        _make_file(
            file_id=index,
            content="<?php system($_GET['cmd']);\n" + ("x" * 8_000),
            file_name=f"src/risk-{index}.php",
            language="php",
        )
        for index in range(1, 7)
    ]
    monkeypatch.setattr(settings, "security_semantic_bounded_total_chars", 12_000)
    monkeypatch.setattr(settings, "security_semantic_bounded_per_file_chars", 3_000)
    monkeypatch.setattr(settings, "security_semantic_bounded_max_files", 4)

    batches = list(agent._project_audit_batches(files, full_content=False))
    parts = [part for batch in batches for part in batch]

    assert len(batches) == 1
    assert sum(len(part.text) for part in parts) == 12_000
    assert len({part.file.id for part in parts}) == 4
    assert all(len(part.text) == 3_000 for part in parts)


def test_project_audit_batches_bounded_scope_caps_many_small_files(monkeypatch):
    """有界模式不得因小文件数量多而突破文件数和初始批次上限。"""
    from app.core.config import settings

    agent = SecuritySentinelAgent()
    files = [
        _make_file(
            file_id=index,
            content="<?php system($_GET['cmd']);\n" + ("x" * 900),
            file_name=f"src/small-risk-{index:02d}.php",
            language="php",
        )
        for index in range(1, 21)
    ]
    monkeypatch.setattr(settings, "security_semantic_bounded_total_chars", 12_000)
    monkeypatch.setattr(settings, "security_semantic_bounded_per_file_chars", 3_000)
    monkeypatch.setattr(settings, "security_semantic_bounded_max_files", 4)

    batches = list(agent._project_audit_batches(files, full_content=False))
    parts = [part for batch in batches for part in batch]

    assert len(batches) == 1
    assert len({part.file.id for part in parts}) == 4
    assert sum(len(part.text) for part in parts) < 12_000


def test_project_audit_batches_keeps_long_paths_in_one_bounded_batch(monkeypatch):
    """保守的路径开销预算下，长路径仍只能生成一个初始批次。"""
    from app.core.config import settings

    agent = SecuritySentinelAgent()
    files = [
        _make_file(
            file_id=index,
            content="x" * 4_000,
            file_name=("nested/" + ("p" * 2_000) + f"-{index}.php"),
            language="php",
        )
        for index in range(1, 5)
    ]
    monkeypatch.setattr(settings, "security_semantic_bounded_total_chars", 16_000)
    monkeypatch.setattr(settings, "security_semantic_bounded_per_file_chars", 4_000)
    monkeypatch.setattr(settings, "security_semantic_bounded_max_files", 4)

    batches = list(agent._project_audit_batches(files, full_content=False))

    assert len(batches) == 1
    assert len({part.file.id for part in batches[0]}) == 4
    assert sum(len(part.text) for part in batches[0]) == 16_000


def test_scan_project_bounded_candidate_pool_refills_binary_and_empty_slots(monkeypatch):
    """候选池应先排除二进制与空文件，再执行 top_n 截断。"""
    agent = SecuritySentinelAgent()
    db = MagicMock()
    project = _make_project()
    empty = _make_file(file_id=1, content="", file_name="admin-empty.php", language="php")
    binary = _make_file(
        file_id=2,
        content="binary",
        file_name="auth-binary.php",
        language="php",
    )
    binary.is_binary = True
    live_files = [
        _make_file(
            file_id=3,
            content="<?php system($_GET['cmd']);",
            file_name="controller-live.php",
            language="php",
        ),
        _make_file(
            file_id=4,
            content="<?php echo $_POST['name'];",
            file_name="route-live.php",
            language="php",
        ),
    ]
    files = [empty, binary, *live_files]
    db.get.return_value = project
    agent.inject(db, user=_make_user())
    _patch_project_source(monkeypatch, files)
    _patch_successful_static_full_scan(agent, monkeypatch)
    scheduled_ids: set[int] = set()

    def capture_batch(parts, ctx=None, budget=None):
        scheduled_ids.update(part.file.id for part in parts)
        return _AuditChunkResult()

    monkeypatch.setattr(agent, "_llm_project_audit_batch", capture_batch)

    result = agent.scan_project(
        project.id,
        top_n=2,
        trace_dataflow=False,
        scan_mode="static_full",
    )

    assert result.success is True
    assert scheduled_ids == {3, 4}
    compliance = result.data["compliance"]
    assert compliance["semantic_candidate_file_count"] == 2
    assert compliance["semantic_planned_file_count"] == 2
    assert compliance["archive_binary_or_empty_file_count"] == 2
    assert compliance["candidate_binary_or_empty_file_count"] == 0


def test_scan_project_bounded_metrics_separate_candidate_plan_success_and_archive(monkeypatch):
    """有界扫描必须区分候选池、调度窗口、执行成功和归档全量。"""
    agent = SecuritySentinelAgent()
    db = MagicMock()
    project = _make_project()
    files = [
        _make_file(
            file_id=index,
            content="<?php system($_GET['cmd']);\n" + ("x" * 7_970),
            file_name=f"src/controller-{index}.php",
            language="php",
        )
        for index in range(1, 7)
    ]
    db.get.return_value = project
    agent.inject(db, user=_make_user())
    _patch_project_source(monkeypatch, files)
    _patch_successful_static_full_scan(agent, monkeypatch)

    result = agent.scan_project(
        project.id,
        top_n=6,
        trace_dataflow=False,
        scan_mode="static_full",
    )

    assert result.success is True
    compliance = result.data["compliance"]
    assert compliance["semantic_candidate_file_count"] == 6
    assert compliance["semantic_planned_file_count"] == 5
    assert compliance["semantic_file_count"] == 5
    assert compliance["archive_text_file_count"] == 6
    assert compliance["semantic_planned_source_chars"] == 32_000
    assert compliance["semantic_source_chars"] == 32_000
    assert compliance["semantic_unscheduled_file_count"] == 1
    assert compliance["semantic_partially_scheduled_file_count"] == 1
    assert compliance["semantic_verified_file_count"] == 4
    assert compliance["semantic_failed_file_count"] == 0
    assert compliance["semantic_scope_execution_complete"] is True
    assert compliance["archive_semantic_complete"] is False
    assert compliance["semantic_request_headroom"] == 255


def test_prioritize_files_uses_stable_path_and_id_tie_breakers():
    agent = SecuritySentinelAgent()
    files = [
        _make_file(file_id=3, file_name="src/controller-c.php"),
        _make_file(file_id=2, file_name="src/controller-b.php"),
        _make_file(file_id=1, file_name="src/controller-a.php"),
    ]

    ordered = agent._prioritize_files(list(reversed(files)))

    assert [file.id for file in ordered] == [1, 2, 3]


def test_scan_project_full_fails_instead_of_claiming_completion_when_semantic_batch_fails(monkeypatch):
    """任一语义批次失败时，full 审计不得返回成功结论。"""
    agent = SecuritySentinelAgent()
    db = MagicMock()
    project = _make_project()
    file = _make_file(content="<?php echo $_GET['name'];", language="php")
    db.get.return_value = project
    query = MagicMock()
    query.filter.return_value.all.return_value = [file]
    db.query.return_value = query
    agent.inject(db, user=_make_user())
    _patch_project_source(monkeypatch, [file])
    monkeypatch.setattr(agent, "_regex_findings", lambda _file: [])
    monkeypatch.setattr(agent, "_static_findings", lambda _file: [])
    monkeypatch.setattr(agent, "_extract_api_endpoints", lambda _file: [])
    monkeypatch.setattr(
        agent,
        "_llm_project_audit_batch",
        lambda parts, ctx=None, budget=None: _AuditChunkResult(
            success=False,
            error="upstream failed",
        ),
    )

    result = agent.scan_project(project.id, trace_dataflow=False)

    assert result.success is False
    assert "项目语义审计执行未完成" in (result.error or "")
    assert result.data["compliance"]["semantic_complete"] is False
    assert result.data["compliance"]["semantic_failed_batch_count"] == 1
    assert result.data["compliance"]["semantic_source_chars"] == 0
    assert result.data["compliance"]["semantic_failed_source_chars"] == len(file.content)
    assert result.data["compliance"]["semantic_accounting_complete"] is True
    assert result.data["compliance"]["semantic_successful_batch_count"] == 0


def test_scan_project_recovered_invalid_contract_counts_only_terminal_leaves(monkeypatch):
    agent = SecuritySentinelAgent()
    db = MagicMock()
    project = _make_project()
    file = _make_file(
        content="line\n" * 1_600,
        file_name="src/recovered-contract.php",
        language="php",
    )
    db.get.return_value = project
    agent.inject(db, user=_make_user())
    _patch_project_source(monkeypatch, [file])
    monkeypatch.setattr(agent, "_regex_findings", lambda _file: [])
    monkeypatch.setattr(agent, "_static_findings", lambda _file: [])
    monkeypatch.setattr(agent, "_extract_api_endpoints", lambda _file: [])
    model_requests = []

    def recover_after_split(parts, ctx=None, budget=None):
        source_chars = sum(len(part.text) for part in parts)
        model_requests.append(source_chars)
        if source_chars > 4_000:
            return _AuditChunkResult(
                success=False,
                error="invalid project audit contract",
                failure_kind="invalid_schema",
            )
        return _AuditChunkResult()

    monkeypatch.setattr(agent, "_llm_project_audit_batch", recover_after_split)

    result = agent.scan_project(project.id, trace_dataflow=False, scan_mode="full")

    assert result.success is True
    assert model_requests == [len(file.content), len(file.content) // 2, len(file.content) // 2]
    compliance = result.data["compliance"]
    assert compliance["semantic_initial_batch_count"] == 1
    assert compliance["semantic_batch_count"] == 2
    assert compliance["semantic_request_count"] == 3
    assert compliance["semantic_split_count"] == 1
    assert compliance["semantic_successful_batch_count"] == 2
    assert compliance["semantic_failed_batch_count"] == 0
    assert compliance["semantic_invalid_contract_leaf_count"] == 0
    assert compliance["semantic_attempted_source_chars"] == len(file.content)
    assert compliance["semantic_source_chars"] == len(file.content)
    assert compliance["semantic_failed_source_chars"] == 0
    assert compliance["semantic_accounted_source_chars"] == len(file.content)
    assert compliance["semantic_accounting_complete"] is True
    assert compliance["semantic_request_accounting_complete"] is True


def test_scan_project_minimum_invalid_contract_leaf_fails_closed_with_exact_ledger(
    monkeypatch,
):
    agent = SecuritySentinelAgent()
    db = MagicMock()
    project = _make_project()
    file = _make_file(
        content="x" * 1_500,
        file_name="src/invalid-contract.php",
        language="php",
    )
    db.get.return_value = project
    agent.inject(db, user=_make_user())
    _patch_project_source(monkeypatch, [file])
    monkeypatch.setattr(agent, "_regex_findings", lambda _file: [])
    monkeypatch.setattr(agent, "_static_findings", lambda _file: [])
    monkeypatch.setattr(agent, "_extract_api_endpoints", lambda _file: [])
    model_requests = []

    def reject_contract(parts, ctx=None, budget=None):
        model_requests.append(sum(len(part.text) for part in parts))
        return _AuditChunkResult(
            success=False,
            error="finding path is outside the current leaf",
            failure_kind="invalid_schema",
        )

    monkeypatch.setattr(agent, "_llm_project_audit_batch", reject_contract)
    events = []

    def capture_event(event_type, _ctx=None, message="", payload=None, **_kwargs):
        events.append((event_type, message, payload or {}))

    monkeypatch.setattr(agent, "_emit", capture_event)

    result = agent.scan_project(project.id, trace_dataflow=False, scan_mode="full")

    assert result.success is False
    assert result.failure_kind == "invalid_contract"
    assert model_requests == [len(file.content)]
    compliance = result.data["compliance"]
    assert compliance["semantic_invalid_contract_leaf_count"] == 1
    assert compliance["semantic_request_count"] == 1
    assert compliance["semantic_request_accounting_complete"] is True
    assert compliance["semantic_attempted_source_chars"] == len(file.content)
    assert compliance["semantic_source_chars"] == 0
    assert compliance["semantic_failed_source_chars"] == len(file.content)
    assert compliance["semantic_accounted_source_chars"] == len(file.content)
    assert compliance["semantic_accounting_complete"] is True
    failed_payloads = [
        payload
        for event_type, _message, payload in events
        if event_type == AgentEventType.FAILED
    ]
    assert failed_payloads[-1]["invalid_contract_leaf_count"] == 1
    assert failed_payloads[-1]["failure_kind"] == "invalid_contract"


def test_scan_project_fails_instead_of_claiming_completion_when_dataflow_fails(monkeypatch):
    """实际触发数据流推断后，上游失败不得伪装成合法零路径。"""
    agent = SecuritySentinelAgent()
    db = MagicMock()
    project = _make_project()
    file = _make_file(content="<?php system($_GET['cmd']);", language="php")
    db.get.return_value = project
    agent.inject(db, user=_make_user())
    _patch_project_source(monkeypatch, [file])
    monkeypatch.setattr(agent, "_regex_findings", lambda _file: [])
    monkeypatch.setattr(agent, "_static_findings", lambda _file: [])
    monkeypatch.setattr(agent, "_extract_api_endpoints", lambda _file: [])
    monkeypatch.setattr(
        agent,
        "_llm_project_audit_batch",
        lambda parts, ctx=None, budget=None: _AuditChunkResult(
            entry_points=[{"file": "foo.py", "function": "handler"}],
            dangerous_sinks=[{"file": "foo.py", "name": "system"}],
        ),
    )
    monkeypatch.setattr(agent, "_llm_dataflow_analysis", lambda *_args, **_kwargs: None)

    result = agent.scan_project(project.id, trace_dataflow=True)

    assert result.success is False
    assert "数据流分析未完成" in (result.error or "")
    assert result.data["threat_model"]["data_flows"] == []
    assert result.data["compliance"]["dataflow_requested"] is True
    assert result.data["compliance"]["dataflow_attempted"] is True
    assert result.data["compliance"]["dataflow_complete"] is False


def test_scan_project_dataflow_cannot_exceed_shared_model_request_budget(monkeypatch):
    from app.core.config import settings

    agent = SecuritySentinelAgent()
    db = MagicMock()
    project = _make_project()
    file = _make_file(content="<?php system($_GET['cmd']);", language="php")
    db.get.return_value = project
    agent.inject(db, user=_make_user())
    _patch_project_source(monkeypatch, [file])
    monkeypatch.setattr(settings, "security_semantic_max_requests", 1)
    monkeypatch.setattr(agent, "_regex_findings", lambda _file: [])
    monkeypatch.setattr(agent, "_static_findings", lambda _file: [])
    monkeypatch.setattr(agent, "_extract_api_endpoints", lambda _file: [])
    monkeypatch.setattr(
        agent,
        "_llm_project_audit_batch",
        lambda parts, ctx=None, budget=None: _AuditChunkResult(
            entry_points=[{"file": "foo.py", "function": "handler"}],
            dangerous_sinks=[{"file": "foo.py", "name": "system"}],
        ),
    )
    dataflow_model_calls = []
    monkeypatch.setattr(
        agent,
        "call_json",
        lambda *_args, **_kwargs: dataflow_model_calls.append(True)
        or AgentResult(success=True, data={"data_flows": []}),
    )

    result = agent.scan_project(project.id, trace_dataflow=True, scan_mode="static_full")

    assert result.success is False
    assert result.failure_kind == "semantic_budget_exhausted"
    assert dataflow_model_calls == []
    compliance = result.data["compliance"]
    assert compliance["semantic_request_count"] == 1
    assert compliance["dataflow_request_count"] == 0
    assert compliance["audit_request_count"] == 1
    assert compliance["audit_request_accounting_complete"] is True


def test_llm_dataflow_distinguishes_valid_empty_result_from_failure(monkeypatch):
    agent = SecuritySentinelAgent()
    call_kwargs = []

    def return_empty_flows(*_args, **kwargs):
        call_kwargs.append(kwargs)
        return AgentResult(success=True, data={"data_flows": []})

    monkeypatch.setattr(agent, "call_json", return_empty_flows)

    empty_result = agent._llm_dataflow_analysis([], [], "demo", None)
    assert empty_result is not None
    assert empty_result.items == []
    assert empty_result.total_count == 0
    assert call_kwargs[0]["thinking"] is False

    monkeypatch.setattr(
        agent,
        "call_json",
        lambda *_args, **_kwargs: AgentResult(success=True, data={"data_flows": "invalid"}),
    )
    assert agent._llm_dataflow_analysis([], [], "demo", None) is None


def test_llm_dataflow_preserves_total_when_return_sample_is_bounded(monkeypatch):
    agent = SecuritySentinelAgent()
    flows = [
        {
            "from": f"entry.py:handler_{index}",
            "via": [],
            "to": f"sink.py:query_{index}",
            "risk_type": "SQL 注入",
            "severity": "高",
        }
        for index in range(150)
    ]
    monkeypatch.setattr(
        agent,
        "call_json",
        lambda *_args, **_kwargs: AgentResult(
            success=True,
            data={"data_flows": flows},
        ),
    )

    result = agent._llm_dataflow_analysis([], [], "demo", None)

    assert result is not None
    assert len(result.items) == 100
    assert result.total_count == 150
    assert result.unique_link_count == 150


def test_scan_project_full_does_not_truncate_above_legacy_top_n(monkeypatch):
    """默认整包白盒审计不得继续受旧 top_n=50/max=200 截断。"""
    agent = SecuritySentinelAgent()
    db = MagicMock()
    project = _make_project()
    files = [
        _make_file(file_id=index, content="x = 1\n", file_name=f"src/f{index}.py")
        for index in range(1, 251)
    ]
    db.get.return_value = project
    query = MagicMock()
    query.filter.return_value.all.return_value = files
    db.query.return_value = query
    agent.inject(db, user=_make_user())
    _patch_project_source(monkeypatch, files)
    monkeypatch.setattr(agent, "_regex_findings", lambda _file: [])
    monkeypatch.setattr(agent, "_static_findings", lambda _file: [])
    monkeypatch.setattr(agent, "_extract_api_endpoints", lambda _file: [])
    monkeypatch.setattr(
        agent,
        "_llm_project_audit_batch",
        lambda parts, ctx=None, budget=None: _AuditChunkResult(),
    )

    result = agent.scan_project(project.id, top_n=1, trace_dataflow=False)

    assert result.success is True
    compliance = result.data["compliance"]
    assert result.data["file_count"] == 250
    assert compliance["total_file_count"] == 250
    assert compliance["scanned_file_count"] == 250
    assert compliance["coverage_ratio"] == 1.0
    assert compliance["truncated"] is False


def test_scan_project_bounds_retained_findings_but_preserves_total_counts(monkeypatch):
    agent = SecuritySentinelAgent()
    # 本用例断言「保留条数有上限但总计数保留」的原始边界行为,
    # 需关闭 v3.3 的去重+对抗复检(它会合并重复 finding)。
    agent._verify_enabled = False
    db = MagicMock()
    project = _make_project()
    files = [
        _make_file(file_id=index, content="x = 1\n", file_name=f"src/f{index}.py")
        for index in range(5)
    ]
    db.get.return_value = project
    agent.inject(db, user=_make_user())
    _patch_project_source(monkeypatch, files)
    finding = {
        "title": "bounded",
        "severity": "高",
        "owasp": "A03:2021-Injection",
        "file_path": "src/f1.py",
        "evidence": "query",
    }
    monkeypatch.setattr(agent, "_regex_findings", lambda _file: [dict(finding) for _ in range(600)])
    monkeypatch.setattr(agent, "_static_findings", lambda _file: [])
    monkeypatch.setattr(agent, "_extract_api_endpoints", lambda _file: [])
    monkeypatch.setattr(
        agent,
        "_llm_project_audit_batch",
        lambda parts, ctx=None, budget=None: _AuditChunkResult(),
    )

    result = agent.scan_project(project.id, trace_dataflow=False, scan_mode="static_full")

    assert result.success is True
    assert len(result.data["findings"]) == 2_000
    compliance = result.data["compliance"]
    assert compliance["finding_total_count"] == 3_000
    assert compliance["retained_finding_count"] == 2_000
    assert compliance["findings_truncated"] is True
    assert compliance["finding_severity_counts"]["高"] == 3_000
    assert compliance["owasp_coverage"] == ["A03"]


def test_scan_project_marks_secondary_graph_response_truncation(monkeypatch):
    agent = SecuritySentinelAgent()
    db = MagicMock()
    project = _make_project()
    file = _make_file(content="x = 1\n", file_name="src/routes.py")
    db.get.return_value = project
    agent.inject(db, user=_make_user())
    _patch_project_source(monkeypatch, [file])
    endpoints = [
        {
            "method": "GET",
            "path": f"/route-{index}",
            "file_path": file.file_path,
            "line_number": index + 1,
            "handler": f"handler_{index}",
        }
        for index in range(150)
    ]
    monkeypatch.setattr(agent, "_extract_api_endpoints", lambda _file: endpoints)
    monkeypatch.setattr(agent, "_regex_findings", lambda _file: [])
    monkeypatch.setattr(agent, "_static_findings", lambda _file: [])
    monkeypatch.setattr(
        agent,
        "_llm_project_audit_batch",
        lambda parts, ctx=None, budget=None: _AuditChunkResult(),
    )

    result = agent.scan_project(project.id, trace_dataflow=False, scan_mode="static_full")

    compliance = result.data["compliance"]
    assert compliance["api_endpoint_total_count"] == 150
    assert compliance["retained_api_endpoint_count"] == 150
    assert compliance["returned_api_endpoint_count"] == 100
    assert compliance["response_graph_truncated"] is True
    assert compliance["graph_items_truncated"] is True


def test_scan_project_fails_when_new_audit_generation_takes_over(monkeypatch):
    agent = SecuritySentinelAgent()
    db = MagicMock()
    project = _make_project()
    file = _make_file(content="x = 1\n", file_name="src/main.py")
    db.get.return_value = project
    agent.inject(db, user=_make_user())
    _patch_project_source(monkeypatch, [file])
    monkeypatch.setattr(
        project_source_service,
        "begin_source_archive_audit",
        lambda *_args, **_kwargs: "old-run-id",
    )
    monkeypatch.setattr(
        project_source_service,
        "finish_source_archive_audit",
        lambda *_args, **_kwargs: False,
    )
    monkeypatch.setattr(agent, "_regex_findings", lambda _file: [])
    monkeypatch.setattr(agent, "_static_findings", lambda _file: [])
    monkeypatch.setattr(agent, "_extract_api_endpoints", lambda _file: [])
    monkeypatch.setattr(
        agent,
        "_llm_project_audit_batch",
        lambda _parts, ctx=None, budget=None: _AuditChunkResult(),
    )
    events = []
    monkeypatch.setattr(
        agent,
        "_emit",
        lambda event_type, *_args, **_kwargs: events.append(event_type),
    )

    result = agent.scan_project(
        project.id,
        trace_dataflow=False,
        scan_mode="static_full",
    )

    assert result.success is False
    assert "新一代运行接管" in (result.error or "")
    assert AgentEventType.FAILED in events
    assert AgentEventType.COMPLETE not in events


def test_scan_project_preserves_raw_dataflow_link_total_after_bounding(monkeypatch):
    agent = SecuritySentinelAgent()
    db = MagicMock()
    project = _make_project()
    file = _make_file(content="x = input()\n", file_name="src/main.py")
    db.get.return_value = project
    agent.inject(db, user=_make_user())
    _patch_project_source(monkeypatch, [file])
    monkeypatch.setattr(agent, "_regex_findings", lambda _file: [])
    monkeypatch.setattr(agent, "_static_findings", lambda _file: [])
    monkeypatch.setattr(agent, "_extract_api_endpoints", lambda _file: [])
    monkeypatch.setattr(
        agent,
        "_llm_project_audit_batch",
        lambda _parts, ctx=None, budget=None: _AuditChunkResult(
            entry_points=[{"file": "src/main.py", "function": "handler"}],
            dangerous_sinks=[{"file": "src/main.py", "name": "query"}],
        ),
    )
    raw_flows = [
        {
            "from": f"entry.py:handler_{index}",
            "via": [],
            "to": f"db.py:query_{index}",
            "risk_type": "SQL 注入",
            "severity": "高",
        }
        for index in range(150)
    ]
    monkeypatch.setattr(
        agent,
        "call_json",
        lambda *_args, **_kwargs: AgentResult(
            success=True,
            data={"data_flows": raw_flows},
        ),
    )

    result = agent.scan_project(
        project.id,
        trace_dataflow=True,
        scan_mode="static_full",
    )

    assert result.success is True
    compliance = result.data["compliance"]
    assert compliance["data_flow_total_count"] == 150
    assert compliance["retained_data_flow_count"] == 100
    assert compliance["returned_data_flow_count"] == 100
    assert compliance["code_link_total_count"] == 150
    assert compliance["retained_code_link_count"] == 100
    assert compliance["returned_code_link_count"] == 100
    assert compliance["response_graph_truncated"] is True


def test_scan_all_projects_empty_scope_returns_empty_result():
    """全量扫描在没有可见项目时应稳定返回空结果"""
    agent = SecuritySentinelAgent()
    db = MagicMock()
    chain = MagicMock()
    chain.filter.return_value = chain
    chain.order_by.return_value.all.return_value = []
    db.query.return_value = chain
    agent.inject(db, user=_make_user())

    result = agent.scan_all_projects()

    assert result.success is True
    assert result.data["findings"] == []
    assert result.data["file_count"] == 0
    assert result.data["risk_score"] == 100
    assert result.data["compliance"]["project_count"] == 0


def test_scan_all_projects_aggregates_project_results(monkeypatch):
    """全量扫描应复用单项目扫描并聚合 findings/文件数/数据流"""
    agent = SecuritySentinelAgent()
    db = MagicMock()
    projects = [
        _make_project(project_id=1, user_id=1, name="alpha"),
        _make_project(project_id=2, user_id=1, name="beta"),
    ]
    chain = MagicMock()
    chain.filter.return_value = chain
    chain.order_by.return_value.all.return_value = projects
    db.query.return_value = chain
    agent.inject(db, user=_make_user())

    scan_modes = []

    def fake_scan_project(project_id, top_n=50, trace_dataflow=True, ctx=None, scan_mode="full"):
        scan_modes.append(scan_mode)
        return AgentResult(success=True, data={
            "findings": [{
                "title": f"SQL 注入 {project_id}",
                "category": "注入",
                "owasp": "A03:2021-Injection",
                "cwe": "CWE-89",
                "severity": "严重",
                "file_path": "api.py",
                "file_id": project_id,
                "lines": "L1",
                "line_number": 1,
                "end_line": 1,
                "evidence": "query",
                "exploit_scenario": "SQL 注入",
                "fix_suggestion": "参数化查询",
                "references": [],
                "confidence": 0.9,
                "source": "llm",
            }],
            "threat_model": {
                "entry_points": [{"file": "api.py", "line": 1}],
                "data_flows": [{
                    "from": "api.py:handler",
                    "via": ["service.py:run"],
                    "to": "db.py:query",
                    "risk_type": "SQL 注入",
                    "severity": "严重",
                }],
                "api_endpoints": [{
                    "method": "POST",
                    "path": "/login",
                    "file_path": "api.py",
                    "line_number": 1,
                    "handler": "handler",
                    "auth_hint": "未发现明显认证线索",
                    "source": "python_route",
                }],
                "code_links": [{
                    "from": "api.py:handler",
                    "to": "db.py:query",
                    "relation": "跨文件数据流",
                    "risk_type": "SQL 注入",
                    "severity": "严重",
                }],
                "attack_surface_summary": "mock",
            },
            "compliance": {},
            "risk_score": 85,
            "summary": "mock",
            "file_count": project_id,
            "duration_ms": 1,
        })

    monkeypatch.setattr(agent, "scan_project", fake_scan_project)

    result = agent.scan_all_projects(top_n_per_project=10)

    assert result.success is True
    assert scan_modes == ["triage", "triage"]
    assert len(result.data["findings"]) == 2
    assert result.data["file_count"] == 3
    assert result.data["risk_score"] == 70
    assert result.data["compliance"]["scanned_project_count"] == 2
    assert result.data["compliance"]["retained_entry_point_count"] == 2
    assert result.data["compliance"]["retained_data_flow_count"] == 2
    assert result.data["compliance"]["retained_api_endpoint_count"] == 2
    assert result.data["compliance"]["retained_code_link_count"] == 2
    assert result.data["findings"][0]["file_path"] == "alpha/api.py"
    assert result.data["threat_model"]["data_flows"][1]["from"] == "beta/api.py:handler"
    assert result.data["threat_model"]["api_endpoints"][0]["file_path"] == "alpha/api.py"
    assert result.data["threat_model"]["code_links"][1]["to"] == "beta/db.py:query"
    assert result.data["discussion"]["turns"]


def test_scan_all_projects_rejects_partial_project_failures(monkeypatch):
    agent = SecuritySentinelAgent()
    db = MagicMock()
    project = _make_project(project_id=1, user_id=1, name="alpha")
    chain = MagicMock()
    chain.filter.return_value = chain
    chain.order_by.return_value.all.return_value = [project]
    db.query.return_value = chain
    agent.inject(db, user=_make_user())

    monkeypatch.setattr(
        agent,
        "scan_project",
        lambda *args, **kwargs: AgentResult(
            success=False,
            error="项目语义审计执行未完成",
            failure_kind="invalid_item",
        ),
    )

    result = agent.scan_all_projects()

    assert result.success is False
    assert result.failure_kind == "project_scan_failed"
    assert result.data["compliance"]["scan_complete"] is False
    assert result.data["compliance"]["project_errors"][0]["project_id"] == 1


def test_extract_api_endpoints_detects_common_routes():
    """接口扫描应识别后端路由和前端 HTTP client 调用"""
    agent = SecuritySentinelAgent()
    file = _make_file(
        content=(
            "from fastapi import APIRouter, Depends\n"
            "router = APIRouter()\n"
            "@router.post('/users')\n"
            "async def create_user(current_user=Depends(get_current_user)):\n"
            "    pass\n"
            "app.get('/health', handler)\n"
            "axios.post('/api/login', data)\n"
        ),
        file_name="api.py",
        language="python",
    )

    endpoints = agent._extract_api_endpoints(file)

    paths = {(item["method"], item["path"]) for item in endpoints}
    assert ("POST", "/users") in paths
    assert ("GET", "/health") in paths
    assert ("POST", "/api/login") in paths
    user_endpoint = next(item for item in endpoints if item["path"] == "/users")
    assert user_endpoint["handler"] == "create_user"
    assert user_endpoint["auth_hint"] == "发现认证/权限线索"


def test_extract_api_endpoints_stops_at_result_limit():
    agent = SecuritySentinelAgent()
    content = "\n".join(
        f"@app.get('/route-{index}')\ndef handler_{index}(): pass"
        for index in range(300)
    )
    file = _make_file(content=content, file_name="routes.py")

    endpoints = agent._extract_api_endpoints(file)

    assert len(endpoints) == 200
    assert endpoints[-1]["path"] == "/route-199"


def test_extract_api_endpoints_rejects_pathological_line_count_before_split():
    agent = SecuritySentinelAgent()
    file = _make_file(content="x", file_name="huge.php", language="php")
    file.line_count = 100_001

    with pytest.raises(RuntimeError, match="审计资源上限"):
        agent._extract_api_endpoints(file)


def test_build_code_links_connects_endpoint_to_sink_and_dataflow():
    """代码联动关系应同时包含接口到 sink 和已有数据流"""
    agent = SecuritySentinelAgent()

    result = agent._build_code_links(
        api_endpoints=[{
            "method": "POST",
            "path": "/users",
            "file_path": "api.py",
            "line_number": 3,
            "handler": "create_user",
        }],
        sinks=[{
            "file": "api.py",
            "name": "raw_query",
            "sink_type": "SQL",
        }],
        data_flows=[{
            "from": "api.py:create_user",
            "to": "db.py:query",
            "risk_type": "SQL 注入",
            "severity": "高",
        }],
    )

    assert result.total_count == 2
    assert any(link["relation"] == "接口到同文件危险接收点" for link in result.items)
    assert any(link["relation"] == "跨文件数据流" for link in result.items)
    assert any(link["risk_type"] == "SQL 注入" for link in result.items)


def test_build_code_links_preserves_total_when_return_sample_is_bounded():
    agent = SecuritySentinelAgent()
    flows = [
        {
            "from": f"entry.py:handler_{index}",
            "to": f"db.py:query_{index}",
            "risk_type": "SQL 注入",
            "severity": "高",
        }
        for index in range(250)
    ]

    result = agent._build_code_links([], [], flows)

    assert len(result.items) == 200
    assert result.total_count == 250


def test_build_multi_agent_discussion_returns_action_items():
    """多 Agent 讨论摘要应给出发言、共识和行动项"""
    agent = SecuritySentinelAgent()
    discussion = agent._build_multi_agent_discussion(
        findings=[{
            "severity": "高",
            "owasp": "A03:2021-Injection",
        }],
        threat_model={
            "api_endpoints": [{
                "auth_hint": "未发现明显认证线索",
            }],
            "data_flows": [],
            "code_links": [{"severity": "高"}],
        },
        project_count=2,
    )

    assert discussion["mode"] == "multi_agent_summary"
    assert len(discussion["turns"]) >= 4
    assert "多 Agent 共识" in discussion["consensus"]
    assert any("认证" in item for item in discussion["action_items"])


# ---------- 风险评分 ----------


def test_compute_risk_score_severe_drops_15_each():
    agent = SecuritySentinelAgent()
    findings = [{"severity": "严重"}, {"severity": "严重"}, {"severity": "中"}]
    score = agent._compute_risk_score(findings)
    # 100 - 15*2 - 3 = 67
    assert score == 67


def test_compute_risk_score_clamps_to_zero():
    agent = SecuritySentinelAgent()
    findings = [{"severity": "严重"}] * 20  # 300 deduct
    assert agent._compute_risk_score(findings) == 0


# ---------- OWASP/CWE 推断 ----------


def test_infer_owasp_cwe_for_sql_injection():
    agent = SecuritySentinelAgent()
    owasp, cwe = agent._infer_owasp_cwe("SQL 注入风险", "存在 sql injection")
    assert "Injection" in owasp
    assert cwe == "CWE-89"


def test_infer_owasp_cwe_for_path_traversal():
    agent = SecuritySentinelAgent()
    owasp, cwe = agent._infer_owasp_cwe("路径遍历", "")
    assert cwe == "CWE-22"


def test_infer_owasp_cwe_fallback_to_empty():
    agent = SecuritySentinelAgent()
    owasp, cwe = agent._infer_owasp_cwe("一些无关问题", "blablabla")
    assert owasp == ""
    assert cwe == ""


# ---------- 任务复审 ----------


def test_scan_task_labels_existing_security_issues(monkeypatch):
    """已落库的安全 issue 应被打上 OWASP/CWE 标签"""
    agent = SecuritySentinelAgent()
    db = MagicMock()
    task = _make_task(task_id=10, user_id=1)
    db.get.return_value = task

    # 模拟 db.query(...).filter(...).all()
    issues = [
        ReviewIssue(
            id=1, task_id=10, file_id=1, file_name="x.py",
            line_number=5, end_line=5,
            issue_type="安全漏洞", severity="高",
            title="SQL 注入风险",
            description="字符串拼接构造 SQL",
            suggestion="参数化查询",
            status="unfixed",
        ),
        ReviewIssue(
            id=2, task_id=10, file_id=1, file_name="x.py",
            line_number=20, end_line=20,
            issue_type="安全漏洞", severity="中",
            title="XSS 风险",
            description="未转义用户输入",
            suggestion="使用模板自动转义",
            status="unfixed",
        ),
    ]
    chain = MagicMock()
    chain.filter.return_value.all.return_value = issues
    db.query.return_value = chain

    agent.inject(db, user=_make_user())
    result = agent.scan_task(task_id=10)
    assert result.success is True
    findings = result.data["findings"]
    assert len(findings) == 2
    titles = {f["title"] for f in findings}
    assert "SQL 注入风险" in titles
    # 自动打了 OWASP/CWE 标签
    sql_finding = next(f for f in findings if "SQL" in f["title"])
    assert "Injection" in sql_finding["owasp"]
    assert sql_finding["cwe"] == "CWE-89"


# ---------- 合规与文件优先级 ----------


def test_compute_compliance_extracts_owasp_codes():
    agent = SecuritySentinelAgent()
    findings = [
        {"owasp": "A03:2021-Injection"},
        {"owasp": "A03:2021-Injection"},
        {"owasp": "A07:2021-Identification"},
    ]
    out = agent._compute_compliance(findings)
    assert set(out["owasp_coverage"]) == {"A03", "A07"}


def test_prioritize_files_puts_high_risk_first():
    agent = SecuritySentinelAgent()
    files = [
        _make_file(file_id=1, file_name="readme.md"),
        _make_file(file_id=2, file_name="api/auth_controller.py"),
        _make_file(file_id=3, file_name="utils/format.py"),
    ]
    ordered = agent._prioritize_files(files)
    assert ordered[0].id == 2  # auth_controller 含 api/auth/controller 三个关键词


def test_upgrade_findings_on_dataflow():
    agent = SecuritySentinelAgent()
    findings = [
        {"severity": "中", "file_path": "api/auth.py"},
        {"severity": "低", "file_path": "utils/helper.py"},
    ]
    flows = [{"from": "api/auth.py:login", "via": [], "to": "db:query"}]
    agent._upgrade_findings_on_dataflow(findings, flows)
    # auth.py 的 finding 升级到 高
    assert findings[0]["severity"] == "高"
    # helper.py 不在数据流上,不变
    assert findings[1]["severity"] == "低"
