"""
代码版本模块Pydantic Schema
"""
from datetime import datetime
from typing import Optional

from pydantic import BaseModel


class VersionOut(BaseModel):
    """版本列表项响应"""
    version_no: int
    change_desc: Optional[str] = None
    operator_id: Optional[int] = None
    create_time: datetime

    model_config = {"from_attributes": True}


class VersionDetailOut(BaseModel):
    """版本详情响应(含代码内容)"""
    file_id: int
    version_no: int
    content: str
    change_desc: Optional[str] = None
    create_time: datetime

    model_config = {"from_attributes": True}
