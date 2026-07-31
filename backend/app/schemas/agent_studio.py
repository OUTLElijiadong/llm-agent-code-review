"""Request and response schemas for the declarative Agent Studio."""

from datetime import datetime
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

SkillType = Literal["llm_transform", "readonly_tool", "agent_delegate", "sequence_workflow"]


class AgentCreateIn(BaseModel):
    code: str = Field(min_length=3, max_length=80, pattern=r"^[a-z][a-z0-9_]*$")
    name: str = Field(min_length=2, max_length=120)
    description: str = Field(default="", max_length=2000)
    prompt: str = Field(min_length=20, max_length=30000)
    review_focus: str = Field(min_length=2, max_length=4000)
    model_config_json: dict[str, Any] = Field(default_factory=dict)


class AgentReviseIn(BaseModel):
    prompt: str = Field(min_length=20, max_length=30000)
    review_focus: str = Field(min_length=2, max_length=4000)
    model_config_json: dict[str, Any] = Field(default_factory=dict)
    note: str = Field(default="", max_length=500)


class SkillCreateIn(BaseModel):
    code: str = Field(min_length=3, max_length=100, pattern=r"^[a-z][a-z0-9_]*$")
    name: str = Field(min_length=2, max_length=120)
    description: str = Field(default="", max_length=2000)
    skill_type: SkillType
    definition: dict[str, Any]
    requested_capabilities: list[str] = Field(default_factory=list, max_length=20)


class SkillReviseIn(BaseModel):
    skill_type: SkillType
    definition: dict[str, Any]
    requested_capabilities: list[str] = Field(default_factory=list, max_length=20)
    note: str = Field(default="", max_length=500)


class SkillBindingIn(BaseModel):
    skill_version_id: int = Field(gt=0)
    position: int = Field(ge=0, lt=8)
    config: dict[str, Any] = Field(default_factory=dict)


class VersionTestIn(BaseModel):
    sample_output: Optional[dict[str, Any]] = None


class SubmitIn(BaseModel):
    note: str = Field(default="", max_length=500)


class DecisionIn(BaseModel):
    note: str = Field(default="", max_length=500)


class AdminReviseIn(AgentReviseIn):
    pass


class AssetOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    code: str
    name: str
    description: Optional[str] = None
    owner_id: int
    status: str
    current_published_version_id: Optional[int] = None
    is_enabled: Optional[int] = None
    create_time: datetime
    update_time: datetime


class VersionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    version_number: int
    checksum: str
    status: str
    original_author_id: int
    revised_by: Optional[int] = None
    revision_note: Optional[str] = None
    test_evidence_json: Optional[str] = None
    create_time: datetime
    update_time: datetime


class ReleaseOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    agent_id: int
    agent_version_id: int
    approval_id: Optional[int] = None
    previous_release_id: Optional[int] = None
    rollback_of_release_id: Optional[int] = None
    package_checksum: str
    status: str
    published_by: int
    published_at: datetime
    disabled_at: Optional[datetime] = None


class CatalogAgentOut(BaseModel):
    id: int
    code: str
    name: str
    description: str
    owner_id: int
    version_id: int
    version_number: int
    release_id: int
    skills: list[dict[str, Any]]

    @field_validator("description", mode="before")
    @classmethod
    def normalize_description(cls, value):
        return value or ""


class CatalogInvokeIn(BaseModel):
    code: str = Field(min_length=1, max_length=200_000)
    language: str = Field(default="plaintext", max_length=40)
    file_name: str = Field(default="snippet.txt", max_length=255)
    rules: list[dict[str, Any]] = Field(default_factory=list, max_length=100)
    line_offset: int = Field(default=0, ge=0, le=10_000_000)
    experience: str = Field(default="", max_length=12_000)
