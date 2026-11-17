"""小菱安全运营 API 的数据契约。"""
# ┏━ 【文件导读】app/schemas/secops.py
# ┃ 层级：数据契约层（Pydantic 请求/响应模型）
# ┃ 作用：安全运营接口的攻击研判、攻击面、候选名单、基线核查、巡检状态与处置动作数据结构。
# ┃ 引用了：pydantic、schemas/common
# ┃ 被引用：v1/secops
# ┗━ 以上导读只是注释，不影响运行；撤销方法见 docs/后端代码导读20261024/README.md

from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field, field_validator

from app.schemas.common import StrictInputModel
from app.utils.input_validation import normalize_plain_text


# ▎一个攻击源画像。
class SecOpsSourceOut(BaseModel):
    """聚合后的攻击源。"""

    ip: str
    hits: int = 0
    sensors: List[str] = Field(default_factory=list)
    kinds: List[str] = Field(default_factory=list)
    severity: str = "low"
    geo: Dict[str, Any] = Field(default_factory=dict)
    last_seen: Optional[str] = None
    recommended: str = "watch"


# ▎单个传感器的采集状态。
class SecOpsSensorOut(BaseModel):
    """传感器状态。"""

    action: str
    ok: bool = False
    events: int = 0
    error: Optional[str] = None


# ▎研判总量。
class SecOpsTotalsOut(BaseModel):
    """研判总量。"""

    sources: int = 0
    critical: int = 0
    high: int = 0


# ▎攻击研判结果。
class SecOpsDigestOut(BaseModel):
    """多源攻击研判结果。"""

    window_hours: int = 24
    generated_at: str = ""
    sensors: List[SecOpsSensorOut] = Field(default_factory=list)
    totals: SecOpsTotalsOut = Field(default_factory=SecOpsTotalsOut)
    sources: List[SecOpsSourceOut] = Field(default_factory=list)
    recommendations: List[str] = Field(default_factory=list)


# ▎攻击面盘点结果（结构由宿主机执行器决定，保持透传）。
class SecOpsSurfaceOut(BaseModel):
    """自身暴露面盘点。"""

    available: bool = False
    surface: Dict[str, Any] = Field(default_factory=dict)


# ▎封禁候选。
class SecOpsCandidatesOut(BaseModel):
    """自动封禁候选名单。"""

    available: bool = False
    candidates: List[Dict[str, Any]] = Field(default_factory=list)
    raw: Optional[Dict[str, Any]] = None


# ▎单项基线检查。
class SecOpsBaselineCheckOut(BaseModel):
    """基线检查项。"""

    key: str
    title: str
    passed: Optional[bool] = None
    detail: str = ""
    remediation: str = ""
    weight: int = 1


# ▎基线核查结果。
class SecOpsBaselineOut(BaseModel):
    """平台自身安全基线。"""

    generated_at: str = ""
    base_url: Optional[str] = None
    score: int = 0
    checks: List[SecOpsBaselineCheckOut] = Field(default_factory=list)
    failed: List[str] = Field(default_factory=list)
    remediations: List[str] = Field(default_factory=list)


# ▎巡检状态。
class SecOpsPatrolOut(BaseModel):
    """自动巡检状态。"""

    auto_block_enabled: bool = False
    patrol_enabled: bool = True
    public_base_url: Optional[str] = None
    asset_hosts: List[str] = Field(default_factory=list)
    last_run: Dict[str, Any] = Field(default_factory=dict)
    recent: List[Dict[str, Any]] = Field(default_factory=list)


# ▎工具目录条目。
class SecOpsToolOut(BaseModel):
    """安全运营工具条目。"""

    tool: str
    risk: str
    description: str


# ▎工具目录。
class SecOpsToolCatalogOut(BaseModel):
    """安全运营工具目录。"""

    tools: List[SecOpsToolOut] = Field(default_factory=list)


# ▎封禁请求。
class SecOpsBlockIn(StrictInputModel):
    """封禁一个来源。"""

    ip: str = Field(min_length=3, max_length=64)
    reason: str = Field(default="", max_length=200)
    duration_seconds: int = Field(default=3600, ge=60, le=86_400)
    confirm: bool = False

    @field_validator("ip")
    @classmethod
    def validate_ip(cls, value: str) -> str:
        return normalize_plain_text(value, field_name="来源 IP", allow_empty=False)


# ▎解封请求。
class SecOpsReleaseIn(StrictInputModel):
    """解封一个来源。"""

    ip: str = Field(min_length=3, max_length=64)
    reason: str = Field(default="", max_length=200)


# ▎处置结果。
class SecOpsBlockOut(BaseModel):
    """封禁/解封结果。"""

    ip: str
    duration_seconds: int = 0
    blocked: bool = False
    released: bool = False
    result: Dict[str, Any] = Field(default_factory=dict)
    configure: Dict[str, Any] = Field(default_factory=dict)


# ▎启动授权渗透的请求体（占位：目前无需额外参数，保留扩展位）。
class SecOpsPentestIn(StrictInputModel):
    """启动已授权渗透测试。"""

    note: str = Field(default="", max_length=200)


# ▎启动结果。
class SecOpsPentestOut(BaseModel):
    """渗透测试启动结果。"""

    public_id: str
    started: bool = False
    engagement: Dict[str, Any] = Field(default_factory=dict)
