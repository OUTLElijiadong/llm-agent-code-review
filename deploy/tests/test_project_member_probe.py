"""标准库本地探测测试：坏权限门不能借角色名/版本标签通过回退检查。"""
from __future__ import annotations

import ast
import pathlib
import subprocess
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[2]
PROBE = ROOT / "deploy/lib/project_member_compatibility.py"
SOURCE = ROOT / "backend/app/services/project_member_service.py"


class MemberReaderProbeTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="prism-member-reader-")
        self.root = pathlib.Path(self.temporary.name)
        (self.root / "services").mkdir()
        self.source = self.root / "services/project_member_service.py"
        self.source.write_text(SOURCE.read_text(encoding="utf-8"), encoding="utf-8")
        self.addCleanup(self.temporary.cleanup)

    def expect(self, expected, code=0):
        result = subprocess.run([sys.executable, str(PROBE), str(self.root)], capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, code, result.stderr)
        self.assertEqual(result.stdout.strip(), expected)

    def mutate_function(self, name, body):
        tree = ast.parse(self.source.read_text(encoding="utf-8"))
        fn = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == name)
        fn.body = ast.parse(body).body
        self.source.write_text(ast.unparse(ast.fix_missing_locations(tree)), encoding="utf-8")

    def test_real_current_contract(self):
        self.expect("supported")

    def test_execution_function_name_does_not_prove_denial(self):
        self.mutate_function("require_project_execution", "return require_project_access(db, project_id, user)")
        self.expect("unsupported")

    def test_write_gate_must_deny_viewer(self):
        source = self.source.read_text(encoding="utf-8")
        self.source.write_text(source.replace('frozenset({"admin", "owner"})', 'frozenset({"admin", "owner", "viewer"})'), encoding="utf-8")
        self.expect("unsupported")

    def test_reject_everything_is_not_compatibility(self):
        self.mutate_function("require_project_execution", "raise ForbiddenError('denied')")
        self.expect("unsupported")

    def test_unknown_role_cannot_be_read_as_valid(self):
        self.mutate_function("is_project_member", "return True, 'unknown-role'")
        self.expect("unsupported")

    def test_missing_execution_gate_fails_closed(self):
        tree = ast.parse(self.source.read_text(encoding="utf-8"))
        tree.body = [node for node in tree.body if not (isinstance(node, ast.FunctionDef) and node.name == "require_project_execution")]
        self.source.write_text(ast.unparse(tree), encoding="utf-8")
        self.expect("unsupported")

    def test_missing_source_fails_closed(self):
        self.source.unlink()
        self.expect("", 2)

    def test_invalid_source_fails_closed(self):
        self.source.write_text("invalid Python !", encoding="utf-8")
        self.expect("", 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
