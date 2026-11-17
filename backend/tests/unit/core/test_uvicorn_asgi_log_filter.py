"""Actual Uvicorn error logs must not disclose exception values."""

import json
import subprocess
import sys
from builtins import BaseExceptionGroup

import pytest


def test_real_uvicorn_sanitizes_errors_without_changing_control_flow():
    program = r'''
import asyncio, io, json, logging, secrets, socket, threading, time
import httpx, uvicorn
from fastapi import Request, WebSocket
from fastapi.responses import JSONResponse, StreamingResponse
from loguru import logger
from starlette.background import BackgroundTask
from websockets.exceptions import ConnectionClosed
from websockets.sync.client import connect

server_sink = io.StringIO()
server_logger = logging.getLogger("uvicorn.error")
server_logger.handlers = [logging.StreamHandler(server_sink)]
server_logger.propagate = False
server_logger.setLevel(logging.INFO)
from app.main import app

application_sink = io.StringIO()
logger.remove()
logger.configure(extra={"request_id": "-"})
logger.add(application_sink, format="{extra[request_id]} | {message}", diagnose=True, backtrace=True)
secret = secrets.token_hex(24)
disconnect_seen = threading.Event()

class LocalFailure(BaseException):
    pass

@app.get("/__test/before")
async def before():
    raise RuntimeError(secret)

@app.get("/__test/stream")
async def stream():
    async def body():
        yield b"partial"
        raise RuntimeError(secret)
    return StreamingResponse(body())

@app.get("/__test/background")
async def background():
    def fail():
        raise RuntimeError(secret)
    return JSONResponse({"ok": True}, background=BackgroundTask(fail))

@app.get("/__test/base")
async def base_failure():
    raise LocalFailure(secret)

@app.get("/__test/cancel")
async def cancel():
    raise asyncio.CancelledError(secret)

@app.get("/__test/base-group")
async def base_group():
    raise BaseExceptionGroup(secret, [asyncio.CancelledError(secret), LocalFailure(secret)])

@app.get("/__test/system-exit")
async def system_exit():
    raise SystemExit(secret)

@app.get("/__test/keyboard-interrupt")
async def keyboard_interrupt():
    raise KeyboardInterrupt(secret)

@app.get("/__test/control-group")
async def control_group():
    raise BaseExceptionGroup(secret, [SystemExit(secret), KeyboardInterrupt(secret)])

@app.post("/__test/disconnect")
async def disconnect(request: Request):
    while True:
        message = await request.receive()
        if message["type"] == "http.disconnect":
            disconnect_seen.set()
            raise RuntimeError(secret)

@app.websocket("/__test/socket")
async def websocket_route(websocket: WebSocket):
    await websocket.accept()
    raise RuntimeError(secret)

@app.get("/__test/health")
async def health():
    return {"ok": True}

sock = socket.socket()
sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
sock.bind(("127.0.0.1", 0))
sock.listen(100)
port = sock.getsockname()[1]
server = uvicorn.Server(uvicorn.Config(app, log_config=None, access_log=False, lifespan="off"))
thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
thread.start()
try:
    for _ in range(200):
        if server.started:
            break
        time.sleep(0.01)
    if not server.started:
        raise RuntimeError("local server did not start")
    root = f"http://127.0.0.1:{port}"
    with httpx.Client(timeout=5, trust_env=False, headers={"Connection": "close"}) as client:
        before = client.get(root + "/__test/before")
        background = client.get(root + "/__test/background")
        status_codes = [client.get(root + "/__test/" + path).status_code
                        for path in ("base", "cancel", "base-group", "system-exit",
                                     "keyboard-interrupt", "control-group")]
        partial = b""
        interrupted = False
        try:
            with client.stream("GET", root + "/__test/stream") as result:
                stream_status = result.status_code
                for chunk in result.iter_bytes():
                    partial += chunk
        except httpx.RemoteProtocolError:
            interrupted = True
        health = client.get(root + "/__test/health")
    before_disconnect = application_sink.getvalue().count("Unhandled request error type=RuntimeError")
    peer = socket.create_connection(("127.0.0.1", port), timeout=5)
    peer.sendall(b"POST /__test/disconnect HTTP/1.1\r\nHost: localhost\r\nContent-Length: 0\r\n\r\n")
    peer.close()
    saw_disconnect = disconnect_seen.wait(5)
    for _ in range(500):
        if application_sink.getvalue().count("Unhandled request error type=RuntimeError") > before_disconnect:
            break
        time.sleep(0.01)
    disconnect_logged = (
        application_sink.getvalue().count("Unhandled request error type=RuntimeError") > before_disconnect
    )
    websocket_closed = False
    with connect(f"ws://127.0.0.1:{port}/__test/socket", open_timeout=5) as websocket:
        try:
            websocket.recv(timeout=5)
        except ConnectionClosed:
            websocket_closed = True
    server_text, app_text = server_sink.getvalue(), application_sink.getvalue()
    print(json.dumps({
        "before_status": before.status_code, "before_code": before.json().get("code"),
        "background_ok": background.status_code == 200 and background.json().get("ok") is True,
        "status_codes": status_codes, "stream_status": stream_status,
        "partial_received": partial == b"partial", "stream_interrupted": interrupted,
        "disconnect_seen": saw_disconnect, "disconnect_logged": disconnect_logged,
        "websocket_closed": websocket_closed, "health_ok": health.status_code == 200,
        "server_exposed": secret in server_text, "app_exposed": secret in app_text,
        "server_diagnostic": "ASGI application" in server_text and "RuntimeError" in server_text,
    }))
finally:
    server.should_exit = True
    thread.join(timeout=10)
    sock.close()
'''
    result = subprocess.run([sys.executable, "-c", program], capture_output=True, text=True, timeout=35, check=True)
    evidence = json.loads(result.stdout)
    assert evidence["before_status"] == 500 and evidence["before_code"] == 50000
    assert evidence["background_ok"] and evidence["health_ok"]
    assert evidence["status_codes"] == [500, 500, 500, 500, 500, 500]
    assert evidence["stream_status"] == 200 and evidence["partial_received"] and evidence["stream_interrupted"]
    assert evidence["disconnect_seen"] and evidence["disconnect_logged"] and evidence["websocket_closed"]
    assert not evidence["app_exposed"] and not evidence["server_exposed"]
    assert evidence["server_diagnostic"]


@pytest.mark.asyncio
async def test_actual_app_keeps_original_cancellation_group_for_taskgroup():
    import asyncio

    from app.main import app

    failure = BaseExceptionGroup("sensitive-group", [asyncio.CancelledError("sensitive-leaf")])
    started, cancelled = asyncio.Event(), asyncio.Event()

    @app.get("/__test/taskgroup-identity")
    async def route():
        raise failure

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(_message):
        pass

    async def invoke():
        await started.wait()
        scope = {"type": "http", "http_version": "1.1", "method": "GET", "scheme": "http",
                 "path": "/__test/taskgroup-identity", "raw_path": b"/__test/taskgroup-identity",
                 "query_string": b"", "headers": []}
        await app(scope, receive, send)

    async def sibling():
        started.set()
        try:
            await asyncio.sleep(0.2)
        except asyncio.CancelledError:
            cancelled.set()
            raise

    with pytest.raises(BaseExceptionGroup) as caught:
        async with asyncio.TaskGroup() as group:
            group.create_task(sibling())
            group.create_task(invoke())
    assert cancelled.is_set()
    assert caught.value.exceptions[0] is failure


@pytest.mark.asyncio
@pytest.mark.parametrize("control", [SystemExit(23), KeyboardInterrupt(), GeneratorExit()])
@pytest.mark.parametrize("grouped", [False, True])
async def test_actual_app_preserves_interpreter_control_identity(control, grouped):
    from app.main import app

    failure = BaseExceptionGroup("control", [control]) if grouped else control
    path = f"/__test/control-{type(control).__name__}-{grouped}"

    async def route():
        raise failure

    app.add_api_route(path, route, methods=["GET"])
    sent = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)

    scope = {"type": "http", "http_version": "1.1", "method": "GET", "scheme": "http",
             "path": path, "raw_path": path.encode(), "query_string": b"", "headers": []}
    with pytest.raises(type(failure)) as caught:
        await app(scope, receive, send)
    assert caught.value is failure
    assert not sent


def test_uvicorn_logging_reconfiguration_keeps_the_filter():
    program = r'''
import io, json, logging, logging.config, secrets
from app.core.uvicorn_log_filter import install_uvicorn_asgi_error_filter
install_uvicorn_asgi_error_filter()
install_uvicorn_asgi_error_filter()
logging.config.dictConfig({
    "version": 1, "disable_existing_loggers": False,
    "handlers": {"null": {"class": "logging.NullHandler"}},
    "loggers": {"uvicorn.error": {"handlers": ["null"], "level": "INFO", "propagate": False}},
})
sink = io.StringIO()
server_logger = logging.getLogger("uvicorn.error")
server_logger.addHandler(logging.StreamHandler(sink))
secret = secrets.token_hex(24)
try:
    raise RuntimeError(secret)
except RuntimeError:
    server_logger.exception("Exception in ASGI application\n")
captured = sink.getvalue()
print(json.dumps({"exposed": secret in captured, "diagnostic": "RuntimeError" in captured,
                  "filter_count": sum(type(f).__name__ == "_ASGIErrorFilter" for f in server_logger.filters)}))
'''
    result = subprocess.run([sys.executable, "-c", program], capture_output=True, text=True, timeout=10, check=True)
    evidence = json.loads(result.stdout)
    assert not evidence["exposed"] and evidence["diagnostic"] and evidence["filter_count"] == 1


@pytest.mark.parametrize("custom_logging", [False, True])
def test_real_uvicorn_workers_each_install_the_filter(custom_logging):
    program = r'''
import json, os, secrets, socket, subprocess, sys, tempfile, time
from pathlib import Path
import httpx

with tempfile.TemporaryDirectory(prefix="prism-worker-test-") as directory:
    fixture = Path(directory) / "worker_fixture.py"
    fixture.write_text("""
import os
from app.main import app
@app.get('/__test/worker')
async def worker():
    return {'pid': os.getpid()}
@app.get('/__test/failure')
async def failure():
    raise RuntimeError(os.environ['PRISM_SYNTHETIC_TEST_SECRET'])
""")
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    secret = secrets.token_hex(24)
    env = dict(os.environ, PRISM_SYNTHETIC_TEST_SECRET=secret,
               PYTHONPATH=directory + os.pathsep + os.getcwd() + os.pathsep + os.environ.get("PYTHONPATH", ""))
    command = [sys.executable, "-m", "uvicorn", "worker_fixture:app", "--host", "127.0.0.1",
               "--port", str(port), "--workers", "2", "--no-access-log", "--lifespan", "off"]
    if sys.argv[1] == "custom":
        config = Path(directory) / "logging.json"
        config.write_text(json.dumps({
            "version": 1, "disable_existing_loggers": False,
            "formatters": {"plain": {"format": "%(levelname)s %(message)s"}},
            "handlers": {"stderr": {"class": "logging.StreamHandler", "stream": "ext://sys.stderr",
                                    "formatter": "plain"}},
            "loggers": {"uvicorn": {"handlers": ["stderr"], "level": "INFO", "propagate": False},
                        "uvicorn.error": {"handlers": ["stderr"], "level": "INFO", "propagate": False}},
        }))
        command.extend(["--log-config", str(config)])
    child = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, env=env)
    observed = set()
    error_statuses = []
    try:
        deadline = time.monotonic() + 20
        while len(observed) < 2 and time.monotonic() < deadline and child.poll() is None:
            try:
                with httpx.Client(timeout=1, trust_env=False) as client:
                    response = client.get(f"http://127.0.0.1:{port}/__test/worker")
                    worker = response.json()["pid"]
                    if worker not in observed:
                        observed.add(worker)
                        error_statuses.append(client.get(f"http://127.0.0.1:{port}/__test/failure").status_code)
            except (httpx.HTTPError, ValueError, KeyError):
                time.sleep(0.05)
        time.sleep(0.2)
    finally:
        child.terminate()
        try:
            _, stderr = child.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            child.kill()
            _, stderr = child.communicate(timeout=5)
    print(json.dumps({"worker_count": len(observed), "error_statuses": error_statuses,
                      "server_exposed": secret.encode() in stderr,
                      "diagnostic_count": stderr.count(b"ASGI application failed type=RuntimeError")}))
'''
    mode = "custom" if custom_logging else "default"
    result = subprocess.run(
        [sys.executable, "-c", program, mode], capture_output=True, text=True, timeout=40, check=True,
    )
    evidence = json.loads(result.stdout)
    assert evidence["worker_count"] == 2
    assert evidence["error_statuses"] == [500, 500]
    assert not evidence["server_exposed"] and evidence["diagnostic_count"] >= 2


def test_real_uvicorn_reload_reinstalls_the_filter():
    program = r'''
import json, os, secrets, socket, subprocess, sys, tempfile, time
from pathlib import Path
import httpx

with tempfile.TemporaryDirectory(prefix="prism-reload-test-") as directory:
    fixture = Path(directory) / "reload_fixture.py"
    fixture.write_text("""
import os
from app.main import app
@app.get('/__test/worker')
async def worker():
    return {'pid': os.getpid()}
@app.get('/__test/failure')
async def failure():
    raise RuntimeError(os.environ['PRISM_SYNTHETIC_TEST_SECRET'])
""")
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    secret = secrets.token_hex(24)
    env = dict(os.environ, PRISM_SYNTHETIC_TEST_SECRET=secret, WATCHFILES_FORCE_POLLING="true",
               PYTHONPATH=directory + os.pathsep + os.getcwd() + os.pathsep + os.environ.get("PYTHONPATH", ""))
    child = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "reload_fixture:app", "--host", "127.0.0.1", "--port", str(port),
         "--reload", "--reload-dir", directory, "--no-access-log", "--lifespan", "off"],
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, env=env,
    )
    first = None
    second = None
    statuses = []
    try:
        deadline = time.monotonic() + 20
        while first is None and time.monotonic() < deadline and child.poll() is None:
            try:
                with httpx.Client(timeout=1, trust_env=False) as client:
                    first = client.get(f"http://127.0.0.1:{port}/__test/worker").json()["pid"]
                    statuses.append(client.get(f"http://127.0.0.1:{port}/__test/failure").status_code)
            except (httpx.HTTPError, ValueError, KeyError):
                time.sleep(0.05)
        if first is not None:
            time.sleep(0.5)
            fixture.write_text(fixture.read_text() + "\n# trigger isolated reload\n")
            while second is None and time.monotonic() < deadline and child.poll() is None:
                try:
                    with httpx.Client(timeout=1, trust_env=False) as client:
                        worker = client.get(f"http://127.0.0.1:{port}/__test/worker").json()["pid"]
                        if worker != first:
                            second = worker
                            statuses.append(client.get(f"http://127.0.0.1:{port}/__test/failure").status_code)
                except (httpx.HTTPError, ValueError, KeyError):
                    time.sleep(0.05)
        time.sleep(0.2)
    finally:
        child.terminate()
        try:
            _, stderr = child.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            child.kill()
            _, stderr = child.communicate(timeout=5)
    print(json.dumps({"reloaded": first is not None and second is not None and first != second,
                      "statuses": statuses, "server_exposed": secret.encode() in stderr,
                      "diagnostic_count": stderr.count(b"ASGI application failed type=RuntimeError")}))
'''
    result = subprocess.run([sys.executable, "-c", program], capture_output=True, text=True, timeout=40, check=True)
    evidence = json.loads(result.stdout)
    assert evidence["reloaded"] and evidence["statuses"] == [500, 500]
    assert not evidence["server_exposed"] and evidence["diagnostic_count"] >= 2
