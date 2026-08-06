#!/usr/bin/env python3
"""Single-origin HTTP CONNECT proxy for the fixed Playwright worker.

The proxy never resolves DNS. It accepts only the approved host and port, then
opens the upstream socket to the public IP pinned by the Prism worker.
"""

from __future__ import annotations

import ipaddress
import os
import selectors
import socket
import socketserver
import sys
import urllib.parse

LISTEN_PORT = 3128
MAX_HEADER_BYTES = 65_536
SOCKET_TIMEOUT = 15.0
TARGET_HOST = os.environ.get("PRISM_TARGET_HOST", "").rstrip(".").casefold()
TARGET_PORT = int(os.environ.get("PRISM_TARGET_PORT", "0") or 0)
TARGET_IP = os.environ.get("PRISM_TARGET_IP", "")


def _authority(value: str) -> tuple[str, int]:
    parsed = urllib.parse.urlsplit(f"//{value}")
    if parsed.username or parsed.password or not parsed.hostname:
        raise ValueError("invalid proxy authority")
    return parsed.hostname.rstrip(".").casefold(), int(parsed.port or 0)


def _relay(client: socket.socket, upstream: socket.socket) -> None:
    selector = selectors.DefaultSelector()
    selector.register(client, selectors.EVENT_READ, upstream)
    selector.register(upstream, selectors.EVENT_READ, client)
    while selector.get_map():
        events = selector.select(timeout=SOCKET_TIMEOUT)
        if not events:
            return
        for key, _mask in events:
            source = key.fileobj
            destination = key.data
            try:
                chunk = source.recv(65_536)
            except OSError:
                chunk = b""
            if not chunk:
                try:
                    selector.unregister(source)
                except Exception:
                    pass
                try:
                    destination.shutdown(socket.SHUT_WR)
                except OSError:
                    pass
                continue
            destination.sendall(chunk)


class ProxyHandler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        self.request.settimeout(SOCKET_TIMEOUT)
        header = bytearray()
        while b"\r\n\r\n" not in header:
            chunk = self.request.recv(4096)
            if not chunk:
                return
            header.extend(chunk)
            if len(header) > MAX_HEADER_BYTES:
                return
        head, remainder = bytes(header).split(b"\r\n\r\n", 1)
        lines = head.split(b"\r\n")
        try:
            method_raw, target_raw, version_raw = lines[0].decode("ascii").split(" ", 2)
        except (UnicodeDecodeError, ValueError):
            return
        method = method_raw.upper()
        if version_raw not in {"HTTP/1.0", "HTTP/1.1"}:
            return
        try:
            if method == "CONNECT":
                host, port = _authority(target_raw)
                if host != TARGET_HOST or port != TARGET_PORT:
                    self.request.sendall(b"HTTP/1.1 403 Forbidden\r\nConnection: close\r\n\r\n")
                    return
                with socket.create_connection((TARGET_IP, TARGET_PORT), timeout=SOCKET_TIMEOUT) as upstream:
                    upstream.settimeout(SOCKET_TIMEOUT)
                    self.request.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                    if remainder:
                        upstream.sendall(remainder)
                    _relay(self.request, upstream)
                return

            parsed = urllib.parse.urlsplit(target_raw)
            if parsed.scheme not in {"http", "https"} or not parsed.hostname:
                return
            port = parsed.port or (443 if parsed.scheme == "https" else 80)
            host = parsed.hostname.rstrip(".").casefold()
            if host != TARGET_HOST or port != TARGET_PORT:
                self.request.sendall(b"HTTP/1.1 403 Forbidden\r\nConnection: close\r\n\r\n")
                return
            origin_form = urllib.parse.urlunsplit(("", "", parsed.path or "/", parsed.query, ""))
            forwarded = [f"{method} {origin_form} {version_raw}".encode("ascii")]
            for line in lines[1:]:
                lower = line.lower()
                if lower.startswith((b"proxy-authorization:", b"proxy-connection:")):
                    continue
                forwarded.append(line)
            request_bytes = b"\r\n".join(forwarded) + b"\r\n\r\n" + remainder
            with socket.create_connection((TARGET_IP, TARGET_PORT), timeout=SOCKET_TIMEOUT) as upstream:
                upstream.settimeout(SOCKET_TIMEOUT)
                upstream.sendall(request_bytes)
                _relay(self.request, upstream)
        except (OSError, ValueError):
            return


class ProxyServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    allow_reuse_address = True
    daemon_threads = True
    request_queue_size = 32


def main() -> None:
    try:
        address = ipaddress.ip_address(TARGET_IP)
    except ValueError as exc:
        raise SystemExit("invalid pinned target ip") from exc
    if not TARGET_HOST or not address.is_global or not 1 <= TARGET_PORT <= 65535:
        raise SystemExit("invalid fixed target")
    with ProxyServer(("0.0.0.0", LISTEN_PORT), ProxyHandler) as server:
        server.serve_forever(poll_interval=0.25)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        sys.stderr.write(f"browser proxy failed: {exc.__class__.__name__}\n")
        raise
