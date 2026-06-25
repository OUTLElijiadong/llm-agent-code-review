"""
审查任务和问题模块Pydantic Schema
"""
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field


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
    """审查任务详情"""
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
    summary: Optional[str] = None
    model_name: Optional[str] = None
    duration_ms: int
    start_time: Optional[datetime] = None
    end_time: Optional[datetime] = None
    create_time: datetime
    files: list[TaskFileOut] = Field(default_factory=list)

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

    # === v2 漏洞元数据 ===
    owasp: Optional[str] = None
    cwe: Optional[str] = None
    evidence: Optional[str] = None
    exploit_scenario: Optional[str] = None
    references_json: Optional[list] = None
    confidence: Optional[float] = None
    source: Optional[str] = None

    # === v3 全量漏洞元数据 ===
    cvss_score: Optional[float] = None
    cvss_vector: Optional[str] = None
    compliance_mapping: Optional[dict] = None
    remediation: Optional[str] = None
    static_rule_hits: int = 0

    model_config = {"from_attributes": True}


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
