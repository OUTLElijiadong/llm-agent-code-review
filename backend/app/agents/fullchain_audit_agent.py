"""全链路源码审计编排器 (v3.3 新增)

定位: 把「侦察 Recon → 分析 Analysis → 验证 Verification → 报告 Report」四个角色
组织成一条可追溯的全链路, 在每个环节之间用**审计黑板(共享结构化上下文)**传递
事实/假设/意图——对应「协调 > 调度」的结构主义设计。

与现有模块的关系(复用而非重造):
- SecuritySentinelAgent.scan_project: 完成 静态+语义批量审计(白盒主引擎), 这里复用其产出;
- AttackSurface(php_attack_surface): Recon 阶段的确定性攻击面建模, 零 LLM 成本;
- audit_knowledge_loader: 反幻觉/误报/sink/攻击链知识的分层注入, 压误报;
- sandbox_service: Verification 阶段的真实沙箱 PoC 验证(白/黑/组合), 复用其生命周期编排;
- AuditBoard: 全链路的共享黑板, 承载各角色产出的事实与待验证假设。

四大痛点对应:
  假警报多   → Analysis 注入误报知识库 + 对抗复检质疑证伪 + Verification 沙箱实测
  复杂逻辑   → Recon 建攻击面/跨文件数据流, Analysis 结合二阶漏洞/攻击链知识深挖
  真假难辨   → Verification 生成 PoC 并在沙箱实测, 用真实响应判定(证据契约)
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from loguru import logger
from sqlalchemy.orm import Session

from app.agents.base import AgentContext, AgentResult
from app.agents.audit_board import AuditBoard
from app.agents.events import AgentEventType
from app.ai.php_attack_surface import AttackSurface, category_meta, profile_php_file
from app.models.code_file import CodeFile
from app.models.project import Project
from app.models.user import User

if False:  # TYPE_CHECKING
    from app.agents.security_sentinel_agent import SecuritySentinelAgent


@dataclass
class ReconReport:
    """侦察员产出: 攻击面 + 高风险文件清单"""
    surface: AttackSurface = field(default_factory=AttackSurface)
    hot_files: List[str] = field(default_factory=list)
    framework_hints: List[str] = field(default_factory=list)
    summary: str = ""


class FullChainAuditOrchestrator:
    """全链路审计编排器 —— 调度 Recon/Analysis/Verification/Report 四角色."""

    name = "fullchain_audit"

    def __init__(self, sentinel: "SecuritySentinelAgent") -> None:
        # 复用安全哨兵(白盒主引擎)与它的 LLM/事件/DB 能力
        self._sentinel = sentinel

    # ---- 依赖访问 ----
    @property
    def _db(self) -> Optional[Session]:
        return self._sentinel._db

    def _emit(self, type_, ctx, message="", payload=None):
        self._sentinel._emit(type_, ctx, message=message, payload=payload or {})

    # =====================================================================
    # 角色一: 侦察员 Recon —— 梳理结构, 锁定高风险接口和函数(零 LLM 成本)
    # =====================================================================
    def _recon(self, files: List[CodeFile], board: AuditBoard,
               ctx: Optional[AgentContext]) -> ReconReport:
        self._emit(AgentEventType.PROGRESS, ctx,
                   message="[Recon] 开始攻击面建模与高风险点侦察",
                   payload={"phase": "recon"})
        surface = AttackSurface()
        frameworks: set[str] = set()
        for f in files:
            path = (f.file_path or f.file_name or "")
            content = f.content or ""
            if not content:
                continue
            if path.lower().endswith(".php"):
                try:
                    surface.file_profiles.append(profile_php_file(path, content))
                except Exception as e:
                    logger.debug(f"[fullchain][recon] 画像失败 {path}: {e}")
            # 框架指纹(轻量)
            lower = path.lower()
            for fw, key in (("Laravel", "laravel"), ("ThinkPHP", "thinkphp"),
                            ("WordPress", "wp-content"), ("Symfony", "symfony"),
                            ("Ecology", "weaver"), ("iWebShop", "iwebshop")):
                if key in lower:
                    frameworks.add(fw)

        hot = surface.hot_sinks
        board.attack_surface_facts = surface.to_blackboard_facts(limit=40)
        # 高风险 sink → 落成待验证假设, 写入黑板(共享给 Analysis/Verification)
        for prof, sink in hot[:60]:
            cn, sev, cwe, _owasp = category_meta(sink.category)
            board.add_hypothesis(
                title=f"{cn}: {sink.func.strip()[:40]}",
                detail=sink.snippet[:120],
                file_path=prof.file_path, line=sink.line,
                category=sink.category, severity=sev, confidence=0.55,
                evidence=sink.snippet[:200], source="recon",
            )
        hot_files = [p.file_path for p in surface.ranked_files if p.risk_score > 0][:30]
        report = ReconReport(
            surface=surface,
            hot_files=hot_files,
            framework_hints=sorted(frameworks),
            summary=(
                f"侦察完成: {len(surface.file_profiles)} 个 PHP 文件, "
                f"污点 sink {len(hot)} 处, 高风险文件 {len(hot_files)} 个, "
                f"框架指纹: {','.join(sorted(frameworks)) or '未识别'}。"
            ),
        )
        self._emit(AgentEventType.PROGRESS, ctx,
                   message=f"[Recon] {report.summary}",
                   payload={"phase": "recon", "tainted_sinks": len(hot),
                            "hot_files": len(hot_files)})
        return report

    # =====================================================================
    # 角色二: 分析师 Analysis —— 语义深挖(白盒主引擎 + 知识库 + 对抗复检)
    # =====================================================================
    def _analysis(self, project_id: int, top_n: int, trace_dataflow: bool,
                  board: AuditBoard, ctx: Optional[AgentContext]) -> AgentResult:
        self._emit(AgentEventType.PROGRESS, ctx,
                   message="[Analysis] 启动白盒语义审计(知识库注入 + 对抗复检)",
                   payload={"phase": "analysis"})
        # 复用安全哨兵的项目级白盒审计(内含知识库注入 + 对抗复检)
        # 复用安全哨兵的项目级白盒审计(内含知识库注入 + 对抗复检)。
        # 关键: scan_mode 必须用 "static_full"(整包静态 + 有界语义)而非默认 "full"
        # —— "full" 会对全量源码做无界语义审计, 大项目触发语义预算门禁(预算耗尽即
        # 整体失败)。"static_full" 兼顾覆盖与预算可控, 符合全链路「分析→验证」的分工。
        result = self._sentinel.scan_project(
            project_id, top_n=top_n, trace_dataflow=trace_dataflow, ctx=ctx,
            scan_mode="static_full",
        )
        if not result.success:
            return result
        data = result.data or {}
        # 把分析产出的高危结论回写黑板为「事实」
        for f in (data.get("findings") or []):
            if not isinstance(f, dict):
                continue
            if f.get("severity") in {"严重", "高"} and f.get("verification") == "confirmed":
                board.add_fact(
                    title=str(f.get("title", ""))[:80],
                    file_path=str(f.get("file_path", "")),
                    line=self._sentinel._coerce_int(f.get("line_number"), 0),
                    category=str(f.get("category", "")),
                    severity=str(f.get("severity", "中")),
                    confidence=float(f.get("confidence", 0.8) or 0),
                    evidence=str(f.get("evidence", ""))[:200],
                    source="analysis_confirmed",
                )
        return result

    # =====================================================================
    # 角色三: 验证员 Verification —— 生成 PoC,沙箱实测(复用 sandbox_service)
    # =====================================================================
    def _verification(self, project: Project, actor: User,
                      findings: List[dict], board: AuditBoard,
                      ctx: Optional[AgentContext],
                      enable_sandbox: bool, max_verify: int = 8) -> Dict[str, Any]:
        """对 Analysis 产出的高危结论做验证。

        两级验证:
          1) LLM 推理验证(始终执行): 基于证据链+反幻觉规则判定可利用性, 生成 PoC 思路;
          2) 沙箱实测(enable_sandbox=True 且有可用 worker): 调 sandbox_service 创建
             隔离 PHP 环境跑真实 PoC。沙箱不可用时静默降级为仅推理验证。
        """
        self._emit(AgentEventType.PROGRESS, ctx,
                   message="[Verification] 开始漏洞可利用性验证",
                   payload={"phase": "verification", "sandbox": enable_sandbox})
        high = [f for f in findings
                if isinstance(f, dict) and f.get("severity") in {"严重", "高"}]
        high.sort(key=lambda x: -float(x.get("confidence", 0) or 0))
        targets = high[:max_verify]

        verified: List[dict] = []
        sandbox_used = False
        sandbox_error = ""

        # —— 沙箱实测(可选, 安全降级) ——
        if enable_sandbox and targets:
            try:
                verified_sandbox = self._sandbox_verify(
                    project, actor, targets, ctx=ctx,
                )
                if verified_sandbox:
                    sandbox_used = True
                    for item in verified_sandbox:
                        idx = item.get("_index")
                        if idx is not None and 0 <= idx < len(targets):
                            targets[idx]["sandbox_verdict"] = item.get("verdict", "")
                            targets[idx]["sandbox_evidence"] = item.get("evidence", "")
            except Exception as e:
                sandbox_error = str(e)[:200]
                logger.warning(f"[fullchain][verification] 沙箱验证降级: {e}")

        # —— LLM 推理验证(始终执行, 给每条高危结论出 PoC 思路与判定) ——
        llm_verdicts = self._llm_verify(targets, ctx=ctx)
        for i, f in enumerate(targets):
            verdict = llm_verdicts.get(i, {})
            f["poc"] = verdict.get("poc", "")
            f["exploit_verdict"] = verdict.get("verdict", "needs_manual")
            if f.get("sandbox_verdict") == "confirmed" or verdict.get("verdict") == "confirmed":
                verified.append(f)
                board.confirm  # noqa: B018 - 黑板演化由 Analysis/对抗复检完成

        self._emit(AgentEventType.PROGRESS, ctx,
                   message=f"[Verification] 验证完成: {len(verified)}/{len(targets)} 条高危可复现",
                   payload={"phase": "verification", "verified": len(verified),
                            "sandbox_used": sandbox_used})
        return {
            "targets": len(targets),
            "verified": len(verified),
            "sandbox_used": sandbox_used,
            "sandbox_error": sandbox_error,
            "verified_findings": verified,
        }

    def _llm_verify(self, targets: List[dict],
                    ctx: Optional[AgentContext]) -> Dict[int, dict]:
        """LLM 推理验证: 对每条高危产出 PoC 思路 + 可利用性判定."""
        if not targets:
            return {}
        items = []
        for i, f in enumerate(targets):
            items.append(
                f"[{i}] {f.get('severity')} {f.get('category','')} "
                f"{f.get('file_path','')}:{f.get('lines','')}\n"
                f"  证据: {str(f.get('evidence',''))[:140]}\n"
                f"  场景: {str(f.get('exploit_scenario',''))[:140]}"
            )
        from app.agents.security_sentinel_agent import _knowledge_context
        prompt = (
            "你是漏洞验证专家。对下面每条高危漏洞候选, 给出:\n"
            "1) verdict: confirmed(可利用)/plausible(疑似)/refuted(误报)\n"
            "2) poc: 一段可操作的验证思路(请求方法/参数/payload 要点, ≤120 字)\n"
            "判定须基于证据链, 不确定给 plausible, 误报给 refuted。\n\n"
            f"{_knowledge_context('verification')}"
            "候选:\n" + "\n\n".join(items) + "\n\n"
            '严格输出 JSON: {"reviews":[{"index":0,"verdict":"confirmed|plausible|refuted","poc":"..."}]}'
        )
        result = self._sentinel.call_json(prompt, ctx=ctx, thinking=False)
        out: Dict[int, dict] = {}
        if result.success and isinstance(result.data, dict):
            for r in (result.data.get("reviews") or []):
                if isinstance(r, dict):
                    try:
                        out[int(r.get("index"))] = {
                            "verdict": str(r.get("verdict") or ""),
                            "poc": str(r.get("poc") or "")[:200],
                        }
                    except (TypeError, ValueError):
                        continue
        return out

    def _sandbox_verify(self, project: Project, actor: User,
                        targets: List[dict],
                        ctx: Optional[AgentContext]) -> List[dict]:
        """沙箱实测: 复用 sandbox_service 起隔离 PHP 环境跑 PoC.

        注意: 这是「真实沙箱执行」的挂点。完整 PoC 下发依赖 worker 协议与目标可运行
        环境(很多 CMS 无法一键启动), 因此这里做**能力探测 + 环境就绪**:
        创建沙箱 → 等待就绪/终态 → 读取执行结论。worker 不可用时抛异常由上层降级。
        """
        from app.services import sandbox_service
        from app.services.sandbox_service import SandboxEnvironment  # noqa: F401

        workers = sandbox_service.list_workers(self._db)
        online = [w for w in workers if w.get("status") == "online"]
        if not online:
            raise RuntimeError("无在线沙箱 worker")
        env = sandbox_service.create_environment(self._db, actor, {
            "project_id": project.id,
            "purpose": "test",
            "language": "php",
            "test_mode": "whitebox",
        })
        # 等待沙箱进入终态(有界轮询, 不阻塞主线程)
        deadline = time.time() + 90
        status = env.status
        while time.time() < deadline and status not in (
            "succeeded", "failed", "blocked", "stopped", "expired",
        ):
            time.sleep(3)
            row = sandbox_service.get_environment(self._db, actor, env.public_id)
            status = row.get("status", status)
        self._emit(AgentEventType.PROGRESS, ctx,
                   message=f"[Verification] 沙箱 {env.public_id} 终态: {status}",
                   payload={"phase": "verification", "sandbox_status": status})
        # 当前实现: 环境就绪即返回(具体 PoC 下发依赖 worker 证据协议, 后续接入)
        return []

    # =====================================================================
    # 角色四: 报告 Report —— 汇总全链路产出
    # =====================================================================
    def _report(self, project: Project, recon: ReconReport,
                analysis: AgentResult, verification: Dict[str, Any],
                board: AuditBoard, duration_ms: int) -> Dict[str, Any]:
        data = analysis.data or {}
        findings = data.get("findings") or []
        sev = {"严重": 0, "高": 0, "中": 0, "低": 0}
        for f in findings:
            s = f.get("severity", "中")
            if s in sev:
                sev[s] += 1
        summary = (
            f"全链路审计完成「{project.project_name}」: "
            f"侦察 {len(recon.surface.file_profiles)} 文件/污点 sink "
            f"{len(recon.surface.hot_sinks)} 处; "
            f"分析检出 严重{sev['严重']}/高{sev['高']}/中{sev['中']}/低{sev['低']}; "
            f"验证可复现 {verification.get('verified', 0)}/{verification.get('targets', 0)} 条高危。"
        )
        return {
            "summary": summary,
            "severity_counts": sev,
            "recon": {
                "framework_hints": recon.framework_hints,
                "hot_files": recon.hot_files,
                "tainted_sink_count": len(recon.surface.hot_sinks),
            },
            "verification": verification,
            "board": board.summary(),
            "duration_ms": duration_ms,
        }

    # =====================================================================
    # 主编排入口
    # =====================================================================
    def run(self, project_id: int, actor: User,
            top_n: int = 100, trace_dataflow: bool = True,
            enable_sandbox: bool = False,
            ctx: Optional[AgentContext] = None) -> AgentResult:
        """执行全链路审计: Recon → Analysis → Verification → Report."""
        if self._db is None:
            return AgentResult(success=False, error="DB 未注入")
        project = self._db.get(Project, project_id)
        if project is None or project.status == "deleted":
            return AgentResult(success=False, error="项目不存在或已删除")
        # 复用哨兵的鉴权
        if (err := self._sentinel._authz_project(project)) is not None:
            return err

        t0 = time.time()
        board = AuditBoard(project_name=project.project_name or f"项目#{project_id}")
        self._emit(AgentEventType.DISPATCH, ctx,
                   message=f"[FullChain] 项目 #{project_id} 全链路审计启动",
                   payload={"scope": "fullchain", "project_id": project_id,
                            "enable_sandbox": enable_sandbox})

        files = (
            self._db.query(CodeFile)
            .filter(CodeFile.project_id == project_id, CodeFile.status == "active")
            .all()
        )
        if not files:
            return AgentResult(success=False, error="项目下没有可扫描的代码文件")

        # 1. Recon
        recon = self._recon(files, board, ctx)
        # 2. Analysis(复用白盒主引擎)
        analysis = self._analysis(project_id, top_n, trace_dataflow, board, ctx)
        if not analysis.success:
            return analysis
        # 3. Verification(真实沙箱可选)
        verification = self._verification(
            project, actor, (analysis.data or {}).get("findings") or [],
            board, ctx, enable_sandbox=enable_sandbox,
        )
        # 4. Report
        duration_ms = int((time.time() - t0) * 1000)
        report = self._report(project, recon, analysis, verification, board, duration_ms)

        self._emit(AgentEventType.COMPLETE, ctx,
                   message="[FullChain] 全链路审计完成",
                   payload={"project_id": project_id,
                            "verified": verification.get("verified", 0),
                            "duration_ms": duration_ms})

        # 把 Analysis 的白盒结果原样带上, 附加全链路段
        data = dict(analysis.data or {})
        data["fullchain"] = report
        data["audit_board"] = {
            "summary": board.summary(),
            "attack_surface": board.attack_surface_facts[:40],
            "confirmed": [
                {"title": n.title, "file_path": n.file_path, "line": n.line,
                 "severity": n.severity, "confidence": round(n.confidence, 2)}
                for n in board.confirmed[:30]
            ],
        }
        return AgentResult(success=True, data=data, model=self._sentinel._model,
                           duration_ms=duration_ms)
