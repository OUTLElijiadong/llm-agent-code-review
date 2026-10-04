"""并发编辑代码文件时的版本冲突与版本历史完整性。"""
from __future__ import annotations

import hashlib
from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.database import Base
from app.core.exceptions import ConflictError
from app.models.code_file import CodeFile
from app.models.code_version import CodeVersion
from app.models.project import Project
from app.models.user import User
from app.services import code_file_service


def _isolated_sessions(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'code-file-concurrency.sqlite'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    return engine, sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def _seed(session_factory):
    with session_factory() as db:
        user = User(username="file-owner", password="x", role="admin", status=1)
        db.add(user)
        db.flush()
        project = Project(
            user_id=user.id,
            project_name="并发编辑项目",
            status="active",
            language="python",
        )
        db.add(project)
        db.flush()
        source = CodeFile(
            project_id=project.id,
            file_name="app.py",
            file_path="app.py",
            language="python",
            size_bytes=9,
            raw_size=9,
            line_count=2,
            version_no=1,
            content="value = 1\n",
            status="active",
            is_binary=0,
        )
        db.add(source)
        db.flush()
        db.add(CodeVersion(
            file_id=source.id,
            version_no=1,
            content=source.content,
            operator_id=user.id,
            create_time=datetime.now(timezone.utc),
        ))
        db.commit()
        return user.id, source.id


def test_stale_editor_version_returns_conflict_without_overwriting_or_losing_version_history(tmp_path):
    engine, session_factory = _isolated_sessions(tmp_path)
    try:
        user_id, file_id = _seed(session_factory)
        stale_session = session_factory()
        writer_session = session_factory()
        try:
            stale_user = stale_session.get(User, user_id)
            writer = writer_session.get(User, user_id)
            stale_file = stale_session.get(CodeFile, file_id)
            assert stale_file.version_no == 1
            # 结束初次读取事务但保留 expire_on_commit=False 的旧 ORM 快照。
            stale_session.commit()

            assert code_file_service.update_content(
                writer_session,
                writer,
                file_id,
                "value = 2\n",
                expected_version=1,
            ) == 2

            with pytest.raises(ConflictError) as conflict:
                code_file_service.update_content(
                    stale_session,
                    stale_user,
                    file_id,
                    "value = 99\n",
                    expected_version=1,
                )

            assert conflict.value.code == 40904
            stale_session.rollback()

            with session_factory() as verify:
                current = verify.get(CodeFile, file_id)
                history = (
                    verify.query(CodeVersion)
                    .filter(CodeVersion.file_id == file_id)
                    .order_by(CodeVersion.version_no)
                    .all()
                )
                assert current.version_no == 2
                assert current.content == "value = 2\n"
                assert [item.version_no for item in history] == [1, 2]
                assert history[-1].content == current.content
                assert code_file_service.get_file_meta(
                    verify, verify.get(User, user_id), file_id,
                )["sha256_hash"] == hashlib.sha256(current.content.encode()).hexdigest()
        finally:
            stale_session.close()
            writer_session.close()
    finally:
        engine.dispose()


def test_successful_consecutive_saves_require_and_advance_exact_versions(tmp_path):
    engine, session_factory = _isolated_sessions(tmp_path)
    try:
        user_id, file_id = _seed(session_factory)
        stale_session = session_factory()
        writer_session = session_factory()
        try:
            stale_user = stale_session.get(User, user_id)
            writer = writer_session.get(User, user_id)
            assert stale_session.get(CodeFile, file_id).version_no == 1
            stale_session.commit()

            assert code_file_service.update_content(
                writer_session, writer, file_id, "value = 2\n", expected_version=1,
            ) == 2
            with pytest.raises(ConflictError) as conflict:
                code_file_service.update_content(
                    stale_session,
                    stale_user,
                    file_id,
                    "value = 3\n",
                    expected_version=1,
                )
            assert conflict.value.code == 40904
            stale_session.rollback()
            assert code_file_service.update_content(
                stale_session,
                stale_user,
                file_id,
                "value = 3\n",
                expected_version=2,
            ) == 3

            with session_factory() as verify:
                current = verify.get(CodeFile, file_id)
                history = (
                    verify.query(CodeVersion.version_no)
                    .filter(CodeVersion.file_id == file_id)
                    .order_by(CodeVersion.version_no)
                    .all()
                )
                assert current.version_no == 3
                assert [row[0] for row in history] == [1, 2, 3]
        finally:
            stale_session.close()
            writer_session.close()
    finally:
        engine.dispose()


def test_stale_rename_and_delete_preserve_latest_content_version_and_history(tmp_path):
    engine, session_factory = _isolated_sessions(tmp_path)
    try:
        user_id, file_id = _seed(session_factory)
        stale_session = session_factory()
        writer_session = session_factory()
        try:
            stale_user = stale_session.get(User, user_id)
            writer = writer_session.get(User, user_id)
            assert stale_session.get(CodeFile, file_id).version_no == 1
            stale_session.commit()
            assert code_file_service.update_content(
                writer_session, writer, file_id, "value = 2\n", expected_version=1,
            ) == 2

            code_file_service.rename_file(stale_session, stale_user, file_id, "renamed.py")
            renamed = stale_session.get(CodeFile, file_id)
            assert renamed.file_name == "renamed.py"
            assert renamed.version_no == 2
            assert renamed.content == "value = 2\n"
            assert code_file_service.get_file_meta(
                stale_session, stale_user, file_id,
            )["sha256_hash"] == hashlib.sha256(renamed.content.encode()).hexdigest()
            stale_session.commit()

            deleter_session = session_factory()
            try:
                deleter = deleter_session.get(User, user_id)
                assert deleter_session.get(CodeFile, file_id).version_no == 2
                deleter_session.commit()
                assert code_file_service.update_content(
                    writer_session, writer, file_id, "value = 3\n", expected_version=2,
                ) == 3
                code_file_service.delete_file(deleter_session, deleter, file_id)
            finally:
                deleter_session.close()

            with session_factory() as verify:
                current = verify.get(CodeFile, file_id)
                history = (
                    verify.query(CodeVersion)
                    .filter(CodeVersion.file_id == file_id)
                    .order_by(CodeVersion.version_no)
                    .all()
                )
                assert current.status == "deleted"
                assert current.file_name == "renamed.py"
                assert current.version_no == 3
                assert current.content == "value = 3\n"
                assert [item.version_no for item in history] == [1, 2, 3]
                assert history[-1].content == current.content
        finally:
            stale_session.close()
            writer_session.close()
    finally:
        engine.dispose()
