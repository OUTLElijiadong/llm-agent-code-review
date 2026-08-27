"""
审查任务和问题模块Pydantic Schema
"""
from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, Field, field_validator

from app.schemas.agent_governance import parse_json_value


class ReviewStartIn(BaseModel):
    """启动审查请求体"""
    project_id: int
    file_ids: list[int] = Field(min_length=1)
    review_type: Optional[str] = Field(default="standard", pattern="^(quick|standard|security|performance|full)$")
    task_name: Optional[str] = Field(default=None, max_length=100)


class TaskOut(BaseModel):
    """审查任务列表项"""
    id: int
    task_name: Optional[str] = None
    project_id: int
    project_name: str = ""
    review_type: str
    status: str
    total_files: int
    total_issues: int
    severe_issues: int
    high_issues: int
    medium_issues: int
    low_issues: int
    score: int
    score_version: Optional[str] = None
    score_breakdown: Optional[dict] = None
    duration_ms: int
    create_time: datetime

    model_config = {"from_attributes": True}


class TaskFileOut(BaseModel):
    """审查任务关联文件"""
    file_id: int
    project_id: int
    file_name: str
    file_path: Optional[str] = None
    language: str
    line_count: int
    version_no: int


class TaskDetailOut(BaseModel):
    """审查任务详情

    R4 修复(2026-06-25):新增 error_message 字段,
    任务失败时前端可展示失败原因(对齐 ReviewTask ORM)。
    """
    id: int
    task_name: Optional[str] = None
    project_id: int
    project_name: str = ""
    review_type: str
    status: str
    total_files: int
    processed_files: int
    total_issues: int
    severe_issues: int
    high_issues: int
    medium_issues: int
    low_issues: int
    score: int
    score_version: Optional[str] = None
    score_breakdown: Optional[dict] = None
    summary: Optional[str] = None
    model_name: Optional[str] = None
    duration_ms: int
    start_time: Optional[datetime] = None
    end_time: Optional[datetime] = None
    create_time: datetime
    # R4 修复:任务失败时返回错误原因,对齐 ReviewTask.error_message
    error_message: Optional[str] = None
    files: list[TaskFileOut] = Field(default_factory=list)
    agent_releases: list[dict[str, Any]] = Field(default_factory=list)

    model_config = {"from_attributes": True}


class ProgressOut(BaseModel):
    """审查进度"""
    status: str
    total_files: int
    processed_files: int
    current_file: Optional[str] = None


class IssueOut(BaseModel):
    """审查问题输出

    含 v2/v3 全量漏洞元数据:owasp/cwe/cvss/compliance_mapping/evidence/
    exploit_scenario/remediation/references/confidence/source/static_rule_hits。
    R2 修复(2026-06-25):补齐 handled_by/handled_at/update_time,
    使前端能展示问题处理人和处理时间。
    """
    id: int
    task_id: int
    file_id: Optional[int] = None
    file_name: Optional[str] = None
    line_number: Optional[int] = None
    end_line: Optional[int] = None
    issue_type: str
    severity: str
    title: Optional[str] = None
    description: str
    suggestion: Optional[str] = None
    fixed_code: Optional[str] = None
    status: str
    create_time: datetime
    # R2 修复:补齐处理人/处理时间/更新时间,与 ORM ReviewIssue 字段对齐
    handled_by: Optional[int] = None
    handled_at: Optional[datetime] = None
    update_time: Optional[datetime] = None

    # === v2 漏洞元数据 ===
    owasp: Optional[str] = None
    cwe: Optional[str] = None
    evidence: Optional[str] = None
    exploit_scenario: Optional[str] = None
    references_json: Optional[list] = None
    confidence: Optional[float] = None
    source: Optional[str] = None
    source_details: Optional[list[dict]] = None
    confirmation_count: int = 1
    finding_fingerprint: Optional[str] = None

    # === v3 全量漏洞元数据 ===
    cvss_score: Optional[float] = None
    cvss_vector: Optional[str] = None
    cvss_version: Optional[str] = None
    cvss_source: Optional[str] = None
    compliance_mapping: Optional[dict] = None
    remediation: Optional[str] = None
    static_rule_hits: int = 0

    model_config = {"from_attributes": True}

    @field_validator("references_json", mode="before")
    @classmethod
    def parse_references_json(cls, value: Any) -> Optional[list]:
        """解析 references_json 字段,防御 JSON 字符串边缘情况。

        ORM JSON 列类型通常返回 Python 原生 list,但若数据通过直接 SQL
        写入 JSON 字符串,此处确保正确解析为 list(与 agent_governance.py 模式一致)。

        Args:
            value: 数据库字段值,可能是 list/str/None。

        Returns:
            Optional[list]: 解析后的列表;非 list 类型返回 None。
        """
        parsed = parse_json_value(value)
        return parsed if isinstance(parsed, list) else None

    @field_validator("compliance_mapping", mode="before")
    @classmethod
    def parse_compliance_mapping(cls, value: Any) -> Optional[dict]:
        """解析 compliance_mapping 字段,防御 JSON 字符串边缘情况。

        Args:
            value: 数据库字段值,可能是 dict/str/None。

        Returns:
            Optional[dict]: 解析后的字典;非 dict 类型返回 None。
        """
        parsed = parse_json_value(value)
        return parsed if isinstance(parsed, dict) else None


class IssueListItemOut(IssueOut):
    """问题列表项: 在 IssueOut 基础上冗余 project / task 信息,便于跨任务表格展示"""
    project_id: int
    project_name: str = ""
    task_name: Optional[str] = None


class IssueStatusIn(BaseModel):
    """更新问题状态请求体"""
    status: str = Field(pattern="^(unfixed|fixed|ignored|pending_review)$")
    note: Optional[str] = Field(default=None, max_length=500)


class IssueBatchStatusIn(BaseModel):
    """批量更新问题状态请求体"""
    ids: list[int]
    status: str = Field(pattern="^(unfixed|fixed|ignored|pending_review)$")
