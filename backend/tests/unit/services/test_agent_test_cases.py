"""agent 动态测试用例:生成注入、zip 注入、结果解析回归。"""

from __future__ import annotations

import base64
import io
import zipfile

from app.services.sandbox_service import (
    _extract_agent_tests_result,
    _inject_agent_test_files,
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
