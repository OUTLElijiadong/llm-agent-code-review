"""
代码文件模块Pydantic Schema
"""
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field


class CodeFileIn(BaseModel):
    """在线新增代码文件请求体"""
    project_id: int
    file_name: str = Field(min_length=1, max_length=255)
    file_path: Optional[str] = Field(default=None, max_length=500)
    language: Optional[str] = Field(default=None, max_length=50)
    content: str = Field(min_length=0)


class CodeFileUpdateIn(BaseModel):
    """更新代码内容请求体"""
    content: str
    change_desc: Optional[str] = Field(default=None, max_length=255)


class RenameIn(BaseModel):
    """重命名文件请求体"""
    file_name: str = Field(min_length=1, max_length=255)
    file_path: Optional[str] = Field(default=None, max_length=500)


class CodeFileOut(BaseModel):
    """代码文件列表项响应"""
    id: int
    project_id: int
    file_name: str
    file_path: Optional[str] = None
    language: str
    size_bytes: int
    line_count: int
    version_no: int
    create_time: datetime
    update_time: datetime

    model_config = {"from_attributes": True}


class CodeFileDetailOut(CodeFileOut):
    """代码文件详情响应(含内容)"""
    content: str
