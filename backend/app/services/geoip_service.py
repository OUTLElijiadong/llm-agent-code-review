"""IP 地理定位服务(GeoLite2 离线库)。

用 maxminddb 直接读 GeoLite2-City.mmdb(无需 geoip2 全库),把登录 IP 映射为
国家/城市/经纬度,供总览大屏"登录来源世界地图"打点。
库文件缺失或查询失败时优雅降级(返回 None 字段),不影响其它模块。
"""
from __future__ import annotations

import ipaddress
import os
from functools import lru_cache

_DB_PATH = os.environ.get(
    "GEOLITE_DB_PATH",
    os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "GeoLite2-City.mmdb"),
)

_reader = None
_load_failed = False


def _get_reader():
    global _reader, _load_failed
    if _reader is not None or _load_failed:
        return _reader
    try:
        import maxminddb  # type: ignore

        if os.path.exists(_DB_PATH):
            _reader = maxminddb.open_database(_DB_PATH)
        else:
            _load_failed = True
    except Exception:  # pragma: no cover
        _load_failed = True
    return _reader


def _is_public_ip(ip: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip)
        return addr.is_global
    except ValueError:
        return False


def locate_ip(ip: str) -> dict | None:
    """把 IP 映射为地理位置。

    Args:
        ip: IPv4/IPv6 字符串

    Returns:
        dict | None: {country, city, latitude, longitude};私网/查询失败返回 None
    """
    if not ip or not _is_public_ip(ip):
        return None
    reader = _get_reader()
    if reader is None:
        return None
    try:
        rec = reader.get(ip)
    except Exception:  # pragma: no cover
        return None
    if not rec:
        return None
    country = (rec.get("country") or {}).get("names", {}).get("zh-CN") or (rec.get("country") or {}).get("names", {}).get("en")
    city = (rec.get("city") or {}).get("names", {}).get("zh-CN") or (rec.get("city") or {}).get("names", {}).get("en")
    loc = rec.get("location") or {}
    lat, lng = loc.get("latitude"), loc.get("longitude")
    if lat is None or lng is None:
        return None
    return {"country": country, "city": city, "latitude": lat, "longitude": lng}


@lru_cache(maxsize=4096)
def locate_ip_cached(ip: str) -> dict | None:
    """locate_ip 的缓存版(IP 定位结果稳定,缓存避免重复读库)。"""
    return locate_ip(ip)
