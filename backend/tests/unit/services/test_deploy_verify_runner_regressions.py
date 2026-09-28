"""部署后黑盒/白盒验证 runner 的失败状态回归。"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
from pathlib import Path

import pytest

from app.services.sandbox_service import _DEPLOY_VERIFY_RUNNER


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.mark.parametrize(
    ("status", "expected_code"),
    [(200, 0), (404, 1), (500, 1)],
)
def test_embedded_blackbox_runner_requires_success_http_status(
    tmp_path: Path,
    status: int,
    expected_code: int,
) -> None:
    """有正文的 4xx/5xx 错误页不能被当作黑盒验证通过。"""
    app = tmp_path / "main.py"
    app.write_text(
        "from http.server import BaseHTTPRequestHandler, HTTPServer\n"
        "import os\n"
        "class Handler(BaseHTTPRequestHandler):\n"
        "    def do_GET(self):\n"
        f"        self.send_response({status})\n"
        "        self.end_headers()\n"
        "        self.wfile.write(b'non-empty response body')\n"
        "    def log_message(self, *_args):\n"
        "        pass\n"
        "HTTPServer(('127.0.0.1', int(os.environ['PRISM_PREVIEW_PORT'])), Handler).serve_forever()\n",
        encoding="utf-8",
    )
    runner = tmp_path / "_prism_verify.sh"
    runner.write_text(_DEPLOY_VERIFY_RUNNER, encoding="utf-8")
    env = {
        **os.environ,
        "PATH": f"{Path(sys.executable).parent}:{os.environ.get('PATH', '')}",
        "PRISM_LANGUAGE": "python",
        "PRISM_PREVIEW_PORT": str(_free_port()),
        "PRISM_WORKSPACE": str(tmp_path),
    }

    result = subprocess.run(
        ["sh", str(runner), "blackbox"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )

    assert result.returncode == expected_code, result.stdout + result.stderr
    if status >= 400:
        assert f"blackbox: 首页返回 HTTP {status}" in result.stdout
        assert "PRISM_VERIFY blackbox fail" in result.stdout
    else:
        assert "PRISM_VERIFY blackbox ok" in result.stdout


def test_embedded_go_whitebox_fails_when_go_vet_fails(tmp_path: Path) -> None:
    """Go 静态检查失败必须向白盒总状态传播，不能被 || true 吞掉。"""
    runner = tmp_path / "_prism_verify.sh"
    runner.write_text(_DEPLOY_VERIFY_RUNNER, encoding="utf-8")
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_go = fake_bin / "go"
    fake_go.write_text("#!/bin/sh\nexit 7\n", encoding="utf-8")
    fake_go.chmod(0o755)
    env = {
        **os.environ,
        "PATH": f"{fake_bin}:{Path(sys.executable).parent}:{os.environ.get('PATH', '')}",
        "PRISM_LANGUAGE": "go",
        "PRISM_WORKSPACE": str(tmp_path),
    }

    result = subprocess.run(
        ["sh", str(runner), "whitebox"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )

    assert result.returncode == 1, result.stdout + result.stderr
    assert "go vet: static analysis failed" in result.stdout
    assert "PRISM_VERIFY whitebox fail" in result.stdout
