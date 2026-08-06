"""单元测试:code_file_service T06 上传安全集成

验证上传流程串行执行:MIME 校验 → 单文件 10MB → 项目总 500MB → MalwareScanner
双引擎扫描 → 解压(若压缩包)→ 入库。任一校验失败抛出 ValueError。

覆盖场景:
1. MIME 白名单(通过/拒绝/可执行文件/未知扩展名/空文件名)
2. 单文件 10MB 上限(超限/恰好等于/正常)
3. 项目总 500MB 上限(超限/正常)
4. 恶意软件扫描(命中拒绝/干净通过/压缩包内恶意文件)
5. 二进制文件入库(is_binary=1, original_blob, raw_size)
6. 文本文件入库(is_binary=0, content, raw_size)
7. 压缩包上传(解压/zip slip/文件数超限/单文件超限/独立记录)
8. 校验链顺序(MIME 优先于大小,大小优先于扫描)
"""
from __future__ import annotations

import io
import zipfile
from unittest.mock import MagicMock

import pytest

from app.models.code_file import CodeFile
from app.models.project import Project
from app.models.user import User
from app.services import code_file_service
from app.utils.archive_extractor import MAX_EXTRACTED_FILES
from app.utils.archive_extractor import MAX_SINGLE_FILE_SIZE as ARCHIVE_MAX_SINGLE
from app.utils.file_validator import MAX_SINGLE_FILE_SIZE
from app.utils.malware_scanner import ScanResult

# ============ 辅助函数 ============

def _make_user(uid: int = 1, role: str = "admin") -> User:
    """构造用户对象

    Args:
        uid: 用户ID
        role: 角色(admin/user)

    Returns:
        User: 未持久化的用户对象
    """
    return User(
        id=uid,
        username=f"tester{uid}",
        password="x",
        role=role,
        status=1,
        email=f"t{uid}@t.com",
    )


def _make_project(pid: int = 1, uid: int = 1) -> Project:
    """构造项目对象

    Args:
        pid: 项目ID
        uid: 所有者用户ID

    Returns:
        Project: 未持久化的项目对象
    """
    return Project(
        id=pid,
        user_id=uid,
        project_name="test_project",
        status="active",
        language="python",
    )


def _make_upload_file(filename: str, content: bytes) -> MagicMock:
    """构造模拟的 UploadFile 对象

    Args:
        filename: 文件名
        content: 文件字节内容

    Returns:
        MagicMock: 模拟的 UploadFile,file 字段为 BytesIO
    """
    f = MagicMock()
    f.filename = filename
    f.file = io.BytesIO(content)
    return f


def _make_zip(files: dict) -> bytes:
    """构造内存 zip 字节流

    Args:
        files: {路径: 内容} 字典;内容为 str 时自动编码为 UTF-8

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


def _make_clean_scanner() -> MagicMock:
    """构造返回 clean 结果的扫描器 mock

    Returns:
        MagicMock: scan() 返回 result="clean" 的 ScanResult
    """
    scanner = MagicMock()
    scanner.scan.return_value = ScanResult(
        engine="heuristic",
        result="clean",
        threat_name=None,
        duration_ms=1,
        degraded=True,
    )
    return scanner


def _make_infected_scanner(threat: str = "Eicar-Test-Signature") -> MagicMock:
    """构造返回 infected 结果的扫描器 mock

    Args:
        threat: 威胁名称

    Returns:
        MagicMock: scan() 返回 result="infected" 的 ScanResult
    """
    scanner = MagicMock()
    scanner.scan.return_value = ScanResult(
        engine="heuristic",
        result="infected",
        threat_name=threat,
        duration_ms=1,
        degraded=True,
    )
    return scanner


def _setup_project(db, uid: int = 1, pid: int = 1):
    """初始化用户与项目并提交到数据库

    Args:
        db: 数据库会话
        uid: 用户ID
        pid: 项目ID

    Returns:
        tuple: (user, project)
    """
    user = _make_user(uid=uid)
    project = _make_project(pid=pid, uid=uid)
    db.add(user)
    db.add(project)
    db.commit()
    return user, project


# ============ MIME 白名单校验 ============

class TestUploadMimeValidation:
    """MIME 白名单校验测试"""

    def test_text_file_py_upload_success(self, db, monkeypatch):
        """.py 文件应上传成功"""
        monkeypatch.setattr(
            "app.services.code_file_service.get_scanner", _make_clean_scanner
        )
        user, project = _setup_project(db)

        upload_file = _make_upload_file("hello.py", b"print('hello')\n")
        file_id, lang, ver = code_file_service.upload(
            db=db, user=user, project_id=project.id, upload_file=upload_file,
        )
        assert file_id > 0
        assert lang == "python"

    def test_text_file_js_upload_success(self, db, monkeypatch):
        """.js 文件应上传成功"""
        monkeypatch.setattr(
            "app.services.code_file_service.get_scanner", _make_clean_scanner
        )
        user, project = _setup_project(db)

        upload_file = _make_upload_file("app.js", b"console.log('hi')\n")
        file_id, lang, ver = code_file_service.upload(
            db=db, user=user, project_id=project.id, upload_file=upload_file,
        )
        assert file_id > 0

    def test_exe_rejected_by_mime(self, db):
        """可执行文件 .exe 应被 MIME 校验拒绝"""
        user, project = _setup_project(db)
        upload_file = _make_upload_file("evil.exe", b"MZ\x90\x00")

        with pytest.raises(ValueError, match="不支持的文件类型"):
            code_file_service.upload(
                db=db, user=user, project_id=project.id, upload_file=upload_file,
            )

    def test_dll_rejected_by_mime(self, db):
        """可执行文件 .dll 应被 MIME 校验拒绝"""
        user, project = _setup_project(db)
        upload_file = _make_upload_file("evil.dll", b"MZ\x90\x00")

        with pytest.raises(ValueError, match="不支持的文件类型"):
            code_file_service.upload(
                db=db, user=user, project_id=project.id, upload_file=upload_file,
            )

    def test_unknown_extension_rejected(self, db):
        """未知扩展名 .bin 应被 MIME 校验拒绝"""
        user, project = _setup_project(db)
        upload_file = _make_upload_file("data.bin", b"\x00\x01\x02")

        with pytest.raises(ValueError, match="不支持的文件类型"):
            code_file_service.upload(
                db=db, user=user, project_id=project.id, upload_file=upload_file,
            )

    def test_empty_filename_rejected(self, db):
        """空文件名应被 MIME 校验拒绝"""
        user, project = _setup_project(db)
        upload_file = _make_upload_file("", b"content")

        with pytest.raises(ValueError, match="不支持的文件类型"):
            code_file_service.upload(
                db=db, user=user, project_id=project.id, upload_file=upload_file,
            )


# ============ 单文件大小校验 ============

class TestUploadSingleFileSize:
    """单文件 10MB 上限校验测试"""

    def test_single_file_over_10mb_rejected(self, db, monkeypatch):
        """单文件超过 10MB 应被拒绝"""
        monkeypatch.setattr(
            "app.services.code_file_service.get_scanner", _make_clean_scanner
        )
        user, project = _setup_project(db)

        # 构造 10MB + 1 字节的文本文件
        big_content = b"x" * (MAX_SINGLE_FILE_SIZE + 1)
        upload_file = _make_upload_file("big.py", big_content)

        with pytest.raises(ValueError, match="超过单文件上限"):
            code_file_service.upload(
                db=db, user=user, project_id=project.id, upload_file=upload_file,
            )

    def test_single_file_exact_10mb_success(self, db, monkeypatch):
        """单文件恰好 10MB 应上传成功(边界值)"""
        monkeypatch.setattr(
            "app.services.code_file_service.get_scanner", _make_clean_scanner
        )
        user, project = _setup_project(db)

        # 恰好 10MB:用 ASCII 文本填充(避免触发二进制检测)
        exact_content = b"a" * MAX_SINGLE_FILE_SIZE
        upload_file = _make_upload_file("exact.py", exact_content)

        file_id, _, _ = code_file_service.upload(
            db=db, user=user, project_id=project.id, upload_file=upload_file,
        )
        assert file_id > 0

    def test_single_file_under_limit_success(self, db, monkeypatch):
        """单文件 1MB 应上传成功"""
        monkeypatch.setattr(
            "app.services.code_file_service.get_scanner", _make_clean_scanner
        )
        user, project = _setup_project(db)

        content = b"x" * (1024 * 1024)
        upload_file = _make_upload_file("normal.py", content)
        file_id, _, _ = code_file_service.upload(
            db=db, user=user, project_id=project.id, upload_file=upload_file,
        )
        assert file_id > 0


# ============ 项目总大小校验 ============

class TestUploadProjectTotalSize:
    """项目总 500MB 上限校验测试"""

    def test_project_total_over_500mb_rejected(self, db, monkeypatch):
        """项目总大小超过 500MB 应被拒绝"""
        monkeypatch.setattr(
            "app.services.code_file_service.get_scanner", _make_clean_scanner
        )
        user, project = _setup_project(db)

        # 预先插入一个接近 500MB 的文件(用 raw_size 模拟,不实际分配大内存)
        huge_file = CodeFile(
            project_id=project.id,
            file_name="huge.py",
            file_path="huge.py",
            language="python",
            size_bytes=495 * 1024 * 1024,
            raw_size=495 * 1024 * 1024,
            line_count=1,
            version_no=1,
            content="x",
            status="active",
            is_binary=0,
        )
        db.add(huge_file)
        db.commit()

        # 再上传 6MB 文件(≤ 10MB 单文件上限),总计 501MB 超限
        new_content = b"x" * (6 * 1024 * 1024)
        upload_file = _make_upload_file("new.py", new_content)

        with pytest.raises(ValueError, match="超过项目上限"):
            code_file_service.upload(
                db=db, user=user, project_id=project.id, upload_file=upload_file,
            )

    def test_project_total_under_500mb_success(self, db, monkeypatch):
        """项目总大小未超过 500MB 应上传成功"""
        monkeypatch.setattr(
            "app.services.code_file_service.get_scanner", _make_clean_scanner
        )
        user, project = _setup_project(db)

        # 预先插入 400MB(用 raw_size 模拟)
        existing = CodeFile(
            project_id=project.id,
            file_name="existing.py",
            file_path="existing.py",
            language="python",
            size_bytes=400 * 1024 * 1024,
            raw_size=400 * 1024 * 1024,
            line_count=1,
            version_no=1,
            content="x",
            status="active",
            is_binary=0,
        )
        db.add(existing)
        db.commit()

        # 再上传 8MB(≤ 10MB 单文件上限),总计 408MB 未超限
        new_content = b"x" * (8 * 1024 * 1024)
        upload_file = _make_upload_file("new.py", new_content)
        file_id, _, _ = code_file_service.upload(
            db=db, user=user, project_id=project.id, upload_file=upload_file,
        )
        assert file_id > 0


# ============ 恶意软件扫描 ============

class TestUploadMalwareScan:
    """MalwareScanner 双引擎扫描集成测试"""

    def test_malware_detected_rejected(self, db, monkeypatch):
        """扫描命中恶意软件应被拒绝"""
        monkeypatch.setattr(
            "app.services.code_file_service.get_scanner",
            lambda: _make_infected_scanner("Eicar-Test-Signature"),
        )
        user, project = _setup_project(db)

        upload_file = _make_upload_file("malware.py", b"evil content")
        with pytest.raises(ValueError, match="检测到恶意软件"):
            code_file_service.upload(
                db=db, user=user, project_id=project.id, upload_file=upload_file,
            )

    def test_malware_threat_name_in_message(self, db, monkeypatch):
        """错误信息应包含威胁名称"""
        monkeypatch.setattr(
            "app.services.code_file_service.get_scanner",
            lambda: _make_infected_scanner("Trojan.Generic.123"),
        )
        user, project = _setup_project(db)

        upload_file = _make_upload_file("trojan.py", b"bad")
        with pytest.raises(ValueError, match="Trojan.Generic.123"):
            code_file_service.upload(
                db=db, user=user, project_id=project.id, upload_file=upload_file,
            )

    def test_clean_file_upload_success(self, db, monkeypatch):
        """干净文件应上传成功"""
        monkeypatch.setattr(
            "app.services.code_file_service.get_scanner", _make_clean_scanner
        )
        user, project = _setup_project(db)

        upload_file = _make_upload_file("clean.py", b"print('clean')\n")
        file_id, _, _ = code_file_service.upload(
            db=db, user=user, project_id=project.id, upload_file=upload_file,
        )
        assert file_id > 0

    def test_archive_with_malware_inside_rejected(self, db, monkeypatch):
        """压缩包内含恶意文件应被拒绝"""
        # 外层扫描通过,内层扫描命中
        outer_scanner = _make_clean_scanner()
        # scan 第一次(外层)返回 clean,第二次(内层)返回 infected
        outer_scanner.scan.side_effect = [
            ScanResult(engine="heuristic", result="clean", degraded=True),
            ScanResult(
                engine="heuristic", result="infected",
                threat_name="Webshell.PHP.Shell", degraded=True,
            ),
        ]
        monkeypatch.setattr(
            "app.services.code_file_service.get_scanner", lambda: outer_scanner
        )
        user, project = _setup_project(db)

        zip_bytes = _make_zip({"shell.php": b'<?php eval(base64_decode("x")); ?>'})
        upload_file = _make_upload_file("evil.zip", zip_bytes)

        with pytest.raises(ValueError, match="压缩包内文件.*检测到恶意软件"):
            code_file_service.upload(
                db=db, user=user, project_id=project.id, upload_file=upload_file,
            )


# ============ 二进制文件入库 ============

class TestUploadBinaryFile:
    """二进制文件入库字段测试"""

    def test_binary_png_upload_success(self, db, monkeypatch):
        """PNG 二进制文件应上传成功并标记 is_binary=1"""
        monkeypatch.setattr(
            "app.services.code_file_service.get_scanner", _make_clean_scanner
        )
        user, project = _setup_project(db)

        png_bytes = b"\x89PNG\r\n\x1a\n\x00\x00\x00\x0dIHDR"
        upload_file = _make_upload_file("logo.png", png_bytes)
        file_id, _, _ = code_file_service.upload(
            db=db, user=user, project_id=project.id, upload_file=upload_file,
        )

        code_file = db.get(CodeFile, file_id)
        assert code_file.is_binary == 1
        assert code_file.original_blob == png_bytes

    def test_binary_file_raw_size_correct(self, db, monkeypatch):
        """二进制文件 raw_size 应等于原始字节数"""
        monkeypatch.setattr(
            "app.services.code_file_service.get_scanner", _make_clean_scanner
        )
        user, project = _setup_project(db)

        png_bytes = b"\x89PNG\r\n\x1a\n\x00\x00\x00\x0dIHDR\x00\x00\x00"
        upload_file = _make_upload_file("logo.png", png_bytes)
        file_id, _, _ = code_file_service.upload(
            db=db, user=user, project_id=project.id, upload_file=upload_file,
        )

        code_file = db.get(CodeFile, file_id)
        assert code_file.raw_size == len(png_bytes)

    def test_binary_file_original_blob_stored(self, db, monkeypatch):
        """二进制文件 original_blob 应存储原始字节"""
        monkeypatch.setattr(
            "app.services.code_file_service.get_scanner", _make_clean_scanner
        )
        user, project = _setup_project(db)

        gif_bytes = b"GIF89a\x01\x00\x01\x00\x00\x00\x00;"
        upload_file = _make_upload_file("anim.gif", gif_bytes)
        file_id, _, _ = code_file_service.upload(
            db=db, user=user, project_id=project.id, upload_file=upload_file,
        )

        code_file = db.get(CodeFile, file_id)
        assert code_file.original_blob == gif_bytes
        assert code_file.is_binary == 1


# ============ 文本文件入库 ============

class TestUploadTextFile:
    """文本文件入库字段测试"""

    def test_text_file_content_stored(self, db, monkeypatch):
        """文本文件 content 应存储 UTF-8 文本"""
        monkeypatch.setattr(
            "app.services.code_file_service.get_scanner", _make_clean_scanner
        )
        user, project = _setup_project(db)

        text = "def hello():\n    print('hi')\n"
        upload_file = _make_upload_file("hello.py", text.encode("utf-8"))
        file_id, _, _ = code_file_service.upload(
            db=db, user=user, project_id=project.id, upload_file=upload_file,
        )

        code_file = db.get(CodeFile, file_id)
        assert code_file.is_binary == 0
        assert code_file.content == text

    def test_text_file_raw_size_correct(self, db, monkeypatch):
        """文本文件 raw_size 应等于 UTF-8 编码字节数"""
        monkeypatch.setattr(
            "app.services.code_file_service.get_scanner", _make_clean_scanner
        )
        user, project = _setup_project(db)

        text = "print('hello')\n"
        upload_file = _make_upload_file("hello.py", text.encode("utf-8"))
        file_id, _, _ = code_file_service.upload(
            db=db, user=user, project_id=project.id, upload_file=upload_file,
        )

        code_file = db.get(CodeFile, file_id)
        assert code_file.raw_size == len(text.encode("utf-8"))

    def test_text_file_original_blob_none(self, db, monkeypatch):
        """文本文件 original_blob 应为 None"""
        monkeypatch.setattr(
            "app.services.code_file_service.get_scanner", _make_clean_scanner
        )
        user, project = _setup_project(db)

        upload_file = _make_upload_file("hello.py", b"print('hi')\n")
        file_id, _, _ = code_file_service.upload(
            db=db, user=user, project_id=project.id, upload_file=upload_file,
        )

        code_file = db.get(CodeFile, file_id)
        assert code_file.original_blob is None
        assert code_file.is_binary == 0


# ============ 压缩包上传 ============

class TestUploadArchive:
    """压缩包上传与解压测试"""

    def test_zip_upload_extracts_multiple_files(self, db, monkeypatch):
        """zip 上传应解压出多个文件"""
        monkeypatch.setattr(
            "app.services.code_file_service.get_scanner", _make_clean_scanner
        )
        user, project = _setup_project(db)

        zip_bytes = _make_zip({
            "main.py": "print('main')\n",
            "utils.py": "def f(): pass\n",
        })
        upload_file = _make_upload_file("project.zip", zip_bytes)
        file_id, lang, ver = code_file_service.upload(
            db=db, user=user, project_id=project.id, upload_file=upload_file,
        )

        files = db.query(CodeFile).filter(CodeFile.project_id == project.id).all()
        assert len(files) == 2
        names = {f.file_name for f in files}
        assert "main.py" in names
        assert "utils.py" in names

    def test_zip_slip_rejected(self, db, monkeypatch):
        """zip slip 攻击(含 ../ 路径)应被拒绝"""
        monkeypatch.setattr(
            "app.services.code_file_service.get_scanner", _make_clean_scanner
        )
        user, project = _setup_project(db)

        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("../../evil.py", "import os\n")
        upload_file = _make_upload_file("evil.zip", buf.getvalue())

        with pytest.raises(ValueError, match="压缩包解压失败"):
            code_file_service.upload(
                db=db, user=user, project_id=project.id, upload_file=upload_file,
            )

    def test_archive_too_many_files_rejected(self, db, monkeypatch):
        """压缩包内文件数超过上限应被拒绝"""
        monkeypatch.setattr(
            "app.services.code_file_service.get_scanner", _make_clean_scanner
        )
        user, project = _setup_project(db)

        files = {f"file_{i}.py": "x = 1\n" for i in range(MAX_EXTRACTED_FILES + 1)}
        zip_bytes = _make_zip(files)
        upload_file = _make_upload_file("big.zip", zip_bytes)

        with pytest.raises(ValueError, match="压缩包解压失败"):
            code_file_service.upload(
                db=db, user=user, project_id=project.id, upload_file=upload_file,
            )

    def test_archive_single_file_too_large_rejected(self, db, monkeypatch):
        """压缩包内单文件超过大小上限应被拒绝"""
        monkeypatch.setattr(
            "app.services.code_file_service.get_scanner", _make_clean_scanner
        )
        user, project = _setup_project(db)

        big_content = b"x" * (ARCHIVE_MAX_SINGLE + 1)
        zip_bytes = _make_zip({"big.bin": big_content})
        upload_file = _make_upload_file("big.zip", zip_bytes)

        with pytest.raises(ValueError, match="压缩包解压失败"):
            code_file_service.upload(
                db=db, user=user, project_id=project.id, upload_file=upload_file,
            )

    def test_archive_creates_independent_records(self, db, monkeypatch):
        """压缩包内每个文件应创建独立的 CodeFile 记录"""
        monkeypatch.setattr(
            "app.services.code_file_service.get_scanner", _make_clean_scanner
        )
        user, project = _setup_project(db)

        zip_bytes = _make_zip({
            "a.py": "a = 1\n",
            "b.py": "b = 2\n",
            "c.py": "c = 3\n",
        })
        upload_file = _make_upload_file("multi.zip", zip_bytes)
        code_file_service.upload(
            db=db, user=user, project_id=project.id, upload_file=upload_file,
        )

        files = db.query(CodeFile).filter(CodeFile.project_id == project.id).all()
        assert len(files) == 3
        # 每个文件应有独立的 ID
        ids = {f.id for f in files}
        assert len(ids) == 3
        # 每个文件应有独立的 content
        contents = {f.content for f in files}
        assert len(contents) == 3

    def test_archive_binary_file_inside(self, db, monkeypatch):
        """压缩包内二进制文件应正确标记 is_binary=1"""
        monkeypatch.setattr(
            "app.services.code_file_service.get_scanner", _make_clean_scanner
        )
        user, project = _setup_project(db)

        zip_bytes = _make_zip({
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


# ============ 校验链顺序 ============

class TestSecurityChainOrder:
    """校验链执行顺序测试"""

    def test_mime_check_before_size_check(self, db):
        """MIME 校验应优先于大小校验(错误信息为 MIME)"""
        user, project = _setup_project(db)

        # .exe 文件 + 超大内容:应先触发 MIME 错误
        big_exe = b"MZ" + b"\x00" * (MAX_SINGLE_FILE_SIZE + 10)
        upload_file = _make_upload_file("evil.exe", big_exe)

        with pytest.raises(ValueError, match="不支持的文件类型"):
            code_file_service.upload(
                db=db, user=user, project_id=project.id, upload_file=upload_file,
            )

    def test_size_check_before_malware_scan(self, db, monkeypatch):
        """大小校验应优先于恶意软件扫描(不触发扫描)"""
        scan_called = MagicMock(return_value=False)
        scanner = MagicMock()
        scanner.scan = scan_called
        monkeypatch.setattr(
            "app.services.code_file_service.get_scanner", lambda: scanner
        )
        user, project = _setup_project(db)

        # 合法 .py 文件 + 超大内容:应触发大小错误,不触发扫描
        big_content = b"x" * (MAX_SINGLE_FILE_SIZE + 1)
        upload_file = _make_upload_file("big.py", big_content)

        with pytest.raises(ValueError, match="超过单文件上限"):
            code_file_service.upload(
                db=db, user=user, project_id=project.id, upload_file=upload_file,
            )
        # 扫描器不应被调用(大小校验先失败)
        assert scan_called.call_count == 0

    def test_malware_scan_before_archive_extraction(self, db, monkeypatch):
        """恶意软件扫描应优先于解压(外层扫描命中即拒绝)"""
        monkeypatch.setattr(
            "app.services.code_file_service.get_scanner",
            lambda: _make_infected_scanner("Zip.Malware"),
        )
        user, project = _setup_project(db)

        zip_bytes = _make_zip({"clean.py": "print('hi')\n"})
        upload_file = _make_upload_file("malware.zip", zip_bytes)

        with pytest.raises(ValueError, match="检测到恶意软件"):
            code_file_service.upload(
                db=db, user=user, project_id=project.id, upload_file=upload_file,
            )


# ============ 集成:端到端 ============

class TestEndToEnd:
    """端到端集成测试"""

    def test_full_chain_success_text(self, db, monkeypatch):
        """完整校验链通过:文本文件入库"""
        monkeypatch.setattr(
            "app.services.code_file_service.get_scanner", _make_clean_scanner
        )
        user, project = _setup_project(db)

        text = "def add(a, b):\n    return a + b\n"
        upload_file = _make_upload_file("math.py", text.encode("utf-8"))
        file_id, lang, ver = code_file_service.upload(
            db=db, user=user, project_id=project.id, upload_file=upload_file,
        )

        code_file = db.get(CodeFile, file_id)
        assert code_file.is_binary == 0
        assert code_file.content == text
        assert code_file.raw_size == len(text.encode("utf-8"))
        assert code_file.original_blob is None
        assert code_file.status == "active"
        assert ver == 1

    def test_full_chain_success_binary(self, db, monkeypatch):
        """完整校验链通过:二进制文件入库"""
        monkeypatch.setattr(
            "app.services.code_file_service.get_scanner", _make_clean_scanner
        )
        user, project = _setup_project(db)

        png_bytes = b"\x89PNG\r\n\x1a\n\x00\x00\x00\x0dIHDR\x00\x00\x00"
        upload_file = _make_upload_file("logo.png", png_bytes)
        file_id, lang, ver = code_file_service.upload(
            db=db, user=user, project_id=project.id, upload_file=upload_file,
        )

        code_file = db.get(CodeFile, file_id)
        assert code_file.is_binary == 1
        assert code_file.original_blob == png_bytes
        assert code_file.raw_size == len(png_bytes)

    def test_multiple_uploads_accumulate_raw_size(self, db, monkeypatch):
        """多次上传应累积 raw_size 用于项目总大小校验"""
        monkeypatch.setattr(
            "app.services.code_file_service.get_scanner", _make_clean_scanner
        )
        user, project = _setup_project(db)

        # 第一次上传 5MB
        content1 = b"x" * (5 * 1024 * 1024)
        upload1 = _make_upload_file("a.py", content1)
        code_file_service.upload(
            db=db, user=user, project_id=project.id, upload_file=upload1,
        )

        # 第二次上传 6MB,总计 11MB(未超 500MB,应成功)
        content2 = b"y" * (6 * 1024 * 1024)
        upload2 = _make_upload_file("b.py", content2)
        file_id2, _, _ = code_file_service.upload(
            db=db, user=user, project_id=project.id, upload_file=upload2,
        )
        assert file_id2 > 0

        # 验证项目总 raw_size 累积正确
        total = code_file_service._get_project_total_size(db, project.id)
        assert total == len(content1) + len(content2)


def test_fail_closed_rejects_degraded_scan_result(db, monkeypatch):
    """生产 fail-closed 开启时，主扫描能力降级必须拒绝上传。"""
    monkeypatch.setattr(code_file_service.settings, "malware_scan_fail_closed", True)
    monkeypatch.setattr(
        "app.services.code_file_service.get_scanner",
        _make_clean_scanner,
    )
    user, project = _setup_project(db)

    with pytest.raises(ValueError, match="恶意软件扫描服务暂不可用"):
        code_file_service.upload(
            db=db,
            user=user,
            project_id=project.id,
            upload_file=_make_upload_file("safe.py", b"print('safe')\n"),
        )


def test_fail_closed_rejects_scanner_timeout(db, monkeypatch):
    """生产 fail-closed 开启时，扫描超时必须拒绝上传。"""
    scanner = MagicMock()
    scanner.scan.return_value = ScanResult(
        engine="clamav",
        result="timeout",
        degraded=True,
        detail="clamav scan timed out",
    )
    monkeypatch.setattr(code_file_service.settings, "malware_scan_fail_closed", True)
    monkeypatch.setattr(
        "app.services.code_file_service.get_scanner",
        lambda: scanner,
    )
    user, project = _setup_project(db)

    with pytest.raises(ValueError, match="恶意软件扫描服务暂不可用"):
        code_file_service.upload(
            db=db,
            user=user,
            project_id=project.id,
            upload_file=_make_upload_file("safe.py", b"print('safe')\n"),
        )
