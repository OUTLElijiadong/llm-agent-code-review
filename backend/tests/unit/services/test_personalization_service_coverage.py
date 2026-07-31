"""画像、个人知识库与个性化服务覆盖率补全测试。

覆盖显式/隐式画像、知识切片与检索隔离、平台数据同步，以及聊天、
审查和论坛助手的个性化上下文与外部模型降级路径。
"""
from __future__ import annotations

import json
from typing import Any

import pytest

from app.core.exceptions import ForbiddenError, NotFoundError
from app.models.code_file import CodeFile
from app.models.forum_post import ForumPost
from app.models.knowledge_chunk import KnowledgeChunk
from app.models.knowledge_doc import KnowledgeDoc
from app.models.maintenance_ticket import MaintenanceTicket
from app.models.project import Project
from app.models.review_issue import ReviewIssue
from app.models.review_task import ReviewTask
from app.models.user import User
from app.models.user_feedback import UserFeedback
from app.models.user_profile import UserProfile
from app.services import knowledge_service, personalization_service, profile_service


def _persist(db, *objects: Any) -> None:
    """批量持久化测试对象并刷新可用主键。

    Args:
        db: 测试数据库会话。
        *objects: 待写入的 SQLAlchemy ORM 对象。

    Returns:
        None: 对象在提交后可直接读取数据库生成字段。
    """
    db.add_all(objects)
    db.commit()
    for obj in objects:
        db.refresh(obj)


def _make_user(db, username: str) -> User:
    """创建个性化服务测试用户。

    Args:
        db: 测试数据库会话。
        username: 唯一用户名。

    Returns:
        User: 已持久化用户。
    """
    user = User(
        username=username,
        password="stored-hash",
        email=f"{username}@example.com",
        nickname=username,
        role="user",
        status=1,
        token_version=0,
    )
    _persist(db, user)
    return user


def _make_project(db, owner: User, name: str, language: str, status: str = "active") -> Project:
    """创建用于画像统计或知识同步的项目。

    Args:
        db: 测试数据库会话。
        owner: 项目所有者。
        name: 项目名称。
        language: 项目主语言。
        status: 项目状态。

    Returns:
        Project: 已持久化项目。
    """
    project = Project(
        user_id=owner.id,
        project_name=name,
        description=f"{name} description",
        language=language,
        status=status,
    )
    _persist(db, project)
    return project


def _make_task(db, owner: User, project: Project, name: str) -> ReviewTask:
    """创建用于问题归属校验的审查任务。

    Args:
        db: 测试数据库会话。
        owner: 任务创建用户。
        project: 所属项目。
        name: 任务名称。

    Returns:
        ReviewTask: 已持久化审查任务。
    """
    task = ReviewTask(
        user_id=owner.id,
        project_id=project.id,
        task_name=name,
        review_type="standard",
        status="success",
        total_files=1,
        processed_files=1,
        total_issues=1,
        score=80,
    )
    _persist(db, task)
    return task


def _make_issue(
    db,
    task: ReviewTask,
    *,
    issue_type: str,
    status: str,
    title: str,
) -> ReviewIssue:
    """创建用于隐式画像或平台同步的审查问题。

    Args:
        db: 测试数据库会话。
        task: 所属审查任务。
        issue_type: 问题类型。
        status: 处理状态。
        title: 问题标题。

    Returns:
        ReviewIssue: 已持久化问题。
    """
    issue = ReviewIssue(
        task_id=task.id,
        file_name="app.py",
        issue_type=issue_type,
        severity="高",
        title=title,
        description=f"{title} 描述",
        suggestion="修复建议",
        status=status,
    )
    _persist(db, issue)
    return issue


def _make_knowledge_doc(
    db,
    user_id: int,
    title: str,
    *,
    source_type: str = "upload",
    status: str = "active",
) -> KnowledgeDoc:
    """创建知识文档以测试检索、列表和删除。

    Args:
        db: 测试数据库会话。
        user_id: 文档所属用户。
        title: 文档标题。
        source_type: 文档来源类型。
        status: 文档状态。

    Returns:
        KnowledgeDoc: 已持久化文档。
    """
    doc = KnowledgeDoc(
        user_id=user_id,
        source_type=source_type,
        title=title,
        char_count=10,
        chunk_count=1,
        status=status,
    )
    _persist(db, doc)
    return doc


def _make_chunk(
    db,
    doc: KnowledgeDoc,
    content: str,
    vector: str,
    *,
    user_id: int | None = None,
) -> KnowledgeChunk:
    """创建带指定向量的知识切片。

    Args:
        db: 测试数据库会话。
        doc: 所属知识文档。
        content: 切片正文。
        vector: JSON 向量或故意构造的非法向量文本。
        user_id: 可选隔离键，默认继承文档用户。

    Returns:
        KnowledgeChunk: 已持久化切片。
    """
    chunk = KnowledgeChunk(
        doc_id=doc.id,
        user_id=doc.user_id if user_id is None else user_id,
        seq=0,
        content=content,
        embedding=vector,
        embed_model="test:model",
    )
    _persist(db, chunk)
    return chunk


def test_profile_create_update_serialization_and_summary_helpers(db):
    """显式画像应可创建、更新、容错解析并生成确定性摘要。

    Args:
        db: 测试数据库会话。

    Returns:
        None: 断言字段更新、枚举保护、JSON 解析和摘要读取。
    """
    user = _make_user(db, "profile-user")
    assert profile_service._parse_focus(None) == []
    assert profile_service._parse_focus("not-json") == []
    assert profile_service._parse_focus('{"focus": "security"}') == []

    profile = profile_service.get_or_create(db, user.id)
    assert profile_service.get_or_create(db, user.id).id == profile.id
    updated = profile_service.update_profile(
        db,
        user.id,
        {
            "hobbies": "开源项目",
            "goals": "提升安全审查能力",
            "tech_stack": "Python,Vue",
            "focus_areas": ["安全", "性能"],
            "preferred_language": "Python",
            "experience_level": "advanced",
            "auto_learn": False,
        },
    )
    updated.derived_summary = "资深 Python 开发者"
    updated.derived_stats = json.dumps({"top_languages": ["Python"]}, ensure_ascii=False)
    db.commit()

    data = profile_service.to_dict(updated)
    assert data["focus_areas"] == ["安全", "性能"]
    assert data["experience_level"] == "advanced"
    assert data["auto_learn"] is False
    assert data["derived_stats"] == {"top_languages": ["Python"]}
    assert profile_service.get_summary_text(db, user.id) == "资深 Python 开发者"
    assert profile_service.get_summary_text(db, 999999) == ""

    profile_service.update_profile(
        db,
        user.id,
        {"experience_level": "expert", "focus_areas": "invalid-json", "auto_learn": None},
    )
    db.refresh(updated)
    assert updated.experience_level == "advanced"
    assert profile_service.to_dict(updated)["focus_areas"] == []

    empty_summary = profile_service._build_summary(UserProfile(user_id=999), {})
    assert empty_summary.startswith("暂无足够数据")
    invalid_focus_summary = profile_service._build_summary(
        UserProfile(user_id=1000, goals="保持可维护", focus_areas="invalid-json"),
        {},
    )
    assert "保持可维护" in invalid_focus_summary


def test_profile_refresh_implicit_respects_opt_out_and_force(db):
    """隐式画像应尊重关闭开关，并在强制刷新时聚合用户行为。

    Args:
        db: 测试数据库会话。

    Returns:
        None: 断言问题偏好、项目语言、论坛活跃和中文摘要。
    """
    user = _make_user(db, "implicit-user")
    python_one = _make_project(db, user, "python-one", "Python")
    _make_project(db, user, "python-two", "Python")
    _make_project(db, user, "go-one", "Go")
    _make_project(db, user, "deleted-project", "Rust", status="deleted")
    task = _make_task(db, user, python_one, "implicit-review")
    _make_issue(db, task, issue_type="安全漏洞", status="fixed", title="SQL 注入")
    _make_issue(db, task, issue_type="安全漏洞", status="fixed", title="命令注入")
    _make_issue(db, task, issue_type="代码规范", status="ignored", title="命名风格")
    _make_issue(db, task, issue_type="性能问题", status="unfixed", title="未处理问题")
    _persist(
        db,
        ForumPost(user_id=user.id, title="正常帖子", content="正文", status="normal"),
        ForumPost(user_id=user.id, title="删除帖子", content="正文", status="deleted"),
    )

    profile = profile_service.update_profile(
        db,
        user.id,
        {
            "goals": "成为安全专家",
            "focus_areas": ["架构"],
            "experience_level": "beginner",
            "auto_learn": False,
        },
    )
    profile.derived_summary = "保持不变"
    db.commit()
    skipped = profile_service.refresh_implicit(db, user.id)
    assert skipped.derived_summary == "保持不变"

    refreshed = profile_service.refresh_implicit(db, user.id, force=True)
    stats = json.loads(refreshed.derived_stats)
    assert stats["fixed_by_type"] == {"安全漏洞": 2}
    assert stats["ignored_by_type"] == {"代码规范": 1}
    assert stats["top_focus_types"] == ["安全漏洞"]
    assert stats["tolerated_types"] == ["代码规范"]
    assert stats["languages"] == {"Go": 1, "Python": 2}
    assert stats["top_languages"] == ["Python", "Go"]
    assert stats["forum_posts"] == 1
    assert refreshed.last_learned_at is not None
    assert "经验水平偏入门" in refreshed.derived_summary
    assert "成为安全专家" in refreshed.derived_summary


def test_chunk_text_handles_empty_paragraph_and_overlapping_windows():
    """知识文本切片应处理空输入、段落边界和超长段落重叠。

    Returns:
        None: 断言短文本直返、段落拆分和字符窗口重叠。
    """
    assert knowledge_service._chunk_text("") == []
    assert knowledge_service._chunk_text("  简短内容  ") == ["简短内容"]

    paragraphs = knowledge_service._chunk_text(f"{'甲' * 400}\n\n{'乙' * 400}")
    assert paragraphs == ["甲" * 400, "乙" * 400]

    windows = knowledge_service._chunk_text("长" * 1500)
    assert [len(item) for item in windows] == [700, 700, 260]
    assert windows[0][-80:] == windows[1][:80]
    assert windows[1][-80:] == windows[2][:80]


def test_add_document_persists_embeddings_and_replaces_existing_source(db, monkeypatch):
    """知识入库应保存切片向量并对同来源执行幂等替换。

    Args:
        db: 测试数据库会话。
        monkeypatch: Pytest 属性替换工具。

    Returns:
        None: 断言向量标签、旧文档软删除和空文档分支。
    """
    embed_calls: list[list[str]] = []

    def fake_embed_texts(_db, texts: list[str]) -> tuple[list[list[float]], str]:
        """为每个文本返回确定性二维向量。

        Args:
            _db: 未使用的测试数据库会话。
            texts: 待嵌入文本列表。

        Returns:
            tuple[list[list[float]], str]: 与文本等长的向量和模型标签。
        """
        embed_calls.append(texts)
        return [[1.0, float(index)] for index, _ in enumerate(texts)], "fake:embedding"

    monkeypatch.setattr(knowledge_service.embedding_service, "embed_texts", fake_embed_texts)
    first = knowledge_service.add_document(
        db,
        user_id=1,
        title="首版文档",
        content="甲" * 800,
        source_type="code",
        source_ref="file:1",
    )
    first_chunks = db.query(KnowledgeChunk).filter(KnowledgeChunk.doc_id == first.id).all()
    assert first.chunk_count == 2
    assert len(first_chunks) == 2
    assert first_chunks[0].embed_model == "fake:embedding"
    assert json.loads(first_chunks[1].embedding) == [1.0, 1.0]

    replacement = knowledge_service.add_document(
        db,
        user_id=1,
        title="新" * 220,
        content="替换后的短内容",
        source_type="code",
        source_ref="file:1",
    )
    db.refresh(first)
    assert first.status == "deleted"
    assert db.query(KnowledgeChunk).filter(KnowledgeChunk.doc_id == first.id).count() == 0
    assert replacement.status == "active"
    assert len(replacement.title) == 200
    assert replacement.chunk_count == 1

    empty = knowledge_service.add_document(db, 1, "", "", source_type="upload")
    assert empty.title == "未命名文档"
    assert empty.chunk_count == 0
    assert len(embed_calls) == 2


def test_retrieve_list_delete_and_stats_enforce_knowledge_isolation(db, monkeypatch):
    """知识管理应按用户隔离检索、列表、删除和统计。

    Args:
        db: 测试数据库会话。
        monkeypatch: Pytest 属性替换工具。

    Returns:
        None: 断言相似度排序、无效向量跳过和跨用户拒绝。
    """
    best = _make_knowledge_doc(db, 1, "最佳匹配", source_type="upload")
    medium = _make_knowledge_doc(db, 1, "次优匹配", source_type="code")
    deleted = _make_knowledge_doc(db, 1, "已删除文档", status="deleted")
    foreign = _make_knowledge_doc(db, 2, "他人文档")
    _make_chunk(db, best, "最佳片段", "[1.0, 0.0]")
    _make_chunk(db, best, "负相关片段", "[-1.0, 0.0]")
    _make_chunk(db, medium, "次优片段", "[0.4, 0.4]")
    _make_chunk(db, medium, "损坏向量", "not-json")
    _make_chunk(db, deleted, "已删除内容", "[1.0, 0.0]")
    _make_chunk(db, foreign, "他人内容", "[1.0, 0.0]")

    def fake_embed_one(_db, text: str) -> tuple[list[float], str]:
        """按查询内容返回正常或空查询向量。

        Args:
            _db: 未使用的测试数据库会话。
            text: 查询文本。

        Returns:
            tuple[list[float], str]: 测试查询向量和模型标签。
        """
        return ([], "fake") if text == "no-vector" else ([1.0, 0.0], "fake")

    monkeypatch.setattr(knowledge_service.embedding_service, "embed_one", fake_embed_one)
    monkeypatch.setattr(knowledge_service.embedding_service, "is_remote_enabled", lambda _db: True)

    assert knowledge_service.retrieve(db, 1, "  ") == []
    assert knowledge_service.retrieve(db, 1, "no-vector") == []
    hits = knowledge_service.retrieve(db, 1, "安全查询", top_k=1)
    assert hits == [{
        "content": "最佳片段",
        "score": 1.0,
        "doc_id": best.id,
        "title": "最佳匹配",
        "source_type": "upload",
    }]

    listed = knowledge_service.list_docs(db, 1, page=1, page_size=10)
    assert listed["total"] == 2
    assert {item["title"] for item in listed["items"]} == {"最佳匹配", "次优匹配"}
    code_only = knowledge_service.list_docs(db, 1, source_type="code")
    assert [item["id"] for item in code_only["items"]] == [medium.id]

    stats = knowledge_service.get_stats(db, 1)
    assert stats == {
        "doc_total": 2,
        "chunk_total": 5,
        "by_source": {"upload": 1, "code": 1},
        "remote_embedding": True,
    }

    with pytest.raises(ForbiddenError):
        knowledge_service.delete_doc(db, 2, best.id)
    with pytest.raises(NotFoundError):
        knowledge_service.delete_doc(db, 1, 999999)
    knowledge_service.delete_doc(db, 1, best.id)
    db.refresh(best)
    assert best.status == "deleted"
    assert db.query(KnowledgeChunk).filter(KnowledgeChunk.doc_id == best.id).count() == 0
    with pytest.raises(NotFoundError):
        knowledge_service.delete_doc(db, 1, best.id)


def test_sync_from_platform_aggregates_only_owner_data_and_survives_source_failure(db, monkeypatch):
    """平台同步应聚合五类本人数据，并在单一来源失败时继续执行。

    Args:
        db: 测试数据库会话。
        monkeypatch: Pytest 属性替换工具。

    Returns:
        None: 断言来源计数、用户隔离、正文拼接和局部降级。
    """
    owner = _make_user(db, "sync-owner")
    foreign_user = _make_user(db, "sync-foreign")
    owner_project = _make_project(db, owner, "owner-project", "Python")
    foreign_project = _make_project(db, foreign_user, "foreign-project", "Go")
    owner_task = _make_task(db, owner, owner_project, "owner-task")
    foreign_task = _make_task(db, foreign_user, foreign_project, "foreign-task")
    owner_issue = _make_issue(db, owner_task, issue_type="安全漏洞", status="fixed", title="本人问题")
    _make_issue(db, foreign_task, issue_type="代码规范", status="fixed", title="他人问题")

    owner_file = CodeFile(
        project_id=owner_project.id,
        file_name="app.py",
        file_path="src/app.py",
        language="python",
        size_bytes=12,
        line_count=1,
        content="print('owner')",
        status="active",
    )
    foreign_file = CodeFile(
        project_id=foreign_project.id,
        file_name="foreign.go",
        file_path="foreign.go",
        language="go",
        size_bytes=12,
        line_count=1,
        content="package main",
        status="active",
    )
    owner_post = ForumPost(user_id=owner.id, title="本人帖子", content="帖子正文", status="normal")
    foreign_post = ForumPost(user_id=foreign_user.id, title="他人帖子", content="外部正文", status="normal")
    owner_feedback = UserFeedback(
        user_id=owner.id,
        feedback_type="bug",
        content="本人反馈",
        admin_reply="反馈回复",
        status="replied",
    )
    foreign_feedback = UserFeedback(
        user_id=foreign_user.id,
        feedback_type="praise",
        content="他人反馈",
        status="new",
    )
    owner_ticket = MaintenanceTicket(
        user_id=owner.id,
        title="本人工单",
        category="bug",
        description="工单描述",
        priority="high",
        status="resolved",
        admin_reply="工单回复",
    )
    foreign_ticket = MaintenanceTicket(
        user_id=foreign_user.id,
        title="他人工单",
        category="other",
        description="外部工单",
        priority="low",
        status="pending",
    )
    _persist(
        db,
        owner_file,
        foreign_file,
        owner_post,
        foreign_post,
        owner_feedback,
        foreign_feedback,
        owner_ticket,
        foreign_ticket,
    )

    calls: list[dict[str, Any]] = []

    def fake_add_document(
        _db,
        user_id: int,
        title: str,
        content: str,
        source_type: str = "upload",
        source_ref: str | None = None,
        replace_existing: bool = True,
    ) -> object:
        """记录同步入库参数而不调用真实嵌入服务。

        Args:
            _db: 未使用的测试数据库会话。
            user_id: 文档所属用户。
            title: 文档标题。
            content: 文档正文。
            source_type: 平台来源类型。
            source_ref: 幂等来源引用。
            replace_existing: 是否替换旧来源。

        Returns:
            object: 无业务用途的占位对象。
        """
        calls.append({
            "user_id": user_id,
            "title": title,
            "content": content,
            "source_type": source_type,
            "source_ref": source_ref,
            "replace_existing": replace_existing,
        })
        return object()

    monkeypatch.setattr(knowledge_service, "add_document", fake_add_document)
    counts = knowledge_service.sync_from_platform(db, owner.id)
    assert counts == {"code": 1, "issue": 1, "forum": 1, "feedback": 1, "ticket": 1, "total": 5}
    assert {call["source_type"] for call in calls} == {"code", "issue", "forum", "feedback", "ticket"}
    assert {call["user_id"] for call in calls} == {owner.id}
    assert {call["source_ref"] for call in calls} == {
        f"file:{owner_file.id}",
        f"issue:{owner_issue.id}",
        f"post:{owner_post.id}",
        f"feedback:{owner_feedback.id}",
        f"ticket:{owner_ticket.id}",
    }
    assert any("反馈回复" in call["content"] for call in calls)
    assert any("工单回复" in call["content"] for call in calls)

    def flaky_add_document(
        _db,
        user_id: int,
        title: str,
        content: str,
        source_type: str = "upload",
        source_ref: str | None = None,
        replace_existing: bool = True,
    ) -> object:
        """令代码来源失败，其余来源继续使用记录替身。

        Args:
            _db: 测试数据库会话。
            user_id: 文档所属用户。
            title: 文档标题。
            content: 文档正文。
            source_type: 平台来源类型。
            source_ref: 幂等来源引用。
            replace_existing: 是否替换旧来源。

        Returns:
            object: 非代码来源的占位对象。

        Raises:
            RuntimeError: 当来源为代码时模拟嵌入失败。
        """
        if source_type == "code":
            raise RuntimeError("embedding unavailable")
        return fake_add_document(
            _db,
            user_id,
            title,
            content,
            source_type=source_type,
            source_ref=source_ref,
            replace_existing=replace_existing,
        )

    monkeypatch.setattr(knowledge_service, "add_document", flaky_add_document)
    degraded = knowledge_service.sync_from_platform(db, owner.id)
    assert degraded == {"code": 0, "issue": 1, "forum": 1, "feedback": 1, "ticket": 1, "total": 4}


def test_personalization_context_builders_cover_empty_and_profile_branches(db, monkeypatch):
    """聊天与审查上下文应组合画像、知识片段并处理空或损坏数据。

    Args:
        db: 测试数据库会话。
        monkeypatch: Pytest 属性替换工具。

    Returns:
        None: 断言片段阈值、上下文拼接、关注点去重和经验分级。
    """
    user = _make_user(db, "context-user")
    hits = [
        {
            "content": "第一行\n第二行",
            "score": 0.9,
            "doc_id": 1,
            "title": "安全手册",
            "source_type": "upload",
        },
        {
            "content": "低分内容",
            "score": 0.2,
            "doc_id": 2,
            "title": "低分文档",
            "source_type": "code",
        },
    ]

    def fake_retrieve(_db, user_id: int, query: str, top_k: int = 5) -> list[dict]:
        """返回固定知识命中以隔离真实嵌入服务。

        Args:
            _db: 未使用的测试数据库会话。
            user_id: 查询用户主键。
            query: 查询文本。
            top_k: 最大返回条数。

        Returns:
            list[dict]: 固定且按分数排序的知识命中。
        """
        assert user_id == user.id
        assert query
        return hits[:top_k]

    def fake_summary(_db, user_id: int) -> str:
        """返回固定用户画像摘要。

        Args:
            _db: 未使用的测试数据库会话。
            user_id: 用户主键。

        Returns:
            str: 固定画像摘要。
        """
        assert user_id == user.id
        return "偏好 Python 与安全审查"

    monkeypatch.setattr(personalization_service.knowledge_service, "retrieve", fake_retrieve)
    monkeypatch.setattr(personalization_service.profile_service, "get_summary_text", fake_summary)
    kb = personalization_service._kb_block(db, user.id, "SQL 注入", min_score=0.5)
    assert "第一行 第二行" in kb
    assert "低分内容" not in kb

    chat = personalization_service.build_chat_context(db, user.id, "SQL 注入")
    assert "【用户画像】偏好 Python 与安全审查" in chat
    assert "安全手册" in chat
    assert "个性化上下文" in chat

    profile = profile_service.update_profile(
        db,
        user.id,
        {"focus_areas": ["安全", "性能", "安全"], "experience_level": "beginner"},
    )
    profile.derived_stats = json.dumps(
        {"top_focus_types": ["性能", "可维护性"], "tolerated_types": ["代码规范"]},
        ensure_ascii=False,
    )
    db.commit()
    review = personalization_service.build_review_context(db, user.id, language="python")
    assert "安全、性能、可维护性" in review
    assert "代码规范" in review
    assert "入门水平" in review

    profile.focus_areas = "invalid-json"
    profile.derived_stats = "invalid-json"
    profile.experience_level = "advanced"
    db.commit()
    advanced = personalization_service.build_review_context(db, user.id)
    assert "资深水平" in advanced

    empty_user = _make_user(db, "empty-context-user")
    assert personalization_service.build_review_context(db, empty_user.id) == ""

    def empty_retrieve(_db, user_id: int, query: str, top_k: int = 5) -> list[dict]:
        """返回空知识命中以验证无上下文分支。

        Args:
            _db: 未使用的测试数据库会话。
            user_id: 用户主键。
            query: 查询文本。
            top_k: 最大返回条数。

        Returns:
            list[dict]: 空列表。
        """
        return []

    def empty_summary(_db, user_id: int) -> str:
        """返回空画像摘要以验证无上下文分支。

        Args:
            _db: 未使用的测试数据库会话。
            user_id: 用户主键。

        Returns:
            str: 空字符串。
        """
        return ""

    monkeypatch.setattr(personalization_service.knowledge_service, "retrieve", empty_retrieve)
    monkeypatch.setattr(personalization_service.profile_service, "get_summary_text", empty_summary)
    assert personalization_service._kb_block(db, user.id, "无命中") == ""
    assert personalization_service.build_chat_context(db, user.id, "无命中") == ""


def test_forum_assist_and_chat_wrapper_use_llm_or_safe_fallback(db, monkeypatch):
    """论坛助手应解析模型结果并在配置或上下文失败时安全降级。

    Args:
        db: 测试数据库会话。
        monkeypatch: Pytest 属性替换工具。

    Returns:
        None: 断言 JSON/纯文本模型输出、RAG 降级和聊天安全封装。
    """
    from app.ai import deepseek_agent
    from app.utils import api_resolver

    hits = [{
        "content": "内部安全规范",
        "score": 0.88,
        "doc_id": 1,
        "title": "团队手册",
        "source_type": "upload",
    }]

    def fake_retrieve(_db, user_id: int, query: str, top_k: int = 5) -> list[dict]:
        """为论坛助手返回固定个人知识库命中。

        Args:
            _db: 未使用的测试数据库会话。
            user_id: 查询用户主键。
            query: 标题与草稿组成的查询。
            top_k: 最大返回条数。

        Returns:
            list[dict]: 固定知识命中。
        """
        assert user_id == 7
        assert "标题" in query
        return hits[:top_k]

    def fake_summary(_db, user_id: int) -> str:
        """为论坛助手返回固定画像摘要。

        Args:
            _db: 未使用的测试数据库会话。
            user_id: 用户主键。

        Returns:
            str: 固定画像摘要。
        """
        assert user_id == 7
        return "偏好简洁的安全建议"

    def fake_resolve(_db, user_id: int) -> dict:
        """返回不会触发网络请求的伪模型配置。

        Args:
            _db: 未使用的测试数据库会话。
            user_id: 用户主键。

        Returns:
            dict: 伪 API 配置。
        """
        return {"provider": "fake", "user_id": user_id}

    class FakeAgent:
        """记录论坛助手调用并返回可切换的固定内容。"""

        response = '{"suggestion": "补充复现步骤"}'
        init_configs: list[dict] = []
        chat_calls: list[dict] = []

        def __init__(self, api_config: dict):
            """记录初始化配置。

            Args:
                api_config: 解析后的伪 API 配置。
            """
            self.init_configs.append(api_config)

        def chat(self, **kwargs: Any) -> tuple[str, dict]:
            """记录聊天参数并返回当前固定响应。

            Args:
                **kwargs: 论坛助手传入的系统提示、用户提示和审计参数。

            Returns:
                tuple[str, dict]: 固定内容与空元数据。
            """
            self.chat_calls.append(kwargs)
            return self.response, {}

    monkeypatch.setattr(personalization_service.knowledge_service, "retrieve", fake_retrieve)
    monkeypatch.setattr(personalization_service.profile_service, "get_summary_text", fake_summary)
    monkeypatch.setattr(api_resolver, "resolve_api_config", fake_resolve)
    monkeypatch.setattr(deepseek_agent, "DeepSeekAgent", FakeAgent)

    result = personalization_service.assist_forum_draft(db, 7, "标题", "草稿")
    assert result["suggestion"] == "补充复现步骤"
    assert result["references"] == [{"title": "团队手册", "source_type": "upload", "score": 0.88}]
    assert FakeAgent.init_configs == [{"provider": "fake", "user_id": 7}]
    assert FakeAgent.chat_calls[0]["agent_label"] == "forum_assist"
    assert "内部安全规范" in FakeAgent.chat_calls[0]["user_prompt"]

    FakeAgent.response = "直接补充错误日志"
    plain = personalization_service.assist_forum_draft(db, 7, "标题", "草稿")
    assert plain["suggestion"] == "直接补充错误日志"

    FakeAgent.response = '{"suggestion": ""}'
    blank = personalization_service.assist_forum_draft(db, 7, "标题", "草稿")
    assert blank["suggestion"] == "(暂无建议)"

    def failing_resolve(_db, user_id: int) -> dict:
        """模拟模型配置解析失败。

        Args:
            _db: 未使用的测试数据库会话。
            user_id: 用户主键。

        Returns:
            dict: 此函数不会正常返回。

        Raises:
            RuntimeError: 始终模拟外部配置不可用。
        """
        raise RuntimeError(f"config unavailable for {user_id}")

    monkeypatch.setattr(api_resolver, "resolve_api_config", failing_resolve)
    fallback = personalization_service.assist_forum_draft(db, 7, "标题", "草稿")
    assert "根据你的知识库" in fallback["suggestion"]
    assert "内部安全规范" in fallback["suggestion"]

    assert personalization_service.chat_context_for_agent(db, None, "query") == ""

    def successful_context(_db, user_id: int, query: str) -> str:
        """返回固定聊天上下文。

        Args:
            _db: 未使用的测试数据库会话。
            user_id: 用户主键。
            query: 用户查询。

        Returns:
            str: 固定上下文。
        """
        return f"context:{user_id}:{query}"

    monkeypatch.setattr(personalization_service, "build_chat_context", successful_context)
    assert personalization_service.chat_context_for_agent(db, 7, "query") == "context:7:query"

    def failing_context(_db, user_id: int, query: str) -> str:
        """模拟聊天个性化上下文构建失败。

        Args:
            _db: 未使用的测试数据库会话。
            user_id: 用户主键。
            query: 用户查询。

        Returns:
            str: 此函数不会正常返回。

        Raises:
            RuntimeError: 始终模拟上下文构建失败。
        """
        raise RuntimeError(f"context failed for {user_id}:{query}")

    monkeypatch.setattr(personalization_service, "build_chat_context", failing_context)
    assert personalization_service.chat_context_for_agent(db, 7, "query") == ""
