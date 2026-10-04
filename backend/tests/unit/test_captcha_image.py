"""注册挑战仅公开位图，保留过期、错误消费与一次性校验契约。"""
import base64
import io
import secrets

import pytest
from PIL import Image

from app.core import captcha
from app.core.exceptions import TooManyRequestsError


@pytest.fixture(autouse=True)
def empty_store():
    captcha._STORE.clear()
    yield
    captcha._STORE.clear()


def test_challenge_exposes_png_without_plain_answer_or_equation(monkeypatch):
    monkeypatch.setattr(secrets, "choice", lambda _: "K")
    result = captcha.create_captcha()
    assert set(result) == {"captcha_id", "question", "image_data"}
    assert "KKKKKK" not in str(result)
    assert result["question"] == "请输入图片中的 6 位字符（不区分大小写）"
    assert result["image_data"].startswith("data:image/png;base64,")
    raw = base64.b64decode(result["image_data"].split(",", 1)[1], validate=True)
    with Image.open(io.BytesIO(raw)) as image:
        assert image.format == "PNG"
        assert image.size == (168, 56)
        assert not image.info
    assert captcha.verify_captcha(result["captcha_id"], " kkkkkk ")
    assert not captcha.verify_captcha(result["captcha_id"], "KKKKKK")


@pytest.mark.parametrize("answer", ["", "INVALID", "ＫＫＫＫＫＫ"])
def test_wrong_answer_consumes_challenge(monkeypatch, answer):
    monkeypatch.setattr(secrets, "choice", lambda _: "K")
    result = captcha.create_captcha()
    assert not captcha.verify_captcha(result["captcha_id"], answer)
    assert not captcha.verify_captcha(result["captcha_id"], "KKKKKK")


def test_expired_challenge_is_rejected(monkeypatch):
    now = [10.0]
    monkeypatch.setattr(captcha.time, "time", lambda: now[0])
    monkeypatch.setattr(secrets, "choice", lambda _: "K")
    result = captcha.create_captcha()
    now[0] += captcha._TTL_SECONDS + 1
    assert not captcha.verify_captcha(result["captcha_id"], "KKKKKK")


def test_exact_expiry_is_rejected(monkeypatch):
    now = [10.0]
    monkeypatch.setattr(captcha.time, "time", lambda: now[0])
    monkeypatch.setattr(secrets, "choice", lambda _: "K")
    result = captcha.create_captcha()
    now[0] += captcha._TTL_SECONDS
    assert not captcha.verify_captcha(result["captcha_id"], "KKKKKK")


def test_capacity_refusal_preserves_existing_live_challenges(monkeypatch):
    monkeypatch.setattr(captcha, "_MAX_ENTRIES", 2)
    monkeypatch.setattr(secrets, "choice", lambda _: "K")
    first = captcha.create_captcha()
    captcha.create_captcha()
    with pytest.raises(TooManyRequestsError):
        captcha.create_captcha()
    assert len(captcha._STORE) == 2
    assert captcha.verify_captcha(first["captcha_id"], "KKKKKK")


def test_render_failure_does_not_leave_answer_in_store(monkeypatch):
    def fail(_):
        raise OSError("renderer unavailable")
    monkeypatch.setattr(captcha, "_render_image", fail, raising=False)
    with pytest.raises(OSError):
        captcha.create_captcha()
    assert not captcha._STORE
