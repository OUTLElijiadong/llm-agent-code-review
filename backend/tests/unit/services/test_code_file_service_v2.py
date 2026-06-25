"""单元测试:code_file_service v2(压缩包处理 + 二进制文件支持)

验证:
1. 压缩包上传自动解压
2. 二进制文件标记 is_binary=1
3. get_file 对二进制文件 content 置空
4. get_binary_content 返回原始字节
5. update_content 拒绝二进制文件编辑
"""
from __future__ import annotations

import io
import zipfile
from unittest.mock import MagicMock

import pytest

from app.core.exceptions import ForbiddenError, NotFoundError, ValidationError
from app.models.code_file import CodeFile
from app.models.project import Project
from app.models.user import User
from app.services import code_file_service

# ============ 辅助函数 ============

def _make_user(uid=1, role="admin") -> User:
    """构造用户对象

    Args:
        uid: 用户ID
        role: 角色

    Returns:
        User: 未持久化的用户对象
    """
    return User(id=uid, username=f"tester{uid}", password="x", role=role, status=1, email=f"t{uid}@t.com")


def _make_project(pid=1, uid=1) -> Project:
    """构造项目对象

    Args:
        pid: 项目ID
        uid: 所有者用户ID

    Returns:
        Project: 未持久化的项目对象
    """
    return Project(
        id=pid, user_id=uid, project_name="test", status="active", language="python",
    )


def _make_upload_file(filename: str, content: bytes) -> MagicMock:
    """构造模拟的 UploadFile 对象

    Args:
        filename: 文件名
        content: 文件字节内容

    Returns:
        MagicMock: 模拟的 UploadFile
    """
    f = MagicMock()
    f.filename = filename
    f.file = io.BytesIO(content)
    return f


# ============ 二进制文件上传 ============

class TestBinaryFileUpload:
    """二进制文件上传测试"""

    def test_binary_file_marked_is_binary(self, db, monkeypatch):
        """二进制文件应标记 is_binary=1"""
        user = _make_user()
        project = _make_project()
        db.add(user)
        db.add(project)
        db.commit()

        # PNG 文件头(含 null 字节,触发二进制检测)
        png_bytes = b"\x89PNG\r\n\x1a\n\x00\x00\x00\x0dIHDR"
        upload_file = _make_upload_file("logo.png", png_bytes)

        file_id, lang, ver = code_file_service.upload(
            db=db, user=user, project_id=project.id, upload_file=upload_file,
        )

        code_file = db.get(CodeFile, file_id)
        assert code_file.is_binary == 1
        assert code_file.original_blob is not None
        assert code_file.original_blob == png_bytes

    def test_text_file_not_marked_binary(self, db):
        """文本文件不应标记 is_binary"""
        user = _make_user()
        project = _make_project()
        db.add(user)
        db.add(project)
        db.commit()

        text = b"print('hello')\n"
        upload_file = _make_upload_file("hello.py", text)

        file_id, lang, ver = code_file_service.upload(
            db=db, user=user, project_id=project.id, upload_file=upload_file,
        )

        code_file = db.get(CodeFile, file_id)
        assert code_file.is_binary == 0
        assert code_file.original_blob is None


# ============ get_file 二进制处理 ============

class TestGetFileBinary:
    """get_file() 二进制文件处理测试"""

    def test_get_file_binary_content_emptied(self, db):
        """二进制文件的 content 应被置空"""
        user = _make_user()
        project = _make_project()
        db.add(user)
        db.add(project)
        db.commit()

        png_bytes = b"\x89PNG\r\n\x1a\n\x00\x00\x00"
        upload_file = _make_upload_file("logo.png", png_bytes)
        file_id, _, _ = code_file_service.upload(
            db=db, user=user, project_id=project.id, upload_file=upload_file,
        )

        code_file = code_file_service.get_file(db, user, file_id)
        assert code_file.is_binary == 1
        assert code_file.content == ""

    def test_get_file_text_content_preserved(self, db):
        """文本文件的 content 应保留"""
        user = _make_user()
        project = _make_project()
        db.add(user)
        db.add(project)
        db.commit()

        text = "print('hello')\n"
        upload_file = _make_upload_file("hello.py", text.encode("utf-8"))
        file_id, _, _ = code_file_service.upload(
            db=db, user=user, project_id=project.id, upload_file=upload_file,
        )

        code_file = code_file_service.get_file(db, user, file_id)
        assert code_file.is_binary == 0
        assert "print" in code_file.content


# ============ get_binary_content ============

class TestGetBinaryContent:
    """get_binary_content() 下载接口测试"""

    def test_get_binary_content_returns_original_bytes(self, db):
        """应返回原始字节"""
        user = _make_user()
        project = _make_project()
        db.add(user)
        db.add(project)
        db.commit()

        png_bytes = b"\x89PNG\r\n\x1a\n\x00\x00\x00\x0dIHDR"
        upload_file = _make_upload_file("logo.png", png_bytes)
        file_id, _, _ = code_file_service.upload(
            db=db, user=user, project_id=project.id, upload_file=upload_file,
        )

        raw, name = code_file_service.get_binary_content(db, user, file_id)
        assert raw == png_bytes
        assert name == "logo.png"

    def test_get_binary_content_rejects_text_file(self, db):
        """文本文件应被拒绝"""
        user = _make_user()
        project = _make_project()
        db.add(user)
        db.add(project)
        db.commit()

        text = "print('hello')\n"
        upload_file = _make_upload_file("hello.py", text.encode("utf-8"))
        file_id, _, _ = code_file_service.upload(
            db=db, user=user, project_id=project.id, upload_file=upload_file,
        )

        with pytest.raises(NotFoundError, match="不是二进制"):
            code_file_service.get_binary_content(db, user, file_id)


# ============ update_content 二进制拒绝 ============

class TestUpdateContentBinary:
    """update_content() 二进制文件拒绝编辑测试"""

    def test_update_rejects_binary_file(self, db):
        """二进制文件应拒绝在线编辑"""
        user = _make_user()
        project = _make_project()
        db.add(user)
        db.add(project)
        db.commit()

        png_bytes = b"\x89PNG\r\n\x1a\n\x00\x00\x00"
        upload_file = _make_upload_file("logo.png", png_bytes)
        file_id, _, _ = code_file_service.upload(
            db=db, user=user, project_id=project.id, upload_file=upload_file,
        )

        with pytest.raises(ValidationError, match="二进制文件不支持在线编辑"):
            code_file_service.update_content(db, user, file_id, "new content")


# ============ 压缩包上传 ============

class TestArchiveUpload:
    """压缩包上传自动解压测试"""

    def _make_zip(self, files: dict) -> bytes:
        """构造内存 zip

        Args:
            files: {路径: 内容} 字典

        Returns:
            bytes: zip 字节流
        """
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for path, content in files.items():
                if isinstance(content, str):
                    content = content.encode("utf-8")
                zf.writestr(path, content)
        return buf.getvalue()

    def test_zip_upload_extracts_multiple_files(self, db):
        """zip 上传应解压出多个文件"""
        user = _make_user()
        project = _make_project()
        db.add(user)
        db.add(project)
        db.commit()

        zip_bytes = self._make_zip({
            "main.py": "print('main')\n",
            "utils.py": "def f(): pass\n",
        })
        upload_file = _make_upload_file("project.zip", zip_bytes)

        file_id, lang, ver = code_file_service.upload(
            db=db, user=user, project_id=project.id, upload_file=upload_file,
        )

        # 验证数据库中有 2 个文件
        files = db.query(CodeFile).filter(CodeFile.project_id == project.id).all()
        assert len(files) == 2
        names = {f.file_name for f in files}
        assert "main.py" in names
        assert "utils.py" in names

    def test_zip_upload_binary_file_inside(self, db):
        """zip 中的二进制文件应正确标记"""
        user = _make_user()
        project = _make_project()
        db.add(user)
        db.add(project)
        db.commit()

        zip_bytes = self._make_zip({
            "code.py": "print('hi')\n",
            "image.png": b"\x89PNG\r\n\x1a\n\x00\x00\x00",
        })
        upload_file = _make_upload_file("mixed.zip", zip_bytes)

        code_file_service.upload(
            db=db, user=user, project_id=project.id, upload_file=upload_file,
        )

        files = db.query(CodeFile).filter(CodeFile.project_id == project.id).all()
        binary_files = [f for f in files if f.is_binary == 1]
        text_files = [f for f in files if f.is_binary == 0]
        assert len(binary_files) == 1
        assert len(text_files) == 1
        assert binary_files[0].file_name == "image.png"


# ============ 权限校验 ============

class TestPermissionCheck:
    """权限校验测试"""

    def test_non_owner_rejected(self, db):
        """非项目所有者应被拒绝"""
        user1 = _make_user(uid=1)
        user2 = _make_user(uid=2, role="user")
        project = _make_project(pid=1, uid=1)  # user1 的项目
        db.add(user1)
        db.add(user2)
        db.add(project)
        db.commit()

        upload_file = _make_upload_file("x.py", b"x = 1\n")
        with pytest.raises(ForbiddenError):
            code_file_service.upload(
                db=db, user=user2, project_id=1, upload_file=upload_file,
            )

    def test_nonexistent_project_rejected(self, db):
        """不存在的项目应被拒绝"""
        user = _make_user()
        db.add(user)
        db.commit()

        upload_file = _make_upload_file("x.py", b"x = 1\n")
        with pytest.raises(NotFoundError):
            code_file_service.upload(
                db=db, user=user, project_id=999, upload_file=upload_file,
            )
