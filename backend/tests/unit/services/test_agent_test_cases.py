"""agent 动态测试用例:生成注入、zip 注入、结果解析回归。"""

from __future__ import annotations

import base64
import io
import zipfile

from app.services.sandbox_service import (
    _extract_agent_tests_result,
    _inject_agent_test_files,
    _inject_deployment_patch,
    _source_summary_for_agent_tests,
)


def _zip_with(files: dict[str, str]) -> str:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, content in files.items():
            zf.writestr(name, content)
    return base64.b64encode(buf.getvalue()).decode("ascii")


def test_source_summary_lists_files_and_entries() -> None:
    archive = _zip_with({"app/main.py": "print(1)", "tests/test_a.py": "def test_a(): pass"})
    summary = _source_summary_for_agent_tests(archive, "python")
    assert summary["language"] == "python"
    assert "app/main.py" in summary["files"]
    assert "tests/test_a.py" in summary["entries"]
    assert summary["coverage_complete"] is True
    assert "app/main.py" in str(summary["source_chunks"])


def test_source_summary_packs_many_short_files_without_losing_tail() -> None:
    files = {f"src/module_{index:03d}.py": f"VALUE = {index}\n" for index in range(301)}
    archive = _zip_with(files)
    summary = _source_summary_for_agent_tests(archive, "python")
    assert len(summary["files"]) == 301
    assert "src/module_300.py" in summary["entries"]
    assert summary["coverage_complete"] is True
    assert "src/module_300.py" in str(summary["source_chunks"])


def test_source_summary_preserves_end_of_long_file_as_later_chunk() -> None:
    archive = _zip_with({"main.py": "# filler\n" * 600 + "TAIL_SECURITY_GATE = True\n"})
    summary = _source_summary_for_agent_tests(archive, "python")
    assert summary["coverage_complete"] is True
    chunks = [item for item in summary["source_chunks"] if item["path"] == "main.py"]
    assert len(chunks) > 1
    assert "TAIL_SECURITY_GATE" in chunks[-1]["text"]


def test_source_summary_marks_uncovered_large_archive() -> None:
    archive = _zip_with({f"module_{index}.py": "x = 1\n" * 800 for index in range(40)})
    summary = _source_summary_for_agent_tests(archive, "python")
    assert summary["coverage_complete"] is False
    assert "上限" in summary["coverage_error"]
    assert len(summary["files"]) == 40


def test_source_summary_rejects_ambiguous_duplicate_zip_members() -> None:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("main.php", "<?php echo 1;")
        zf.writestr("main.php", "<?php echo 2;")
    summary = _source_summary_for_agent_tests(base64.b64encode(buf.getvalue()).decode(), "php")
    assert summary["coverage_complete"] is False
    assert "重复路径" in summary["coverage_error"]


def test_inject_agent_test_files_adds_agent_tests_dir() -> None:
    archive = _zip_with({"main.py": "print(1)"})
    files = [{"path": "test_ai_1.py", "content": "assert 1 == 1"}]
    augmented = _inject_agent_test_files(archive, files)
    raw = base64.b64decode(augmented)
    with zipfile.ZipFile(io.BytesIO(raw)) as zf:
        names = set(zf.namelist())
        assert "_agent_tests/test_ai_1.py" in names
        assert zf.read("_agent_tests/test_ai_1.py").decode() == "assert 1 == 1"


def test_extract_agent_tests_result_parses_marker() -> None:
    log = (
        "start\nPRISM_AGENT_TESTS_BEGIN "
        '{"generated":2,"passed":1,"failed":1,"passed_count":1,"files":{"a.py":"pass","b.py":"fail"}} '
        "PRISM_AGENT_TESTS_END\nend"
    )
    result = _extract_agent_tests_result(log)
    assert result is not None
    assert result["generated"] == 2
    assert result["passed_count"] == 1
    assert result["files"]["b.py"] == "fail"


def test_extract_agent_tests_result_none_when_missing() -> None:
    assert _extract_agent_tests_result("no marker") is None


def test_inject_deployment_patch_adds_launch_script() -> None:
    archive = _zip_with({"main.py": "print(1)"})
    augmented = _inject_deployment_patch(archive, "exec python main.py\n")
    with zipfile.ZipFile(io.BytesIO(base64.b64decode(augmented))) as zf:
        assert "_prism_launch.sh" in zf.namelist()
        assert zf.read("_prism_launch.sh").decode() == "exec python main.py\n"


def test_syntax_repair_round_writes_complete_reconstructed_file(db, monkeypatch) -> None:
    from types import SimpleNamespace

    from app.services import sandbox_service

    source = "<?php\n" + "// keep\n" * 6_000 + "echo broken;\n"
    archive = _zip_with({"main.php": source, "untouched.php": "<?php echo 'keep';\n"})
    environment = SimpleNamespace(
        public_id="sbx_repair", owner_id=7, project_id=9, language="php", source_sha256="sha",
    )

    class FakeRepair:
        _api_key = "configured"

        def repair(self, **_kwargs):
            return {"files": {"main.php": source.replace("echo broken;", "echo 'fixed';")}}

    monkeypatch.setattr(sandbox_service, "SyntaxRepairAgent", FakeRepair)
    monkeypatch.setattr(sandbox_service, "configure_subagent", lambda _db, agent, _user_id: agent)
    monkeypatch.setattr(sandbox_service, "_append_event", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        sandbox_service.project_source_revision_service, "save_revision",
        lambda *_args, **_kwargs: SimpleNamespace(revision_no=1),
    )
    result = sandbox_service._syntax_repair_round(
        db, environment, archive,
        [{"file": "main.php", "line": 6002, "message": "unexpected identifier"}],
    )
    assert result is not None
    with zipfile.ZipFile(io.BytesIO(base64.b64decode(result["source"]))) as zf:
        assert zf.read("main.php").decode() == source.replace("echo broken;", "echo 'fixed';")
        assert zf.read("untouched.php").decode() == "<?php echo 'keep';\n"
        assert zf.namelist().count("main.php") == 1


def test_large_source_does_not_call_dynamic_test_agent_with_partial_context(db, monkeypatch) -> None:
    from types import SimpleNamespace

    from app.services import sandbox_service

    calls: list[dict] = []

    class FakeGenerator:
        _api_key = "configured"

        def generate(self, **kwargs):
            calls.append(kwargs)
            return {"files": []}

    archive = _zip_with({f"module_{index}.py": "x = 1\n" * 800 for index in range(40)})
    environment = SimpleNamespace(
        public_id="sbx_large", owner_id=7, project_id=9, agent_config_json="{}",
    )
    monkeypatch.setattr(
        "app.agents.test_case_generator_agent.TestCaseGeneratorAgent", FakeGenerator,
    )
    monkeypatch.setattr(sandbox_service, "configure_subagent", lambda _db, agent, _user_id: agent)
    monkeypatch.setattr(sandbox_service, "_append_event", lambda *_args, **_kwargs: None)
    result = sandbox_service._generate_agent_test_cases(db, environment, archive, "python", "whitebox")
    assert result is None
    assert calls == []


def test_generate_deployment_patch_uses_agent_plan(db, monkeypatch) -> None:
    from types import SimpleNamespace

    from app.services.sandbox_service import _generate_deployment_patch

    environment = SimpleNamespace(
        id=1, public_id="sbx_deploy_patch", project_id=1, owner_id=1,
        test_mode="blackbox", language="python",
    )
    captured: dict = {}
    monkeypatch.setattr("app.services.sandbox_service.configure_subagent", lambda _db, agent, user_id: agent)

    class FakeDeploymentAgent:
        _api_key = "test-key"

        def plan(self, **kwargs):
            captured.update(kwargs)
            return {"launch_script": "exec python -m http.server $PRISM_PREVIEW_PORT", "notes": "无入口,已补全"}

    monkeypatch.setattr(
        "app.agents.deployment_coordinator_agent.DeploymentCoordinatorAgent",
        FakeDeploymentAgent,
    )
    result = _generate_deployment_patch(db, environment, _zip_with({"a.py": "x=1"}), "python")
    assert result is not None
    assert "launch_script" in result
    assert captured["language"] == "python"
