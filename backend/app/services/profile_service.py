"""
用户画像服务 — 让 AI 更懂每个用户

两条腿:
- 显式: 用户填写爱好/目标/技术栈/关注重点/经验水平/偏好语言
- 隐式: 从行为数据(已处理问题的类型偏好、项目语言分布、社区活跃)
        推断 derived_stats 与 derived_summary,需用户开启 auto_learn。

画像最终经 personalization_service 注入到聊天/审查/论坛,形成个性化闭环。
"""
import json
from datetime import datetime, timezone

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models.user_profile import UserProfile

_VALID_LEVELS = ("beginner", "intermediate", "advanced")


def _now():
    return datetime.now(timezone.utc)


def get_or_create(db: Session, user_id: int) -> UserProfile:
    profile = db.query(UserProfile).filter(UserProfile.user_id == user_id).first()
    if not profile:
        profile = UserProfile(user_id=user_id, auto_learn=True)
        db.add(profile)
        db.commit()
        db.refresh(profile)
    return profile


def _parse_focus(raw) -> list:
    if not raw:
        return []
    try:
        val = json.loads(raw)
        return val if isinstance(val, list) else []
    except (json.JSONDecodeError, TypeError):
        return []


def to_dict(profile: UserProfile) -> dict:
    return {
        "user_id": profile.user_id,
        "hobbies": profile.hobbies or "",
        "goals": profile.goals or "",
        "tech_stack": profile.tech_stack or "",
        "focus_areas": _parse_focus(profile.focus_areas),
        "preferred_language": profile.preferred_language or "",
        "experience_level": profile.experience_level or "",
        "auto_learn": bool(profile.auto_learn),
        "derived_summary": profile.derived_summary or "",
        "derived_stats": json.loads(profile.derived_stats) if profile.derived_stats else {},
        "last_learned_at": profile.last_learned_at,
        "update_time": profile.update_time,
    }


def update_profile(db: Session, user_id: int, payload: dict) -> UserProfile:
    """更新显式画像字段(仅本人)"""
    profile = get_or_create(db, user_id)
    if "hobbies" in payload:
        profile.hobbies = payload["hobbies"]
    if "goals" in payload:
        profile.goals = payload["goals"]
    if "tech_stack" in payload:
        profile.tech_stack = payload["tech_stack"]
    if "focus_areas" in payload and payload["focus_areas"] is not None:
        fa = payload["focus_areas"]
        profile.focus_areas = json.dumps(fa, ensure_ascii=False) if isinstance(fa, list) else fa
    if "preferred_language" in payload:
        profile.preferred_language = payload["preferred_language"]
    if "experience_level" in payload:
        lvl = payload["experience_level"]
        profile.experience_level = lvl if lvl in _VALID_LEVELS else profile.experience_level
    if "auto_learn" in payload and payload["auto_learn"] is not None:
        profile.auto_learn = bool(payload["auto_learn"])
    db.commit()
    db.refresh(profile)
    return profile


def refresh_implicit(db: Session, user_id: int, force: bool = False) -> UserProfile:
    """从行为数据推断隐式画像,写入 derived_stats / derived_summary

    Args:
        force: 即使 auto_learn 关闭也强制刷新(用户手动点"重新学习"时为 True)
    """
    from app.models.forum_post import ForumPost
    from app.models.project import Project
    from app.models.review_issue import ReviewIssue
    from app.models.review_task import ReviewTask

    profile = get_or_create(db, user_id)
    if not profile.auto_learn and not force:
        return profile

    # 1) 已处理问题的类型偏好
    rows = (
        db.query(ReviewIssue.issue_type, ReviewIssue.status, func.count(ReviewIssue.id))
        .join(ReviewTask, ReviewIssue.task_id == ReviewTask.id)
        .filter(ReviewTask.user_id == user_id,
                ReviewIssue.status.in_(("fixed", "ignored")))
        .group_by(ReviewIssue.issue_type, ReviewIssue.status)
        .all()
    )
    fixed_by_type: dict = {}
    ignored_by_type: dict = {}
    for issue_type, status, cnt in rows:
        if status == "fixed":
            fixed_by_type[issue_type] = fixed_by_type.get(issue_type, 0) + cnt
        else:
            ignored_by_type[issue_type] = ignored_by_type.get(issue_type, 0) + cnt
    # 用户最在意(修得最多)的问题类型
    top_focus = sorted(fixed_by_type.items(), key=lambda x: x[1], reverse=True)
    top_focus_types = [t for t, _ in top_focus[:3]]
    tolerated = sorted(ignored_by_type.items(), key=lambda x: x[1], reverse=True)
    tolerated_types = [t for t, _ in tolerated[:3]]

    # 2) 项目语言分布
    lang_rows = (
        db.query(Project.language, func.count(Project.id))
        .filter(Project.user_id == user_id, Project.status != "deleted",
                Project.language.isnot(None))
        .group_by(Project.language)
        .all()
    )
    languages = {lang: cnt for lang, cnt in lang_rows if lang}
    top_languages = [language for language, _ in sorted(languages.items(), key=lambda x: x[1], reverse=True)[:3]]

    # 3) 社区活跃
    forum_posts = db.query(ForumPost).filter(
        ForumPost.user_id == user_id, ForumPost.status == "normal").count()

    stats = {
        "fixed_by_type": fixed_by_type,
        "ignored_by_type": ignored_by_type,
        "top_focus_types": top_focus_types,
        "tolerated_types": tolerated_types,
        "languages": languages,
        "top_languages": top_languages,
        "forum_posts": forum_posts,
    }
    profile.derived_stats = json.dumps(stats, ensure_ascii=False)
    profile.derived_summary = _build_summary(profile, stats)
    profile.last_learned_at = _now()
    db.commit()
    db.refresh(profile)
    return profile


def _build_summary(profile: UserProfile, stats: dict) -> str:
    """从显式+隐式信息合成一段中文画像摘要(确定性,无需外呼模型)"""
    parts = []
    if profile.preferred_language or stats.get("top_languages"):
        langs = profile.preferred_language or "、".join(stats.get("top_languages", []))
        if langs:
            parts.append(f"主要使用 {langs}")
    if profile.experience_level:
        level_cn = {"beginner": "入门", "intermediate": "进阶", "advanced": "资深"}
        parts.append(f"经验水平偏{level_cn.get(profile.experience_level, profile.experience_level)}")
    if stats.get("top_focus_types"):
        parts.append(f"尤其关注「{'、'.join(stats['top_focus_types'])}」类问题")
    if stats.get("tolerated_types"):
        parts.append(f"对「{'、'.join(stats['tolerated_types'])}」类相对宽容")
    if profile.goals:
        parts.append(f"目标: {profile.goals.strip()[:60]}")
    if profile.focus_areas:
        try:
            fa = json.loads(profile.focus_areas)
            if fa:
                parts.append(f"自述关注: {'、'.join(fa)}")
        except (json.JSONDecodeError, TypeError):
            pass
    if not parts:
        return "暂无足够数据生成画像,建议完善个人偏好或积累更多审查记录。"
    return "该用户" + "；".join(parts) + "。"


def get_summary_text(db: Session, user_id: int) -> str:
    """供个性化注入使用的纯文本画像;无画像返回空串"""
    profile = db.query(UserProfile).filter(UserProfile.user_id == user_id).first()
    if not profile:
        return ""
    return profile.derived_summary or ""
