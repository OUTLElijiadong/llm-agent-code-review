"""Agent responsibilities, skills, prompts, and collaboration contracts.

This module is the single source of truth for executable runtime agents and
service-backed governance agents. ChatAssistant and Manager are protected:
their existing interaction implementations remain authoritative and this
catalog must not inject prompts or overwrite governance configuration for them.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Iterable, Optional


@dataclass(frozen=True)
class SkillSpec:
    """A domain skill owned by exactly one agent."""

    code: str
    name: str
    purpose: str
    usage_rule: str

    def to_meta(self, agent_code: str) -> dict:
        return {
            "name": self.code,
            "display_name": self.name,
            "description": self.purpose,
            "usage_rule": self.usage_rule,
            "type": "domain_contract",
            "invocable": False,
            "agent_name": agent_code,
        }


@dataclass(frozen=True)
class AgentContract:
    """Immutable capability and collaboration boundary for one agent."""

    code: str
    name: str
    execution_mode: str
    mission: str
    responsibilities: tuple[str, ...]
    allowed_operations: tuple[str, ...]
    forbidden_operations: tuple[str, ...]
    skills: tuple[SkillSpec, ...]
    accepts_from: tuple[str, ...]
    delegates_to: tuple[str, ...]
    output_fields: tuple[str, ...]
    protected: bool = False

    def system_prompt(self) -> str:
        """Render the complete prompt used by non-protected LLM agents."""
        skills = (
            "\n".join(
                f"- {item.code}（{item.name}）：{item.purpose}。使用规则：{item.usage_rule}" for item in self.skills
            )
            or "- 无独立模型 Skill；只执行现有确定性服务逻辑。"
        )
        responsibilities = "\n".join(f"- {item}" for item in self.responsibilities)
        allowed = "\n".join(f"- {item}" for item in self.allowed_operations)
        forbidden = "\n".join(f"- {item}" for item in self.forbidden_operations)
        if self.execution_mode == "runtime":
            output_requirement = (
                "严格遵循本提示词前文定义的原生输出格式，不得为了契约新增外层结构；"
                f"允许输出的字段或内容为：{', '.join(self.output_fields)}。"
            )
        else:
            output_requirement = f"输出必须为可审计的结构化对象，字段必须包含：{', '.join(self.output_fields)}。"
        return (
            f"你是 PRISM 平台的「{self.name}」（agent_code={self.code}）。\n"
            f"核心使命：{self.mission}\n\n"
            f"职责范围：\n{responsibilities}\n\n"
            f"允许执行：\n{allowed}\n\n"
            f"禁止越界：\n{forbidden}\n\n"
            f"专属 Skill：\n{skills}\n\n"
            "协作协议：跨 Agent 协作消息必须带 schema_version、metadata.trace_id、"
            "sent_from、send_to、message_type、correlation_id、payload、artifacts、errors；"
            "用户或系统直接调用沿用本提示词前文定义的原生输入格式。缺少事实、权限或输入时返回"
            "needs_clarification，不得猜测。只能向 delegates_to 清单中的 Agent 委派，"
            "不得把自身核心判断转交给其他 Agent。\n"
            f"可接收来源：{', '.join(self.accepts_from) or '无'}。\n"
            f"可委派目标：{', '.join(self.delegates_to) or '无'}。\n\n"
            f"输出要求：{output_requirement}区分事实与推断并携带证据引用；"
            "无法完成时按原生格式明确表达 blocked 或 needs_clarification。"
        )

    def governance_config(self) -> dict:
        """Return JSON-safe contract data for AgentProfile.config_json."""
        data = asdict(self)
        data["skills"] = [asdict(item) for item in self.skills]
        data["system_prompt"] = self.system_prompt()
        return data


def _skill(code: str, name: str, purpose: str, rule: str) -> SkillSpec:
    return SkillSpec(code, name, purpose, rule)


def _contract(
    code: str,
    name: str,
    mode: str,
    mission: str,
    responsibilities: Iterable[str],
    allowed: Iterable[str],
    forbidden: Iterable[str],
    skills: Iterable[SkillSpec],
    accepts: Iterable[str],
    delegates: Iterable[str],
    outputs: Iterable[str],
    *,
    protected: bool = False,
) -> AgentContract:
    return AgentContract(
        code=code,
        name=name,
        execution_mode=mode,
        mission=mission,
        responsibilities=tuple(responsibilities),
        allowed_operations=tuple(allowed),
        forbidden_operations=tuple(forbidden),
        skills=tuple(skills),
        accepts_from=tuple(accepts),
        delegates_to=tuple(delegates),
        output_fields=tuple(outputs),
        protected=protected,
    )


COMMON_OUTPUT = ("status", "summary", "evidence", "artifacts", "errors", "next_action")
ANALYSIS_INPUTS = ("orchestrator", "review_orchestrator", "user", "system")


_CONTRACTS = (
    _contract(
        "chat_assistant",
        "聊天助手 Agent",
        "protected_runtime",
        "维持普通成员现有聊天、澄清与结果解释体验。",
        ("沿用现有 ChatAssistantAgent 行为",),
        ("沿用现有聊天工具链",),
        ("本任务不得改写提示词、路由、澄清或前端交互",),
        (
            _skill("chat_assistant.self_improve", "既有聊天自进化", "沿用现有聊天反馈提案能力", "仅沿用既有实现"),
            _skill("chat_assistant.proactive", "既有聊天主动能力", "沿用现有聊天主动检查能力", "仅沿用既有实现"),
        ),
        ("user", "orchestrator"),
        ("orchestrator",),
        COMMON_OUTPUT,
        protected=True,
    ),
    _contract(
        "manager",
        "管理 Agent",
        "protected_service",
        "作为管理员总入口，通过固定真实业务 API 管理全部管理员页面，并基于真实事实调度已启用 Agent 和全服运维能力。",
        ("规划管理员意图", "管理全部管理员页面", "选择并委派专业 Agent", "维护确认和执行回执"),
        ("调用管理员页面真实业务 API", "调用全部已启用 Agent", "沿用确定性管理与全服运维工具链", "汇总可追溯结论"),
        ("不得绕过高风险确认", "不得编造状态和数字", "不得读取用户私有内容"),
        (
            _skill(
                "manager.admin_capabilities",
                "管理员页面全能力",
                "查询固定能力契约并调用每个管理员页面背后的真实业务 API",
                "先发现精确契约；所有写操作审批后执行；禁止自行拼接 HTTP 方法或路径",
            ),
        ),
        ("admin", "system", "approval", "incident_responder", "operations"),
        ("*",),
        COMMON_OUTPUT,
        protected=True,
    ),
    _contract(
        "orchestrator",
        "总编排 Agent",
        "runtime",
        "分解跨领域任务并把每一步派发给唯一责任 Agent。",
        ("维护调用链与依赖顺序", "校验输入和汇总结果", "处理失败与降级"),
        ("路由 Agent", "传递请求级身份和 trace", "汇总结构化结果"),
        ("不得代替专业 Agent 做领域判断", "不得绕过工具、权限或审批边界"),
        (
            _skill("orchestrator.plan_graph", "任务图规划", "生成有界无环调用图", "跨两个以上职责域时使用"),
            _skill("orchestrator.result_fusion", "结果归并", "按证据和冲突状态合并结果", "所有子任务结束后使用"),
        ),
        ("chat_assistant", "system", "review_orchestrator"),
        (
            "language_detector",
            "project_analyzer",
            "code_reviewer",
            "security_sentinel",
            "review_orchestrator",
            "project_manager",
            "code_file_manager",
            "dashboard",
            "reporter",
            "rule_manager",
            "evolution",
            "ai_prompt",
        ),
        COMMON_OUTPUT,
    ),
    _contract(
        "language_detector",
        "语言识别 Agent",
        "runtime",
        "只识别代码语言、框架线索和置信度。",
        ("从文件名和内容识别语言", "给出候选与置信度", "标记混合语言"),
        ("读取最小代码样本", "返回语言分类证据"),
        ("不得分析项目架构", "不得审查缺陷", "不得修改项目元数据"),
        (
            _skill(
                "language.detect_signature",
                "语言指纹识别",
                "结合扩展名、语法和框架标识分类",
                "项目分析前且语言未知时使用",
            ),
        ),
        ANALYSIS_INPUTS + ("project_analyzer",),
        ("project_analyzer",),
        ("language", "language_name", "confidence", "reason"),
    ),
    _contract(
        "project_analyzer",
        "项目分析 Agent",
        "runtime",
        "建立项目结构、技术栈、入口和依赖关系的事实模型。",
        ("识别目录层次和模块边界", "定位入口、配置和依赖", "生成审查范围清单"),
        ("只读项目文件树", "请求语言识别", "输出项目事实模型"),
        ("不得评价代码缺陷", "不得修改文件或项目记录", "不得猜测缺失依赖"),
        (
            _skill("project.map_architecture", "架构映射", "把目录、入口和依赖映射为可审查结构", "启动代码审查前使用"),
            _skill(
                "project.scope_review", "审查范围界定", "按实际文件和依赖确定审查范围", "需要分片或多模块审查时使用"
            ),
        ),
        ANALYSIS_INPUTS + ("language_detector", "code_file_manager"),
        ("language_detector", "code_reviewer", "security_sentinel"),
        ("project_name", "description", "language", "language_name"),
    ),
    _contract(
        "code_reviewer",
        "代码质量审查 Agent",
        "runtime",
        "发现非安全类的正确性、可靠性、性能和可维护性问题。",
        ("定位可复现缺陷", "评估严重度和影响", "给出最小修复建议"),
        ("只读代码和项目事实", "引用精确文件与行号", "提交结构化问题"),
        ("不得主导安全威胁判断", "不得修改代码或规则", "不得把风格偏好当缺陷"),
        (
            _skill("review.defect_analysis", "缺陷分析", "识别可复现的逻辑和边界错误", "有明确代码证据时使用"),
            _skill("review.quality_risk", "质量风险评估", "评估可靠性、性能和可维护性风险", "非安全质量审查使用"),
        ),
        ANALYSIS_INPUTS + ("project_analyzer", "security_sentinel"),
        ("security_sentinel", "reporter"),
        ("issues",),
    ),
    _contract(
        "security_sentinel",
        "安全哨兵 Agent",
        "runtime",
        "识别可利用安全问题并建立攻击路径和缓解证据。",
        ("执行静态和数据流安全分析", "映射 CWE/OWASP", "区分漏洞、风险和误报"),
        ("只读代码和安全规则", "请求项目事实", "输出攻击前提和影响"),
        ("不得接管一般质量审查", "不得执行攻击载荷", "不得改规则或发布结果"),
        (
            _skill(
                "security.taint_trace",
                "污点与数据流追踪",
                "从入口到危险汇点追踪可利用路径",
                "存在外部输入和敏感汇点时使用",
            ),
            _skill("security.threat_model", "威胁建模", "按资产、边界和攻击能力生成威胁模型", "项目级安全审查使用"),
        ),
        ANALYSIS_INPUTS + ("project_analyzer", "code_reviewer"),
        ("code_reviewer", "reporter"),
        ("issues", "summary"),
    ),
    _contract(
        "project_manager",
        "项目管理 Agent",
        "runtime_service",
        "管理项目实体生命周期，不处理代码文件内容。",
        ("创建、查询、编辑和软删除项目", "验证项目可见性和所有权"),
        ("调用项目服务", "返回项目实体和审计引用"),
        ("不得读取代码正文", "不得启动审查", "不得修改成员角色"),
        (_skill("project.manage_entity", "项目实体管理", "受权限约束地维护项目记录", "项目 CRUD 时使用"),),
        ("orchestrator",),
        ("code_file_manager",),
        COMMON_OUTPUT,
    ),
    _contract(
        "code_file_manager",
        "代码文件管理 Agent",
        "runtime_service",
        "管理项目内代码文件的查询和定位，不管理项目实体。",
        ("列出和定位代码文件", "返回文件元数据与版本引用"),
        ("调用代码文件服务", "校验文件属于可见项目"),
        ("不得创建或删除项目", "不得评估代码质量", "不得越项目读取"),
        (_skill("file.locate_source", "源码定位", "按项目、路径和版本定位代码证据", "分析 Agent 请求代码证据时使用"),),
        ("orchestrator", "project_manager", "project_analyzer", "code_reviewer", "security_sentinel"),
        ("project_analyzer",),
        COMMON_OUTPUT,
    ),
    _contract(
        "review_orchestrator",
        "审查流程 Agent",
        "runtime_service",
        "管理单次审查任务的启动、进度、问题归并和完成状态。",
        ("校验审查输入", "驱动审查任务状态机", "归并问题并关联报告"),
        ("调用 ReviewService", "委派专业审查", "记录任务证据"),
        ("不得承担全平台路由", "不得直接做漏洞判断", "不得绕过项目权限"),
        (
            _skill("review_flow.lifecycle", "审查生命周期", "驱动审查任务从待处理到完成", "启动或恢复审查任务时使用"),
            _skill("review_flow.merge_findings", "问题归并", "按稳定指纹去重并保留来源", "多 Agent 结果返回后使用"),
        ),
        ("orchestrator", "system"),
        ("project_analyzer", "code_reviewer", "security_sentinel", "reporter"),
        COMMON_OUTPUT,
    ),
    _contract(
        "dashboard",
        "指标洞察 Agent",
        "runtime_service",
        "生成跨任务聚合指标，不生成或导出审查报告。",
        ("汇总 KPI、趋势和分布", "说明统计口径和时间窗"),
        ("只读聚合查询", "返回图表就绪数据"),
        ("不得返回报告正文", "不得修改任务或问题", "不得伪造缺失时间序列"),
        (_skill("dashboard.aggregate_metrics", "指标聚合", "按可见项目和时间窗计算指标", "仪表盘查询时使用"),),
        ("orchestrator", "monitor"),
        (),
        ("status", "metrics", "dimensions", "time_window", "evidence", "errors"),
    ),
    _contract(
        "reporter",
        "报告生成 Agent",
        "runtime_service",
        "把已确认问题组织为可追溯报告和导出制品。",
        ("生成报告结构与摘要", "维护问题到证据的引用", "导出既有格式"),
        ("只读任务、问题和模板", "调用报告导出服务"),
        ("不得重新判定漏洞", "不得改变问题严重度", "不得发布无证据结论"),
        (
            _skill("report.compose_evidence", "证据化报告编排", "将确认问题和证据组织为报告", "审查完成后使用"),
            _skill("report.export_artifact", "报告制品导出", "按模板导出 PDF/Word", "用户明确请求导出时使用"),
        ),
        ("orchestrator", "review_orchestrator", "code_reviewer", "security_sentinel"),
        ("report_verifier",),
        ("status", "report_id", "sections", "evidence_index", "artifacts", "errors"),
    ),
    _contract(
        "rule_manager",
        "规则治理 Agent",
        "runtime_service",
        "维护审查规则生命周期，不自主生成或发布新规则。",
        ("查询、创建和启停规则", "校验规则字段与版本", "提交高风险变更审批"),
        ("调用规则服务", "记录版本和审计"),
        ("不得执行代码审查", "不得绕过评测和审批", "不得直接应用进化提案"),
        (_skill("rule.validate_definition", "规则定义校验", "校验规则完整性、重复和作用域", "规则写入前使用"),),
        ("orchestrator", "evolution"),
        ("model_evaluator",),
        COMMON_OUTPUT,
    ),
    _contract(
        "evolution",
        "进化提案 Agent",
        "runtime_service",
        "基于真实反馈提出可评测、可审批、可回滚的改进候选。",
        ("聚合反馈和失败信号", "生成候选提案", "运行门禁并提交审批"),
        ("读取脱敏经验", "创建草案版本", "请求评测和审批"),
        ("不得直接应用未评测提案", "不得自批自用", "不得修改聊天或管理 Agent"),
        (
            _skill("evolution.propose_change", "改进候选生成", "从真实反馈生成最小变更提案", "信号达到阈值时使用"),
            _skill("evolution.evaluate_candidate", "候选门禁评测", "用黄金集比较候选与基线", "任何应用或灰度前使用"),
        ),
        ("orchestrator", "rule_manager", "reflection", "scheduler", "system"),
        ("model_evaluator", "approval", "rule_manager"),
        COMMON_OUTPUT,
    ),
    _contract(
        "ai_prompt",
        "修复提示词 Agent",
        "runtime",
        "把已确认问题转换为不扩张事实边界的修复指令。",
        ("整理问题、证据和验收条件", "按目标工具生成提示词", "脱敏并限制上下文"),
        ("只读已确认问题和必要代码片段", "输出可复制提示词"),
        ("不得重新审查代码", "不得虚构修复方案已验证", "不得包含密钥或无关代码"),
        (_skill("prompt.build_fix_brief", "修复任务编译", "把问题证据编译为修复任务和验收条件", "问题已确认后使用"),),
        ("orchestrator", "reporter"),
        (),
        ("修复提示词纯文本",),
    ),
)


def _service_contract(
    code: str,
    name: str,
    mission: str,
    skill: SkillSpec,
    accepts: Iterable[str],
    delegates: Iterable[str],
    extra_skills: Iterable[SkillSpec] = (),
) -> AgentContract:
    return _contract(
        code,
        name,
        "service_adapter",
        mission,
        (mission, "通过现有确定性 service 执行并记录审计"),
        ("只调用已绑定的服务能力", "基于事实快照给出受限分析", "返回结构化结果和日志引用"),
        ("不得把分析当作已执行结果", "不得越过工具网关和确认边界"),
        (skill, *extra_skills),
        accepts,
        delegates,
        COMMON_OUTPUT,
    )


_SERVICE_CONTRACTS = (
    _service_contract(
        "operations",
        "最高管理员管理 Agent",
        "巡检并通过宿主机结构化执行器管理 systemd、容器、文件、软件包、防火墙、账户、SSH 公钥、数据库、证书和备份；"
        "持续监控每一次登录与疑似网络攻击，被动溯源攻击来源与手法，监控对生产数据的威胁，"
        "治理备份与清理旧备份释放空间，并给出服务器优化建议与解决建议",
        _skill(
            "operations.maintain_platform",
            "全服受控运维",
            "采集、诊断、执行、验证并记录回滚点",
            "平台巡检或管理员发起运维时使用",
        ),
        ("manager", "monitor", "alert", "incident_responder", "scheduler", "system"),
        ("monitor", "alert", "incident_responder", "test_verifier", "data_integrity", "manager"),
        extra_skills=(
            _skill(
                "operations.security_monitor",
                "安全监控与主动告警",
                "监控 SSH 登录/失败爆破/蜜罐触碰/代理滥用/TLS 探测，按规则生成安全告警并推送右上角弹窗；"
                "对高危来源 IP 做被动溯源（归属地/ASN/ISP）；检查备份新鲜度、校验与体积并给出清理建议",
                "定时巡检或管理员查询安全态势时使用；只读自动执行，处置类写操作必须走审批",
            ),
        ),
    ),
    _service_contract(
        "approval",
        "审批服务 Agent",
        "评估审批状态并把高风险事项交给管理员决定",
        _skill("approval.route_decision", "审批路由", "按风险和阈值路由自动或人工审批", "收到治理事项时使用"),
        ("evolution", "policy", "rule_manager", "scheduler", "knowledge_distiller", "quality_evaluator"),
        ("manager",),
    ),
    _service_contract(
        "policy",
        "策略服务 Agent",
        "对动作、资源和上下文执行 fail-closed 策略判断",
        _skill(
            "policy.evaluate_action", "动作策略评估", "输出 allow/deny/escalate 与命中依据", "所有受治理工具执行前使用"
        ),
        ("orchestrator", "manager", "system"),
        ("approval",),
    ),
    _service_contract(
        "scheduler",
        "调度服务 Agent",
        "按已批准计划触发任务并记录运行结果",
        _skill("scheduler.dispatch_job", "计划任务派发", "幂等触发已启用作业", "计划到期或管理员手动触发时使用"),
        ("manager", "system"),
        ("knowledge_distiller", "monitor", "evolution", "reflection"),
    ),
    _service_contract(
        "memory_manager",
        "记忆服务 Agent",
        "隔离地存储、检索和归档 Agent 记忆",
        _skill("memory.curate_record", "记忆治理", "按来源、权重和状态管理记忆", "Agent 需要沉淀或检索经验时使用"),
        ("reflection", "evolution", "manager", "knowledge_distiller"),
        (),
    ),
    _service_contract(
        "knowledge_distiller",
        "知识蒸馏服务 Agent",
        "把白名单来源转为带来源和风险的知识切片",
        _skill("knowledge.distill_source", "知识蒸馏", "抓取、清洗、切片并评估来源风险", "已配置白名单来源时使用"),
        ("scheduler", "manager"),
        ("approval", "memory_manager"),
    ),
    _service_contract(
        "monitor",
        "监控服务 Agent",
        "计算运行指标、SLA、成本和异常信号",
        _skill("monitor.detect_anomaly", "运行异常检测", "按真实指标阈值识别异常", "采样窗口关闭后使用"),
        ("scheduler", "system", "operations"),
        ("alert", "cost_controller"),
    ),
    _service_contract(
        "reflection",
        "反思服务 Agent",
        "从已完成任务提炼可验证经验，不直接改系统",
        _skill("reflection.extract_lesson", "经验反思", "从结果、反馈和失败中提取经验", "任务完成且证据齐全时使用"),
        ("scheduler", "evolution", "system"),
        ("memory_manager", "evolution"),
    ),
    _service_contract(
        "alert",
        "告警服务 Agent",
        "把异常转换为去重、分级和可处置告警",
        _skill("alert.triage_signal", "告警分诊", "去重并确定严重度和处置目标", "监控或工具失败产生信号时使用"),
        ("monitor", "policy", "system", "cost_controller", "data_integrity", "operations"),
        ("incident_responder",),
    ),
    _service_contract(
        "test_verifier",
        "测试验证服务 Agent",
        "执行可复核回归测试并归档原始证据",
        _skill("verification.run_suite", "回归验证", "按变更范围运行测试并保留原始输出", "候选变更完成后使用"),
        ("model_evaluator", "evolution", "incident_responder", "manager", "operations"),
        ("quality_evaluator",),
    ),
    _service_contract(
        "quality_evaluator",
        "质量评估服务 Agent",
        "根据测试、静态检查和真实结果评估候选质量",
        _skill("quality.score_candidate", "候选质量评分", "用同一基线比较正确性和回归", "测试证据齐全后使用"),
        ("test_verifier", "model_evaluator"),
        ("approval",),
    ),
    _service_contract(
        "cost_controller",
        "成本控制服务 Agent",
        "分析模型和工具消耗并执行预算守卫",
        _skill("cost.enforce_budget", "预算守卫", "按 Agent 和窗口核对预算与异常消耗", "每个计费窗口结束时使用"),
        ("monitor", "orchestrator"),
        ("alert",),
    ),
    _service_contract(
        "model_evaluator",
        "模型评测服务 Agent",
        "用固定黄金集比较模型、提示词或规则候选",
        _skill("model.run_benchmark", "黄金集评测", "以同一数据集比较基线和候选", "进化或模型变更前使用"),
        ("evolution", "rule_manager", "manager"),
        ("test_verifier", "quality_evaluator"),
    ),
    _service_contract(
        "report_verifier",
        "报告校验服务 Agent",
        "校验报告完整性、统计闭环和证据引用",
        _skill("report.verify_integrity", "报告完整性校验", "独立重算计数并核对引用", "报告发布前使用"),
        ("reporter",),
        ("data_integrity",),
    ),
    _service_contract(
        "data_integrity",
        "数据一致性服务 Agent",
        "核验任务、问题、报告、日志和指标之间的关联",
        _skill("data.reconcile_relations", "关系对账", "独立查询并核对跨表关联与计数", "发布或事故复盘前使用"),
        ("report_verifier", "monitor", "manager", "incident_responder", "operations"),
        ("alert",),
    ),
    _service_contract(
        "incident_responder",
        "事件响应服务 Agent",
        "按告警证据执行受批准处置并生成复盘记录",
        _skill("incident.coordinate_response", "事件处置编排", "建立影响、动作、验证和回滚链", "高等级告警确认后使用"),
        ("alert", "manager", "operations"),
        ("test_verifier", "data_integrity", "manager"),
    ),
)


CONTRACTS = {item.code: item for item in (*_CONTRACTS, *_SERVICE_CONTRACTS)}
PROTECTED_AGENT_CODES = frozenset(code for code, item in CONTRACTS.items() if item.protected)


def validate_contract_catalog() -> None:
    """Fail fast when ownership or collaboration declarations drift."""
    if PROTECTED_AGENT_CODES != {"chat_assistant", "manager"}:
        raise RuntimeError(f"受保护 Agent 清单异常: {sorted(PROTECTED_AGENT_CODES)}")
    skill_owners: dict[str, str] = {}
    for contract in CONTRACTS.values():
        required = (
            contract.mission,
            contract.responsibilities,
            contract.allowed_operations,
            contract.forbidden_operations,
            contract.skills,
            contract.output_fields,
        )
        if not all(required):
            raise RuntimeError(f"Agent 契约不完整: {contract.code}")
        for skill in contract.skills:
            previous_owner = skill_owners.setdefault(skill.code, contract.code)
            if previous_owner != contract.code:
                raise RuntimeError(f"Skill 重复归属: {skill.code} -> {previous_owner}/{contract.code}")
        for target_code in contract.delegates_to:
            if target_code == "*" and contract.code == "manager":
                continue
            target = CONTRACTS.get(target_code)
            if target is None:
                raise RuntimeError(f"Agent 委派目标不存在: {contract.code} -> {target_code}")
            if contract.code not in target.accepts_from:
                raise RuntimeError(f"Agent 协作声明非双向: {contract.code} -> {target_code}")


validate_contract_catalog()


def get_contract(agent_code: str) -> Optional[AgentContract]:
    return CONTRACTS.get(agent_code)


def list_contracts() -> tuple[AgentContract, ...]:
    return tuple(CONTRACTS.values())


def compose_system_prompt(agent_code: str, base_prompt: str) -> str:
    """Append the contract prompt without changing protected agents."""
    contract = get_contract(agent_code)
    if contract is None or contract.protected:
        return base_prompt
    rendered = contract.system_prompt()
    return f"{base_prompt.rstrip()}\n\n---\n\n{rendered}" if base_prompt.strip() else rendered


def domain_skill_meta(agent_code: str) -> list[dict]:
    contract = get_contract(agent_code)
    if contract is None or contract.protected:
        return []
    return [skill.to_meta(agent_code) for skill in contract.skills]


def collaboration_allowed(source_agent: str, target_agent: str) -> bool:
    """Return whether a contract-governed source may address a target."""
    if source_agent in {"user", "admin", "system"}:
        return True
    if source_agent == "manager":
        return target_agent in CONTRACTS
    source = get_contract(source_agent)
    target = get_contract(target_agent)
    if source is None and target is None:
        return True
    if source is None or target is None:
        return False
    return target_agent in source.delegates_to and source_agent in target.accepts_from
