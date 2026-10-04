"""管理员安全中心输入契约。"""

from pydantic import Field

from app.schemas.common import StrictInputModel


class SecurityMonitorPolicyIn(StrictInputModel):
    """只允许提高或保持被动监控灵敏度。"""

    ssh_failed_threshold: int = Field(ge=1, le=5000)
    ssh_window_hours: int = Field(ge=1, le=24)
    nginx_failure_threshold: int = Field(ge=1, le=5000)
    nginx_window_hours: int = Field(ge=1, le=24)
