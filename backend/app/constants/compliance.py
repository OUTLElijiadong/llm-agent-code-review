"""合规条款字典模块 (T04)

集中维护 4 套主流合规标准条款字典及 CWE → 合规反向映射,供
SecuritySentinelAgent / ReportAgent 在漏洞命中后即时输出"违反了哪些合规条款"。

覆盖标准:
- ISO 27001:2022 附录 A 控制措施 (安全开发相关)
- GDPR 关键条款
- PCI-DSS v4.0 关键要求
- HIPAA Security Rule

设计原则:
1. 纯数据 + 纯函数,无外部依赖,便于单测与跨模块复用
2. 字典键统一为条款编号字符串,值类型为 ComplianceControl
3. CWE 映射键统一归一化为大写 "CWE-XXX",对外查询大小写不敏感
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional


@dataclass(frozen=True)
class ComplianceControl:
    """合规条款描述

    单条合规条款的元数据,用于在审查报告 / 仪表盘中展示条款编号、标题与分类。

    Attributes:
        code: 条款编号,如 "A.14.2.1"、"Art.32"、"Req-6.2.4"、"§164.312(b)"
        title: 条款标题(中文),如 "处理的安全性"
        description: 简要说明(中文),描述条款核心要求
        category: 分类(中文),如 "安全开发"、"数据保护原则"
    """

    code: str
    title: str
    description: str
    category: str


# ============ ISO 27001:2022 附录 A 控制措施 (安全开发相关) ============

ISO_27001_CONTROLS: Dict[str, ComplianceControl] = {
    "A.5.1": ComplianceControl(
        code="A.5.1",
        title="信息安全的策略",
        description="组织应制定、发布并定期更新信息安全策略,并由管理层批准。",
        category="组织策略",
    ),
    "A.5.7": ComplianceControl(
        code="A.5.7",
        title="威胁情报",
        description="应建立收集与分析威胁情报的能力,用于支撑安全决策与风险处置。",
        category="组织策略",
    ),
    "A.5.17": ComplianceControl(
        code="A.5.17",
        title="身份验证信息",
        description="身份验证信息(口令/密钥/令牌)的分配与使用应受控,并强制定期轮换。",
        category="身份验证与访问管理",
    ),
    "A.5.34": ComplianceControl(
        code="A.5.34",
        title="隐私与个人数据保护",
        description="应识别并保护个人数据,满足适用隐私法规要求,贯穿数据全生命周期。",
        category="隐私保护",
    ),
    "A.6.3": ComplianceControl(
        code="A.6.3",
        title="信息安全意识、教育和培训",
        description="全体员工及相关方应接受与其角色匹配的信息安全培训并定期复训。",
        category="人员安全",
    ),
    "A.8.1": ComplianceControl(
        code="A.8.1",
        title="用户终端资产清单",
        description="应维护用户终端与相关资产的清单,明确责任人与使用范围。",
        category="资产管理",
    ),
    "A.8.24": ComplianceControl(
        code="A.8.24",
        title="加密",
        description="应制定加密策略,对敏感数据在存储与传输环节使用合适算法与密钥管理。",
        category="加密",
    ),
    "A.8.25": ComplianceControl(
        code="A.8.25",
        title="安全开发生命周期",
        description="应在软件开发生命周期各阶段集成安全要求与活动,建立安全开发流程。",
        category="安全开发",
    ),
    "A.8.26": ComplianceControl(
        code="A.8.26",
        title="应用安全要求",
        description="应明确并记录应用与服务的安全要求,作为开发与验收的依据。",
        category="安全开发",
    ),
    "A.8.27": ComplianceControl(
        code="A.8.27",
        title="安全系统架构与工程",
        description="应基于安全原则设计系统架构,运用纵深防御与最小权限等工程实践。",
        category="安全开发",
    ),
    "A.8.28": ComplianceControl(
        code="A.8.28",
        title="安全编码",
        description="应遵循安全编码规范,防范常见漏洞并实施代码审查。",
        category="安全开发",
    ),
    "A.8.29": ComplianceControl(
        code="A.8.29",
        title="开发与验收中的安全测试",
        description="应在开发与验收阶段开展安全测试,验证安全要求得到满足。",
        category="安全开发",
    ),
    "A.8.30": ComplianceControl(
        code="A.8.30",
        title="外包开发",
        description="应对外包开发活动施加安全要求,并在合同与交付环节进行验证。",
        category="安全开发",
    ),
    "A.8.31": ComplianceControl(
        code="A.8.31",
        title="信息的分离",
        description="应根据敏感度与业务需要对信息进行分离,降低未授权访问与耦合风险。",
        category="安全开发",
    ),
    "A.8.32": ComplianceControl(
        code="A.8.32",
        title="变更管理",
        description="应对变更实施、测试与审批进行控制,记录变更以支持审计与回滚。",
        category="变更管理",
    ),
}


# ============ GDPR 关键条款 ============

GDPR_ARTICLES: Dict[str, ComplianceControl] = {
    "Art.5": ComplianceControl(
        code="Art.5",
        title="个人数据处理原则",
        description="个人数据处理应遵循合法、公平、透明、目的限制、数据最小化等原则。",
        category="数据保护原则",
    ),
    "Art.6": ComplianceControl(
        code="Art.6",
        title="处理的合法性",
        description="处理个人数据应有合法依据,如同意、合同、法定义务等。",
        category="合法性",
    ),
    "Art.7": ComplianceControl(
        code="Art.7",
        title="同意的条件",
        description="基于同意的处理应确保同意自由作出、可撤回,并留存同意记录。",
        category="合法性",
    ),
    "Art.25": ComplianceControl(
        code="Art.25",
        title="数据保护设计与默认设置",
        description="应在系统设计阶段融入数据保护措施,默认仅处理必要数据。",
        category="数据保护设计",
    ),
    "Art.32": ComplianceControl(
        code="Art.32",
        title="处理的安全性",
        description="应采取技术与组织措施保障处理安全,包括加密、访问控制与容灾。",
        category="安全性",
    ),
    "Art.33": ComplianceControl(
        code="Art.33",
        title="向监管机构通知数据违规",
        description="发生数据违规应在 72 小时内向监管机构报告,除非风险较低。",
        category="数据违规通知",
    ),
    "Art.34": ComplianceControl(
        code="Art.34",
        title="向数据主体通知数据违规",
        description="高风险数据违规应及时通知受影响数据主体,说明风险与应对措施。",
        category="数据违规通知",
    ),
    "Art.35": ComplianceControl(
        code="Art.35",
        title="数据保护影响评估",
        description="高风险处理活动应开展数据保护影响评估并记录处理结果。",
        category="影响评估",
    ),
    "Art.44": ComplianceControl(
        code="Art.44",
        title="跨境数据传输",
        description="向第三国或国际组织传输个人数据应满足充分性决定或适当保障措施。",
        category="跨境传输",
    ),
    "Art.89": ComplianceControl(
        code="Art.89",
        title="与处理目的相关的限制",
        description="为科学研究等目的进一步处理时,应施加技术与组织限制保障数据主体权利。",
        category="数据主体权利",
    ),
}


# ============ PCI-DSS v4.0 关键要求 ============

PCI_DSS_REQUIREMENTS: Dict[str, ComplianceControl] = {
    "Req-1.1": ComplianceControl(
        code="Req-1.1",
        title="安全网络架构",
        description="应建立并维护网络架构与配置,隔离持卡人数据环境。",
        category="网络隔离",
    ),
    "Req-2.1": ComplianceControl(
        code="Req-2.1",
        title="配置标准",
        description="应对系统组件实施安全配置标准,关闭不必要的服务与默认值。",
        category="配置管理",
    ),
    "Req-3.1": ComplianceControl(
        code="Req-3.1",
        title="最小化存储",
        description="应最小化存储持卡人数据,并定义数据保留与销毁策略。",
        category="数据保护",
    ),
    "Req-4.1": ComplianceControl(
        code="Req-4.1",
        title="传输中加密",
        description="在开放公共网络传输持卡人数据时应使用强加密与协议保护。",
        category="加密",
    ),
    "Req-5.1": ComplianceControl(
        code="Req-5.1",
        title="防病毒部署",
        description="应在常见受恶意软件影响的系统部署防病毒并保持更新。",
        category="恶意软件防护",
    ),
    "Req-6.2.1": ComplianceControl(
        code="Req-6.2.1",
        title="漏洞扫描",
        description="应定期扫描系统组件漏洞并对高风险漏洞及时修补。",
        category="漏洞管理",
    ),
    "Req-6.2.3": ComplianceControl(
        code="Req-6.2.3",
        title="代码审查",
        description="应对自定义代码变更进行代码审查,确保安全质量。",
        category="安全开发",
    ),
    "Req-6.2.4": ComplianceControl(
        code="Req-6.2.4",
        title="安全编码培训",
        description="开发人员应定期接受安全编码培训,了解常见漏洞与防护。",
        category="安全开发",
    ),
    "Req-6.4.1": ComplianceControl(
        code="Req-6.4.1",
        title="面向公众应用的安全控制",
        description="面向公众的应用应部署安全控制,防止攻击并持续监控。",
        category="应用安全",
    ),
    "Req-6.4.2": ComplianceControl(
        code="Req-6.4.2",
        title="公共应用测试",
        description="应在发布前对面向公众的应用进行安全测试,包括漏洞扫描与渗透测试。",
        category="应用安全",
    ),
    "Req-7.1": ComplianceControl(
        code="Req-7.1",
        title="访问控制",
        description="应基于业务需要定义并实施访问控制,限制对持卡人数据的访问。",
        category="访问控制",
    ),
    "Req-8.1": ComplianceControl(
        code="Req-8.1",
        title="身份验证",
        description="应对所有系统组件实施强身份验证,管理唯一 ID 与凭证。",
        category="身份验证",
    ),
    "Req-10.1": ComplianceControl(
        code="Req-10.1",
        title="审计日志",
        description="应启用并保护审计日志,记录对持卡人数据的访问与关键操作。",
        category="日志监控",
    ),
}


# ============ HIPAA Security Rule ============

HIPAA_SECTIONS: Dict[str, ComplianceControl] = {
    "§164.308": ComplianceControl(
        code="§164.308",
        title="管理性保障措施",
        description="应建立管理性流程与策略,包括风险评估、人员培训与应急计划。",
        category="行政保障",
    ),
    "§164.310": ComplianceControl(
        code="§164.310",
        title="物理性保障措施",
        description="应实施物理访问控制、设施安全与设备介质管理。",
        category="物理保障",
    ),
    "§164.312(a)": ComplianceControl(
        code="§164.312(a)",
        title="访问控制",
        description="应实施技术访问控制,限制电子受保护健康信息(ePHI)的访问。",
        category="技术保障",
    ),
    "§164.312(b)": ComplianceControl(
        code="§164.312(b)",
        title="审计控制",
        description="应部署审计控制,记录并审查涉及 ePHI 的系统活动。",
        category="技术保障",
    ),
    "§164.312(c)": ComplianceControl(
        code="§164.312(c)",
        title="完整性",
        description="应保护 ePHI 完整性,防止未授权的修改或销毁。",
        category="技术保障",
    ),
    "§164.312(d)": ComplianceControl(
        code="§164.312(d)",
        title="人员或实体身份验证",
        description="应验证访问 ePHI 的人员或实体身份。",
        category="技术保障",
    ),
    "§164.312(e)(1)": ComplianceControl(
        code="§164.312(e)(1)",
        title="传输安全",
        description="应对电子传输的 ePHI 实施安全保护措施。",
        category="技术保障",
    ),
    "§164.312(e)(2)(ii)": ComplianceControl(
        code="§164.312(e)(2)(ii)",
        title="加密",
        description="在适当情况下应对传输中的 ePHI 实施加密。",
        category="技术保障",
    ),
}


# ============ CWE → 合规反向映射 ============
# 键统一为 "CWE-XXX" 格式;值为 4 套标准到条款编号列表的映射

CWE_TO_COMPLIANCE: Dict[str, Dict[str, List[str]]] = {
    "CWE-22": {
        "iso27001": ["A.8.25", "A.8.28"],
        "gdpr": ["Art.25", "Art.32"],
        "pci_dss": ["Req-6.4.1", "Req-7.1"],
        "hipaa": ["§164.312(a)"],
    },
    "CWE-78": {
        "iso27001": ["A.8.25", "A.8.28", "A.8.29"],
        "gdpr": ["Art.25", "Art.32"],
        "pci_dss": ["Req-6.2.4", "Req-6.4.1"],
        "hipaa": ["§164.312(a)", "§164.312(c)"],
    },
    "CWE-79": {
        "iso27001": ["A.8.25", "A.8.28", "A.8.29"],
        "gdpr": ["Art.32"],
        "pci_dss": ["Req-6.4.1", "Req-6.4.2"],
        "hipaa": ["§164.312(a)"],
    },
    "CWE-89": {
        "iso27001": ["A.8.25", "A.8.26", "A.8.28", "A.8.29"],
        "gdpr": ["Art.25", "Art.32"],
        "pci_dss": ["Req-6.2.4", "Req-6.4.1", "Req-6.4.2"],
        "hipaa": ["§164.312(a)", "§164.312(b)"],
    },
    "CWE-200": {
        "iso27001": ["A.8.25"],
        "gdpr": ["Art.32"],
        "pci_dss": ["Req-6.4.1"],
        "hipaa": ["§164.312(a)"],
    },
    "CWE-327": {
        "iso27001": ["A.8.24", "A.8.25"],
        "gdpr": ["Art.32"],
        "pci_dss": ["Req-4.1", "Req-3.1"],
        "hipaa": ["§164.312(e)(2)(ii)"],
    },
    "CWE-502": {
        "iso27001": ["A.8.25", "A.8.28", "A.8.29"],
        "gdpr": ["Art.25", "Art.32"],
        "pci_dss": ["Req-6.2.4", "Req-6.4.1"],
        "hipaa": ["§164.312(c)"],
    },
    "CWE-798": {
        "iso27001": ["A.5.17", "A.8.25"],
        "gdpr": ["Art.25", "Art.32"],
        "pci_dss": ["Req-8.1", "Req-6.4.1"],
        "hipaa": ["§164.312(a)", "§164.312(d)"],
    },
    "CWE-862": {
        "iso27001": ["A.8.25", "A.8.28"],
        "gdpr": ["Art.25", "Art.32"],
        "pci_dss": ["Req-7.1", "Req-8.1"],
        "hipaa": ["§164.312(a)", "§164.312(d)"],
    },
    "CWE-918": {
        "iso27001": ["A.8.25", "A.8.28", "A.8.29"],
        "gdpr": ["Art.32"],
        "pci_dss": ["Req-6.4.1"],
        "hipaa": ["§164.312(a)"],
    },
}


# ============ 模块常量 ============

SUPPORTED_STANDARDS: tuple = ("iso27001", "gdpr", "pci_dss", "hipaa")

STANDARD_DICTS: Dict[str, Dict[str, ComplianceControl]] = {
    "iso27001": ISO_27001_CONTROLS,
    "gdpr": GDPR_ARTICLES,
    "pci_dss": PCI_DSS_REQUIREMENTS,
    "hipaa": HIPAA_SECTIONS,
}

# 各标准在合规汇总中"covered_*"字段名映射
_COVERED_FIELD_NAMES: Dict[str, str] = {
    "iso27001": "covered_controls",
    "gdpr": "covered_articles",
    "pci_dss": "covered_requirements",
    "hipaa": "covered_sections",
}


def _normalize_cwe_id(cwe_id: str) -> str:
    """将 CWE ID 归一化为大写 "CWE-XXX" 格式

    支持传入 "cwe-89"、"CWE-89"、"CWE89" 等变体,统一输出 "CWE-89"。

    Args:
        cwe_id: CWE 编号原始输入,大小写不敏感

    Returns:
        str: 归一化后的 "CWE-XXX" 格式字符串;输入为空或不含数字时原样返回大写形式
    """
    if not cwe_id:
        return ""
    raw = cwe_id.strip().upper()
    # 兼容 "CWE89" / "CWE-89" / "CWE_89"
    if raw.startswith("CWE"):
        rest = raw[3:]
        rest = rest.lstrip("-_").lstrip()
        return f"CWE-{rest}" if rest else raw
    return raw


def get_compliance_mapping(cwe_id: str) -> Dict[str, List[str]]:
    """根据 CWE ID 返回 4 套合规标准映射

    内部对 cwe_id 归一化为大写 "CWE-XXX" 格式后查表,大小写不敏感。
    未命中映射时返回 4 个标准均为空列表的字典,保证下游使用稳定。

    Args:
        cwe_id: CWE 编号,如 "CWE-89"(大小写不敏感,内部归一化)

    Returns:
        Dict[str, List[str]]: 形如
        {"iso27001": ["A.8.25"], "gdpr": ["Art.32"], "pci_dss": ["Req-6.4.1"], "hipaa": ["§164.312(a)"]}
        若 CWE 未命中映射,返回 4 个标准均为空列表的字典
    """
    normalized = _normalize_cwe_id(cwe_id)
    mapping = CWE_TO_COMPLIANCE.get(normalized)
    if mapping is None:
        return {std: [] for std in SUPPORTED_STANDARDS}
    # 始终返回包含 4 个标准的完整字典,缺失标准补空列表
    return {std: list(mapping.get(std, [])) for std in SUPPORTED_STANDARDS}


def lookup_control(standard: str, code: str) -> Optional[ComplianceControl]:
    """查询某标准的某条款详情

    Args:
        standard: 标准名(iso27001/gdpr/pci_dss/hipaa),未知标准返回 None
        code: 条款编号(如 "A.8.25"、"Art.32"、"Req-6.2.4"、"§164.312(b)"),大小写敏感

    Returns:
        Optional[ComplianceControl]: 条款详情;标准不支持或编号未找到返回 None
    """
    controls = STANDARD_DICTS.get(standard)
    if controls is None:
        return None
    return controls.get(code)


def list_controls(standard: str) -> Dict[str, ComplianceControl]:
    """列出某标准全部条款

    Args:
        standard: 标准名(iso27001/gdpr/pci_dss/hipaa)

    Returns:
        Dict[str, ComplianceControl]: 条款编号到详情的映射;
        若标准不支持,返回空字典
    """
    controls = STANDARD_DICTS.get(standard)
    if controls is None:
        return {}
    return dict(controls)


def build_compliance_summary(issues: List[dict]) -> dict:
    """根据 issue 列表生成合规汇总(用于报告)

    遍历 issues,统计每个标准命中的 issue 数(total_findings)与去重条款列表
    (covered_*)。若 issue.compliance_mapping 为空,尝试用 issue.cwe 调用
    get_compliance_mapping 补全映射后再统计。

    Args:
        issues: issue 字典列表,每个 issue 需含 cwe 和 compliance_mapping 字段;
                compliance_mapping 缺失或为空时按 cwe 自动补全

    Returns:
        dict: 形如
        {
            "iso27001": {"total_findings": N, "covered_controls": ["A.8.25", ...]},
            "gdpr": {"total_findings": N, "covered_articles": ["Art.32", ...]},
            "pci_dss": {"total_findings": N, "covered_requirements": ["Req-6.2.4", ...]},
            "hipaa": {"total_findings": N, "covered_sections": ["§164.312(b)", ...]}
        }
        total_findings 为关联到该标准(映射非空)的 issue 数;
        covered_* 为去重后的条款列表(保持插入顺序)
    """
    # 初始化汇总结构:每个标准 {total_findings: 0, covered_*: []}
    summary: Dict[str, dict] = {
        std: {"total_findings": 0, _COVERED_FIELD_NAMES[std]: []}
        for std in SUPPORTED_STANDARDS
    }
    # 各标准 covered 去重集合,避免列表线性去重开销
    seen: Dict[str, set] = {std: set() for std in SUPPORTED_STANDARDS}

    for issue in issues or []:
        cwe = issue.get("cwe") or ""
        mapping = issue.get("compliance_mapping") or {}
        # 若 mapping 为空或缺失,尝试按 cwe 自动补全
        if not mapping and cwe:
            mapping = get_compliance_mapping(cwe)
        for std in SUPPORTED_STANDARDS:
            codes = mapping.get(std) or []
            if not codes:
                continue
            # 该标准被本 issue 命中,计入 total_findings
            summary[std]["total_findings"] += 1
            field = _COVERED_FIELD_NAMES[std]
            for code in codes:
                if code not in seen[std]:
                    seen[std].add(code)
                    summary[std][field].append(code)

    return summary
