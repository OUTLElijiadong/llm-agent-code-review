import json
from typing import List

from app.agents.base import AgentResult, BaseAgent
from app.agents.contracts import compose_system_prompt
from app.core.config import settings

_MAX_SOURCE_CHUNKS = 64
_PARTIAL_SUMMARY_PROMPT = (
    "你是软件项目文件清单压缩员。当前只看到项目文件清单的一个有编号分片，"
    "不得推断未提供分片的内容。输出 JSON，字段仅有 project_name、description、"
    "language、language_name。这里的 description 是本分片的来源摘要，"
    "应保留目录角色、入口/配置/测试文件、语言线索及不确定性，最多 600 字。"
)
_PARTIAL_SUMMARY_SYSTEM = compose_system_prompt("project_analyzer", _PARTIAL_SUMMARY_PROMPT)


class ProjectAnalyzerAgent(BaseAgent):
    """项目分析智能体

    根据文件夹名称和文件列表,推断项目名称、描述和主语言。
    """

    name = "project_analyzer"
    description = "项目体检员:看一眼文件结构就能判断这是什么项目、用什么技术、风险高不高"
    icon = "project_analyzer"
    color = "#5BB89A"
    category = "analyzer"
    skills = ("项目分析", "目录结构识别", "文件分类")

    def __init__(self):
        system_prompt = (
            "你是一个专业的软件项目分析专家。请根据提供的文件夹名称和文件列表,"
            "分析这个项目并生成合适的元数据。\n\n"
            "要求:\n"
            "1. project_name: 根据文件夹名和文件内容推断,生成一个简洁有意义的中文项目名(2-15字)\n"
            "2. description: 简要描述项目功能和用途(15-60字)\n"
            "3. language: 根据文件扩展名判断主要编程语言标识\n\n"
            "支持的语言标识: python/javascript/typescript/java/go/cpp/vue/html/css/php/c/sql/plaintext\n\n"
            "输出格式: 严格JSON对象, 包含 project_name, description, language, language_name 四个字段。"
        )
        super().__init__(
            system_prompt=compose_system_prompt(self.name, system_prompt),
            temperature=0.3,
            # 思维链模型的 reasoning 也计入该上限;500 时 JSON 还没开始输出就
            # finish_reason=length(实测生产事故)。2000 给推理留余量。
            max_tokens=2000,
        )

    def _init_skills(self) -> None:
        """子类 override:挂载 ProjectAnalyzerSelfImprovementSkill + ProjectAnalyzerProactiveSkill

        将项目分析 Agent 的自进化与主动监测能力下沉到 Skill,通过 SkillRegistry
        统一注册,供 Orchestrator.invoke_skill / ChatPlanner 查询调用。
        """
        from app.agents.skills.project_analyzer import (
            ProjectAnalyzerProactiveSkill,
            ProjectAnalyzerSelfImprovementSkill,
        )

        self.attach_skill(ProjectAnalyzerSelfImprovementSkill(self.name))
        self.attach_skill(ProjectAnalyzerProactiveSkill(self.name))

    def execute(
        self,
        folder_name: str,
        file_names: List[str],
        strategy_instruction: str = "",
    ) -> AgentResult:
        chunks = self._file_chunks(file_names)
        if chunks is None:
            return AgentResult(
                success=False,
                error="文件清单单项或分片数超过上下文压缩预算，未丢弃文件名后继续分析",
                failure_kind="input_exceeds_context",
            )
        if len(chunks) > _MAX_SOURCE_CHUNKS:
            return AgentResult(
                success=False,
                error="文件清单超出项目分析分片调用预算，未抽样或遗漏后继续分析",
                failure_kind="semantic_budget_exhausted",
            )
        user_msg = self._file_prompt(folder_name, chunks[0])
        if strategy_instruction.strip():
            user_msg += f"\n\n上一次失败后的改道策略:\n{strategy_instruction.strip()}"
        if len(chunks) == 1:
            result = self.call_json(user_msg)
        else:
            partials: list[dict] = []
            for index, chunk in enumerate(chunks, start=1):
                prompt = self._file_prompt(folder_name, chunk)
                prompt += f"\n\n来源分片: {index}/{len(chunks)}；本片 {len(chunk)} 个文件。"
                if strategy_instruction.strip():
                    prompt += f"\n改道策略: {strategy_instruction.strip()}"
                partial = self.call_json(prompt, system_prompt=_PARTIAL_SUMMARY_SYSTEM)
                if not partial.success:
                    return partial
                if not isinstance(partial.data, dict) or not self._valid_source_summary(partial.data):
                    return AgentResult(
                        success=False,
                        error=f"项目文件分片 {index}/{len(chunks)} 缺少可追溯来源摘要",
                        failure_kind="invalid_summary",
                    )
                partials.append({
                    "source_part": index,
                    "source_files": len(chunk),
                    "project_name": partial.data.get("project_name", ""),
                    "language": partial.data.get("language", ""),
                    "source_summary": partial.data["description"],
                })
            result = self._synthesize_partials(folder_name, partials)
        if not result.success:
            return result
        data = result.data
        if not isinstance(data, dict):
            return AgentResult(success=False, error="项目分析结果不是 JSON 对象", failure_kind="invalid_json")
        project_name = data.get("project_name") or folder_name or "未命名项目"
        description = data.get("description") or ""
        if not isinstance(project_name, str) or not isinstance(description, str):
            return AgentResult(success=False, error="项目名称或描述类型无效", failure_kind="invalid_output_contract")
        if len(project_name) > 50 or len(description) > 200:
            return AgentResult(
                success=False,
                error="模型生成的项目名称或描述超过字段约束，未截断后当作完整结果",
                failure_kind="invalid_output_contract",
            )
        valid = {"python", "javascript", "typescript", "java", "go",
                 "cpp", "vue", "html", "css", "php", "c", "sql", "plaintext"}
        lang = data.get("language", "plaintext")
        if lang not in valid:
            lang = "plaintext"
        result.data = {
            "project_name": project_name,
            "description": description,
            "language": lang,
            "language_name": data.get("language_name", lang.capitalize()),
            "coverage": {
                "total_files": len(file_names),
                "processed_files": sum(len(chunk) for chunk in chunks),
                "source_chunks": len(chunks),
            },
        }
        return result

    def _file_chunk_chars(self) -> int:
        """使用模型输入预算决定分片大小，保留系统指令和输出空间。"""
        window = int(getattr(settings, "deepseek_context_window_tokens", 1_000_000) or 1_000_000)
        capacity = max(0, (window - max(8_192, self._max_tokens)) * 2 - 4_096)
        return min(20_000, capacity)

    @staticmethod
    def _valid_source_summary(data: dict) -> bool:
        summary = data.get("description")
        return isinstance(summary, str) and bool(summary.strip()) and len(summary) <= 600

    def _file_chunks(self, file_names: List[str]) -> list[list[str]] | None:
        limit = self._file_chunk_chars()
        if limit <= 0:
            return None
        chunks: list[list[str]] = []
        current: list[str] = []
        current_chars = 0
        for name in file_names:
            size = len(name) + 3
            if size > limit:
                return None
            if current and current_chars + size > limit:
                chunks.append(current)
                current = []
                current_chars = 0
            current.append(name)
            current_chars += size
        chunks.append(current)
        return chunks

    @staticmethod
    def _file_prompt(folder_name: str, files: list[str]) -> str:
        return (
            f"文件夹名称: {folder_name or '(未命名)'}\n"
            "包含的文件:\n" + ("\n".join(f"- {name}" for name in files) or "(无)")
        )

    def _synthesize_partials(self, folder_name: str, partials: list[dict]) -> AgentResult:
        """按来源编号逐层汇总所有分片，失败时不得交付局部结果。"""
        current = partials
        rounds = 0
        while len(current) > 1:
            rounds += 1
            if rounds > 8:
                return AgentResult(
                    success=False, error="项目摘要压缩层数超限", failure_kind="semantic_budget_exhausted",
                )
            groups: list[list[dict]] = []
            group: list[dict] = []
            used = 0
            for item in current:
                item_chars = len(json.dumps(item, ensure_ascii=False)) + 2
                if item_chars > self._file_chunk_chars():
                    return AgentResult(
                        success=False, error="单个项目来源摘要过长", failure_kind="input_exceeds_context",
                    )
                if group and used + item_chars > self._file_chunk_chars():
                    groups.append(group)
                    group = []
                    used = 0
                group.append(item)
                used += item_chars
            if group:
                groups.append(group)
            if len(groups) == 1:
                prompt = (
                    f"文件夹名称: {folder_name or '(未命名)'}\n"
                    "以下摘要按来源分片编号覆盖了完整文件清单，请合并为项目元数据，"
                    "不得遗漏任何分片或假称看过未列出的源码。\n"
                    + json.dumps(groups[0], ensure_ascii=False)
                )
                return self.call_json(prompt)
            next_level: list[dict] = []
            for group in groups:
                prompt = "压缩以下有来源编号的项目文件摘要，保留各分片线索与不确定性:\n"
                prompt += json.dumps(group, ensure_ascii=False)
                summary = self.call_json(prompt, system_prompt=_PARTIAL_SUMMARY_SYSTEM)
                if not summary.success:
                    return summary
                if not isinstance(summary.data, dict) or not self._valid_source_summary(summary.data):
                    return AgentResult(success=False, error="层级来源摘要缺失", failure_kind="invalid_summary")
                next_level.append({
                    "source_parts": [item.get("source_part", item.get("source_parts")) for item in group],
                    "source_files": sum(int(item.get("source_files") or 0) for item in group),
                    "project_name": summary.data.get("project_name", ""),
                    "language": summary.data.get("language", ""),
                    "source_summary": summary.data["description"],
                })
            if len(next_level) >= len(current):
                return AgentResult(success=False, error="项目摘要未能收敛", failure_kind="semantic_budget_exhausted")
            current = next_level
        return AgentResult(success=False, error="缺少可合并项目摘要", failure_kind="invalid_summary")
