"""单元测试:压缩包自动解压工具(archive_extractor)

覆盖正常解压、zip slip 防护、文件数量/大小限制等场景。
"""
from __future__ import annotations

import io
import tarfile
import zipfile

import pytest

from app.core.exceptions import ValidationError
from app.utils.archive_extractor import (
    MAX_EXTRACTED_FILES,
    MAX_SINGLE_FILE_SIZE,
    extract_archive,
    is_archive,
)

# ============ is_archive ============

class TestIsArchive:
    """is_archive() 格式识别测试"""

    @pytest.mark.parametrize("filename", [
        "test.zip", "test.tar.gz", "test.tgz", "test.tar.bz2",
        "test.tar.xz", "test.tar", "TEST.ZIP", "Test.Tar.Gz",
    ])
    def test_supported_formats(self, filename):
        """支持的压缩包格式应返回 True"""
        assert is_archive(filename) is True

    @pytest.mark.parametrize("filename", [
        "test.py", "test.js", "test.txt", "test.json", "test.rar", "test.7z", "",
    ])
    def test_unsupported_formats(self, filename):
        """不支持的格式应返回 False"""
        assert is_archive(filename) is False


# ============ 正常解压 ============

class TestExtractZip:
    """zip 格式正常解压测试"""

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

    def test_extract_simple_zip(self):
        """简单 zip 应正确解压出文件"""
        raw = self._make_zip({
            "main.py": "print('hello')\n",
            "utils.py": "def f(): pass\n",
        })
        files = extract_archive(raw, "test.zip")
        assert len(files) == 2
        names = {f.name for f in files}
        assert "main.py" in names
        assert "utils.py" in names

    def test_extract_nested_directories(self):
        """嵌套目录路径应保留"""
        raw = self._make_zip({
            "src/main.py": "print('main')\n",
            "src/utils/helper.py": "def f(): pass\n",
        })
        files = extract_archive(raw, "test.zip")
        assert len(files) == 2
        paths = {f.path for f in files}
        assert "src/main.py" in paths
        assert "src/utils/helper.py" in paths

    def test_extract_text_file_content(self):
        """文本文件 content 应为 UTF-8 字符串"""
        raw = self._make_zip({"hello.py": "print('hello')\n"})
        files = extract_archive(raw, "test.zip")
        assert files[0].content == "print('hello')\n"
        assert files[0].is_binary is False

    def test_extract_binary_file(self):
        """二进制文件应标记 is_binary=True"""
        raw = self._make_zip({"image.png": b"\x89PNG\r\n\x1a\n\x00\x00\x00"})
        files = extract_archive(raw, "test.zip")
        assert files[0].is_binary is True
        assert files[0].raw_bytes is not None

    def test_extract_skips_directories(self):
        """目录条目应被跳过"""
        raw = self._make_zip({
            "dir/": "",
            "dir/file.py": "x = 1\n",
        })
        files = extract_archive(raw, "test.zip")
        assert len(files) == 1
        assert files[0].name == "file.py"


class TestExtractTar:
    """tar/tar.gz/tar.bz2 格式解压测试"""

    def _make_tar(self, files: dict, mode: str = "w:gz") -> bytes:
        """构造内存 tar

        Args:
            files: {路径: 内容} 字典
            mode: tarfile 打开模式

        Returns:
            bytes: tar 字节流
        """
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode=mode) as tf:
            for path, content in files.items():
                if isinstance(content, str):
                    content = content.encode("utf-8")
                info = tarfile.TarInfo(name=path)
                info.size = len(content)
                tf.addfile(info, io.BytesIO(content))
        return buf.getvalue()

    def test_extract_tar_gz(self):
        """tar.gz 应正确解压"""
        raw = self._make_tar({"main.py": "print('hi')\n"}, mode="w:gz")
        files = extract_archive(raw, "test.tar.gz")
        assert len(files) == 1
        assert files[0].content == "print('hi')\n"

    def test_extract_tar_bz2(self):
        """tar.bz2 应正确解压"""
        raw = self._make_tar({"main.py": "print('hi')\n"}, mode="w:bz2")
        files = extract_archive(raw, "test.tar.bz2")
        assert len(files) == 1

    def test_extract_plain_tar(self):
        """plain tar 应正确解压"""
        raw = self._make_tar({"main.py": "print('hi')\n"}, mode="w:")
        files = extract_archive(raw, "test.tar")
        assert len(files) == 1


# ============ 安全校验 ============

class TestSecurityChecks:
    """安全校验测试(zip slip / 超限)"""

    def test_zip_slip_rejected(self):
        """含 ../ 的 zip 路径应被拒绝"""
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("../../evil.py", "import os\n")
        with pytest.raises(ValidationError, match="zip slip"):
            extract_archive(buf.getvalue(), "evil.zip")

    def test_absolute_path_rejected(self):
        """绝对路径应被拒绝(Windows 盘符形式)"""
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            # zip 不会对反斜杠做处理,触发 Windows 盘符防护
            zf.writestr("C:/Windows/evil.py", "import os\n")
        with pytest.raises(ValidationError, match="zip slip"):
            extract_archive(buf.getvalue(), "evil.zip")

    def test_posix_absolute_path_rejected_before_normalization(self):
        """POSIX 绝对路径不得被去掉前导斜杠后接受。"""
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("/etc/evil.py", "import os\n")
        with pytest.raises(ValidationError, match="绝对路径"):
            extract_archive(buf.getvalue(), "evil.zip")

    def test_too_many_files_rejected(self):
        """超过文件数量上限应被拒绝"""
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            for i in range(MAX_EXTRACTED_FILES + 1):
                zf.writestr(f"file_{i}.py", "x = 1\n")
        with pytest.raises(ValidationError, match="文件数量"):
            extract_archive(buf.getvalue(), "big.zip")

    def test_single_file_too_large_rejected(self):
        """单个文件超过大小上限应被拒绝"""
        buf = io.BytesIO()
        big_content = b"x" * (MAX_SINGLE_FILE_SIZE + 1)
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("big.bin", big_content)
        with pytest.raises(ValidationError, match="大小"):
            extract_archive(buf.getvalue(), "big.zip")

    def test_sensitive_files_filtered(self):
        """敏感文件(.env/.ssh 等)应被过滤跳过"""
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr(".env", "SECRET=xxx\n")
            zf.writestr("normal.py", "x = 1\n")
        files = extract_archive(buf.getvalue(), "test.zip")
        # .env 被过滤,只保留 normal.py
        assert len(files) == 1
        assert files[0].name == "normal.py"

    def test_hidden_directories_filtered(self):
        """隐藏目录(.git/、__pycache__/)应被过滤"""
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr(".git/config", "[core]\n")
            zf.writestr("__pycache__/main.cpython-39.pyc", "binary")
            zf.writestr("src/main.py", "print('hi')\n")
        files = extract_archive(buf.getvalue(), "test.zip")
        assert len(files) == 1
        assert files[0].name == "main.py"

    def test_empty_archive_rejected(self):
        """空压缩包应被拒绝"""
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w"):
            pass
        with pytest.raises(ValidationError, match="没有可用的文件"):
            extract_archive(buf.getvalue(), "empty.zip")

    def test_corrupted_zip_rejected(self):
        """损坏的 zip 应被拒绝"""
        with pytest.raises(ValidationError, match="损坏"):
            extract_archive(b"not a zip file", "bad.zip")

    def test_unsupported_format_rejected(self):
        """不支持的格式应被拒绝"""
        with pytest.raises(ValidationError, match="不支持"):
            extract_archive(b"x", "test.rar")
