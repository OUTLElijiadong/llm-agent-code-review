"""只读重算生产HTTPS夹具验收回执，不重发任何请求。"""
import collections
import hashlib
import json
import re
from pathlib import Path

HERE = Path(__file__).resolve().parent


def main():
    path = HERE / "生产HTTPS权限与数据闭环.json"
    data = json.loads(path.read_text())
    rows = data["requests"]
    assert data["status"] == "passed" and data["phase"] == "fixtures"
    assert data["base_url"] == "https://lijiadong.cn"
    assert len(rows) == data["summary"]["passed"] == 92
    assert len({row["request_id"] for row in rows}) == 92
    assert len({row["response_request_id"] for row in rows}) == 92
    assert all(row["status"] == "passed" and row["actual"] == row["expected"] for row in rows)
    assert all(re.fullmatch("[a-f0-9]{64}", row["response_sha256"]) for row in rows)
    assert all("payload" not in row and "access_token" not in row and "password" not in row for row in rows)
    counts = collections.Counter(row["actual"] for row in rows)
    assert counts == {200: 57, 404: 27, 403: 7, 401: 1}
    assert len(data["assertions"]) == 38 and all(row["passed"] for row in data["assertions"])
    a, b = data["resources"]["a"], data["resources"]["b"]
    assert (a["project_id"], a["file_id"], a["reviewer_user_id"]) == (162, 1892, 104)
    assert (b["project_id"], b["file_id"]) == (163, 1893)
    assert a["content_sha256"] == hashlib.sha256(b"print('version two')\n").hexdigest()
    assert b["content_sha256"] == hashlib.sha256(b"# QA 20260907 b\nprint('permission acceptance')\n").hexdigest()
    assert a["file_name"] == "permission_renamed.py"
    assert all(row["method"] == "GET" for row in rows if row["path"].startswith(("/api/review/", "/api/reports", "/api/issues")))
    no_permission = [row for row in rows if row["account"] == "no_permission"]
    assert len(no_permission) == 3 and all(row["actual"] == 200 for row in no_permission)
    result = {"status": "passed", "http_requests_sent": 0, "recounted_requests": 92,
              "status_counts": dict(counts), "assertions_passed": 38,
              "resources": data["resources"],
              "scope": "生产fixtures阶段：正向/跨项目/同项目reviewer/旧JWT拒绝；无权限账号此阶段只有登录和自身RBAC读取，完整403矩阵须发布后另验。",
              "source_sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
    (HERE / "独立生产HTTPS夹具复核.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"status": "passed", "requests": 92, "assertions": 38}))


if __name__ == "__main__":
    main()
