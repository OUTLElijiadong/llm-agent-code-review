"""既有QA107/108对项目164原描述有效PUT的403补验；默认dry-run无凭据无请求。"""
from __future__ import annotations

import argparse
import collections
import hashlib
import importlib.util
import json
from datetime import datetime, timezone
from pathlib import Path

ACCOUNTS = {"owner_a": 107, "member_a": 108}
PROJECT = "/api/projects/164"
SOURCE_SHA256 = {
    "scripts/verify_permission_acceptance_https.py": "7fdf72a02e6f612c5a38ff4aba3dee1d6326fb7c7b6f8724d40a3aae0052b5b4",
    "app/api/v1/projects.py": "00cc0215fe54e86c035fdfdfe6ff941717713ad0f2442adf5f38de49c3fa1411",
    "app/schemas/project.py": "7a69e3dcfda62d779b7e5e6dde1ffb0aa4746dc21dc984c00a88114beb18729d",
    "app/services/project_service.py": "e1ffeff1544e0a0fa920689f33b37c20ebe9ce98fbb269fb031f7d229b5a6bb5",
    "app/api/v1/project_members.py": "0d5ec973eb523d2bc9a1732b77ec82e49aa7fe32a81bfbb0efeb1f5b885f49d6",
    "app/services/project_member_service.py": "e5d4923b8ac250451b7464d05bad4b6988c6f29dd34871122bff439dc64a6d8a",
    "app/services/project_source_revision_service.py": "188e63e102bf1b648bc1a95b0318d68987d8ced356903901b4a2148e5d6e0869",
}


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def validate_sources(root):
    # 必须先验证Runner文件，之后才import，避免先运行已被改写的执行器。
    for name, expected in SOURCE_SHA256.items():
        if hashlib.sha256((root / name).read_bytes()).hexdigest() != expected:
            raise ValueError("reviewed_source_changed:" + name)


def load_core(root):
    spec = importlib.util.spec_from_file_location(
        "qa_member_description_core", root / "scripts/verify_permission_acceptance_https.py")
    core = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(core)
    return core


def validate_manifest(manifest):
    if manifest.get("marker") != "20260908" or manifest.get("role_code") != "qa_permission_20260908":
        raise ValueError("wrong_qa_manifest")
    for account, uid in ACCOUNTS.items():
        item = manifest.get("accounts", {}).get(account, {})
        if item.get("id") != uid or item.get("username") != f"qa_20260908_{account}" or not item.get("password"):
            raise ValueError("wrong_qa_identity")


def sequence_for(manifest):
    steps = []
    for account, uid in ACCOUNTS.items():
        item = manifest["accounts"][account]
        steps.append((account, "POST", "/api/auth/login", 200,
                      {"username": item["username"], "password": item["password"]}))
        steps.extend((account, "GET", f"/api/rbac/users/{uid}/{suffix}", 200, None)
                     for suffix in ("roles", "permissions"))
    return steps + [
        ("owner_a", "GET", PROJECT, 200, None),
        ("owner_a", "GET", PROJECT + "/members", 200, None),
        ("member_a", "PUT", PROJECT, 403, None),
        ("owner_a", "GET", PROJECT, 200, None),
    ]


def execute(core, base, manifest, plan, output):
    sequence = sequence_for(manifest)

    class StrictRunner(core.Runner):
        cursor = 0
        original_description = None

        def request(self, account, method, path, expected=200, payload=None, **kwargs):
            if self.cursor >= len(sequence) or kwargs.keys() - {"purpose"}:
                raise ValueError("request_outside_whitelist")
            row = sequence[self.cursor]
            wanted_payload = row[4]
            if self.cursor == 8:
                if not isinstance(self.original_description, str) or len(self.original_description) > 500:
                    raise ValueError("original_description_not_validated")
                wanted_payload = {"description": self.original_description}
            if (account, method, path, expected, payload) != (*row[:4], wanted_payload):
                raise ValueError("request_outside_whitelist")
            self.cursor += 1
            return super().request(account, method, path, expected, payload, **kwargs)

    runner = StrictRunner(base, manifest, plan, output, phase="existing_member_original_description_403")
    runner.log.update(planned_requests=10, reviewed_source_sha256=SOURCE_SHA256,
                      authentication_writes="仅QA107/108各正常登录一次；登录修改认证状态/撤销旧令牌，不并发使用这两会话",
                      expected_audit_write="项目写权限检查会保存允许或拒绝的访问审计；不宣称本次零数据库写入",
                      scope="仅项目164原描述有效PUT一次，预期403；失败不重试，不写源码/版本/删除/历史状态")
    try:
        runner.log["status"] = "running"
        runner.check(len(core.EXPECTED_PERMISSIONS) == 20 and "project:update" in core.EXPECTED_PERMISSIONS,
                     "权限合同包含project:update且精确20项")
        for account in ACCOUNTS:
            runner.login(account)
        before = runner.request("owner_a", "GET", PROJECT, purpose="保留完整项目前态，仅摘要落日志")
        runner.check(isinstance(before, dict) and before.get("id") == 164
                     and before.get("project_name") == "QA权限验收-20260908-a"
                     and before.get("status") == "active" and before.get("can_update") is True,
                     "既有QA项目身份和owner写能力符合固定合同")
        description = before.get("description")
        runner.check(isinstance(description, str) and len(description) <= 500,
                     "原描述为有效字符串且不超过500字；不规范时停止不补写")
        runner.original_description = description
        runner.log["resources"] = {"project_id": 164, "before_sha256": digest(before),
                                   "before_fields_sha256": {k: digest(v) for k, v in before.items()},
                                   "description_chars": len(description),
                                   "description_sha256": digest(description)}
        runner.save()
        members = runner.request("owner_a", "GET", PROJECT + "/members", purpose="确认owner107与项目reviewer108")
        runner.check(isinstance(members, list) and len(members) == 2
                     and {(r.get("user_id"), r.get("role_in_project")) for r in members}
                     == {(107, "owner"), (108, "reviewer")}, "项目成员合同精确核对")
        put_error = None
        try:
            runner.request("member_a", "PUT", PROJECT, 403, {"description": description},
                           purpose="真实reviewer有效原描述PUT应403，禁止把200或422记为通过")
        except Exception as error:
            put_error = type(error).__name__
            runner.log["write_failure_type"] = put_error
        # 这是独立读取后态，不是对失败写请求重试；即使意外200也保全变化证据。
        after = runner.request("owner_a", "GET", PROJECT, purpose="写拒绝尝试后的全字段回显核对")
        changed = sorted(k for k in set(before) | set(after) if before.get(k) != after.get(k)
                         or (k in before) != (k in after))
        runner.log["resources"].update(after_sha256=digest(after),
                                       after_fields_sha256={k: digest(v) for k, v in after.items()},
                                       changed_fields=changed)
        runner.check(before == after, "项目全字段前后完全一致，包含update_time与嵌套字段")
        runner.check(put_error is None, "原描述写尝试严格403，意外200也是安全失败")
        runner.check(runner.cursor == 10 and len(runner.log["requests"]) == 10, "仅执行固定10请求")
        runner.log["status"] = "passed"
    except Exception as error:
        runner.log.update(status="failed", failure_type=type(error).__name__)
    finally:
        runner.log["finished_at"] = datetime.now(timezone.utc).isoformat()
        runner.log["summary"] = dict(collections.Counter(r["status"] for r in runner.log["requests"]))
        runner.log["actual_requests"] = len(runner.log["requests"])
        runner.save()
        runner.tokens.clear()
        runner.original_description = None
    return {k: runner.log[k] for k in ("status", "summary", "actual_requests")}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--credentials", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--base-url", default="https://lijiadong.cn", choices=["https://lijiadong.cn"])
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    root = args.source_root.resolve()
    validate_sources(root)
    core = load_core(root)
    plan = core.private_json(args.plan)
    core.validate_plan(plan, root)
    if not args.execute:
        print(json.dumps({"status": "plan_verified", "requests_sent": 0, "credentials_read": False,
                          "planned_requests": 10, "project_id": 164}))
        return 0
    if not args.credentials or not args.output:
        parser.error("执行必须提供0600凭据文件与独占输出路径")
    manifest = core.private_json(args.credentials)
    validate_manifest(manifest)
    result = execute(core, args.base_url, manifest, plan, args.output)
    print(json.dumps(result))
    return 0 if result["status"] == "passed" else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(json.dumps({"status": "failed", "failure_type": type(error).__name__}))
        raise SystemExit(1) from None
