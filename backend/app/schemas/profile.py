"""
用户画像 Pydantic Schema
"""
from datetime import datetime
from typing import List, Optional

from pydantic import BaseModel, Field


class ProfileUpdateIn(BaseModel):
    """更新显式画像(全部可选,部分更新)"""
    hobbies: Optional[str] = Field(default=None, max_length=2000)
    goals: Optional[str] = Field(default=None, max_length=2000)
    tech_stack: Optional[str] = Field(default=None, max_length=2000)
    focus_areas: Optional[List[str]] = None
    preferred_language: Optional[str] = Field(default=None, max_length=50)
    experience_level: Optional[str] = Field(
        default=None, pattern="^(beginner|intermediate|advanced)$")
    auto_learn: Optional[bool] = None


class ProfileOut(BaseModel):
    user_id: int
    hobbies: str = ""
    goals: str = ""
    tech_stack: str = ""
    focus_areas: List[str] = []
    preferred_language: str = ""
    experience_level: str = ""
    auto_learn: bool = True
    derived_summary: str = ""
    derived_stats: dict = {}
    last_learned_at: Optional[datetime] = None
    update_time: Optional[datetime] = None
