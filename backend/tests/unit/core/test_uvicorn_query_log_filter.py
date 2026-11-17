"""Verify query credentials stay out of real Uvicorn HTTP and WebSocket logs."""

import json
import subprocess
import sys

import pytest


def test_real_uvicorn_redacts_query_credentials_and_keeps_connections_working():
    program = r'''
import io, json, logging, secrets, socket, threading, time
import httpx, uvicorn
from fastapi import WebSocket
from fastapi.responses import StreamingResponse
from websockets.sync.client import connect
from websockets.exceptions import InvalidStatus

access_sink, error_sink = io.StringIO(), io.StringIO()
for name, sink in (("uvicorn.access", access_sink), ("uvicorn.error", error_sink)):
    log = logging.getLogger(name)
    log.handlers = [logging.StreamHandler(sink)]
    log.setLevel(logging.INFO)
    log.propagate = False
from app.main import app

secret = secrets.token_hex(24)

@app.get('/__test/events')
async def events():
    return StreamingResponse(iter([b'data: ready\n\n']), media_type='text/event-stream')

@app.websocket('/__test/socket')
async def socket_route(ws: WebSocket):
    await ws.accept()
    await ws.send_text(ws.query_params.get('token', ''))
    await ws.close()

@app.websocket('/__test/rejected')
async def rejected(ws: WebSocket):
    await ws.close()

sock = socket.socket()
sock.bind(('127.0.0.1', 0))
sock.listen(100)
port = sock.getsockname()[1]
server = uvicorn.Server(uvicorn.Config(app, log_config=None, lifespan='off'))
thread = threading.Thread(target=server.run, kwargs={'sockets': [sock]}, daemon=True)
thread.start()
try:
    for _ in range(500):
        if server.started:
            break
        time.sleep(0.01)
    assert server.started
    root = f'http://127.0.0.1:{port}'
    with httpx.Client(timeout=5, trust_env=False) as client:
        actual = client.get(root + '/api/agents/events', params={'token': secret, 'replay': '2'})
        stream = client.get(root + '/__test/events?to%6Ben=' + secret + '&replay=2')
    with connect(f'ws://127.0.0.1:{port}/__test/socket?token={secret}&page=2', open_timeout=5) as ws:
        websocket_unchanged = ws.recv(timeout=5) == secret
    try:
        with connect(f'ws://127.0.0.1:{port}/__test/rejected?token={secret}&page=2', open_timeout=5):
            pass
    except InvalidStatus:
        pass
    try:
        with connect(f'ws://127.0.0.1:{port}/api/ws/discuss/1?token={secret}&page=2', open_timeout=5):
            pass
    except InvalidStatus:
        pass
    time.sleep(0.1)
    access, error = access_sink.getvalue(), error_sink.getvalue()
    print(json.dumps({'http_exposed': secret in access, 'ws_exposed': secret in error,
                      'actual_status': actual.status_code,
                      'stream_ok': stream.status_code == 200 and stream.text == 'data: ready\n\n',
                      'websocket_unchanged': websocket_unchanged,
                      'http_fields': 'GET ' in access and 'replay=2' in access and ' 200' in access,
                      'ws_fields': 'WebSocket ' in error and 'page=2' in error and '[accepted]' in error and '403' in error}))
finally:
    server.should_exit = True
    thread.join(timeout=10)
    sock.close()
'''
    result = subprocess.run([sys.executable, "-c", program], capture_output=True, text=True, timeout=30, check=True)
    evidence = json.loads(result.stdout)
    assert evidence["actual_status"] in (401, 403)
    assert evidence["stream_ok"] and evidence["websocket_unchanged"]
    assert evidence["http_fields"] and evidence["ws_fields"]
    assert (evidence["http_exposed"], evidence["ws_exposed"]) == (False, False)


def test_query_filter_handles_encoded_duplicate_and_case_variant_keys():
    from app.core.uvicorn_log_filter import _redact_query

    secret = "synthetic-value"
    request_target = f"/resource?TOKEN={secret}&to%6ben={secret}&token={secret}&page=2"
    result = _redact_query(request_target)
    assert secret not in result
    assert result.count("[REDACTED]") == 3
    assert result.startswith("/resource?") and result.endswith("&page=2")


def test_default_uvicorn_cli_redacts_both_log_streams():
    program = r'''
import json, os, secrets, socket, subprocess, sys, tempfile, time
from pathlib import Path
import httpx
from websockets.sync.client import connect

with tempfile.TemporaryDirectory(prefix='prism-query-log-') as directory:
    Path(directory, 'query_fixture.py').write_text("""
from fastapi import WebSocket
from fastapi.responses import StreamingResponse
from app.main import app
@app.get('/__test/events')
async def events():
    return StreamingResponse(iter([b'data: ready\\n\\n']), media_type='text/event-stream')
@app.websocket('/__test/socket')
async def socket_route(ws: WebSocket):
    await ws.accept()
    await ws.send_text(ws.query_params.get('token', ''))
    await ws.close()
""")
    probe = socket.socket()
    probe.bind(('127.0.0.1', 0))
    port = probe.getsockname()[1]
    probe.close()
    secret = secrets.token_hex(24)
    env = dict(os.environ, PYTHONPATH=directory + os.pathsep + os.getcwd() + os.pathsep + os.environ.get('PYTHONPATH', ''))
    child = subprocess.Popen([sys.executable, '-m', 'uvicorn', 'query_fixture:app', '--host', '127.0.0.1',
                              '--port', str(port), '--lifespan', 'off'],
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
    stream_ok = socket_ok = False
    try:
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline and child.poll() is None:
            try:
                with httpx.Client(timeout=1, trust_env=False) as client:
                    response = client.get(f'http://127.0.0.1:{port}/__test/events?token={secret}&page=2')
                    stream_ok = response.status_code == 200 and response.text == 'data: ready\n\n'
                break
            except httpx.HTTPError:
                time.sleep(0.05)
        if stream_ok:
            with connect(f'ws://127.0.0.1:{port}/__test/socket?token={secret}&page=2', open_timeout=5) as ws:
                socket_ok = ws.recv(timeout=5) == secret
        time.sleep(0.1)
    finally:
        child.terminate()
        try:
            stdout, stderr = child.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            child.kill()
            stdout, stderr = child.communicate(timeout=5)
    print(json.dumps({'stream_ok': stream_ok, 'socket_ok': socket_ok,
                      'http_exposed': secret.encode() in stdout, 'ws_exposed': secret.encode() in stderr,
                      'access_logged': b'page=2' in stdout and b'200 OK' in stdout,
                      'handshake_logged': b'page=2' in stderr and b'[accepted]' in stderr}))
'''
    result = subprocess.run([sys.executable, "-c", program], capture_output=True, text=True, timeout=40, check=True)
    evidence = json.loads(result.stdout)
    assert evidence["stream_ok"] and evidence["socket_ok"]
    assert evidence["access_logged"] and evidence["handshake_logged"]
    assert (evidence["http_exposed"], evidence["ws_exposed"]) == (False, False)


@pytest.mark.parametrize("custom_logging", [False, True])
@pytest.mark.parametrize("log_level", ["debug", "trace"])
def test_debug_websocket_handshake_is_private_in_each_worker(custom_logging, log_level):
    program = r'''
import json, os, secrets, socket, subprocess, sys, tempfile, time
import httpx
from pathlib import Path
from websockets.sync.client import connect

with tempfile.TemporaryDirectory(prefix='prism-ws-debug-') as directory:
    Path(directory, 'debug_fixture.py').write_text("""
import os
from fastapi import WebSocket
from app.main import app
@app.websocket('/__test/socket')
async def socket_route(ws: WebSocket):
    await ws.accept()
    await ws.send_text(str(os.getpid()) + ':' + ws.query_params.get('token', ''))
    await ws.close()
""")
    probe = socket.socket()
    probe.bind(('127.0.0.1', 0))
    port = probe.getsockname()[1]
    probe.close()
    secret = secrets.token_hex(24)
    env = dict(os.environ, PYTHONPATH=directory + os.pathsep + os.getcwd() + os.pathsep + os.environ.get('PYTHONPATH', ''))
    command = [sys.executable, '-m', 'uvicorn', 'debug_fixture:app', '--host', '127.0.0.1', '--port', str(port),
               '--workers', '2', '--log-level', sys.argv[2], '--lifespan', 'off']
    if sys.argv[1] == 'custom':
        config = Path(directory, 'logging.json')
        config.write_text(json.dumps({'version': 1, 'disable_existing_loggers': False,
          'formatters': {'plain': {'format': '%(levelname)s %(message)s'}},
          'handlers': {'stderr': {'class': 'logging.StreamHandler', 'stream': 'ext://sys.stderr', 'formatter': 'plain'},
                       'stdout': {'class': 'logging.StreamHandler', 'stream': 'ext://sys.stdout', 'formatter': 'plain'}},
          'loggers': {'uvicorn': {'handlers': ['stderr'], 'level': 'DEBUG', 'propagate': False},
                      'uvicorn.error': {'handlers': ['stderr'], 'level': 'DEBUG', 'propagate': False},
                      'uvicorn.access': {'handlers': ['stdout'], 'level': 'DEBUG', 'propagate': False}}}))
        command.extend(['--log-config', str(config)])
    with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
        child = subprocess.Popen(command, stdout=stdout, stderr=stderr, env=env)
        workers = set()
        actual_status = None
        try:
            deadline = time.monotonic() + 20
            while len(workers) < 2 and time.monotonic() < deadline and child.poll() is None:
                try:
                    with connect(f'ws://127.0.0.1:{port}/__test/socket?token={secret}&page=2',
                                 additional_headers={'Authorization': 'Bearer ' + secret, 'Cookie': 'session=' + secret},
                                 subprotocols=['prism-auth', secret], open_timeout=1) as ws:
                        worker, received = ws.recv(timeout=2).split(':', 1)
                        assert received == secret
                        workers.add(worker)
                except (OSError, TimeoutError):
                    time.sleep(0.05)
            if workers:
                with httpx.Client(timeout=5, trust_env=False) as client:
                    actual_status = client.get(f'http://127.0.0.1:{port}/api/agents/events?token={secret}&replay=2').status_code
            time.sleep(0.1)
        finally:
            child.terminate()
            try:
                child.wait(timeout=10)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(timeout=5)
        stdout.seek(0)
        stderr.seek(0)
        logged = stdout.read() + stderr.read()
    print(json.dumps({'workers': len(workers), 'actual_status': actual_status, 'exposed': secret.encode() in logged,
                      'handshake_logged': b'[accepted]' in logged and b'page=2' in logged}))
'''
    mode = "custom" if custom_logging else "default"
    result = subprocess.run([sys.executable, "-c", program, mode, log_level], capture_output=True, text=True, timeout=40, check=True)
    evidence = json.loads(result.stdout)
    assert evidence["workers"] == 2
    assert evidence["actual_status"] in (401, 403)
    assert evidence["handshake_logged"]
    assert not evidence["exposed"]
