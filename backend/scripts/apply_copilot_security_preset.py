"""把小菱安全中心策略落到当前数据库。

用途：给管理员小菱（``admin_copilot``）一次性配置"最顶级"口径，或者事故期间拉停机开关。
脚本只调用 ``copilot_security_service``，不手写 SQL，可重复执行（幂等：同样的入参得到同样的策略）。

用法::

    # 看当前策略与态势评分
    python scripts/apply_copilot_security_preset.py --show

    # 套用最顶级预设，并把来源限制在本机与管理网段
    python scripts/apply_copilot_security_preset.py --preset max \
        --allow 127.0.0.1/32 --allow 10.0.0.0/8

    # 只预演，不写库
    python scripts/apply_copilot_security_preset.py --preset max --allow 127.0.0.1/32 --dry-run

    # 事故处置：紧急停机
    python scripts/apply_copilot_security_preset.py --kill-switch on --reason "疑似提示注入投毒"

注意：``--allow`` 一旦填了非空白名单，管理小菱就只接受这些来源的请求。
本机开发/单机部署请至少保留 ``127.0.0.1/32``。
"""

# ruff: noqa: E402
from __future__ import annotations

import argparse
import sys
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.core.database import SessionLocal  # noqa: E402
from app.core.exceptions import AppError  # noqa: E402
from app.services import copilot_security_rules as rules  # noqa: E402
from app.services import copilot_security_service as service  # noqa: E402


# ▎把态势评分打印成人类可读的一段。
def _print_posture(label: str, data: dict) -> None:
    print(f"[{label}] 评分 {data['score']} / 等级 {data['grade']}")
    policy = data["policy"]
    print(f"  mode={policy['mode']} preset={policy['preset']} 停机={'是' if policy['kill_switch'] else '否'}")
    print(f"  IP 白名单={policy['ip_allowlist'] or '（未限制）'}")
    gaps = [(item["title"], item["remediation"]) for item in data["checks"] if not item["passed"]]
    if not gaps:
        print("  全部检查项通过")
        return
    print(f"  未满足 {len(gaps)} 项：")
    for title, remediation in gaps:
        print(f"   - {title}：{remediation}")


# ▎解析命令行参数。
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="配置/查看小菱安全中心策略")
    parser.add_argument("--show", action="store_true", help="只打印当前策略与态势，不修改")
    parser.add_argument(
        "--preset", choices=[*rules.PRESETS, "balanced"],
        help="套用预设（monitor/balanced/strict/max）",
    )
    parser.add_argument(
        "--allow", action="append", default=[],
        metavar="CIDR", help="追加一条来源白名单，可重复；如 127.0.0.1/32 或 10.0.0.0/8",
    )
    parser.add_argument("--tool-allow", default="", help="工具白名单，逗号分隔；留空表示不限制")
    parser.add_argument("--tool-deny", default="", help="工具黑名单，逗号分隔")
    parser.add_argument("--kill-switch", choices=["on", "off"], help="紧急停机开关")
    parser.add_argument("--reason", default="", help="停机原因（写入审计）")
    parser.add_argument("--dry-run", action="store_true", help="只预演，不写库")
    return parser


# ▎把逗号分隔的工具编码拆成列表。
def _split(value: str) -> list[str]:
    return [item.strip() for item in (value or "").split(",") if item.strip()]


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    db = SessionLocal()
    try:
        before = service.posture(db)
        _print_posture("当前", before)
        if args.show:
            return 0

        patch: dict = {}
        if args.allow:
            patch["ip_allowlist"] = list(args.allow)
        if args.tool_allow:
            patch["tool_allowlist"] = _split(args.tool_allow)
        if args.tool_deny:
            patch["tool_denylist"] = _split(args.tool_deny)

        if args.dry_run:
            policy = service.load_policy(db)
            if args.preset:
                merged = rules.resolve_preset(args.preset)
                for key in ("ip_allowlist", "tool_allowlist", "tool_denylist", "kill_switch", "kill_switch_reason"):
                    merged[key] = policy.get(key)
                merged["preset"] = args.preset
                policy = merged
            policy.update(patch)
            preview = {
                "score": None, "grade": "preview", "policy": {
                    "mode": policy["mode"], "preset": policy.get("preset"),
                    "kill_switch": bool(policy.get("kill_switch")),
                    "ip_allowlist": policy.get("ip_allowlist"),
                }, "checks": [],
            }
            print("\n[预演] 将写入以下关键字段（未写库）：")
            for key in ("mode", "preset", "ip_allowlist", "tool_allowlist", "tool_denylist"):
                print(f"  {key} = {policy.get(key)}")
            _ = preview
            return 0

        if args.preset:
            policy = service.apply_preset(db, preset=args.preset, client_ip="cli")
            print(f"\n[写入] 已套用预设 {args.preset}")
        if patch:
            service.save_policy(db, patch=patch, client_ip="cli")
            print(f"[写入] 已更新字段：{', '.join(sorted(patch))}")
        if args.kill_switch:
            service.set_kill_switch(
                db, enabled=args.kill_switch == "on", reason=args.reason, client_ip="cli",
            )
            print(f"[写入] 紧急停机 → {args.kill_switch}")

        after = service.posture(db)
        print()
        _print_posture("应用后", after)
        if after["critical_gaps"]:
            print("\n仍有关键缺口：", ", ".join(after["critical_gaps"]))
        return 0
    except AppError as exc:
        print(f"配置被拒绝：{exc}", file=sys.stderr)
        return 2
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
