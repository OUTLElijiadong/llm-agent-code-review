"""Replay only the trusted original compactor guard in this isolated process.

Run from candidate/backend with .venv311/bin/python. Expected exit code: 1
(the new validation-retry regression must fail under the old method). This
does not modify a file, call a provider, or replace any deployed implementation.
The new byte estimator stays active so this isolates the missing retry/schema
protocol budget rather than conflating it with the ASCII/4 underestimate.
"""

from __future__ import annotations

import ast
import subprocess

import pytest

from app.services import deepseek_responses_runtime as module


def main() -> int:
    source = subprocess.check_output(
        ["git", "show", "732f48f6929ceda1585bcae2489a0b01f0f2c5fb:backend/app/services/deepseek_responses_runtime.py"],
        text=True,
    )
    class_node = next(
        node for node in ast.parse(source).body
        if isinstance(node, ast.ClassDef) and node.name == "DeepSeekResponsesRuntime"
    )
    method = next(
        node for node in class_node.body
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "_call_compactor"
    )
    namespace = dict(module.__dict__)
    exec(compile(ast.fix_missing_locations(ast.Module(body=[method], type_ignores=[])),
                 "<trusted-original-compactor>", "exec"), namespace)
    module.DeepSeekResponsesRuntime._call_compactor = namespace["_call_compactor"]
    return pytest.main([
        "--no-cov", "-q", "tests/unit/services/test_responses_context_token_budget.py",
        "-k", "rechecks_added_validation_retry",
    ])


if __name__ == "__main__":
    raise SystemExit(main())
