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
    """代码文件列表项响应

    R3 修复(2026-06-25):补齐 status/raw_size 字段,对齐 CodeFile ORM,
    前端可显示文件状态和真实大小(含二进制文件原始字节数)。
    """
    id: int
    project_id: int
    file_name: str
    file_path: Optional[str] = None
    language: str
    size_bytes: int
    line_count: int
    version_no: int
    is_binary: int = 0
    # R3 修复:补齐 status/raw_size,对齐 CodeFile ORM 与 CodeFileMetaOut
    status: str = "active"
    raw_size: int = 0
    create_time: datetime
    update_time: datetime

    model_config = {"from_attributes": True}


class CodeFileDetailOut(CodeFileOut):
    """代码文件详情响应(含内容)

    v2: 二进制文件(is_binary=1)的 content 字段为空字符串,
    前端通过 is_binary=1 判断,改用下载接口获取原文件。
    v3: 新增 md5_hash/sha256_hash 字段(后端实时计算,不入库),
    用于前端二进制文件展示卡片显示文件摘要。
    """
    content: str
    md5_hash: Optional[str] = None
    sha256_hash: Optional[str] = None


class CodeFileMetaOut(BaseModel):
    """代码文件元信息响应(不含内容)

    v3 新增:用于二进制文件展示提示卡片时,通过此接口获取文件元数据
    (MD5/SHA-256/MIME 类型等),避免下载完整文件内容。
    所有 v3 字段均为 Optional,后端实时计算或推断,不入库存储。
    """
    id: int
    file_name: str
    file_path: Optional[str] = None
    language: str
    size_bytes: int
    raw_size: int = 0
    line_count: int
    version_no: int
    is_binary: int = 0
    mime_type: Optional[str] = None
    md5_hash: Optional[str] = None
    sha256_hash: Optional[str] = None
    create_time: datetime
    update_time: datetime

    model_config = {"from_attributes": True}
