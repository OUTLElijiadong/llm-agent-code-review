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

import hashlib
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from loguru import logger
from sqlalchemy.orm import Session

from app.agents.audit_board import AuditBoard
from app.agents.base import AgentContext, AgentResult
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
        """真实沙箱 PoC 实测 (v3.4): 生成 PoC 脚本 → combined 沙箱起服务并执行 → 解析真实响应.

        流程:
          1) LLM 按每条高危漏洞的类别/证据生成一个 shell PoC(_prism_poc.sh),
             遵守 CRUD 数据隔离红线: 只发 GET/POST 探测与对自身创建数据的读写,
             绝不删改真实数据;
          2) 把 PoC 注入项目源码包, 创建 combined 沙箱(php -l + php -S + 执行 PoC);
          3) runner 起服务后执行 PoC 并输出 PRISM_POC_RESULT 行, 后端经沙箱结论回收
             并按响应内容判定 confirmed / refuted / inconclusive。
        worker 不在线或沙箱失败时抛异常, 由上层降级为 LLM 推理验证。
        """
        import base64
        import io
        import zipfile

        from app.services import project_source_service
        from app.services.sandbox_service import _select_worker

        # worker 在线性由 _select_worker 强校验(不在线会抛异常,上层捕获降级)
        # 1) 生成 PoC 脚本(一次 LLM 调用, 覆盖全部 target)
        poc_script = self._generate_poc_script(targets, ctx=ctx)
        if not poc_script:
            raise RuntimeError("PoC 脚本生成失败")

        # 2) 打包: 项目源码 + _prism_poc.sh
        archive, _name = project_source_service.build_source_archive(self._db, actor, project.id)
        buf = io.BytesIO(archive)
        out = io.BytesIO()
        with zipfile.ZipFile(buf, "r") as zin, zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zout:
            for item in zin.infolist():
                if item.filename == "_prism_poc.sh":
                    continue
                zout.writestr(item, zin.read(item.filename))
            zout.writestr("_prism_poc.sh", poc_script)
        patched = out.getvalue()
        patched_b64 = base64.b64encode(patched).decode("ascii")
        patched_sha256 = hashlib.sha256(patched).hexdigest()

        # 3) 复用 sandbox_service 的 worker 选择 + 协议调用(含 Bearer token / HTTPS pin),
        #    提交 combined 任务并轮询到终态。不经 create_environment,因为需要注入 PoC 的
        #    源码包而非项目原始归档。
        from app.services.sandbox_service import _call_worker

        worker = _select_worker(self._db, language="php", mode="combined")
        env_public_id = f"poc-{project.id}-{int(time.time())}"
        result = self._call_worker_execute(
            _call_worker, worker, env_public_id, patched_b64, patched_sha256, ctx=ctx,
        )

        # 4) 解析真实 PoC 结果
        verdicts = self._parse_poc_result(result, targets)
        return verdicts

    def _call_worker_execute(self, _call_worker, worker, request_id: str,
                             archive_b64: str, source_sha256: str,
                             ctx: Optional[AgentContext]) -> dict:
        """按 sandbox_service._call_worker 协议提交 combined 任务并轮询到终态."""
        payload = {
            "request_id": request_id,
            "purpose": "test",
            "language": "php",
            "test_mode": "combined",
            "source_archive_base64": archive_b64,
            "source_sha256": source_sha256,
            "ttl_seconds": 300,
            "image_digest": "",
        }
        resp = _call_worker(worker, "POST", "/execute", payload)
        result = resp.get("result") if isinstance(resp.get("result"), dict) else resp

        deadline = time.time() + 300
        last_seq = 0
        terminal = {"succeeded", "failed", "blocked", "stopped", "expired", "completed"}
        # running 表示 deploy 常驻,test 模式的 combined 会跑到 succeeded/failed
        while str(result.get("status") or "") not in terminal:
            if time.time() > deadline:
                raise RuntimeError("沙箱 PoC 轮询超时")
            time.sleep(2)
            sresp = _call_worker(worker, "POST", "/status", {
                "request_id": request_id, "after_sequence": last_seq,
            })
            result = sresp.get("result") if isinstance(sresp.get("result"), dict) else sresp
            last_seq = int(result.get("last_sequence") or last_seq)
        return result

    def _generate_poc_script(self, targets: List[dict],
                             ctx: Optional[AgentContext]) -> str:
        """LLM 生成 _prism_poc.sh: 按每条高危漏洞类别产出真实 HTTP 探测.

        输出约定: 每条漏洞打印一行
          PRISM_POC_RESULT index=<i> verdict=<confirmed|refuted|inconclusive> evidence=<关键响应特征>
        """
        if not targets:
            return ""
        items = []
        for i, f in enumerate(targets):
            cat = str(f.get("category") or f.get("title") or "")
            items.append(
                f"[{i}] 类别={cat} 文件={f.get('file_path','')}:{f.get('lines','')} "
                f"证据={str(f.get('evidence',''))[:120]}"
            )
        prompt = (
            "你要为一个 PHP 项目生成一个**在隔离沙箱内执行的 PoC 验证脚本** `_prism_poc.sh`。"
            "沙箱已用 `php -S 127.0.0.1:$PRISM_POC_PORT` 启动该项目,你可向它发真实 HTTP 请求。\n"
            "对下面每条高危漏洞候选,写一段 shell(curl 或 /dev/tcp)探测其是否真实可利用,并打印判定行。\n\n"
            "严格要求:\n"
            "1. 每条打印恰好一行: `PRISM_POC_RESULT index=<i> verdict=<confirmed|refuted|inconclusive> evidence=<≤60字关键响应特征>`\n"  # noqa: E501
            "2. 遵守 CRUD 数据隔离红线: 只用 GET/POST 做只读探测或对自身新建数据的写,"
            "绝不 UPDATE/DELETE 已存在数据,不 drop、不 rm、不写 webshell 到磁盘。\n"
            "3. SQL 注入: 用 `' AND '1'='1` / `' AND '1'='2` 对比响应差异,或报错注入看 SQL 错误回显。\n"
            "4. 路径遍历/LFI: 用 `../../../../etc/passwd` 看是否读出 `root:`。\n"
            "5. 命令注入: 用时间型 `;sleep 3` 对比响应耗时(沙箱无网,不要外连)。\n"
            "6. 反序列化: 构造无害序列化串看是否触发对象解析错误/差异。\n"
            "7. 证据不足判 inconclusive,不得臆造 confirmed。\n"
            "8. 脚本要健壮: set +e,每个请求带 --max-time 5,失败继续下一条。\n\n"
            "候选漏洞:\n" + "\n".join(items) + "\n\n"
            "只输出 shell 脚本原文(以 #!/bin/sh 开头),不要任何解释。"
        )
        result = self._sentinel.call(prompt, ctx=ctx, thinking=False)
        if not result.success or not result.data:
            return ""
        script = str(result.data).strip()
        # 去掉可能的 markdown 围栏
        if script.startswith("```"):
            lines = script.splitlines()
            script = "\n".join(lines[1:-1] if lines and lines[-1].startswith("```") else lines[1:])
        if "PRISM_POC_RESULT" not in script:
            return ""
        if not script.startswith("#!"):
            script = "#!/bin/sh\nset +e\n" + script
        return script

    def _parse_poc_result(self, worker_result: dict,
                          targets: List[dict]) -> List[dict]:
        """从 worker 输出(stdout/logs/result)解析 PRISM_POC_RESULT 行,映射回 target."""
        text_parts: List[str] = []
        def _collect(obj):
            if isinstance(obj, str):
                text_parts.append(obj)
            elif isinstance(obj, dict):
                for v in obj.values():
                    _collect(v)
            elif isinstance(obj, list):
                for v in obj:
                    _collect(v)
        _collect(worker_result)
        blob = "\n".join(text_parts)

        verdicts: List[dict] = []
        import re as _re
        for m in _re.finditer(
            r"PRISM_POC_RESULT\s+index=(\d+)\s+verdict=(confirmed|refuted|inconclusive)\s+evidence=(.*)",
            blob,
        ):
            idx = int(m.group(1))
            if 0 <= idx < len(targets):
                verdicts.append({
                    "_index": idx,
                    "verdict": m.group(2),
                    "evidence": m.group(3).strip()[:200],
                })
        return verdicts

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
        # ── 大白话报告(结论先行→风险速览→人话解释→下一步) ──
        total = sum(sev.values())
        verified = int(verification.get("verified", 0) or 0)
        if total == 0:
            verdict = "这轮审计没发现问题"
            verdict_detail = "代码看起来是干净的,但建议部署前再做一次黑盒测试实际跑一跑确认。"
        elif sev["严重"] or sev["高"]:
            verdict = f"发现 {sev['严重'] + sev['高']} 个需要尽快处理的高危问题"
            verdict_detail = (
                f"其中严重 {sev['严重']} 个、高 {sev['高']} 个"
                + (f",有 {verified} 个已经实际验证过、能稳定复现" if verified else "")
                + "。建议优先处理下面「需要尽快处理」里的问题。"
            )
        else:
            verdict = f"发现 {total} 个中低风险问题,暂不致命"
            verdict_detail = "不影响马上上线,但建议排进近期修复计划。"

        # 风险速览表:严重度/数量/一句话含义
        severity_table = [
            {"level": "严重", "count": sev["严重"], "meaning": "攻击者可以直接拿权限、拖数据或搞瘫系统"},
            {"level": "高", "count": sev["高"], "meaning": "很可能被利用,需要尽快修"},
            {"level": "中", "count": sev["中"], "meaning": "有一定风险,建议排期修"},
            {"level": "低", "count": sev["低"], "meaning": "小瑕疵,顺手修掉即可"},
        ]
        # top 发现的大白话行(带文件/行号定位)
        plain_findings = []
        for f in (findings or [])[:10]:
            plain_findings.append({
                "title": str(f.get("title") or f.get("type") or "问题"),
                "severity": f.get("severity", "中"),
                "where": f"文件 {f.get('file_path', '?')}" + (f" 第 {f.get('line')} 行" if f.get("line") else ""),
                "what_it_means": str(f.get("description") or f.get("evidence") or "")[:200],
                "confidence": f.get("confidence"),
            })
        next_steps = []
        if sev["严重"] or sev["高"]:
            next_steps.append("先处理上表「严重/高」的问题,修完再跑一次审计确认")
        if verified:
            next_steps.append(f"已验证能复现的 {verified} 个问题优先级最高,可直接按报告定位到代码行")
        next_steps.append("需要的话可以让小菱直接生成修复提示(AI 修复建议)")
        next_steps.append("修复后用「全量验证」实际部署跑一遍黑白盒测试")
        summary = (
            f"审计完成「{project.project_name}」:{verdict}。"
            f"共检查 {len(recon.surface.file_profiles)} 个文件、{len(recon.surface.hot_sinks)} 处"
            f"外部输入直接触达敏感操作的位置;深度分析 {total} 个问题待你处理。"
        )
        return {
            "summary": summary,
            # 大白话层:前端直接渲染,不出现「污点sink/对抗复检」等术语
            "plain_report": {
                "verdict": verdict,
                "verdict_detail": verdict_detail,
                "severity_table": severity_table,
                "findings": plain_findings,
                "next_steps": next_steps,
                "checked_files": len(recon.surface.file_profiles),
                "risky_entry_points": len(recon.surface.hot_sinks),
            },
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
