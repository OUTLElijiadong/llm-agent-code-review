"""真实Runner配内存HTTPS替身：验证精确原描述、意外200、前后字段与源码保护。"""
import copy
import importlib.util
import io
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

FOLDER = Path(__file__).resolve().parent
ROOT = FOLDER.parents[2] / "backend"
SPEC = importlib.util.spec_from_file_location("member_write_executor", FOLDER / "既有QA成员原描述写拒绝.py")
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)
CORE = MODULE.load_core(ROOT)


def manifest():
    return {"marker": "20260908", "role_code": "qa_permission_20260908", "accounts": {
        key: {"id": uid, "username": f"qa_20260908_{key}", "password": "offline-secret-password"}
        for key, uid in MODULE.ACCOUNTS.items()
    }}


class Response(io.BytesIO):
    def __init__(self, data, status=200):
        super().__init__(json.dumps({"code": 0, "data": data}).encode())
        self.code = status
        self.headers = {"X-Request-Id": "offline-id"}


class Client:
    def __init__(self, credentials, *, put_status=403, change=False, description="offline-secret-description", missing_perm=False):
        self.sequence = MODULE.sequence_for(credentials)
        self.calls = []
        self.credentials = credentials
        self.put_status = put_status
        self.change = change
        self.missing_perm = missing_perm
        self.before = {"id": 164, "project_name": "QA权限验收-20260908-a", "status": "active",
                       "can_update": True, "description": description, "update_time": "2026-09-08T00:00:00",
                       "source_revisions": [{"version_no": 2, "nested": ["preserve"]}]}

    def open(self, request, timeout):
        index = len(self.calls)
        account, method, path, _, _ = self.sequence[index]
        self.calls.append((request.method, request.full_url))
        assert request.method == method
        assert request.full_url == "https://offline.invalid" + path
        if path == "/api/auth/login":
            item = self.credentials["accounts"][account]
            assert json.loads(request.data)["password"] == item["password"]
            return Response({"access_token": "offline-secret-token", "user": {
                "id": item["id"], "username": item["username"], "role": "user", "status": 1}})
        if path.endswith("/roles"):
            return Response([{"code": "qa_permission_20260908"}])
        if path.endswith("/permissions"):
            perms = CORE.EXPECTED_PERMISSIONS - ({"project:update"} if self.missing_perm else set())
            return Response(sorted(perms))
        if path.endswith("/members"):
            return Response([{"user_id": 107, "role_in_project": "owner"},
                             {"user_id": 108, "role_in_project": "reviewer"}])
        if method == "PUT":
            assert json.loads(request.data) == {"description": self.before["description"]}
            return Response("offline-secret-body", self.put_status)
        result = copy.deepcopy(self.before)
        if index == 9 and self.change:
            result["update_time"] = "2026-09-12T00:00:00"
        return Response(result)


class MemberWriteContracts(unittest.TestCase):
    def run_executor(self, **kwargs):
        credentials = manifest()
        client = Client(credentials, **kwargs)
        with tempfile.TemporaryDirectory(prefix="prism-member-write-offline-") as folder:
            output = Path(folder) / "result.json"
            with patch.object(CORE.urllib.request, "build_opener", return_value=client), patch.object(CORE.time, "sleep"):
                result = MODULE.execute(CORE, "https://offline.invalid", credentials, {"schema": 2}, output)
            raw = output.read_text()
            self.assertEqual(output.stat().st_mode & 0o777, 0o600)
            for secret in ("offline-secret-password", "offline-secret-token", "offline-secret-description", "offline-secret-body"):
                self.assertNotIn(secret, raw)
            return result, json.loads(raw), client.calls

    def test_original_description_403_passes_exactly_ten_requests(self):
        result, log, calls = self.run_executor()
        self.assertEqual(result["status"], "passed")
        self.assertEqual(len(calls), 10)
        self.assertEqual(sum(method == "PUT" for method, _ in calls), 1)
        self.assertEqual(log["resources"]["before_sha256"], log["resources"]["after_sha256"])
        self.assertEqual(log["resources"]["changed_fields"], [])

    def test_unexpected_200_is_failed_even_without_change_and_only_reads_after(self):
        result, log, calls = self.run_executor(put_status=200)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(log["requests"][8]["actual"], 200)
        self.assertEqual(log["requests"][8]["status"], "failed")
        self.assertEqual(calls[-1], ("GET", "https://offline.invalid/api/projects/164"))
        self.assertEqual(sum(method == "PUT" for method, _ in calls), 1)
        self.assertEqual(log["resources"]["before_sha256"], log["resources"]["after_sha256"])

    def test_unexpected_200_and_updated_timestamp_preserve_diff_not_repair(self):
        result, log, calls = self.run_executor(put_status=200, change=True)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(log["resources"]["changed_fields"], ["update_time"])
        self.assertNotEqual(log["resources"]["before_sha256"], log["resources"]["after_sha256"])
        self.assertEqual(len(calls), 10)

    def test_bad_original_description_stops_before_any_put(self):
        for description in (None, 7, "x" * 501):
            with self.subTest(description_type=type(description).__name__):
                result, _, calls = self.run_executor(description=description)
                self.assertEqual(result["status"], "failed")
                self.assertEqual(len(calls), 7)
                self.assertNotIn("PUT", [method for method, _ in calls])

    def test_missing_project_update_permission_stops_before_resource_access(self):
        result, _, calls = self.run_executor(missing_perm=True)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(len(calls), 3)

    def test_non_whitelisted_write_is_rejected_before_network(self):
        def bad_login(runner, account):
            runner.request(account, "DELETE", MODULE.PROJECT, 200)
        with patch.object(CORE.Runner, "login", bad_login):
            result, _, calls = self.run_executor()
        self.assertEqual(result["status"], "failed")
        self.assertEqual(calls, [])

    def test_reviewed_source_hash_change_stops_before_import_or_requests(self):
        MODULE.validate_sources(ROOT)
        with tempfile.TemporaryDirectory(prefix="prism-source-digest-offline-") as folder:
            root = Path(folder)
            for name in MODULE.SOURCE_SHA256:
                (root / name).parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(ROOT / name, root / name)
            MODULE.validate_sources(root)
            (root / "app/services/project_service.py").write_text("tampered")
            with self.assertRaisesRegex(ValueError, "reviewed_source_changed"):
                MODULE.validate_sources(root)

    def test_default_dry_run_does_not_read_credentials_or_send_requests(self):
        with tempfile.TemporaryDirectory() as folder:
            args = ["executor", "--source-root", str(ROOT), "--plan", str(Path(folder) / "plan"),
                    "--credentials", str(Path(folder) / "must-not-open")]
            with patch.object(MODULE, "load_core", return_value=CORE), patch.object(CORE, "private_json", return_value={}) as read, \
                 patch.object(CORE, "validate_plan") as validate, patch.object(CORE.urllib.request, "build_opener") as network, \
                 patch("sys.argv", args), patch("sys.stdout", new_callable=io.StringIO):
                self.assertEqual(MODULE.main(), 0)
            self.assertEqual(read.call_count, 1)
            self.assertEqual(read.call_args.args[0], Path(folder) / "plan")
            validate.assert_called_once()
            network.assert_not_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)
