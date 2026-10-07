"""部署后黑盒/白盒验证 runner 的失败状态回归。"""

from __future__ import annotations

import os
import shutil
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

from app.services.sandbox_service import (
    _DEPLOY_VERIFY_RUNNER,
    _extract_agent_tests_result,
    _extract_blackbox_result,
)


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
        ["bash", str(runner)],
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
    assert "application did not become ready" in output
    receipt = _extract_blackbox_result(output)
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
        }
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
        assert any(
            marker in output
            for marker in (
                "readiness_timeout",
                "did not become ready",
                "未在回环端口就绪",
                "PRISM_VERIFY blackbox fail",
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
    """无成功路由时，忽略 TERM 的本地应用也必须在沙箱时限前被强制回收。"""
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
    receipt = _extract_blackbox_result(output)
    assert receipt is not None, output
    assert receipt["status"] == "failed"
    assert receipt["route_passed"] is False
    assert receipt["agent_assertions_passed"] is True
    assert "no discovered application route returned HTTP 2xx" in output
    assert '"passed":false' in output
    agent_result = _extract_agent_tests_result(output)
    assert agent_result is not None
    assert agent_result["files"].get("blackbox.py") == "pass"
    assert (
        agent_result["file_results"]["blackbox.py"].get("output")
        == "controlled agent blackbox assertion passed\n"
    )
    assert time.monotonic() - started_at < 8, output
    assert ticks.exists(), output
    first_size = ticks.stat().st_size
    time.sleep(0.2)
    assert ticks.stat().st_size == first_size, f"SIGTERM-ignoring app was not stopped\n{output}"


def test_real_runner_does_not_treat_implicit_root_health_as_business_route(tmp_path: Path) -> None:
    """注释中的路由和普通 get('PATH_INFO') 取值不能伪装成已发现的业务路由。"""
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

    assert process.returncode != 0, output
    receipt = _extract_blackbox_result(output)
    assert receipt is not None, output
    assert receipt["status"] == "failed"
    assert receipt["route_passed"] is False
    assert "no discovered application route returned HTTP 2xx" in output, output
    assert '"passed":false' in output


def test_real_runner_does_not_treat_route_text_inside_string_as_business_route(tmp_path: Path) -> None:
    """普通字符串中提到路由，不等于应用注册了该路由。"""
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

    assert process.returncode != 0, output
    receipt = _extract_blackbox_result(output)
    assert receipt is not None, output
    assert receipt["status"] == "failed"
    assert receipt["route_passed"] is False
    assert "no discovered application route returned HTTP 2xx" in output, output
    assert "no discovered application route returned HTTP 2xx" in output, output


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
    """应用接受 TCP 后不返回 HTTP 首行时，就绪探测必须按阶段时限退出。"""
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
        "PRISM_BLACKBOX_STARTUP_TIMEOUT_SECONDS": "1",
    }
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
    receipt = _extract_blackbox_result(output)
    assert receipt is not None, output
    assert receipt["status"] == "failed"
    assert receipt["failure_kind"] == "application_startup"
    assert "application_readiness_timeout" in output
    assert time.monotonic() - started_at < 9, output


def test_real_runner_times_out_hanging_agent_blackbox_and_cleans_app(tmp_path: Path) -> None:
    """挂起的 Agent 黑盒断言应被限时终止，随后清理应用进程组并返回失败回执。"""
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
        "    is_business = environ['PATH_INFO'] == '/business'\n"
        "    start_response('200 OK' if is_business else '404 Not Found', [('Content-Type', 'text/plain')])\n"
        "    return [b'business' if is_business else b'health']\n",
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
    receipt = _extract_blackbox_result(output)
    assert receipt is not None, output
    assert receipt["status"] == "failed"
    assert receipt["route_passed"] is True, output
    assert receipt["route"] == "/business", output
    assert receipt["route_origin"] == "source_route", output
    assert receipt["agent_assertions_passed"] is False
    agent_result = _extract_agent_tests_result(output)
    assert agent_result is not None
    assert agent_result["file_results"]["blackbox.py"]["failure_kind"] == "timeout"
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
