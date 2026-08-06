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
import re
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Iterator, List, Optional, Tuple

from loguru import logger
from sqlalchemy.orm import Session

from app.agents.base import AgentContext, AgentResult, BaseAgent
from app.agents.contracts import compose_system_prompt
from app.agents.events import AgentEventType
from app.ai.code_chunker import chunk_code
from app.ai.security_patterns import list_patterns, scan_secrets
from app.ai.security_static_rules import apply_static_rules, list_static_rules
from app.models.code_file import CodeFile
from app.models.project import Project
from app.models.review_issue import ReviewIssue
from app.models.review_task import ReviewTask
from app.models.user import User

if TYPE_CHECKING:
    from app.ai.static_analyzer import Finding

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
    success: bool = True
    error: str = ""


@dataclass
class _ProjectAuditPart:
    """项目级语义审计中的一段源码及其原文件定位。"""

    file: CodeFile
    text: str
    start_line: int


_PROJECT_PART_CHARS = 24_000
_PROJECT_BATCH_CHARS = 240_000
_TRIAGE_PER_FILE_SEMANTIC_CHARS = 240_000
_TRIAGE_SEMANTIC_BUDGET_CHARS = 2_400_000
_PROJECT_RISK_RE = re.compile(
    r"(?i)(exec\s*\(|system\s*\(|shell_exec\s*\(|eval\s*\(|unserialize\s*\(|"
    r"select\s+.+\s+from|insert\s+into|update\s+.+\s+set|delete\s+from|"
    r"requests?\.|curl_|file_get_contents\s*\(|\$_(?:GET|POST|REQUEST|COOKIE|FILES)|"
    r"password|token|secret|authorize|permission|route|controller|handler)"
)


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
            system_prompt=compose_system_prompt(self.name, SYSTEM_PROMPT),
            temperature=0.1,
            max_tokens=4096,
        )
        self._db: Optional[Session] = None
        self._user: Optional[User] = None

    def _init_skills(self) -> None:
        """子类 override:挂载 SecuritySentinelSelfImprovementSkill + SecuritySentinelProactiveSkill

        将安全哨兵 Agent 的自进化与主动监测能力下沉到 Skill,通过 SkillRegistry
        统一注册,供 Orchestrator.invoke_skill / ChatPlanner 查询调用。
        """
        from app.agents.skills.security_sentinel import (
            SecuritySentinelProactiveSkill,
            SecuritySentinelSelfImprovementSkill,
        )

        self.attach_skill(SecuritySentinelSelfImprovementSkill(self.name))
        self.attach_skill(SecuritySentinelProactiveSkill(self.name))

    # ---- 注入 ----

    def inject(self, db: Session, user: Optional[User] = None) -> None:
        self._db = db
        self._user = user

    # ---- review_service 主流程集成入口(v2 新增 2026-06-25)----

    def scan_file_for_review(
        self,
        *,
        code: str,
        language: str,
        file_name: str,
        line_offset: int = 0,
        experience_section: str = "",
        api_config=None,
        ctx: Optional[AgentContext] = None,
    ) -> AgentResult:
        """供 review_service 主流程调用的安全审查入口(双引擎之引擎2:LLM 安全深度审查)

        与 CodeReviewerAgent.execute_review() 同结构,返回 AgentResult,
        data["issues"] 为 List[Finding](与 static_analyzer.Finding 同结构)。
        复用 _build_audit_prompt() 与 _normalize_finding() 逻辑。

        Args:
            code: 代码内容(单分片)
            language: 编程语言标识
            file_name: 文件名(含扩展名)
            line_offset: 行号偏移(分片时使用)
            experience_section: 历史经验参考段落(自进化注入,可空,本 Agent 暂不使用)
            api_config: 可选,用户自定义 API 配置;为 None 时用系统默认
            ctx: Agent 上下文(含 task_id/user_id/project_id/file_id/trace_id)

        Returns:
            AgentResult: data["issues"] 为 List[Finding],data["summary"] 为整体评价;
                         失败时 success=False,error 字段含错误信息
        """
        # 1. 复用 _build_audit_prompt 生成安全审查 prompt
        user_msg = self._build_audit_prompt(code, language, file_name, line_offset)

        # 2. 通过 BaseAgent.call_json 调用 LLM(自动 emit 事件、重试)
        result = self.call_json(user_msg, ctx=ctx, api_config=api_config)
        if not result.success:
            return result

        if not isinstance(result.data, dict):
            return AgentResult(
                success=False,
                error="[security_sentinel] LLM 返回非 JSON 对象",
                model=result.model,
                duration_ms=result.duration_ms,
                tokens=result.tokens,
            )

        # 3. 解析 findings 数组,转换为 Finding 列表
        raw_findings = result.data.get("findings") or []
        if not isinstance(raw_findings, list):
            raw_findings = []

        findings: List[Finding] = []
        # 构造一个临时 CodeFile-like 对象供 _normalize_finding 使用
        class _FileStub:
            pass
        file_stub = _FileStub()
        file_stub.file_path = file_name
        file_stub.file_name = file_name
        file_stub.id = (ctx.file_id if ctx and ctx.file_id else 0)

        for raw in raw_findings:
            if not isinstance(raw, dict):
                continue
            normalized = self._normalize_finding(
                raw, file_stub, line_offset=0, code=code,
            )
            if not normalized:
                continue
            findings.append(_normalized_dict_to_finding(normalized))

        # 4. 合并 summary(若 LLM 返回了 summary 字段则用,否则用默认)
        summary = str(result.data.get("summary") or "") or (
            f"安全审查完成,共发现 {len(findings)} 条安全问题。"
        )

        return AgentResult(
            success=True,
            data={
                "issues": findings,
                "summary": summary,
                "score": _compute_security_score(findings),
            },
            model=result.model,
            duration_ms=result.duration_ms,
            tokens=result.tokens,
        )

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
                     ctx: Optional[AgentContext] = None,
                     scan_mode: str = "full") -> AgentResult:
        """一次性形成项目源码白盒审计结果。

        full 为默认模式，稳定遍历全部 active 文件；triage 仅兼容旧的风险
        优先抽样调用，并在结果中明确报告实际覆盖范围。
        """
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

        if scan_mode not in {"full", "triage"}:
            return AgentResult(success=False, error="不支持的 scan_mode,只能是 full 或 triage")
        files_sorted = (
            self._prioritize_files(files)[:max(1, min(200, int(top_n)))]
            if scan_mode == "triage"
            else sorted(files, key=lambda item: ((item.file_path or item.file_name or "").lower(), item.id))
        )
        self._emit(
            AgentEventType.DISPATCH, ctx,
            message=f"项目 #{project_id} 开始整包白盒审计 {len(files_sorted)}/{len(files)} 个文件",
            payload={
                "scope": "project",
                "project_id": project_id,
                "file_count": len(files_sorted),
                "total_file_count": len(files),
                "scan_mode": scan_mode,
                "trace_dataflow": trace_dataflow,
            },
        )

        all_findings: List[dict] = []
        all_entries: List[dict] = []
        all_sinks: List[dict] = []
        all_endpoints: List[dict] = []
        for idx, file in enumerate(files_sorted):
            file_path = file.file_path or file.file_name
            endpoints = self._extract_api_endpoints(file)
            all_endpoints.extend(endpoints)
            for endpoint in endpoints:
                all_entries.append({
                    "file": file_path,
                    "function": endpoint.get("handler") or endpoint.get("path") or "",
                    "line": endpoint.get("line_number") or 0,
                    "risk": f"{endpoint.get('method', '')} {endpoint.get('path', '')}".strip(),
                    "input_source": "HTTP API",
                })
            # 正则
            all_findings.extend(self._regex_findings(file))
            # 静态语义规则 (v2.1.1)
            all_findings.extend(self._static_findings(file))

            if (idx + 1) % 25 == 0 or idx + 1 == len(files_sorted):
                self._emit(
                    AgentEventType.PROGRESS, ctx,
                    message=f"全量静态分析 {idx + 1}/{len(files_sorted)}",
                    payload={
                        "phase": "static_analysis",
                        "index": idx + 1,
                        "total": len(files_sorted),
                        "findings_count": len(all_findings),
                    },
                )

        # 项目源码统一编批。一个模型请求可同时包含多个文件，并要求每条结果
        # 返回原始路径；这仍是一次项目级白盒审计，不会退化成 N 个文件任务。
        semantic_files = [
            file for file in files_sorted
            if not bool(file.is_binary) and bool((file.content or "").strip())
        ]
        total_text_chars = sum(len(file.content or "") for file in semantic_files)
        semantic_attempted_chars_by_file: dict[int, int] = {}
        semantic_chars_by_file: dict[int, int] = {}
        semantic_batch_count = 0
        semantic_failed_batch_count = 0
        audit_batches = self._project_audit_batches(
            semantic_files,
            full_content=scan_mode == "full",
        )
        for idx, batch in enumerate(audit_batches, start=1):
            semantic_batch_count = idx
            for part in batch:
                semantic_attempted_chars_by_file[part.file.id] = (
                    semantic_attempted_chars_by_file.get(part.file.id, 0) + len(part.text)
                )
            chunk_result = self._llm_project_audit_batch(batch, ctx=ctx)
            if chunk_result.success:
                for part in batch:
                    semantic_chars_by_file[part.file.id] = (
                        semantic_chars_by_file.get(part.file.id, 0) + len(part.text)
                    )
                all_findings.extend(chunk_result.findings)
                all_entries.extend(chunk_result.entry_points)
                all_sinks.extend(chunk_result.dangerous_sinks)
            else:
                semantic_failed_batch_count += 1

            self._emit(
                AgentEventType.PROGRESS, ctx,
                message=f"项目源码语义审计批次 {idx}",
                payload={
                    "phase": "semantic_analysis",
                    "index": idx,
                    "batch_file_count": len({part.file.id for part in batch}),
                    "batch_source_chars": sum(len(part.text) for part in batch),
                    "batch_success": chunk_result.success,
                    "findings_count": len(all_findings),
                },
            )

        semantic_attempted_source_chars = sum(semantic_attempted_chars_by_file.values())
        semantic_source_chars = sum(semantic_chars_by_file.values())
        semantic_truncated_files = sum(
            1 for file in semantic_files
            if semantic_chars_by_file.get(file.id, 0) < len(file.content or "")
        )
        semantic_complete = (
            semantic_failed_batch_count == 0
            and semantic_source_chars == total_text_chars
            and len(semantic_chars_by_file) == len(semantic_files)
        )

        # 数据流分析(第二轮 LLM)
        threat_model: dict = {
            "entry_points": all_entries[:30],
            "data_flows": [],
            "api_endpoints": all_endpoints[:100],
            "code_links": [],
            "attack_surface_summary": (
                f"扫描接口 {len(all_endpoints)} 个,入口 {len(all_entries)} 处,"
                f"危险接收点 {len(all_sinks)} 处。"
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
                api_endpoints=all_endpoints,
            )
            threat_model["data_flows"] = data_flows
            # 升级出现在数据流上的 finding 严重度
            self._upgrade_findings_on_dataflow(all_findings, data_flows)
        threat_model["code_links"] = self._build_code_links(
            all_endpoints, all_sinks, threat_model["data_flows"],
        )

        duration_ms = int((time.time() - t0) * 1000)
        risk_score = self._compute_risk_score(all_findings)
        sev_counts = self._severity_counts(all_findings)
        compliance = self._compute_compliance(all_findings)
        compliance.update({
            "scan_mode": scan_mode,
            "total_file_count": len(files),
            "scanned_file_count": len(files_sorted),
            "skipped_file_count": len(files) - len(files_sorted),
            "coverage_ratio": round(len(files_sorted) / len(files), 4) if files else 0,
            "truncated": bool(len(files_sorted) < len(files)),
            "requested_top_n": int(top_n),
            "static_scanned_file_count": len(files_sorted),
            "semantic_candidate_file_count": len(semantic_files),
            "semantic_file_count": len(semantic_chars_by_file),
            "binary_or_empty_file_count": len(files_sorted) - len(semantic_files),
            "semantic_batch_count": semantic_batch_count,
            "semantic_failed_batch_count": semantic_failed_batch_count,
            "semantic_attempted_source_chars": semantic_attempted_source_chars,
            "semantic_source_chars": semantic_source_chars,
            "total_text_source_chars": total_text_chars,
            "semantic_char_coverage_ratio": (
                round(semantic_source_chars / total_text_chars, 4)
                if total_text_chars else 0
            ),
            "semantic_truncated_file_count": semantic_truncated_files,
            "semantic_complete": semantic_complete,
        })
        summary = (
            f"项目「{project.project_name}」整包白盒审计覆盖 {len(files_sorted)}/{len(files)} 个文件,"
            f"发现 {sev_counts['严重']} 处严重 / {sev_counts['高']} 处高危 / "
            f"{sev_counts['中']} 处中危 / {sev_counts['低']} 处低危。"
            f"风险评分 {risk_score}/100。"
        )

        result_data = {
            "findings": all_findings,
            "threat_model": threat_model,
            "compliance": compliance,
            "risk_score": risk_score,
            "summary": summary,
            "file_count": len(files_sorted),
            "duration_ms": duration_ms,
        }
        if scan_mode == "full" and not semantic_complete:
            error = (
                "整包语义审计未完成: "
                f"成功覆盖 {semantic_source_chars}/{total_text_chars} 字符,"
                f"失败批次 {semantic_failed_batch_count}"
            )
            self._emit(
                AgentEventType.FAILED, ctx,
                message=error,
                payload={
                    "phase": "semantic_analysis",
                    "semantic_source_chars": semantic_source_chars,
                    "total_text_source_chars": total_text_chars,
                    "failed_batch_count": semantic_failed_batch_count,
                },
            )
            return AgentResult(
                success=False,
                data=result_data,
                error=error,
                model=self._model,
                duration_ms=duration_ms,
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
            data=result_data,
            model=self._model,
            duration_ms=duration_ms,
        )

    def scan_all_projects(self, top_n_per_project: int = 50,
                          trace_dataflow: bool = True,
                          ctx: Optional[AgentContext] = None) -> AgentResult:
        """全量项目安全扫描:聚合当前用户可见的全部活跃项目

        Args:
            top_n_per_project: 每个项目最多扫描的文件数量。
            trace_dataflow: 是否对每个项目启用跨文件数据流追踪。
            ctx: Agent 调用上下文。

        Returns:
            AgentResult: 复用 SecurityScanOut 结构的聚合结果。
        """
        if (err := self._ensure_db()) is not None:
            return err

        try:
            top_n = int(top_n_per_project)
        except (TypeError, ValueError):
            top_n = 50
        top_n = max(1, min(200, top_n))

        q = self._db.query(Project).filter(Project.status == "active")
        if self._user and self._user.role != "admin":
            q = q.filter(Project.user_id == self._user.id)
        projects = q.order_by(Project.id.asc()).all()

        t0 = time.time()
        self._emit(
            AgentEventType.DISPATCH, ctx,
            message=f"开始全量扫描 {len(projects)} 个项目",
            payload={
                "scope": "all_projects",
                "project_count": len(projects),
                "top_n_per_project": top_n,
                "trace_dataflow": trace_dataflow,
            },
        )

        if not projects:
            duration_ms = int((time.time() - t0) * 1000)
            return AgentResult(
                success=True,
                data={
                    "findings": [],
                    "threat_model": {
                        "entry_points": [],
                        "data_flows": [],
                        "api_endpoints": [],
                        "code_links": [],
                        "attack_surface_summary": "当前账号暂无可扫描的活跃项目。",
                    },
                    "discussion": self._build_multi_agent_discussion([], {
                        "entry_points": [],
                        "data_flows": [],
                        "api_endpoints": [],
                        "code_links": [],
                    }),
                    "compliance": {
                        "project_count": 0,
                        "scanned_project_count": 0,
                        "skipped_project_count": 0,
                        "project_errors": [],
                    },
                    "risk_score": 100,
                    "summary": "当前账号暂无可扫描的活跃项目。",
                    "file_count": 0,
                    "duration_ms": duration_ms,
                },
                model=self._model,
                duration_ms=duration_ms,
            )

        all_findings: List[dict] = []
        all_entries: List[dict] = []
        all_flows: List[dict] = []
        all_endpoints: List[dict] = []
        all_code_links: List[dict] = []
        project_errors: List[dict] = []
        total_files = 0
        scanned_projects = 0

        def project_path(project: Project, path: str) -> str:
            name = project.project_name or f"项目 #{project.id}"
            return f"{name}/{path}" if path else name

        for idx, project in enumerate(projects, start=1):
            self._emit(
                AgentEventType.PROGRESS, ctx,
                message=f"全量扫描进度 {idx}/{len(projects)}: {project.project_name}",
                payload={
                    "scope": "all_projects",
                    "index": idx,
                    "total": len(projects),
                    "project_id": project.id,
                },
            )
            result = self.scan_project(
                project.id,
                top_n=top_n,
                trace_dataflow=trace_dataflow,
                ctx=ctx,
                scan_mode="triage",
            )
            if not result.success:
                project_errors.append({
                    "project_id": project.id,
                    "project_name": project.project_name,
                    "error": result.error or "扫描失败",
                })
                continue

            data = result.data or {}
            scanned_projects += 1
            total_files += int(data.get("file_count") or 0)

            for raw in data.get("findings") or []:
                if not isinstance(raw, dict):
                    continue
                finding = dict(raw)
                finding["file_path"] = project_path(project, str(finding.get("file_path") or ""))
                all_findings.append(finding)

            threat_model = data.get("threat_model") or {}
            for raw_entry in threat_model.get("entry_points") or []:
                if not isinstance(raw_entry, dict):
                    continue
                entry = dict(raw_entry)
                entry["file"] = project_path(project, str(entry.get("file") or ""))
                all_entries.append(entry)

            for raw_flow in threat_model.get("data_flows") or []:
                if not isinstance(raw_flow, dict):
                    continue
                flow = dict(raw_flow)
                if flow.get("from"):
                    flow["from"] = project_path(project, str(flow.get("from")))
                if flow.get("to"):
                    flow["to"] = project_path(project, str(flow.get("to")))
                flow["via"] = [
                    project_path(project, str(v))
                    for v in (flow.get("via") or [])
                    if v
                ]
                all_flows.append(flow)

            for raw_endpoint in threat_model.get("api_endpoints") or []:
                if not isinstance(raw_endpoint, dict):
                    continue
                endpoint = dict(raw_endpoint)
                endpoint["file_path"] = project_path(
                    project, str(endpoint.get("file_path") or ""),
                )
                all_endpoints.append(endpoint)

            for raw_link in threat_model.get("code_links") or []:
                if not isinstance(raw_link, dict):
                    continue
                link = dict(raw_link)
                if link.get("from"):
                    link["from"] = project_path(project, str(link.get("from")))
                if link.get("to"):
                    link["to"] = project_path(project, str(link.get("to")))
                all_code_links.append(link)

        duration_ms = int((time.time() - t0) * 1000)
        risk_score = self._compute_risk_score(all_findings)
        sev_counts = self._severity_counts(all_findings)
        skipped_projects = len(projects) - scanned_projects
        compliance = self._compute_compliance(all_findings)
        compliance.update({
            "project_count": len(projects),
            "scanned_project_count": scanned_projects,
            "skipped_project_count": skipped_projects,
            "project_errors": project_errors,
        })
        summary = (
            f"全量项目扫描完成:可见项目 {len(projects)} 个,成功扫描 {scanned_projects} 个,"
            f"跳过 {skipped_projects} 个,累计扫描文件 {total_files} 个;"
            f"识别接口 {len(all_endpoints)} 个,代码联动关系 {len(all_code_links)} 条;"
            f"发现 {sev_counts['严重']} 处严重 / {sev_counts['高']} 处高危 / "
            f"{sev_counts['中']} 处中危 / {sev_counts['低']} 处低危。"
            f"综合风险评分 {risk_score}/100。"
        )
        threat_model = {
            "entry_points": all_entries[:100],
            "data_flows": all_flows[:100],
            "api_endpoints": all_endpoints[:200],
            "code_links": all_code_links[:200],
            "attack_surface_summary": (
                f"全量扫描覆盖 {scanned_projects} 个项目,"
                f"识别接口 {len(all_endpoints)} 个,入口 {len(all_entries)} 处,"
                f"跨文件攻击路径 {len(all_flows)} 条,代码联动关系 {len(all_code_links)} 条。"
            ),
        }
        discussion = self._build_multi_agent_discussion(
            all_findings, threat_model, project_count=scanned_projects,
        )

        self._emit(
            AgentEventType.COMPLETE, ctx,
            message="全量项目扫描完成",
            payload={
                "scope": "all_projects",
                "project_count": len(projects),
                "scanned_project_count": scanned_projects,
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
                "discussion": discussion,
                "compliance": compliance,
                "risk_score": risk_score,
                "summary": summary,
                "file_count": total_files,
                "duration_ms": duration_ms,
            },
            model=self._model,
            duration_ms=duration_ms,
        )

    # ============ 内部辅助 ============

    def _extract_api_endpoints(self, file: CodeFile) -> List[dict]:
        """从常见框架代码中抽取接口定义和前端接口调用封装。

        Args:
            file: 已落库的代码文件对象。

        Returns:
            List[dict]: 接口方法、路径、定义位置、处理函数和认证线索。
        """
        content = file.content or ""
        if not content:
            return []

        file_path = file.file_path or file.file_name
        lines = content.splitlines()
        endpoints: List[dict] = []
        seen: set[tuple[str, str, str, int, str]] = set()

        def add_endpoint(method: str, path: str, line_no: int,
                         source: str, handler: str = "") -> None:
            method_norm = (method or "GET").upper()
            path_norm = (path or "").strip()
            if not path_norm:
                return
            if not path_norm.startswith(("/", "http://", "https://")):
                path_norm = f"/{path_norm.lstrip('/')}"
            key = (method_norm, path_norm, file_path, line_no, source)
            if key in seen:
                return
            seen.add(key)
            endpoints.append({
                "method": method_norm,
                "path": path_norm,
                "file_path": file_path,
                "line_number": line_no,
                "handler": handler or self._next_handler_name(lines, line_no - 1),
                "auth_hint": self._endpoint_auth_hint(lines, line_no - 1),
                "source": source,
            })

        fastapi_pattern = re.compile(
            r"@\s*(?:[\w_]+\.)?(?:router|app|api)\."
            r"(get|post|put|delete|patch|options|head)\(\s*['\"]([^'\"]+)['\"]",
            re.IGNORECASE,
        )
        flask_route_pattern = re.compile(
            r"@\s*(?:[\w_]+\.)?(?:app|blueprint|bp|router)\.route\("
            r"\s*['\"]([^'\"]+)['\"](?P<opts>.*)",
            re.IGNORECASE,
        )
        express_pattern = re.compile(
            r"\b(?:app|router)\."
            r"(get|post|put|delete|patch|options|head|all)\(\s*['\"`]([^'\"`]+)['\"`]",
            re.IGNORECASE,
        )
        spring_method_pattern = re.compile(
            r"@\s*(Get|Post|Put|Delete|Patch)Mapping\("
            r"\s*(?:value\s*=\s*|path\s*=\s*)?['\"]([^'\"]+)['\"]",
            re.IGNORECASE,
        )
        spring_request_pattern = re.compile(
            r"@\s*RequestMapping\((?P<body>[^)]*)\)",
            re.IGNORECASE,
        )
        django_path_pattern = re.compile(
            r"\b(?:path|re_path)\(\s*[rR]?['\"]([^'\"]+)['\"]",
            re.IGNORECASE,
        )
        http_client_pattern = re.compile(
            r"\b(?:axios|request|apiClient|http)\."
            r"(get|post|put|delete|patch)\(\s*['\"`]([^'\"`]+)['\"`]",
            re.IGNORECASE,
        )

        for idx, line in enumerate(lines, start=1):
            for match in fastapi_pattern.finditer(line):
                add_endpoint(match.group(1), match.group(2), idx, "python_route")

            flask_match = flask_route_pattern.search(line)
            if flask_match:
                path = flask_match.group(1)
                methods = self._parse_route_methods(flask_match.group("opts"))
                for method in methods:
                    add_endpoint(method, path, idx, "python_route")

            for match in express_pattern.finditer(line):
                add_endpoint(match.group(1), match.group(2), idx, "node_route")

            spring_match = spring_method_pattern.search(line)
            if spring_match:
                method = spring_match.group(1).replace("Mapping", "")
                add_endpoint(method, spring_match.group(2), idx, "java_route")

            request_match = spring_request_pattern.search(line)
            if request_match:
                body = request_match.group("body")
                path_match = re.search(
                    r"(?:value|path)\s*=\s*['\"]([^'\"]+)['\"]|['\"]([^'\"]+)['\"]",
                    body,
                    re.IGNORECASE,
                )
                if path_match:
                    path = path_match.group(1) or path_match.group(2)
                    methods = re.findall(r"RequestMethod\.([A-Z]+)", body)
                    for method in (methods or ["ANY"]):
                        add_endpoint(method, path, idx, "java_route")

            django_match = django_path_pattern.search(line)
            if django_match:
                add_endpoint("ANY", django_match.group(1), idx, "django_url")

            for match in http_client_pattern.finditer(line):
                add_endpoint(match.group(1), match.group(2), idx, "http_client_wrapper")

        return endpoints[:200]

    def _parse_route_methods(self, route_options: str) -> List[str]:
        """解析 Flask/Django 风格路由声明中的 HTTP method 列表。

        Args:
            route_options: 路由装饰器中 path 之后的参数文本。

        Returns:
            List[str]: 识别到的方法列表,未声明时默认 GET。
        """
        if not route_options:
            return ["GET"]
        match = re.search(
            r"methods\s*=\s*(?:\[|\()(?P<methods>[^\]\)]+)(?:\]|\))",
            route_options,
            re.IGNORECASE,
        )
        if not match:
            return ["GET"]
        methods = re.findall(r"['\"]([A-Z]+)['\"]", match.group("methods"), re.IGNORECASE)
        return [m.upper() for m in methods] or ["GET"]

    def _next_handler_name(self, lines: List[str], start_idx: int) -> str:
        """从路由声明后的几行代码中推断处理函数名称。

        Args:
            lines: 文件内容按行切分后的列表。
            start_idx: 路由声明行的零基索引。

        Returns:
            str: 处理函数名称,无法判断时为空字符串。
        """
        for idx in range(start_idx + 1, min(len(lines), start_idx + 8)):
            line = lines[idx].strip()
            py_match = re.search(r"\b(?:async\s+def|def)\s+([\w_]+)\s*\(", line)
            if py_match:
                return py_match.group(1)
            js_match = re.search(
                r"\b(?:async\s+)?(?:function\s+)?([A-Za-z_$][\w$]*)\s*(?:=|\()",
                line,
            )
            if js_match and js_match.group(1) not in {"return", "if", "for", "while"}:
                return js_match.group(1)
            java_match = re.search(
                r"\b(?:public|private|protected)\s+[\w<>\[\],\s]+\s+(\w+)\s*\(",
                line,
            )
            if java_match:
                return java_match.group(1)
        return ""

    def _endpoint_auth_hint(self, lines: List[str], start_idx: int) -> str:
        """判断接口附近是否存在认证或授权相关线索。

        Args:
            lines: 文件内容按行切分后的列表。
            start_idx: 接口声明行的零基索引。

        Returns:
            str: 认证/授权线索说明。
        """
        lower = "\n".join(
            lines[max(0, start_idx - 3): min(len(lines), start_idx + 10)]
        ).lower()
        auth_keywords = (
            "depends", "get_current_user", "login_required", "permission",
            "authorize", "authenticated", "jwt", "bearer", "token",
            "role", "admin", "principal", "security", "preauthorize",
            "rolesallowed", "current_user", "session",
        )
        if any(keyword in lower for keyword in auth_keywords):
            return "发现认证/权限线索"
        return "未发现明显认证线索"

    def _build_code_links(self, api_endpoints: List[dict], sinks: List[dict],
                          data_flows: List[dict]) -> List[dict]:
        """生成接口、数据流和危险接收点之间的代码联动关系。

        Args:
            api_endpoints: 接口扫描结果。
            sinks: LLM 或规则识别到的危险接收点。
            data_flows: 跨文件数据流推断结果。

        Returns:
            List[dict]: 用于前端展示的联动关系列表。
        """
        links: List[dict] = []
        seen: set[tuple[str, str, str]] = set()

        def add_link(from_loc: str, to_loc: str, relation: str,
                     risk_type: str, severity: str) -> None:
            if not from_loc or not to_loc:
                return
            sev = severity if severity in _ALLOWED_SEVERITY else "中"
            key = (from_loc, to_loc, relation)
            if key in seen:
                return
            seen.add(key)
            links.append({
                "from": from_loc,
                "to": to_loc,
                "relation": relation,
                "risk_type": risk_type or "代码联动风险",
                "severity": sev,
            })

        for flow in data_flows or []:
            add_link(
                str(flow.get("from") or ""),
                str(flow.get("to") or ""),
                "跨文件数据流",
                str(flow.get("risk_type") or ""),
                str(flow.get("severity") or "中"),
            )

        sinks_by_file: dict[str, List[dict]] = {}
        for sink in sinks or []:
            sink_file = str(sink.get("file") or "")
            if sink_file:
                sinks_by_file.setdefault(sink_file, []).append(sink)

        for endpoint in api_endpoints or []:
            endpoint_file = str(endpoint.get("file_path") or "")
            if not endpoint_file:
                continue
            endpoint_loc = (
                f"{endpoint_file}:{endpoint.get('handler') or endpoint.get('path') or ''}"
            )
            for sink in sinks_by_file.get(endpoint_file, []):
                sink_text = str(sink.get("sink_type") or sink.get("name") or "")
                sink_lower = sink_text.lower()
                if "sql" in sink_lower:
                    risk_type, severity = "SQL 注入", "高"
                elif "exec" in sink_lower or "command" in sink_lower:
                    risk_type, severity = "命令执行/RCE", "严重"
                elif "request" in sink_lower or "http" in sink_lower:
                    risk_type, severity = "SSRF", "高"
                elif "open" in sink_lower or "file" in sink_lower:
                    risk_type, severity = "路径遍历/任意文件访问", "高"
                else:
                    risk_type, severity = "危险接收点", "中"
                sink_loc = f"{endpoint_file}:{sink.get('name') or sink_text or 'sink'}"
                add_link(endpoint_loc, sink_loc, "接口到同文件危险接收点", risk_type, severity)
                if len(links) >= 200:
                    return links

        return links[:200]

    def _build_multi_agent_discussion(self, findings: List[dict],
                                      threat_model: dict,
                                      project_count: int = 0) -> dict:
        """构造多 Agent 讨论式审查摘要。

        Args:
            findings: 聚合后的安全发现。
            threat_model: 聚合后的威胁模型。
            project_count: 本次成功扫描的项目数量。

        Returns:
            dict: 多 Agent 发言、共识和行动项。
        """
        counts = self._severity_counts(findings)
        endpoints = threat_model.get("api_endpoints") or []
        data_flows = threat_model.get("data_flows") or []
        code_links = threat_model.get("code_links") or []
        unauth_count = sum(
            1 for endpoint in endpoints
            if "未发现" in str(endpoint.get("auth_hint") or "")
        )

        owasp_counts: dict[str, int] = {}
        for finding in findings:
            owasp = str(finding.get("owasp") or "").strip()
            if not owasp:
                continue
            key = owasp.split(":")[0]
            owasp_counts[key] = owasp_counts.get(key, 0) + 1
        top_owasp = sorted(owasp_counts.items(), key=lambda item: item[1], reverse=True)[:3]
        top_owasp_text = "、".join(f"{code}({count})" for code, count in top_owasp) or "暂无集中 OWASP 类别"

        participants = ["安全审查 Agent", "可靠性 Agent", "性能 Agent", "可维护性 Agent", "主持 Agent"]
        turns = [
            {
                "agent_code": "security_sentinel",
                "agent_name": "安全审查 Agent",
                "role": "reviewer",
                "content": (
                    f"共发现严重 {counts['严重']} 处、高危 {counts['高']} 处。"
                    f"接口面 {len(endpoints)} 个,其中 {unauth_count} 个未发现明显认证线索。"
                    f"高频 OWASP 类别: {top_owasp_text}。"
                ),
            },
            {
                "agent_code": "reliability_agent",
                "agent_name": "可靠性 Agent",
                "role": "reviewer",
                "content": (
                    f"已识别跨文件攻击路径 {len(data_flows)} 条、代码联动关系 {len(code_links)} 条。"
                    "建议优先验证接口输入是否在服务层、数据层持续保持校验。"
                ),
            },
            {
                "agent_code": "performance_agent",
                "agent_name": "性能 Agent",
                "role": "reviewer",
                "content": (
                    f"本次覆盖 {project_count} 个项目。全量扫描会按项目逐个聚合,"
                    "大仓库建议控制每项目文件数并保留跨文件追踪开关。"
                ),
            },
            {
                "agent_code": "maintainability_agent",
                "agent_name": "可维护性 Agent",
                "role": "reviewer",
                "content": (
                    "接口、入口、危险接收点已拆成独立结构输出。"
                    "后续修复可按接口路径和联动关系定位责任模块。"
                ),
            },
        ]

        action_items: List[str] = []
        if counts["严重"] or counts["高"]:
            action_items.append("优先修复严重和高危发现,完成后重新运行全量扫描确认风险评分回升。")
        if unauth_count:
            action_items.append("复核未发现明显认证线索的接口,确认是否需要登录态、角色或权限校验。")
        if code_links:
            action_items.append("沿代码联动关系逐条验证接口输入是否能抵达 SQL、命令执行、文件或请求类危险接收点。")
        if not action_items:
            action_items.append("当前未发现高优先级风险,建议保留定期全量扫描和新增接口后的回归扫描。")

        consensus = (
            "多 Agent 共识:本次全量扫描应以接口暴露面、跨文件数据流和高危发现为主线推进整改。"
            if findings or code_links or endpoints
            else "多 Agent 共识:当前范围未形成明确攻击面,可作为基线结果保存。"
        )
        turns.append({
            "agent_code": "orchestrator",
            "agent_name": "主持 Agent",
            "role": "moderator",
            "content": consensus,
        })

        return {
            "mode": "multi_agent_summary",
            "participants": participants,
            "turns": turns,
            "consensus": consensus,
            "action_items": action_items,
        }

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
                    code=chunk.text,
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

    def _project_audit_batches(
        self,
        files: List[CodeFile],
        *,
        full_content: bool = True,
    ) -> Iterator[List[_ProjectAuditPart]]:
        """流式构建项目级多文件语义批次。

        full 模式逐字符覆盖所有可解析源码；triage 才使用风险窗口预算。
        返回迭代器以便调用方每次只保留一个批次，避免大型项目内存翻倍。
        """
        if full_content:
            ordered = sorted(
                files,
                key=lambda file: (
                    (file.file_path or file.file_name or "").lower(),
                    file.id,
                ),
            )
            remaining: Optional[int] = None
        else:
            ordered = sorted(
                files,
                key=lambda file: (
                    -self._semantic_risk_score(file),
                    (file.file_path or file.file_name or "").lower(),
                    file.id,
                ),
            )
            remaining = _TRIAGE_SEMANTIC_BUDGET_CHARS

        current: List[_ProjectAuditPart] = []
        current_chars = 0
        for file in ordered:
            content_size = len(file.content or "")
            if content_size <= 0:
                continue
            if remaining is not None and remaining <= 0:
                break
            file_budget = (
                content_size
                if remaining is None
                else min(_TRIAGE_PER_FILE_SEMANTIC_CHARS, remaining)
            )
            consumed = 0
            for part in self._semantic_parts_for_file(file, file_budget):
                path = part.file.file_path or part.file.file_name
                estimated = len(part.text) + len(path) + 120
                if current and current_chars + estimated > _PROJECT_BATCH_CHARS:
                    yield current
                    current = []
                    current_chars = 0
                current.append(part)
                current_chars += estimated
                consumed += len(part.text)
            if remaining is not None:
                remaining -= consumed
        if current:
            yield current

    def _semantic_risk_score(self, file: CodeFile) -> int:
        """仅用于模型语义上下文排序，不影响全量静态扫描覆盖。"""
        path = (file.file_path or file.file_name or "").lower()
        path_keywords = (
            "api", "controller", "route", "handler", "auth", "login",
            "admin", "service", "sql", "query", "db", "upload", "webhook",
            "config", "permission", "token", "payment", "password",
        )
        score = sum(8 for keyword in path_keywords if keyword in path)
        content = file.content or ""
        sample = content[:200_000]
        if len(content) > 200_000:
            sample += content[-200_000:]
        score += min(40, sum(1 for _ in _PROJECT_RISK_RE.finditer(sample)))
        return score

    def _semantic_parts_for_file(
        self, file: CodeFile, max_chars: int,
    ) -> Iterator[_ProjectAuditPart]:
        """按行边界流式切分单文件，并兼容超长单行。"""
        content = file.content or ""
        if not content or max_chars <= 0:
            return

        limit = min(len(content), max_chars)
        cursor = 0
        start_line = 0
        while cursor < limit:
            end = min(limit, cursor + _PROJECT_PART_CHARS)
            if end < limit:
                newline = content.rfind("\n", cursor, end)
                if newline >= cursor + (_PROJECT_PART_CHARS // 2):
                    end = newline + 1
            text = content[cursor:end]
            if not text:
                break
            yield _ProjectAuditPart(file, text, start_line)
            start_line += text.count("\n")
            cursor = end

    def _llm_project_audit_batch(
        self,
        parts: List[_ProjectAuditPart],
        ctx: Optional[AgentContext],
    ) -> _AuditChunkResult:
        """同时审查一个项目批次中的多个源码文件并恢复原文件定位。"""
        out = _AuditChunkResult()
        if not parts:
            return out

        files_by_path: dict[str, CodeFile] = {}
        sections: List[str] = []
        for part in parts:
            path = part.file.file_path or part.file.file_name
            files_by_path[path] = part.file
            sections.append(
                f"===== FILE {path} | LANGUAGE {part.file.language or 'plaintext'} "
                f"| START_LINE {part.start_line + 1} =====\n{part.text}"
            )
        source = "\n\n".join(sections)
        prompt = (
            "你正在执行一次项目级白盒安全审计。下面是同一个项目源码索引中的一个多文件批次，"
            "必须结合路由、调用、配置、数据访问和相邻模块关系分析，不能把它当成互不相关的单文件任务。\n\n"
            "逐项排查 OWASP Top10、访问控制、注入、弱加密、认证会话、反序列化、供应链、"
            "日志泄密和 SSRF；只报告代码证据可以支撑的问题。\n"
            "严格输出 JSON，结构为：\n"
            '{"findings":[{"file_path":"源码中的精确路径","title":"...","category":"...",'
            '"owasp":"A03:2021-Injection","cwe":"CWE-89","severity":"严重|高|中|低",'
            '"line_start":1,"line_end":1,"evidence":"源码原文","exploit_scenario":"...",'
            '"fix_suggestion":"...","references":[],"confidence":0.9}],'
            '"entry_points":[{"file_path":"精确路径","name":"...","line":1,'
            '"input_source":"HTTP body|query|header"}],'
            '"dangerous_sinks":[{"file_path":"精确路径","name":"...","line":1,'
            '"sink_type":"SQL|exec|open|requests"}]}\n'
            "硬约束：file_path 必须逐字取自 FILE 标记；行号必须是原文件绝对行号；"
            "evidence 必须是源码中的原文；禁止输出 Markdown 或解释。\n\n"
            f"{source}"
        )
        result = self.call_json(prompt, ctx=ctx)
        if not result.success or not isinstance(result.data, dict):
            error = result.error or "LLM 返回了无效的项目审计结果"
            logger.warning(
                f"[security_sentinel] 项目源码批次 LLM 调用失败: {error}"
            )
            return _AuditChunkResult(success=False, error=error)

        for raw in result.data.get("findings") or []:
            if not isinstance(raw, dict):
                continue
            raw_path = str(raw.get("file_path") or "").strip().removeprefix("./")
            file = files_by_path.get(raw_path)
            if file is None and len(files_by_path) == 1:
                file = next(iter(files_by_path.values()))
            if file is None:
                continue
            normalized = self._normalize_finding(
                raw,
                file=file,
                line_offset=0,
                code=file.content or "",
            )
            if normalized:
                out.findings.append(normalized)

        for key, destination in (
            ("entry_points", out.entry_points),
            ("dangerous_sinks", out.dangerous_sinks),
        ):
            for raw in result.data.get(key) or []:
                if not isinstance(raw, dict):
                    continue
                raw_path = str(raw.get("file_path") or "").strip().removeprefix("./")
                if raw_path not in files_by_path:
                    continue
                item = dict(raw)
                item["file"] = raw_path
                item.pop("file_path", None)
                item["line"] = self._coerce_int(item.get("line"), 0)
                destination.append(item)
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
            "请对以下代码做系统化网络安全审查,尽量把每一类真实存在的风险都找全,"
            "宁可多给低置信度线索,也不要漏报。\n\n"
            "## 覆盖面:逐项对照 OWASP Top10 2021 排查(命中才报,不适用就跳过,不硬凑)\n"
            "- A01 失效的访问控制:水平/垂直越权、IDOR、路径遍历、强制浏览、CORS 过宽\n"
            "- A02 加密失败:明文存储、弱哈希(MD5/SHA1)、弱算法(DES/ECB)、硬编码密钥、"
            "证书不校验、随机数不安全\n"
            "- A03 注入:SQL/NoSQL/命令/LDAP/XPath/模板注入、XSS、HTTP 响应拆分、Open Redirect\n"
            "- A04 不安全设计:缺少限流/风控、可被滥用的业务流程、TOCTOU 竞态\n"
            "- A05 安全配置错误:调试开关、默认口令、目录列举、危险 CORS/安全头缺失、错误堆栈泄露\n"
            "- A06 易受攻击与过时组件:已知漏洞依赖、过时框架、危险反序列化库\n"
            "- A07 认证与会话失败:弱口令策略、会话固定、JWT 缺陷(alg=none/弱密钥)、"
            "验证码绕过、越权重置\n"
            "- A08 软件与数据完整性失败:不受信反序列化、未校验的自动更新/插件、CI 供应链\n"
            "- A09 日志与监控失败:关键操作无审计、日志泄露 PII/凭据\n"
            "- A10 SSRF:用户可控 URL 发起请求、云元数据地址(169.254.169.254)可达\n"
            "另含业务逻辑漏洞:整数溢出、价格/数量篡改、条件竞争。\n\n"
            "## 严格按此 JSON Schema 输出(只输出 JSON,无其他文字):\n"
            "{\n"
            '  "findings": [{\n'
            '    "title": "...",\n'
            '    "category": "...",\n'
            '    "owasp": "A03:2021-Injection",\n'
            '    "cwe": "CWE-89",\n'
            '    "severity": "严重|高|中|低",\n'
            '    "line_start": 12,    // 必填:问题起始行(当前代码块相对行号)\n'
            '    "line_end": 18,      // 必填:问题结束行,单行时与 line_start 相同\n'
            '    "evidence": "从下方代码原样摘录的 1-3 行触发代码",  // 必填,不得改写\n'
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
            "## 硬约束\n"
            "- 每条 finding 必须给出 line_start 和 evidence:它们是「漏洞点」的定位依据,缺一不可;"
            "evidence 必须是下方代码里真实出现的原文,便于交叉校验行号。\n"
            "- 不报告代码风格/命名/注释类问题(那是 code_reviewer 的活)\n"
            "- 不臆造漏洞;不确定的把 confidence 标到 0.6 以下,但仍要给出 line_start 与 evidence\n"
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
                               ctx: Optional[AgentContext],
                               api_endpoints: Optional[List[dict]] = None) -> List[dict]:
        """第二轮 LLM:跨文件数据流推断"""
        def _short(items: List[dict], limit: int = 30) -> List[dict]:
            return items[:limit]

        user_msg = (
            f"项目「{project_name}」接口/入口/接收点清单(已截断到 30 条以内):\n\n"
            f"## 接口 api_endpoints\n"
            f"{json_lib.dumps(_short(api_endpoints or []), ensure_ascii=False, indent=2)}\n\n"
            f"## 入口 entry_points\n"
            f"{json_lib.dumps(_short(entries), ensure_ascii=False, indent=2)}\n\n"
            f"## 危险接收点 dangerous_sinks\n"
            f"{json_lib.dumps(_short(sinks), ensure_ascii=False, indent=2)}\n\n"
            "请推断哪些接口或入口数据流可以通过 import / 函数调用 / 路由抵达哪些接收点。\n"
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
                           line_offset: int,
                           code: Optional[str] = None) -> Optional[dict]:
        """把 LLM 原始 finding 标准化。

        Args:
            raw: LLM 返回的单条 finding。
            file: 所属代码文件(取 file_path/file_id)。
            line_offset: 分片行号偏移(相对行号 + 偏移 = 绝对行号)。
            code: 本次送审的代码块原文;当 LLM 漏给 line_start 时,用 evidence
                在其中反查真实行号,保证每条 finding 都有可定位的「漏洞点」。
        """
        if not isinstance(raw, dict):
            return None
        severity = raw.get("severity") or "中"
        if severity not in _ALLOWED_SEVERITY:
            severity = "中"
        evidence = str(raw.get("evidence") or "")[:500]
        line_start = self._coerce_int(raw.get("line_start"), 0)
        line_end = self._coerce_int(raw.get("line_end"), 0)
        # 兜底:模型漏给行号时,用 evidence 在代码块里定位,relative → +offset
        if not line_start and evidence and code:
            located = self._locate_evidence_line(code, evidence)
            if located:
                line_start = located
                if not line_end:
                    line_end = located
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
            "evidence": evidence,
            "exploit_scenario": str(raw.get("exploit_scenario") or ""),
            "fix_suggestion": str(raw.get("fix_suggestion") or ""),
            "references": [str(r) for r in (raw.get("references") or []) if r][:5],
            "confidence": confidence,
            "source": "llm",
        }

    @staticmethod
    def _locate_evidence_line(code: str, evidence: str) -> int:
        """用 evidence 原文在代码块里反查行号(1-based 相对行号),找不到返回 0。

        LLM(尤其小模型)常漏给 line_start,导致漏洞只有描述、没有定位。
        这里取 evidence 中最有辨识度的一行,在代码里做子串匹配兜底。
        """
        if not code or not evidence:
            return 0
        code_lines = code.splitlines()
        ev_lines = [ln.strip() for ln in evidence.splitlines() if ln.strip()]
        if not ev_lines:
            return 0
        needle = max(ev_lines, key=len).strip("`. ").strip()
        if len(needle) < 4:
            return 0
        for idx, ln in enumerate(code_lines, start=1):
            if needle in ln:
                return idx
        # 放宽:用前若干字符再试一次(应对 evidence 带省略号/尾部差异)
        compact = needle[:16]
        if len(compact) >= 6:
            for idx, ln in enumerate(code_lines, start=1):
                if compact in ln:
                    return idx
        return 0

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


# ============ 模块级辅助函数(v2 新增 2026-06-25)============

def _normalized_dict_to_finding(normalized: dict) -> "Finding":
    """将 _normalize_finding 输出的 dict 转换为 Finding 数据类

    Args:
        normalized: _normalize_finding 输出的标准化字典

    Returns:
        Finding: static_analyzer.Finding 实例
    """
    from app.ai.static_analyzer import Finding

    return Finding(
        line_number=int(normalized.get("line_number") or 0),
        end_line=int(normalized.get("end_line") or 0) or None,
        issue_type="安全漏洞",
        severity=str(normalized.get("severity") or "中"),
        title=str(normalized.get("title") or "安全问题"),
        description=str(normalized.get("exploit_scenario") or normalized.get("fix_suggestion") or ""),
        suggestion=str(normalized.get("fix_suggestion") or ""),
        fixed_code="",
        owasp=str(normalized.get("owasp") or ""),
        cwe=str(normalized.get("cwe") or ""),
        evidence=str(normalized.get("evidence") or ""),
        exploit_scenario=str(normalized.get("exploit_scenario") or ""),
        references=list(normalized.get("references") or []),
        confidence=float(normalized.get("confidence") or 0.8),
        source="llm",
    )


def _compute_security_score(findings: list) -> int:
    """根据安全问题列表计算安全评分(0-100,越高越安全)

    Args:
        findings: Finding 列表

    Returns:
        int: 安全评分 0-100
    """
    deduct = {"严重": 15, "高": 8, "中": 3, "低": 1}
    total_deduct = 0
    for f in findings:
        sev = getattr(f, "severity", "中")
        total_deduct += deduct.get(sev, 3)
    return max(0, 100 - total_deduct)
