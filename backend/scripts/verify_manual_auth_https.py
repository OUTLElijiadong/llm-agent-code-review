"""补测8个在handler/service内鉴权的接口；仅无凭据请求，默认不发送HTTP。"""

import argparse
import collections
import importlib.util
import json
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUNNER_PATH = ROOT / "scripts/verify_permission_acceptance_https.py"
SPEC = importlib.util.spec_from_file_location("manual_auth_http_runner", RUNNER_PATH)
http_runner = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(http_runner)

CASES = (
    ("GET", "/api/agents/events", "缺token在_resolve_sse_token拒绝，先于事件总线订阅"),
    ("GET", "/api/sandboxes/-1/preview/-1", "空预览cookie解码失败，先于worker代理"),
    ("HEAD", "/api/sandboxes/-1/preview/-1", "空预览cookie解码失败，先于worker代理"),
    ("POST", "/api/sandboxes/-1/preview/-1", "空预览cookie解码失败，先于worker代理"),
    ("POST", "/v1/responses", "空Authorization在_bearer_identity拒绝，先于上游/存储"),
    ("GET", "/v1/responses/-1", "空Authorization在_bearer_identity拒绝，先于存储读取"),
    ("DELETE", "/v1/responses/-1", "空Authorization在_bearer_identity拒绝，先于存储删除"),
    ("GET", "/v1/responses/-1/input_items", "空Authorization在_bearer_identity拒绝，先于存储读取"),
)
SOURCES = (
    "app/main.py",
    "app/core/database.py",
    "app/core/dependencies.py",
    "app/core/security.py",
    "app/core/observability.py",
    "app/api/v1/agents.py",
    "app/api/v1/sandboxes.py",
    "app/services/sandbox_service.py",
    "app/api/responses.py",
    "app/services/deepseek_responses_service.py",
    "scripts/verify_permission_acceptance_https.py",
)


def build_plan(source_root):
    return {
        "schema": 1,
        "cases": [list(case) for case in CASES],
        "created_at": datetime.now(timezone.utc).isoformat(),
        "source_sha256": {name: http_runner.sha((source_root / name).read_bytes()) for name in SOURCES},
        "identity": "无Authorization、无Cookie、无APIKey；与用户JWT/RBAC凭据不同，不尝试正向付费请求",
    }


def validate_plan(plan, source_root):
    if plan.get("schema") != 1 or plan.get("cases") != [list(case) for case in CASES]:
        raise ValueError("补充计划必须精确包含8条已审请求")
    if set(plan.get("source_sha256", {})) != set(SOURCES):
        raise ValueError("补充计划源码集合不完整")
    for name, expected in plan["source_sha256"].items():
        if http_runner.sha((source_root / name).read_bytes()) != expected:
            raise ValueError(f"补充计划与执行源码不匹配: {name}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--build-plan", action="store_true")
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--source-root", type=Path, default=ROOT)
    parser.add_argument("--base-url")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    if args.build_plan:
        if args.execute:
            parser.error("生成计划与执行必须分离")
        http_runner.write_private(args.plan, build_plan(args.source_root), exclusive=True)
        print(json.dumps({"status": "plan_created", "anonymous_cases": 8, "requests_sent": 0}))
        return
    plan = http_runner.private_json(args.plan)
    validate_plan(plan, args.source_root)
    if not args.execute:
        print(json.dumps({"status": "plan_verified", "requests_sent": 0}))
        return
    if not args.base_url or not args.output:
        parser.error("执行必须指定base-url和output")
    runner = http_runner.Runner(
        args.base_url, {"marker": "anonymous20260907"}, plan, args.output, phase="anonymous_supplemental"
    )
    try:
        for method, path, proof in CASES:
            runner.request("anonymous", method, path, 401, {} if method == "POST" else None, purpose=proof)
        runner.log["status"] = "passed"
    except Exception as error:
        runner.log["status"] = "failed"
        runner.log["failure_type"] = type(error).__name__
        raise
    finally:
        runner.log["finished_at"] = datetime.now(timezone.utc).isoformat()
        runner.log["summary"] = dict(collections.Counter(row["status"] for row in runner.log["requests"]))
        runner.save()
    print(json.dumps({"status": "passed", "anonymous_cases": 8}))


if __name__ == "__main__":
    main()
