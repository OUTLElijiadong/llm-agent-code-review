from app.agents.base import AgentResult, BaseAgent
from app.agents.contracts import compose_system_prompt


class LanguageDetectorAgent(BaseAgent):
    """语言检测智能体

    根据项目名称和描述推断主要编程语言。
    """

    name = "language_detector"
    description = "根据项目名称和描述智能识别编程语言"
    icon = "language_detector"
    color = "#4B9BFF"
    category = "analyzer"
    skills = ("语言识别", "项目元数据补全")

    def __init__(self):
        system_prompt = (
            "你是一个编程语言识别专家。根据用户提供的项目名称和项目描述,"
            "判断该项目主要使用的编程语言。\n\n"
            "支持的编程语言值(python/javascript/typescript/java/go/cpp/vue/html/css/php/c/sql/plaintext)之一。\n\n"
            "输出格式: JSON对象, 包含字段:\n"
            '- language: 英文语言标识, 如 "python"\n'
            '- language_name: 中文名称, 如 "Python"\n'
            '- confidence: 置信度 high/medium/low\n'
            '- reason: 简短说明判断理由\n\n'
            "严格JSON格式输出,不包含其他内容。"
        )
        super().__init__(
            system_prompt=compose_system_prompt(self.name, system_prompt),
            temperature=0.1,
            max_tokens=200,
        )

    def _init_skills(self) -> None:
        """子类 override:挂载 LanguageDetectorSelfImprovementSkill + LanguageDetectorProactiveSkill

        将语言检测 Agent 的自进化与主动监测能力下沉到 Skill,通过 SkillRegistry
        统一注册,供 Orchestrator.invoke_skill / ChatPlanner 查询调用。
        """
        from app.agents.skills.language_detector import (
            LanguageDetectorProactiveSkill,
            LanguageDetectorSelfImprovementSkill,
        )

        self.attach_skill(LanguageDetectorSelfImprovementSkill(self.name))
        self.attach_skill(LanguageDetectorProactiveSkill(self.name))

    def execute(self, project_name: str, description: str = "") -> AgentResult:
        user_msg = f"项目名称: {project_name}\n项目描述: {description or '无'}"
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
            "language": lang,
            "language_name": data.get("language_name", lang.capitalize()),
            "confidence": data.get("confidence", "high"),
            "reason": data.get("reason", ""),
        }
        return result
