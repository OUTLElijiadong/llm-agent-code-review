"""
审查规则服务模块
"""
from sqlalchemy.orm import Session

from app.core.exceptions import ConflictError, ForbiddenError, NotFoundError
from app.models.review_rule import ReviewRule
from app.models.user import User
from app.schemas.rule import RuleIn, RuleUpdateIn


def _ensure_can_mutate(user: User, rule: ReviewRule, *, allow_builtin_for_admin: bool = True) -> None:
    """规则写操作统一鉴权

    - 管理员: 可操作全部规则(内置规则的启停由管理员统一治理)。
    - 普通用户: 只能操作自己创建的自定义规则;内置/全局规则(user_id 为空)一律拒绝。

    Args:
        user: 当前操作用户。
        rule: 目标规则。
        allow_builtin_for_admin: 管理员是否可操作内置规则(启停=True,改/删=False)。

    Raises:
        ForbiddenError: 无权操作该规则。
        ConflictError: 内置规则不可改/删。
    """
    is_global = rule.is_builtin or rule.user_id is None
    if user.role == "admin":
        if is_global and not allow_builtin_for_admin:
            raise ConflictError("不可修改内置规则", code=40901)
        return
    if is_global:
        raise ForbiddenError("无权操作内置规则", code=40300)
    if rule.user_id != user.id:
        raise ForbiddenError("无权操作他人规则", code=40300)


def get_enabled_rules(db: Session, user_id: int, language: str = "") -> list[ReviewRule]:
    """获取用户视角下所有启用的规则

    返回通用规则 + 匹配语言的规则。
    规则按 severity_rank + sort_order 排序,优先展示高严重度规则。

    Args:
        db: 数据库会话
        user_id: 用户ID
        language: 编程语言(空字符串表示不按语言过滤)

    Returns:
        list[ReviewRule]: 启用的规则列表
    """
    lang = (language or "").strip().lower()
    q = db.query(ReviewRule).filter(
        ReviewRule.enabled == 1,
        (ReviewRule.user_id.is_(None)) | (ReviewRule.user_id == user_id),
    )
    if lang:
        q = q.filter(
            (ReviewRule.language == "*") | (ReviewRule.language == lang),
        )
    return q.order_by(ReviewRule.sort_order).all()


_SEVERITY_RANK = {"严重": 0, "高": 1, "中": 2, "低": 3}


def list_rules(db: Session, user_id: int) -> list[ReviewRule]:
    """获取规则列表(含内置和当前用户自定义)

    Args:
        db: 数据库会话
        user_id: 用户ID

    Returns:
        list[ReviewRule]: 规则列表
    """
    return db.query(ReviewRule).filter(
        (ReviewRule.user_id.is_(None)) | (ReviewRule.user_id == user_id)
    ).order_by(ReviewRule.sort_order).all()


def toggle_rule(db: Session, user: User, rule_id: int, enabled: int) -> None:
    """启用/禁用规则

    Args:
        db: 数据库会话
        user: 当前操作用户
        rule_id: 规则ID
        enabled: 1启用/0禁用
    """
    rule = db.get(ReviewRule, rule_id)
    if not rule:
        raise NotFoundError("规则不存在", code=40400)
    # 启停允许管理员操作内置规则(全局治理),普通用户仅限自有规则
    _ensure_can_mutate(user, rule, allow_builtin_for_admin=True)
    rule.enabled = enabled
    db.commit()


def create_rule(db: Session, user_id: int, payload: RuleIn) -> ReviewRule:
    """创建自定义规则"""
    exists = db.query(ReviewRule.id).filter(
        ReviewRule.rule_code == payload.rule_code,
        (ReviewRule.user_id == user_id) | (ReviewRule.user_id.is_(None)),
    ).first()
    if exists:
        raise ConflictError("规则代码重复", code=40901)
    max_order = db.query(ReviewRule.sort_order).order_by(ReviewRule.sort_order.desc()).first()
    sort_order = (max_order[0] + 1) if max_order else 0
    rule = ReviewRule(
        user_id=user_id, rule_code=payload.rule_code,
        rule_name=payload.rule_name, rule_type=payload.rule_type,
        rule_content=payload.rule_content, enabled=1,
        is_builtin=0, sort_order=sort_order,
        language=getattr(payload, "language", "*") or "*",
        severity=getattr(payload, "severity", "中") or "中",
    )
    db.add(rule)
    db.commit()
    db.refresh(rule)
    return rule


def update_rule(db: Session, user: User, rule_id: int, payload: RuleUpdateIn) -> None:
    """更新自定义规则"""
    rule = db.get(ReviewRule, rule_id)
    if not rule:
        raise NotFoundError("规则不存在", code=40400)
    # 内置规则任何人都不可改;自定义规则仅创建者或管理员可改
    _ensure_can_mutate(user, rule, allow_builtin_for_admin=False)
    if payload.rule_name is not None:
        rule.rule_name = payload.rule_name
    if payload.rule_type is not None:
        rule.rule_type = payload.rule_type
    if payload.rule_content is not None:
        rule.rule_content = payload.rule_content
    if payload.language is not None:
        rule.language = payload.language
    if payload.severity is not None:
        rule.severity = payload.severity
    db.commit()


def delete_rule(db: Session, user: User, rule_id: int) -> None:
    """删除自定义规则"""
    rule = db.get(ReviewRule, rule_id)
    if not rule:
        raise NotFoundError("规则不存在", code=40400)
    # 自定义规则仅创建者或管理员可删
    _ensure_can_mutate(user, rule, allow_builtin_for_admin=False)
    db.delete(rule)
    db.commit()
