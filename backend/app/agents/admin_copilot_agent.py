"""真实调用 LLM 的管理员副驾驶与受限 Agent 委派适配器。"""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy.orm import Session

from app.agents.base import AgentContext, AgentResult, BaseAgent
from app.models.user import User
from app.utils.api_resolver import resolve_api_config

MANAGER_SYSTEM_PROMPT = """你是棱镜 Prism 管理副驾驶，是管理员的总调度 Agent。
所有状态和数字只允许使用输入中的事实快照，不得编造。你可以选择一个已启用 Agent 委派任务。
只输出 JSON：
{"mode":"answer|delegate","answer":"中文结论","agent_code":"目标编码或空","task":"给目标的具体任务"}
规则：管理查询结论先行；需要专业 Agent 时 mode=delegate；目标必须来自可用 Agent 清单；
answer 不超过 400 个中文字符，task 不超过 200 个中文字符；缺少事实时明确说没有查到；
不得输出密钥、用户私有代码或个人知识库内容；不得在这里执行写操作。"""


class AdminCopilotAgent(BaseAgent):
    name = "manager"
    description = "管理员总调度与治理副驾驶"
    icon = "manager"
    color = "#006EFF"
    category = "governance"
    skills = ("管理意图规划", "Agent 委派", "事实归纳")

    def __init__(self):
        # DeepSeek 推理模型的 max_tokens 同时覆盖隐藏推理与最终 JSON，
        # 过小会在 answer 字符串中途截断，导致合法调用被误判为解析失败。
        super().__init__(system_prompt=MANAGER_SYSTEM_PROMPT, temperature=0.1, max_tokens=4096)

    def plan(
        self,
        db: Session,
        admin: User,
        *,
        message: str,
        history: list[dict[str, str]],
        snapshot: dict[str, Any],
        agents: list[dict[str, Any]],
        trace_id: str,
    ) -> AgentResult:
        ctx = AgentContext(user_id=admin.id, extra={"trace_id": trace_id, "source": "admin_copilot"})
        prompt = json.dumps(
            {
                "管理员问题": message,
                "最近对话": history[-12:],
                "事实快照": snapshot,
                "可用Agent": agents,
            },
            ensure_ascii=False,
            default=str,
        )
        api_config = resolve_api_config(db, None)
        result = self.call_json(prompt, ctx, api_config=api_config)
        if not result.success and result.failure_kind in {"invalid_json", "output_truncated"}:
            compact_prompt = json.dumps(
                {
                    "管理员问题": message[:500],
                    "事实快照": snapshot,
                    "可用Agent": agents,
                    "纠正要求": "只输出一行完整 JSON；answer 最多 200 字；不要 Markdown。",
                },
                ensure_ascii=False,
                default=str,
            )
            result = self.call_json(
                compact_prompt,
                ctx,
                api_config=api_config,
            )
        self._log_call(
            db,
            user_id=admin.id,
            result=result,
            status="success" if result.success else "failed",
            error=result.error,
            user_prompt=prompt,
            response_text=json.dumps(result.data, ensure_ascii=False, default=str) if result.success else "",
        )
        return result


class DelegatedAdminAgent(BaseAgent):
    """把治理画像转成可真实调用、可审计的请求级 Agent。"""

    def __init__(self, *, code: str, name: str, system_prompt: str):
        self.name = code
        self.description = name
        self.category = "delegated"
        super().__init__(system_prompt=system_prompt, temperature=0.15, max_tokens=4096)

    def run(
        self,
        db: Session,
        admin: User,
        *,
        task: str,
        snapshot: dict[str, Any],
        trace_id: str,
    ) -> AgentResult:
        ctx = AgentContext(
            user_id=admin.id,
            extra={"trace_id": trace_id, "source": "manager_delegate", "agent_code": self.name},
        )
        prompt = (
            "管理员通过管理副驾驶委派你完成以下任务。只能依据给定事实；缺少输入时提出明确问题。"
            "不得执行写操作，也不得访问用户私有内容。只输出面向管理员的最终结论，中文不超过 600 字，"
            "不得输出分析过程、思维链或 reasoning_content。\n\n"
            f"任务：{task}\n事实快照：{json.dumps(snapshot, ensure_ascii=False, default=str)}"
        )
        result = self.call(prompt, ctx, api_config=resolve_api_config(db, None))
        self._log_call(
            db,
            user_id=admin.id,
            result=result,
            status="success" if result.success else "failed",
            error=result.error,
            user_prompt=prompt,
            response_text=str(result.data or "") if result.success else "",
        )
        return result
