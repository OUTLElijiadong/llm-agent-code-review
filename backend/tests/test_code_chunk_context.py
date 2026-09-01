"""跨分片符号、调用和继承上下文回归。"""

from __future__ import annotations

from app.ai.code_chunker import build_symbol_index, chunk_code_with_context

PYTHON_SOURCE = """import os

COMMAND_PREFIX = "safe:"

class BaseRunner:
    def validate(self, value):
        return bool(value)

class ShellRunner(BaseRunner):
    def run(self, user_input):
        def normalize(value):
            return value.strip()
        command = COMMAND_PREFIX + normalize(user_input)
        if self.validate(command):
            return execute(command)

def execute(command):
    return os.system(command)
"""


def test_python_symbol_index_keeps_nested_calls_and_inheritance() -> None:
    index = build_symbol_index(PYTHON_SOURCE, "python")

    assert index.mode == "ast"
    assert index.symbols["ShellRunner"].bases == ("BaseRunner",)
    assert "ShellRunner.run.normalize" in index.symbols
    assert index.symbols["COMMAND_PREFIX"].kind == "variable"
    assert index.symbols["ShellRunner.run.user_input"].kind == "parameter"
    assert ("ShellRunner.run", "execute") in index.call_edges
    assert ("ShellRunner.run.normalize", "value.strip") in index.call_edges
    assert ("ShellRunner.run", "value.strip") not in index.call_edges
    assert ("execute", "os.system") in index.call_edges
    assert ("ShellRunner.run", "COMMAND_PREFIX") in index.reference_edges


def test_each_contextual_chunk_sees_relevant_cross_chunk_symbols() -> None:
    chunks = chunk_code_with_context(PYTHON_SOURCE, "python", threshold=120)

    assert len(chunks) >= 2
    run_chunk = next(item for item in chunks if "def run" in item.text)
    execute_chunk = next(item for item in chunks if "def execute" in item.text)
    assert "ShellRunner(BaseRunner)" in run_chunk.context
    assert "ShellRunner.run -> execute" in run_chunk.context
    assert "ShellRunner.run => COMMAND_PREFIX" in run_chunk.context
    assert "ShellRunner.run -> execute" in execute_chunk.context
    assert "execute -> os.system" in execute_chunk.context


def test_context_and_fingerprint_are_stable() -> None:
    first = chunk_code_with_context(PYTHON_SOURCE, "python", threshold=120)
    second = chunk_code_with_context(PYTHON_SOURCE, "python", threshold=120)

    assert [(item.context, item.context_fingerprint) for item in first] == [
        (item.context, item.context_fingerprint) for item in second
    ]


def test_syntax_error_uses_visible_lexical_fallback() -> None:
    index = build_symbol_index("def broken(:\n    call(value)\n", "python")

    assert index.mode == "lexical"
    assert index.diagnostics
