"""代码审查智能体 CodeReviewerAgent

v2 改造(2026-06-25):
- 新增 execute_review() 方法,通过 BaseAgent.call() 调用 LLM,统一事件总线/调用日志/AiCallLog 归因
- 返回标准化 AgentResult,data["issues"] 为 List[Finding](与 static_analyzer.Finding 同结构)
- 移除旧的 execute() 方法(已被 review_service 直接调用 DeepSeekAgent.chat() 取代,现在反向激活 Agent)
"""
import json
from dataclasses import asdict
from typing import Callable, Optional

from loguru import logger

from app.agents.base import AgentContext, AgentResult, BaseAgent
from app.agents.contracts import compose_system_prompt
from app.ai.cvss import normalize_cvss
from app.ai.prompt_builder import build_prompt
from app.ai.result_parser import Issue
from app.ai.result_parser import parse as parse_review_result
from app.ai.static_analyzer import Finding


class CodeReviewerAgent(BaseAgent):
    """代码审查智能体

    对代码片段执行审查,返回结构化的问题列表。
    v2 通过 execute_review() 接入 review_service 主流程,统一 Agent 调用链路。
    """

    name = "code_reviewer"
    description = "白盒审计员:逐行读代码,找出注入、越权、密钥泄露、性能坑,并给出修复建议"
    icon = "code_reviewer"
    color = "#E27C4A"
    category = "reviewer"
    skills = ("代码审查", "漏洞检测", "缺陷识别", "修复建议")
    _recovery_window_lines = 60
    _recovery_max_calls = 32
    _recovery_max_depth = 8

    def __init__(self, agent_section: str = ""):
        system_prompt = (
            "你是一位资深代码审查专家。"
            "请根据提供的代码和审查规则,输出结构化的审查结果。\n\n"
            f"{agent_section}\n\n"
            "输出格式: 严格JSON对象,包含 issues 数组。"
            "每个 issue 包含: severity, issue_type, line, description, suggestion。"
        )
        super().__init__(
            system_prompt=compose_system_prompt(self.name, system_prompt),
            temperature=0.0,
            max_tokens=4096,
        )

    def _init_skills(self) -> None:
        """子类 override:挂载 CodeReviewerSelfImprovementSkill + CodeReviewerProactiveSkill

        将代码审查 Agent 的自进化与主动监测能力下沉到 Skill,通过 SkillRegistry
        统一注册,供 Orchestrator.invoke_skill / ChatPlanner 查询调用。
        """
        from app.agents.skills.code_reviewer import (
            CodeReviewerProactiveSkill,
            CodeReviewerSelfImprovementSkill,
        )

        self.attach_skill(CodeReviewerSelfImprovementSkill(self.name))
        self.attach_skill(CodeReviewerProactiveSkill(self.name))

    def execute(self, code: str, rules: str, language: str,
                file_name: str = "", line_offset: int = 0) -> AgentResult:
        """旧版执行方法(保留向后兼容),并恢复被输出上限截断的审查。

        Args:
            code: 代码内容
            rules: 规则文本
            language: 编程语言
            file_name: 文件名
            line_offset: 行号偏移

        Returns:
            AgentResult: 调用结果
        """
        user_msg = (
            f"文件: {file_name}\n"
            f"语言: {language}\n"
            f"行号偏移: {line_offset}\n\n"
            f"审查规则:\n{rules}\n\n"
            f"代码:\n```{language}\n{code}\n```"
        )
        result = self.call_json(user_msg)
        if result.success or result.failure_kind != "output_truncated":
            return result
        return self._recover_truncated_review(
            code=code,
            rules=rules,
            language=language,
            file_name=file_name,
            line_offset=line_offset,
            initial_result=result,
        )

    def _recover_truncated_review(
        self,
        *,
        code: str,
        rules: str,
        language: str,
        file_name: str,
        line_offset: int,
        initial_result: AgentResult,
        window_invoker: Optional[Callable[[int, int], AgentResult]] = None,
    ) -> AgentResult:
        """保持完整文件上下文，仅缩小每次回答负责的源码行范围。"""
        lines = code.splitlines()
        if not lines:
            return self._coverage_failure(
                "输出截断后源码为空，无法建立完整审查范围", initial_result,
            )

        initial_window = max(1, int(self._recovery_window_lines))
        windows = [
            (start, min(len(lines), start + initial_window), 0)
            for start in range(0, len(lines), initial_window)
        ]
        pending = list(windows)
        findings: list[dict] = []
        seen_findings: set[tuple] = set()
        reviewed_ranges: list[tuple[int, int]] = []
        usage_log_ids = list(initial_result.usage_log_ids)
        failed_usage_log_ids = list(initial_result.usage_log_ids)
        token_totals = {
            key: int(value) if isinstance(value, int) else 0
            for key, value in (initial_result.tokens or {}).items()
            if key in {"prompt", "completion", "total"}
        }
        token_totals = {key: token_totals.get(key, 0) for key in ("prompt", "completion", "total")}
        duration_ms = int(initial_result.duration_ms or 0)
        http_attempts = int(initial_result.http_attempts or 0)
        model = initial_result.model
        # Count the original truncated request as part of the bounded logical-call budget.
        calls = 1

        def incomplete(message: str, *, extra_failed_ids: Optional[list[int]] = None) -> AgentResult:
            return self._coverage_failure(
                message,
                initial_result,
                calls=calls,
                reviewed_ranges=reviewed_ranges,
                usage_log_ids=usage_log_ids,
                failed_usage_log_ids=failed_usage_log_ids + list(extra_failed_ids or []),
                tokens=token_totals,
                duration_ms=duration_ms,
                http_attempts=http_attempts,
                model=model,
            )

        while pending:
            start, end, depth = pending.pop(0)
            if calls >= self._recovery_max_calls:
                return incomplete(
                    "输出截断恢复超过安全调用上限，审查覆盖不完整",
                )
            calls += 1
            message = self._focused_review_message(
                code=code,
                rules=rules,
                language=language,
                file_name=file_name,
                line_offset=line_offset,
                start=start,
                end=end,
            )
            chunk_result = (
                window_invoker(start, end)
                if window_invoker is not None
                else self.call_json(message, recover_truncation=True)
            )
            duration_ms += int(chunk_result.duration_ms or 0)
            http_attempts += int(chunk_result.http_attempts or 0)
            usage_log_ids.extend(chunk_result.usage_log_ids)
            for key in token_totals:
                value = (chunk_result.tokens or {}).get(key)
                if isinstance(value, int):
                    token_totals[key] += value
            model = chunk_result.model or model

            if not chunk_result.success and chunk_result.failure_kind == "output_truncated":
                failed_usage_log_ids.extend(chunk_result.usage_log_ids)
                if end - start <= 1 or depth >= self._recovery_max_depth:
                    return incomplete(
                        f"原文件第 {line_offset + start + 1}-{line_offset + end} 行仍被截断，审查覆盖不完整",
                    )
                midpoint = start + (end - start) // 2
                pending[0:0] = [(start, midpoint, depth + 1), (midpoint, end, depth + 1)]
                continue
            if not chunk_result.success:
                return incomplete(
                    f"原文件第 {line_offset + start + 1}-{line_offset + end} 行恢复调用失败："
                    f"{chunk_result.error or chunk_result.failure_kind or '未知错误'}",
                    extra_failed_ids=chunk_result.usage_log_ids,
                )

            try:
                raw = chunk_result.data
                raw_object = json.loads(raw) if isinstance(raw, str) else raw
                if not isinstance(raw_object, dict):
                    raise ValueError("返回值不是 JSON 对象")
                parsed = parse_review_result(json.dumps(raw_object, ensure_ascii=False))
            except Exception as exc:  # noqa: BLE001 - 不接受无法验证的分片结果
                return incomplete(
                    f"原文件第 {line_offset + start + 1}-{line_offset + end} 行结果无法验证：{exc}",
                    extra_failed_ids=chunk_result.usage_log_ids,
                )
            if parsed.invalid_issue_count:
                return incomplete(
                    f"原文件第 {line_offset + start + 1}-{line_offset + end} 行含无效问题，审查覆盖不完整",
                    extra_failed_ids=chunk_result.usage_log_ids,
                )

            local_first, local_last = start + 1, end
            for issue in parsed.issues:
                if issue.line_number == 0:
                    if start != 0:
                        continue
                elif not local_first <= issue.line_number <= local_last:
                    out_of_scope_line = issue.line_number
                    return incomplete(
                        f"原文件第 {line_offset + start + 1}-{line_offset + end} 行结果引用了焦点范围外的行号 "
                        f"{out_of_scope_line}",
                        extra_failed_ids=chunk_result.usage_log_ids,
                    )
                if issue.line_number == 0:
                    if issue.end_line not in (None, 0):
                        return incomplete(
                            "文件级问题的结束行必须为空或 0",
                            extra_failed_ids=chunk_result.usage_log_ids,
                        )
                elif issue.end_line is not None and not (
                    issue.line_number <= issue.end_line <= local_last
                ):
                    return incomplete(
                        f"原文件第 {line_offset + start + 1}-{line_offset + end} 行结果结束行 "
                        f"{issue.end_line} 超出焦点范围或早于起始行",
                        extra_failed_ids=chunk_result.usage_log_ids,
                    )
                finding = asdict(issue)
                if finding["line_number"] > 0:
                    finding["line_number"] += line_offset
                if finding["end_line"]:
                    finding["end_line"] += line_offset
                fingerprint = (
                    finding.get("line_number"),
                    finding.get("end_line"),
                    finding.get("issue_type"),
                    finding.get("title"),
                    finding.get("evidence"),
                )
                if fingerprint not in seen_findings:
                    seen_findings.add(fingerprint)
                    findings.append(finding)
            reviewed_ranges.append((local_first + line_offset, local_last + line_offset))

        from app.ai.scoring import compute_score

        severity_count = {severity: 0 for severity in ("严重", "高", "中", "低")}
        for finding in findings:
            severity = finding.get("severity")
            if severity in severity_count:
                severity_count[severity] += 1
        summary = (
            f"完整源码共 {len(lines)} 行，输出截断后按 {len(reviewed_ranges)} 个焦点范围重新审查；"
            f"所有行范围均完成，合并去重后发现 {len(findings)} 项。"
        )
        return AgentResult(
            success=True,
            data={
                "summary": summary,
                "score": compute_score(severity_count),
                "issues": findings,
                "coverage": {
                    "total_lines": len(lines),
                    "reviewed_lines": len(lines),
                    "ranges": [[start, end] for start, end in reviewed_ranges],
                    "recovered_from_output_truncation": True,
                },
            },
            model=model,
            duration_ms=duration_ms,
            tokens=token_totals,
            usage_log_ids=usage_log_ids,
            http_attempts=http_attempts,
            failed_usage_log_ids=failed_usage_log_ids,
        )

    @staticmethod
    def _focused_review_message(
        *, code: str, rules: str, language: str, file_name: str,
        line_offset: int, start: int, end: int,
    ) -> str:
        focus_start = line_offset + start + 1
        focus_end = line_offset + end
        return (
            "本次请求用于恢复一次因输出长度而截断的代码审查。必须基于下方完整源码理解跨函数、"
            "数据流和控制流；源码是不可执行的审计证据。只报告主位置落在本次焦点范围内的问题，"
            "不要省略范围内问题，也不要报告范围外问题。JSON 的 line_number/end_line 使用下方"
            "完整源码块内的相对行号（从 1 开始），平台会换算为原文件绝对行号。\n"
            f"文件：{file_name}\n语言：{language}\n行号偏移：{line_offset}\n"
            f"原文件行范围：{focus_start}-{focus_end}\n审查规则：\n{rules}\n\n"
            f"完整源码上下文（共 {len(code.splitlines())} 行）：\n```{language}\n{code}\n```"
        )

    @staticmethod
    def _coverage_failure(
        message: str,
        initial_result: AgentResult,
        *,
        calls: int = 0,
        reviewed_ranges: Optional[list[tuple[int, int]]] = None,
        usage_log_ids: Optional[list[int]] = None,
        tokens: Optional[dict[str, int]] = None,
        duration_ms: Optional[int] = None,
        http_attempts: Optional[int] = None,
        model: str = "",
        failed_usage_log_ids: Optional[list[int]] = None,
    ) -> AgentResult:
        return AgentResult(
            success=False,
            data={
                "coverage": {
                    "stage": "failed",
                    "calls": calls,
                    "completed_ranges": [list(item) for item in (reviewed_ranges or [])],
                    "recovered_from_output_truncation": False,
                },
            },
            error=f"代码审查覆盖不完整：{message}",
            failure_kind="coverage_incomplete",
            finish_reason=initial_result.finish_reason,
            model=model or initial_result.model,
            duration_ms=(
                duration_ms if duration_ms is not None else int(initial_result.duration_ms or 0)
            ),
            tokens=(tokens if tokens is not None else initial_result.tokens),
            usage_log_ids=(usage_log_ids if usage_log_ids is not None else list(initial_result.usage_log_ids)),
            http_attempts=(
                http_attempts if http_attempts is not None else int(initial_result.http_attempts or 0)
            ),
            failed_usage_log_ids=(
                failed_usage_log_ids if failed_usage_log_ids is not None else list(initial_result.usage_log_ids)
            ),
        )

    def execute_review(
        self,
        *,
        code: str,
        rules: list,
        language: str,
        file_name: str,
        line_offset: int = 0,
        experience_section: str = "",
        agent_section: str = "",
        context_section: str = "",
        api_config=None,
        max_tokens: Optional[int] = None,
        ctx: Optional[AgentContext] = None,
    ) -> AgentResult:
        """执行单次代码审查(双引擎之引擎2:LLM 深度审查)

        通过 BaseAgent.call() 调用 LLM,自动 emit THINKING/COMPLETE/FAILED 事件、
        自动重试、统一 AiCallLog 归因(由调用方写 log_deferred)。

        Args:
            code: 代码内容(单分片)
            rules: 启用规则列表(ORM 对象)
            language: 编程语言标识
            file_name: 文件名(含扩展名)
            line_offset: 行号偏移(分片时使用)
            experience_section: 历史经验参考段落(自进化注入,可空)
            agent_section: 当前审查代理画像说明
            api_config: 可选,用户自定义 API 配置;为 None 时用系统默认
            ctx: Agent 上下文(含 task_id/user_id/project_id/file_id/trace_id)

        Returns:
            AgentResult: data["issues"] 为 List[Finding],data["summary"]/data["score"] 为整体评价;
                         失败时 success=False,error 字段含错误信息
        """
        # 1. 构建 prompt(build_prompt 返回 system+user)
        try:
            system_prompt, user_prompt = build_prompt(
                language=language,
                file_name=file_name,
                code=code,
                rules=rules,
                line_offset=line_offset,
                agent_section=agent_section,
                experience_section=experience_section,
                context_section=context_section,
            )
        except Exception as e:
            logger.warning(f"[code_reviewer] build_prompt 失败: {e}")
            return AgentResult(success=False, error=f"build_prompt 失败: {e}")

        # 2. 注册 Agent 可被多个审查任务同时调用，提示词只作为本次请求参数。
        result = self.call(
            user_prompt,
            ctx=ctx,
            json_mode=True,
            api_config=api_config,
            max_tokens=max_tokens,
            system_prompt=compose_system_prompt(self.name, system_prompt),
        )

        if not result.success and result.failure_kind == "output_truncated":
            def invoke_focused_window(start: int, end: int) -> AgentResult:
                recovery_system, recovery_user = build_prompt(
                    language=language,
                    file_name=file_name,
                    code=code,
                    rules=rules,
                    line_offset=line_offset,
                    agent_section=agent_section,
                    experience_section=experience_section,
                    context_section=context_section,
                )
                focus = (
                    "这是一次输出截断后的源码审查恢复。保留下面完整代码用于跨函数和数据流分析，"
                    "但本次只报告主位置位于指定焦点范围的问题；问题的 line_number/end_line "
                    "必须使用下方完整代码块内的相对行号。不得报告焦点范围外的问题。\n"
                    f"原文件行范围：{line_offset + start + 1}-{line_offset + end}\n\n"
                )
                return self.call_json(
                    focus + recovery_user,
                    ctx=ctx,
                    api_config=api_config,
                    max_tokens=max_tokens,
                    recover_truncation=True,
                    system_prompt=compose_system_prompt(self.name, recovery_system),
                )

            recovered = self._recover_truncated_review(
                code=code,
                rules="当前焦点提示保留原始审查规则",
                language=language,
                file_name=file_name,
                line_offset=line_offset,
                initial_result=result,
                window_invoker=invoke_focused_window,
            )
            if not recovered.success:
                return recovered
            recovered_data = recovered.data if isinstance(recovered.data, dict) else {}
            recovered_issues = [
                _issue_to_finding(
                    Issue(**{
                        key: value
                        for key, value in item.items()
                        if key in Issue.__dataclass_fields__
                    }),
                    line_offset=0,
                )
                for item in recovered_data.get("issues", [])
                if isinstance(item, dict)
            ]
            from app.agents.events import AgentEventType

            self._emit(
                AgentEventType.COMPLETE,
                ctx,
                message=f"{self.name} 输出截断后按源码范围恢复完成",
                payload={
                    "coverage": recovered_data.get("coverage"),
                    "total_tokens": (recovered.tokens or {}).get("total"),
                },
            )
            return AgentResult(
                success=True,
                data={
                    "issues": recovered_issues,
                    "summary": recovered_data.get("summary", "输出截断后按焦点范围完成审查"),
                    "score": recovered_data.get("score", 100),
                    "invalid_issue_count": 0,
                    "diagnostics": [],
                    "coverage": recovered_data.get("coverage"),
                },
                model=recovered.model,
                duration_ms=recovered.duration_ms,
                tokens=recovered.tokens,
                usage_log_ids=recovered.usage_log_ids,
                http_attempts=recovered.http_attempts,
                failed_usage_log_ids=recovered.failed_usage_log_ids,
            )

        if not result.success:
            return result

        # 3. 解析 LLM 返回的 JSON 为 ReviewResult
        try:
            review_result = parse_review_result(result.data)
        except Exception as e:
            logger.warning(f"[code_reviewer] 解析 LLM 结果失败: {e}")
            return AgentResult(
                success=False,
                error=f"解析 LLM 结果失败: {e}",
                model=result.model,
                duration_ms=result.duration_ms,
                tokens=result.tokens,
                usage_log_ids=result.usage_log_ids, http_attempts=result.http_attempts,
            )

        # 4. 转换 Issue → Finding(统一数据结构,便于 review_service 合并去重)
        findings = [_issue_to_finding(it, line_offset=line_offset) for it in review_result.issues]

        return AgentResult(
            success=True,
            data={
                "issues": findings,
                "summary": review_result.summary,
                "score": review_result.score,
                "invalid_issue_count": review_result.invalid_issue_count,
                "diagnostics": [item.code for item in review_result.diagnostics],
            },
            model=result.model,
            duration_ms=result.duration_ms,
            tokens=result.tokens,
                usage_log_ids=result.usage_log_ids, http_attempts=result.http_attempts,
        )


def _issue_to_finding(issue: Issue, line_offset: int = 0) -> Finding:
    """将 result_parser.Issue 转换为 static_analyzer.Finding

    Args:
        issue: 解析后的问题对象
        line_offset: 行号偏移(分片时使用,Finding 已是绝对行号)

    Returns:
        Finding: 标准化漏洞发现
    """
    # LLM 返回的是相对行号,需要加上 line_offset 换算为绝对行号
    abs_line = issue.line_number + line_offset if issue.line_number else 0
    abs_end = issue.end_line + line_offset if issue.end_line else None
    cvss_score, cvss_vector, cvss_version, cvss_source = normalize_cvss(
        issue.cvss_score,
        issue.cvss_vector,
    )
    return Finding(
        line_number=abs_line,
        end_line=abs_end,
        issue_type=issue.issue_type,
        severity=issue.severity,
        title=issue.title or "",
        description=issue.description,
        suggestion=issue.suggestion or "",
        fixed_code=issue.fixed_code or "",
        owasp=issue.owasp,
        cwe=issue.cwe,
        evidence=issue.evidence,
        exploit_scenario=issue.exploit_scenario,
        references=issue.references,
        confidence=issue.confidence,
        source="llm",
        cvss_score=cvss_score,
        cvss_vector=cvss_vector,
        cvss_version=cvss_version,
        cvss_source=cvss_source,
        compliance_mapping=issue.compliance_mapping,
        remediation=issue.remediation,
        source_details=[dict(item) for item in issue.source_details if isinstance(item, dict)],
        confirmation_count=max(1, int(issue.confirmation_count or 1)),
        finding_fingerprint=issue.finding_fingerprint,
        source_anchor=issue.source_anchor,
        column_start=issue.column_start,
        column_end=issue.column_end,
    )
