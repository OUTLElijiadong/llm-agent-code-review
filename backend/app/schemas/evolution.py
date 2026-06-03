"""Agent 自进化模块 Pydantic Schema"""
import json
from datetime import datetime
from typing import Optional, Union

from pydantic import BaseModel, Field, field_validator


def _to_str(v):
    if v is None:
        return None
    if isinstance(v, datetime):
        return v.isoformat()
    return str(v)


def _to_json(v):
    if v is None or isinstance(v, (dict, list)):
        return v
    if isinstance(v, str):
        try:
            return json.loads(v)
        except json.JSONDecodeError:
            return None
    return None


class RunIn(BaseModel):
    """触发一轮进化"""
    window_days: int = Field(default=90, ge=1, le=3650)


class RejectIn(BaseModel):
    """驳回/回滚说明"""
    note: str = Field(default="", max_length=500)


class ProposalOut(BaseModel):
    """进化提案输出"""
    id: int
    proposal_type: str
    target_rule_id: Optional[int] = None
    title: str
    payload: Optional[Union[dict, list]] = None
    evidence: Optional[Union[dict, list]] = None
    status: str
    eval_score: Optional[Union[dict, list]] = None
    applied_rule_id: Optional[int] = None
    created_by: str
    reviewed_by: Optional[int] = None
    reviewed_at: Optional[str] = None
    note: Optional[str] = None
    create_time: Optional[str] = None

    model_config = {"from_attributes": True}

    @field_validator("payload", "evidence", "eval_score", mode="before")
    @classmethod
    def _parse_json(cls, v):
        return _to_json(v)

    @field_validator("reviewed_at", "create_time", mode="before")
    @classmethod
    def _dt(cls, v):
        return _to_str(v)


class ExperienceOut(BaseModel):
    """经验记忆输出"""
    id: int
    fingerprint: str
    language: str = "*"
    issue_type: str
    title: Optional[str] = None
    canonical_suggestion: Optional[str] = None
    accepted_count: int = 0
    rejected_count: int = 0
    weight: float = 0.0
    last_seen: Optional[str] = None
    create_time: Optional[str] = None

    model_config = {"from_attributes": True}

    @field_validator("last_seen", "create_time", mode="before")
    @classmethod
    def _dt(cls, v):
        return _to_str(v)


class EvalCaseOut(BaseModel):
    """黄金集用例输出"""
    id: int
    name: str
    language: str = "*"
    expected_issues: Optional[Union[dict, list]] = None
    tags: Optional[str] = None
    enabled: int = 1
    source: str = "seed"

    model_config = {"from_attributes": True}

    @field_validator("expected_issues", mode="before")
    @classmethod
    def _parse_json(cls, v):
        return _to_json(v)
