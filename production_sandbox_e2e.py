#!/usr/bin/env python3
"""Production UDS smoke/e2e for the fixed Prism sandbox profiles."""

from __future__ import annotations

import base64
import hashlib
import http.client
import io
import json
import os
import socket
import time
import uuid
import zipfile
from pathlib import Path


SOCKET = "/run/prism-sandbox/agent.sock"
ENV_FILE = "/opt/code-review/deploy/.env"


def read_token() -> str:
    for raw in Path(ENV_FILE).read_text(encoding="utf-8").splitlines():
        if raw.startswith("SANDBOX_EXECUTOR_TOKEN="):
            value = raw.split("=", 1)[1].strip()
            if len(value) >= 32:
                return value.strip("'\"")
    raise RuntimeError("sandbox token not found")


class UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, path: str) -> None:
        super().__init__("localhost")
        self.path = path

    def connect(self) -> None:
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.connect(self.path)


TOKEN = read_token()


def call(method: str, path: str, payload: dict[str, object] | None = None) -> tuple[int, dict[str, object] | bytes]:
    connection = UnixHTTPConnection(SOCKET)
    body = json.dumps(payload, separators=(",", ":")).encode() if payload is not None else None
    headers = {"Authorization": f"Bearer {TOKEN}"}
    if body is not None:
        headers.update({"Content-Type": "application/json", "Content-Length": str(len(body))})
    connection.request(method, path, body=body, headers=headers)
    response = connection.getresponse()
    raw = response.read()
    status = response.status
    connection.close()
    if response.getheader("Content-Type", "").startswith("application/json"):
        return status, json.loads(raw)
    return status, raw


def archive(files: dict[str, str | bytes]) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as bundle:
        for name, content in files.items():
            bundle.writestr(name, content.encode() if isinstance(content, str) else content)
    return output.getvalue()


def submit(request_id: str, language: str, files: dict[str, str | bytes], purpose: str = "test", mode: str = "combined") -> dict[str, object]:
    data = archive(files)
    payload: dict[str, object] = {
        "request_id": request_id,
        "purpose": purpose,
        "language": language,
        "test_mode": mode,
        "source_archive_base64": base64.b64encode(data).decode(),
        "source_sha256": hashlib.sha256(data).hexdigest(),
        "ttl_seconds": 600,
    }
    status, response = call("POST", "/execute", payload)
    if status != 200 or not isinstance(response, dict) or not response.get("ok"):
        raise RuntimeError(f"submit {request_id} failed: {status} {response}")
    return response["result"]  # type: ignore[return-value]


def wait_terminal(request_id: str, *, running: bool = False) -> dict[str, object]:
    deadline = time.monotonic() + 180
    while time.monotonic() < deadline:
        status, response = call("POST", "/status", {"request_id": request_id})
        if status != 200 or not isinstance(response, dict) or not response.get("ok"):
            raise RuntimeError(f"status {request_id} failed: {status} {response}")
        state = response["result"]
        value = state["status"]  # type: ignore[index]
        if running and value == "running":
            return state  # type: ignore[return-value]
        if not running and value in {"succeeded", "failed", "blocked", "stopped", "expired"}:
            return state  # type: ignore[return-value]
        time.sleep(1)
    raise TimeoutError(f"sandbox timeout: {request_id}")


def assert_success(request_id: str, state: dict[str, object]) -> None:
    if state.get("status") != "succeeded" or state.get("runtime") != "runsc":
        raise RuntimeError(f"{request_id} unexpected state: {json.dumps(state, ensure_ascii=False)}")
    result = state.get("result") or {}
    if result.get("outcome") != "succeeded":  # type: ignore[union-attr]
        raise RuntimeError(f"{request_id} unexpected result: {result}")


def stop(request_id: str) -> None:
    status, response = call("POST", "/stop", {"request_id": request_id})
    if status != 200 or not isinstance(response, dict) or not response.get("ok"):
        raise RuntimeError(f"stop {request_id} failed: {status} {response}")
    state = wait_terminal(request_id)
    if state.get("status") != "stopped":
        raise RuntimeError(f"stop {request_id} did not stop: {state}")


HTTP_APP_PY = """from http.server import BaseHTTPRequestHandler, HTTPServer
class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        data = ("preview:" + self.path).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)
    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        data = b"post:" + self.rfile.read(length)
        self.send_response(201)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)
    def log_message(self, *_args): pass
HTTPServer(("127.0.0.1", int(__import__("os").environ.get("PORT", "8080"))), Handler).serve_forever()
"""


def main() -> None:
    status, health = call("GET", "/health")
    if status != 200 or not isinstance(health, dict) or not health.get("ok"):
        raise RuntimeError(f"health failed: {status} {health}")

    cases = [
        ("python", {"main.py": HTTP_APP_PY, "test_smoke.py": "import unittest\nclass SmokeTest(unittest.TestCase):\n    def test_ok(self): self.assertEqual(1 + 1, 2)\n"}, "combined"),
        ("node", {"package.json": '{"scripts":{"start":"node server.js"}}', "server.js": "require('http').createServer((q,s)=>{s.end('node')}).listen(process.env.PORT || 8080, '127.0.0.1')\n"}, "combined"),
        ("java", {"Hello.java": "class Hello { public static void main(String[] a) {} }\n"}, "whitebox"),
        ("go", {"go.mod": "module example.com/smoke\ngo 1.23\n", "main.go": "package main\nimport (\"net/http\"; \"os\")\nfunc main(){p:=os.Getenv(\"PORT\"); if p==\"\"{p=\"8080\"}; http.ListenAndServe(\"127.0.0.1:\"+p,http.HandlerFunc(func(w http.ResponseWriter,r *http.Request){w.Write([]byte(\"go\"))}))}\n"}, "combined"),
        ("php", {"index.php": "<?php echo 'php';\n"}, "combined"),
    ]
    for language, files, mode in cases:
        request_id = f"prod-e2e-{language}-{uuid.uuid4().hex[:12]}"
        result = submit(request_id, language, files, mode=mode)
        state = wait_terminal(request_id)
        assert_success(request_id, state)
        print(f"{language}: succeeded runtime={state.get('runtime')} image={state.get('image_digest')}")

    deploy_id = f"prod-deploy-{uuid.uuid4().hex[:16]}"
    submit(deploy_id, "python", {"main.py": HTTP_APP_PY}, purpose="deploy", mode="whitebox")
    running = wait_terminal(deploy_id, running=True)
    if not running.get("preview_supported"):
        raise RuntimeError(f"deploy preview not ready: {running}")
    preview_status, preview = call("GET", f"/preview/{deploy_id}/hello?q=hello%20world")
    if preview_status != 200 or preview != b"preview:/hello?q=hello%20world":
        raise RuntimeError(f"preview GET failed: {preview_status} {preview!r}")
    preview_status, preview = call("POST", f"/preview/{deploy_id}/echo", {"body": "unused"})
    if preview_status not in {201, 200}:
        raise RuntimeError(f"preview POST failed: {preview_status} {preview!r}")
    print("deploy preview: GET/POST passed")
    stop(deploy_id)

    bad_id = f"prod-bad-health-{uuid.uuid4().hex[:16]}"
    bad_app = """from http.server import BaseHTTPRequestHandler, HTTPServer
class H(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(500); self.end_headers()
    def log_message(self, *_args): pass
HTTPServer(('127.0.0.1', 8080), H).handle_request()
"""
    submit(bad_id, "python", {"main.py": bad_app}, purpose="deploy", mode="whitebox")
    bad_state = wait_terminal(bad_id)
    if bad_state.get("status") != "failed":
        raise RuntimeError(f"bad health was not rejected: {bad_state}")
    print("deploy 500 health: rejected and cleaned")


if __name__ == "__main__":
    main()
