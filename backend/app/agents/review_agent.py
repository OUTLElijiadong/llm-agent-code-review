"""代码审查智能体 CodeReviewerAgent

v2 改造(2026-06-25):
- 新增 execute_review() 方法,通过 BaseAgent.call() 调用 LLM,统一事件总线/调用日志/AiCallLog 归因
- 返回标准化 AgentResult,data["issues"] 为 List[Finding](与 static_analyzer.Finding 同结构)
- 移除旧的 execute() 方法(已被 review_service 直接调用 DeepSeekAgent.chat() 取代,现在反向激活 Agent)
"""
from typing import Optional

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
        """旧版执行方法(保留向后兼容,新流程请用 execute_review)

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
        return self.call_json(user_msg)

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

        # 2. 临时覆盖 system_prompt(BaseAgent.call 用 self._system_prompt)
        #    review_service 后台线程顺序调用,无并发安全问题
        original_system = self._system_prompt
        self._system_prompt = compose_system_prompt(self.name, system_prompt)
        try:
            result = self.call(user_prompt, ctx=ctx, json_mode=True, api_config=api_config)
        finally:
            self._system_prompt = original_system

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
            )

        # 4. 转换 Issue → Finding(统一数据结构,便于 review_service 合并去重)
        findings = [_issue_to_finding(it, line_offset=line_offset) for it in review_result.issues]

        return AgentResult(
            success=True,
            data={
                "issues": findings,
                "summary": review_result.summary,
                "score": review_result.score,
            },
            model=result.model,
            duration_ms=result.duration_ms,
            tokens=result.tokens,
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
