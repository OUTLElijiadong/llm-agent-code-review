"""导出 Agent 契约的审阅文档与机器可读快照。"""

# ruff: noqa: E402
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.agents.contracts import CONTRACTS, PROTECTED_AGENT_CODES, validate_contract_catalog


def build_markdown() -> str:
    lines = [
        "# Agent 职责边界、专属 Skill 与协作协议",
        "",
        "本文件由 `backend/scripts/export_agent_contracts.py` 从运行时唯一契约源生成。",
        "聊天助手 `chat_assistant` 与管理 Agent `manager` 仅登记现状，不注入提示词、",
        "不覆盖治理配置；其余 28 个 Agent/服务画像进入职责、Skill 与工具边界治理。",
        "",
        "## 架构口径",
        "",
        "- 14 个 `BaseAgent` 是实际运行 Agent。",
        "- 5 个 general/security/performance/maintainability/reliability 是审查策略视角，不提升为运行 Agent。",
        "- 16 个治理画像是确定性 service adapter，不伪装成 LLM Agent。",
        "- 专属领域 Skill 只归属一个 Agent；`invocable=false`，不自动变成可调用 LLM 工具。",
        "- 自进化 Skill 只允许生成候选和只读反思；应用、回滚由管理员审批接口独占。",
        "",
        "## 消息协议",
        "",
        "跨 Agent 消息使用 schema_version=1.0，字段为 `id/role/sent_from/send_to/",
        "message_type/cause_by/correlation_id/content/payload/artifacts/errors/metadata/timestamp`。",
        "定向消息的目标必须已注册，已治理 Agent 的委派必须同时满足发送方 `delegates_to` 与",
        "接收方 `accepts_from`；未知目标和单向声明均拒绝。`metadata.trace_id` 在环境入口补齐。",
        "",
        "## Agent 总览",
        "",
        "| Agent | 名称 | 模式 | 专属 Skill 数 | 保护状态 |",
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
        "protected_agents": sorted(PROTECTED_AGENT_CODES),
        "agents": [asdict(item) | {"system_prompt": item.system_prompt()} for item in CONTRACTS.values()],
    }
    return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--markdown", type=Path, required=True)
    parser.add_argument("--json", type=Path, required=True)
    args = parser.parse_args()
    validate_contract_catalog()
    args.markdown.parent.mkdir(parents=True, exist_ok=True)
    args.json.parent.mkdir(parents=True, exist_ok=True)
    args.markdown.write_text(build_markdown(), encoding="utf-8")
    args.json.write_text(build_json(), encoding="utf-8")
    print(
        json.dumps(
            {
                "contracts": len(CONTRACTS),
                "protected": sorted(PROTECTED_AGENT_CODES),
                "markdown": str(args.markdown),
                "json": str(args.json),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
