"""管理员安全中心输入契约。"""

import ipaddress

from pydantic import Field, field_validator

from app.schemas.common import StrictInputModel


class SecurityMonitorPolicyIn(StrictInputModel):
    """只允许提高或保持被动监控灵敏度。"""

    ssh_failed_threshold: int = Field(ge=1, le=5000)
    ssh_window_hours: int = Field(ge=1, le=24)
    nginx_failure_threshold: int = Field(ge=1, le=5000)
    nginx_window_hours: int = Field(ge=1, le=24)


class AutomaticBlockingPolicyIn(StrictInputModel):
    """固定规则、短期单 IP 封禁；不允许正文指定管理来源或任意命令。"""

    enabled: bool = Field(strict=True)
    ai_anomaly_enabled: bool = Field(default=False, strict=True)
    # 租约上限 3600 秒：宿主机侧同值硬约束，定期租约仍由内核 ipset TTL 自动到期。
    duration_seconds: int = Field(default=900, ge=60, le=3600, strict=True)
    window_seconds: int = Field(default=300, ge=60, le=900, strict=True)
    auto_escalate: bool = Field(default=False, strict=True)
    ssh_threshold: int = Field(default=20, ge=20, le=200, strict=True)
    web_threshold: int = Field(default=30, ge=30, le=500, strict=True)
    allowlist_cidrs: list[str] = Field(default_factory=list, max_length=32)

    @field_validator("allowlist_cidrs")
    @classmethod
    def narrow_protected_networks(cls, values: list[str]) -> list[str]:
        normalized = []
        for raw in values:
            network = ipaddress.ip_network(raw.strip(), strict=False)
            if network.prefixlen < (24 if network.version == 4 else 64):
                raise ValueError("IPv4 保护网段不能宽于 /24，IPv6 不能宽于 /64")
            if str(network) not in normalized:
                normalized.append(str(network))
        return normalized


class AutomaticBlockingReleaseIn(StrictInputModel):
    ip: str = Field(min_length=2, max_length=64)
    reason: str = Field(min_length=1, max_length=200)

    @field_validator("ip")
    @classmethod
    def canonical_ip(cls, value: str) -> str:
        return str(ipaddress.ip_address(value.strip()))

    @field_validator("reason")
    @classmethod
    def meaningful_reason(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("请填写解封原因")
        return text


class SecurityTraceIn(StrictInputModel):
    """被动来源溯源：只接受单个 IP，不接受命令、路径或端口参数。"""

    ip: str = Field(min_length=2, max_length=64)

    @field_validator("ip")
    @classmethod
    def single_address(cls, value: str) -> str:
        raw = value.strip()
        if "/" in raw:
            raise ValueError("溯源只接受单个 IP，不接受网段")
        return str(ipaddress.ip_address(raw))


class DecoyApplyIn(StrictInputModel):
    """诱捕层引流：只接受一个有意义的操作原因，目标由宿主机规则判定。"""

    reason: str = Field(min_length=1, max_length=200)

    @field_validator("reason")
    @classmethod
    def meaningful_reason(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("请填写引流原因")
        return text
