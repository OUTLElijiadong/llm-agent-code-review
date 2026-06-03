"""SecuritySentinel 安全哨兵 Agent (v2.1 新成员)

定位:网络安全深度审查 Agent。
- 与 v1.0 的 SECURITY_AGENT(prompt 画像)并行,后者只服务 ReviewService 编排;
  本 Agent 是独立注册的 BaseAgent,可被 Agent 办公室、ChatAgent、专用 API 调度。
- 与 v2.0 的 code_reviewer(通用八维审查)并行,本 Agent 只输出安全/CWE/OWASP 类问题。

三种调用形态:
- scan_file(file_id):           单文件深度安全扫描(正则秘钥 + LLM 漏洞审查)
- scan_task(task_id):           任务级复审,给已检出 issue 补 OWASP/CWE 标签
- scan_project(project_id):     项目级威胁建模 + 跨文件数据流追踪
"""
from __future__ import annotations

import json as json_lib
import time
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

from loguru import logger
from sqlalchemy.orm import Session

from app.agents.base import AgentContext, AgentResult, BaseAgent
from app.agents.events import AgentEventType
from app.ai.code_chunker import chunk_code
from app.ai.security_patterns import list_patterns, scan_secrets
from app.ai.security_static_rules import apply_static_rules, list_static_rules
from app.models.code_file import CodeFile
from app.models.project import Project
from app.models.review_issue import ReviewIssue
from app.models.review_task import ReviewTask
from app.models.user import User

SYSTEM_PROMPT = (
    "你是 PRISM 棱镜平台的网络安全审计 Agent,"
    "具备 OWASP Top10、CWE、SANS Top25、等保 2.0 的完整知识。\n"
    "工作目标:在用户提供的代码中识别**确定的、可解释、可演示**的网络安全漏洞。\n\n"
    "约束:\n"
    "1. 严格 JSON 输出,字段见用户消息中的 schema\n"
    "2. 每条 finding 必须给出 owasp 和 cwe 编号(无法判断时填空字符串)\n"
    "3. severity 仅取 严重 / 高 / 中 / 低\n"
    "4. 不臆造漏洞;不确定时 confidence < 0.6 并明确标注\n"
    "5. 不输出风格、命名、注释类问题(那是 code_reviewer 的领域)\n"
)


# OWASP Top10 2021 检查清单(供 GET /api/security/checklist 用)
_OWASP_TOP10_2021: Tuple[Tuple[str, str, str], ...] = (
    ("A01", "Broken Access Control", "失效的访问控制"),
    ("A02", "Cryptographic Failures", "加密失败 / 敏感数据泄露"),
    ("A03", "Injection", "注入(SQL/Command/LDAP/XPath/模板)"),
    ("A04", "Insecure Design", "不安全的设计"),
    ("A05", "Security Misconfiguration", "安全配置错误"),
    ("A06", "Vulnerable and Outdated Components", "易受攻击和过时的组件"),
    ("A07", "Identification and Authentication Failures", "身份识别和身份验证失败"),
    ("A08", "Software and Data Integrity Failures", "软件和数据完整性失败"),
    ("A09", "Security Logging and Monitoring Failures", "安全日志和监控失败"),
    ("A10", "Server-Side Request Forgery (SSRF)", "服务端请求伪造"),
)


# 严重度扣分(沿用 app/ai/scoring.py 模型)
_SEVERITY_DEDUCT = {"严重": 15, "高": 8, "中": 3, "低": 1}
_ALLOWED_SEVERITY = {"严重", "高", "中", "低"}


@dataclass
class _AuditChunkResult:
    """单分片 LLM 审查结果"""

    findings: List[dict] = field(default_factory=list)
    entry_points: List[dict] = field(default_factory=list)
    dangerous_sinks: List[dict] = field(default_factory=list)


class SecuritySentinelAgent(BaseAgent):
    """安全哨兵 Agent"""

    name = "security_sentinel"
    description = "网络安全深度审查: OWASP Top10 / CWE / 敏感信息 / 项目级威胁建模"
    icon = "security_sentinel"
    color = "#D93B3B"
    category = "security"
    skills = (
        "OWASP Top10",
        "CWE 漏洞分类",
        "敏感信息扫描",
        "跨文件威胁建模",
        "合规检查",
        "POC 演示",
    )

    def __init__(self):
        super().__init__(
            system_prompt=SYSTEM_PROMPT,
            temperature=0.1,
            max_tokens=4096,
        )
        self._db: Optional[Session] = None
        self._user: Optional[User] = None

    # ---- 注入 ----

    def inject(self, db: Session, user: Optional[User] = None) -> None:
        self._db = db
        self._user = user

    def _ensure_db(self) -> Optional[AgentResult]:
        if self._db is None:
            return AgentResult(success=False, error="DB 未注入")
        return None

    def _authz_project(self, project: Project) -> Optional[AgentResult]:
        if self._user and self._user.role != "admin" and project.user_id != self._user.id:
            return AgentResult(success=False, error="无权访问该项目")
        return None

    def _authz_task(self, task: ReviewTask) -> Optional[AgentResult]:
        if self._user and self._user.role != "admin" and task.user_id != self._user.id:
            return AgentResult(success=False, error="无权访问该任务")
        return None

    def _authz_file(self, file: CodeFile) -> Optional[AgentResult]:
        if self._user is None or self._user.role == "admin":
            return None
        project = self._db.get(Project, file.project_id)
        if project is None or project.user_id != self._user.id:
            return AgentResult(success=False, error="无权访问该文件")
        return None

    # ---- checklist ----

    def get_checklist(self) -> dict:
        owasp = [
            {
                "code": code,
                "name": cn,
                "owasp": f"{code}:2021-{en}",
                "cwe": "",
                "description": en,
            }
            for code, en, cn in _OWASP_TOP10_2021
        ]
        secret_items = [
            {
                "code": p["name"].replace(" ", "_").lower(),
                "name": p["name"],
                "owasp": p["owasp"],
                "cwe": p["cwe"],
                "description": p["description"],
            }
            for p in list_patterns()
        ]
        static_items = [
            {
                "code": r["code"],
                "name": r["name"],
                "owasp": r["owasp"],
                "cwe": r["cwe"],
                "description": r["description"],
            }
            for r in list_static_rules()
        ]
        return {
            "owasp_top10": owasp,
            "secret_patterns": secret_items,
            "static_rules": static_items,
        }

    # ============ 单文件扫描 ============

    def scan_file(self, file_id: int, scan_depth: str = "standard",
                  ctx: Optional[AgentContext] = None) -> AgentResult:
        """单文件深度安全扫描"""
        if (err := self._ensure_db()) is not None:
            return err
        if scan_depth not in {"quick", "standard", "deep"}:
            return AgentResult(success=False, error=f"不支持的 scan_depth: {scan_depth}")

        file = self._db.get(CodeFile, file_id)
        if file is None or file.status != "active":
            return AgentResult(success=False, error="代码文件不存在或已删除")
        if (err := self._authz_file(file)) is not None:
            return err

        t0 = time.time()
        self._emit(
            AgentEventType.DISPATCH, ctx,
            message=f"开始扫描文件 {file.file_name}",
            payload={"scope": "file", "file_id": file_id, "scan_depth": scan_depth},
        )

        findings: List[dict] = []
        # 1) 正则秘钥扫描(无 token 成本)
        secret_findings = self._regex_findings(file)
        findings.extend(secret_findings)

        # 2) 静态语义规则(无 token 成本) — v2.1.1 新增
        static_findings = self._static_findings(file)
        findings.extend(static_findings)

        # 3) LLM 审查(quick 跳过大文件)
        if not (scan_depth == "quick" and len(file.content or "") > 12000):
            llm_findings = self._llm_findings_for_file(file, ctx=ctx, scan_depth=scan_depth)
            findings.extend(llm_findings)

        duration_ms = int((time.time() - t0) * 1000)
        risk_score = self._compute_risk_score(findings)
        summary = self._build_file_summary(file, findings)

        self._emit(
            AgentEventType.COMPLETE, ctx,
            message=f"{file.file_name} 扫描完成",
            payload={
                "findings_count": len(findings),
                "risk_score": risk_score,
                "duration_ms": duration_ms,
            },
        )

        return AgentResult(
            success=True,
            data={
                "findings": findings,
                "threat_model": None,
                "compliance": self._compute_compliance(findings),
                "risk_score": risk_score,
                "summary": summary,
                "file_count": 1,
                "duration_ms": duration_ms,
            },
            model=self._model,
            duration_ms=duration_ms,
        )

    # ============ 任务复审 ============

    def scan_task(self, task_id: int,
                  ctx: Optional[AgentContext] = None) -> AgentResult:
        """对已完成审查任务做安全复审:仅给安全类 issue 补 OWASP/CWE 标签

        不调用 LLM,基于已落库 ReviewIssue 的 title/description 做规则化标记。
        """
        if (err := self._ensure_db()) is not None:
            return err
        task = self._db.get(ReviewTask, task_id)
        if task is None:
            return AgentResult(success=False, error="审查任务不存在")
        if (err := self._authz_task(task)) is not None:
            return err

        t0 = time.time()
        self._emit(
            AgentEventType.DISPATCH, ctx,
            message=f"任务 #{task_id} 安全复审开始",
            payload={"scope": "task", "task_id": task_id},
        )

        rows = (
            self._db.query(ReviewIssue)
            .filter(
                ReviewIssue.task_id == task_id,
                ReviewIssue.issue_type == "安全漏洞",
            )
            .all()
        )
        findings: List[dict] = []
        for issue in rows:
            owasp, cwe = self._infer_owasp_cwe(issue.title or "", issue.description or "")
            findings.append({
                "title": issue.title or "安全问题",
                "category": "安全漏洞",
                "owasp": owasp,
                "cwe": cwe,
                "severity": issue.severity or "中",
                "file_path": issue.file_name or "",
                "file_id": issue.file_id,
                "lines": (
                    f"L{issue.line_number}-L{issue.end_line}"
                    if issue.end_line and issue.end_line != issue.line_number
                    else (f"L{issue.line_number}" if issue.line_number else "文件级")
                ),
                "line_number": issue.line_number or 0,
                "end_line": issue.end_line or 0,
                "evidence": "",
                "exploit_scenario": issue.description or "",
                "fix_suggestion": issue.suggestion or "",
                "references": [],
                "confidence": 0.9,
                "source": "task_review",
            })

        duration_ms = int((time.time() - t0) * 1000)
        risk_score = self._compute_risk_score(findings)
        summary = (
            f"任务 #{task_id} 已有 {len(findings)} 条安全类问题,"
            f"已自动打 OWASP/CWE 标签(基于关键词推断)。"
            if findings else f"任务 #{task_id} 暂未检出安全类问题。"
        )

        self._emit(
            AgentEventType.COMPLETE, ctx,
            message=f"任务 #{task_id} 复审完成",
            payload={"findings_count": len(findings), "duration_ms": duration_ms},
        )

        return AgentResult(
            success=True,
            data={
                "findings": findings,
                "threat_model": None,
                "compliance": self._compute_compliance(findings),
                "risk_score": risk_score,
                "summary": summary,
                "file_count": 0,
                "duration_ms": duration_ms,
            },
            model=self._model,
            duration_ms=duration_ms,
        )

    # ============ 项目级 ============

    def scan_project(self, project_id: int, top_n: int = 50,
                     trace_dataflow: bool = True,
                     ctx: Optional[AgentContext] = None) -> AgentResult:
        """项目级威胁建模:聚合所有文件入口 + 跨文件数据流"""
        if (err := self._ensure_db()) is not None:
            return err
        project = self._db.get(Project, project_id)
        if project is None or project.status == "deleted":
            return AgentResult(success=False, error="项目不存在或已删除")
        if (err := self._authz_project(project)) is not None:
            return err

        t0 = time.time()
        files = (
            self._db.query(CodeFile)
            .filter(CodeFile.project_id == project_id, CodeFile.status == "active")
            .all()
        )
        if not files:
            return AgentResult(success=False, error="项目下没有可扫描的代码文件")

        # 优先扫描"危险文件":含 api/controller/view/auth/sql/raw 关键字
        files_sorted = self._prioritize_files(files)[:top_n]
        self._emit(
            AgentEventType.DISPATCH, ctx,
            message=f"项目 #{project_id} 开始扫描 {len(files_sorted)} 个文件",
            payload={
                "scope": "project",
                "project_id": project_id,
                "file_count": len(files_sorted),
                "trace_dataflow": trace_dataflow,
            },
        )

        all_findings: List[dict] = []
        all_entries: List[dict] = []
        all_sinks: List[dict] = []
        for idx, file in enumerate(files_sorted):
            # 正则
            all_findings.extend(self._regex_findings(file))
            # 静态语义规则 (v2.1.1)
            all_findings.extend(self._static_findings(file))
            # LLM
            chunk_result = self._llm_audit_collect(file, ctx=ctx, scan_depth="standard")
            all_findings.extend(chunk_result.findings)
            for ep in chunk_result.entry_points:
                ep["file"] = file.file_path or file.file_name
                all_entries.append(ep)
            for sk in chunk_result.dangerous_sinks:
                sk["file"] = file.file_path or file.file_name
                all_sinks.append(sk)

            self._emit(
                AgentEventType.PROGRESS, ctx,
                message=f"已完成 {idx + 1}/{len(files_sorted)} ({file.file_name})",
                payload={
                    "index": idx + 1,
                    "total": len(files_sorted),
                    "findings_count": len(all_findings),
                },
            )

        # 数据流分析(第二轮 LLM)
        threat_model: dict = {
            "entry_points": all_entries[:30],
            "data_flows": [],
            "attack_surface_summary": (
                f"扫描入口 {len(all_entries)} 处,危险接收点 {len(all_sinks)} 处。"
            ),
        }
        if trace_dataflow and all_entries and all_sinks:
            self._emit(
                AgentEventType.PROGRESS, ctx,
                message="开始跨文件数据流分析",
                payload={"phase": "dataflow_analysis"},
            )
            data_flows = self._llm_dataflow_analysis(
                all_entries, all_sinks, project_name=project.project_name, ctx=ctx,
            )
            threat_model["data_flows"] = data_flows
            # 升级出现在数据流上的 finding 严重度
            self._upgrade_findings_on_dataflow(all_findings, data_flows)

        duration_ms = int((time.time() - t0) * 1000)
        risk_score = self._compute_risk_score(all_findings)
        sev_counts = self._severity_counts(all_findings)
        summary = (
            f"项目「{project.project_name}」共扫描 {len(files_sorted)} 个文件,"
            f"发现 {sev_counts['严重']} 处严重 / {sev_counts['高']} 处高危 / "
            f"{sev_counts['中']} 处中危 / {sev_counts['低']} 处低危。"
            f"风险评分 {risk_score}/100。"
        )

        self._emit(
            AgentEventType.COMPLETE, ctx,
            message="项目扫描完成",
            payload={
                "findings_count": len(all_findings),
                "risk_score": risk_score,
                "duration_ms": duration_ms,
            },
        )

        return AgentResult(
            success=True,
            data={
                "findings": all_findings,
                "threat_model": threat_model,
                "compliance": self._compute_compliance(all_findings),
                "risk_score": risk_score,
                "summary": summary,
                "file_count": len(files_sorted),
                "duration_ms": duration_ms,
            },
            model=self._model,
            duration_ms=duration_ms,
        )

    # ============ 内部辅助 ============

    def _regex_findings(self, file: CodeFile) -> List[dict]:
        """正则秘钥扫描 → findings"""
        if not file.content:
            return []
        matches = scan_secrets(file.content)
        out: List[dict] = []
        for m in matches:
            file_path = file.file_path or file.file_name
            out.append({
                "title": f"硬编码 {m.pattern_name}",
                "category": "敏感信息泄露",
                "owasp": m.owasp,
                "cwe": m.cwe,
                "severity": "严重",
                "file_path": file_path,
                "file_id": file.id,
                "lines": f"L{m.line_number}",
                "line_number": m.line_number,
                "end_line": m.line_number,
                "evidence": m.evidence_redacted,
                "exploit_scenario": (
                    f"{m.description}硬编码在源代码中,"
                    f"若代码被泄露(公开仓库 / 镜像 / 日志)将立即被攻击者复用。"
                ),
                "fix_suggestion": (
                    "改从环境变量、配置中心或密钥管理服务读取;"
                    "立即轮换该凭据,并将本文件加入 .gitignore 或扫描白名单。"
                ),
                "references": [
                    "https://cwe.mitre.org/data/definitions/798.html",
                    "https://owasp.org/Top10/A07_2021-Identification_and_Authentication_Failures/",
                ],
                "confidence": 0.99,
                "source": "regex",
            })
        return out

    def _static_findings(self, file: CodeFile) -> List[dict]:
        """静态语义规则 → findings (v2.1.1)

        弱加密 / 危险 API / HTTP 安全头缺失,无 token 成本,确定性命中。
        """
        if not file.content:
            return []
        matches = apply_static_rules(file.content, file.file_name)
        out: List[dict] = []
        file_path = file.file_path or file.file_name
        for m in matches:
            lines_label = f"L{m.line_number}" if m.line_number else "文件级"
            out.append({
                "title": m.rule_name,
                "category": "静态规则",
                "owasp": m.owasp,
                "cwe": m.cwe,
                "severity": m.severity,
                "file_path": file_path,
                "file_id": file.id,
                "lines": lines_label,
                "line_number": m.line_number,
                "end_line": m.line_number,
                "evidence": m.evidence_line,
                "exploit_scenario": m.description,
                "fix_suggestion": m.fix_suggestion,
                "references": [
                    f"https://cwe.mitre.org/data/definitions/{m.cwe.replace('CWE-', '')}.html"
                    if m.cwe else "",
                    f"https://owasp.org/Top10/{m.owasp.split(':')[0]}_2021/"
                    if m.owasp else "",
                ],
                "confidence": 0.95,
                "source": "static",
            })
        return out

    def list_static_rule_metadata(self) -> List[dict]:
        """供 checklist 接口暴露的静态规则元数据"""
        return list_static_rules()

    def _llm_findings_for_file(self, file: CodeFile,
                               ctx: Optional[AgentContext],
                               scan_depth: str) -> List[dict]:
        """单文件 LLM 审查(主入口,内部按分片汇总)"""
        result = self._llm_audit_collect(file, ctx=ctx, scan_depth=scan_depth)
        return result.findings

    def _llm_audit_collect(self, file: CodeFile, ctx: Optional[AgentContext],
                           scan_depth: str) -> _AuditChunkResult:
        """对一个文件分片做 LLM 审查,汇总所有分片的 findings/entries/sinks"""
        out = _AuditChunkResult()
        if not file.content:
            return out

        # 大文件保护
        if len(file.content) > 60000:
            logger.warning(
                f"[security_sentinel] 文件 {file.file_name} 过大({len(file.content)} chars),"
                f"跳过 LLM 审查,仅依赖正则。"
            )
            return out

        threshold = 8000 if scan_depth == "deep" else 6000
        chunks = chunk_code(file.content, file.language or "plaintext", threshold=threshold)
        file_path = file.file_path or file.file_name
        for chunk in chunks:
            parsed = self._llm_audit_chunk(
                code=chunk.text,
                language=file.language or "plaintext",
                file_path=file_path,
                line_offset=chunk.start_line,
                ctx=ctx,
            )
            if not parsed:
                continue
            for raw in parsed.get("findings") or []:
                normalized = self._normalize_finding(
                    raw, file=file, line_offset=chunk.start_line,
                )
                if normalized:
                    out.findings.append(normalized)
            for ep in parsed.get("entry_points") or []:
                if isinstance(ep, dict):
                    ep_line = self._coerce_int(ep.get("line"), 0)
                    if ep_line:
                        ep["line"] = ep_line + chunk.start_line
                    out.entry_points.append(ep)
            for sk in parsed.get("dangerous_sinks") or []:
                if isinstance(sk, dict):
                    sk_line = self._coerce_int(sk.get("line"), 0)
                    if sk_line:
                        sk["line"] = sk_line + chunk.start_line
                    out.dangerous_sinks.append(sk)
        return out

    def _llm_audit_chunk(self, code: str, language: str, file_path: str,
                        line_offset: int,
                        ctx: Optional[AgentContext]) -> Optional[dict]:
        """单分片 LLM 审查 → 已解析 dict;失败返回 None 不中断流程"""
        user_msg = self._build_audit_prompt(code, language, file_path, line_offset)
        result = self.call_json(user_msg, ctx=ctx)
        if not result.success:
            logger.warning(f"[security_sentinel] LLM 调用失败: {result.error}")
            return None
        if not isinstance(result.data, dict):
            logger.warning("[security_sentinel] LLM 返回非 JSON 对象")
            return None
        return result.data

    def _build_audit_prompt(self, code: str, language: str,
                            file_path: str, line_offset: int) -> str:
        return (
            "请对以下代码做网络安全审查。\n\n"
            "## 检查范围(若不适用则跳过,不要硬凑)\n"
            "1. 注入类:SQL/Command/LDAP/XPath/模板注入\n"
            "2. 跨站脚本 XSS / CSRF / SSRF / Open Redirect\n"
            "3. 反序列化 / RCE / 文件上传 / 路径遍历\n"
            "4. 越权:水平/垂直越权,IDOR\n"
            "5. 认证授权:弱密码、会话固定、JWT 缺陷、OAuth 配置错误\n"
            "6. 加密:弱算法、ECB 模式、未校验证书、随机数不安全\n"
            "7. 敏感信息:硬编码秘钥、日志泄露 PII、错误堆栈泄露\n"
            "8. 业务逻辑:竞态条件、整数溢出、订单价格篡改\n\n"
            "## 严格按此 JSON Schema 输出(只输出 JSON,无其他文字):\n"
            "{\n"
            '  "findings": [{\n'
            '    "title": "...",\n'
            '    "category": "...",\n'
            '    "owasp": "A03:2021-Injection",\n'
            '    "cwe": "CWE-89",\n'
            '    "severity": "严重|高|中|低",\n'
            '    "line_start": 12,\n'
            '    "line_end": 18,\n'
            '    "evidence": "关键代码片段(1-3 行)",\n'
            '    "exploit_scenario": "30-200 字攻击场景",\n'
            '    "fix_suggestion": "30-200 字修复方案",\n'
            '    "references": ["https://owasp.org/..."],\n'
            '    "confidence": 0.85\n'
            "  }],\n"
            '  "entry_points": [{"name": "<函数名>", "line": 数字, '
            '"input_source": "HTTP body | query | header"}],\n'
            '  "dangerous_sinks": [{"name": "<函数名>", "line": 数字, '
            '"sink_type": "SQL | exec | open | requests"}]\n'
            "}\n\n"
            "## 严格约束\n"
            "- 不报告代码风格/命名/注释类问题\n"
            "- 不臆造漏洞;不确定的标记 confidence < 0.6\n"
            "- 行号是当前代码块的相对行号(后端会自动加偏移)\n"
            "- 输出纯 JSON,不要 markdown 围栏,不要解释\n\n"
            f"## 代码信息\n"
            f"- 文件: {file_path}\n"
            f"- 语言: {language}\n"
            f"- 行号偏移: {line_offset}\n\n"
            f"## 代码内容\n"
            f"```{language}\n{code}\n```"
        )

    def _llm_dataflow_analysis(self, entries: List[dict], sinks: List[dict],
                               project_name: str,
                               ctx: Optional[AgentContext]) -> List[dict]:
        """第二轮 LLM:跨文件数据流推断"""
        def _short(items: List[dict], limit: int = 30) -> List[dict]:
            return items[:limit]

        user_msg = (
            f"项目「{project_name}」入口/接收点清单(已截断到 30 条以内):\n\n"
            f"## 入口 entry_points\n"
            f"{json_lib.dumps(_short(entries), ensure_ascii=False, indent=2)}\n\n"
            f"## 危险接收点 dangerous_sinks\n"
            f"{json_lib.dumps(_short(sinks), ensure_ascii=False, indent=2)}\n\n"
            "请推断哪些入口数据流可以通过 import / 函数调用 / 路由抵达哪些接收点。\n"
            "对每条可达路径输出 JSON 对象,字段:\n"
            "- from: 入口位置(file:function 或 file:line)\n"
            "- via: 中间经过的函数/模块列表(string 数组)\n"
            "- to: 抵达的危险接收点\n"
            "- risk_type: 攻击类型(SQL 注入 / RCE / SSRF / XSS / 越权 等)\n"
            "- severity: 严重/高/中/低\n\n"
            '严格输出 JSON: {"data_flows": [...]} ,无可达路径时 data_flows 为 []。'
        )
        result = self.call_json(user_msg, ctx=ctx)
        if not result.success or not isinstance(result.data, dict):
            return []
        raw_flows = result.data.get("data_flows") or []
        if not isinstance(raw_flows, list):
            return []
        flows: List[dict] = []
        for f in raw_flows:
            if not isinstance(f, dict):
                continue
            flows.append({
                "from": str(f.get("from") or ""),
                "via": [str(v) for v in (f.get("via") or []) if v],
                "to": str(f.get("to") or ""),
                "risk_type": str(f.get("risk_type") or ""),
                "severity": (
                    f.get("severity") if f.get("severity") in _ALLOWED_SEVERITY else "中"
                ),
            })
        return flows

    def _normalize_finding(self, raw: dict, file: CodeFile,
                           line_offset: int) -> Optional[dict]:
        """把 LLM 原始 finding 标准化"""
        if not isinstance(raw, dict):
            return None
        severity = raw.get("severity") or "中"
        if severity not in _ALLOWED_SEVERITY:
            severity = "中"
        line_start = self._coerce_int(raw.get("line_start"), 0)
        line_end = self._coerce_int(raw.get("line_end"), 0)
        if line_start:
            line_start += line_offset
        if line_end:
            line_end += line_offset
        confidence_raw = raw.get("confidence")
        try:
            confidence = float(confidence_raw) if confidence_raw is not None else 0.8
        except (TypeError, ValueError):
            confidence = 0.8
        confidence = max(0.0, min(1.0, confidence))
        file_path = file.file_path or file.file_name
        return {
            "title": str(raw.get("title") or "安全问题")[:200],
            "category": str(raw.get("category") or "安全漏洞")[:50],
            "owasp": str(raw.get("owasp") or ""),
            "cwe": str(raw.get("cwe") or ""),
            "severity": severity,
            "file_path": file_path,
            "file_id": file.id,
            "lines": (
                f"L{line_start}-L{line_end}"
                if line_start and line_end and line_end != line_start
                else (f"L{line_start}" if line_start else "文件级")
            ),
            "line_number": line_start,
            "end_line": line_end,
            "evidence": str(raw.get("evidence") or "")[:500],
            "exploit_scenario": str(raw.get("exploit_scenario") or ""),
            "fix_suggestion": str(raw.get("fix_suggestion") or ""),
            "references": [str(r) for r in (raw.get("references") or []) if r][:5],
            "confidence": confidence,
            "source": "llm",
        }

    def _infer_owasp_cwe(self, title: str, description: str) -> tuple[str, str]:
        """基于关键词推断 OWASP/CWE(任务复审用,无 LLM 调用)"""
        text = f"{title} {description}".lower()
        rules: tuple[tuple[tuple[str, ...], str, str], ...] = (
            (("sql 注入", "sql注入", "sql injection"),
             "A03:2021-Injection", "CWE-89"),
            (("命令注入", "command injection"),
             "A03:2021-Injection", "CWE-78"),
            (("xss", "跨站脚本"),
             "A03:2021-Injection", "CWE-79"),
            (("ssrf", "服务端请求伪造"),
             "A10:2021-Server-Side Request Forgery", "CWE-918"),
            (("csrf", "跨站请求伪造"),
             "A01:2021-Broken Access Control", "CWE-352"),
            (("反序列化", "deserialization"),
             "A08:2021-Software and Data Integrity Failures", "CWE-502"),
            (("路径遍历", "path traversal", "directory traversal"),
             "A01:2021-Broken Access Control", "CWE-22"),
            (("越权", "idor", "broken access"),
             "A01:2021-Broken Access Control", "CWE-639"),
            (("硬编码", "hardcoded", "明文密码"),
             "A07:2021-Identification and Authentication Failures", "CWE-798"),
            (("弱加密", "md5", "sha1", "des", "ecb"),
             "A02:2021-Cryptographic Failures", "CWE-327"),
            (("jwt"), "A07:2021-Identification and Authentication Failures", "CWE-522"),
        )
        for keywords, owasp, cwe in rules:
            if isinstance(keywords, str):
                keywords = (keywords,)
            if any(k in text for k in keywords):
                return owasp, cwe
        return "", ""

    def _coerce_int(self, v, default: int = 0) -> int:
        try:
            return int(v)
        except (TypeError, ValueError):
            return default

    def _compute_risk_score(self, findings: List[dict]) -> int:
        counts = self._severity_counts(findings)
        deduct = sum(_SEVERITY_DEDUCT.get(k, 0) * v for k, v in counts.items())
        return max(0, min(100, 100 - deduct))

    def _severity_counts(self, findings: List[dict]) -> dict:
        out = {"严重": 0, "高": 0, "中": 0, "低": 0}
        for f in findings:
            sev = f.get("severity", "中")
            if sev in out:
                out[sev] += 1
        return out

    def _compute_compliance(self, findings: List[dict]) -> dict:
        """计算简易合规覆盖"""
        owasp_hits = set()
        for f in findings:
            owasp = f.get("owasp") or ""
            if owasp.startswith("A") and ":" in owasp:
                owasp_hits.add(owasp.split(":")[0])
        return {
            "owasp_coverage": sorted(owasp_hits),
            "gb_t_22239": (
                f"等保 2.0 应用安全相关命中风险 {len(owasp_hits)} 类"
                if owasp_hits else "未触及等保 2.0 应用安全条款"
            ),
        }

    def _prioritize_files(self, files: List[CodeFile]) -> List[CodeFile]:
        """高危关键词文件优先扫描"""
        keywords = (
            "api", "controller", "route", "view", "handler",
            "auth", "login", "user", "admin", "service",
            "sql", "query", "db", "model",
        )

        def score(f: CodeFile) -> int:
            name = (f.file_path or f.file_name).lower()
            return -sum(1 for k in keywords if k in name)

        return sorted(files, key=score)

    def _upgrade_findings_on_dataflow(self, findings: List[dict],
                                     data_flows: List[dict]) -> None:
        """若 finding 出现在 dataflow 链路上,severity 升一档"""
        if not data_flows or not findings:
            return
        path_files = set()
        for flow in data_flows:
            for loc in [flow.get("from", ""), flow.get("to", "")] + list(flow.get("via") or []):
                if ":" in loc:
                    path_files.add(loc.split(":")[0].strip())
                elif loc:
                    path_files.add(loc.strip())
        upgrade = {"低": "中", "中": "高", "高": "严重", "严重": "严重"}
        for f in findings:
            fp = f.get("file_path", "")
            if any(p and p in fp for p in path_files):
                f["severity"] = upgrade.get(f.get("severity", "中"), f.get("severity"))

    def _build_file_summary(self, file: CodeFile, findings: List[dict]) -> str:
        if not findings:
            return f"文件 {file.file_name} 未发现明显安全风险。"
        counts = self._severity_counts(findings)
        return (
            f"文件 {file.file_name} 共发现 {len(findings)} 处安全问题:"
            f"严重 {counts['严重']} · 高 {counts['高']} · "
            f"中 {counts['中']} · 低 {counts['低']}。"
        )
