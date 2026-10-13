"""浏览器桥仅接受结构化观察，禁止任意脚本、选择器和输入值回传。"""

from typing import Literal

from pydantic import Field

from app.schemas.common import StrictInputModel


class BrowserContextIn(StrictInputModel):
    session_id: str = Field(min_length=8, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")
    surface: Literal["user", "admin"] = "user"
    client_nonce: str = Field(min_length=32, max_length=128)


class BrowserControlIn(StrictInputModel):
    client_handle: str = Field(min_length=32, max_length=32, pattern=r"^[a-f0-9]{32}$")
    role: Literal["button", "link", "textbox", "combobox", "checkbox", "radio", "tab", "option"]
    label: str = Field(min_length=1, max_length=160)
    type: str = Field(default="", max_length=32)
    readonly: bool = False


class BrowserObservationIn(StrictInputModel):
    snapshot_id: str = Field(min_length=32, max_length=32, pattern=r"^[a-f0-9]{32}$")
    route: str = Field(min_length=1, max_length=500)
    title: str = Field(default="", max_length=160)
    controls: list[BrowserControlIn] = Field(default_factory=list, max_length=120)
    messages: list[str] = Field(default_factory=list, max_length=20)


class BrowserAckIn(BrowserContextIn):
    claim_token: str = Field(min_length=32, max_length=128)
    outcome: Literal["completed", "failed", "indeterminate", "user_required"]
    observation: BrowserObservationIn
    error: str = Field(default="", max_length=500)
