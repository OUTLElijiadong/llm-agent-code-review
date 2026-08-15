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
    "纪律:总体结论必须严格服从原始证据中的 conclusion.passed 和 agent_tests 计数；"
    "任一已生成动态用例失败时不得写成测试通过或评分 100。每条发现挂证据;"
    "通过≠安全,须写明未覆盖项;复检降级的条目只能进'建议验证';"
    "禁止编造文件路径/行号/URL。\n"
    "代码段要求(专业漏洞报告规范):每条问题必须包含"
    "「漏洞位置」(文件:行号)、「 vulnerable code 」代码块(从证据原文引用 3-10 行,不得改写)、"
    "「POC/复现」代码块(实际执行的探测请求与关键响应,如 HTTP 请求行+响应码+响应片段)、"
    "「修复建议」代码块(给出修复后的写法示例)。证据附录中按发现逐条归档原始输出;"
    "白盒语法/编译错误要贴出 php -l 等工具的报错原文与出错文件名。"
)

# ── 动态专项角色(由编排 Agent 按证据追加) ──────────────
_EXTRA_ROLE_PROMPTS = {
    "sast": (
        "你是 SAST 静态应用安全审查 Agent。输入含源码级证据(白盒测试、静态检查、agent 生成的静态审计用例结果)。"
        "输出中文 Markdown 小节(## SAST 静态审计),含:危险函数/硬编码密钥/不安全的反序列化/SQL 拼接/越权逻辑发现,"
        "每条挂证据(文件/用例),无证据只写'建议验证'。"
    ),
    "dast": (
        "你是 DAST 动态应用安全测试审查 Agent。输入为运行态应用(已稳定部署)的黑盒探测与注入测试证据。"
        "输出中文 Markdown 小节(## DAST 动态测试),含:SQL 注入/XSS/SSRF/越权/目录探测结果,"
        "每条挂实际响应证据,未验证的写'建议验证',禁止写成已存在漏洞。"
    ),
    "injection": (
        "你是注入测试专项审查 Agent。输入为黑盒注入探测证据(SQLi/XSS/SSRF/命令注入/文件包含)。"
        "输出中文 Markdown 小节(## 注入测试结果),按危害排序,标注是否真实可利用(需要响应级证据)。"
    ),
    "dependency": (
        "你是依赖与供应链审查 Agent(SCA)。输入含依赖清单与离线安装/补全记录。"
        "输出中文 Markdown 小节(## 依赖审计),列出依赖来源(镜像内置/vendor/缺失)、已知风险提示、"
        "缺失依赖对完整运行的影响;无漏洞库数据时写'建议在联网环境用 Trivy 复核'。"
    ),
    "penetration": (
        "你是渗透测试审查 Agent。输入为应用稳定运行后的多路径探测证据(首页/接口/错误页/注入尝试)。"
        "输出中文 Markdown 小节(## 渗透测试结果),按攻击链串联已验证或建议验证的利用路径,"
        "强调证据纪律,禁止编造。"
    ),
}
_ORCHESTRATOR_PROMPT = (
    "你是审查编排 Agent。输入是沙箱测试证据(语言/模式/是否完整部署并稳定运行/是否含静态审计与动态注入证据/依赖清单)。"
    "基础角色 whitebox/blackbox/verify/report 始终保留;根据证据追加专项角色,可选:"
    "sast(有源码/静态审计证据), dast(有运行态注入/探测证据), injection(有注入探测证据), "
    "dependency(有依赖清单或补全记录), penetration(应用稳定运行且证据充分)。"
    "输出 JSON: {'extra_roles':['...']} 最多追加 3 个;不确定时输出 {'extra_roles':[]}。"
)

_QUERIES = {
    "whitebox": "白盒编译 静态检查 单元测试 跳过 警告 修复方向",
    "blackbox": "黑盒冒烟 状态码 响应头 错误页泄露 攻击面 待验证清单",
    "verify": "漏洞认定标准 误报熔断 证据纪律 无证据降级",
    "report": "审查顺序 报告结构 证据纪律 通过不等于安全",
    "sast": "SAST 静态审计 危险函数 硬编码密钥 SQL注入 反序列化",
    "dast": "DAST 动态测试 SQL注入 XSS SSRF 越权 探测证据",
    "injection": "注入测试 SQLi XSS SSRF 命令注入 利用证据",
    "dependency": "依赖审计 SCA 供应链 离线安装 缺失依赖",
    "penetration": "渗透测试 攻击链 利用验证 证据纪律",
}


class TestReviewReporterAgent(BaseAgent):
    """黑白盒测试结论 → 多 Agent 结构化中文审查报告。

    四角色 Whitebox/Blackbox/Verify/Report 各自一次 LLM 调用(≤4 次),
    由 sandbox_service 在测试终态后调用;任一角色失败静默降级,不阻断测试结论。
    """

    name = "test_reviewer"
    description = "审计汇报员:汇总黑白盒证据、对抗复检去伪存真,产出带代码段的人话报告"
    icon = "test_reviewer"
    color = "#3B6FD9"
    category = "review"
    skills = ("白盒结论审查", "黑盒冒烟审查", "对抗复检", "测试报告生成", "攻击面待验证清单")

    def __init__(self) -> None:
        # 报告输出预算顶到 DeepSeek 输出上限(65536),输入由 BaseAgent 按 1M 窗口投影。
        from app.core.config import settings
        super().__init__(system_prompt="", temperature=0.2,
                         max_tokens=min(65536, int(settings.deepseek_max_output_tokens)))

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

    def _role_call(self, system: str, user: str, ctx: Optional[AgentContext],
                   max_tokens: Optional[int] = None) -> AgentResult:
        old = self._system_prompt
        old_max = self._max_tokens
        self._system_prompt = system
        if max_tokens is not None:
            self._max_tokens = max_tokens
        try:
            return self.call(user, ctx=ctx, thinking=False)
        finally:
            self._system_prompt = old
            self._max_tokens = old_max

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
        # 提取黑盒关键信号(loopback 状态/PoC/探活),单独标注避免被大日志淹没
        blackbox_signals = []
        full_log = ""
        try:
            wr = (conclusion.get("evidence") or {}).get("worker_result") or {}
            full_log = str((wr.get("logs") or {}).get("text") or "")
        except Exception:
            pass
        for line in full_log.splitlines():
            if any(k in line for k in ("blackbox loopback status", "PRISM_VERIFY", "prism poc", "Development Server", "did not become ready")):  # noqa: E501
                blackbox_signals.append(line.strip())
        evidence = json.dumps(
            {
                "environment": env_brief,
                "recon_facts": facts,
                "blackbox_signals": blackbox_signals[-20:],
                "conclusion": safe_conclusion,
            },
            ensure_ascii=False, default=str,
        )

        roles: dict[str, Any] = {}
        # 1) 白盒角色
        wb = self._role_call(
            _WHITEBOX_PROMPT,
            "请审查白盒测试证据并输出 ## 白盒结果 小节:\n" + evidence + self._knowledge_refs(db, owner_id, "whitebox"),
            ctx,
        )
        roles["whitebox"] = {"ok": wb.success, "text": (wb.data or "")[:16000] if wb.success else f"未执行: {wb.error}"}
        # 2) 黑盒角色
        bb = self._role_call(
            _BLACKBOX_PROMPT,
            "请审查黑盒/冒烟测试证据并输出 ## 黑盒结果 小节:\n" + evidence + self._knowledge_refs(db, owner_id, "blackbox"),  # noqa: E501
            ctx,
        )
        roles["blackbox"] = {"ok": bb.success, "text": (bb.data or "")[:16000] if bb.success else f"未执行: {bb.error}"}
        # 3) 对抗复检角色(gate:无证据降级)
        vf = self._role_call(
            _VERIFY_PROMPT,
            "白盒草稿:\n" + str(roles["whitebox"]["text"])[:16000]
            + "\n\n黑盒草稿:\n" + str(roles["blackbox"]["text"])[:16000]
            + "\n\n原始证据:\n" + evidence
            + self._knowledge_refs(db, owner_id, "verify"),
            ctx,
        )
        roles["verify"] = {"ok": vf.success, "text": (vf.data or "")[:16000] if vf.success else f"未执行: {vf.error}"}
        # 4) 编排 Agent 按证据追加专项角色(SAST/DAST/注入/依赖/渗透),失败则保持基础四角色
        extra_roles: list[str] = []
        try:
            orch = self._role_call(
                _ORCHESTRATOR_PROMPT,
                "测试证据:\n" + evidence[:12000] + self._knowledge_refs(db, owner_id, "report"),
                ctx,
                max_tokens=1024,
            )
            orch_data = orch.data if isinstance(orch.data, dict) else None
            if orch.success and orch_data is None and isinstance(orch.data, str) and orch.data.strip():
                try:
                    orch_data = json.loads(orch.data)
                except (ValueError, TypeError):
                    orch_data = None
            if isinstance(orch_data, dict) and isinstance(orch_data.get("extra_roles"), list):
                extra_roles = [
                    str(r).strip() for r in orch_data["extra_roles"]
                    if isinstance(r, str) and r.strip() in _EXTRA_ROLE_PROMPTS
                ][:3]
        except (ValueError, TypeError):
            extra_roles = []
        for role_name in extra_roles:
            prompt = _EXTRA_ROLE_PROMPTS[role_name]
            rr = self._role_call(
                prompt,
                "请审查对应证据并输出小节:\n" + evidence[:16000]
                + self._knowledge_refs(db, owner_id, role_name),
                ctx,
            )
            roles[role_name] = {
                "ok": rr.success,
                "text": (rr.data or "")[:16000] if rr.success else f"未执行: {rr.error}",
            }
        # 5) 报告角色(七段报告在大量语法错误时输出较长,用满输出预算防截断)
        extra_conclusions = "".join(
            f"\n\n{role_name}专项结论:\n" + str(roles[role_name]["text"])[:16000]
            for role_name in extra_roles
        )
        rp = self._role_call(
            _REPORT_PROMPT,
            "白盒结论:\n" + str(roles["whitebox"]["text"])[:16000]
            + "\n\n黑盒结论:\n" + str(roles["blackbox"]["text"])[:16000]
            + "\n\n对抗复检裁决:\n" + str(roles["verify"]["text"])[:16000]
            + extra_conclusions
            + "\n\n原始证据:\n" + evidence
            + self._knowledge_refs(db, owner_id, "report"),
            ctx,
            max_tokens=self._max_tokens,
        )
        roles["report"] = {"ok": rp.success, "text": ""}

        # 截断兜底:8192 仍截断时降级为精简重试(只求核心三段),保证总能产出报告
        if (not rp.success or not (isinstance(rp.data, str) and rp.data.strip())) and rp.finish_reason == "length":
            extra_short = "".join(
                f"\n\n{role_name}专项结论(精简):\n" + str(roles[role_name]["text"])[:1500]
                for role_name in extra_roles
            )
            rp = self._role_call(
                _REPORT_PROMPT + "\n(上次输出超长被截断。本次只输出 ## 总体结论 / ## 问题清单 / ## 下一步建议 三段,问题清单最多列15条。)",  # noqa: E501
                "白盒结论:\n" + str(roles["whitebox"]["text"])[:2000]
                + "\n\n黑盒结论:\n" + str(roles["blackbox"]["text"])[:2000]
                + "\n\n对抗复检裁决:\n" + str(roles["verify"]["text"])[:2000]
                + extra_short,
                ctx,
                max_tokens=8192,
            )

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
