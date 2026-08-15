from typing import List

from app.agents.base import AgentResult, BaseAgent
from app.agents.contracts import compose_system_prompt


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
        file_list = file_names[:30]
        file_list_str = "\n".join(f"- {f}" for f in file_list)
        user_msg = (
            f"文件夹名称: {folder_name or '(未命名)'}\n"
            f"包含的文件:\n{file_list_str or '(无)'}"
        )
        if strategy_instruction.strip():
            user_msg += f"\n\n上一次失败后的改道策略:\n{strategy_instruction.strip()}"
        result = self.call_json(user_msg)
        if not result.success:
            return result
        data = result.data
        valid = {"python", "javascript", "typescript", "java", "go",
                 "cpp", "vue", "html", "css", "php", "c", "sql", "plaintext"}
        lang = data.get("language", "plaintext")
        if lang not in valid:
            lang = "plaintext"
        result.data = {
            "project_name": data.get("project_name", folder_name or "未命名项目")[:50],
            "description": data.get("description", "")[:200],
            "language": lang,
            "language_name": data.get("language_name", lang.capitalize()),
        }
        return result
