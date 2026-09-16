"""仅内存 fake；不导入应用、不读凭据、不访问网络或生产数据库。"""
import hashlib
import importlib.util
from pathlib import Path
import unittest

PATH = Path(__file__).with_name("既有QA读取隔离复验.py")
SPEC = importlib.util.spec_from_file_location("qa_read_isolation", PATH)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class FakeRunner:
    def __init__(self, *, leak=False, writable=False, mutate=False):
        self.log = {}
        self.calls = []
        self.leak, self.writable, self.mutate = leak, writable, mutate
        self.source_reads = 0

    def check(self, condition, label):
        if not condition:
            raise AssertionError(label)

    def request(self, account, method, path, expected=200, payload=None, **kwargs):
        MODULE.guard_request(account, method, path, payload)
        self.calls.append((account, method, path, expected))
        if expected != 200:
            if self.leak:
                raise RuntimeError("模拟错误的200资源泄漏；实际Runner会拒绝状态不符")
            return None
        a = account in {"owner_a", "member_a"}
        project, file, version = (164, 1894, 2) if a else (165, 1895, 1)
        if path == "/api/projects":
            return {"total": 1, "items": [{"id": project}]}
        if path == f"/api/projects/{project}":
            can = account != "member_a" or self.writable
            return {"id": project, "status": "active", "active_file_count": 1,
                    "can_update": can, "can_delete": can}
        if path.endswith("/members"):
            return ([{"user_id": 107, "role_in_project": "owner"},
                     {"user_id": 108, "role_in_project": "reviewer"}] if a
                    else [{"user_id": 109, "role_in_project": "owner"}])
        if path == f"/api/code-files?project_id={project}":
            return {"total": 1, "items": [{"id": file}]}
        if path == f"/api/code-files/{file}":
            self.source_reads += 1
            content = "fixture" if not self.mutate or self.source_reads <= 3 else "changed"
            return {"id": file, "project_id": project, "status": "active",
                    "version_no": version, "content": content}
        if path.endswith("/meta"):
            return {"id": file, "version_no": version}
        if path.endswith("/versions"):
            return {"total": version, "items": [{"version_no": x} for x in range(1, version + 1)]}
        if path.startswith(("/api/review/tasks?", "/api/issues?", "/api/reports?")):
            return {"total": 0, "items": []}
        raise AssertionError("未模拟的路径 " + path)


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


class ReadOnlyContracts(unittest.TestCase):
    def test_exact_reads_and_no_resource_writes(self):
        runner = FakeRunner()
        MODULE.verify_reads(runner, sha)
        # 30 正向 GET + 18 跨项目 404 + 19 无权限 403 + 12 前后保持 GET。
        self.assertEqual(len(runner.calls), 79)
        self.assertTrue(all(method == "GET" for _, method, _, _ in runner.calls))
        self.assertEqual(sum(expected == 404 for *_, expected in runner.calls), 18)
        self.assertEqual(sum(expected == 403 for *_, expected in runner.calls), 19)
        self.assertEqual(len(runner.log["resources"]), 2)

    def test_mutations_and_unknown_gets_are_blocked_before_network(self):
        for method in ("POST", "PUT", "PATCH", "DELETE"):
            with self.subTest(method=method), self.assertRaises(ValueError):
                MODULE.guard_request("member_a", method, "/api/projects/164", {})
        for path in ("/api/discuss/start", "/api/projects/164/source-archive", "//outside"):
            with self.subTest(path=path), self.assertRaises(ValueError):
                MODULE.guard_request("owner_a", "GET", path, None)

    def test_only_normal_login_payload_is_allowed(self):
        MODULE.guard_request("owner_a", "POST", "/api/auth/login", {"username": "fixture", "password": "fixture"})
        with self.assertRaises(ValueError):
            MODULE.guard_request("owner_a", "POST", "/api/auth/login", {})

    def test_foreign_resource_leak_is_failure(self):
        with self.assertRaises(RuntimeError):
            MODULE.verify_reads(FakeRunner(leak=True), sha)

    def test_reviewer_writable_flag_is_failure(self):
        with self.assertRaises(AssertionError):
            MODULE.verify_reads(FakeRunner(writable=True), sha)

    def test_resource_change_is_failure(self):
        with self.assertRaises(AssertionError):
            MODULE.verify_reads(FakeRunner(mutate=True), sha)


if __name__ == "__main__":
    unittest.main(verbosity=2)
