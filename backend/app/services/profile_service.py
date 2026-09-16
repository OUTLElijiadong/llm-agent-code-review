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


def get_or_create(db: Session, user_id: int, *, commit: bool = True) -> UserProfile:
    profile = db.query(UserProfile).filter(UserProfile.user_id == user_id).first()
    if not profile:
        profile = UserProfile(user_id=user_id, auto_learn=True)
        db.add(profile)
        if commit:
            db.commit()
            db.refresh(profile)
        else:
            db.flush()
    return profile


def _parse_focus(raw) -> list:
    if not raw:
        return []
    try:
        val = json.loads(raw)
        return val if isinstance(val, list) else []
    except (json.JSONDecodeError, TypeError):
        return []


def _parse_stats(raw) -> dict:
    try:
        value = json.loads(raw) if raw else {}
        return value if isinstance(value, dict) else {}
    except (ValueError, TypeError):
        return {}


def read_profile(db: Session, user_id: int) -> dict:
    """读请求不初始化记录，避免首页探测产生写入。"""
    profile = db.query(UserProfile).filter(UserProfile.user_id == user_id).first()
    if profile is None:
        from app.schemas.profile import ProfileOut

        result = ProfileOut(user_id=user_id).model_dump()
    else:
        result = to_dict(profile)
    # 经常使用者无需被基础偏好问卷打断；本人成功任务是明确的使用证据。
    from app.models.agent_response_run import AgentResponseRun
    from app.models.review_task import ReviewTask

    reviews = (
        db.query(ReviewTask.id).filter(ReviewTask.user_id == user_id, ReviewTask.status == "success").limit(3).all()
    )
    conversations = (
        db.query(AgentResponseRun.id)
        .filter(AgentResponseRun.user_id == user_id, AgentResponseRun.status == "completed")
        .limit(3)
        .all()
    )
    result["should_prompt"] = len(reviews) + len(conversations) < 3
    return result


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
        "derived_stats": _parse_stats(profile.derived_stats),
        "last_learned_at": profile.last_learned_at,
        "preference_prompted": int(profile.preference_prompted or 0),
        "preference_prompted_at": profile.preference_prompted_at,
        "update_time": profile.update_time,
    }


def mark_preference_prompted(db: Session, user_id: int, state: int) -> UserProfile:
    """记录小菱偏好询问结果(1=已答 2=跳过),防止反复打扰。"""
    if state not in (1, 2):
        from app.core.exceptions import ValidationError

        raise ValidationError("无效的偏好询问状态", code=40001)
    profile = get_or_create(db, user_id)
    profile.preference_prompted = state
    profile.preference_prompted_at = _now()
    db.commit()
    db.refresh(profile)
    return profile


def update_profile(db: Session, user_id: int, payload: dict) -> UserProfile:
    """更新显式画像字段(仅本人)"""
    profile = get_or_create(db, user_id, commit=False)
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
        profile.experience_level = lvl if lvl in (*_VALID_LEVELS, "") else profile.experience_level
    if "auto_learn" in payload and payload["auto_learn"] is not None:
        profile.auto_learn = bool(payload["auto_learn"])
    if payload.get("preference_prompted") in (1, 2):
        profile.preference_prompted = payload["preference_prompted"]
        profile.preference_prompted_at = _now()
    if payload.get("clear_learned"):
        profile.derived_stats = None
        profile.last_learned_at = None
    profile.derived_summary = _build_summary(profile, _parse_stats(profile.derived_stats) if profile.auto_learn else {})
    _sync_private_snapshot(db, profile)
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
    from app.models.user import User
    from app.services.project_member_service import get_visible_project_ids

    profile = get_or_create(db, user_id)
    if not profile.auto_learn and not force:
        return profile

    # 1) 已处理问题的类型偏好
    rows = (
        db.query(ReviewIssue.issue_type, ReviewIssue.status, func.count(ReviewIssue.id))
        .join(ReviewTask, ReviewIssue.task_id == ReviewTask.id)
        .filter(ReviewTask.user_id == user_id, ReviewIssue.status.in_(("fixed", "ignored")))
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
    user = db.get(User, user_id)
    visible_ids, _scope = get_visible_project_ids(db, user) if user else ([], "self")
    lang_rows = (
        db.query(Project.language, func.count(Project.id))
        .filter(Project.user_id == user_id, Project.id.in_(visible_ids), Project.language.isnot(None))
        .group_by(Project.language)
        .all()
    )
    languages = {lang: cnt for lang, cnt in lang_rows if lang}
    top_languages = [language for language, _ in sorted(languages.items(), key=lambda x: x[1], reverse=True)[:3]]

    # 3) 社区活跃
    forum_posts = db.query(ForumPost).filter(ForumPost.user_id == user_id, ForumPost.status == "normal").count()

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
    _sync_private_snapshot(db, profile)
    db.commit()
    db.refresh(profile)
    return profile


def _build_summary(profile: UserProfile, stats: dict) -> str:
    """从显式+隐式信息合成一段中文画像摘要(确定性,无需外呼模型)"""
    parts = []
    if profile.hobbies:
        parts.append(f"自述兴趣: {profile.hobbies.strip()[:300]}")
    if profile.tech_stack:
        parts.append(f"自述技术栈: {profile.tech_stack.strip()[:300]}")
    if profile.preferred_language or stats.get("top_languages"):
        langs = profile.preferred_language or "、".join(stats.get("top_languages", []))
        if langs:
            parts.append(f"主要使用 {langs}")
    if profile.experience_level:
        level_cn = {"beginner": "入门", "intermediate": "进阶", "advanced": "资深"}
        parts.append(f"经验水平偏{level_cn.get(profile.experience_level, profile.experience_level)}")
    if stats.get("top_focus_types"):
        parts.append(f"历史修复较多的类型「{'、'.join(stats['top_focus_types'])}」类问题（行为统计，非用户自述）")
    if stats.get("tolerated_types"):
        parts.append(f"历史忽略较多的类型「{'、'.join(stats['tolerated_types'])}」类（不推断兴趣或放宽安全要求）")
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
    if not profile.auto_learn:
        return _build_summary(profile, {})
    return profile.derived_summary or ""


def _sync_private_snapshot(db: Session, profile: UserProfile) -> None:
    """同事务沉淀本人偏好；不向嵌入/LLM 服务发送个人兴趣。"""
    from app.models.knowledge_chunk import KnowledgeChunk
    from app.models.knowledge_doc import KnowledgeDoc

    source_ref = "profile:preferences"
    doc = (
        db.query(KnowledgeDoc)
        .filter(
            KnowledgeDoc.user_id == profile.user_id,
            KnowledgeDoc.source_ref == source_ref,
            KnowledgeDoc.status == "active",
        )
        .first()
    )
    content = "用户自述与行为统计分开记录；行为统计不是兴趣事实。\n" + _build_summary(
        profile, _parse_stats(profile.derived_stats) if profile.auto_learn else {}
    )
    if (
        doc is None
        and db.query(KnowledgeDoc.id)
        .filter(
            KnowledgeDoc.user_id == profile.user_id,
            KnowledgeDoc.source_ref == source_ref,
            KnowledgeDoc.status == "deleted",
        )
        .first()
    ):
        return  # 本人删除的知识条目不会被后台学习重新创建。
    if doc is None:
        doc = KnowledgeDoc(
            user_id=profile.user_id,
            title="我的偏好与使用记录",
            source_type="preference",
            source_ref=source_ref,
            status="active",
            char_count=len(content),
            chunk_count=1,
        )
        db.add(doc)
        db.flush()
    doc.char_count = len(content)
    doc.chunk_count = 1
    chunk = (
        db.query(KnowledgeChunk)
        .filter(KnowledgeChunk.doc_id == doc.id, KnowledgeChunk.user_id == profile.user_id, KnowledgeChunk.seq == 0)
        .first()
    )
    if chunk is None:
        chunk = KnowledgeChunk(doc_id=doc.id, user_id=profile.user_id, seq=0)
        db.add(chunk)
    chunk.content = content
    chunk.embedding = None
    chunk.embed_model = "local:profile-context"


def refresh_background(user_id: int) -> None:
    """成功会话后低频更新本人确定性统计，非关键失败不影响主任务。"""
    from loguru import logger

    from app.core.database import SessionLocal

    with SessionLocal() as db:
        try:
            profile = db.query(UserProfile).filter(UserProfile.user_id == user_id).first()
            if profile is None or not profile.auto_learn:
                return
            last = profile.last_learned_at
            if last and (_now() - last.replace(tzinfo=timezone.utc)).total_seconds() < 86400:
                return
            refresh_implicit(db, user_id)
        except Exception:
            db.rollback()
            logger.warning("[personalization] 后台偏好学习暂不可用，保留已有结果，后续可重试")
