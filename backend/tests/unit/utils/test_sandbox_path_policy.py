"""沙箱导入路径服务器端白名单：正向放行、负向全部拒绝。"""

import base64
import io
import stat
import zipfile

import pytest

from app.core.exceptions import ValidationError
from app.services import sandbox_service
from app.utils.sandbox_path_policy import validate_sandbox_relative_path, validate_sandbox_zip


@pytest.mark.parametrize("path", [
    "src/main.py", "web/index.php", "测试/用例_1.py", "pkg/Foo$Bar.class",
    "app/[slug]/page.tsx", "docs/100%.md", "a/b.c/d-e+f@g.txt",
])
def test_allowed_paths_pass(path):
    assert validate_sandbox_relative_path(path) == path


@pytest.mark.parametrize("path", [
    "../etc/passwd", "a/../../b", "./a", "a//b", "a/./b",           # 穿越与点段
    "%2e%2e/etc/passwd", "a/%2e%2e%2fb", "%252e%252e%252fetc",       # 编码、双重编码穿越
    "..%2f..%2fetc", "a%5c..%5cb", "%00x",
    "/etc/passwd", "C:/Windows/win.ini", "c:evil",                     # 绝对路径与盘符
    "a\\..\\b", "a\x00b.php", "a\nb", "a\x7fb",                       # 反斜杠、空字节、控制字符
    "file:///etc/passwd", "php://filter/resource=x", "data:text/plain,1",
    "zip://a.zip#b", "phar://x", "expect://id", "a/b:c",               # URI 包装器与设备路径
    "a/<b>", "a/b|c", "a/b*",                                          # 白名单外字符
    "", "a/" + "x" * 300,
])
def test_rejected_paths(path):
    with pytest.raises(ValidationError):
        validate_sandbox_relative_path(path)


def _zip(members):
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as zf:
        for name, mode in members:
            info = zipfile.ZipInfo(name)
            if mode is not None:
                info.external_attr = mode << 16
            zf.writestr(info, "x")
    return out.getvalue()


def test_zip_with_safe_members_passes():
    validate_sandbox_zip(_zip([("src/", stat.S_IFDIR | 0o755), ("src/a.py", stat.S_IFREG | 0o644),
                               ("b.txt", None)]))


@pytest.mark.parametrize("members", [
    [("../evil.py", None)],
    [("src/%2e%2e%2fevil.py", None)],
    [("/abs.py", None)],
    [("link", stat.S_IFLNK | 0o777)],           # 符号链接越界
    [("fifo", stat.S_IFIFO | 0o644)],
    [("php://filter", None)],
])
def test_zip_with_unsafe_member_rejected(members):
    with pytest.raises(ValidationError):
        validate_sandbox_zip(_zip(members))


def test_zip_input_is_validated_before_reaching_worker():
    with pytest.raises(ValidationError):
        sandbox_service._normalize_source_archive_for_worker(_zip([("../x.py", None)]), "src.zip")
    good = _zip([("a.py", None)])
    assert sandbox_service._normalize_source_archive_for_worker(good, "src.zip") == (good, "src.zip")


def test_agent_generated_test_paths_are_whitelisted():
    source = base64.b64encode(_zip([("a.py", None)])).decode()
    out = sandbox_service._inject_agent_test_files(source, [{"path": "test_a.py", "content": "assert 1"}])
    with zipfile.ZipFile(io.BytesIO(base64.b64decode(out))) as zf:
        assert "_agent_tests/test_a.py" in zf.namelist()
    with pytest.raises(ValidationError):
        sandbox_service._inject_agent_test_files(source, [{"path": "../../app.py", "content": "x"}])
