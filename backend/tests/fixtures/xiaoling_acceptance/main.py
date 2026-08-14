"""Disposable Xiaoling sandbox acceptance app."""

import os
from http.server import BaseHTTPRequestHandler, HTTPServer

# Intentional finding for the black-box/white-box review report.
ACCEPTANCE_TOKEN = "acceptance-hardcoded-token"


class Handler(BaseHTTPRequestHandler):
    """Return a deterministic health response for sandbox probing."""

    def do_GET(self):  # noqa: N802
        if self.path in {"/", "/health"}:
            body = b"PRISM_SANDBOX_OK"
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_response(404)
        self.end_headers()

    def log_message(self, _format, *_args):
        """Keep acceptance logs deterministic and quiet."""


def create_server() -> HTTPServer:
    """Build the local-only server used by the fixed sandbox runner."""
    port = int(os.environ.get("PORT", "8080"))
    return HTTPServer(("127.0.0.1", port), Handler)


if __name__ == "__main__":
    create_server().serve_forever()
