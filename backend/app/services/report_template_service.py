"""报告模板管理服务 (T12)

提供报告模板的 CRUD 与内置模板载入能力,供报告生成 API 调用。
内置模板(is_builtin=1)不可删除,自定义模板(is_builtin=0)可由创建者或管理员管理。

依赖:
- T01 ReportTemplate ORM: name / type / content / is_builtin / creator_id / description
- T01 ReportTemplateIn / ReportTemplateOut Schema
- T11 report_exporter.load_builtin_template: 文件系统降级载入

设计要点:
1. 内置模板删除保护: delete_template 检测 is_builtin=1 时抛 ValueError
2. 内置模板内容载入优先级: 数据库 > 文件系统(app/templates/*.j2)
3. 模板类型筛选: list_templates 支持按 type 过滤(simple/detailed/compliance/custom)
"""
from __future__ import annotations

from typing import List, Optional

from sqlalchemy.orm import Session

from app.core.exceptions import NotFoundError
from app.models.report_template import ReportTemplate
from app.schemas.report_template import ReportTemplateIn, ReportTemplateUpdate
from app.services.report_exporter import load_builtin_template

# ============ 查询接口 ============

def list_templates(db: Session, template_type: Optional[str] = None) -> List[ReportTemplate]:
    """列出全部报告模板,可按类型筛选。

    返回结果按 id 升序排列(内置模板 id 较小,自然排在前面)。

    Args:
        db: 数据库会话。
        template_type: 模板类型筛选(simple/detailed/compliance/custom),
            为 None 时返回全部模板。

    Returns:
        List[ReportTemplate]: 模板 ORM 对象列表,按 id 升序排列;
            无匹配时返回空列表。
    """
    query = db.query(ReportTemplate)
    if template_type:
        query = query.filter(ReportTemplate.type == template_type)
    return query.order_by(ReportTemplate.id.asc()).all()


def get_template(db: Session, template_id: int) -> ReportTemplate:
    """按主键获取单个报告模板。

    Args:
        db: 数据库会话。
        template_id: 模板主键 ID。

    Returns:
        ReportTemplate: 模板 ORM 对象。

    Raises:
        NotFoundError: 模板不存在(code=40400)。
    """
    template = db.get(ReportTemplate, template_id)
    if not template:
        raise NotFoundError(f"报告模板 #{template_id} 不存在", code=40400)
    return template


def get_template_by_code(db: Session, template_code: str) -> ReportTemplate:
    """按模板类型编码(type 字段)获取模板。

    注:ReportTemplate ORM 无独立 code 字段,以 type(simple/detailed/compliance/custom)
    作为模板编码。内置模板的 type 唯一,自定义模板 type=custom 可能有多条。

    Args:
        db: 数据库会话。
        template_code: 模板类型编码(如 simple/detailed/compliance)。

    Returns:
        ReportTemplate: 第一条匹配 type 的模板 ORM 对象。

    Raises:
        NotFoundError: 指定类型的模板不存在(code=40400)。
    """
    template = (
        db.query(ReportTemplate)
        .filter(ReportTemplate.type == template_code)
        .order_by(ReportTemplate.id.asc())
        .first()
    )
    if not template:
        raise NotFoundError(f"类型为 '{template_code}' 的报告模板不存在", code=40400)
    return template


# ============ 写入接口 ============

def create_template(db: Session, template_in: ReportTemplateIn, creator_id: Optional[int] = None) -> ReportTemplate:
    """创建自定义报告模板。

    自定义模板 is_builtin=0,可被创建者或管理员后续修改/删除。

    Args:
        db: 数据库会话。
        template_in: 模板创建请求体(name/type/content/description)。
        creator_id: 创建者用户 ID,可选(系统预置时为 None)。

    Returns:
        ReportTemplate: 已入库的模板 ORM 对象(含自增 id 与时间戳)。
    """
    template = ReportTemplate(
        name=template_in.name,
        type=template_in.type,
        content=template_in.content,
        is_builtin=0,
        creator_id=creator_id,
        description=template_in.description,
    )
    db.add(template)
    db.commit()
    db.refresh(template)
    return template


def update_template(db: Session, template_id: int, template_in: ReportTemplateUpdate) -> ReportTemplate:
    """更新报告模板(内置模板亦可修改内容,但 is_builtin 不可变)。

    仅更新 template_in 中非 None 的字段,未提供字段保持原值。

    Args:
        db: 数据库会话。
        template_id: 模板主键 ID。
        template_in: 模板更新请求体(全部字段可选)。

    Returns:
        ReportTemplate: 更新后的模板 ORM 对象。

    Raises:
        NotFoundError: 模板不存在(code=40400)。
    """
    template = get_template(db, template_id)
    update_data = template_in.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        setattr(template, field, value)
    db.commit()
    db.refresh(template)
    return template


def delete_template(db: Session, template_id: int) -> None:
    """删除报告模板(系统内置模板不可删除)。

    Args:
        db: 数据库会话。
        template_id: 模板主键 ID。

    Returns:
        None

    Raises:
        NotFoundError: 模板不存在(code=40400)。
        ValueError: 试图删除内置模板(is_builtin=1)时抛出。
    """
    template = get_template(db, template_id)
    if template.is_builtin == 1:
        raise ValueError(f"内置模板 '#{template_id}'({template.name})不可删除")
    db.delete(template)
    db.commit()


# ============ 内置模板载入 ============

def get_builtin_template_content(db: Session, template_type: str) -> str:
    """获取内置模板内容(优先数据库,降级到文件系统)。

    载入优先级:
        1. 数据库 report_template 表中 type=template_type 且 is_builtin=1 的记录
        2. 文件系统 app/templates/report_{type}.html.j2(T11 load_builtin_template)

    Args:
        db: 数据库会话。
        template_type: 模板类型(simple/detailed/compliance)。

    Returns:
        str: Jinja2 模板字符串。

    Raises:
        NotFoundError: 数据库与文件系统均未找到该类型模板(code=40400)。
    """
    # 优先从数据库载入
    db_template = (
        db.query(ReportTemplate)
        .filter(
            ReportTemplate.type == template_type,
            ReportTemplate.is_builtin == 1,
        )
        .first()
    )
    if db_template and db_template.content:
        return db_template.content

    # 降级到文件系统
    try:
        return load_builtin_template(template_type)
    except FileNotFoundError:
        raise NotFoundError(
            f"内置模板 '{template_type}' 在数据库与文件系统中均不存在",
            code=40400,
        )
