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

import hashlib
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
from app.core.config import settings
from app.core.exceptions import AppError, ConflictError
from app.models.code_file import CodeFile
from app.models.project import Project
from app.models.review_issue import ReviewIssue
from app.models.review_task import ReviewTask
from app.models.user import User
from app.services import project_source_service
from app.services.project_member_service import get_visible_project_ids, require_project_access
from app.utils.encoding_utils import MAX_AUDIT_TEXT_LINES_PER_FILE
from app.utils.source_archive_gate import source_archive_workload

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
    failure_kind: str = ""
    finish_reason: str = ""
    invalid_item_count: int = 0
    invalid_item_kinds: dict[str, int] = field(default_factory=dict)


@dataclass
class _ProjectAuditPart:
    """项目级语义审计中的一段源码及其原文件定位。"""

    file: CodeFile
    text: str
    start_line: int


@dataclass
class _AdaptiveAuditResult:
    """语义批次自适应拆分后的终端叶片及请求统计。"""

    leaves: List[Tuple[List[_ProjectAuditPart], _AuditChunkResult]] = field(default_factory=list)
    request_count: int = 0
    split_count: int = 0


@dataclass
class _SemanticAuditBudget:
    """一次项目语义审计共享的请求数与墙钟时间预算。"""

    max_requests: int
    deadline: float
    request_count: int = 0
    exhausted_reason: str = ""

    def reserve(self) -> bool:
        if self.request_count >= self.max_requests:
            self.exhausted_reason = f"语义审计超过最多 {self.max_requests} 次模型请求"
            return False
        if time.monotonic() >= self.deadline:
            self.exhausted_reason = "语义审计超过全局执行时限"
            return False
        self.request_count += 1
        return True


@dataclass
class _BoundedGraphResult:
    """图谱分析的有界返回样本与完整有效总量。"""

    items: List[dict] = field(default_factory=list)
    total_count: int = 0
    unique_link_count: int = 0


_PROJECT_PART_CHARS = 24_000
_MAX_RETAINED_FINDINGS = 2_000
_MAX_RETAINED_GRAPH_ITEMS = 2_000
_MAX_AUDIT_RESULT_JSON_BYTES = 16 * 1024 * 1024
_INVALID_CONTRACT_FAILURE_KINDS = frozenset({
    "invalid_json",
    "invalid_schema",
    "invalid_item",
})
_RECOVERABLE_BATCH_FAILURE_KINDS = frozenset({
    "output_truncated",
    "output_limited",
    *_INVALID_CONTRACT_FAILURE_KINDS,
})
_PROJECT_RISK_RE = re.compile(
    r"(?i)(exec\s*\(|system\s*\(|shell_exec\s*\(|eval\s*\(|unserialize\s*\(|"
    r"select\s+.+\s+from|insert\s+into|update\s+.+\s+set|delete\s+from|"
    r"requests?\.|curl_|file_get_contents\s*\(|\$_(?:GET|POST|REQUEST|COOKIE|FILES)|"
    r"password|token|secret|authorize|permission|route|controller|handler)"
)


class SecuritySentinelAgent(BaseAgent):
    """安全哨兵 Agent"""

    name = "security_sentinel"
    description = "安全哨兵:全方位安全审计(OWASP Top10/硬编码密钥/威胁建模),并派出侦察员摸底"
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
            max_tokens=settings.security_semantic_max_output_tokens,
        )
        self._db: Optional[Session] = None
        self._user: Optional[User] = None
        # v3.3: 是否在项目审计末尾执行「去重 + 对抗复检」。生产默认开启;
        # 单测可置 False 以保留未经复检的原始 findings 计数做边界断言。
        self._verify_enabled: bool = True

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
        if self._user is None or self._user.role in {"admin", "super_admin"}:
            return None
        if project.user_id == self._user.id:
            return None
        try:
            require_project_access(self._db, project.id, self._user, need_write=False)
        except AppError:
            return AgentResult(success=False, error="无权访问该项目")
        return None

    def _authz_task(self, task: ReviewTask) -> Optional[AgentResult]:
        if self._user is None or self._user.role in {"admin", "super_admin"}:
            return None
        if task.user_id == self._user.id:
            return None
        try:
            require_project_access(self._db, task.project_id, self._user, need_write=False)
        except AppError:
            return AgentResult(success=False, error="无权访问该任务")
        return None

    def _authz_file(self, file: CodeFile) -> Optional[AgentResult]:
        if self._user is None or self._user.role in {"admin", "super_admin"}:
            return None
        try:
            require_project_access(self._db, file.project_id, self._user, need_write=False)
        except AppError:
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
        """执行项目级白盒审计，并确保异常不会遗留 running 状态。"""
        started_at = time.time()
        audit_state: dict[str, str] = {}
        try:
            with source_archive_workload():
                return self._scan_project_impl(
                    project_id,
                    top_n=top_n,
                    trace_dataflow=trace_dataflow,
                    ctx=ctx,
                    scan_mode=scan_mode,
                    audit_state=audit_state,
                )
        except ConflictError as exc:
            return AgentResult(
                success=False,
                error=str(exc),
                model=self._model,
                duration_ms=int((time.time() - started_at) * 1000),
            )
        except Exception:
            logger.exception("[security_sentinel] 项目 #%s 白盒审计异常", project_id)
            audit_run_id = audit_state.get("audit_run_id", "")
            if self._db is not None and audit_run_id:
                try:
                    self._db.rollback()
                    project_source_service.finish_source_archive_audit(
                        self._db,
                        project_id,
                        "failed",
                        {
                            "error": "项目白盒审计执行失败",
                            "duration_ms": int((time.time() - started_at) * 1000),
                        },
                        audit_run_id=audit_run_id,
                    )
                except Exception:
                    self._db.rollback()
                    logger.exception("[security_sentinel] 写入隔离归档失败状态异常")
            return AgentResult(
                success=False,
                error="项目白盒审计执行失败",
                model=self._model,
                duration_ms=int((time.time() - started_at) * 1000),
            )

    def _scan_project_impl(self, project_id: int, top_n: int = 50,
                           trace_dataflow: bool = True,
                           ctx: Optional[AgentContext] = None,
                           scan_mode: str = "full",
                           audit_state: Optional[dict[str, str]] = None) -> AgentResult:
        """一次性形成项目源码白盒审计结果。

        full 覆盖全部静态与语义内容；static_full 对全包做静态审计并对
        风险文件做有界语义分析；triage 只扫风险优先子集。
        """
        if (err := self._ensure_db()) is not None:
            return err
        project = self._db.get(Project, project_id)
        if project is None or project.status == "deleted":
            return AgentResult(success=False, error="项目不存在或已删除")
        if (err := self._authz_project(project)) is not None:
            return err

        if scan_mode not in {"full", "static_full", "triage"}:
            return AgentResult(
                success=False,
                error="不支持的 scan_mode,只能是 full、static_full 或 triage",
            )

        t0 = time.time()
        audit_run_id = project_source_service.begin_source_archive_audit(
            self._db,
            project_id,
        )
        archive_audit_active = bool(audit_run_id)
        if audit_run_id and audit_state is not None:
            audit_state["audit_run_id"] = audit_run_id
        files = project_source_service.load_project_source_files(
            self._db,
            self._user,
            project_id,
        )
        if not files:
            if archive_audit_active:
                project_source_service.finish_source_archive_audit(
                    self._db,
                    project_id,
                    "failed",
                    {"error": "项目下没有可扫描的代码文件"},
                    audit_run_id=audit_run_id,
                )
            return AgentResult(success=False, error="项目下没有可扫描的代码文件")
        source_archive, source_archive_filename = project_source_service.build_source_archive(
            self._db,
            self._user,
            project_id,
        )
        source_archive_sha256 = hashlib.sha256(source_archive).hexdigest()
        top_limit = max(1, min(200, int(top_n)))
        all_files_sorted = sorted(
            files,
            key=lambda item: ((item.file_path or item.file_name or "").lower(), item.id),
        )
        archive_text_files = [
            file for file in all_files_sorted
            if not bool(file.is_binary) and bool((file.content or "").strip())
        ]
        archive_text_source_chars = sum(
            len(file.content or "") for file in archive_text_files
        )
        prioritized = self._prioritize_files(archive_text_files)[:top_limit]
        static_files = prioritized if scan_mode == "triage" else all_files_sorted
        semantic_files = archive_text_files if scan_mode == "full" else prioritized
        total_text_chars = sum(len(file.content or "") for file in semantic_files)
        self._emit(
            AgentEventType.DISPATCH, ctx,
            message=f"项目 #{project_id} 开始白盒审计 {len(static_files)}/{len(files)} 个文件",
            payload={
                "scope": "project",
                "project_id": project_id,
                "file_count": len(static_files),
                "total_file_count": len(files),
                "semantic_candidate_file_count": len(semantic_files),
                "semantic_candidate_source_chars": total_text_chars,
                "scan_mode": scan_mode,
                "trace_dataflow": trace_dataflow,
                "source_archive_sha256": source_archive_sha256,
                "source_archive_bytes": len(source_archive),
            },
        )

        all_findings: List[dict] = []
        all_entries: List[dict] = []
        all_sinks: List[dict] = []
        all_endpoints: List[dict] = []
        finding_total_count = 0
        finding_severity_counts = {"严重": 0, "高": 0, "中": 0, "低": 0}
        finding_owasp_hits: set[str] = set()
        entry_total_count = 0
        sink_total_count = 0
        endpoint_total_count = 0

        def record_findings(items: List[dict]) -> None:
            nonlocal finding_total_count
            for finding in items:
                finding_total_count += 1
                severity = finding.get("severity") or "中"
                if severity in finding_severity_counts:
                    finding_severity_counts[severity] += 1
                owasp = str(finding.get("owasp") or "")
                if owasp.startswith("A") and ":" in owasp:
                    finding_owasp_hits.add(owasp.split(":", 1)[0])
                if len(all_findings) < _MAX_RETAINED_FINDINGS:
                    all_findings.append(finding)

        def record_entries(items: List[dict]) -> None:
            nonlocal entry_total_count
            entry_total_count += len(items)
            remaining = _MAX_RETAINED_GRAPH_ITEMS - len(all_entries)
            if remaining > 0:
                all_entries.extend(items[:remaining])

        def record_sinks(items: List[dict]) -> None:
            nonlocal sink_total_count
            sink_total_count += len(items)
            remaining = _MAX_RETAINED_GRAPH_ITEMS - len(all_sinks)
            if remaining > 0:
                all_sinks.extend(items[:remaining])

        def record_endpoints(items: List[dict]) -> None:
            nonlocal endpoint_total_count
            endpoint_total_count += len(items)
            remaining = _MAX_RETAINED_GRAPH_ITEMS - len(all_endpoints)
            if remaining > 0:
                all_endpoints.extend(items[:remaining])

        for idx, file in enumerate(static_files):
            file_path = file.file_path or file.file_name
            endpoints = self._extract_api_endpoints(file)
            record_endpoints(endpoints)
            endpoint_entries: List[dict] = []
            for endpoint in endpoints:
                endpoint_entries.append({
                    "file": file_path,
                    "function": endpoint.get("handler") or endpoint.get("path") or "",
                    "line": endpoint.get("line_number") or 0,
                    "risk": f"{endpoint.get('method', '')} {endpoint.get('path', '')}".strip(),
                    "input_source": "HTTP API",
                })
            record_entries(endpoint_entries)
            # 正则
            record_findings(self._regex_findings(file))
            # 静态语义规则 (v2.1.1)
            record_findings(self._static_findings(file))

            if (idx + 1) % 25 == 0 or idx + 1 == len(static_files):
                if archive_audit_active:
                    project_source_service.touch_source_archive_audit(
                        self._db,
                        project_id,
                        audit_run_id,
                    )
                self._emit(
                    AgentEventType.PROGRESS, ctx,
                    message=f"静态分析 {idx + 1}/{len(static_files)}",
                    payload={
                        "phase": "static_analysis",
                        "index": idx + 1,
                        "total": len(static_files),
                        "findings_count": len(all_findings),
                    },
                )

        # 项目源码统一编批。一个模型请求可同时包含多个文件，并要求每条结果
        # 返回原始路径；这仍是一次项目级白盒审计，不会退化成 N 个文件任务。
        semantic_attempted_chars_by_file: dict[int, int] = {}
        semantic_chars_by_file: dict[int, int] = {}
        semantic_failed_chars_by_file: dict[int, int] = {}
        semantic_initial_batch_count = 0
        semantic_batch_count = 0
        semantic_request_count = 0
        semantic_split_count = 0
        semantic_successful_batch_count = 0
        semantic_failed_batch_count = 0
        semantic_output_truncated_leaf_count = 0
        semantic_output_limited_leaf_count = 0
        semantic_budget_exhausted_leaf_count = 0
        semantic_invalid_contract_leaf_count = 0
        semantic_invalid_item_count = 0
        semantic_invalid_item_kinds: dict[str, int] = {}
        semantic_budget = _SemanticAuditBudget(
            max_requests=settings.security_semantic_max_requests,
            deadline=time.monotonic() + settings.security_semantic_timeout_seconds,
        )
        audit_batches = self._project_audit_batches(
            semantic_files,
            full_content=scan_mode == "full",
        )
        for idx, batch in enumerate(audit_batches, start=1):
            semantic_initial_batch_count = idx
            for part in batch:
                semantic_attempted_chars_by_file[part.file.id] = (
                    semantic_attempted_chars_by_file.get(part.file.id, 0) + len(part.text)
                )
            adaptive = self._audit_project_batch_resilient(
                batch,
                ctx=ctx,
                budget=semantic_budget,
            )
            semantic_request_count += adaptive.request_count
            semantic_split_count += adaptive.split_count
            for leaf_parts, chunk_result in adaptive.leaves:
                semantic_batch_count += 1
                semantic_invalid_item_count += chunk_result.invalid_item_count
                for kind, count in chunk_result.invalid_item_kinds.items():
                    semantic_invalid_item_kinds[kind] = (
                        semantic_invalid_item_kinds.get(kind, 0) + count
                    )
                if chunk_result.success:
                    semantic_successful_batch_count += 1
                    for part in leaf_parts:
                        semantic_chars_by_file[part.file.id] = (
                            semantic_chars_by_file.get(part.file.id, 0) + len(part.text)
                        )
                    record_findings(chunk_result.findings)
                    record_entries(chunk_result.entry_points)
                    record_sinks(chunk_result.dangerous_sinks)
                else:
                    semantic_failed_batch_count += 1
                    for part in leaf_parts:
                        semantic_failed_chars_by_file[part.file.id] = (
                            semantic_failed_chars_by_file.get(part.file.id, 0) + len(part.text)
                        )
                    if chunk_result.failure_kind == "output_truncated":
                        semantic_output_truncated_leaf_count += 1
                    elif chunk_result.failure_kind == "output_limited":
                        semantic_output_limited_leaf_count += 1
                    elif chunk_result.failure_kind == "semantic_budget_exhausted":
                        semantic_budget_exhausted_leaf_count += 1
                    elif chunk_result.failure_kind in _INVALID_CONTRACT_FAILURE_KINDS:
                        semantic_invalid_contract_leaf_count += 1

            if archive_audit_active:
                project_source_service.touch_source_archive_audit(
                    self._db,
                    project_id,
                    audit_run_id,
                )

            self._emit(
                AgentEventType.PROGRESS, ctx,
                message=f"项目源码语义审计批次 {idx}",
                payload={
                    "phase": "semantic_analysis",
                    "index": idx,
                    "batch_file_count": len({part.file.id for part in batch}),
                    "batch_source_chars": sum(len(part.text) for part in batch),
                    "terminal_leaf_count": len(adaptive.leaves),
                    "request_count": adaptive.request_count,
                    "split_count": adaptive.split_count,
                    "batch_success": all(result.success for _, result in adaptive.leaves),
                    "findings_count": len(all_findings),
                },
            )

        semantic_attempted_source_chars = sum(semantic_attempted_chars_by_file.values())
        semantic_planned_file_count = len(semantic_attempted_chars_by_file)
        semantic_planned_source_chars = semantic_attempted_source_chars
        semantic_source_chars = sum(semantic_chars_by_file.values())
        semantic_failed_source_chars = sum(semantic_failed_chars_by_file.values())
        semantic_accounted_source_chars = semantic_source_chars + semantic_failed_source_chars
        semantic_accounting_complete = (
            semantic_accounted_source_chars == semantic_attempted_source_chars
        )
        semantic_request_accounting_complete = (
            semantic_request_count == semantic_budget.request_count
        )
        semantic_truncated_files = sum(
            1 for file in semantic_files
            if semantic_chars_by_file.get(file.id, 0) < len(file.content or "")
        )
        semantic_unscheduled_file_count = sum(
            1 for file in semantic_files
            if semantic_attempted_chars_by_file.get(file.id, 0) == 0
        )
        semantic_partially_scheduled_file_count = sum(
            1 for file in semantic_files
            if 0 < semantic_attempted_chars_by_file.get(file.id, 0) < len(file.content or "")
        )
        semantic_fully_scheduled_file_count = sum(
            1 for file in semantic_files
            if semantic_attempted_chars_by_file.get(file.id, 0) == len(file.content or "")
        )
        semantic_verified_file_count = sum(
            1 for file in semantic_files
            if semantic_chars_by_file.get(file.id, 0) == len(file.content or "")
            and semantic_failed_chars_by_file.get(file.id, 0) == 0
        )
        semantic_failed_file_count = sum(
            1 for file in semantic_files
            if semantic_failed_chars_by_file.get(file.id, 0) > 0
        )
        semantic_complete = (
            semantic_failed_batch_count == 0
            and semantic_source_chars == total_text_chars
            and len(semantic_chars_by_file) == len(semantic_files)
        )
        semantic_execution_complete = (
            semantic_failed_batch_count == 0
            and semantic_source_chars == semantic_attempted_source_chars
            and semantic_accounting_complete
            and semantic_request_accounting_complete
        )
        archive_semantic_complete = (
            semantic_execution_complete
            and semantic_source_chars == archive_text_source_chars
            and len(semantic_chars_by_file) == len(archive_text_files)
        )

        # 数据流分析(第二轮 LLM)
        threat_model: dict = {
            "entry_points": all_entries[:30],
            "data_flows": [],
            "api_endpoints": all_endpoints[:100],
            "code_links": [],
            "attack_surface_summary": (
                f"扫描接口 {endpoint_total_count} 个,入口 {entry_total_count} 处,"
                f"危险接收点 {sink_total_count} 处。"
            ),
        }
        dataflow_requested = bool(trace_dataflow)
        dataflow_attempted = False
        dataflow_complete = True
        dataflow_failure_kind = ""
        dataflow_request_count = 0
        data_flow_total_count = 0
        data_flow_link_total_count = 0
        if trace_dataflow and semantic_execution_complete and all_entries and all_sinks:
            dataflow_attempted = True
            self._emit(
                AgentEventType.PROGRESS, ctx,
                message="开始跨文件数据流分析",
                payload={"phase": "dataflow_analysis"},
            )
            request_count_before_dataflow = semantic_budget.request_count
            dataflow_result = self._llm_dataflow_analysis(
                all_entries, all_sinks, project_name=project.project_name, ctx=ctx,
                api_endpoints=all_endpoints,
                budget=semantic_budget,
            )
            dataflow_request_count = (
                semantic_budget.request_count - request_count_before_dataflow
            )
            if dataflow_result is None:
                dataflow_complete = False
                dataflow_failure_kind = (
                    "semantic_budget_exhausted"
                    if semantic_budget.exhausted_reason
                    else "dataflow_analysis_failed"
                )
            else:
                data_flows = dataflow_result.items
                data_flow_total_count = dataflow_result.total_count
                data_flow_link_total_count = dataflow_result.unique_link_count
                threat_model["data_flows"] = data_flows
                # 升级出现在数据流上的 finding 严重度
                severities_before = [finding.get("severity") or "中" for finding in all_findings]
                self._upgrade_findings_on_dataflow(all_findings, data_flows)
                for previous, finding in zip(severities_before, all_findings):
                    current = finding.get("severity") or "中"
                    if previous != current:
                        if previous in finding_severity_counts:
                            finding_severity_counts[previous] -= 1
                        if current in finding_severity_counts:
                            finding_severity_counts[current] += 1
        audit_request_count = semantic_request_count + dataflow_request_count
        audit_request_accounting_complete = (
            audit_request_count == semantic_budget.request_count
        )
        code_link_result = self._build_code_links(
            all_endpoints,
            all_sinks,
            threat_model["data_flows"],
            data_flow_link_total_count=data_flow_link_total_count,
        )
        threat_model["code_links"] = code_link_result.items
        code_link_total_count = code_link_result.total_count
        returned_entry_count = len(threat_model["entry_points"])
        returned_flow_count = len(threat_model["data_flows"])
        returned_endpoint_count = len(threat_model["api_endpoints"])
        returned_code_link_count = len(threat_model["code_links"])

        duration_ms = int((time.time() - t0) * 1000)
        deduct = sum(
            _SEVERITY_DEDUCT[severity] * count
            for severity, count in finding_severity_counts.items()
        )
        risk_score = max(0, min(100, 100 - deduct))

        # ── v3.3 全链路: 去重 + 对抗复检(解决「假警报多 / 真假难辨」) ──
        verification: dict = {"confirmed": 0, "refuted": 0, "reviewed": 0}
        if self._verify_enabled:
            try:
                all_findings = self._dedup_findings(all_findings)
                verification = self._adversarial_verify(all_findings, ctx=ctx)
                self._emit(
                    AgentEventType.PROGRESS, ctx,
                    message=(
                        f"对抗复检: 确认 {verification['confirmed']} 条 / "
                        f"证伪 {verification['refuted']} 条"
                    ),
                    payload={"phase": "adversarial_verify", **verification},
                )
            except Exception:
                logger.exception("[security_sentinel] 对抗复检异常,跳过")

        sev_counts = dict(finding_severity_counts)
        compliance = self._compute_compliance(all_findings)
        compliance.update({
            "verification": verification,
            "owasp_coverage": sorted(finding_owasp_hits),
            "gb_t_22239": (
                f"等保 2.0 应用安全相关命中风险 {len(finding_owasp_hits)} 类"
                if finding_owasp_hits else "未触及等保 2.0 应用安全条款"
            ),
            "scan_mode": scan_mode,
            "total_file_count": len(files),
            "scanned_file_count": len(static_files),
            "skipped_file_count": len(files) - len(static_files),
            "coverage_ratio": round(len(static_files) / len(files), 4) if files else 0,
            "truncated": bool(len(static_files) < len(files)),
            "requested_top_n": int(top_n),
            "static_scanned_file_count": len(static_files),
            "static_complete": len(static_files) == len(files),
            "semantic_candidate_file_count": len(semantic_files),
            "semantic_candidate_source_chars": total_text_chars,
            "semantic_selection_strategy": (
                "all_source"
                if scan_mode == "full"
                else "path_keyword_pool_then_content_risk_prefix"
            ),
            "semantic_planned_file_count": semantic_planned_file_count,
            "semantic_planned_source_chars": semantic_planned_source_chars,
            "semantic_file_count": len(semantic_chars_by_file),
            "archive_text_file_count": len(archive_text_files),
            "archive_text_source_chars": archive_text_source_chars,
            "binary_or_empty_file_count": len(files) - len(archive_text_files),
            "archive_binary_or_empty_file_count": len(files) - len(archive_text_files),
            "candidate_binary_or_empty_file_count": 0,
            "semantic_initial_batch_count": semantic_initial_batch_count,
            "semantic_batch_count": semantic_batch_count,
            "semantic_request_count": semantic_request_count,
            "semantic_split_count": semantic_split_count,
            "semantic_successful_batch_count": semantic_successful_batch_count,
            "semantic_failed_batch_count": semantic_failed_batch_count,
            "semantic_output_truncated_leaf_count": semantic_output_truncated_leaf_count,
            "semantic_output_limited_leaf_count": semantic_output_limited_leaf_count,
            "semantic_budget_exhausted_leaf_count": semantic_budget_exhausted_leaf_count,
            "semantic_invalid_contract_leaf_count": semantic_invalid_contract_leaf_count,
            "semantic_invalid_item_count": semantic_invalid_item_count,
            "semantic_invalid_item_kinds": semantic_invalid_item_kinds,
            "semantic_attempted_source_chars": semantic_attempted_source_chars,
            "semantic_source_chars": semantic_source_chars,
            "semantic_failed_source_chars": semantic_failed_source_chars,
            "semantic_accounted_source_chars": semantic_accounted_source_chars,
            "total_text_source_chars": total_text_chars,
            "semantic_char_coverage_ratio": (
                round(semantic_source_chars / total_text_chars, 4)
                if total_text_chars else 0
            ),
            "semantic_scheduled_coverage_ratio": (
                round(semantic_planned_source_chars / total_text_chars, 4)
                if total_text_chars else 0
            ),
            "semantic_archive_coverage_ratio": (
                round(semantic_source_chars / archive_text_source_chars, 4)
                if archive_text_source_chars else 0
            ),
            "semantic_truncated_file_count": semantic_truncated_files,
            "semantic_unscheduled_file_count": semantic_unscheduled_file_count,
            "semantic_partially_scheduled_file_count": semantic_partially_scheduled_file_count,
            "semantic_fully_scheduled_file_count": semantic_fully_scheduled_file_count,
            "semantic_verified_file_count": semantic_verified_file_count,
            "semantic_failed_file_count": semantic_failed_file_count,
            "semantic_complete": semantic_complete,
            "semantic_candidate_complete": semantic_complete,
            "semantic_execution_complete": semantic_execution_complete,
            "semantic_scope_execution_complete": semantic_execution_complete,
            "archive_semantic_complete": archive_semantic_complete,
            "semantic_accounting_complete": semantic_accounting_complete,
            "semantic_request_accounting_complete": semantic_request_accounting_complete,
            "audit_request_count": audit_request_count,
            "semantic_request_headroom": max(
                0,
                settings.security_semantic_max_requests - semantic_budget.request_count,
            ),
            "dataflow_request_count": dataflow_request_count,
            "dataflow_scope": (
                "all_semantic_source"
                if scan_mode == "full"
                else "bounded_semantic_results_and_static_endpoints"
            ),
            "audit_request_accounting_complete": audit_request_accounting_complete,
            "semantic_request_budget": settings.security_semantic_max_requests,
            "semantic_timeout_seconds": settings.security_semantic_timeout_seconds,
            "semantic_bounded_total_chars": (
                None
                if scan_mode == "full"
                else settings.security_semantic_bounded_total_chars
            ),
            "semantic_bounded_per_file_chars": (
                None
                if scan_mode == "full"
                else settings.security_semantic_bounded_per_file_chars
            ),
            "semantic_bounded_max_files": (
                None
                if scan_mode == "full"
                else settings.security_semantic_bounded_max_files
            ),
            "dataflow_requested": dataflow_requested,
            "dataflow_attempted": dataflow_attempted,
            "dataflow_complete": dataflow_complete,
            "finding_total_count": finding_total_count,
            "retained_finding_count": len(all_findings),
            "findings_truncated": finding_total_count > len(all_findings),
            "finding_severity_counts": sev_counts,
            "entry_point_total_count": entry_total_count,
            "retained_entry_point_count": len(all_entries),
            "dangerous_sink_total_count": sink_total_count,
            "retained_dangerous_sink_count": len(all_sinks),
            "api_endpoint_total_count": endpoint_total_count,
            "retained_api_endpoint_count": len(all_endpoints),
            "data_flow_total_count": data_flow_total_count,
            "retained_data_flow_count": len(threat_model["data_flows"]),
            "code_link_total_count": code_link_total_count,
            "retained_code_link_count": len(threat_model["code_links"]),
            "returned_entry_point_count": returned_entry_count,
            "returned_data_flow_count": returned_flow_count,
            "returned_api_endpoint_count": returned_endpoint_count,
            "returned_code_link_count": returned_code_link_count,
            "response_graph_truncated": any((
                entry_total_count > returned_entry_count,
                data_flow_total_count > returned_flow_count,
                endpoint_total_count > returned_endpoint_count,
                code_link_total_count > returned_code_link_count,
            )),
            "graph_items_truncated": any((
                entry_total_count > len(all_entries),
                sink_total_count > len(all_sinks),
                endpoint_total_count > len(all_endpoints),
                entry_total_count > returned_entry_count,
                data_flow_total_count > returned_flow_count,
                endpoint_total_count > returned_endpoint_count,
                code_link_total_count > returned_code_link_count,
            )),
        })
        summary = (
            f"项目「{project.project_name}」白盒审计静态覆盖 {len(static_files)}/{len(files)} 个文件,"
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
            "file_count": len(static_files),
            "duration_ms": duration_ms,
            "source_archive_sha256": source_archive_sha256,
            "source_archive_bytes": len(source_archive),
            "source_archive_filename": source_archive_filename,
        }
        result_json_bytes = len(
            json_lib.dumps(result_data, ensure_ascii=False, default=str).encode("utf-8")
        )
        while result_json_bytes > _MAX_AUDIT_RESULT_JSON_BYTES and result_data["findings"]:
            result_data["findings"] = result_data["findings"][: len(result_data["findings"]) // 2]
            compliance["retained_finding_count"] = len(result_data["findings"])
            compliance["findings_truncated"] = True
            compliance["result_payload_truncated"] = True
            result_json_bytes = len(
                json_lib.dumps(result_data, ensure_ascii=False, default=str).encode("utf-8")
            )
        if result_json_bytes > _MAX_AUDIT_RESULT_JSON_BYTES:
            raise RuntimeError("白盒审计结果超过 16MiB 持久化上限")
        compliance["result_json_bytes"] = result_json_bytes
        result_json_bytes = len(
            json_lib.dumps(result_data, ensure_ascii=False, default=str).encode("utf-8")
        )
        if result_json_bytes > _MAX_AUDIT_RESULT_JSON_BYTES:
            raise RuntimeError("白盒审计结果超过 16MiB 持久化上限")
        compliance["result_json_bytes"] = result_json_bytes
        if not semantic_execution_complete or (scan_mode == "full" and not semantic_complete):
            semantic_failure_kind = (
                "output_truncated" if semantic_output_truncated_leaf_count else
                "output_limited" if semantic_output_limited_leaf_count else
                "semantic_budget_exhausted" if semantic_budget_exhausted_leaf_count else
                "invalid_item" if semantic_invalid_item_count else
                "invalid_contract" if semantic_invalid_contract_leaf_count else
                "semantic_accounting_failed" if not semantic_accounting_complete else
                "semantic_analysis_failed"
            )
            error = (
                "项目语义审计执行未完成: "
                f"成功覆盖 {semantic_source_chars}/{semantic_attempted_source_chars} 个已调度字符,"
                f"失败叶片 {semantic_failed_batch_count},"
                f"终端截断 {semantic_output_truncated_leaf_count},"
                f"结果受限 {semantic_output_limited_leaf_count},"
                f"预算耗尽 {semantic_budget_exhausted_leaf_count},"
                f"契约无效 {semantic_invalid_contract_leaf_count}"
            )
            self._emit(
                AgentEventType.FAILED, ctx,
                message=error,
                payload={
                    "phase": "semantic_analysis",
                    "semantic_source_chars": semantic_source_chars,
                    "semantic_attempted_source_chars": semantic_attempted_source_chars,
                    "total_text_source_chars": total_text_chars,
                    "failed_batch_count": semantic_failed_batch_count,
                    "output_truncated_leaf_count": semantic_output_truncated_leaf_count,
                    "output_limited_leaf_count": semantic_output_limited_leaf_count,
                    "budget_exhausted_leaf_count": semantic_budget_exhausted_leaf_count,
                    "invalid_contract_leaf_count": semantic_invalid_contract_leaf_count,
                    "semantic_failed_source_chars": semantic_failed_source_chars,
                    "semantic_accounting_complete": semantic_accounting_complete,
                    "failure_kind": semantic_failure_kind,
                },
            )
            blocked = any((
                semantic_output_truncated_leaf_count,
                semantic_output_limited_leaf_count,
                semantic_budget_exhausted_leaf_count,
            ))
            project_source_service.finish_source_archive_audit(
                self._db,
                project_id,
                "blocked" if blocked else "failed",
                result_data,
                audit_run_id=audit_run_id,
            )
            return AgentResult(
                success=False,
                data=result_data,
                error=error,
                model=self._model,
                duration_ms=duration_ms,
                failure_kind=semantic_failure_kind,
            )

        if dataflow_attempted and not dataflow_complete:
            error = (
                "跨文件数据流分析未完成:"
                "模型请求预算或返回结构不满足可信审计契约"
            )
            self._emit(
                AgentEventType.FAILED,
                ctx,
                message=error,
                payload={
                    "phase": "dataflow_analysis",
                    "failure_kind": dataflow_failure_kind or "dataflow_analysis_failed",
                },
            )
            project_source_service.finish_source_archive_audit(
                self._db,
                project_id,
                "blocked" if dataflow_failure_kind == "semantic_budget_exhausted" else "failed",
                result_data,
                audit_run_id=audit_run_id,
            )
            return AgentResult(
                success=False,
                data=result_data,
                error=error,
                model=self._model,
                duration_ms=duration_ms,
                failure_kind=dataflow_failure_kind or "dataflow_analysis_failed",
            )

        if not audit_request_accounting_complete:
            error = "项目审计模型请求账目不一致,拒绝生成可信结论"
            self._emit(
                AgentEventType.FAILED,
                ctx,
                message=error,
                payload={
                    "phase": "audit_budget",
                    "audit_request_count": audit_request_count,
                    "budget_request_count": semantic_budget.request_count,
                },
            )
            project_source_service.finish_source_archive_audit(
                self._db,
                project_id,
                "blocked",
                result_data,
                audit_run_id=audit_run_id,
            )
            return AgentResult(
                success=False,
                data=result_data,
                error=error,
                model=self._model,
                duration_ms=duration_ms,
                failure_kind="semantic_budget_accounting_failed",
            )

        if archive_audit_active and not project_source_service.finish_source_archive_audit(
            self._db,
            project_id,
            "succeeded",
            result_data,
            audit_run_id=audit_run_id,
        ):
            error = "当前源码审计已被新一代运行接管,结果未持久化"
            self._emit(
                AgentEventType.FAILED,
                ctx,
                message=error,
                payload={"phase": "persist_audit_result"},
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
                "findings_total_count": finding_total_count,
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
        if self._user and self._user.role not in {"admin", "super_admin"}:
            visible_project_ids, _scope = get_visible_project_ids(self._db, self._user)
            q = q.filter(Project.id.in_(visible_project_ids))
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
        finding_total_count = 0
        finding_severity_counts = {"严重": 0, "高": 0, "中": 0, "低": 0}
        owasp_hits: set[str] = set()
        entry_total_count = 0
        flow_total_count = 0
        endpoint_total_count = 0
        code_link_total_count = 0

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
                if len(project_errors) < 500:
                    project_errors.append({
                        "project_id": project.id,
                        "project_name": project.project_name,
                        "error": result.error or "扫描失败",
                    })
                continue

            data = result.data or {}
            scanned_projects += 1
            total_files += int(data.get("file_count") or 0)
            project_compliance = data.get("compliance") or {}
            project_findings = [
                raw for raw in (data.get("findings") or []) if isinstance(raw, dict)
            ]
            project_finding_total = int(
                project_compliance.get("finding_total_count") or len(project_findings)
            )
            finding_total_count += project_finding_total
            project_severity_counts = project_compliance.get("finding_severity_counts") or self._severity_counts(
                project_findings
            )
            for severity in finding_severity_counts:
                finding_severity_counts[severity] += int(project_severity_counts.get(severity) or 0)
            owasp_hits.update(project_compliance.get("owasp_coverage") or [])

            for raw in project_findings:
                if len(all_findings) >= _MAX_RETAINED_FINDINGS:
                    break
                finding = dict(raw)
                finding["file_path"] = project_path(project, str(finding.get("file_path") or ""))
                all_findings.append(finding)

            threat_model = data.get("threat_model") or {}
            entry_total_count += int(
                project_compliance.get("entry_point_total_count")
                or len(threat_model.get("entry_points") or [])
            )
            endpoint_total_count += int(
                project_compliance.get("api_endpoint_total_count")
                or len(threat_model.get("api_endpoints") or [])
            )
            flow_total_count += int(
                project_compliance.get("data_flow_total_count")
                or len(threat_model.get("data_flows") or [])
            )
            code_link_total_count += int(
                project_compliance.get("code_link_total_count")
                or len(threat_model.get("code_links") or [])
            )
            for raw_entry in threat_model.get("entry_points") or []:
                if not isinstance(raw_entry, dict):
                    continue
                if len(all_entries) >= _MAX_RETAINED_GRAPH_ITEMS:
                    break
                entry = dict(raw_entry)
                entry["file"] = project_path(project, str(entry.get("file") or ""))
                all_entries.append(entry)

            for raw_flow in threat_model.get("data_flows") or []:
                if not isinstance(raw_flow, dict):
                    continue
                if len(all_flows) >= _MAX_RETAINED_GRAPH_ITEMS:
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
                if len(all_endpoints) >= _MAX_RETAINED_GRAPH_ITEMS:
                    break
                endpoint = dict(raw_endpoint)
                endpoint["file_path"] = project_path(
                    project, str(endpoint.get("file_path") or ""),
                )
                all_endpoints.append(endpoint)

            for raw_link in threat_model.get("code_links") or []:
                if not isinstance(raw_link, dict):
                    continue
                if len(all_code_links) >= _MAX_RETAINED_GRAPH_ITEMS:
                    continue
                link = dict(raw_link)
                if link.get("from"):
                    link["from"] = project_path(project, str(link.get("from")))
                if link.get("to"):
                    link["to"] = project_path(project, str(link.get("to")))
                all_code_links.append(link)

        duration_ms = int((time.time() - t0) * 1000)
        deduct = sum(
            _SEVERITY_DEDUCT[severity] * count
            for severity, count in finding_severity_counts.items()
        )
        risk_score = max(0, min(100, 100 - deduct))
        sev_counts = dict(finding_severity_counts)
        skipped_projects = len(projects) - scanned_projects
        compliance = self._compute_compliance(all_findings)
        compliance.update({
            "owasp_coverage": sorted(owasp_hits),
            "finding_total_count": finding_total_count,
            "retained_finding_count": len(all_findings),
            "findings_truncated": finding_total_count > len(all_findings),
            "finding_severity_counts": sev_counts,
            "entry_point_total_count": entry_total_count,
            "retained_entry_point_count": len(all_entries),
            "api_endpoint_total_count": endpoint_total_count,
            "retained_api_endpoint_count": len(all_endpoints),
            "data_flow_total_count": flow_total_count,
            "retained_data_flow_count": len(all_flows),
            "code_link_total_count": code_link_total_count,
            "retained_code_link_count": len(all_code_links),
            "graph_items_truncated": any((
                entry_total_count > len(all_entries),
                endpoint_total_count > len(all_endpoints),
                flow_total_count > len(all_flows),
                code_link_total_count > len(all_code_links),
            )),
            "project_count": len(projects),
            "scanned_project_count": scanned_projects,
            "skipped_project_count": skipped_projects,
            "project_errors": project_errors,
        })
        scan_success = not project_errors
        summary = (
            f"全量项目扫描完成:可见项目 {len(projects)} 个,成功扫描 {scanned_projects} 个,"
            f"跳过 {skipped_projects} 个,累计扫描文件 {total_files} 个;"
            f"识别接口 {endpoint_total_count} 个,代码联动关系 {code_link_total_count} 条;"
            f"发现 {sev_counts['严重']} 处严重 / {sev_counts['高']} 处高危 / "
            f"{sev_counts['中']} 处中危 / {sev_counts['低']} 处低危。"
            f"综合风险评分 {risk_score}/100。"
        )
        compliance["scan_complete"] = scan_success
        if not scan_success:
            summary = (
                f"全量项目扫描未完成:可见项目 {len(projects)} 个,成功扫描 {scanned_projects} 个,"
                f"失败 {len(project_errors)} 个;已拒绝生成完整结论。"
            )
        threat_model = {
            "entry_points": all_entries[:100],
            "data_flows": all_flows[:100],
            "api_endpoints": all_endpoints[:200],
            "code_links": all_code_links[:200],
            "attack_surface_summary": (
                f"全量扫描覆盖 {scanned_projects} 个项目,"
                f"识别接口 {endpoint_total_count} 个,入口 {entry_total_count} 处,"
                f"跨文件攻击路径 {flow_total_count} 条,代码联动关系 {code_link_total_count} 条。"
            ),
        }
        returned_entry_count = len(threat_model["entry_points"])
        returned_flow_count = len(threat_model["data_flows"])
        returned_endpoint_count = len(threat_model["api_endpoints"])
        returned_code_link_count = len(threat_model["code_links"])
        compliance.update({
            "returned_entry_point_count": returned_entry_count,
            "returned_data_flow_count": returned_flow_count,
            "returned_api_endpoint_count": returned_endpoint_count,
            "returned_code_link_count": returned_code_link_count,
            "response_graph_truncated": any((
                entry_total_count > returned_entry_count,
                flow_total_count > returned_flow_count,
                endpoint_total_count > returned_endpoint_count,
                code_link_total_count > returned_code_link_count,
            )),
            "graph_items_truncated": any((
                compliance["graph_items_truncated"],
                entry_total_count > returned_entry_count,
                flow_total_count > returned_flow_count,
                endpoint_total_count > returned_endpoint_count,
                code_link_total_count > returned_code_link_count,
            )),
        })
        discussion = self._build_multi_agent_discussion(
            all_findings, threat_model, project_count=scanned_projects,
        )

        self._emit(
            AgentEventType.COMPLETE if scan_success else AgentEventType.FAILED,
            ctx,
            message="全量项目扫描完成" if scan_success else "全量项目扫描存在失败项目",
            payload={
                "scope": "all_projects",
                "project_count": len(projects),
                "scanned_project_count": scanned_projects,
                "findings_count": len(all_findings),
                "findings_total_count": finding_total_count,
                "risk_score": risk_score,
                "duration_ms": duration_ms,
            },
        )

        return AgentResult(
            success=scan_success,
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
            error=(
                f"全量项目扫描有 {len(project_errors)} 个项目失败"
                if not scan_success else None
            ),
            failure_kind="project_scan_failed" if not scan_success else "",
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
        line_count = int(file.line_count or 0)
        if line_count <= 0:
            line_count = content.count("\n") + 1
        if line_count > MAX_AUDIT_TEXT_LINES_PER_FILE:
            raise RuntimeError(
                f"源码文件超过单文件 {MAX_AUDIT_TEXT_LINES_PER_FILE} 行审计资源上限"
            )

        file_path = file.file_path or file.file_name
        lines = content.splitlines()
        endpoints: List[dict] = []
        seen: set[tuple[str, str, str, int]] = set()

        class _EndpointLimitReached(Exception):
            pass

        def add_endpoint(method: str, path: str, line_no: int,
                         source: str, handler: str = "") -> None:
            method_norm = (method or "GET").upper()
            path_norm = (path or "").strip()
            if not path_norm:
                return
            path_norm = path_norm[:2_048]
            if not path_norm.startswith(("/", "http://", "https://")):
                path_norm = f"/{path_norm.lstrip('/')}"
            key = (method_norm, path_norm, file_path, line_no)
            if key in seen:
                return
            seen.add(key)
            endpoints.append({
                "method": method_norm,
                "path": path_norm,
                "file_path": file_path,
                "line_number": line_no,
                "handler": (handler or self._next_handler_name(lines, line_no - 1))[:200],
                "auth_hint": self._endpoint_auth_hint(lines, line_no - 1),
                "source": source,
            })
            if len(endpoints) >= 200:
                raise _EndpointLimitReached

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

        try:
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
        except _EndpointLimitReached:
            pass

        return endpoints

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

    def _build_code_links(
        self,
        api_endpoints: List[dict],
        sinks: List[dict],
        data_flows: List[dict],
        *,
        data_flow_link_total_count: int = 0,
    ) -> _BoundedGraphResult:
        """生成接口、数据流和危险接收点之间的代码联动关系。

        Args:
            api_endpoints: 接口扫描结果。
            sinks: LLM 或规则识别到的危险接收点。
            data_flows: 跨文件数据流推断结果。

        Returns:
            _BoundedGraphResult: 用于前端展示的有界样本及完整总量。
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
            if len(links) < 200:
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
        flow_unique_count = len(seen)

        sinks_by_file: dict[str, dict[str, tuple[str, str]]] = {}
        for sink in sinks or []:
            sink_file = str(sink.get("file") or "")
            if sink_file:
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
                sink_loc = f"{sink_file}:{sink.get('name') or sink_text or 'sink'}"
                sinks_by_file.setdefault(sink_file, {}).setdefault(
                    sink_loc,
                    (risk_type, severity),
                )

        endpoints_by_file: dict[str, dict[str, None]] = {}
        for endpoint in api_endpoints or []:
            endpoint_file = str(endpoint.get("file_path") or "")
            if not endpoint_file:
                continue
            endpoint_loc = (
                f"{endpoint_file}:{endpoint.get('handler') or endpoint.get('path') or ''}"
            )
            endpoints_by_file.setdefault(endpoint_file, {}).setdefault(endpoint_loc, None)

        endpoint_sink_total = 0
        for endpoint_file, endpoint_locations in endpoints_by_file.items():
            sink_locations = sinks_by_file.get(endpoint_file, {})
            endpoint_sink_total += len(endpoint_locations) * len(sink_locations)
            if len(links) >= 200:
                continue
            for endpoint_loc in endpoint_locations:
                for sink_loc, (risk_type, severity) in sink_locations.items():
                    if len(links) >= 200:
                        break
                    add_link(
                        endpoint_loc,
                        sink_loc,
                        "接口到同文件危险接收点",
                        risk_type,
                        severity,
                    )

        return _BoundedGraphResult(
            items=links,
            total_count=(
                max(flow_unique_count, int(data_flow_link_total_count or 0))
                + endpoint_sink_total
            ),
        )

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

        full 模式逐字符覆盖所有可解析源码；static_full / triage 使用
        可在共享模型请求预算内闭合的多文件风险窗口。
        返回迭代器以便调用方每次只保留一个批次，避免大型项目内存翻倍。
        """
        if not full_content:
            ordered = sorted(
                files,
                key=lambda file: (
                    -self._semantic_risk_score(file),
                    (file.file_path or file.file_name or "").lower(),
                    file.id,
                ),
            )
            remaining = settings.security_semantic_bounded_total_chars
            current: List[_ProjectAuditPart] = []
            current_chars = 0
            selected_file_count = 0
            batch_full = False
            for file in ordered:
                if remaining <= 0 or selected_file_count >= settings.security_semantic_bounded_max_files:
                    break
                content_size = len(file.content or "")
                if content_size <= 0:
                    continue
                file_budget = min(
                    content_size,
                    settings.security_semantic_bounded_per_file_chars,
                    remaining,
                )
                consumed = 0
                for part in self._semantic_parts_for_file(file, file_budget):
                    path = part.file.file_path or part.file.file_name
                    estimated = len(part.text) + len(path) + 120
                    if current_chars + estimated > settings.security_semantic_batch_chars:
                        batch_full = True
                        break
                    current.append(part)
                    current_chars += estimated
                    consumed += len(part.text)
                if consumed:
                    selected_file_count += 1
                    remaining -= consumed
                if batch_full:
                    break
            if current:
                yield current
            return

        ordered = sorted(
            files,
            key=lambda file: (
                (file.file_path or file.file_name or "").lower(),
                file.id,
            ),
        )
        current: List[_ProjectAuditPart] = []
        current_chars = 0
        for file in ordered:
            content_size = len(file.content or "")
            if content_size <= 0:
                continue
            for part in self._semantic_parts_for_file(file, content_size):
                path = part.file.file_path or part.file.file_name
                estimated = len(part.text) + len(path) + 120
                if current and current_chars + estimated > settings.security_semantic_batch_chars:
                    yield current
                    current = []
                    current_chars = 0
                current.append(part)
                current_chars += estimated
        if current:
            yield current

    @staticmethod
    def _split_project_audit_parts(
        parts: List[_ProjectAuditPart],
    ) -> Optional[Tuple[List[_ProjectAuditPart], List[_ProjectAuditPart]]]:
        """按字符量将语义批次二分，并保持原文与绝对行号连续。"""
        total_chars = sum(len(part.text) for part in parts)
        if total_chars < 2:
            return None
        target = total_chars // 2
        consumed = 0
        left: List[_ProjectAuditPart] = []
        right: List[_ProjectAuditPart] = []
        for part in parts:
            part_size = len(part.text)
            if consumed >= target:
                right.append(part)
            elif consumed + part_size <= target:
                left.append(part)
            else:
                approximate = target - consumed
                candidates = []
                before = part.text.rfind("\n", max(0, approximate // 2), approximate)
                after = part.text.find(
                    "\n",
                    approximate,
                    min(part_size, approximate + max(2, approximate // 2)),
                )
                if before >= 0:
                    candidates.append(before + 1)
                if after >= 0:
                    candidates.append(after + 1)
                cut = min(candidates, key=lambda value: abs(value - approximate)) if candidates else approximate
                cut = max(1, min(cut, part_size - 1))
                left_text = part.text[:cut]
                right_text = part.text[cut:]
                left.append(_ProjectAuditPart(part.file, left_text, part.start_line))
                right.append(_ProjectAuditPart(
                    part.file,
                    right_text,
                    part.start_line + left_text.count("\n"),
                ))
            consumed += part_size
        if not left or not right:
            return None
        return left, right

    def _audit_project_batch_resilient(
        self,
        parts: List[_ProjectAuditPart],
        *,
        ctx: Optional[AgentContext],
        depth: int = 0,
        budget: Optional[_SemanticAuditBudget] = None,
    ) -> _AdaptiveAuditResult:
        """输出截断或契约无效时缩小批次，不重发相同 prompt。"""
        if budget is None:
            budget = _SemanticAuditBudget(
                max_requests=settings.security_semantic_max_requests,
                deadline=time.monotonic() + settings.security_semantic_timeout_seconds,
            )
        request_count_before = budget.request_count
        if not budget.reserve():
            return _AdaptiveAuditResult(leaves=[(
                parts,
                _AuditChunkResult(
                    success=False,
                    error=budget.exhausted_reason,
                    failure_kind="semantic_budget_exhausted",
                ),
            )])

        result = self._llm_project_audit_batch(parts, ctx=ctx, budget=budget)
        outcome = _AdaptiveAuditResult(
            request_count=budget.request_count - request_count_before,
        )
        if result.failure_kind == "invalid_item":
            # 先用同一源码叶片做一次严格契约修复，避免因单条模型幻觉直接
            # 放弃整批已覆盖源码；修复仍失败时才进入常规拆分/失败门禁。
            repair_before = budget.request_count
            if budget.reserve():
                repaired = self._llm_project_audit_batch(
                    parts,
                    ctx=ctx,
                    budget=budget,
                    contract_repair=True,
                )
                outcome.request_count += budget.request_count - repair_before
                if repaired.success:
                    outcome.leaves.append((parts, repaired))
                    return outcome
                result = repaired
            else:
                result = _AuditChunkResult(
                    success=False,
                    error=budget.exhausted_reason,
                    failure_kind="semantic_budget_exhausted",
                    invalid_item_count=result.invalid_item_count,
                    invalid_item_kinds=result.invalid_item_kinds,
                )
        if result.success or result.failure_kind not in _RECOVERABLE_BATCH_FAILURE_KINDS:
            outcome.leaves.append((parts, result))
            return outcome

        source_chars = sum(len(part.text) for part in parts)
        if (
            depth >= settings.security_semantic_max_split_depth
            or source_chars <= settings.security_semantic_min_split_chars
        ):
            outcome.leaves.append((parts, result))
            return outcome
        split = self._split_project_audit_parts(parts)
        if split is None:
            outcome.leaves.append((parts, result))
            return outcome

        left, right = split
        self._emit(
            AgentEventType.PROGRESS,
            ctx,
            message=f"语义批次结果不可用，自适应拆分至深度 {depth + 1}",
            payload={
                "phase": "semantic_batch_split",
                "failure_kind": result.failure_kind,
                "depth": depth + 1,
                "source_chars": source_chars,
                "left_chars": sum(len(part.text) for part in left),
                "right_chars": sum(len(part.text) for part in right),
            },
        )
        left_result = self._audit_project_batch_resilient(
            left,
            ctx=ctx,
            depth=depth + 1,
            budget=budget,
        )
        right_result = self._audit_project_batch_resilient(
            right,
            ctx=ctx,
            depth=depth + 1,
            budget=budget,
        )
        outcome.leaves.extend(left_result.leaves)
        outcome.leaves.extend(right_result.leaves)
        outcome.request_count += left_result.request_count + right_result.request_count
        outcome.split_count = 1 + left_result.split_count + right_result.split_count
        return outcome

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
        budget: Optional[_SemanticAuditBudget] = None,
        contract_repair: bool = False,
    ) -> _AuditChunkResult:
        """同时审查一个项目批次中的多个源码文件并恢复原文件定位。"""
        out = _AuditChunkResult()
        if not parts:
            return out

        files_by_path: dict[str, CodeFile] = {}
        scoped_parts_by_path: dict[str, List[_ProjectAuditPart]] = {}
        sections: List[str] = []
        for part in parts:
            path = part.file.file_path or part.file.file_name
            files_by_path[path] = part.file
            scoped_parts_by_path.setdefault(path, []).append(part)
            sections.append(
                f"===== FILE {path} | LANGUAGE {part.file.language or 'plaintext'} "
                f"| START_LINE {part.start_line + 1} =====\n{part.text}"
            )
        source = "\n\n".join(sections)

        def canonicalize_line_endings(value: str) -> str:
            """让 JSON 中常见的 LF 证据可与 CRLF/CR 源码安全比对。"""
            return value.replace("\r\n", "\n").replace("\r", "\n")

        def contiguous_runs(path: str) -> List[List[_ProjectAuditPart]]:
            """只把当前模型请求中连续的同文件分片拼接用于定位证据。"""
            runs: List[List[_ProjectAuditPart]] = []
            for part in scoped_parts_by_path.get(path, []):
                if not runs:
                    runs.append([part])
                    continue
                previous = runs[-1][-1]
                previous_line_end = previous.start_line + previous.text.count("\n")
                previous_has_newline = previous.text.endswith(("\n", "\r"))
                is_contiguous = (
                    part.start_line == previous_line_end
                    if previous_has_newline
                    else part.start_line == previous.start_line
                )
                if is_contiguous:
                    runs[-1].append(part)
                else:
                    runs.append([part])
            return runs

        scoped_runs_by_path = {
            path: contiguous_runs(path) for path in scoped_parts_by_path
        }
        repair_instruction = (
            "上一轮同一源码叶片的输出包含无法逐字定位的条目。现在进行契约修复："
            "重新检查每一条结果，file_path、函数名、变量名和 evidence 必须直接复制当前叶片；"
            "不能定位的条目必须省略，禁止猜测、补全、改写或使用省略号。\n"
            if contract_repair else ""
        )
        prompt = (
            "你正在执行一次项目级白盒安全审计。下面是同一个项目源码索引中的一个多文件批次，"
            "必须结合路由、调用、配置、数据访问和相邻模块关系分析，不能把它当成互不相关的单文件任务。\n\n"
            "逐项排查 OWASP Top10、访问控制、注入、弱加密、认证会话、反序列化、供应链、"
            "日志泄密和 SSRF；只报告代码证据可以支撑的问题。\n"
            "严格输出 JSON，结构为：\n"
            '{"output_limited":false,'
            '"findings":[{"file_path":"源码中的精确路径","title":"...","category":"...",'
            '"owasp":"A03:2021-Injection","cwe":"CWE-89","severity":"严重|高|中|低",'
            '"line_start":1,"line_end":1,"evidence":"源码原文","exploit_scenario":"...",'
            '"fix_suggestion":"...","references":[],"confidence":0.9}],'
            '"entry_points":[{"file_path":"精确路径","name":"...","line":1,'
            '"evidence":"源码原文","input_source":"HTTP body|query|header"}],'
            '"dangerous_sinks":[{"file_path":"精确路径","name":"...","line":1,'
            '"evidence":"源码原文","sink_type":"SQL|exec|open|requests"}]}\n'
            "硬约束：file_path 必须逐字取自 FILE 标记；行号必须是原文件绝对行号；"
            "evidence 必须是源码中的原文（仅允许把 CRLF/CR 换行规范化为 LF）；禁止输出 Markdown 或解释。\n"
            "禁止根据语义改写函数名、变量名或 SQL；禁止使用省略号、占位符或源码片段之外的内容。"
            "无法在当前 FILE 叶片逐字定位的结果不要输出。\n"
            f"最多返回 {settings.security_semantic_max_findings_per_batch} 条 findings、"
            f"{settings.security_semantic_max_graph_items_per_batch} 条 entry_points 和 "
            f"{settings.security_semantic_max_graph_items_per_batch} 条 dangerous_sinks；"
            "按置信度和风险优先级保留，evidence 不超过 160 字符，"
            "exploit_scenario 和 fix_suggestion 各不超过 240 字符。"
            "如果当前源码存在超过上述任一条数上限的合格结果，必须把 output_limited 设为 true；"
            "否则必须设为 false。不得为了返回 false 而漏掉合格结果。\n\n"
            f"{repair_instruction}"
            f"{_knowledge_context('analysis')}"
            f"{source}"
        )
        result = self.call_json(
            prompt,
            ctx=ctx,
            recover_truncation=True,
            retry_reserver=budget.reserve if budget is not None else None,
            deadline_monotonic=budget.deadline if budget is not None else None,
            thinking=False,
        )
        if not result.success or not isinstance(result.data, dict):
            error = result.error or "LLM 返回了无效的项目审计结果"
            failure_kind = result.failure_kind
            if result.success and not isinstance(result.data, dict):
                failure_kind = "invalid_schema"
            logger.warning(
                f"[security_sentinel] 项目源码批次 LLM 调用失败: {error}"
            )
            return _AuditChunkResult(
                success=False,
                error=error,
                failure_kind=failure_kind,
                finish_reason=result.finish_reason,
            )

        required_lists = ("findings", "entry_points", "dangerous_sinks")
        if not isinstance(result.data.get("output_limited"), bool) or any(
            key not in result.data
            or not isinstance(result.data[key], list)
            or any(not isinstance(item, dict) for item in result.data[key])
            for key in required_lists
        ):
            error = "LLM 返回的项目审计 JSON 不符合列表结构契约"
            logger.warning(f"[security_sentinel] {error}")
            return _AuditChunkResult(
                success=False,
                error=error,
                failure_kind="invalid_schema",
                finish_reason=result.finish_reason,
            )
        if result.data["output_limited"]:
            return _AuditChunkResult(
                success=False,
                error="LLM 明确声明当前批次结果超过有界输出容量",
                failure_kind="output_limited",
                finish_reason=result.finish_reason,
            )

        finding_count = len(result.data["findings"])
        entry_count = len(result.data["entry_points"])
        sink_count = len(result.data["dangerous_sinks"])
        if any((
            finding_count >= settings.security_semantic_max_findings_per_batch,
            entry_count >= settings.security_semantic_max_graph_items_per_batch,
            sink_count >= settings.security_semantic_max_graph_items_per_batch,
        )):
            return _AuditChunkResult(
                success=False,
                error="LLM 返回结果达到或超过有界容量，必须缩小批次排除静默漏报",
                failure_kind="output_limited",
                finish_reason=result.finish_reason,
            )

        def valid_path(raw: dict) -> bool:
            path = str(raw.get("file_path") or "").strip().removeprefix("./")
            return bool(path and path in files_by_path)

        def locate_exact_source_span(
            path: str,
            needle: str,
            hinted_line: int = 0,
        ) -> Optional[Tuple[int, int]]:
            """用当前叶片的精确原文恢复绝对行号，避免信任模型的计数。"""
            if not needle:
                return None
            candidates: List[Tuple[int, int]] = []
            normalized_needle = canonicalize_line_endings(needle)
            for run in scoped_runs_by_path.get(path, []):
                normalized_source = canonicalize_line_endings(
                    "".join(part.text for part in run)
                )
                cursor = 0
                while True:
                    found = normalized_source.find(normalized_needle, cursor)
                    if found < 0:
                        break
                    start = run[0].start_line + 1 + normalized_source[:found].count("\n")
                    end_offset = found + max(0, len(normalized_needle) - 1)
                    end = run[0].start_line + 1 + normalized_source[:end_offset].count("\n")
                    candidates.append((start, end))
                    cursor = found + 1
            if not candidates:
                return None
            if hinted_line > 0:
                return min(candidates, key=lambda span: abs(span[0] - hinted_line))
            return candidates[0]

        findings = result.data["findings"]
        if any(
            (
                not valid_path(raw)
                or not str(raw.get("title") or "").strip()
                or raw.get("severity") not in _ALLOWED_SEVERITY
                or not str(raw.get("evidence") or "").strip()
                or (
                    "references" in raw
                    and not isinstance(raw.get("references"), list)
                )
            )
            for raw in findings
        ):
            error = "LLM 返回的项目审计 finding 不符合定位与证据契约"
            logger.warning(f"[security_sentinel] {error}")
            return _AuditChunkResult(success=False, error=error, failure_kind="invalid_schema")

        for raw in findings:
            raw_path = str(raw.get("file_path") or "").strip().removeprefix("./")
            evidence = str(raw.get("evidence") or "").strip()
            span = locate_exact_source_span(
                raw_path,
                evidence,
                self._coerce_int(raw.get("line_start"), 0),
            )
            if span is None:
                out.invalid_item_count += 1
                out.invalid_item_kinds["finding_evidence"] = (
                    out.invalid_item_kinds.get("finding_evidence", 0) + 1
                )
                logger.warning(
                    "[security_sentinel] 丢弃无法在当前源码叶片定位的 finding evidence"
                )
                continue
            raw["line_start"], raw["line_end"] = span
            normalized = self._normalize_finding(
                raw,
                file=files_by_path[raw_path],
                line_offset=0,
                code=files_by_path[raw_path].content or "",
            )
            if normalized:
                out.findings.append(normalized)

        for key in ("entry_points", "dangerous_sinks"):
            for raw in result.data[key]:
                raw_path = str(raw.get("file_path") or "").strip().removeprefix("./")
                name = str(raw.get("name") or "").strip()
                evidence = str(raw.get("evidence") or "").strip()
                if not valid_path(raw) or not name or not evidence:
                    out.invalid_item_count += 1
                    structure_kind = f"{key}_structure"
                    out.invalid_item_kinds[structure_kind] = (
                        out.invalid_item_kinds.get(structure_kind, 0) + 1
                    )
                    logger.warning(
                        "[security_sentinel] 丢弃结构不完整的 {} 条目",
                        key,
                    )
                    continue
                span = (
                    locate_exact_source_span(
                        raw_path,
                        evidence,
                        self._coerce_int(raw.get("line"), 0),
                    )
                )
                if span is None:
                    out.invalid_item_count += 1
                    out.invalid_item_kinds[f"{key}_evidence"] = (
                        out.invalid_item_kinds.get(f"{key}_evidence", 0) + 1
                    )
                    logger.warning(
                        "[security_sentinel] 丢弃无法在当前源码叶片定位的 {} evidence",
                        key,
                    )
                    continue
                raw["line"] = span[0]
                item = {
                    "file": raw_path,
                    "name": name[:200],
                    "line": self._coerce_int(raw.get("line"), 0),
                }
                if key == "entry_points":
                    item["input_source"] = str(raw.get("input_source") or "")[:200]
                    out.entry_points.append(item)
                else:
                    item["sink_type"] = str(raw.get("sink_type") or "")[:100]
                    out.dangerous_sinks.append(item)
        if out.invalid_item_count:
            error = (
                "LLM 返回的项目审计包含无法严格验证的条目: "
                f"{out.invalid_item_count} 条"
            )
            logger.warning(f"[security_sentinel] {error}")
            out.success = False
            out.error = error
            out.failure_kind = "invalid_item"
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
                               api_endpoints: Optional[List[dict]] = None,
                               budget: Optional[_SemanticAuditBudget] = None,
                               ) -> Optional[_BoundedGraphResult]:
        """第二轮 LLM:跨文件数据流推断"""
        if budget is not None and not budget.reserve():
            return None
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
        result = self.call_json(
            user_msg,
            ctx=ctx,
            recover_truncation=budget is not None,
            retry_reserver=budget.reserve if budget is not None else None,
            deadline_monotonic=budget.deadline if budget is not None else None,
            thinking=False,
        )
        if not result.success or not isinstance(result.data, dict):
            return None
        if "data_flows" not in result.data:
            return None
        raw_flows = result.data["data_flows"]
        if not isinstance(raw_flows, list):
            return None
        flows: List[dict] = []
        flow_link_keys: set[tuple[str, str]] = set()
        for f in raw_flows:
            if not isinstance(f, dict):
                return None
            via = f.get("via", [])
            if not isinstance(via, list):
                return None
            normalized = {
                "from": str(f.get("from") or "")[:500],
                "via": [str(v)[:500] for v in via[:20] if v],
                "to": str(f.get("to") or "")[:500],
                "risk_type": str(f.get("risk_type") or "")[:200],
                "severity": (
                    f.get("severity")
                    if f.get("severity") in _ALLOWED_SEVERITY else "中"
                ),
            }
            if normalized["from"] and normalized["to"]:
                flow_link_keys.add((normalized["from"], normalized["to"]))
            if len(flows) < 100:
                flows.append(normalized)
        return _BoundedGraphResult(
            items=flows,
            total_count=len(raw_flows),
            unique_link_count=len(flow_link_keys),
        )

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
            "exploit_scenario": str(raw.get("exploit_scenario") or "")[:1_000],
            "fix_suggestion": str(raw.get("fix_suggestion") or "")[:1_000],
            "references": [str(r)[:500] for r in (raw.get("references") or []) if r][:5],
            "confidence": confidence,
            "source": "llm",
        }

    @staticmethod
    def _locate_evidence_line(code: str, evidence: str) -> int:
        """用 evidence 原文在代码块里反查行号(1-based 相对行号),找不到返回 0。

        LLM(尤其小模型)常漏给 line_start,导致漏洞只有描述、没有定位。
        这里取 evidence 中最有辨识度的一行,在代码里做子串匹配兜底；仅规范化换行符。
        """
        if not code or not evidence:
            return 0
        ev_lines = [ln.strip() for ln in evidence.splitlines() if ln.strip()]
        if not ev_lines:
            return 0
        needle = max(ev_lines, key=len).strip("`. ").strip()
        if len(needle) < 4:
            return 0
        normalized_code = code.replace("\r\n", "\n").replace("\r", "\n")
        normalized_needle = needle.replace("\r\n", "\n").replace("\r", "\n")
        position = normalized_code.find(normalized_needle)
        if position >= 0:
            return normalized_code.count("\n", 0, position) + 1
        # 放宽:用前若干字符再试一次(应对 evidence 带省略号/尾部差异)
        compact = normalized_needle[:16]
        if len(compact) >= 6:
            position = normalized_code.find(compact)
            if position >= 0:
                return normalized_code.count("\n", 0, position) + 1
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

        def score(f: CodeFile) -> tuple[int, str, int]:
            name = (f.file_path or f.file_name or "").lower()
            return -sum(1 for k in keywords if k in name), name, f.id

        return sorted(files, key=score)

    # ============ v3.3 全链路: 对抗复检 / 去重 / 攻击面 ============

    def _dedup_findings(self, findings: List[dict]) -> List[dict]:
        """按 (文件, 行号归组, 类别) 去重,合并多来源重复报告(压误报第一步)"""
        seen: dict[tuple, dict] = {}
        order: List[tuple] = []
        for f in findings:
            if not isinstance(f, dict):
                continue
            path = str(f.get("file_path") or "")
            line = self._coerce_int(f.get("line_number"), 0)
            bucket = line // 4
            cat = str(f.get("category") or f.get("title") or "")[:12]
            key = (path, bucket, cat)
            if key in seen:
                cur = seen[key]
                if (float(f.get("confidence", 0) or 0),
                        _SEVERITY_DEDUCT.get(f.get("severity", "中"), 3)) > \
                   (float(cur.get("confidence", 0) or 0),
                        _SEVERITY_DEDUCT.get(cur.get("severity", "中"), 3)):
                    seen[key] = f
            else:
                seen[key] = f
                order.append(key)
        return [seen[k] for k in order]

    def _adversarial_verify(self, findings: List[dict],
                            ctx: Optional[AgentContext],
                            max_review: int = 12) -> dict:
        """对抗式复检: 以质疑者视角 + 已知误报模式复核高危 finding,确认或证伪.

        解决「漏洞真假难辨」: 初判(LLM 易幻觉)→ 对抗复检(试图证伪)→ 确证/证伪。
        注入知识库的「已知误报模式」,让模型识别框架自动转义/参数绑定/类型约束等
        常见误报,显著压掉误报。只复核 严重/高 危且数量受控,避免 token 爆炸。
        """
        confirmed = 0
        refuted = 0
        reviewed = 0
        high = [f for f in findings
                if isinstance(f, dict) and f.get("severity") in {"严重", "高"}]
        high.sort(key=lambda x: (
            -_SEVERITY_DEDUCT.get(x.get("severity", "中"), 3),
            -float(x.get("confidence", 0) or 0),
        ))
        targets = high[:max_review]
        if not targets:
            return {"confirmed": 0, "refuted": 0, "reviewed": 0,
                    "note": "无高危 finding 需复检"}

        items = []
        for i, f in enumerate(targets, 1):
            items.append(
                f"[{i}] {f.get('severity')} {f.get('category','')} "
                f"{f.get('file_path','')}:{f.get('lines','')}\n"
                f"  证据: {str(f.get('evidence',''))[:160]}\n"
                f"  描述: {str(f.get('exploit_scenario',''))[:160]}"
            )
        prompt = (
            "你是资深安全审计员,下面是某项目自动扫描出的高危漏洞候选。"
            "请以**质疑者**视角逐条复核,结合 PHP 常见误报模式"
            "(框架自动转义、ORM 参数绑定、路由类型约束、整数强转、该参数非用户可控、"
            "全局过滤器已拦截、仅是日志/注释等),判断每条是否**真实可利用**。\n"
            "对每条输出 verdict: confirmed(确认可利用)/plausible(疑似,需人工)/"
            "refuted(误报,给出理由)。\n\n"
            f"{_knowledge_context('verification')}"
            "候选漏洞:\n" + "\n\n".join(items) + "\n\n"
            "严格输出 JSON(不要 markdown): "
            '{"reviews":[{"index":1,"verdict":"confirmed|plausible|refuted","reason":"..."}]}'
        )
        result = self.call_json(prompt, ctx=ctx, thinking=False)
        if result.success and isinstance(result.data, dict):
            reviews = result.data.get("reviews") or []
            verdict_by_index = {}
            for r in reviews:
                if isinstance(r, dict):
                    try:
                        verdict_by_index[int(r.get("index"))] = str(r.get("verdict") or "")
                    except (TypeError, ValueError):
                        continue
            for i, f in enumerate(targets, 1):
                reviewed += 1
                verdict = verdict_by_index.get(i, "")
                f["verification"] = verdict or "unreviewed"
                if verdict == "confirmed":
                    confirmed += 1
                    f["confidence"] = min(1.0, float(f.get("confidence", 0.8) or 0) + 0.15)
                elif verdict == "refuted":
                    refuted += 1
                    f["confidence"] = min(float(f.get("confidence", 0.8) or 0), 0.3)
        else:
            logger.warning("[security_sentinel] 对抗复检 LLM 调用失败,跳过错杀")
        return {"confirmed": confirmed, "refuted": refuted, "reviewed": reviewed}

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

from functools import lru_cache  # noqa: E402


@lru_cache(maxsize=8)
def _knowledge_context(role: str) -> str:
    """加载 PHP 全链路审计知识库并按角色分层注入(v3.3).

    结果缓存——同一进程内多次批次调用共享,避免重复读盘。
    迁移自 yunmengya/PHP_AUDIT_SKILLS 的反幻觉/误报/sink/攻击链等知识,
    用于压低误报率、约束模型不乱报(对应「假警报多」痛点)。
    加载失败时静默降级为空串,不影响主审计流程。
    """
    try:
        from app.ai import audit_knowledge_loader
        ctx = audit_knowledge_loader.build_prompt_context(role)
        return ("\n\n" + ctx + "\n\n") if ctx else ""
    except Exception:
        return ""


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
