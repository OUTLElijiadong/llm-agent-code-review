"""导出或只读校验静态职责契约的审阅文档与机器可读快照。"""

from __future__ import annotations

import argparse
import json
import runpy
from collections import Counter
from dataclasses import asdict
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
_catalog = runpy.run_path(str(BACKEND_ROOT / "app" / "agents" / "contracts.py"))
CONTRACTS = _catalog["CONTRACTS"]
validate_contract_catalog = _catalog["validate_contract_catalog"]


def protected_codes() -> list[str]:
    """从当前静态职责契约的保护字段获取清单。"""
    return sorted(contract.code for contract in CONTRACTS.values() if contract.protected)


def build_markdown() -> str:
    protected = protected_codes()
    mode_counts = Counter(contract.execution_mode for contract in CONTRACTS.values())
    lines = [
        "# Agent 静态职责契约、专属 Skill 与协作协议",
        "",
        "本文件由 `backend/scripts/export_agent_contracts.py` 从 `app/agents/contracts.py` 唯一契约源生成。",
        f"本目录包含 {len(CONTRACTS)} 份静态职责契约，其中 {len(protected)} 份受保护、"
        f"{len(CONTRACTS) - len(protected)} 份受治理。",
        f"受保护契约：{', '.join(f'`{code}`' for code in protected) or '无'}。",
        "受保护契约仅登记现状，不注入提示词、不覆盖治理配置；其余契约描述职责、Skill 与工具边界。",
        "",
        "## 架构口径",
        "",
        "按 `execution_mode` 字段统计静态职责契约：",
        "",
        *[f"- `{mode}`：{count} 份静态职责契约。" for mode, count in sorted(mode_counts.items())],
        "",
        "上述数量不是模型数量、运行实例数量或已执行 Agent 数量；执行模式标签不能证明实际调用。",
        "审查策略视角不因出现在提示词中而成为新的静态职责契约。",
        "",
        "- 专属领域 Skill 只归属一个 Agent；`invocable=false`，不自动变成可调用 LLM 工具。",
        "- 自进化 Skill 只允许生成候选和只读反思；应用、回滚由管理员审批接口独占。",
        "",
        "## 生成与只读校验",
        "",
        "生成时使用原有 `--markdown` 和 `--json` 参数；追加 `--check` 只比较文件字节，不写入或创建目录。",
        "两个产物均匹配时退出码为 0；任一缺失、无法读取或字节不同则非零退出。",
        "",
        "## 消息协议",
        "",
        "跨 Agent 消息使用 schema_version=1.0，字段为 `id/role/sent_from/send_to/",
        "message_type/cause_by/correlation_id/content/payload/artifacts/errors/metadata/timestamp`。",
        "定向消息的目标必须已注册，已治理 Agent 的委派必须同时满足发送方 `delegates_to` 与",
        "接收方 `accepts_from`；未知目标和单向声明均拒绝。`metadata.trace_id` 在环境入口补齐。",
        "",
        "## 静态职责契约总览",
        "",
        "| 契约标识 | 名称 | 执行模式字段 | 专属 Skill 数 | 保护状态 |",
        "|---|---|---|---:|---|",
    ]
    for contract in CONTRACTS.values():
        lines.append(
            f"| `{contract.code}` | {contract.name} | `{contract.execution_mode}` | "
            f"{len(contract.skills)} | {'不改动' if contract.protected else '受治理'} |"
        )

    lines.extend(["", "## 完整系统提示词", ""])
    for contract in CONTRACTS.values():
        application = (
            "仅文档化既有行为，不注入运行时" if contract.protected else "与原生业务提示词组合或由确定性服务执行"
        )
        lines.extend(
            [
                f"### {contract.code} - {contract.name}",
                "",
                f"- 执行模式：`{contract.execution_mode}`",
                f"- 接收来源：{', '.join(f'`{item}`' for item in contract.accepts_from) or '无'}",
                f"- 委派目标：{', '.join(f'`{item}`' for item in contract.delegates_to) or '无'}",
                f"- 应用方式：{application}",
                "",
                "```text",
                contract.system_prompt(),
                "```",
                "",
            ]
        )
    return "\n".join(lines).rstrip() + "\n"


def build_json() -> str:
    payload = {
        "schema_version": "1.0",
        "contract_count": len(CONTRACTS),
        "protected_agents": protected_codes(),
        "agents": [asdict(item) | {"system_prompt": item.system_prompt()} for item in CONTRACTS.values()],
    }
    return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--markdown", type=Path, required=True)
    parser.add_argument("--json", type=Path, required=True)
    parser.add_argument("--check", action="store_true", help="只读比较静态职责契约产物，缺失或字节不同时非零退出")
    args = parser.parse_args()
    validate_contract_catalog()
    outputs = ((args.markdown, build_markdown()), (args.json, build_json()))
    summary = {
        "contracts": len(CONTRACTS),
        "protected": protected_codes(),
        "markdown": str(args.markdown),
        "json": str(args.json),
    }
    differences = []
    for destination, content in outputs:
        if args.check:
            try:
                actual = destination.read_bytes()
            except FileNotFoundError:
                differences.append({"path": str(destination), "status": "missing"})
            except OSError:
                differences.append({"path": str(destination), "status": "unreadable"})
            else:
                if actual != content.encode("utf-8"):
                    differences.append({"path": str(destination), "status": "different"})
        else:
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(content, encoding="utf-8")
    if args.check:
        summary["differences"] = differences
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    return int(bool(differences))


if __name__ == "__main__":
    raise SystemExit(main())
