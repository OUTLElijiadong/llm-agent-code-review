"""
多 Agent 审查编排配置模块

该模块只定义审查代理画像和审查类型到代理组合的映射,不直接调用 LLM。
ReviewService 根据这些画像多次构造 Prompt,复用现有 DeepSeekAgent、ResultParser 和日志表。
"""
from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class ReviewAgentProfile:
    """代码审查代理画像

    Args:
        code: 代理机器标识,用于日志和任务摘要。
        name: 代理中文名称,用于 Prompt 与文档展示。
        focus: 代理关注范围的简短描述。
        issue_types: 该代理重点覆盖的问题类型。
        instruction: 额外审查指令,用于降低多代理重复审查。
    """

    code: str
    name: str
    focus: str
    issue_types: tuple[str, ...]
    instruction: str
    system_prompt: str = ""
    is_custom: bool = False
    release_id: int = 0
    version_id: int = 0
    temperature: float = 0.2
    max_tokens: int = 4096


GENERAL_AGENT = ReviewAgentProfile(
    code="general",
    name="通用质量代理",
    focus="综合检查代码规范、潜在 Bug、可维护性和注释完整性。",
    issue_types=("代码规范", "潜在Bug", "异常处理", "命名规范", "可维护性", "注释完整性", "其他"),
    instruction="请优先报告确定性高、能直接定位到代码行的问题,避免与安全和性能专项重复。",
)

SECURITY_AGENT = ReviewAgentProfile(
    code="security",
    name="安全审查代理",
    focus="重点识别注入、越权、敏感信息泄露、不安全反序列化和输入校验缺失。",
    issue_types=("安全漏洞", "异常处理", "其他"),
    instruction="只报告有明确安全影响或异常处理风险的问题,不要泛化为普通代码风格建议。",
)

PERFORMANCE_AGENT = ReviewAgentProfile(
    code="performance",
    name="性能审查代理",
    focus="重点识别 N+1 查询、重复计算、低效循环、阻塞调用和资源释放问题。",
    issue_types=("性能问题", "可维护性", "其他"),
    instruction="优先报告对响应时间、内存、数据库访问或 I/O 有实际影响的问题。",
)

MAINTAINABILITY_AGENT = ReviewAgentProfile(
    code="maintainability",
    name="可维护性代理",
    focus="重点识别函数过长、职责混乱、重复代码、命名不清和注释缺失。",
    issue_types=("代码规范", "命名规范", "可维护性", "注释完整性", "其他"),
    instruction="请关注后续修改成本和可读性,不要重复报告安全或性能专项已覆盖的问题。",
)

RELIABILITY_AGENT = ReviewAgentProfile(
    code="reliability",
    name="可靠性代理",
    focus="重点识别潜在 Bug、边界条件、空值处理、异常吞掉和状态不一致。",
    issue_types=("潜在Bug", "异常处理", "其他"),
    instruction="请优先报告可能导致运行时错误、数据错误或状态流转异常的问题。",
)

_PROFILE_MAP: dict[str, tuple[ReviewAgentProfile, ...]] = {
    "quick": (GENERAL_AGENT,),
    "standard": (GENERAL_AGENT,),
    "security": (SECURITY_AGENT, RELIABILITY_AGENT),
    "performance": (PERFORMANCE_AGENT, MAINTAINABILITY_AGENT),
    "full": (SECURITY_AGENT, RELIABILITY_AGENT, PERFORMANCE_AGENT, MAINTAINABILITY_AGENT),
}

# 圆桌讨论强调观点互补，固定邀请全部代码审查画像。该集合与普通 full
# 审查分离，避免改变既有并行审查的调用次数和成本口径。
_DISCUSSION_PROFILES: tuple[ReviewAgentProfile, ...] = (
    GENERAL_AGENT,
    SECURITY_AGENT,
    RELIABILITY_AGENT,
    PERFORMANCE_AGENT,
    MAINTAINABILITY_AGENT,
)


def get_agent_profiles(review_type: Optional[str]) -> tuple[ReviewAgentProfile, ...]:
    """根据审查类型返回代理组合

    Args:
        review_type: 审查类型,支持 quick/standard/security/performance/full。

    Returns:
        tuple[ReviewAgentProfile, ...]: 对应的代理画像组合,未知类型回退为 standard。
    """
    normalized = (review_type or "standard").lower()
    return _PROFILE_MAP.get(normalized, _PROFILE_MAP["standard"])


def get_discussion_agent_profiles() -> tuple[ReviewAgentProfile, ...]:
    """返回圆桌讨论的完整代码审查子 Agent 集合。

    Returns:
        tuple[ReviewAgentProfile, ...]: 按通用、安全、可靠性、性能和可维护性
        排列的稳定参会画像集合。
    """
    return _DISCUSSION_PROFILES


def format_agent_section(profile: ReviewAgentProfile) -> str:
    """将代理画像格式化为 Prompt 片段

    Args:
        profile: 当前审查代理画像。

    Returns:
        str: 可直接插入 Prompt 的角色说明片段。
    """
    issue_types = " / ".join(profile.issue_types)
    return (
        f"- 代理名称: {profile.name}\n"
        f"- 关注范围: {profile.focus}\n"
        f"- 重点问题类型: {issue_types}\n"
        f"- 审查要求: {profile.instruction}"
    )


def build_agent_summary(profiles: tuple[ReviewAgentProfile, ...]) -> str:
    """生成任务摘要中的代理组合描述

    Args:
        profiles: 本次审查使用的代理画像组合。

    Returns:
        str: 简短中文描述,用于 review_task.summary。
    """
    if len(profiles) == 1:
        return profiles[0].name
    return "多 Agent 协同(" + "、".join(profile.name for profile in profiles) + ")"


def get_model_label(model_name: str, profiles: tuple[ReviewAgentProfile, ...]) -> str:
    """生成可存入 review_task.model_name 的模型标签

    Args:
        model_name: 实际调用的 LLM 模型名称。
        profiles: 本次审查使用的代理画像组合。

    Returns:
        str: 长度受控的模型/代理标签。
    """
    if len(profiles) == 1 and profiles[0].code == "general":
        return model_name
    if len(profiles) == 1:
        return f"{model_name}/{profiles[0].code}-agent"
    return f"{model_name}/multi-agent"


# ============ 协同审查 Prompt 模板 (v2.1.2) ============

COLLAB_CROSS_REVIEW_SYSTEM = (
    "你是一名首席代码审查架构师(Cross-Review Lead),"
    "你的职责是审阅多个专项代理(安全/性能/可靠性/可维护性)各自独立审查的结果,"
    "并输出一份交叉复审报告。\n\n"
    "严格约束:\n"
    "1. 只输出 JSON,不输出任何额外文字\n"
    "2. 不要凭空添加新问题,只基于已有 findings 做判断\n"
    "3. 标记 confirmed(≥2个代理独立标注同一位置)、disputed(代理间结论矛盾)、"
    "escalated(交叉分析后发现风险比单代理判断更严重)"
)

COLLAB_CROSS_REVIEW_USER = """## 审查目标

请对以下多个代码审查代理的独立发现做交叉复审。

## 代码文件

```
语言: {language}
文件名: {file_name}
行偏移: {line_offset}
```

## 各代理独立发现

{agent_findings}

## 交叉复审要求

请逐条分析上述发现,输出以下 JSON:

{{
  "cross_review": [
    {{
      "verdict": "confirmed|disputed|escalated|merged",
      "merged_from": ["代理名称"],
      "original_titles": ["原始标题1", "原始标题2"],
      "final_title": "统合后的问题标题",
      "category": "问题类别",
      "explanation": "交叉复审的理由(30-100字)",
      "severity": "严重|高|中|低",
      "severity_reason": "若与原始严重度不同,说明调整原因",
      "line_start": 12,
      "line_end": 18,
      "confidence": 0.9,
      "owasp": "OWASP编号(仅安全类)",
      "cwe": "CWE编号(仅安全类)",
      "evidence": "直接来自代码的证据行",
      "exploit_scenario": "可复现的利用场景(仅安全类)",
      "references": ["参考链接"],
      "cvss_score": 9.8,
      "cvss_vector": "AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H",
      "remediation": "详细修复步骤",
      "source_details": [
        {{"source":"llm:security","confidence":0.9,"evidence":"证据行","line_number":12,"title":"原始标题"}}
      ]
    }}
  ],
  "summary": {{
    "confirmed": 0,
    "disputed": 0,
    "escalated": 0,
    "merged": 0,
    "total_input": 0,
    "total_output": 0
  }}
}}

## 判断规则

- **confirmed**: ≥2个代理独立命中同一代码位置,且问题描述一致。severity 取最高。
- **disputed**: 代理 A 标记为严重,代理 B 标记为误报/不相关。需要保留但降低 confidence。
- **escalated**: 交叉分析发现该问题在另一个代理的视角下风险更高。例如: 性能代理标记"N+1 查询",
  但安全代理视角下可被 DoS 利用从而升级严重度。
- **merged**: 多个代理用不同措辞描述了本质上同一条问题。合并为 1 条,标注 merged_from。
- 同一 CWE 出现在相邻行不代表同一问题;证据代码或调用点不同的独立 sink 必须分别保留。
- evidence、CVSS、来源明细必须从原始 finding 透传;不得根据 severity 猜测 CVSS 分数。
- 每条原始 finding 必须被至少 1 条 cross_review 条目引用。
- 单代理审查(only_reviewer 只有一个)时: 直接 copy 原始 findings 为 confirmed。
"""


COLLAB_CONSENSUS_SYSTEM = (
    "你是一名代码审查仲裁官,负责将交叉复审的结果转化为最终问题清单。"
    "只输出 JSON,不输出额外文字。"
)

COLLAB_CONSENSUS_USER = """## 任务

将交叉复审报告转化为最终问题清单,补充修复建议和攻击场景。

## 交叉复审结果

{cross_review_json}

## 输出 Schema

{{
  "issues": [
    {{
      "issue_type": "问题类型(安全漏洞/性能问题/潜在Bug/代码规范/异常处理/可维护性/命名规范/注释完整性/其他)",
      "severity": "严重|高|中|低",
      "title": "问题标题",
      "line_number": 12,
      "end_line": 18,
      "description": "问题详细描述(50-200字)",
      "suggestion": "具体修复建议(50-200字)",
      "fixed_code": "修复示例代码(可为空)",
      "confidence": 0.9,
      "cross_agent_count": 2,
      "cross_agent_names": ["安全审查代理", "可靠性代理"],
      "owasp": "OWASP编号(仅安全类,可选)",
      "cwe": "CWE编号(仅安全类,可选)",
      "evidence": "直接来自代码的证据行",
      "exploit_scenario": "攻击场景(可选)",
      "references": ["参考链接"],
      "cvss_score": 9.8,
      "cvss_vector": "AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H",
      "remediation": "详细修复步骤",
      "source_details": [
        {{"source":"llm:security","confidence":0.9,"evidence":"证据行","line_number":12,"title":"原始标题"}}
      ]
    }}
  ],
  "discarded": [
    {{
      "title": "被丢弃的问题标题",
      "reason": "丢弃原因(误报/重复/已合并)",
      "by_agent": "来源代理名称"
    }}
  ]
}}

## 规则

- confirmed/escalated 条目 → 生成 issue,保留 cross_agent_count 和 cross_agent_names
- disputed 条目 → 降低 severity 1 档,降低 confidence 到 0.5-0.6
- merged 条目 → 合并为 1 条 issue,merged_from 中的代理名写入 cross_agent_names
- discarded 列表记录所有误报/已被合并覆盖的原因
- evidence 不同的独立代码调用点不得合并;最终列表仍会经过确定性 N-way 归一化。
- 有合法 CVSS 向量时原样透传;没有向量或分数时保留为空,禁止用 severity 估算。
"""
