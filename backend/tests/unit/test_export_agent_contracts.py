"""静态职责契约导出与无写入校验测试，不加载应用生命周期。"""

from __future__ import annotations

import builtins
import importlib.util
import json
import runpy
import subprocess
import sys
from collections import Counter
from dataclasses import replace
from pathlib import Path
from types import ModuleType

import pytest

BACKEND_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = BACKEND_ROOT / "scripts" / "export_agent_contracts.py"
CONTRACTS_PATH = BACKEND_ROOT / "app" / "agents" / "contracts.py"


@pytest.fixture
def exporter(monkeypatch):
    """旧脚本也只接触独立加载的契约，禁止间接导入应用配置。"""
    catalog = ModuleType("isolated_contract_catalog")
    catalog.__dict__.update(runpy.run_path(str(CONTRACTS_PATH)))
    original_import = builtins.__import__

    def isolated_import(name, *args, **kwargs):
        if name == "app.agents.contracts":
            return catalog
        if name == "app" or name.startswith("app.") or name.startswith("dotenv"):
            raise AssertionError(f"禁止应用导入：{name}")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", isolated_import)
    spec = importlib.util.spec_from_file_location("isolated_contract_exporter", SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def invoke_main(exporter, monkeypatch, capsys, markdown_path, json_path, *, check=False):
    """保留现有两个必填输出参数，按 CLI 语义收集退出码和标准输出。"""
    arguments = [str(SCRIPT_PATH), "--markdown", str(markdown_path), "--json", str(json_path)]
    if check:
        arguments.append("--check")
    monkeypatch.setattr(sys, "argv", arguments)
    try:
        exit_code = exporter.main() or 0
    except SystemExit as error:
        exit_code = error.code
    captured = capsys.readouterr()
    return exit_code, captured.out, captured.err


def seed_outputs(exporter, tmp_path):
    """生成仅供测试使用的临时文件，不接触仓库产物。"""
    markdown_path = tmp_path / "contracts.md"
    json_path = tmp_path / "contracts.json"
    markdown_path.write_bytes(exporter.build_markdown().encode("utf-8"))
    json_path.write_bytes(exporter.build_json().encode("utf-8"))
    return markdown_path, json_path


def test_markdown_counts_static_contracts_by_actual_fields(exporter):
    """实际目录的总数、保护状态和每种执行模式都来自契约字段。"""
    contracts = tuple(exporter.CONTRACTS.values())
    protected_count = sum(contract.protected for contract in contracts)
    markdown = exporter.build_markdown()
    assert (
        f"本目录包含 {len(contracts)} 份静态职责契约，其中 {protected_count} 份受保护、"
        f"{len(contracts) - protected_count} 份受治理。"
    ) in markdown
    for mode, count in Counter(contract.execution_mode for contract in contracts).items():
        assert f"`{mode}`：{count} 份" in markdown
    for stale_claim in ("其余 28 个", "14 个 `BaseAgent`", "16 个治理画像", "实际运行 Agent"):
        assert stale_claim not in markdown
    assert "不是模型数量、运行实例数量或已执行 Agent 数量" in markdown


@pytest.mark.parametrize("empty", [False, True])
def test_markdown_adapts_to_new_modes_and_protection_flags(exporter, monkeypatch, empty):
    """增删契约、改变保护状态或新增执行模式不能留下硬编码数量。"""
    baseline = next(iter(exporter.CONTRACTS.values()))
    contracts = {} if empty else {
        "alpha": replace(baseline, code="alpha", execution_mode="future_mode", protected=True),
        "beta": replace(baseline, code="beta", execution_mode="future_mode", protected=False),
        "gamma": replace(baseline, code="gamma", execution_mode="service", protected=False),
    }
    monkeypatch.setattr(exporter, "CONTRACTS", contracts)
    markdown = exporter.build_markdown()
    if empty:
        assert "本目录包含 0 份静态职责契约，其中 0 份受保护、0 份受治理。" in markdown
        assert "受保护契约：无。" in markdown
    else:
        assert "本目录包含 3 份静态职责契约，其中 1 份受保护、2 份受治理。" in markdown
        assert "`future_mode`：2 份" in markdown
        assert "`service`：1 份" in markdown
        assert "受保护契约：`alpha`。" in markdown


def test_json_protected_codes_follow_contract_fields(exporter, monkeypatch):
    """JSON 保护清单不能依赖与当前 CONTRACTS 分离的旧集合。"""
    baseline = next(iter(exporter.CONTRACTS.values()))
    monkeypatch.setattr(exporter, "CONTRACTS", {
        "alpha": replace(baseline, code="alpha", protected=True),
        "beta": replace(baseline, code="beta", protected=False),
    })
    payload = json.loads(exporter.build_json())
    assert payload["contract_count"] == 2
    assert payload["protected_agents"] == ["alpha"]
    assert [contract["code"] for contract in payload["agents"]] == ["alpha", "beta"]


def test_default_mode_keeps_existing_output_arguments(exporter, monkeypatch, capsys, tmp_path):
    """非 check 模式继续创建指定目录并生成原有 Markdown/JSON 输出。"""
    markdown_path = tmp_path / "markdown" / "custom.md"
    json_path = tmp_path / "json" / "custom.json"
    exit_code, stdout, stderr = invoke_main(exporter, monkeypatch, capsys, markdown_path, json_path)
    assert exit_code == 0, stderr
    assert markdown_path.read_bytes() == exporter.build_markdown().encode("utf-8")
    assert json_path.read_bytes() == exporter.build_json().encode("utf-8")
    assert json.loads(stdout) == {
        "contracts": len(exporter.CONTRACTS),
        "protected": sorted(contract.code for contract in exporter.CONTRACTS.values() if contract.protected),
        "markdown": str(markdown_path),
        "json": str(json_path),
    }


def test_check_matching_outputs_never_writes(exporter, monkeypatch, capsys, tmp_path):
    """完全匹配返回零，且禁止 mkdir、文本写入和字节写入。"""
    markdown_path, json_path = seed_outputs(exporter, tmp_path)
    before = [(path.read_bytes(), path.stat().st_mtime_ns) for path in (markdown_path, json_path)]

    def reject_write(*args, **kwargs):
        raise AssertionError("--check 不得写入或创建目录")

    with monkeypatch.context() as guard:
        for method in ("mkdir", "write_text", "write_bytes"):
            guard.setattr(Path, method, reject_write)
        exit_code, stdout, stderr = invoke_main(exporter, guard, capsys, markdown_path, json_path, check=True)
    assert exit_code == 0, stderr
    assert json.loads(stdout)["differences"] == []
    assert before == [(path.read_bytes(), path.stat().st_mtime_ns) for path in (markdown_path, json_path)]


def test_check_missing_outputs_does_not_create_parents(exporter, monkeypatch, capsys, tmp_path):
    """缺失输出返回一，且不得创建任何输出父目录。"""
    markdown_path = tmp_path / "missing_markdown" / "contracts.md"
    json_path = tmp_path / "missing_json" / "contracts.json"
    exit_code, stdout, stderr = invoke_main(exporter, monkeypatch, capsys, markdown_path, json_path, check=True)
    assert exit_code == 1, stderr
    assert json.loads(stdout)["differences"] == [
        {"path": str(markdown_path), "status": "missing"},
        {"path": str(json_path), "status": "missing"},
    ]
    assert not markdown_path.parent.exists()
    assert not json_path.parent.exists()


@pytest.mark.parametrize("target_index", [0, 1])
@pytest.mark.parametrize("mutation", ["extra_byte", "crlf", "invalid_utf8"])
def test_check_detects_byte_drift_without_rewriting(
    exporter, monkeypatch, capsys, tmp_path, target_index, mutation
):
    """两种输出的新增字节、换行变化和非法 UTF-8 均视为字节漂移。"""
    paths = seed_outputs(exporter, tmp_path)
    target = paths[target_index]
    content = target.read_bytes()
    changed = {"extra_byte": content + b" ", "crlf": content.replace(b"\n", b"\r\n"), "invalid_utf8": b"\xff"}
    target.write_bytes(changed[mutation])
    before = [(path.read_bytes(), path.stat().st_mtime_ns) for path in paths]
    exit_code, stdout, stderr = invoke_main(exporter, monkeypatch, capsys, *paths, check=True)
    assert exit_code == 1, stderr
    assert json.loads(stdout)["differences"] == [{"path": str(target), "status": "different"}]
    assert before == [(path.read_bytes(), path.stat().st_mtime_ns) for path in paths]


@pytest.mark.parametrize("missing_index", [0, 1])
def test_check_one_missing_output_preserves_the_other(exporter, monkeypatch, capsys, tmp_path, missing_index):
    """任一产物缺失均返回非零，不补写、不改变另一产物。"""
    paths = seed_outputs(exporter, tmp_path)
    missing = paths[missing_index]
    missing.unlink()
    existing = paths[1 - missing_index]
    before = (existing.read_bytes(), existing.stat().st_mtime_ns)
    exit_code, stdout, stderr = invoke_main(exporter, monkeypatch, capsys, *paths, check=True)
    assert exit_code == 1, stderr
    assert json.loads(stdout)["differences"] == [{"path": str(missing), "status": "missing"}]
    assert not missing.exists()
    assert before == (existing.read_bytes(), existing.stat().st_mtime_ns)


@pytest.mark.parametrize("drifted", [False, True])
def test_cli_check_exit_code_without_application_imports(exporter, tmp_path, drifted):
    """真实 CLI 退出码匹配检查结果，导入应用、读取 .env 或联网会被提前阻断。"""
    markdown_path, json_path = seed_outputs(exporter, tmp_path)
    if drifted:
        json_path.write_bytes(b"stale")
    driver = """
import builtins
import runpy
import sys
from pathlib import Path

original_import = builtins.__import__

def isolated_import(name, *args, **kwargs):
    if name == 'app' or name.startswith('app.') or name.startswith('dotenv'):
        raise AssertionError('Application import prohibited: ' + name)
    return original_import(name, *args, **kwargs)

def guard(event, arguments):
    if event == 'open' and isinstance(arguments[0], (str, bytes)):
        name = Path(arguments[0].decode() if isinstance(arguments[0], bytes) else arguments[0]).name
        if name == '.env' or name.startswith('.env.'):
            raise AssertionError('Environment file access prohibited')
    if event in ('socket.connect', 'socket.bind', 'socket.getaddrinfo'):
        raise AssertionError('Network access prohibited')

builtins.__import__ = isolated_import
sys.addaudithook(guard)
sys.argv = sys.argv[1:]
runpy.run_path(sys.argv[0], run_name='__main__')
"""
    completed = subprocess.run(
        [sys.executable, "-B", "-c", driver, str(SCRIPT_PATH), "--markdown", str(markdown_path),
         "--json", str(json_path), "--check"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == int(drifted), completed.stderr
    payload = json.loads(completed.stdout)
    assert bool(payload["differences"]) is drifted
