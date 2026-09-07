"""专用账号的真实 HTTPS 权限验收；默认只生成/检查计划，--execute 才发送请求。

计划绑定已复核依赖和路由源码 SHA。未知依赖或缺少授权门禁列为 blocked。
执行仅创建两个标记项目/文件及 reviewer 关系，不创建审查、报告或模型任务。
凭据/日志须为本机所有的 0600 普通文件；日志不保存密码、令牌或响应正文。
"""

from __future__ import annotations

import argparse
import collections
import hashlib
import inspect
import json
import os
import re
import ssl
import stat
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ACCOUNTS = ("owner_a", "member_a", "owner_b", "no_permission")
EXPECTED_PERMISSIONS = {
    "project:view",
    "project:create",
    "project:update",
    "project:delete",
    "project:member:manage",
    "file:view",
    "file:upload",
    "file:edit",
    "file:delete",
    "file:download",
    "review:view",
    "review:cancel",
    "issue:view",
    "issue:handle",
    "issue:batch",
    "report:view",
    "report:export:json",
    "report:export:html",
    "security:view",
    "agent:view",
}
READ_DEPENDENCIES = {
    "app.core.database.get_db",
    "app.core.dependencies.get_current_user",
    "app.core.dependencies.require_admin",
    "app.core.dependencies.require_super_admin",
    "app.core.rbac_dependency.require_admin",
    "app.core.rbac_dependency.require_permission.<locals>._dependency",
    "app.api.v1.reports._require_report_export_permission",
}
AUTH = "app.core.dependencies.get_current_user"
GUARDS = READ_DEPENDENCIES - {AUTH, "app.core.database.get_db"}


def sha(data):
    return hashlib.sha256(data).hexdigest()


def private_json(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(descriptor) as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600 or info.st_uid != os.geteuid():
            raise ValueError("必须使用当前账号拥有的 0600 普通文件")
        return json.load(stream)


def write_private(path, value, *, exclusive=False):
    flags = os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW | (os.O_EXCL if exclusive else os.O_TRUNC)
    descriptor = os.open(path, flags, 0o600)
    with os.fdopen(descriptor, "w") as stream:
        os.fchmod(stream.fileno(), 0o600)
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())


def build_plan():
    # 只构造已安装 app 的路由依赖图；不使用 TestClient，不启动 lifespan。
    sys.path.insert(0, str(ROOT))
    from fastapi.routing import APIRoute

    from app.main import app

    sources = {
        "app/main.py",
        "app/api/__init__.py",
        "app/core/database.py",
        "app/core/dependencies.py",
        "app/core/rbac_dependency.py",
        "app/core/security.py",
        "app/core/super_admin.py",
        "app/core/observability.py",
        "app/services/rbac_service.py",
        "app/api/v1/reports.py",
    }
    rows = []

    def ordered(dependant):
        for child in dependant.dependencies:
            yield from ordered(child)
        yield dependant.call

    middleware = {item.cls.__module__ + "." + item.cls.__qualname__ for item in app.user_middleware}
    expected_middleware = {
        "starlette.middleware.cors.CORSMiddleware",
        "app.core.observability.RequestContextMiddleware",
    }
    for route in app.routes:
        if not isinstance(route, APIRoute):
            continue
        calls = list(ordered(route.dependant))[:-1]
        names = [call.__module__ + "." + call.__qualname__ for call in calls]
        # SlowAPI 的 wraps 闭包位于本机虚拟环境；绑定被包装的应用源码才能跨主机核验。
        endpoint_function = inspect.unwrap(route.endpoint)
        endpoint = Path(inspect.getsourcefile(endpoint_function)).resolve().relative_to(ROOT)
        sources.add(str(endpoint))
        for call in calls:
            sources.add(str(Path(inspect.getsourcefile(call)).resolve().relative_to(ROOT)))
        unknown = sorted(set(names) - READ_DEPENDENCIES)
        safe = not unknown and middleware == expected_middleware
        auth_index = names.index(AUTH) if AUTH in names else None
        guard = next((name for name in names if name in GUARDS), None)
        for method in sorted(route.methods):
            rows.append(
                {
                    "method": method,
                    "path": route.path,
                    "endpoint": str(endpoint),
                    "line": inspect.getsourcelines(endpoint_function)[1],
                    "dependency_order": names,
                    "anonymous": "ready" if safe and auth_index is not None else "blocked",
                    "no_permission": "ready" if safe and guard else "blocked",
                    "guard": guard,
                    "unknown_dependencies": unknown,
                }
            )
    return {
        "schema": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "middleware": sorted(middleware),
        "routes": rows,
        "source_sha256": {name: sha((ROOT / name).read_bytes()) for name in sorted(sources)},
        "proof": "FastAPI按依赖树先求子依赖再调用依赖；仅已复核只读身份/RBAC依赖可通过计划。"
        "缺token或无权限在端点调用前抛出；未知依赖和公开路由不发送负向请求。"
        "中间件只有CORS及日志/进程内指标；不声称HTTP日志本身零写入。",
    }


def validate_plan(plan, source_root):
    if plan.get("schema") != 1 or not plan.get("routes") or not plan.get("source_sha256"):
        raise ValueError("计划格式不完整")
    for name, expected in plan["source_sha256"].items():
        path = (source_root / name).resolve()
        if not path.is_relative_to(source_root.resolve()) or sha(path.read_bytes()) != expected:
            raise ValueError(f"计划与执行后端源码不匹配: {name}")
    for row in plan["routes"]:
        if row["method"] not in {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"}:
            raise ValueError("未知HTTP方法")
        if not row["path"].startswith("/") or "//" in row["path"] or "?" in row["path"]:
            raise ValueError("非法路由路径")
        if row["anonymous"] == "ready" and (AUTH not in row["dependency_order"] or row["unknown_dependencies"]):
            raise ValueError("匿名矩阵缺少安全证明")
        if row["no_permission"] == "ready" and (row["guard"] not in GUARDS or row["unknown_dependencies"]):
            raise ValueError("无权限矩阵缺少安全证明")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class Runner:
    def __init__(self, base_url, manifest, plan, output, *, interval=0.15, phase="all"):
        parsed = urllib.parse.urlsplit(base_url)
        if (
            parsed.scheme != "https"
            or not parsed.netloc
            or parsed.path not in {"", "/"}
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("只接受无认证信息、无路径的HTTPS站点地址")
        self.base = base_url.rstrip("/")
        self.manifest, self.plan, self.output = manifest, plan, output
        self.phase = phase
        self.interval, self.tokens = interval, {}
        self.client = urllib.request.build_opener(
            urllib.request.ProxyHandler({}),
            NoRedirect(),
            urllib.request.HTTPSHandler(context=ssl.create_default_context()),
        )
        self.log = {
            "status": "prepared",
            "base_url": self.base,
            "marker": manifest["marker"],
            "phase": phase,
            "started_at": datetime.now(timezone.utc).isoformat(),
            "requests": [],
            "assertions": [],
            "resources": {},
            "blocked": [],
            "plan_sha256": sha(json.dumps(plan, sort_keys=True).encode()),
        }
        write_private(output, self.log, exclusive=True)

    def save(self):
        write_private(self.output, self.log)

    def check(self, condition, label):
        self.log["assertions"].append({"check": label, "passed": bool(condition)})
        self.save()
        if not condition:
            raise RuntimeError(f"内容回显不符合预期: {label}")

    def request(self, account, method, path, expected=200, payload=None, *, purpose="", token=None):
        if not path.startswith("/") or "//" in path:
            raise ValueError("请求路径非法")
        request_id = "qa-" + uuid.uuid4().hex
        headers = {"Accept": "application/json", "X-Request-Id": request_id}
        current_token = token if token is not None else self.tokens.get(account)
        if current_token:
            headers["Authorization"] = "Bearer " + current_token
        body = None if payload is None else json.dumps(payload).encode()
        if body is not None:
            headers["Content-Type"] = "application/json"
        entry = {
            "account": account,
            "method": method,
            "path": path,
            "expected": expected,
            "purpose": purpose,
            "request_id": request_id,
            "status": "pending",
        }
        self.log["requests"].append(entry)
        self.save()  # 变更发出前记录意图；失联时不自动重放POST。
        started = time.monotonic()
        try:
            try:
                response = self.client.open(
                    urllib.request.Request(self.base + path, body, headers, method=method), timeout=20
                )
            except urllib.error.HTTPError as error:
                response = error
            with response:
                status_code = response.code
                data = response.read(2_000_001)
                entry.update(
                    actual=status_code,
                    response_bytes=len(data),
                    response_sha256=sha(data),
                    response_request_id=response.headers.get("X-Request-Id"),
                    elapsed_ms=round((time.monotonic() - started) * 1000),
                )
            entry["status"] = "passed" if status_code == expected else "failed"
            self.save()
            if status_code != expected or len(data) > 2_000_000:
                raise RuntimeError(f"HTTP验收失败: {account} {method} {path} actual={status_code} expected={expected}")
            if expected != 200:
                return None  # 错误正文不解析、不中转、不记录。
            parsed = json.loads(data)
            if parsed.get("code") != 0:
                raise RuntimeError("业务响应code非0")
            return parsed.get("data")
        finally:
            time.sleep(self.interval)

    def login(self, account):
        item = self.manifest["accounts"][account]
        # 登录不复用旧Authorization；password/token均只在内存使用。
        self.tokens.pop(account, None)
        data = self.request(
            account,
            "POST",
            "/api/auth/login",
            payload={"username": item["username"], "password": item["password"]},
            purpose="专用账号真实登录",
        )
        self.check(
            data["user"]["id"] == item["id"]
            and data["user"]["username"] == item["username"]
            and data["user"]["role"] == "user"
            and data["user"]["status"] == 1,
            f"{account}身份精确核对",
        )
        self.tokens[account] = data["access_token"]
        roles = self.request(account, "GET", f"/api/rbac/users/{item['id']}/roles", purpose="自身真实角色")
        permissions = self.request(account, "GET", f"/api/rbac/users/{item['id']}/permissions", purpose="自身真实权限")
        if account == "no_permission":
            self.check(roles == [] and permissions == [], "无权限账号无任何角色和权限")
        else:
            self.check({role["code"] for role in roles} == {self.manifest["role_code"]}, f"{account}仅专用角色")
            self.check(set(permissions) == EXPECTED_PERMISSIONS, f"{account}权限严格等于20个专用权限")

    def negative_matrix(self):
        for account, expected, field in (("anonymous", 401, "anonymous"), ("no_permission", 403, "no_permission")):
            for route in self.plan["routes"]:
                if route[field] != "ready":
                    self.log["blocked"].append(
                        {
                            "account": account,
                            "method": route["method"],
                            "path": route["path"],
                            "reason": "无已证明的前置拒绝门禁或存在未知依赖",
                        }
                    )
                    continue
                path = re.sub(r"\{[^}]+\}", "-1", route["path"])
                self.request(account, route["method"], path, expected, {}, purpose="全路由前置认证/授权拒绝")
        self.save()

    def fixture_loop(self):
        marker = self.manifest["marker"]
        member_id = self.manifest["accounts"]["member_a"]["id"]
        outsider_id = self.manifest["accounts"]["owner_b"]["id"]
        for scope in ("a", "b"):
            account = "owner_" + scope
            name = f"QA权限验收-{marker}-{scope}"
            listing = self.request(
                account, "GET", "/api/projects?keyword=" + urllib.parse.quote(name), purpose="避免重复创建验收项目"
            )
            self.check(listing["total"] == 0, f"{scope}标记项目不存在")
            created = self.request(
                account,
                "POST",
                "/api/projects",
                payload={
                    "project_name": name,
                    "description": f"专用权限验收{marker}；不执行模型",
                    "language": "python",
                },
                purpose="创建专用项目",
            )
            self.log["resources"][scope] = {"project_id": created["id"], "project_name": name}
            self.save()
            content = f"# QA {marker} {scope}\nprint('permission acceptance')\n"
            file = self.request(
                account,
                "POST",
                "/api/code-files",
                payload={
                    "project_id": created["id"],
                    "file_name": "permission_acceptance.py",
                    "language": "python",
                    "content": content,
                },
                purpose="创建普通验收源码",
            )
            self.log["resources"][scope].update(file_id=file["file_id"], content_sha256=sha(content.encode()))
            self.save()
        a, b = self.log["resources"]["a"], self.log["resources"]["b"]
        pa, fa, pb, fb = a["project_id"], a["file_id"], b["project_id"], b["file_id"]
        self.request(
            "owner_a",
            "POST",
            f"/api/projects/{pa}/members",
            payload={"user_id": member_id, "role_in_project": "reviewer"},
            purpose="添加普通reviewer",
        )
        a["reviewer_user_id"] = member_id
        self.save()
        for account, project, file, fixture in (
            ("owner_a", pa, fa, a),
            ("member_a", pa, fa, a),
            ("owner_b", pb, fb, b),
        ):
            listing = self.request(account, "GET", "/api/projects", purpose="项目列表账号隔离")
            self.check(
                {row["id"] for row in listing["items"]} == {project} and listing["total"] == 1, account + "仅本方项目"
            )
            detail = self.request(account, "GET", f"/api/projects/{project}", purpose="项目详情正向")
            self.check(detail["project_name"] == fixture["project_name"], account + "项目内容回显")
            source = self.request(account, "GET", f"/api/code-files/{file}", purpose="源码正向")
            self.check(sha(source["content"].encode()) == fixture["content_sha256"], account + "源码摘要回显")
            self.request(account, "GET", f"/api/code-files/{file}/meta", purpose="源码元数据正向")
            self.request(account, "GET", f"/api/projects/{project}/members", purpose="成员列表正向")
            for route in ("/api/review/tasks", "/api/issues", "/api/reports"):
                rows = self.request(
                    account, "GET", route + f"?project_id={project}", purpose="空任务/问题/报告列表正向"
                )
                self.check(rows["total"] == 0 and rows["items"] == [], account + route + "没有伪造任务")
        cases = [
            ("GET", "/api/projects/{p}", None),
            ("PUT", "/api/projects/{p}", {"description": "不应写入"}),
            ("DELETE", "/api/projects/{p}", None),
            ("GET", "/api/projects/{p}/members", None),
            ("POST", "/api/projects/{p}/members", {"user_id": member_id, "role_in_project": "reviewer"}),
            ("GET", "/api/code-files?project_id={p}", None),
            ("GET", "/api/code-files/{f}", None),
            ("GET", "/api/code-files/{f}/meta", None),
            ("PUT", "/api/code-files/{f}", {"content": "不应写入"}),
            ("POST", "/api/code-files/{f}/rename", {"file_name": "forbidden.py"}),
            ("DELETE", "/api/code-files/{f}", None),
            ("GET", "/api/code-files/{f}/versions", None),
            ("POST", "/api/code-files", {"project_id": pb, "file_name": "forbidden.py", "content": ""}),
        ]
        for account in ("owner_a", "member_a"):
            for method, path, payload in cases:
                self.request(account, method, path.format(p=pb, f=fb), 404, payload, purpose="跨项目真实资源拒绝")
        for method, path, payload in cases:
            if method == "GET":
                continue
            own_payload = dict(payload) if payload else None
            if own_payload and "project_id" in own_payload:
                own_payload["project_id"] = pa
            if own_payload and "user_id" in own_payload:
                own_payload["user_id"] = outsider_id
            self.request(
                "member_a", method, path.format(p=pa, f=fa), 403, own_payload, purpose="同项目reviewer写入拒绝"
            )
        for account, project, file, fixture in (("owner_a", pa, fa, a), ("owner_b", pb, fb, b)):
            state = self.request(account, "GET", f"/api/projects/{project}", purpose="拒绝后项目保持")
            source = self.request(account, "GET", f"/api/code-files/{file}", purpose="拒绝后源码保持")
            self.check(
                state["status"] == "active"
                and state["description"] == f"专用权限验收{marker}；不执行模型"
                and source["file_name"] == "permission_acceptance.py"
                and sha(source["content"].encode()) == fixture["content_sha256"],
                account + "拒绝没有改变资源",
            )
        self.request(
            "owner_a",
            "PUT",
            f"/api/projects/{pa}",
            payload={"description": f"专用权限验收{marker}；更新回显"},
            purpose="owner正向修改",
        )
        self.request(
            "owner_a",
            "PUT",
            f"/api/code-files/{fa}",
            payload={"content": "print('version two')\n", "change_desc": "权限验收版本2"},
            purpose="owner源码真实版本2",
        )
        self.request(
            "owner_a",
            "POST",
            f"/api/code-files/{fa}/rename",
            payload={"file_name": "permission_renamed.py"},
            purpose="owner真实重命名",
        )
        versions = self.request("owner_a", "GET", f"/api/code-files/{fa}/versions", purpose="两个版本持久化")
        self.check(
            versions["total"] == 2 and {row["version_no"] for row in versions["items"]} == {1, 2}, "真实版本1和2"
        )
        self.request("owner_a", "DELETE", f"/api/projects/{pa}/members/{member_id}", purpose="撤销专用成员关系")
        self.request("member_a", "GET", f"/api/code-files/{fa}", 404, purpose="撤权立即拒绝")
        self.request(
            "owner_a",
            "POST",
            f"/api/projects/{pa}/members",
            payload={"user_id": member_id, "role_in_project": "reviewer"},
            purpose="恢复reviewer供浏览器验收",
        )
        old = self.tokens["member_a"]
        self.login("member_a")
        self.request("member_a", "GET", "/api/auth/me", 401, token=old, purpose="重登撤销旧JWT")
        source = self.request("member_a", "GET", f"/api/code-files/{fa}", purpose="重登权限与数据持久化")
        self.check(
            source["content"] == "print('version two')\n" and source["file_name"] == "permission_renamed.py",
            "重登后普通成员读取版本2",
        )
        a["content_sha256"] = sha(source["content"].encode())
        a["file_name"] = source["file_name"]

    def run(self):
        try:
            self.log["status"] = "running"
            for account in ACCOUNTS:
                self.login(account)
            if self.phase in {"matrix", "all"}:
                self.negative_matrix()
            if self.phase in {"fixtures", "all"}:
                self.fixture_loop()
            self.log["status"] = "passed"
        except Exception as error:
            self.log["status"] = "failed"
            # HTTP/JSON原文和异常repr可能含凭据，不记录它们。
            self.log["failure_type"] = type(error).__name__
            raise
        finally:
            self.log["finished_at"] = datetime.now(timezone.utc).isoformat()
            self.log["summary"] = dict(collections.Counter(row["status"] for row in self.log["requests"]))
            self.save()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--build-plan", action="store_true")
    parser.add_argument("--plan", type=Path)
    parser.add_argument("--phase", choices=("fixtures", "matrix", "all"), default="all")
    parser.add_argument("--source-root", type=Path, default=ROOT)
    parser.add_argument("--credentials", type=Path)
    parser.add_argument("--base-url")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    if args.build_plan:
        if not args.plan:
            parser.error("生成计划必须指定plan")
        if args.execute:
            parser.error("生成计划与执行必须分离")
        plan = build_plan()
        write_private(args.plan, plan, exclusive=True)
        print(
            json.dumps(
                {
                    "routes": len(plan["routes"]),
                    "anonymous_ready": sum(row["anonymous"] == "ready" for row in plan["routes"]),
                    "no_permission_ready": sum(row["no_permission"] == "ready" for row in plan["routes"]),
                }
            )
        )
        return
    plan = None
    if args.phase != "fixtures":
        if not args.plan:
            parser.error("matrix/all必须提供与发布源码一致的plan")
        plan = private_json(args.plan)
        validate_plan(plan, args.source_root)
    if not args.execute:
        print(
            json.dumps(
                {"status": "fixtures_ready" if args.phase == "fixtures" else "plan_verified", "requests_sent": 0}
            )
        )
        return
    if not args.credentials or not args.base_url or not args.output:
        parser.error("执行要求credentials、base-url、output")
    manifest = private_json(args.credentials)
    marker = manifest.get("marker", "")
    if not marker.isalnum() or len(marker) > 16 or manifest.get("role_code") != f"qa_permission_{marker}":
        raise ValueError("凭据标记非法")
    if set(manifest.get("accounts", {})) != set(ACCOUNTS):
        raise ValueError("专用账号集合不匹配")
    for account, item in manifest["accounts"].items():
        if item["username"] != f"qa_{marker}_{account}" or not isinstance(item["id"], int) or item["id"] <= 0:
            raise ValueError("专用账号身份非法")
    runner = Runner(args.base_url, manifest, plan, args.output, phase=args.phase)
    runner.run()
    print(
        json.dumps(
            {"status": runner.log["status"], "summary": runner.log["summary"], "blocked": len(runner.log["blocked"])}
        )
    )


if __name__ == "__main__":
    main()
