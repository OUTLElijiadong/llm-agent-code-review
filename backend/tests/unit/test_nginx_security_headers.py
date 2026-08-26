"""Nginx 安全响应头与 CSP 回归测试。"""

from pathlib import Path

TEMPLATE = Path(__file__).resolve().parents[3] / "frontend/nginx.conf.template"
SECURITY_HEADERS = (
    "X-Content-Type-Options",
    "X-Frame-Options",
    "Referrer-Policy",
    "Strict-Transport-Security",
    "Content-Security-Policy-Report-Only",
)


def _block(source: str, marker: str) -> str:
    start = source.index(marker)
    opening = source.index("{", start)
    depth = 0
    for index in range(opening, len(source)):
        if source[index] == "{":
            depth += 1
        elif source[index] == "}":
            depth -= 1
            if depth == 0:
                return source[start:index + 1]
    raise AssertionError(f"未找到完整配置块: {marker}")


def test_static_child_locations_repeat_parent_security_headers() -> None:
    source = TEMPLATE.read_text(encoding="utf-8")

    for marker in ("location = /index.html", "location ^~ /assets/"):
        block = _block(source, marker)
        for header in SECURITY_HEADERS:
            assert f"add_header {header} " in block


def test_report_only_csp_covers_current_frontend_resource_types() -> None:
    source = TEMPLATE.read_text(encoding="utf-8")
    csp = next(
        line.strip()
        for line in source.splitlines()
        if "add_header Content-Security-Policy-Report-Only" in line
    )

    for directive in (
        "default-src 'self'",
        "script-src 'self' 'unsafe-inline'",
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com",
        "font-src 'self' data: https://fonts.gstatic.com",
        "connect-src 'self' wss:",
        "worker-src 'self' blob:",
        "object-src 'none'",
        "base-uri 'self'",
    ):
        assert directive in csp


def test_api_and_websocket_proxy_contracts_remain_present() -> None:
    source = TEMPLATE.read_text(encoding="utf-8")

    websocket = _block(source, "location /api/ws/")
    assert "proxy_set_header Upgrade $http_upgrade;" in websocket
    assert 'proxy_set_header Connection "upgrade";' in websocket
    assert "proxy_read_timeout 3600s;" in websocket

    api = _block(source, "location /api/ {")
    assert "proxy_pass http://backend:8000/api/;" in api
    assert "proxy_buffering off;" in api
