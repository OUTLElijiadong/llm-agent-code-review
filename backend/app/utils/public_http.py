"""把已校验的公网 HTTP(S) URL 固定到本次解析得到的公网 IP。

仅在校验域名后再用域名发起请求仍存在 DNS 重绑定窗口。本模块保留原始 Host
与 TLS SNI，但让 TCP 连接直接指向已经验证的公网 IP，从连接层闭合该窗口。
"""

from __future__ import annotations

import ipaddress
import socket
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit

from app.core.exceptions import ValidationError


@dataclass(frozen=True)
class PinnedPublicUrl:
    """一次公网 URL 解析后的固定连接目标。"""

    original_url: str
    request_url: str
    host_header: str
    sni_hostname: str
    ip_address: str

    @property
    def request_extensions(self) -> dict[str, str]:
        """返回 httpx/httpcore 用于 TLS 主机名校验的扩展。"""

        return {"sni_hostname": self.sni_hostname}


def pin_public_http_url(url: str, *, require_https: bool = False) -> PinnedPublicUrl:
    """校验 URL 并把连接目标固定到已解析的公网 IP。

    Args:
        url: 不含内嵌凭据的 HTTP(S) URL。
        require_https: 是否强制 HTTPS。

    Returns:
        PinnedPublicUrl: 可直接交给 httpx 的固定目标及 Host/SNI 信息。

    Raises:
        ValidationError: URL 非法、DNS 失败或任一解析结果不是公网地址。
    """

    value = (url or "").strip()
    parsed = urlsplit(value)
    allowed_schemes = {"https"} if require_https else {"http", "https"}
    if parsed.scheme.lower() not in allowed_schemes or not parsed.hostname:
        scheme_label = "HTTPS" if require_https else "HTTP(S)"
        raise ValidationError(f"地址必须是有效的 {scheme_label} URL", code=40001)
    if parsed.username or parsed.password:
        raise ValidationError("地址不能包含用户名或密码", code=40001)
    try:
        port = parsed.port or (443 if parsed.scheme.lower() == "https" else 80)
    except ValueError as exc:
        raise ValidationError("地址端口格式无效", code=40001) from exc

    hostname = parsed.hostname.rstrip(".").lower()
    try:
        parsed_host_ip = ipaddress.ip_address(hostname)
        dns_hostname = hostname
        host_name_for_header = f"[{hostname}]" if parsed_host_ip.version == 6 else hostname
    except ValueError:
        try:
            dns_hostname = hostname.encode("idna").decode("ascii")
        except UnicodeError as exc:
            raise ValidationError("地址域名格式无效", code=40001) from exc
        host_name_for_header = dns_hostname
    try:
        rows = socket.getaddrinfo(dns_hostname, port, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise ValidationError("地址域名无法解析", code=40001) from exc

    addresses: set[ipaddress.IPv4Address | ipaddress.IPv6Address] = set()
    for row in rows:
        try:
            address = ipaddress.ip_address(row[4][0])
        except ValueError as exc:
            raise ValidationError("地址解析结果无效", code=40001) from exc
        if not address.is_global:
            raise ValidationError("地址解析到内网或保留地址", code=40001)
        addresses.add(address)
    if not addresses:
        raise ValidationError("地址域名没有可用的公网解析结果", code=40001)

    # IPv4 优先，避免仅部分部署环境具备 IPv6 出口时选择不可达地址。
    selected = sorted(addresses, key=lambda item: (item.version, int(item)))[0]
    ip_host = str(selected) if selected.version == 4 else f"[{selected}]"
    explicit_port = parsed.port is not None
    request_netloc = f"{ip_host}:{port}" if explicit_port else ip_host
    default_port = (parsed.scheme.lower() == "https" and port == 443) or (
        parsed.scheme.lower() == "http" and port == 80
    )
    host_header = (
        host_name_for_header
        if default_port and not explicit_port
        else f"{host_name_for_header}:{port}"
    )
    request_url = urlunsplit(
        (parsed.scheme.lower(), request_netloc, parsed.path, parsed.query, parsed.fragment)
    )
    return PinnedPublicUrl(
        original_url=value,
        request_url=request_url,
        host_header=host_header,
        sni_hostname=dns_hostname,
        ip_address=str(selected),
    )
