"""Request and response schemas for the declarative Agent Studio."""

from datetime import datetime
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.schemas.common import StrictInputModel
from app.utils.input_validation import normalize_plain_text, validate_json_payload

SkillType = Literal["llm_transform", "readonly_tool", "agent_delegate", "sequence_workflow"]


def _validate_text(value: str, *, field_name: str, allow_empty: bool = False, allow_newlines: bool = True) -> str:
    return normalize_plain_text(
        value,
        field_name=field_name,
        allow_empty=allow_empty,
        allow_newlines=allow_newlines,
    )


class AgentCreateIn(StrictInputModel):
    code: str = Field(min_length=3, max_length=80, pattern=r"^[a-z][a-z0-9_]*$")
    name: str = Field(min_length=2, max_length=120)
    description: str = Field(default="", max_length=2000)
    prompt: str = Field(min_length=20, max_length=30000)
    review_focus: str = Field(min_length=2, max_length=4000)
    model_config_json: dict[str, Any] = Field(default_factory=dict)

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        return _validate_text(value, field_name="Agent 名称", allow_newlines=False)

    @field_validator("description")
    @classmethod
    def validate_description(cls, value: str) -> str:
        return _validate_text(value, field_name="Agent 描述", allow_empty=True)

    @field_validator("prompt", "review_focus")
    @classmethod
    def validate_agent_text(cls, value: str) -> str:
        return _validate_text(value, field_name="Agent 配置文本")

    @field_validator("model_config_json")
    @classmethod
    def validate_model_config(cls, value: dict[str, Any]) -> dict[str, Any]:
        return validate_json_payload(
            value,
            field_name="Agent 模型配置",
            max_bytes=32_768,
            max_depth=8,
            max_items=256,
        )


class AgentReviseIn(StrictInputModel):
    prompt: str = Field(min_length=20, max_length=30000)
    review_focus: str = Field(min_length=2, max_length=4000)
    model_config_json: dict[str, Any] = Field(default_factory=dict)
    note: str = Field(default="", max_length=500)

    @field_validator("prompt", "review_focus")
    @classmethod
    def validate_agent_text(cls, value: str) -> str:
        return _validate_text(value, field_name="Agent 配置文本")

    @field_validator("note")
    @classmethod
    def validate_note(cls, value: str) -> str:
        return _validate_text(value, field_name="Agent 修订备注", allow_empty=True)

    @field_validator("model_config_json")
    @classmethod
    def validate_model_config(cls, value: dict[str, Any]) -> dict[str, Any]:
        return validate_json_payload(
            value,
            field_name="Agent 模型配置",
            max_bytes=32_768,
            max_depth=8,
            max_items=256,
        )


class SkillCreateIn(StrictInputModel):
    code: str = Field(min_length=3, max_length=100, pattern=r"^[a-z][a-z0-9_]*$")
    name: str = Field(min_length=2, max_length=120)
    description: str = Field(default="", max_length=2000)
    skill_type: SkillType
    definition: dict[str, Any]
    requested_capabilities: list[str] = Field(default_factory=list, max_length=20)

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        return _validate_text(value, field_name="Skill 名称", allow_newlines=False)

    @field_validator("description")
    @classmethod
    def validate_description(cls, value: str) -> str:
        return _validate_text(value, field_name="Skill 描述", allow_empty=True)

    @field_validator("definition")
    @classmethod
    def validate_definition(cls, value: dict[str, Any]) -> dict[str, Any]:
        return validate_json_payload(
            value,
            field_name="Skill 定义",
            max_bytes=131_072,
            max_depth=12,
            max_items=1_000,
        )

    @field_validator("requested_capabilities")
    @classmethod
    def validate_capabilities(cls, value: list[str]) -> list[str]:
        return [
            _validate_text(item, field_name="Skill 申请能力", allow_newlines=False)
            for item in value
        ]


class SkillReviseIn(StrictInputModel):
    skill_type: SkillType
    definition: dict[str, Any]
    requested_capabilities: list[str] = Field(default_factory=list, max_length=20)
    note: str = Field(default="", max_length=500)

    @field_validator("definition")
    @classmethod
    def validate_definition(cls, value: dict[str, Any]) -> dict[str, Any]:
        return validate_json_payload(
            value,
            field_name="Skill 定义",
            max_bytes=131_072,
            max_depth=12,
            max_items=1_000,
        )

    @field_validator("requested_capabilities")
    @classmethod
    def validate_capabilities(cls, value: list[str]) -> list[str]:
        return [
            _validate_text(item, field_name="Skill 申请能力", allow_newlines=False)
            for item in value
        ]

    @field_validator("note")
    @classmethod
    def validate_note(cls, value: str) -> str:
        return _validate_text(value, field_name="Skill 修订备注", allow_empty=True)


class SkillBindingIn(StrictInputModel):
    skill_version_id: int = Field(gt=0)
    position: int = Field(ge=0, lt=8)
    config: dict[str, Any] = Field(default_factory=dict)

    @field_validator("config")
    @classmethod
    def validate_config(cls, value: dict[str, Any]) -> dict[str, Any]:
        return validate_json_payload(
            value,
            field_name="Skill 绑定配置",
            max_bytes=32_768,
            max_depth=8,
            max_items=256,
        )


class VersionTestIn(StrictInputModel):
    sample_output: Optional[dict[str, Any]] = None

    @field_validator("sample_output")
    @classmethod
    def validate_sample_output(cls, value: Optional[dict[str, Any]]) -> Optional[dict[str, Any]]:
        if value is None:
            return None
        return validate_json_payload(
            value,
            field_name="Agent 测试样本输出",
            max_bytes=131_072,
            max_depth=12,
            max_items=1_000,
        )


class SubmitIn(StrictInputModel):
    note: str = Field(default="", max_length=500)

    @field_validator("note")
    @classmethod
    def validate_note(cls, value: str) -> str:
        return _validate_text(value, field_name="提交备注", allow_empty=True)


class DecisionIn(StrictInputModel):
    note: str = Field(default="", max_length=500)

    @field_validator("note")
    @classmethod
    def validate_note(cls, value: str) -> str:
        return _validate_text(value, field_name="审批备注", allow_empty=True)


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
    version_id: int
    version_number: int
    release_id: int
    skills: list[dict[str, Any]]

    @field_validator("description", mode="before")
    @classmethod
    def normalize_description(cls, value):
        return value or ""


class CatalogInvokeIn(StrictInputModel):
    code: str = Field(min_length=1, max_length=200_000)
    language: str = Field(default="plaintext", max_length=40)
    file_name: str = Field(default="snippet.txt", max_length=255)
    rules: list[dict[str, Any]] = Field(default_factory=list, max_length=100)
    line_offset: int = Field(default=0, ge=0, le=10_000_000)
    experience: str = Field(default="", max_length=12_000)

    @field_validator("code")
    @classmethod
    def validate_code(cls, value: str) -> str:
        # 源码前后空白对行号和证据有意义：只校验，不改写原值。
        _validate_text(value, field_name="待审查代码")
        return value

    @field_validator("language", "file_name")
    @classmethod
    def validate_metadata(cls, value: str) -> str:
        return _validate_text(value, field_name="代码元数据", allow_newlines=False)

    @field_validator("experience")
    @classmethod
    def validate_experience(cls, value: str) -> str:
        return _validate_text(value, field_name="审查经验", allow_empty=True)

    @field_validator("rules")
    @classmethod
    def validate_rules(cls, value: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return validate_json_payload(
            value,
            field_name="审查规则",
            max_bytes=131_072,
            max_depth=12,
            max_items=1_000,
        )
