"""在临时 Unix-socket-only Redis 实例上运行登录限流 Lua 集成测试。"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

import redis

BACKEND_ROOT = Path(__file__).resolve().parents[1]
TEST_PATH = "tests/unit/core/test_login_rate_limit_real_redis.py"
MINIMAL_ENV_KEYS = ("LANG", "LC_ALL")


def _redis_server_binary() -> str:
    explicit = os.environ.get("PRISM_REDIS_SERVER_BIN", "").strip()
    candidate = explicit or shutil.which("redis-server")
    if not candidate or not Path(candidate).is_file():
        raise SystemExit(
            "需要官方 Redis 服务器可执行文件；可安装 Redis 7 并设置 PRISM_REDIS_SERVER_BIN。"
        )
    return str(Path(candidate).resolve())


def _minimal_environment(root: Path) -> dict[str, str]:
    environment = {key: os.environ[key] for key in MINIMAL_ENV_KEYS if os.environ.get(key)}
    environment.update({"HOME": str(root), "TMPDIR": str(root)})
    return environment


def _redis_version(redis_server: str, root: Path) -> tuple[str, str]:
    result = subprocess.run(
        [redis_server, "--version"],
        capture_output=True,
        check=True,
        text=True,
        timeout=10,
        env=_minimal_environment(root),
    )
    version_output = result.stdout.strip()
    match = re.search(r"\bv=(\d+)\.(\d+)\.(\d+)\b", version_output)
    if not match or int(match.group(1)) != 7:
        raise SystemExit(f"仅允许 Redis 7.x 运行此集成测试；检测到：{version_output or result.stderr.strip()}")
    binary_sha256 = hashlib.sha256(Path(redis_server).read_bytes()).hexdigest()
    return ".".join(match.groups()), binary_sha256


def _wait_until_ready(client: redis.Redis, process: subprocess.Popen, log_path: Path) -> None:
    deadline = time.monotonic() + 15
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        if process.poll() is not None:
            break
        try:
            if client.ping():
                return
        except redis.RedisError as exc:
            last_error = exc
            time.sleep(0.1)
    log_tail = log_path.read_text(errors="replace")[-3000:] if log_path.exists() else ""
    raise RuntimeError(f"临时 Redis 未就绪；last_error={last_error!r}\n{log_tail}")


def _stop_redis(process: subprocess.Popen | None) -> bool:
    if process is None or process.poll() is not None:
        return True
    try:
        process.terminate()
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        try:
            process.kill()
        except ProcessLookupError:
            pass
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            return False
    return process.poll() is not None


def main() -> int:
    sys.stdout.reconfigure(line_buffering=True)
    redis_server = _redis_server_binary()
    if (BACKEND_ROOT / ".env").exists():
        raise SystemExit("拒绝在存在 backend/.env 的工作树运行，避免读取本地服务凭据。")
    run_id = uuid.uuid4().hex
    root = Path(tempfile.mkdtemp(prefix="prism-login-redis-"))
    socket_path = root / "redis.sock"
    log_path = root / "redis.log"
    process: subprocess.Popen | None = None
    client: redis.Redis | None = None
    pytest_return_code: int | None = None
    process_stopped = True
    try:
        version, binary_sha256 = _redis_version(redis_server, root)
        test_source = BACKEND_ROOT / TEST_PATH
        limiter_source = BACKEND_ROOT / "app/core/rate_limit.py"
        runner_source = Path(__file__).resolve()
        print(f"RUN_ID={run_id}")
        print(f"REDIS_VERSION={version}")
        print(f"REDIS_SERVER_SHA256={binary_sha256}")
        print(f"RUNNER_SOURCE_SHA256={hashlib.sha256(runner_source.read_bytes()).hexdigest()}")
        print(f"TEST_SOURCE_SHA256={hashlib.sha256(test_source.read_bytes()).hexdigest()}")
        print(f"LIMITER_SOURCE_SHA256={hashlib.sha256(limiter_source.read_bytes()).hexdigest()}")
        print(f"ISOLATION_ROOT_MODE={oct(root.stat().st_mode & 0o777)}")
        if root.stat().st_mode & 0o077:
            raise RuntimeError("临时 Redis 根目录权限过宽")
        with log_path.open("w") as log_stream:
            process = subprocess.Popen(
                [
                    redis_server,
                    "--port",
                    "0",
                    "--unixsocket",
                    str(socket_path),
                    "--unixsocketperm",
                    "700",
                    "--protected-mode",
                    "yes",
                    "--save",
                    "",
                    "--appendonly",
                    "no",
                    "--dir",
                    str(root),
                    "--dbfilename",
                    "temporary.rdb",
                    "--daemonize",
                    "no",
                    "--logfile",
                    "",
                ],
                cwd=root,
                stdin=subprocess.DEVNULL,
                stdout=log_stream,
                stderr=subprocess.STDOUT,
                env=_minimal_environment(root),
            )
            client = redis.Redis(
                unix_socket_path=str(socket_path),
                decode_responses=True,
                socket_connect_timeout=1,
                socket_timeout=2,
            )
            try:
                _wait_until_ready(client, process, log_path)
                settings = client.config_get("port")
                tls_settings = client.config_get("tls-port")
                unix_socket = client.config_get("unixsocket")
                socket_is_expected = (
                    Path(unix_socket.get("unixsocket", "")).resolve()
                    == socket_path.resolve()
                )
                if settings.get("port") != "0" or tls_settings.get("tls-port") != "0" or not socket_is_expected:
                    raise RuntimeError("临时 Redis 网络隔离配置校验失败")
                print(f"REDIS_TCP_PORT={settings.get('port')}")
                print(f"REDIS_TLS_PORT={tls_settings.get('tls-port')}")
                print(f"REDIS_UNIX_SOCKET={unix_socket.get('unixsocket')}")
                socket_mode = socket_path.stat().st_mode & 0o777
                print(f"REDIS_SOCKET_MODE={oct(socket_mode)}")
                if socket_mode & 0o077:
                    raise RuntimeError("Redis Unix socket 权限过宽")

                environment = _minimal_environment(root)
                environment["APP_ENV"] = "dev"
                environment["REDIS_URL"] = ""
                environment["PRISM_LOGIN_TEST_REDIS_CONTAINER"] = ""
                environment["PRISM_LOGIN_TEST_REDIS_SOCKET"] = str(socket_path)
                result = subprocess.run(
                    [
                        sys.executable,
                        "-m",
                        "pytest",
                        "-q",
                        "-o",
                        "addopts=",
                        TEST_PATH,
                    ],
                    cwd=BACKEND_ROOT,
                    env=environment,
                    check=False,
                    timeout=180,
                )
                pytest_return_code = result.returncode
                print(f"PYTEST_EXIT_CODE={pytest_return_code}")
            finally:
                close_error: Exception | None = None
                try:
                    client.close()
                except Exception as exc:  # cleanup must still stop the server
                    close_error = exc
                finally:
                    client = None
                    process_stopped = _stop_redis(process)
                    if not process_stopped:
                        raise RuntimeError(
                            f"临时 Redis 子进程仍存活，保留隔离目录以避免删除运行时文件："
                            f"pid={process.pid} root={root}"
                        )
                    print(f"REDIS_PROCESS_EXIT_CODE={process.returncode}")
                if close_error is not None:
                    raise RuntimeError("临时 Redis 客户端关闭失败") from close_error
    finally:
        close_error: Exception | None = None
        if client is not None:
            try:
                client.close()
            except Exception as exc:  # cleanup must still stop and isolate Redis
                close_error = exc
            finally:
                client = None
        if process is not None and process.poll() is None:
            process_stopped = _stop_redis(process)
        if not process_stopped or (process is not None and process.poll() is None):
            print(f"TEMP_ROOT_RETAINED_FOR_LIVE_PROCESS={root}")
            raise RuntimeError(
                f"Redis 子进程未能退出，保留隔离目录：pid={process.pid if process else 'unknown'} root={root}"
            )
        if socket_path.exists():
            socket_path.unlink()
        shutil.rmtree(root)
        temporary_root_removed = not root.exists()
        print("REDIS_SOCKET_REMOVED=true")
        print(f"TEMP_ROOT_REMOVED={temporary_root_removed}")
        if not temporary_root_removed:
            raise RuntimeError("临时 Redis 目录清理失败")
        if close_error is not None:
            raise RuntimeError("临时 Redis 客户端关闭失败") from close_error
    return pytest_return_code if pytest_return_code is not None else 1


if __name__ == "__main__":
    raise SystemExit(main())
