"""部署后黑盒/白盒验证 runner 的失败状态回归。"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
from pathlib import Path

import pytest

from app.services.sandbox_service import _DEPLOY_VERIFY_RUNNER, _extract_blackbox_result


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.mark.parametrize(
    ("status", "expected_code"),
    [(200, 0), (204, 0), (302, 1), (401, 1), (404, 1), (500, 1)],
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
    if status >= 300:
        assert "blackbox: no discovered application route returned HTTP 2xx" in result.stdout
        assert '"passed":false' in result.stdout
        assert "PRISM_VERIFY blackbox fail" in result.stdout
    else:
        assert '"passed":true' in result.stdout
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


def test_real_runner_separates_route_success_from_agent_assertion_failure(tmp_path: Path) -> None:
    """HTTP 路由可达但 AI 黑盒断言失败时，回执必须归因为动态断言失败。"""
    source = tmp_path / "source"
    source.mkdir()
    (source / "wsgi.py").write_text(
        "# path('/healthz', health)\n"
        "def application(environ, start_response):\n"
        "    status = '204 No Content' if environ.get('PATH_INFO') == '/healthz' else '404 Not Found'\n"
        "    start_response(status, [])\n"
        "    return []\n",
        encoding="utf-8",
    )
    tests_dir = source / "_agent_tests"
    tests_dir.mkdir()
    (tests_dir / "blackbox.py").write_text("raise AssertionError('expected failure')\n", encoding="utf-8")
    workspace = tmp_path / "workspace"
    runner = Path(__file__).resolve().parents[4] / "deploy" / "sandbox" / "runner.sh"
    env = {
        **os.environ,
        "PATH": f"{Path(sys.executable).parent}:{os.environ.get('PATH', '')}",
        "PRISM_ACTION": "test",
        "PRISM_LANGUAGE": "python",
        "PRISM_TEST_MODE": "blackbox",
        "PRISM_PREVIEW_PORT": str(_free_port()),
        "PRISM_SOURCE_DIR": str(source),
        "PRISM_WORKSPACE_DIR": str(workspace),
    }
    result = subprocess.run(
        ["sh", str(runner)],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert result.returncode != 0
    receipt = _extract_blackbox_result(result.stdout + result.stderr)
    assert receipt is not None
    assert receipt["route_passed"] is True
    assert receipt["status"] == "failed"
    assert receipt["failure_kind"] == "dynamic_assertion"


@pytest.mark.parametrize(
    "app_source",
    [
        "print('controlled startup output; server did not start')\n",
        "print('controlled startup output; server crashed')\nraise RuntimeError('controlled startup crash')\n",
    ],
)
def test_real_runner_reports_application_startup_failure_with_diagnostics(
    tmp_path: Path, app_source: str
) -> None:
    """应用入口正常退出或崩溃时必须保留启动日志并返回结构化失败回执。"""
    source = tmp_path / "source"
    source.mkdir()
    (source / "app.py").write_text(app_source, encoding="utf-8")
    workspace = tmp_path / "workspace"
    runner = Path(__file__).resolve().parents[4] / "deploy" / "sandbox" / "runner.sh"
    env = {
        **os.environ,
        "PATH": f"{Path(sys.executable).parent}:{os.environ.get('PATH', '')}",
        "PRISM_ACTION": "test",
        "PRISM_LANGUAGE": "python",
        "PRISM_TEST_MODE": "blackbox",
        "PRISM_PREVIEW_PORT": str(_free_port()),
        "PRISM_SOURCE_DIR": str(source),
        "PRISM_WORKSPACE_DIR": str(workspace),
    }

    result = subprocess.run(
        ["sh", str(runner)],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )

    output = result.stdout + result.stderr
    assert result.returncode != 0, output
    assert "blackbox application exited before readiness" in output
    assert "controlled startup output" in output
    receipt = _extract_blackbox_result(output)
    assert receipt is not None
    assert receipt["status"] == "failed"
    assert receipt["route_passed"] is False
    assert receipt["status_code"] == 0
    assert receipt["failure_kind"] == "application_startup"
    assert receipt["failure_reason"] == "application_exited_before_ready"


def test_real_runner_reports_readiness_timeout_with_structured_failure(tmp_path: Path) -> None:
    """入口持续运行但未监听 HTTP 端口时不能把就绪超时当作成功。"""
    source = tmp_path / "source"
    source.mkdir()
    (source / "main.py").write_text("import time\ntime.sleep(60)\n", encoding="utf-8")
    workspace = tmp_path / "workspace"
    runner = Path(__file__).resolve().parents[4] / "deploy" / "sandbox" / "runner.sh"
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_sleep = fake_bin / "sleep"
    fake_sleep.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    fake_sleep.chmod(0o755)
    env = {
        **os.environ,
        "PATH": f"{fake_bin}:{Path(sys.executable).parent}:{os.environ.get('PATH', '')}",
        "PRISM_ACTION": "test",
        "PRISM_LANGUAGE": "python",
        "PRISM_TEST_MODE": "blackbox",
        "PRISM_PREVIEW_PORT": str(_free_port()),
        "PRISM_SOURCE_DIR": str(source),
        "PRISM_WORKSPACE_DIR": str(workspace),
    }

    result = subprocess.run(
        ["sh", str(runner)],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )

    output = result.stdout + result.stderr
    assert result.returncode != 0, output
    assert "application did not become ready" in output
    receipt = _extract_blackbox_result(output)
    assert receipt is not None
    assert receipt["status"] == "failed"
    assert receipt["route_passed"] is False
    assert receipt["status_code"] == 0
    assert receipt["failure_kind"] == "application_startup"
    assert receipt["failure_reason"] == "application_readiness_timeout"


def test_embedded_node_blackbox_discovers_route_without_python3(tmp_path: Path) -> None:
    """非 Python 镜像必须能靠固定 shell 工具发现 API 路由，不依赖 python3。"""
    (tmp_path / "server.js").write_text(
        "const http = require('http');\n"
        "const routes = [];\n"
        "const app = { get: (path, handler) => routes.push([path, handler]) };\n"
        "app.get('/api/v1/health', (_req, res) => { res.statusCode = 204; res.end(); });\n"
        "http.createServer((req, res) => {\n"
        "  const route = routes.find(([path]) => path === req.url);\n"
        "  if (route) return route[1](req, res);\n"
        "  res.statusCode = 404; res.end('not found');\n"
        "}).listen(Number(process.env.PRISM_PREVIEW_PORT), '127.0.0.1');\n",
        encoding="utf-8",
    )
    runner = tmp_path / "_prism_verify.sh"
    runner.write_text(_DEPLOY_VERIFY_RUNNER, encoding="utf-8")
    restricted_bin = tmp_path / "bin"
    restricted_bin.mkdir()
    for command in (
        "sh", "bash", "node", "grep", "find", "xargs", "sed", "awk", "head",
        "sleep", "cat", "tail", "cp", "mkdir", "chmod", "rm", "tr", "base64",
        "cut", "dirname", "sort",
    ):
        located = shutil.which(command)
        if located:
            (restricted_bin / command).symlink_to(located)
    assert not (restricted_bin / "python").exists()
    assert not (restricted_bin / "python3").exists()
    env = {
        **os.environ,
        "PATH": str(restricted_bin),
        "PRISM_LANGUAGE": "node",
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

    assert result.returncode == 0, result.stdout + result.stderr
    assert "blackbox probe /api/v1/health -> 204" in result.stdout
    assert '"route":"/api/v1/health","status_code":204' in result.stdout
