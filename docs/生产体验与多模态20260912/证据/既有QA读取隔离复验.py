"""既有 QA 资源的只读 HTTPS 隔离验收；默认不读凭据、不发送请求。

仅 --execute 允许四次正常登录及白名单 GET；登录会更新认证状态并撤销旧 JWT。
禁止所有资源写入，禁止 fixtures/all。日志复用最终容器的 Runner 脱敏实现。
"""
from __future__ import annotations

import argparse
import collections
import importlib.util
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ACCOUNT_IDS = {"owner_a": 107, "member_a": 108, "owner_b": 109, "no_permission": 110}
FIXTURES = {"a": (164, 1894, 2), "b": (165, 1895, 1)}
SOURCE_SHA256 = {
    "scripts/verify_permission_acceptance_https.py": "7fdf72a02e6f612c5a38ff4aba3dee1d6326fb7c7b6f8724d40a3aae0052b5b4",
    "app/api/v1/projects.py": "00cc0215fe54e86c035fdfdfe6ff941717713ad0f2442adf5f38de49c3fa1411",
    "app/api/v1/project_members.py": "0d5ec973eb523d2bc9a1732b77ec82e49aa7fe32a81bfbb0efeb1f5b885f49d6",
    "app/api/v1/code_files.py": "c5da67b84478650c4741a0cfbca131483df4f902f3f0c6cbc2bd5d3c891978c1",
    "app/services/project_service.py": "e1ffeff1544e0a0fa920689f33b37c20ebe9ce98fbb269fb031f7d229b5a6bb5",
    "app/services/project_member_service.py": "e5d4923b8ac250451b7464d05bad4b6988c6f29dd34871122bff439dc64a6d8a",
    "app/services/code_file_service.py": "c36f0b5a428802c99b7f5f05064fbbe7a04a3d8c61a774ef9e145462ec88a48c",
    "app/services/project_source_revision_service.py": "188e63e102bf1b648bc1a95b0318d68987d8ced356903901b4a2148e5d6e0869",
    "app/schemas/project.py": "7a69e3dcfda62d779b7e5e6dde1ffb0aa4746dc21dc984c00a88114beb18729d",
    "app/schemas/code_file.py": "6a46d4800935d4816a3c4ac839a307262535a94b1a81bd0dd5e9a299e879ad54"
}
NOT_TESTED = [
    "member 对真实项目编辑/代码保存的写入 403：无效 body 在业务角色门禁前返回 422，不能冒充授权验收",
    "owner 正向创建/编辑/重命名/删除及版本写入",
    "成员撤权、恢复与旧 JWT 撤销专项断言",
    "服务重启后的数据/权限持久化",
    "组织实体级隔离（当前仅复验现有项目所有者/成员访问边界）",
    "全路由权限矩阵、WebSocket、浏览器按钮与响应式布局",
]


def load_runner(root):
    path = root / "scripts/verify_permission_acceptance_https.py"
    spec = importlib.util.spec_from_file_location("existing_qa_https_runner", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def paths(project, file):
    return [
        f"/api/projects/{project}",
        f"/api/projects/{project}/members",
        f"/api/code-files?project_id={project}",
        f"/api/code-files/{file}",
        f"/api/code-files/{file}/meta",
        f"/api/code-files/{file}/versions",
    ]


def allowed_get_paths():
    allowed = {"/api/projects"}
    for project, file, _ in FIXTURES.values():
        allowed.update(paths(project, file))
        allowed.update(f"{route}?project_id={project}" for route in
                       ("/api/review/tasks", "/api/issues", "/api/reports"))
    for user_id in ACCOUNT_IDS.values():
        allowed.update(f"/api/rbac/users/{user_id}/{suffix}" for suffix in ("roles", "permissions"))
    return allowed


def guard_request(account, method, path, payload):
    if account not in ACCOUNT_IDS:
        raise ValueError("只允许四个既有 QA 账号")
    if method == "GET" and path in allowed_get_paths() and payload is None:
        return
    if (method == "POST" and path == "/api/auth/login" and isinstance(payload, dict)
            and set(payload) == {"username", "password"}):
        return
    raise ValueError("请求不在只读白名单；仅允许登录 POST 和已复核 GET")


def digest(value, sha):
    return sha(json.dumps(value, ensure_ascii=False, sort_keys=True).encode())


def verify_reads(runner, sha):
    snapshots = {}
    for account, scope in (("owner_a", "a"), ("member_a", "a"), ("owner_b", "b")):
        project, file, version = FIXTURES[scope]
        listing = runner.request(account, "GET", "/api/projects", purpose="项目列表精确隔离")
        runner.check(listing["total"] == 1 and [x["id"] for x in listing["items"]] == [project],
                     account + "仅看见本方一个项目")
        values = {path: runner.request(account, "GET", path, purpose="既有资源正向读取")
                  for path in paths(project, file)}
        detail, members, files, source, meta, versions = values.values()
        runner.check(detail["id"] == project and detail["status"] == "active"
                     and detail["active_file_count"] == 1, account + "项目身份及活跃文件口径")
        writable = account != "member_a"
        runner.check(detail["can_update"] is writable and detail["can_delete"] is writable,
                     account + "读态能力标记正确（不代表实测写请求）")
        expected_members = {(107, "owner"), (108, "reviewer")} if scope == "a" else {(109, "owner")}
        runner.check(len(members) == len(expected_members)
                     and {(x["user_id"], x["role_in_project"]) for x in members} == expected_members,
                     account + "成员关系精确核对")
        runner.check(files["total"] == 1 and [x["id"] for x in files["items"]] == [file],
                     account + "文件列表仅包含本项目文件")
        runner.check(source["id"] == file and source["project_id"] == project
                     and source["status"] == "active" and source["version_no"] == version
                     and isinstance(source["content"], str), account + "文件内容可读且版本正确")
        runner.check(meta["id"] == file and meta["version_no"] == version,
                     account + "元数据与详情一致")
        runner.check(versions["total"] == version
                     and {x["version_no"] for x in versions["items"]} == set(range(1, version + 1)),
                     account + "既有版本列表准确")
        hashed = {path: digest(value, sha) for path, value in values.items()}
        # can_update/can_delete 按角色不同；其余文件、成员、版本回显必须完全一致。
        if account == "member_a":
            runner.check(all(hashed[path] == snapshots["a"][path]
                             for path in paths(project, file)[1:]), "普通成员与 owner 文件/成员/版本回显一致")
        else:
            snapshots[scope] = hashed
        for route in ("/api/review/tasks", "/api/issues", "/api/reports"):
            rows = runner.request(account, "GET", route + f"?project_id={project}", purpose="QA空业务列表")
            runner.check(rows["total"] == 0 and rows["items"] == [], account + route + "无伪造任务/问题/报告")
    for account, foreign_scope in (("owner_a", "b"), ("member_a", "b"), ("owner_b", "a")):
        project, file, _ = FIXTURES[foreign_scope]
        for path in paths(project, file):
            runner.request(account, "GET", path, 404, purpose="跨项目真实资源不可枚举")
    denied = {"/api/projects"}
    for project, file, _ in FIXTURES.values():
        denied.update(paths(project, file))
        denied.update(f"{route}?project_id={project}" for route in
                      ("/api/review/tasks", "/api/issues", "/api/reports"))
    for path in sorted(denied):
        runner.request("no_permission", "GET", path, 403, purpose="无权限真实列表及详情拒绝")
    for scope, before in snapshots.items():
        for path, expected in before.items():
            after = runner.request("owner_" + scope, "GET", path, purpose="只读验收后既有资源保持")
            runner.check(digest(after, sha) == expected, "既有资源前后摘要一致:" + path)
    runner.log["resources"] = {scope: {"project_id": FIXTURES[scope][0], "file_id": FIXTURES[scope][1],
                                      "version_no": FIXTURES[scope][2], "response_sha256": hashes}
                               for scope, hashes in snapshots.items()}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--credentials", type=Path)
    parser.add_argument("--base-url", default="https://lijiadong.cn")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    root = args.source_root.resolve()
    core = load_runner(root)
    for name, expected in SOURCE_SHA256.items():
        if core.sha((root / name).read_bytes()) != expected:
            raise ValueError("已复核读取实现变更，须重审: " + name)
    if not SOURCE_SHA256:
        raise ValueError("未绑定只读业务源码")
    plan = core.private_json(args.plan)
    core.validate_plan(plan, root)
    if not args.execute:
        print(json.dumps({"status": "read_plan_verified", "requests_sent": 0,
                          "not_tested": NOT_TESTED}, ensure_ascii=False))
        return
    if not args.credentials or not args.output:
        parser.error("执行必须指定 credentials 和独占 output 路径")
    manifest = core.private_json(args.credentials)
    if (manifest.get("marker") != "20260908" or manifest.get("role_code") != "qa_permission_20260908"
            or set(manifest.get("accounts", {})) != set(ACCOUNT_IDS)):
        raise ValueError("必须使用已复核 9/8 QA 凭据")
    for account, expected_id in ACCOUNT_IDS.items():
        item = manifest["accounts"][account]
        if item.get("id") != expected_id or item.get("username") != f"qa_20260908_{account}":
            raise ValueError("QA 身份与既有资源绑定不符")

    class ReadOnlyRunner(core.Runner):
        def request(self, account, method, path, expected=200, payload=None, **kwargs):
            guard_request(account, method, path, payload)
            return super().request(account, method, path, expected, payload, **kwargs)

    runner = ReadOnlyRunner(args.base_url, manifest, plan, args.output, phase="existing_read_isolation")
    runner.log.update(not_tested=NOT_TESTED, source_sha256=SOURCE_SHA256,
                      authentication_writes="4次登录会更新认证状态、令牌版本、审计及限流；无业务资源写请求")
    try:
        runner.log["status"] = "running"
        for account in ACCOUNT_IDS:
            runner.login(account)
        verify_reads(runner, core.sha)
        runner.log["status"] = "passed"
    except Exception as error:
        runner.log.update(status="failed", failure_type=type(error).__name__)
        # 不回传异常正文/响应正文，以免泄漏凭据、用户名、邮箱或源码。
    finally:
        runner.log["finished_at"] = datetime.now(timezone.utc).isoformat()
        runner.log["summary"] = dict(collections.Counter(x["status"] for x in runner.log["requests"]))
        runner.save()
    print(json.dumps({"status": runner.log["status"], "summary": runner.log["summary"]}))
    if runner.log["status"] != "passed":
        raise SystemExit(1)


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(json.dumps({"status": "failed", "failure_type": type(error).__name__}))
        sys.exit(1)
