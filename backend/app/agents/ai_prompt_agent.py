"""
AI 提示词生成 Agent — v2.0 新成员

把棱镜审查检出的问题翻译成可直接粘贴给 Cursor / Copilot / ChatGPT / Claude Code
等外部 AI 工具的高质量提示词,告诉外部 AI:在哪个文件、哪一行、修复什么、怎么修复。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List, Optional, Tuple

from loguru import logger
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agents.base import AgentContext, AgentResult, BaseAgent
from app.agents.contracts import compose_system_prompt
from app.models.code_file import CodeFile
from app.models.project import Project
from app.models.review_issue import ReviewIssue
from app.models.review_task import ReviewTask
from app.models.user import User
from app.utils.encoding_utils import BASE64_PREFIX

SUPPORTED_TOOLS: Tuple[str, ...] = (
    "generic", "cursor", "copilot", "chatgpt", "claude_code",
)
MAX_CONTEXT_CHARS = 4000
BINARY_LANGUAGES = {
    "binary", "unknown", "image", "jpeg", "jpg", "png", "gif", "svg",
}

TOOL_LABELS = {
    "generic": "通用 AI",
    "cursor": "Cursor",
    "copilot": "GitHub Copilot Chat",
    "chatgpt": "ChatGPT",
    "claude_code": "Claude Code",
}

# 敏感字段脱敏正则
SENSITIVE_RE = re.compile(
    r"""(?ix)
    ( (?:api[_-]?key|secret|password|passwd|token|access[_-]?key) \s* [:=] \s* )
    (["']?)([^"'\s,;}]{4,})\2
    """,
)


def _redact(text: str) -> str:
    """简单脱敏:把常见敏感字段值替换为 <REDACTED>"""
    return SENSITIVE_RE.sub(r"\1\2<REDACTED>\2", text)


def _format_lines(start: Optional[int], end: Optional[int]) -> str:
    if not start:
        return "文件级"
    if end and end != start:
        return f"L{start}-L{end}"
    return f"L{start}"


def _tool_footer(target_tool: str, file_path: str, start: int, end: int) -> str:
    if target_tool == "cursor":
        return (
            f"\n> 提示:在 Cursor 中按 ⌘K 唤起内联编辑,"
            f"选中第 {start}-{end} 行后粘贴上面的指令。"
        )
    if target_tool == "claude_code":
        return (
            f"\n> 提示:在 Claude Code 中可直接说 "
            f'"请阅读 {file_path}:{start} 附近代码,按上面的描述修复"。'
        )
    if target_tool == "copilot":
        return "\n> 提示:在 Copilot Chat 中粘贴后,可补一句 `/fix` 触发修复建议。"
    if target_tool == "chatgpt":
        return "\n> 提示:在 ChatGPT 中可上传相关文件以获得更精准的修复方案。"
    return ""


@dataclass
class _IssueContext:
    issue: ReviewIssue
    file: Optional[CodeFile]
    snippet: str
    start_line: int
    end_line: int


class AiPromptAgent(BaseAgent):
    """AI 提示词生成 Agent

    根据审查问题生成可粘贴的外部 AI 修复提示词。
    支持两种模式:
    - template_only=True 时仅做模板渲染,不调用 LLM,零延迟、零 token。
    - 默认 LLM 模式会让 DeepSeek 把模板渲染结果再"润色"为更口语化、更适配
      目标工具的提示词。
    """

    name = "ai_prompt"
    description = "修复提示生成器:把审查发现一键翻译成可粘贴给 AI 编程工具的修复指令"
    icon = "ai_prompt"
    color = "#E25C73"
    category = "output"
    skills = (
        "提示词生成", "上下文片段抽取", "敏感信息脱敏", "多 AI 工具适配",
    )

    def __init__(self):
        system_prompt = (
            "你是 PRISM 棱镜平台的提示词专家。"
            "你的工作:把已经渲染好的模板再润色一遍,让其他 AI 工具拿到后能更准确地修复代码。\n"
            "原则:\n"
            "1. 保留全部技术细节:文件路径、行号、严重度、问题描述、修复建议、代码上下文\n"
            "2. 必须出现 '文件:' '行号:' '问题:' '修复要求:' 这四个字段\n"
            "3. 不要新增'我'之类的口语化前缀,直接给出指令\n"
            "4. 末尾必须保留模板自带的'提示:'小段(如有)\n"
            "5. 输出纯文本,不要带 Markdown 代码块包裹整个提示词"
        )
        super().__init__(
            system_prompt=compose_system_prompt(self.name, system_prompt),
            temperature=0.3,
            max_tokens=1200,
        )
        self._db: Optional[Session] = None
        self._user: Optional[User] = None

    def _init_skills(self) -> None:
        """子类 override:挂载 AiPromptSelfImprovementSkill + AiPromptProactiveSkill

        将 AI 提示词生成 Agent 的自进化与主动监测能力下沉到 Skill,通过 SkillRegistry
        统一注册,供 Orchestrator.invoke_skill / ChatPlanner 查询调用。
        """
        from app.agents.skills.ai_prompt import (
            AiPromptProactiveSkill,
            AiPromptSelfImprovementSkill,
        )

        self.attach_skill(AiPromptSelfImprovementSkill(self.name))
        self.attach_skill(AiPromptProactiveSkill(self.name))

    def inject(self, db: Session, user: Optional[User] = None) -> None:
        self._db = db
        self._user = user

    def _ensure_db(self) -> Optional[AgentResult]:
        if self._db is None:
            return AgentResult(success=False, error="DB 未注入")
        return None

    def _authz_issue(self, issue: ReviewIssue) -> Optional[AgentResult]:
        """普通用户只能给自己任务下的问题生成提示词"""
        if self._user is None or self._user.role in {"admin", "super_admin"}:
            return None
        task = self._db.get(ReviewTask, issue.task_id)
        if task is None or task.user_id != self._user.id:
            return AgentResult(success=False, error="无权访问该问题")
        return None

    def _extract_context(
        self, file_id: Optional[int], line: Optional[int], padding: int = 5,
    ) -> Tuple[str, int, int, Optional[CodeFile]]:
        """从代码文件中抽取问题前后 padding 行作为上下文"""
        if not file_id:
            return "", 0, 0, None
        file = self._db.get(CodeFile, file_id)
        if not file or not file.content:
            return "", 0, 0, file
        if (
            file.content.startswith(BASE64_PREFIX)
            or (file.language or "").lower() in BINARY_LANGUAGES
        ):
            return "", 0, 0, file
        lines = file.content.splitlines()
        total = len(lines)
        anchor = max(1, min(line or 1, total))
        start = max(1, anchor - padding)
        end = min(total, anchor + padding)
        snippet_lines = []
        for i in range(start - 1, end):
            snippet_lines.append(f"{i + 1:>4}| {lines[i]}")
        snippet = _redact("\n".join(snippet_lines))
        if len(snippet) > MAX_CONTEXT_CHARS:
            snippet = f"{snippet[:MAX_CONTEXT_CHARS]}\n... [context truncated]"
        return snippet, start, end, file

    def _render_template(self, target_tool: str, issue: ReviewIssue,
                         file_path: str, language: str, snippet: str,
                         start: int, end: int) -> str:
        """渲染本地模板提示词(LLM 调用前的基础版本)"""
        sev_label = {"严重": "严重", "高": "高", "中": "中", "低": "低"}.get(
            issue.severity, issue.severity)
        body = (
            f"任务: 帮我修复一段代码,这是棱镜 Prism 平台 AI 审查检出的问题。\n\n"
            f"文件: {file_path}\n"
            f"行号: {_format_lines(start, end)}\n"
            f"语言: {language or '未知'}\n"
            f"严重度: {sev_label}\n"
            f"问题类型: {issue.issue_type}\n\n"
            f"问题描述:\n{issue.description}\n\n"
        )
        if issue.suggestion:
            body += f"棱镜给出的修复建议:\n{issue.suggestion}\n\n"
        if snippet:
            body += f"上下文代码:\n```{language or ''}\n{snippet}\n```\n\n"
        body += (
            "修复要求:\n"
            "1. 给出完整修复后的代码片段\n"
            "2. 简述修复思路与潜在副作用\n"
            "3. 如果有更优实现,也请一并提出\n"
        )
        body += _tool_footer(target_tool, file_path, start, end)
        return body

    def _polish_with_llm(self, draft: str, target_tool: str) -> Tuple[str, AgentResult]:
        """让 LLM 把模板润色一次。失败时回退使用模板原文。"""
        tool_label = TOOL_LABELS.get(target_tool, target_tool)
        user_msg = (
            f"目标 AI 工具: {tool_label}\n\n"
            f"以下是已经渲染好的提示词草稿,请按系统指令润色后输出最终版本:\n\n"
            f"```\n{draft}\n```"
        )
        result = self.call(user_msg)
        if result.success and isinstance(result.data, str) and result.data.strip():
            return result.data.strip(), result
        logger.warning(
            f"[AiPromptAgent] LLM 润色失败,使用模板原文: {result.error}",
        )
        return draft, result

    def _build_for_issue(self, issue: ReviewIssue, target_tool: str,
                         use_llm: bool) -> dict:
        snippet, start, end, file = self._extract_context(
            issue.file_id, issue.line_number, padding=5)
        file_path = file.file_path or file.file_name if file else (
            issue.file_name or "(unknown_file)")
        language = file.language if file else ""
        draft = self._render_template(
            target_tool, issue, file_path, language, snippet, start, end)
        prompt_text = draft
        tokens: dict = {}
        if use_llm:
            polished, raw = self._polish_with_llm(draft, target_tool)
            prompt_text = polished
            tokens = raw.tokens
        sev_label = issue.severity
        title = (
            f"[{sev_label}] {file_path}:{start} {issue.issue_type}"
            if start else f"[{sev_label}] {file_path} {issue.issue_type}"
        )
        return {
            "issue_id": issue.id,
            "title": title,
            "target_tool": target_tool,
            "target_label": TOOL_LABELS.get(target_tool, target_tool),
            "file_path": file_path,
            "lines": _format_lines(start, end),
            "language": language,
            "severity": issue.severity,
            "issue_type": issue.issue_type,
            "prompt_text": prompt_text,
            "code_context": snippet,
            "tokens": tokens,
        }

    def _build_aggregate(self, issues: List[ReviewIssue], target_tool: str,
                         scope_label: str, agg_id: int) -> dict:
        """把一个审查任务下的全部问题合成为单条「一键修复全部」提示词。

        与逐条提示词互补:逐条用于精修单点,聚合用于让外部 AI 一次性系统修复整批问题。
        为控制体量、保证稳定,聚合提示词只做模板渲染,不走 LLM 润色。
        """
        sev_rank = {"严重": 0, "高": 1, "中": 2, "低": 3}
        # 按严重度排序,严重的排前面
        ordered = sorted(issues, key=lambda i: (sev_rank.get(i.severity, 9),
                                                 i.line_number or 0))
        by_file: dict[str, list[tuple]] = {}
        file_lang: dict[str, str] = {}
        for iss in ordered:
            snippet, _s, _e, file = self._extract_context(
                iss.file_id, iss.line_number, padding=3)
            fp = (file.file_path or file.file_name) if file else (
                iss.file_name or "(unknown_file)")
            if file and fp not in file_lang:
                file_lang[fp] = file.language or ""
            by_file.setdefault(fp, []).append((iss, snippet))

        total = len(ordered)
        sev_count = {"严重": 0, "高": 0, "中": 0, "低": 0}
        for iss in ordered:
            sev_count[iss.severity] = sev_count.get(iss.severity, 0) + 1
        # 问题较多时省略代码上下文,避免提示词过长
        include_snippet = total <= 15

        parts = [
            f"任务: 这是棱镜 Prism 平台 AI 审查在「{scope_label}」中检出的全部 "
            f"{total} 个问题,请你一次性、系统地修复它们。\n\n",
            f"问题分布: 严重 {sev_count['严重']} · 高 {sev_count['高']} · "
            f"中 {sev_count['中']} · 低 {sev_count['低']}\n\n",
            "请按文件逐个修复以下问题(已按严重度排序,请优先处理靠前的):\n",
        ]
        idx = 0
        for fp, items in by_file.items():
            lang = file_lang.get(fp, "")
            parts.append(f"\n### 文件: {fp}" + (f" ({lang})" if lang else "") + "\n")
            for iss, snippet in items:
                idx += 1
                line_str = _format_lines(iss.line_number, iss.end_line)
                parts.append(
                    f"\n{idx}. [{iss.severity}·{iss.issue_type}] "
                    f"{iss.title or '问题'} @ {line_str}\n"
                    f"   问题: {iss.description}\n"
                )
                if iss.suggestion:
                    parts.append(f"   修复建议: {iss.suggestion}\n")
                if include_snippet and snippet:
                    parts.append(f"   上下文:\n```{lang}\n{snippet}\n```\n")
        parts.append(
            "\n修复要求:\n"
            "1. 逐条修复上面列出的所有问题,不要遗漏;\n"
            "2. 按文件给出修复后的完整代码或最小化补丁(diff),并标注对应问题编号;\n"
            "3. 若多个问题相互关联,可合并修复并说明原因;\n"
            "4. 简述每处修复的思路与潜在副作用。\n"
        )
        body = "".join(parts)
        first_file = next(iter(by_file), "")
        agg_footer = {
            "cursor": "\n> 提示:在 Cursor 中可逐个打开上述文件,用 ⌘K / Composer "
                      "按本清单逐项修复。",
            "claude_code": "\n> 提示:在 Claude Code 中可直接说"
                           "「按上面的问题清单逐个修复这些文件」。",
            "copilot": "\n> 提示:在 Copilot Chat 中粘贴后可对每个文件补一句 `/fix`。",
            "chatgpt": "\n> 提示:在 ChatGPT 中可上传相关文件以获得更精准的整批修复方案。",
        }.get(target_tool, "")
        body += agg_footer

        top_sev = ordered[0].severity if ordered else "中"
        file_count = len(by_file)
        return {
            "issue_id": agg_id,          # 负数,避免与真实 issue_id 及其它聚合条目冲突
            "kind": "aggregate",
            "title": f"🛠 一键修复全部 · {scope_label}({total} 个问题)",
            "target_tool": target_tool,
            "target_label": TOOL_LABELS.get(target_tool, target_tool),
            "file_path": f"{file_count} 个文件" if file_count != 1 else first_file,
            "lines": f"{total} 个问题",
            "language": "",
            "severity": top_sev,
            "issue_type": "汇总修复",
            "prompt_text": body,
            "code_context": "",
            "tokens": {},
        }

    def execute_for_issue(self, issue_id: int, target_tool: str = "generic",
                          use_llm: bool = True,
                          ctx: Optional[AgentContext] = None) -> AgentResult:
        if (err := self._ensure_db()) is not None:
            return err
        if target_tool not in SUPPORTED_TOOLS:
            return AgentResult(success=False, error=f"不支持的目标工具: {target_tool}")
        issue = self._db.get(ReviewIssue, issue_id)
        if issue is None:
            return AgentResult(success=False, error="问题不存在")
        if (err := self._authz_issue(issue)) is not None:
            return err
        prompt = self._build_for_issue(issue, target_tool, use_llm)
        return AgentResult(
            success=True,
            data={
                "prompts": [prompt],
                "aggregates": [],
                "summary": f"生成了 1 条针对 {TOOL_LABELS.get(target_tool, target_tool)} 的提示词。",
            },
            model=self._model,
            tokens=prompt["tokens"],
        )

    def execute_for_task(self, task_id: int, target_tool: str = "generic",
                         severity_filter: Optional[List[str]] = None,
                         use_llm: bool = True,
                         ctx: Optional[AgentContext] = None) -> AgentResult:
        if (err := self._ensure_db()) is not None:
            return err
        if target_tool not in SUPPORTED_TOOLS:
            return AgentResult(success=False, error=f"不支持的目标工具: {target_tool}")
        task = self._db.get(ReviewTask, task_id)
        if task is None:
            return AgentResult(success=False, error="审查任务不存在")
        if self._user and self._user.role not in {"admin", "super_admin"} and task.user_id != self._user.id:
            return AgentResult(success=False, error="无权访问该任务")
        q = self._db.query(ReviewIssue).filter(ReviewIssue.task_id == task_id)
        if severity_filter:
            q = q.filter(ReviewIssue.severity.in_(severity_filter))
        issues = q.order_by(
            ReviewIssue.severity.asc(), ReviewIssue.line_number.asc()).limit(50).all()
        if not issues:
            return AgentResult(
                success=False,
                error="该任务下没有匹配的问题,无法生成提示词",
            )
        prompts = [self._build_for_issue(i, target_tool, use_llm) for i in issues]
        # 一键修复全部:把整批问题合成一条完整提示词(≥2 个问题时才有意义)
        aggregates: list[dict] = []
        if len(issues) >= 2:
            aggregates.append(self._build_aggregate(
                issues, target_tool,
                scope_label=task.task_name or f"审查任务 #{task_id}",
                agg_id=-task_id,
            ))
        agg_hint = f"、1 条一键修复全部({len(issues)} 个问题)" if aggregates else ""
        return AgentResult(
            success=True,
            data={
                "prompts": prompts,
                "aggregates": aggregates,
                "summary": (
                    f"任务 #{task_id} 共生成 {len(prompts)} 条逐条修复提示词{agg_hint},"
                    f"目标工具 {TOOL_LABELS.get(target_tool, target_tool)}。"
                ),
            },
            model=self._model,
        )

    def execute_for_project(self, project_id: int, target_tool: str = "generic",
                            top_n: int = 30, use_llm: bool = True,
                            ctx: Optional[AgentContext] = None) -> AgentResult:
        """项目级生成: 按严重度优先取前 top_n 条问题打包"""
        if (err := self._ensure_db()) is not None:
            return err
        if target_tool not in SUPPORTED_TOOLS:
            return AgentResult(success=False, error=f"不支持的目标工具: {target_tool}")
        project = self._db.get(Project, project_id)
        if project is None:
            return AgentResult(success=False, error="项目不存在")
        if self._user and self._user.role not in {"admin", "super_admin"} and project.user_id != self._user.id:
            return AgentResult(success=False, error="无权访问该项目")
        # 严重度排序: 严重 > 高 > 中 > 低
        severity_order = ["严重", "高", "中", "低"]
        task_ids_query = select(ReviewTask.id).where(
            ReviewTask.project_id == project_id)
        all_issues: List[ReviewIssue] = []
        for sev in severity_order:
            if len(all_issues) >= top_n:
                break
            remaining = top_n - len(all_issues)
            chunk = (
                self._db.query(ReviewIssue)
                .filter(
                    ReviewIssue.task_id.in_(task_ids_query),
                    ReviewIssue.severity == sev,
                    ReviewIssue.status == "unfixed",
                )
                .order_by(ReviewIssue.create_time.desc())
                .limit(remaining)
                .all()
            )
            all_issues.extend(chunk)
        if not all_issues:
            return AgentResult(
                success=False,
                error="该项目下没有待修复的问题,无法生成提示词",
            )
        prompts = [self._build_for_issue(i, target_tool, use_llm) for i in all_issues]
        # 一键修复全部:项目跨多个审查任务,按「审查任务」分组各生成一条完整提示词
        by_task: dict[int, list[ReviewIssue]] = {}
        for iss in all_issues:
            by_task.setdefault(iss.task_id, []).append(iss)
        aggregates: list[dict] = []
        for tid, group in by_task.items():
            if len(group) < 2:
                continue
            task = self._db.get(ReviewTask, tid)
            label = (task.task_name if task else None) or f"审查任务 #{tid}"
            aggregates.append(self._build_aggregate(
                group, target_tool, scope_label=label, agg_id=-tid))
        agg_hint = (f"、{len(aggregates)} 条分任务一键修复全部"
                    if aggregates else "")
        return AgentResult(
            success=True,
            data={
                "prompts": prompts,
                "aggregates": aggregates,
                "summary": (
                    f"项目「{project.project_name}」共生成 {len(prompts)} 条逐条修复提示词"
                    f"{agg_hint}(按严重度优先),"
                    f"目标工具 {TOOL_LABELS.get(target_tool, target_tool)}。"
                ),
            },
            model=self._model,
        )

    def list_supported_tools(self) -> List[dict]:
        return [{"value": k, "label": v} for k, v in TOOL_LABELS.items()]
