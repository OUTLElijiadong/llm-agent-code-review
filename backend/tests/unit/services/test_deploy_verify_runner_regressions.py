"""部署后黑盒/白盒验证 runner 的失败状态回归。"""

from __future__ import annotations

import base64
import os
import shutil
import signal
import socket
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from app.services.sandbox_service import (
    _DEPLOY_VERIFY_RUNNER,
    _extract_blackbox_result,
)


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _http_scope(*requests: tuple[str, tuple[int, ...]]) -> tuple[list[dict[str, object]], str]:
    """Encode the runner's exact GET plan and return its expected receipt contract."""
    scope = [
        {"method": "GET", "path": path, "query": {}, "expected_statuses": list(statuses)}
        for path, statuses in requests
    ]
    lines = [
        f"GET\t{item['path']}\t{','.join(str(code) for code in item['expected_statuses']) or '-'}"
        for item in scope
    ]
    encoded = base64.b64encode(("\n".join(lines) + "\n").encode("ascii")).decode("ascii")
    return scope, encoded


def _add_http_scope(env: dict[str, str], *requests: tuple[str, tuple[int, ...]]) -> list[dict[str, object]]:
    scope, encoded = _http_scope(*requests)
    env["PRISM_HTTP_SCOPE_B64"] = encoded
    return scope


def _fake_php_server(tmp_path: Path) -> Path:
    """Provide a deterministic PHP-like server for runner root-discovery tests."""
    bin_dir = tmp_path / "fake-bin"
    bin_dir.mkdir()
    fake_php = bin_dir / "php"
    fake_php.write_text(
        f"#!{sys.executable}\n"
        + textwrap.dedent(
            """\
            import http.server
            import json
            import os
            import pathlib
            import sys
            args = sys.argv[1:]
            if args and args[0] == '-S':
                port = int(args[1].rsplit(':', 1)[1])
                root = pathlib.Path(args[args.index('-t') + 1]).resolve()
                class Handler(http.server.BaseHTTPRequestHandler):
                    def do_GET(self):
                        path = self.path.split('?', 1)[0].lstrip('/')
                        target = (root / path).resolve()
                        has_index = (root / 'index.php').is_file() or (root / 'index.html').is_file()
                        if self.path.split('?', 1)[0] == '/' and has_index:
                            status = int(os.environ.get('FAKE_PHP_ROOT_STATUS', '200'))
                        elif path == 'index.php' and target.is_file():
                            status = int(os.environ.get('FAKE_PHP_INDEX_STATUS', '200'))
                        elif target.is_file() and target.is_relative_to(root):
                            status = 200
                        else:
                            status = 404
                        self.send_response(status)
                        self.end_headers()
                        self.wfile.write(b'ok')
                    def log_message(self, *args):
                        pass
                http.server.HTTPServer(('127.0.0.1', port), Handler).serve_forever()
            else:
                print('PRISM_FACTS_BEGIN')
                facts = {'entrypoints': [], 'test_files': {'found': 0, 'framework': ''},
                         'endpoints': [], 'param_hints': [], 'hardcoded_secrets': []}
                print(json.dumps(facts))
                print('PRISM_FACTS_END')
            """
        ),
        encoding="utf-8",
    )
    fake_php.chmod(0o755)
    return bin_dir


def _write_bWAPP_layout(root: Path) -> None:
    app = root / "bWAPP-master" / "app"
    (app / "admin").mkdir(parents=True)
    (app / "index.php").write_text("<?php echo 'bWAPP'; ?>", encoding="utf-8")
    (app / "admin" / "index.php").write_text("<?php echo 'admin'; ?>", encoding="utf-8")


def _run_worker_php_blackbox(
    source: Path, work: Path, fake_bin: Path, *, root_status: int | None = None, index_status: int | None = None,
    http_scope: list[dict[str, object]] | None = None,
) -> subprocess.CompletedProcess[str]:
    runner = Path(__file__).resolve().parents[4] / "deploy" / "sandbox" / "runner.sh"
    scope, encoded_scope = _http_scope(("/", (200,)))
    if http_scope is not None:
        scope = http_scope
        lines = []
        for item in scope:
            path = str(item["path"])
            statuses = item.get("expected_statuses", [])
            lines.append(f"GET\t{path}\t{','.join(str(code) for code in statuses) or '-'}")
        encoded_scope = base64.b64encode(("\n".join(lines) + "\n").encode("ascii")).decode("ascii")
    env = {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ.get('PATH', '')}",
        "PRISM_ACTION": "test",
        "PRISM_LANGUAGE": "php",
        "PRISM_TEST_MODE": "blackbox",
        "PRISM_HTTP_SCOPE_B64": encoded_scope,
        "PRISM_PREVIEW_PORT": str(_free_port()),
        "PRISM_SOURCE_DIR": str(source),
        "PRISM_WORKSPACE_DIR": str(work),
        "PRISM_TEST_TIMEOUT_SECONDS": "20",
        "PRISM_BLACKBOX_STARTUP_TIMEOUT_SECONDS": "5",
        "PRISM_BLACKBOX_PROBE_BUDGET_SECONDS": "5",
    }
    if root_status is not None:
        env["FAKE_PHP_ROOT_STATUS"] = str(root_status)
    if index_status is not None:
        env["FAKE_PHP_INDEX_STATUS"] = str(index_status)
    return subprocess.run(
        ["bash", str(runner)],
        cwd=source,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


@pytest.mark.parametrize("layout", ["nested", "root", "public"])
def test_worker_php_blackbox_finds_nested_document_root(tmp_path: Path, layout: str) -> None:
    source = tmp_path / "source"
    source.mkdir()
    if layout == "nested":
        _write_bWAPP_layout(source)
    elif layout == "root":
        (source / "index.php").write_text("<?php echo 'root'; ?>", encoding="utf-8")
    else:
        public = source / "public"
        public.mkdir()
        (public / "index.php").write_text("<?php echo 'public'; ?>", encoding="utf-8")
    result = _run_worker_php_blackbox(source, tmp_path / "workspace", _fake_php_server(tmp_path))
    output = result.stdout + result.stderr
    assert result.returncode == 0, output
    assert '"passed":true' in output
    assert '"status_code":200' in output


def test_worker_php_blackbox_falls_back_to_safe_php_entrypoint_when_root_is_404(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    _write_bWAPP_layout(source)
    app = source / "bWAPP-master" / "app"
    (app / "login.php").write_text("<?php echo 'login'; ?>", encoding="utf-8")

    scope, _ = _http_scope(("/", (404,)), ("/login.php", (200,)))
    result = _run_worker_php_blackbox(
        source,
        tmp_path / "workspace",
        _fake_php_server(tmp_path),
        root_status=404,
        index_status=302,
        http_scope=scope,
    )
    output = result.stdout + result.stderr

    assert result.returncode == 0, output
    receipt = _extract_blackbox_result(output, expected_http_scope=scope)
    assert receipt is not None
    assert receipt["status"] == "passed"
    assert receipt["requests"][0]["target"] == "/"
    assert receipt["requests"][0]["status_code"] == 404
    assert receipt["requests"][1]["target"] == "/login.php"
    assert receipt["requests"][1]["status_code"] == 200


def test_worker_php_blackbox_fails_closed_for_ambiguous_document_roots(tmp_path: Path) -> None:
    source = tmp_path / "source"
    (source / "frontend").mkdir(parents=True)
    (source / "backend").mkdir()
    (source / "frontend" / "index.php").write_text("<?php echo 'front'; ?>", encoding="utf-8")
    (source / "backend" / "index.php").write_text("<?php echo 'back'; ?>", encoding="utf-8")
    result = _run_worker_php_blackbox(source, tmp_path / "workspace", _fake_php_server(tmp_path))
    output = result.stdout + result.stderr
    assert result.returncode == 1, output
    scope, _ = _http_scope(("/", (200,)))
    receipt = _extract_blackbox_result(output, expected_http_scope=scope)
    assert receipt is not None
    assert receipt["failure_kind"] == "application_startup", output
    assert receipt["request_count"] == 0
    assert "scoped application exited before readiness" in output


@pytest.mark.parametrize("layout", ["empty", "too_deep"])
def test_worker_php_blackbox_fails_closed_when_document_root_is_missing(
    tmp_path: Path, layout: str
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    if layout == "too_deep":
        nested = source.joinpath(*[f"level-{index}" for index in range(9)])
        nested.mkdir(parents=True)
        (nested / "index.php").write_text("<?php echo 'too deep'; ?>", encoding="utf-8")

    result = _run_worker_php_blackbox(source, tmp_path / "workspace", _fake_php_server(tmp_path))
    output = result.stdout + result.stderr
    assert result.returncode == 1, output
    scope, _ = _http_scope(("/", (200,)))
    receipt = _extract_blackbox_result(output, expected_http_scope=scope)
    assert receipt is not None
    assert receipt["status"] == "failed"
    assert receipt["failure_kind"] == "application_startup"
    assert receipt["request_count"] == 0
    assert "scoped application exited before readiness" in output
    assert '"status_code":0' in output


def test_embedded_deploy_verifier_fails_closed_when_php_document_root_is_missing(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    runner = tmp_path / "_prism_verify.sh"
    runner.write_text(_DEPLOY_VERIFY_RUNNER, encoding="utf-8")
    fake_bin = _fake_php_server(tmp_path)
    env = {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ.get('PATH', '')}",
        "PRISM_LANGUAGE": "php",
        "PRISM_PREVIEW_PORT": str(_free_port()),
        "PRISM_WORKSPACE": str(workspace),
        "PRISM_STARTUP_TIMEOUT_SECONDS": "5",
    }

    result = subprocess.run(
        ["bash", str(runner), "blackbox"],
        cwd=workspace,
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    output = result.stdout + result.stderr
    assert result.returncode != 0, output
    assert "PHP document root not found within supported depth (8)" in output
    assert "PRISM_BLACKBOX_DONE" in output
    assert '"passed":false' in output


def test_embedded_deploy_verifier_finds_nested_php_document_root(tmp_path: Path) -> None:
    from app.services.sandbox_service import _DEPLOY_VERIFY_RUNNER

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _write_bWAPP_layout(workspace)
    runner = tmp_path / "_prism_verify.sh"
    runner.write_text(_DEPLOY_VERIFY_RUNNER, encoding="utf-8")
    fake_bin = _fake_php_server(tmp_path)
    env = {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ.get('PATH', '')}",
        "PRISM_LANGUAGE": "php",
        "PRISM_PREVIEW_PORT": str(_free_port()),
        "PRISM_WORKSPACE": str(workspace),
        "PRISM_STARTUP_TIMEOUT_SECONDS": "5",
    }
    result = subprocess.run(
        ["bash", str(runner), "blackbox"],
        cwd=workspace,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    output = result.stdout + result.stderr
    assert result.returncode == 0, output
    assert '"passed":true' in output
    assert '"status_code":200' in output


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
        "def route(path): return path\n"
        "route('/health')\n"
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
        ["bash", str(runner), "blackbox"],
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
        ["bash", str(runner), "whitebox"],
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


def test_embedded_blackbox_bounds_connected_socket_without_http_status(tmp_path: Path) -> None:
    """部署核验 runner 遇到 TCP 接通但无 HTTP 首行时，必须按启动期限失败收尾。"""
    source = tmp_path / "source"
    source.mkdir()
    (source / "main.py").write_text(
        "import os, socket, time\n"
        "listener = socket.socket()\n"
        "listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)\n"
        "listener.bind(('127.0.0.1', int(os.environ['PORT'])))\n"
        "listener.listen()\n"
        "while True:\n"
        "    client, _ = listener.accept()\n"
        "    time.sleep(30)\n",
        encoding="utf-8",
    )
    runner = tmp_path / "_prism_verify.sh"
    runner.write_text(_DEPLOY_VERIFY_RUNNER, encoding="utf-8")
    env = {
        **os.environ,
        "PATH": f"{Path(sys.executable).parent}:{os.environ.get('PATH', '')}",
        "PRISM_LANGUAGE": "python",
        "PRISM_PREVIEW_PORT": str(_free_port()),
        "PRISM_STARTUP_TIMEOUT_SECONDS": "1",
        "PRISM_WORKSPACE": str(source),
    }
    started_at = time.monotonic()
    result = subprocess.run(
        ["bash", str(runner), "blackbox"], cwd=source, env=env,
        capture_output=True, text=True, timeout=10, check=False
    )
    output = result.stdout + result.stderr
    assert result.returncode == 1, output
    receipt = _extract_blackbox_result(output)
    assert receipt is not None, output
    assert receipt["failure_kind"] == "application_startup"
    assert receipt["failure_reason"] == "application_readiness_timeout"
    assert time.monotonic() - started_at < 9, output


def test_real_runner_separates_route_success_from_agent_assertion_failure(tmp_path: Path) -> None:
    """精确白名单逐项执行状态断言；不执行源码包中的黑盒脚本。"""
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
    scope = _add_http_scope(env, ("/", (404,)), ("/healthz", (200,)))
    result = subprocess.run(
        ["bash", str(runner)],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert result.returncode != 0
    output = result.stdout + result.stderr
    receipt = _extract_blackbox_result(output, expected_http_scope=scope)
    assert receipt is not None
    assert receipt["route_passed"] is False
    assert receipt["status"] == "failed"
    assert receipt["agent_assertions_passed"] is None
    assert [item["status_code"] for item in receipt["requests"]] == [404, 204]
    assert "expected failure" not in output


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
    scope = _add_http_scope(env, ("/", (200,)))

    result = subprocess.run(
        ["bash", str(runner)],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )

    output = result.stdout + result.stderr
    assert result.returncode != 0, output
    assert "blackbox scoped application exited before readiness" in output
    assert "controlled startup output" not in output
    receipt = _extract_blackbox_result(output, expected_http_scope=scope)
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
        "PRISM_BLACKBOX_STARTUP_TIMEOUT_SECONDS": "1",
    }
    scope = _add_http_scope(env, ("/", (200,)))

    result = subprocess.run(
        ["bash", str(runner)],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )

    output = result.stdout + result.stderr
    assert result.returncode != 0, output
    assert "scoped application readiness timed out" in output
    receipt = _extract_blackbox_result(output, expected_http_scope=scope)
    assert receipt is not None
    assert receipt["status"] == "failed"
    assert receipt["route_passed"] is False
    assert receipt["status_code"] == 0
    assert receipt["failure_kind"] == "application_startup"
    assert receipt["failure_reason"] == "application_readiness_timeout"


@pytest.mark.parametrize("runner_kind", ["trusted-runner", "embedded-deploy-runner"])
def test_node_readiness_timeout_stops_application_child_processes(
    tmp_path: Path, runner_kind: str
) -> None:
    """黑盒就绪超时必须回收 npm 下启动的 Node 子进程，而非只杀 npm 父进程。"""
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is required for the isolated process-tree regression")

    source = tmp_path / "source"
    source.mkdir()
    ticks = tmp_path / "ticks.log"
    child_pid = tmp_path / "child.pid"
    (source / "package.json").write_text(
        '{"name":"timeout-fixture","version":"1.0.0","scripts":{"start":"node server.js"}}',
        encoding="utf-8",
    )
    (source / "package-lock.json").write_text(
        '{"name":"timeout-fixture","version":"1.0.0","lockfileVersion":3,"packages":{"":{"name":"timeout-fixture","version":"1.0.0"}}}',
        encoding="utf-8",
    )
    (source / "server.js").write_text(
        "const fs = require('node:fs');\n"
        "fs.writeFileSync(process.env.PRISM_TEST_CHILD_PID, String(process.pid));\n"
        "setInterval(() => fs.appendFileSync(process.env.PRISM_TEST_TICKS, 'x'), 20);\n",
        encoding="utf-8",
    )
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_npm = fake_bin / "npm"
    fake_npm.write_text(
        "#!/bin/sh\n"
        "case \"$1\" in\n"
        "  ci) exit 0 ;;\n"
        "  start)\n"
        "    \"$PRISM_TEST_REAL_NODE\" server.js &\n"
        "    child=$!\n"
        "    wait \"$child\" ;;\n"
        "  *) exit 64 ;;\n"
        "esac\n",
        encoding="utf-8",
    )
    fake_npm.chmod(0o755)
    fake_sleep = fake_bin / "sleep"
    if runner_kind == "trusted-runner":
        # Keep real readiness intervals here so the asynchronously started npm
        # child has time to create its PID marker before the short timeout fires.
        fake_sleep.write_text("#!/bin/sh\nexec /bin/sleep \"$@\"\n", encoding="utf-8")
    else:
        # The embedded compatibility runner has a separate, long timeout path.
        fake_sleep.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    fake_sleep.chmod(0o755)

    if runner_kind == "trusted-runner":
        runner = Path(__file__).resolve().parents[4] / "deploy" / "sandbox" / "runner.sh"
        cwd = tmp_path
        env = {
            **os.environ,
            "PATH": f"{fake_bin}:{Path(node).parent}:{Path(sys.executable).parent}:{os.environ.get('PATH', '')}",
            "PRISM_ACTION": "test",
            "PRISM_LANGUAGE": "node",
            "PRISM_TEST_MODE": "blackbox",
            "PRISM_PREVIEW_PORT": str(_free_port()),
            "PRISM_SOURCE_DIR": str(source),
            "PRISM_WORKSPACE_DIR": str(tmp_path / "workspace"),
            "PRISM_BLACKBOX_STARTUP_TIMEOUT_SECONDS": "3",
        }
        scope = _add_http_scope(env, ("/", (200,)))
        command = ["bash", str(runner)]
    else:
        from app.services.sandbox_service import _DEPLOY_VERIFY_RUNNER

        runner = tmp_path / "_prism_verify.sh"
        runner.write_text(_DEPLOY_VERIFY_RUNNER, encoding="utf-8")
        cwd = source
        env = {
            **os.environ,
            "PATH": f"{fake_bin}:{Path(node).parent}:{Path(sys.executable).parent}:{os.environ.get('PATH', '')}",
            "PRISM_LANGUAGE": "node",
            "PRISM_PREVIEW_PORT": str(_free_port()),
            "PRISM_WORKSPACE": str(source),
        }
        command = ["bash", str(runner), "blackbox"]

    env.update(
        {
            "PRISM_TEST_REAL_NODE": node,
            "PRISM_TEST_CHILD_PID": str(child_pid),
            "PRISM_TEST_TICKS": str(ticks),
        }
    )
    result = subprocess.run(
        command, cwd=cwd, env=env, capture_output=True, text=True, timeout=20, check=False
    )
    pid = int(child_pid.read_text(encoding="utf-8")) if child_pid.exists() else None
    try:
        output = result.stdout + result.stderr
        assert result.returncode != 0, output
        if runner_kind == "trusted-runner":
            receipt = _extract_blackbox_result(output, expected_http_scope=scope)
            assert receipt is not None and receipt["status"] == "failed"
            assert receipt["status_code"] == 0
        assert any(
            marker in output
            for marker in (
                "readiness_timeout",
                "did not become ready",
                "未在回环端口就绪",
                "PRISM_VERIFY blackbox fail",
                "scoped application readiness timed out",
                "application_readiness_timeout",
                "scoped application exited before readiness",
                "application_exited_before_ready",
                "status=0 bytes=0",
            )
        ), output
        assert pid is not None, output
        time.sleep(0.2)
        first_size = ticks.stat().st_size if ticks.exists() else 0
        time.sleep(0.2)
        second_size = ticks.stat().st_size if ticks.exists() else 0
        assert second_size == first_size, f"Node child {pid} kept running after timeout\n{output}"
    finally:
        if pid is not None:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


@pytest.mark.parametrize(("interrupt_signal", "expected_exit"), [(signal.SIGINT, 130), (signal.SIGTERM, 143)])
def test_runner_signal_stops_application_child_processes(
    tmp_path: Path, interrupt_signal: int, expected_exit: int
) -> None:
    """runner 收到取消信号时，必须回收应用进程组及 npm 启动的 Node 子进程。"""
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is required for the isolated signal-cleanup regression")

    source = tmp_path / "source"
    source.mkdir()
    ticks = tmp_path / "ticks.log"
    child_pid = tmp_path / "child.pid"
    (source / "package.json").write_text(
        '{"name":"signal-fixture","version":"1.0.0","scripts":{"start":"node server.js"}}',
        encoding="utf-8",
    )
    (source / "package-lock.json").write_text(
        '{"name":"signal-fixture","version":"1.0.0","lockfileVersion":3,"packages":{"":{"name":"signal-fixture","version":"1.0.0"}}}',
        encoding="utf-8",
    )
    (source / "server.js").write_text(
        "const fs = require('node:fs');\n"
        "fs.writeFileSync(process.env.PRISM_TEST_CHILD_PID, String(process.pid));\n"
        "setInterval(() => fs.appendFileSync(process.env.PRISM_TEST_TICKS, 'x'), 20);\n",
        encoding="utf-8",
    )
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_npm = fake_bin / "npm"
    fake_npm.write_text(
        "#!/bin/sh\n"
        "case \"$1\" in\n"
        "  ci) exit 0 ;;\n"
        "  start)\n"
        "    \"$PRISM_TEST_REAL_NODE\" server.js &\n"
        "    child=$!\n"
        "    wait \"$child\" ;;\n"
        "  *) exit 64 ;;\n"
        "esac\n",
        encoding="utf-8",
    )
    fake_npm.chmod(0o755)

    runner = Path(__file__).resolve().parents[4] / "deploy" / "sandbox" / "runner.sh"
    env = {
        **os.environ,
        "PATH": f"{fake_bin}:{Path(node).parent}:{Path(sys.executable).parent}:{os.environ.get('PATH', '')}",
        "PRISM_ACTION": "test",
        "PRISM_LANGUAGE": "node",
        "PRISM_TEST_MODE": "blackbox",
        "PRISM_PREVIEW_PORT": str(_free_port()),
        "PRISM_SOURCE_DIR": str(source),
        "PRISM_WORKSPACE_DIR": str(tmp_path / "workspace"),
        "PRISM_TEST_REAL_NODE": node,
        "PRISM_TEST_CHILD_PID": str(child_pid),
        "PRISM_TEST_TICKS": str(ticks),
    }
    _add_http_scope(env, ("/", (200,)))
    process = subprocess.Popen(
        ["bash", str(runner)],
        cwd=tmp_path,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    pid: int | None = None
    try:
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline and process.poll() is None and not child_pid.exists():
            time.sleep(0.05)
        assert child_pid.exists(), "application child did not start before cancellation"
        pid = int(child_pid.read_text(encoding="utf-8"))
        process.send_signal(interrupt_signal)
        stdout, stderr = process.communicate(timeout=10)
        output = stdout + stderr
        assert process.returncode == expected_exit, output
        first_size = ticks.stat().st_size if ticks.exists() else 0
        time.sleep(0.2)
        second_size = ticks.stat().st_size if ticks.exists() else 0
        assert second_size == first_size, f"Node child {pid} kept running after signal\n{output}"
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate(timeout=5)
        if pid is not None:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


def test_real_runner_route_failure_force_stops_sigterm_ignoring_app(tmp_path: Path) -> None:
    """精确状态断言失败后，忽略 TERM 的本地应用也必须被强制回收。"""
    source = tmp_path / "source"
    source.mkdir()
    ticks = tmp_path / "ticks.log"
    (source / "wsgi.py").write_text(
        "import os, signal, threading, time\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "ticks = os.environ['PRISM_TEST_TICKS']\n"
        "def write_ticks():\n"
        "    while True:\n"
        "        with open(ticks, 'a', encoding='utf-8') as handle: handle.write('x')\n"
        "        time.sleep(0.05)\n"
        "threading.Thread(target=write_ticks, daemon=True).start()\n"
        "def application(environ, start_response):\n"
        "    start_response('404 Not Found', [('Content-Type', 'text/plain')])\n"
        "    return [b'not found']\n",
        encoding="utf-8",
    )
    agent_tests = source / "_agent_tests"
    agent_tests.mkdir()
    (agent_tests / "blackbox.py").write_text(
        "print('controlled agent blackbox assertion passed')\n",
        encoding="utf-8",
    )
    runner = Path(__file__).resolve().parents[4] / "deploy" / "sandbox" / "runner.sh"
    env = {
        **os.environ,
        "PATH": f"{Path(sys.executable).parent}:{os.environ.get('PATH', '')}",
        "PRISM_ACTION": "test",
        "PRISM_LANGUAGE": "python",
        "PRISM_TEST_MODE": "blackbox",
        "PRISM_PREVIEW_PORT": str(_free_port()),
        "PRISM_SOURCE_DIR": str(source),
        "PRISM_WORKSPACE_DIR": str(tmp_path / "workspace"),
        "PRISM_TEST_TICKS": str(ticks),
    }
    scope = _add_http_scope(env, ("/", (200,)))
    process = subprocess.Popen(
        ["bash", str(runner)],
        cwd=tmp_path,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    started_at = time.monotonic()
    try:
        stdout, stderr = process.communicate(timeout=10)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        stdout, stderr = process.communicate()
        pytest.fail(f"blackbox runner hung after route failure\n{stdout}\n{stderr}")

    output = stdout + stderr
    assert process.returncode != 0, output
    receipt = _extract_blackbox_result(output, expected_http_scope=scope)
    assert receipt is not None, output
    assert receipt["status"] == "failed"
    assert receipt["route_passed"] is False
    assert receipt["agent_assertions_passed"] is None
    assert receipt["requests"][0]["status_code"] == 404
    assert '"passed":false' in output
    assert "controlled agent blackbox assertion passed" not in output
    assert time.monotonic() - started_at < 8, output
    assert ticks.exists(), output
    first_size = ticks.stat().st_size
    time.sleep(0.2)
    assert ticks.stat().st_size == first_size, f"SIGTERM-ignoring app was not stopped\n{output}"


def test_real_runner_does_not_treat_implicit_root_health_as_business_route(tmp_path: Path) -> None:
    """只访问显式白名单中的根路由，不因隐式健康响应扩展探测范围。"""
    source = tmp_path / "source"
    source.mkdir()
    (source / "wsgi.py").write_text(
        "# PATH_INFO == '/'\n"
        "# @app.get('/')\n"
        "def application(environ, start_response):\n"
        "    path = environ.get('PATH_INFO')  # PATH_INFO == '/'\n"
        "    start_response('200 OK', [('Content-Type', 'text/plain')])\n"
        "    return [path.encode('utf-8')]\n",
        encoding="utf-8",
    )
    (source / "index.html").write_text("unrelated static file", encoding="utf-8")
    runner = Path(__file__).resolve().parents[4] / "deploy" / "sandbox" / "runner.sh"
    env = {
        **os.environ,
        "PATH": f"{Path(sys.executable).parent}:{os.environ.get('PATH', '')}",
        "PRISM_ACTION": "test",
        "PRISM_LANGUAGE": "python",
        "PRISM_TEST_MODE": "blackbox",
        "PRISM_PREVIEW_PORT": str(_free_port()),
        "PRISM_SOURCE_DIR": str(source),
        "PRISM_WORKSPACE_DIR": str(tmp_path / "workspace"),
    }
    scope = _add_http_scope(env, ("/", (200,)))
    process = subprocess.Popen(
        ["bash", str(runner)],
        cwd=tmp_path,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    stdout, stderr = process.communicate(timeout=12)
    output = stdout + stderr

    assert process.returncode == 0, output
    receipt = _extract_blackbox_result(output, expected_http_scope=scope)
    assert receipt is not None, output
    assert receipt["status"] == "passed"
    assert receipt["requests"][0]["target"] == "/"
    assert '"request_count":1' in output


def test_real_runner_does_not_treat_route_text_inside_string_as_business_route(tmp_path: Path) -> None:
    """源码字符串中的路由文本不会改变固定白名单请求计划。"""
    source = tmp_path / "source"
    source.mkdir()
    (source / "wsgi.py").write_text(
        'ROUTE_DOC = "route(\'/healthz\')"\n'
        r'''ESCAPED_DOC = "example \" route('/escaped')"''' + "\n"
        r'''TRIPLE_DOC = """example \""" route('/triple')"""''' + "\n"
        'EXAMPLE = """\n@app.get(\'/docs\')\n"""\n'
        "def application(environ, start_response):\n"
        "    start_response('200 OK', [('Content-Type', 'text/plain')])\n"
        "    return [b'fallback']\n",
        encoding="utf-8",
    )
    compile((source / "wsgi.py").read_text(encoding="utf-8"), "wsgi.py", "exec")
    runner = Path(__file__).resolve().parents[4] / "deploy" / "sandbox" / "runner.sh"
    env = {
        **os.environ,
        "PATH": f"{Path(sys.executable).parent}:{os.environ.get('PATH', '')}",
        "PRISM_ACTION": "test",
        "PRISM_LANGUAGE": "python",
        "PRISM_TEST_MODE": "blackbox",
        "PRISM_PREVIEW_PORT": str(_free_port()),
        "PRISM_SOURCE_DIR": str(source),
        "PRISM_WORKSPACE_DIR": str(tmp_path / "workspace"),
    }
    scope = _add_http_scope(env, ("/", (200,)))
    process = subprocess.Popen(
        ["bash", str(runner)],
        cwd=tmp_path,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    stdout, stderr = process.communicate(timeout=12)
    output = stdout + stderr

    assert process.returncode == 0, output
    receipt = _extract_blackbox_result(output, expected_http_scope=scope)
    assert receipt is not None, output
    assert receipt["status"] == "passed"
    assert [item["target"] for item in receipt["requests"]] == ["/"]


def test_embedded_blackbox_returns_structured_receipt_when_app_cannot_start(tmp_path: Path) -> None:
    """部署核验 runner 无可运行入口时也必须给出可解析的启动失败回执。"""
    source = tmp_path / "source"
    source.mkdir()
    (source / "README.md").write_text("no runnable app", encoding="utf-8")
    runner = tmp_path / "_prism_verify.sh"
    runner.write_text(_DEPLOY_VERIFY_RUNNER, encoding="utf-8")
    env = {
        **os.environ,
        "PATH": f"{Path(sys.executable).parent}:{os.environ.get('PATH', '')}",
        "PRISM_LANGUAGE": "python",
        "PRISM_PREVIEW_PORT": str(_free_port()),
        "PRISM_WORKSPACE": str(source),
    }

    result = subprocess.run(
        ["bash", str(runner), "blackbox"], cwd=source, env=env,
        capture_output=True, text=True, timeout=10, check=False
    )

    output = result.stdout + result.stderr
    assert result.returncode == 1, output
    receipt = _extract_blackbox_result(output)
    assert receipt is not None, output
    assert receipt["status"] == "failed"
    assert receipt["failure_kind"] == "application_startup"
    assert receipt["failure_reason"] == "application_start_failed"


def test_embedded_blackbox_does_not_treat_route_text_inside_string_as_route(tmp_path: Path) -> None:
    """嵌入式部署 runner 也不能把字符串中的路由示例当成已注册路由。"""
    source = tmp_path / "source"
    source.mkdir()
    (source / "wsgi.py").write_text(
        'ROUTE_DOC = "route(\'/healthz\')"\n'
        r'''ESCAPED_DOC = "example \" route('/escaped')"''' + "\n"
        r'''TRIPLE_DOC = """example \""" route('/triple')"""''' + "\n"
        'EXAMPLE = """\n@app.get(\'/docs\')\n"""\n'
        "def application(environ, start_response):\n"
        "    start_response('200 OK', [('Content-Type', 'text/plain')])\n"
        "    return [b'fallback']\n",
        encoding="utf-8",
    )
    compile((source / "wsgi.py").read_text(encoding="utf-8"), "wsgi.py", "exec")
    runner = tmp_path / "_prism_verify.sh"
    runner.write_text(_DEPLOY_VERIFY_RUNNER, encoding="utf-8")
    env = {
        **os.environ,
        "PATH": f"{Path(sys.executable).parent}:{os.environ.get('PATH', '')}",
        "PRISM_LANGUAGE": "python",
        "PRISM_PREVIEW_PORT": str(_free_port()),
        "PRISM_WORKSPACE": str(source),
    }

    result = subprocess.run(
        ["bash", str(runner), "blackbox"], cwd=source, env=env,
        capture_output=True, text=True, timeout=12, check=False
    )

    output = result.stdout + result.stderr
    assert result.returncode == 1, output
    receipt = _extract_blackbox_result(output)
    assert receipt is not None, output
    assert receipt["status"] == "failed"
    assert receipt["route_passed"] is False
    assert "no discovered application route returned HTTP 2xx" in output, output


def test_real_runner_bounds_readiness_when_app_accepts_but_never_sends_http_status(tmp_path: Path) -> None:
    """TCP 就绪但白名单 HTTP 请求无状态行时，必须形成失败回执并清理进程。"""
    source = tmp_path / "source"
    source.mkdir()
    (source / "wsgi.py").write_text(
        "import time\n"
        "def application(environ, start_response):\n"
        "    time.sleep(60)\n"
        "    start_response('200 OK', [('Content-Type', 'text/plain')])\n"
        "    return [b'too late']\n",
        encoding="utf-8",
    )
    runner = Path(__file__).resolve().parents[4] / "deploy" / "sandbox" / "runner.sh"
    env = {
        **os.environ,
        "PATH": f"{Path(sys.executable).parent}:{os.environ.get('PATH', '')}",
        "PRISM_ACTION": "test",
        "PRISM_LANGUAGE": "python",
        "PRISM_TEST_MODE": "blackbox",
        "PRISM_PREVIEW_PORT": str(_free_port()),
        "PRISM_SOURCE_DIR": str(source),
        "PRISM_WORKSPACE_DIR": str(tmp_path / "workspace"),
        "PRISM_BLACKBOX_STARTUP_TIMEOUT_SECONDS": "5",
    }
    scope = _add_http_scope(env, ("/", (200,)))
    process = subprocess.Popen(
        ["bash", str(runner)],
        cwd=tmp_path,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    started_at = time.monotonic()
    stdout, stderr = process.communicate(timeout=12)
    output = stdout + stderr

    assert process.returncode != 0, output
    receipt = _extract_blackbox_result(output, expected_http_scope=scope)
    assert receipt is not None, output
    assert receipt["status"] == "failed"
    assert receipt["requests"][0]["status_code"] == 0
    assert receipt["requests"][0]["assertion_status"] == "no_response"
    assert '"request_count":1' in output
    assert time.monotonic() - started_at < 9, output


def test_real_runner_times_out_hanging_agent_blackbox_and_cleans_app(tmp_path: Path) -> None:
    """白名单模式不运行挂起的 Agent 脚本，并在确定性 HTTP 检查后回收应用进程。"""
    source = tmp_path / "source"
    source.mkdir()
    ticks = tmp_path / "ticks.log"
    (source / "wsgi.py").write_text(
        "import os, signal, threading, time\n"
        "def route(path): return lambda handler: handler\n"
        "@route('/business')\n"
        "def declared_business_route(*args): pass\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "ticks = os.environ['PRISM_TEST_TICKS']\n"
        "def write_ticks():\n"
        "    while True:\n"
        "        with open(ticks, 'a', encoding='utf-8') as handle: handle.write('x')\n"
        "        time.sleep(0.05)\n"
        "threading.Thread(target=write_ticks, daemon=True).start()\n"
        "def application(environ, start_response):\n"
        "    start_response('200 OK', [('Content-Type', 'text/plain')])\n"
        "    return [b'ok']\n",
        encoding="utf-8",
    )
    agent_tests = source / "_agent_tests"
    agent_tests.mkdir()
    (agent_tests / "blackbox.py").write_text(
        "import signal, time\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "print('agent blackbox started', flush=True)\n"
        "while True: time.sleep(1)\n",
        encoding="utf-8",
    )
    runner = Path(__file__).resolve().parents[4] / "deploy" / "sandbox" / "runner.sh"
    env = {
        **os.environ,
        "PATH": f"{Path(sys.executable).parent}:{os.environ.get('PATH', '')}",
        "PRISM_ACTION": "test",
        "PRISM_LANGUAGE": "python",
        "PRISM_TEST_MODE": "blackbox",
        "PRISM_PREVIEW_PORT": str(_free_port()),
        "PRISM_SOURCE_DIR": str(source),
        "PRISM_WORKSPACE_DIR": str(tmp_path / "workspace"),
        "PRISM_TEST_TICKS": str(ticks),
        "PRISM_AGENT_TEST_TIMEOUT_SECONDS": "1",
    }
    scope = _add_http_scope(env, ("/", (200,)))
    process = subprocess.Popen(
        ["bash", str(runner)],
        cwd=tmp_path,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    started_at = time.monotonic()
    stdout, stderr = process.communicate(timeout=12)
    output = stdout + stderr

    assert process.returncode == 0, output
    receipt = _extract_blackbox_result(output, expected_http_scope=scope)
    assert receipt is not None, output
    assert receipt["status"] == "passed"
    assert receipt["requests"][0]["target"] == "/"
    assert receipt["agent_assertions_passed"] is None
    assert "agent blackbox started" not in output
    assert "PRISM_AGENT_TESTS_BEGIN" not in output
    assert time.monotonic() - started_at < 9, output
    assert ticks.exists(), output
    first_size = ticks.stat().st_size
    time.sleep(0.2)
    assert ticks.stat().st_size == first_size, f"application kept running after timeout\n{output}"


def test_real_runner_global_deadline_stops_hanging_whitebox_stage(tmp_path: Path) -> None:
    """白盒测试卡住时，runner 应早于 Worker profile 硬时限退出并输出失败回执。"""
    source = tmp_path / "source"
    source.mkdir()
    pid_file = tmp_path / "test-process.pid"
    (source / "test_hang.py").write_text(
        "import os, time, unittest\n"
        "class HangingTest(unittest.TestCase):\n"
        "    def test_never_finishes(self):\n"
        "        with open(os.environ['PRISM_TEST_HANG_PID_FILE'], 'w') as handle: handle.write(str(os.getpid()))\n"
        "        while True: time.sleep(1)\n",
        encoding="utf-8",
    )
    runner = Path(__file__).resolve().parents[4] / "deploy" / "sandbox" / "runner.sh"
    env = {
        **os.environ,
        "PATH": f"{Path(sys.executable).parent}:{os.environ.get('PATH', '')}",
        "PRISM_ACTION": "test",
        "PRISM_LANGUAGE": "python",
        "PRISM_TEST_MODE": "whitebox",
        "PRISM_TEST_TIMEOUT_SECONDS": "10",
        "PRISM_SOURCE_DIR": str(source),
        "PRISM_WORKSPACE_DIR": str(tmp_path / "workspace"),
        "PRISM_TEST_HANG_PID_FILE": str(pid_file),
    }
    process = subprocess.Popen(
        ["bash", str(runner)], cwd=tmp_path, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, start_new_session=True
    )
    started_at = time.monotonic()
    stdout, stderr = process.communicate(timeout=12)
    output = stdout + stderr
    assert process.returncode == 124, output
    assert 'PRISM_WHITEBOX_DONE {"executed":true,"passed":false,"reason":"runner_budget_exhausted"}' in output
    assert time.monotonic() - started_at < 11, output
    if pid_file.exists():
        child_pid = int(pid_file.read_text(encoding="utf-8"))
        time.sleep(0.2)
        try:
            os.kill(child_pid, 0)
        except ProcessLookupError:
            pass
        else:
            os.kill(child_pid, signal.SIGKILL)
            pytest.fail(f"whitebox test process {child_pid} remained after runner deadline\n{output}")


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
        ["bash", str(runner), "blackbox"],
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
