from app.agents.base import AgentResult, BaseAgent


class CodeReviewerAgent(BaseAgent):
    """代码审查智能体

    对代码片段执行审查,返回结构化的问题列表。
    实际的审查逻辑由 review_service + multi_agent 编排完成,
    本 Agent 提供单次审查调用的封装。
    """

    name = "code_reviewer"
    description = "对代码片段执行智能审查,检测 Bug、安全、性能等问题"
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
        super().__init__(system_prompt=system_prompt, temperature=0.2, max_tokens=4096)

    def execute(self, code: str, rules: str, language: str,
                file_name: str = "", line_offset: int = 0) -> AgentResult:
        user_msg = (
            f"文件: {file_name}\n"
            f"语言: {language}\n"
            f"行号偏移: {line_offset}\n\n"
            f"审查规则:\n{rules}\n\n"
            f"代码:\n```{language}\n{code}\n```"
        )
        return self.call_json(user_msg)
