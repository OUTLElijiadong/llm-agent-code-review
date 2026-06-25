"""
审查问题表ORM模型

v2 增强:新增漏洞元数据字段(owasp/cwe/evidence/exploit_scenario/references_json/confidence/source),
支持双引擎(静态规则 + LLM)漏洞识别的结构化输出。
"""
from sqlalchemy import JSON, BigInteger, Column, DateTime, Float, Index, Integer, String, Text
from sqlalchemy.dialects.mysql import LONGTEXT

from app.core.database import Base
from app.models.base import IdMixin, TimestampMixin


class ReviewIssue(Base, IdMixin, TimestampMixin):
    """审查问题 ORM 模型

    一条 ReviewIssue 对应代码审查中发现的一个问题/漏洞。
    v2 增强:支持 owasp/cwe/evidence/exploit_scenario/references/confidence/source 等漏洞元数据。
    """

    __tablename__ = "review_issue"
    __table_args__ = (
        Index("ix_review_issue_task_severity", "task_id", "severity"),
        Index("ix_review_issue_task_status", "task_id", "status"),
        Index("ix_review_issue_file", "file_id"),
        Index("ix_review_issue_create_time", "create_time"),
        Index("ix_review_issue_task_cwe", "task_id", "cwe"),
        Index("ix_review_issue_task_severity_cwe", "task_id", "severity", "cwe"),
    )

    task_id = Column(BigInteger, nullable=False)
    file_id = Column(BigInteger)
    file_name = Column(String(255), comment="冗余字段,便于报告导出")
    line_number = Column(Integer, comment="问题所在行,0表示文件级")
    end_line = Column(Integer, comment="可选区间结束行")
    issue_type = Column(String(50), nullable=False, comment="问题类型枚举")
    severity = Column(String(20), nullable=False, comment="严重/高/中/低")
    title = Column(String(200))
    description = Column(Text, nullable=False)
    suggestion = Column(Text)
    fixed_code = Column(LONGTEXT().with_variant(Text, "sqlite"))
    status = Column(String(20), nullable=False, default="unfixed", comment="unfixed/fixed/ignored/pending_review")
    handled_by = Column(BigInteger, comment="状态变更人")
    handled_at = Column(DateTime)

    # === v2 漏洞元数据字段(2026-06-25 新增)===
    owasp = Column(String(32), nullable=True, comment="OWASP 编号,如 A03:2021-Injection")
    cwe = Column(String(32), nullable=True, comment="CWE 编号,如 CWE-89")
    evidence = Column(Text, nullable=True, comment="漏洞证据代码片段(1-3 行)")
    exploit_scenario = Column(Text, nullable=True, comment="攻击场景说明,30-200 字")
    references_json = Column(JSON, nullable=True, comment="参考链接列表")
    confidence = Column(Float, nullable=True, comment="置信度 0.0-1.0")
    source = Column(String(16), nullable=True, default="llm", comment="发现来源:llm/static/regex")

    # === v3 全量漏洞元数据(2026-06-25 006 迁移新增)===
    cvss_score = Column(Float, nullable=True, comment="CVSS v3.1 基础分 0.0-10.0")
    cvss_vector = Column(String(64), nullable=True, comment="CVSS 向量,如 AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H")
    compliance_mapping = Column(JSON, nullable=True, comment='合规映射 {"iso27001":[...],"gdpr":[...],"pci_dss":[...],"hipaa":[...]}')
    remediation = Column(Text, nullable=True, comment="修复建议详细文本")
    static_rule_hits = Column(Integer, nullable=False, default=0, comment="静态规则命中次数(双引擎统计)")
