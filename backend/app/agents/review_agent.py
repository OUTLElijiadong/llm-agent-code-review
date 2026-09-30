"""代码审查智能体 CodeReviewerAgent

v2 改造(2026-06-25):
- 新增 execute_review() 方法,通过 BaseAgent.call() 调用 LLM,统一事件总线/调用日志/AiCallLog 归因
- 返回标准化 AgentResult,data["issues"] 为 List[Finding](与 static_analyzer.Finding 同结构)
- 移除旧的 execute() 方法(已被 review_service 直接调用 DeepSeekAgent.chat() 取代,现在反向激活 Agent)
"""
import hashlib
import json
from dataclasses import asdict
from typing import Callable, Optional

from loguru import logger

from app.agents.base import AgentContext, AgentResult, BaseAgent
from app.agents.contracts import compose_system_prompt
from app.ai.code_chunker import build_symbol_index, chunk_code_with_context
from app.ai.cvss import normalize_cvss
from app.ai.prompt_builder import build_prompt, format_rules
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
    _recovery_window_chars = 6_000
    _recovery_max_calls = 32
    _recovery_max_depth = 8
    _rule_batch_max_bytes = 250_000

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
                file_name: str = "", line_offset: int = 0,
                ctx: Optional[AgentContext] = None) -> AgentResult:
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
        # 团队调度器直接调用该兼容入口。大文件在首次模型调用前就按源码
        # 结构分片，避免“整份源码 + 受限输出预算”先撞上长度上限。
        user_msg = (
            f"文件: {file_name}\n"
            f"语言: {language}\n"
            f"行号偏移: {line_offset}\n\n"
            f"审查规则:\n{rules}\n\n"
            f"代码:\n```{language}\n{code}\n```"
        )
        _, input_exceeded = self._project_input(user_msg)
        if len(code) > self._recovery_window_chars or input_exceeded:
            return self._execute_bounded_rule_batches(
                code=code,
                rules=rules,
                language=language,
                file_name=file_name,
                line_offset=line_offset,
                ctx=ctx,
            )
        result = self.call_json(user_msg, ctx=ctx)
        if result.success or result.failure_kind != "output_truncated":
            if result.success or result.failure_kind not in {
                "invalid_response", "invalid_json", "incomplete_response",
            }:
                return result
        return self._recover_truncated_review(
            code=code,
            rules=rules,
            language=language,
            file_name=file_name,
            line_offset=line_offset,
            initial_result=result,
            coverage_scope="file",
            ctx=ctx,
        )

    def _execute_bounded_rule_batches(
        self,
        *,
        code: str,
        rules: str,
        language: str,
        file_name: str,
        line_offset: int,
        ctx: Optional[AgentContext],
    ) -> AgentResult:
        """Losslessly review all rules in bounded batches, covering the full source each time."""
        from app.ai.scoring import compute_score
        from app.core.config import settings

        window = int(settings.deepseek_context_window_tokens)
        batch_limit = max(1_024, min(self._rule_batch_max_bytes, max(1_024, (window - 40_000) // 3)))
        try:
            batches = self._split_review_rules(rules, batch_limit)
        except ValueError as exc:
            return AgentResult(
                success=False,
                error=f"审查规则无法无损分批：{exc}",
                failure_kind="input_exceeds_context",
                data={"coverage": {"stage": "failed", "calls": 0, "rule_batches_completed": 0}},
            )
        if len(batches) > self._recovery_max_calls:
            return AgentResult(
                success=False,
                error="审查规则分批超过安全调用预算",
                failure_kind="semantic_budget_exhausted",
                data={"coverage": {"stage": "failed", "calls": 0, "rule_batches_total": len(batches)}},
            )

        pending_batches = list(batches)
        results: list[tuple[str, AgentResult]] = []
        calls = 0
        usage_log_ids: list[int] = []
        failed_usage_log_ids: list[int] = []
        tokens = {"prompt": 0, "completion": 0, "total": 0}
        duration_ms = 0
        http_attempts = 0
        model = ""
        source_digest = hashlib.sha256(code.encode("utf-8")).hexdigest()

        index = 0
        while index < len(pending_batches):
            batch = pending_batches[index]
            initial = AgentResult(
                success=False,
                error="输入经 UTF-8 字节预检后进入完整覆盖的规则分批",
                failure_kind="bounded_review_required",
            )
            result = self._recover_truncated_review(
                code=code,
                rules=batch,
                language=language,
                file_name=file_name,
                line_offset=line_offset,
                initial_result=initial,
                coverage_scope="file",
                ctx=ctx,
                max_calls=self._recovery_max_calls - calls,
            )
            coverage = result.data.get("coverage", {}) if isinstance(result.data, dict) else {}
            calls += int(coverage.get("calls", 0) or 0)
            duration_ms += int(result.duration_ms or 0)
            http_attempts += int(result.http_attempts or 0)
            model = result.model or model
            usage_log_ids.extend(result.usage_log_ids)
            failed_usage_log_ids.extend(result.failed_usage_log_ids)
            for key in tokens:
                tokens[key] += int((result.tokens or {}).get(key) or 0)

            # The byte-based initial batch limit is deliberately conservative, but the
            # focused recovery envelope also carries a system prompt and symbol context.
            # If BaseAgent rejects that full envelope before any HTTP request, retry by
            # splitting only at whole JSON-rule or free-form line boundaries. Never split
            # a rule's contents or treat source-line splitting as a remedy for large rules.
            if (
                not result.success
                and result.failure_kind == "input_exceeds_context"
                and result.http_attempts == 0
                and not result.usage_log_ids
            ):
                smaller_batches = self._split_review_rules_in_half(batch)
                if smaller_batches:
                    pending_batches[index:index + 1] = smaller_batches
                    continue

            if not result.success:
                failed_coverage = result.data.get("coverage", {}) if isinstance(result.data, dict) else {}
                completed_ranges = [
                    item[1].data.get("coverage", {}).get("ranges", [])
                    for item in results
                ]
                return AgentResult(
                    success=False,
                    data={"coverage": {
                        "stage": "failed",
                        "calls": calls,
                        "rule_batches_completed": len(results),
                        "rule_batches_total": len(pending_batches),
                        "failed_batch": index + 1,
                        "completed_ranges": completed_ranges,
                        "failed_batch_completed_ranges": failed_coverage.get("completed_ranges", []),
                        "input_rejected_before_http": (
                            result.failure_kind == "input_exceeds_context"
                            and result.http_attempts == 0
                            and not result.usage_log_ids
                        ),
                        "total_lines": len(code.splitlines()),
                        "recovered_from_output_truncation": (
                            result.failure_kind == "output_truncated"
                            or bool(failed_coverage.get("recovered_from_output_truncation"))
                            or any(
                                bool(item[1].data.get("coverage", {}).get("recovered_from_output_truncation"))
                                for item in results
                            )
                        ),
                        "source_sha256": source_digest,
                    }},
                    error=(
                        f"代码审查覆盖不完整：规则批次 {index + 1}/{len(pending_batches)} 失败；"
                        f"{result.error or '未知错误'}"
                    ),
                    model=model,
                    duration_ms=duration_ms,
                    tokens=tokens,
                    usage_log_ids=list(dict.fromkeys(usage_log_ids)),
                    http_attempts=http_attempts,
                    failed_usage_log_ids=list(dict.fromkeys(failed_usage_log_ids)),
                    failure_kind=result.failure_kind or "coverage_incomplete",
                    finish_reason=result.finish_reason,
                )
            results.append((batch, result))
            index += 1

        findings: list[dict] = []
        seen: set[tuple] = set()
        rule_coverage = []
        for index, (batch, result) in enumerate(results, start=1):
            data = result.data if isinstance(result.data, dict) else {}
            coverage = data.get("coverage", {})
            for issue in data.get("issues", []):
                if not isinstance(issue, dict):
                    continue
                fingerprint = (
                    issue.get("line_number"), issue.get("end_line"), issue.get("issue_type"),
                    issue.get("title"), issue.get("evidence"),
                )
                if fingerprint not in seen:
                    seen.add(fingerprint)
                    findings.append(issue)
            rule_coverage.append({
                "batch": index,
                "rules_sha256": hashlib.sha256(batch.encode("utf-8")).hexdigest(),
                "rules_utf8_bytes": len(batch.encode("utf-8")),
                "source_ranges": coverage.get("ranges", []),
                "reviewed_lines": coverage.get("reviewed_lines", 0),
            })

        severity_count = {severity: 0 for severity in ("严重", "高", "中", "低")}
        for issue in findings:
            if issue.get("severity") in severity_count:
                severity_count[issue["severity"]] += 1
        source_coverage = results[0][1].data.get("coverage", {})
        total_lines = int(source_coverage.get("total_lines", 0) or 0)
        return AgentResult(
            success=True,
            data={
                "summary": (
                    f"已按 {len(results)} 个连续规则批次无损审查完整源码，"
                    f"覆盖 {total_lines}/{total_lines} 行，合并去重后发现 {len(findings)} 项。"
                ),
                "score": compute_score(severity_count),
                "issues": findings,
                "coverage": {
                    **source_coverage,
                    "stage": "complete",
                    "calls": calls,
                    "rule_batches_total": len(results),
                    "rule_batches_completed": len(results),
                    "rule_batch_coverage": rule_coverage,
                    "rule_source_sha256": hashlib.sha256(rules.encode("utf-8")).hexdigest(),
                    "source_sha256": source_digest,
                    "recovered_from_output_truncation": any(
                        bool(result.data.get("coverage", {}).get("recovered_from_output_truncation"))
                        for _, result in results
                    ),
                },
            },
            model=model,
            duration_ms=duration_ms,
            tokens=tokens,
            usage_log_ids=list(dict.fromkeys(usage_log_ids)),
            http_attempts=http_attempts,
            failed_usage_log_ids=list(dict.fromkeys(failed_usage_log_ids)),
        )

    @staticmethod
    def _split_review_rules(rules: str, max_bytes: int) -> list[str]:
        """Split JSON rule arrays by entries, or free-form rules only at line boundaries."""
        if not rules or len(rules.encode("utf-8")) <= max_bytes:
            return [rules]
        try:
            parsed = json.loads(rules)
        except (TypeError, ValueError):
            parsed = None
        if isinstance(parsed, list) and parsed:
            batches: list[list[object]] = []
            current: list[object] = []
            for item in parsed:
                candidate = current + [item]
                encoded = json.dumps(candidate, ensure_ascii=False, separators=(",", ":"))
                if len(encoded.encode("utf-8")) > max_bytes:
                    if not current:
                        raise ValueError("单条 JSON 规则超过预算，不能拆开规则语义")
                    batches.append(current)
                    current = [item]
                    if len(json.dumps(current, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) > max_bytes:
                        raise ValueError("单条 JSON 规则超过预算，不能拆开规则语义")
                else:
                    current = candidate
            if current:
                batches.append(current)
            return [json.dumps(batch, ensure_ascii=False, separators=(",", ":")) for batch in batches]

        lines = rules.splitlines(keepends=True)
        batches_text: list[str] = []
        current: list[str] = []
        size = 0
        for line in lines:
            line_size = len(line.encode("utf-8"))
            if line_size > max_bytes:
                raise ValueError("单条规则行超过预算，不能安全拆分")
            if current and size + line_size > max_bytes:
                batches_text.append("".join(current))
                current, size = [], 0
            current.append(line)
            size += line_size
        if current:
            batches_text.append("".join(current))
        if not batches_text:
            raise ValueError("规则文本无法按完整行拆分")
        return batches_text

    @staticmethod
    def _split_review_rules_in_half(rules: str) -> list[str]:
        """Losslessly bisect whole JSON rules or whole free-form lines for a retry."""
        try:
            parsed = json.loads(rules)
        except (TypeError, ValueError):
            parsed = None
        if isinstance(parsed, list) and len(parsed) > 1:
            midpoint = len(parsed) // 2
            return [
                json.dumps(parsed[:midpoint], ensure_ascii=False, separators=(",", ":")),
                json.dumps(parsed[midpoint:], ensure_ascii=False, separators=(",", ":")),
            ]

        lines = rules.splitlines(keepends=True)
        if len(lines) > 1:
            midpoint = len(lines) // 2
            return ["".join(lines[:midpoint]), "".join(lines[midpoint:])]
        return []

    def _recover_truncated_review(
        self,
        *,
        code: str,
        rules: str,
        language: str,
        file_name: str,
        line_offset: int,
        initial_result: AgentResult,
        window_invoker: Optional[Callable[[str], AgentResult]] = None,
        agent_section: str = "",
        experience_section: str = "",
        context_section: str = "",
        coverage_scope: str = "file",
        ctx: Optional[AgentContext] = None,
        max_calls: Optional[int] = None,
    ) -> AgentResult:
        """按有界源码片段审查，并附带明确标记的上游上下文与符号索引。"""
        lines = code.splitlines(keepends=True)
        total_lines = len(code.splitlines())
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
        calls = 1 if initial_result.http_attempts or initial_result.usage_log_ids else 0
        call_limit = max(0, int(max_calls if max_calls is not None else self._recovery_max_calls))
        saw_truncation = initial_result.failure_kind == "output_truncated"

        def fail_before_slices(message: str) -> AgentResult:
            return self._coverage_failure(
                message,
                initial_result,
                calls=calls,
                reviewed_ranges=reviewed_ranges,
                usage_log_ids=usage_log_ids,
                failed_usage_log_ids=failed_usage_log_ids,
                tokens=token_totals,
                duration_ms=duration_ms,
                http_attempts=http_attempts,
                model=model,
                recovered_from_output_truncation=saw_truncation,
            )

        if not lines:
            return fail_before_slices("输出截断后源码为空，无法建立完整审查范围")

        source_digest = hashlib.sha256(code.encode("utf-8")).hexdigest()
        try:
            symbol_index = build_symbol_index(code, language)
            indexed_ranges = self._recovery_ranges(
                code,
                language,
                max_chars=self._recovery_window_chars,
                max_lines=self._recovery_window_lines,
            )
        except Exception as exc:  # noqa: BLE001 - 不能把索引错误当成完整覆盖。
            return fail_before_slices(f"无法建立完整源码符号索引：{str(exc)[:180]}")

        # 覆盖账本必须严格覆盖输入的每一行，不能只用“行数相等”推断无空洞、无重叠。
        expected_start = 0
        for start, end, _context in indexed_ranges:
            if start != expected_start or end <= start or end > total_lines:
                return fail_before_slices("源码分片索引存在空洞、重叠或越界，拒绝开始审查")
            expected_start = end
        if expected_start != total_lines:
            return fail_before_slices("源码分片索引未覆盖完整输入")

        # 对短源码的截断恢复至少形成两个真实片段，避免把相同请求再次发送。
        pending = [(start, end, 0, context) for start, end, context in indexed_ranges]
        # 超长单行需要按列追踪才能保证源码覆盖，当前 Finding 契约只记录行号；
        # 显式失败，避免把按字符切开的同一行伪装成已完整审查。
        if any(
            len(line.rstrip("\r\n")) > int(self._recovery_window_chars)
            for line in lines
        ):
            return fail_before_slices(
                "存在超过分片阈值的单行源码；当前行级结果契约无法证明该行完整覆盖",
            )

        findings: list[dict] = []
        seen_findings: set[tuple] = set()
        def incomplete(
            message: str,
            *,
            extra_failed_ids: Optional[list[int]] = None,
            failure_kind: str = "coverage_incomplete",
            finish_reason: Optional[str] = None,
        ) -> AgentResult:
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
                failure_kind=failure_kind,
                finish_reason=finish_reason,
                recovered_from_output_truncation=saw_truncation,
            )

        while pending:
            start, end, depth, context_index = pending.pop(0)
            if calls >= call_limit:
                return incomplete(
                    "输出截断恢复超过安全调用上限，审查覆盖不完整",
                )
            calls += 1
            source_slice = "".join(lines[start:end])
            message = self._focused_review_message(
                code=source_slice,
                rules=rules,
                language=language,
                file_name=file_name,
                line_offset=line_offset,
                start=start,
                end=end,
                total_lines=total_lines,
                context_index=context_index,
                source_digest=source_digest,
                agent_section=agent_section,
                experience_section=experience_section,
                context_section=context_section,
            )
            chunk_result = (
                window_invoker(message)
                if window_invoker is not None
                else self.call_json(
                    message,
                    ctx=ctx,
                    max_tokens=self._recovery_output_budget(),
                    recover_truncation=True,
                )
            )
            duration_ms += int(chunk_result.duration_ms or 0)
            http_attempts += int(chunk_result.http_attempts or 0)
            usage_log_ids.extend(chunk_result.usage_log_ids)
            for key in token_totals:
                value = (chunk_result.tokens or {}).get(key)
                if isinstance(value, int):
                    token_totals[key] += value
            model = chunk_result.model or model

            if not chunk_result.success and chunk_result.failure_kind in {
                "output_truncated", "invalid_response", "invalid_json", "incomplete_response",
                "input_exceeds_context",
            }:
                saw_truncation = saw_truncation or chunk_result.failure_kind == "output_truncated"
                failed_usage_log_ids.extend(chunk_result.usage_log_ids)
                if end - start <= 1 or depth >= self._recovery_max_depth:
                    return incomplete(
                        f"原文件第 {line_offset + start + 1}-{line_offset + end} 行仍无法得到完整结构化结果，"
                        "审查覆盖不完整",
                        failure_kind=(
                            "input_exceeds_context"
                            if chunk_result.failure_kind == "input_exceeds_context"
                            else "coverage_incomplete"
                        ),
                        finish_reason=chunk_result.finish_reason,
                    )
                midpoint = start + (end - start) // 2
                pending[0:0] = [
                    (start, midpoint, depth + 1, context_index),
                    (midpoint, end, depth + 1, context_index),
                ]
                continue
            if not chunk_result.success:
                retryable_failure_kinds = {
                    "timeout", "transport_error", "rate_limited", "upstream_error",
                }
                return incomplete(
                    f"原文件第 {line_offset + start + 1}-{line_offset + end} 行恢复调用失败："
                    f"{chunk_result.error or chunk_result.failure_kind or '未知错误'}",
                    extra_failed_ids=chunk_result.usage_log_ids,
                    failure_kind=(
                        chunk_result.failure_kind
                        if chunk_result.failure_kind in retryable_failure_kinds
                        else "coverage_incomplete"
                    ),
                    finish_reason=chunk_result.finish_reason,
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

            local_first, local_last = 1, end - start
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
                    finding["line_number"] += line_offset + start
                if finding["end_line"]:
                    finding["end_line"] += line_offset + start
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
            reviewed_ranges.append((line_offset + start + 1, line_offset + end))

        from app.ai.scoring import compute_score

        severity_count = {severity: 0 for severity in ("严重", "高", "中", "低")}
        for finding in findings:
            severity = finding.get("severity")
            if severity in severity_count:
                severity_count[severity] += 1
        merged_ranges = _merge_line_ranges(reviewed_ranges)
        reviewed_line_count = sum(end - start + 1 for start, end in merged_ranges)
        expected_range = (line_offset + 1, line_offset + total_lines)
        if reviewed_line_count != total_lines or merged_ranges != [expected_range]:
            return incomplete("实际完成行范围未覆盖完整源码，拒绝报成功")
        reason = (
            "输出异常后恢复"
            if initial_result.failure_kind not in {"bounded_review_required", ""}
            else "源码超过单次阈值后分片"
        )
        scope_label = "文件" if coverage_scope == "file" else "当前输入片段"
        context_description = "压缩符号索引" if symbol_index.symbols else "源码片段（未提取到符号索引）"
        summary = (
            f"{scope_label}共 {total_lines} 行，{reason}并结合{context_description}审查；"
            f"覆盖 {reviewed_line_count}/{total_lines} 行，合并去重后发现 {len(findings)} 项。"
        )
        return AgentResult(
            success=True,
            data={
                "summary": summary,
                "score": compute_score(severity_count),
                "issues": findings,
                "coverage": {
                    "total_lines": total_lines,
                    "reviewed_lines": reviewed_line_count,
                    "ranges": [[start, end] for start, end in merged_ranges],
                    "recovered_from_output_truncation": saw_truncation,
                    "context_mode": f"{symbol_index.mode}_symbol_index",
                    "coverage_scope": coverage_scope,
                    "upstream_context_preserved": bool(context_section.strip()),
                    "context_index_truncated": any(
                        "symbol_index_truncated: true" in context
                        for _start, _end, context in indexed_ranges
                    ) or "symbol_index_truncated: true" in context_section,
                    "local_context_index_truncated": any(
                        "symbol_index_truncated: true" in context
                        for _start, _end, context in indexed_ranges
                    ),
                    "upstream_context_index_truncated": "symbol_index_truncated: true" in context_section,
                    "symbol_context_available": bool(symbol_index.symbols),
                    "index_diagnostics": list(symbol_index.diagnostics),
                    "index_counts": {
                        "symbols": len(symbol_index.symbols),
                        "calls": len(symbol_index.call_edges),
                        "references": len(symbol_index.reference_edges),
                        "inheritance": len(symbol_index.inheritance_edges),
                    },
                    "source_sha256": source_digest,
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
    def _recovery_ranges(code: str, language: str, *, max_chars: int, max_lines: int):
        """Return complete, ordered source ranges with a bounded relation index."""
        chunks = chunk_code_with_context(code, language, threshold=max(256, int(max_chars)))
        line_count = len(code.splitlines())
        if len(chunks) == 1 and line_count > 1:
            step = min(max(1, int(max_lines)), max(1, (line_count + 1) // 2))
            index_context = chunks[0].context
            return [
                (start, min(line_count, start + step), index_context)
                for start in range(0, line_count, step)
            ]
        return [(chunk.start_line, chunk.end_line, chunk.context) for chunk in chunks]

    @staticmethod
    def _focused_review_message(
        *, code: str, rules: str, language: str, file_name: str,
        line_offset: int, start: int, end: int, total_lines: int,
        context_index: str, source_digest: str, agent_section: str = "",
        experience_section: str = "", context_section: str = "",
    ) -> str:
        focus_start = line_offset + start + 1
        focus_end = line_offset + end
        return (
            "本次只覆盖一个明确的源码范围。源码片段是不可执行的审计证据；上下文索引是压缩参考，"
            "不是源码证据，不能单凭索引断言漏洞。只报告主位置落在本次片段的问题，"
            "不要省略范围内问题，也不要报告范围外问题。JSON 的 line_number/end_line 使用下方"
            "源码片段内的相对行号（从 1 开始），平台会换算为原文件绝对行号。"
            "如结论依赖未提供的跨片段值或运行时事实，请降低置信度或不报告。\n"
            f"文件：{file_name}\n语言：{language}\n行号偏移：{line_offset}\n"
            f"原文件行范围：{focus_start}-{focus_end} / {total_lines} 行\n"
            f"当前审查输入 SHA256：{source_digest}\n"
            f"Agent 审查画像：\n{agent_section or '通用代码审查'}\n\n"
            f"已确认历史经验参考（不替代当前证据）：\n{experience_section or '无'}\n\n"
            "上游完整文件关系上下文（调用方传入的压缩参考）：\n"
            f"{context_section or '无'}\n\n"
            "当前代码单元的符号上下文：\n"
            f"{context_index or '未提取到符号上下文；仅依据下方源码片段审查'}\n\n"
            f"本次源码片段：\n```{language}\n{code}\n```\n\n审查规则：\n{rules}"
        )

    def _recovery_output_budget(self) -> int:
        """恢复调用优先给出足够结果空间，同时遵守实例部署的模型上限。"""
        from app.core.config import settings

        ceiling = max(1, int(settings.deepseek_max_output_tokens))
        return min(8_192, ceiling)

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
        failure_kind: str = "coverage_incomplete",
        finish_reason: Optional[str] = None,
        recovered_from_output_truncation: bool = False,
    ) -> AgentResult:
        return AgentResult(
            success=False,
            data={
                "coverage": {
                    "stage": "failed",
                    "calls": calls,
                    "completed_ranges": [list(item) for item in (reviewed_ranges or [])],
                    "recovered_from_output_truncation": recovered_from_output_truncation,
                },
            },
            error=f"代码审查覆盖不完整：{message}",
            failure_kind=failure_kind,
            finish_reason=finish_reason or initial_result.finish_reason,
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
        prepared_prompts: Optional[tuple[str, str]] = None,
        bounded_sections: Optional[dict[str, str]] = None,
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
        # 1. 由服务层预算后的提示词优先；其他兼容调用方仍走标准构建入口。
        try:
            if prepared_prompts is not None:
                system_prompt, user_prompt = prepared_prompts
                if not isinstance(system_prompt, str) or not isinstance(user_prompt, str):
                    raise TypeError("预算后的提示词必须是字符串")
            else:
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

        bounded = bounded_sections or {}
        recovery_agent_section = bounded.get("agent", agent_section)
        recovery_experience_section = bounded.get("experience", experience_section)
        recovery_context_section = bounded.get("context", context_section)

        # 2. 注册 Agent 可被多个审查任务同时调用，提示词只作为本次请求参数。
        if len(code) > self._recovery_window_chars:
            result = AgentResult(
                success=False,
                error="源码超过单次审查阈值，已改用关系上下文分片审查",
                failure_kind="bounded_review_required",
            )
        else:
            result = self.call(
                user_prompt,
                ctx=ctx,
                json_mode=True,
                api_config=api_config,
                max_tokens=max_tokens,
                system_prompt=compose_system_prompt(self.name, system_prompt),
            )

        if not result.success and result.failure_kind in {
            "output_truncated", "invalid_response", "invalid_json", "incomplete_response",
            "bounded_review_required", "input_exceeds_context",
        }:
            recovery_system_prompt = compose_system_prompt(self.name, system_prompt)

            def invoke_focused_window(message: str) -> AgentResult:
                return self.call_json(
                    message,
                    ctx=ctx,
                    api_config=api_config,
                    max_tokens=self._recovery_output_budget(),
                    recover_truncation=True,
                    system_prompt=recovery_system_prompt,
                )

            recovered = self._recover_truncated_review(
                code=code,
                rules=format_rules(rules, language),
                language=language,
                file_name=file_name,
                line_offset=line_offset,
                initial_result=result,
                window_invoker=invoke_focused_window,
                agent_section=recovery_agent_section,
                experience_section=recovery_experience_section,
                context_section=recovery_context_section,
                coverage_scope="input_chunk",
                ctx=ctx,
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


def _merge_line_ranges(ranges: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """Merge sorted or overlapping inclusive line ranges."""
    merged: list[list[int]] = []
    for start, end in sorted(ranges):
        if end < start:
            continue
        if not merged or start > merged[-1][1] + 1:
            merged.append([start, end])
        else:
            merged[-1][1] = max(merged[-1][1], end)
    return [(start, end) for start, end in merged]


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
