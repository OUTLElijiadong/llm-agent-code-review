import asyncio
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest


@pytest.mark.asyncio
async def test_direct_http11_trace_observes_body_before_response_headers():
    received, respond, closed = threading.Event(), threading.Event(), threading.Event()
    events = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            received.set()
            assert respond.wait(5)
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            self.wfile.write(b'data: {"type":"response.completed"}\n\n')
            self.wfile.flush()

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()

    async def trace(name, _info):
        if name == "http11.send_request_body.complete":
            events.append("body_complete")
            closed.set()

    async def collect():
        async with httpx.AsyncClient(timeout=5, trust_env=False, http1=True, http2=False) as client:
            async with client.stream("POST", f"http://127.0.0.1:{server.server_port}",
                                     json={"model": "test"}, extensions={"trace": trace}) as response:
                return await response.aread()

    try:
        task = asyncio.create_task(collect())
        assert await asyncio.to_thread(received.wait, 5)
        assert await asyncio.to_thread(closed.wait, 5)
        assert events == ["body_complete"]
        assert not task.done()
        respond.set()
        assert b"response.completed" in await task
    finally:
        respond.set()
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=5)
