"""沙箱多 Agent 测试审查：白盒/黑盒/对抗复检/报告 四角色 LLM 编排。"""

from __future__ import annotations

import json
import re
from typing import Any, Optional

from sqlalchemy.orm import Session

from app.agents.base import AgentContext, AgentResult, BaseAgent
from app.services import agent_knowledge_service

# ── 四角色系统提示词 ──────────────────────────────────────────
_WHITEBOX_PROMPT = (
    "你是白盒测试审查 Agent。输入是沙箱白盒执行的结构化事实(Recon facts)与日志摘要。"
    "输出中文 Markdown 小节(## 白盒结果),含:结论(通过/未通过/有条件通过)、证据(退出码/编译/测试数)、"
    "发现的问题(每条挂证据)、修复建议。没有证据的只能写'建议验证'。"
)
_BLACKBOX_PROMPT = (
    "你是黑盒测试审查 Agent。输入是运行态 HTTP 探测记录(状态码/响应头/错误页/探活路径)。"
    "输出中文 Markdown 小节(## 黑盒结果),含:结论、证据(状态码/时延/响应头)、"
    "攻击面待验证清单(未授权访问/错误页泄露/敏感路径/Cookie 安全属性),"
    "没有实测证据的一律标'建议验证',禁止写成已存在漏洞。"
)
_VERIFY_PROMPT = (
    "你是对抗复检 Agent(Verify)。输入是白盒/黑盒两个 Agent 的结论草稿与原始证据。"
    "任务:证伪——逐条检查'发现'是否有证据支撑,无证据的降级为'建议验证'或删除;"
    "检查'通过'结论是否与退出码/状态码矛盾。"
    "输出 JSON: {\"confirmed\":[\"...\"],\"downgraded\":[\"...\"],\"contradictions\":[\"...\"],\"notes\":\"...\"}。"
)
_REPORT_PROMPT = (
    "你是报告 Agent(Report)。输入是白盒/黑盒结论与对抗复检的裁决。"
    "汇总为一份中文 Markdown 测试审查报告,固定七段:\n"
    "## 总体结论 / ## 白盒结果 / ## 黑盒结果 / ## 问题清单(按严重度,含位置/证据/修复建议) / "
    "## 攻击面待验证清单 / ## 下一步建议 / ## 证据附录。\n"
    "纪律:每条发现挂证据;通过≠安全,须写明未覆盖项;复检降级的条目只能进'建议验证';"
    "禁止编造文件路径/行号/URL。"
)

_QUERIES = {
    "whitebox": "白盒编译 静态检查 单元测试 跳过 警告 修复方向",
    "blackbox": "黑盒冒烟 状态码 响应头 错误页泄露 攻击面 待验证清单",
    "verify": "漏洞认定标准 误报熔断 证据纪律 无证据降级",
    "report": "审查顺序 报告结构 证据纪律 通过不等于安全",
}


class TestReviewReporterAgent(BaseAgent):
    """黑白盒测试结论 → 多 Agent 结构化中文审查报告。

    四角色 Whitebox/Blackbox/Verify/Report 各自一次 LLM 调用(≤4 次),
    由 sandbox_service 在测试终态后调用;任一角色失败静默降级,不阻断测试结论。
    """

    name = "test_reviewer"
    description = "多Agent编排:对沙箱黑白盒测试结论做审查、对抗复检并产出结构化中文报告"
    icon = "test_reviewer"
    color = "#3B6FD9"
    category = "review"
    skills = ("白盒结论审查", "黑盒冒烟审查", "对抗复检", "测试报告生成", "攻击面待验证清单")

    def __init__(self) -> None:
        super().__init__(system_prompt="", temperature=0.2, max_tokens=4096)

    # ── 工具 ──────────────────────────────────────────────
    @staticmethod
    def _redact(value: Any, depth: int = 0) -> Any:
        """防止把密钥/Token 写进报告或发给 LLM。"""
        if depth > 6:
            return "..."
        if isinstance(value, dict):
            out = {}
            for key, item in value.items():
                if re.search(r"token|secret|password|key|authorization", str(key), re.I):
                    out[key] = "***"
                else:
                    out[key] = TestReviewReporterAgent._redact(item, depth + 1)
            return out
        if isinstance(value, list):
            return [TestReviewReporterAgent._redact(v, depth + 1) for v in value[:50]]
        if isinstance(value, str):
            return value[:4000]
        return value

    def _knowledge_refs(self, db: Session, owner_id: int, stage: str) -> str:
        try:
            hits = agent_knowledge_service.unified_retrieve(
                db, user_id=owner_id, agent_code="test_review",
                query=_QUERIES.get(stage, _QUERIES["report"]), top_k=3,
            )
        except Exception:
            return ""
        lines = [str(h.get("content") or "").strip() for h in hits or [] if str(h.get("content") or "").strip()]
        if not lines:
            return ""
        return "\n\n【审查方法论参考】\n" + "\n---\n".join(lines)[:2000]

    def _role_call(self, system: str, user: str, ctx: Optional[AgentContext]) -> AgentResult:
        old = self._system_prompt
        self._system_prompt = system
        try:
            return self.call(user, ctx=ctx, thinking=False)
        finally:
            self._system_prompt = old

    @staticmethod
    def _collect_facts(conclusion: dict[str, Any]) -> dict[str, Any]:
        merged: dict[str, Any] = {}
        for item in conclusion.get("auto_test_chain") or []:
            facts = item.get("facts") if isinstance(item, dict) else None
            if isinstance(facts, dict):
                merged.update(facts)
        return merged

    # ── 主入口 ────────────────────────────────────────────
    def review(
        self,
        db: Session,
        *,
        environment: Any,
        conclusion: dict[str, Any],
        ctx: Optional[AgentContext] = None,
    ) -> AgentResult:
        """四角色编排生成测试审查报告;data = {"report_md": str, "roles": {...}}。"""
        if not self._api_key:
            return AgentResult(success=False, error="LLM 未配置,跳过测试审查报告", failure_kind="no_api_key")

        owner_id = int(getattr(environment, "owner_id", 0) or 0)
        mode = str(getattr(environment, "test_mode", "") or "combined")
        safe_conclusion = self._redact(conclusion)
        facts = self._collect_facts(conclusion)
        env_brief = {
            "public_id": getattr(environment, "public_id", ""),
            "purpose": getattr(environment, "purpose", ""),
            "test_mode": mode,
            "language": getattr(environment, "language", ""),
            "status": getattr(environment, "status", ""),
            "project_id": getattr(environment, "project_id", None),
        }
        evidence = json.dumps(
            {"environment": env_brief, "recon_facts": facts, "conclusion": safe_conclusion},
            ensure_ascii=False, default=str,
        )[:12000]

        roles: dict[str, Any] = {}
        # 1) 白盒角色
        wb = self._role_call(
            _WHITEBOX_PROMPT,
            "请审查白盒测试证据并输出 ## 白盒结果 小节:\n" + evidence + self._knowledge_refs(db, owner_id, "whitebox"),
            ctx,
        )
        roles["whitebox"] = {"ok": wb.success, "text": (wb.data or "")[:3000] if wb.success else f"未执行: {wb.error}"}
        # 2) 黑盒角色
        bb = self._role_call(
            _BLACKBOX_PROMPT,
            "请审查黑盒/冒烟测试证据并输出 ## 黑盒结果 小节:\n" + evidence + self._knowledge_refs(db, owner_id, "blackbox"),
            ctx,
        )
        roles["blackbox"] = {"ok": bb.success, "text": (bb.data or "")[:3000] if bb.success else f"未执行: {bb.error}"}
        # 3) 对抗复检角色(gate:无证据降级)
        vf = self._role_call(
            _VERIFY_PROMPT,
            "白盒草稿:\n" + str(roles["whitebox"]["text"])[:3000]
            + "\n\n黑盒草稿:\n" + str(roles["blackbox"]["text"])[:3000]
            + "\n\n原始证据:\n" + evidence[:4000]
            + self._knowledge_refs(db, owner_id, "verify"),
            ctx,
        )
        roles["verify"] = {"ok": vf.success, "text": (vf.data or "")[:3000] if vf.success else f"未执行: {vf.error}"}
        # 4) 报告角色
        rp = self._role_call(
            _REPORT_PROMPT,
            "白盒结论:\n" + str(roles["whitebox"]["text"])[:3000]
            + "\n\n黑盒结论:\n" + str(roles["blackbox"]["text"])[:3000]
            + "\n\n对抗复检裁决:\n" + str(roles["verify"]["text"])[:3000]
            + "\n\n原始证据:\n" + evidence[:3000]
            + self._knowledge_refs(db, owner_id, "report"),
            ctx,
        )
        roles["report"] = {"ok": rp.success, "text": ""}

        if not rp.success or not isinstance(rp.data, str) or not rp.data.strip():
            return AgentResult(
                success=False,
                error=f"报告角色失败: {rp.error or '空输出'}",
                failure_kind=rp.failure_kind,
            )
        report_md = rp.data.strip()
        if "## 总体结论" not in report_md:
            report_md = "## 总体结论\n（模型输出缺少固定段首,以下为原始内容）\n\n" + report_md
        roles["report"]["text"] = "已生成"
        executed = sum(1 for r in roles.values() if r.get("ok"))
        return AgentResult(
            success=True,
            data={"report_md": report_md, "roles": roles, "roles_executed": executed},
            model=rp.model, duration_ms=rp.duration_ms, tokens=rp.tokens,
        )

