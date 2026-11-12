import base64
import io
import json
from concurrent.futures import ThreadPoolExecutor
from fractions import Fraction
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.agents.base import BaseAgent
from app.agents.chat_agent import ChatAssistantAgent
from app.ai.deepseek_agent import DeepSeekAgent
from app.core.database import Base
from app.models.billing_ledger import BillingReservation
from app.models.enterprise_chat import EnterpriseAssistantRun
from app.services import billing_ledger_service
from app.services import billing_runtime_gate as billing_runtime
from app.services.agent_responses_service import NativeResponsesTransport
from app.services.billing_ledger_service import BillingError, available_milli, get_or_create_wallet, grant, quote_milli
from app.services.billing_runtime_gate import (
    _next_request_key,
    billing_scope,
    finish_attempt,
    request_input_upper_bound,
)
from app.services.billing_runtime_gate import (
    begin_attempt as _begin_attempt,
)
from app.services.billing_runtime_gate import (
    metered_response_request as _metered_response_request,
)
from app.services.deepseek_responses_service import DeepSeekResponsesService
from app.utils.api_resolver import ApiConfig


def _image_data_url(width=1, height=1):
    from PIL import Image

    raw = bytes((index * 31) % 256 for index in range(width * height * 3))
    image = Image.frombytes("RGB", (width, height), raw)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode("ascii")


def _responses_image_payload(data_url, *, role="user", detail="auto"):
    image = {"type": "input_image", "image_url": data_url, "detail": detail}
    return {
        "model": "deepseek-flash",
        "input": [{"type": "message", "role": role, "content": [
            {"type": "input_text", "text": "请描述这张图。"}, image,
        ]}],
        "max_output_tokens": 10,
    }


def begin_attempt(*, model_name, payload, **kwargs):
    kwargs.setdefault("provider_name", "deepseek")
    kwargs.setdefault("base_url", "https://api.deepseek.com/v1")
    kwargs.setdefault("credential_source", "system")
    return _begin_attempt(model_name=model_name, payload=payload, **kwargs)


def metered_response_request(*, model_name, payload, send, **kwargs):
    kwargs.setdefault("provider_name", "deepseek")
    kwargs.setdefault("base_url", "https://api.deepseek.com/v1")
    kwargs.setdefault("credential_source", "system")
    return _metered_response_request(model_name=model_name, payload=payload, send=send, **kwargs)


@pytest.fixture
def billing_context(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'runtime.sqlite'}")
    names = ("billing_wallet", "billing_credit_batch", "billing_limit", "billing_reservation",
             "billing_reservation_part", "billing_reservation_limit", "enterprise_assistant_run",
             "billing_rate_catalog", "billing_rate_catalog_item", "billing_rate_catalog_event")
    Base.metadata.create_all(engine, tables=[Base.metadata.tables[name] for name in names])
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    with Session.begin() as db:
        from app.services.billing_rate_catalog_service import seed_test_baseline

        seed_test_baseline(db)
        wallet = get_or_create_wallet(db, "user", 22)
        grant(db, wallet_id=wallet.id, grant_key="runtime-fund", kind="purchase", amount_milli=1000)
        wallet_id = wallet.id
    yield Session, wallet_id
    engine.dispose()


def assert_reservation_held(db, wallet_id, funded_milli):
    reservation = db.query(BillingReservation).one()
    assert available_milli(db, wallet_id) == funded_milli - reservation.requested_milli


@pytest.mark.parametrize("thinking", [None, {"type": "enabled"}, {"type": "disabled"}])
def test_thinking_known_shapes_are_included_in_bound(thinking):
    payload = {"model": "deepseek-flash", "messages": [{"role": "user", "content": "文本"}]}
    base = request_input_upper_bound(payload, 10_000, model_name="deepseek-flash")
    payload["thinking"] = thinking
    assert request_input_upper_bound(payload, 10_000, model_name="deepseek-flash") > base
    with pytest.raises(BillingError, match="输入上界"):
        request_input_upper_bound(payload, base, model_name="deepseek-flash")


@pytest.mark.parametrize("thinking", [True, "disabled", {}, {"type": "other"},
                                      {"type": "disabled", "unknown": "x"}, {"type": []}])
def test_thinking_unknown_shapes_fail_closed(thinking):
    payload = {"model": "deepseek-flash", "messages": [{"role": "user", "content": "文本"}],
               "thinking": thinking}
    with pytest.raises(BillingError):
        request_input_upper_bound(payload, 10_000, model_name="deepseek-flash")


def test_attempt_guard_fences_an_old_handler_before_reservation(billing_context):
    Session, wallet_id = billing_context
    with Session.begin() as db:
        db.add(EnterpriseAssistantRun(
            public_id="ear_fenced", request_key="run-fenced", enterprise_id=10, conversation_id=20,
            group_id=None, initiator_user_id=22, source_message_id=30, status="running",
            attempt_count=2, prompt_sha256="a" * 64,
        ))

    def stale_attempt_guard(db, _request_key):
        current = db.query(EnterpriseAssistantRun).filter_by(public_id="ear_fenced")\
            .populate_existing().with_for_update().one()
        if current.status != "running" or current.attempt_count != 1:
            raise BillingError("旧 attempt 已过期")

    with billing_scope(session_factory=Session, user_id=22, personal_wallet_id=wallet_id,
                       max_input_tokens=256, max_output_tokens=50,
                       request_key_prefix="enterprise-assistant:ear_fenced:1",
                       attempt_guard=stale_attempt_guard):
        with pytest.raises(BillingError, match="旧 attempt"):
            begin_attempt(model_name="deepseek-flash", payload={
                "model": "deepseek-flash", "input": "only once", "max_output_tokens": 10,
            })

    with Session() as db:
        assert db.query(BillingReservation).count() == 0
        assert available_milli(db, wallet_id) == 1000


@pytest.mark.parametrize(
    "tool",
    [
        {"type": "web_search"},
        {"type": "web_fetch"},
        {"type": "web_search_20250305", "name": "web_search", "max_uses": 1},
        {"type": "future_server_tool"},
    ],
)
def test_unpriced_builtin_tool_rejected_before_reservation(billing_context, tool, monkeypatch):
    Session, wallet_id = billing_context
    from app.services import billing_runtime_gate

    monkeypatch.setattr(billing_runtime_gate.settings, "billing_allow_unmetered_external_calls", True)
    with billing_scope(session_factory=Session, user_id=22, personal_wallet_id=wallet_id,
                       max_input_tokens=100, max_output_tokens=50):
        with pytest.raises(BillingError, match="工具"):
            begin_attempt(model_name="deepseek-flash", payload={"max_output_tokens": 50, "tools": [tool]})
    with Session() as db:
        assert db.query(BillingReservation).count() == 0
        assert available_milli(db, wallet_id) == 1000


def test_client_functions_remain_available_but_unpriced_builtin_tools_require_opt_in(
    billing_context, monkeypatch
):
    from app.services import billing_runtime_gate

    Session, wallet_id = billing_context
    payload = {"max_output_tokens": 50, "tools": [{"type": "function", "name": "web_search"}]}
    monkeypatch.setattr(billing_runtime_gate.settings, "billing_commercial_metering_ready", False)
    monkeypatch.setattr(billing_runtime_gate.settings, "billing_allow_unmetered_external_calls", False)
    billing_runtime_gate.verify_priced_tools(payload)
    with pytest.raises(BillingError, match="平台模型调用.*默认关闭"):
        begin_attempt(model_name="deepseek-flash", payload=payload, credential_source="system")
    with pytest.raises(BillingError, match="服务端工具.*未计量"):
        billing_runtime_gate.verify_priced_tools({"tools": [{"type": "web_search"}]})
    assert begin_attempt(
        model_name="deepseek-flash", payload=payload, credential_source="user",
    ) is None

    monkeypatch.setattr(billing_runtime_gate.settings, "billing_allow_unmetered_external_calls", True)
    assert begin_attempt(model_name="deepseek-flash", payload={"tools": [{"type": "web_search"}]}) is None
    with billing_scope(session_factory=Session, user_id=22, personal_wallet_id=wallet_id,
                       max_input_tokens=256, max_output_tokens=50):
        ticket = begin_attempt(model_name="deepseek-flash", payload=payload)
        finish_attempt(ticket, {"input_tokens": 10, "output_tokens": 5,
                                "server_tool_use": {"tool_search": 0, "tool_fetch": 0}})
    with Session() as db:
        record = db.query(BillingReservation).one()
        assert record.status == "settled"
        assert (record.server_tool_search_calls, record.server_tool_fetch_calls) == (0, 0)


def test_finish_attempt_uses_reserved_rate_snapshot_after_catalog_changes(billing_context, monkeypatch):
    Session, wallet_id = billing_context
    with billing_scope(session_factory=Session, user_id=22, personal_wallet_id=wallet_id,
                       max_input_tokens=256, max_output_tokens=50):
        ticket = begin_attempt(model_name="deepseek-flash", payload={
            "model": "deepseek-flash", "input": "before price change", "max_output_tokens": 10,
        })
        monkeypatch.setitem(billing_ledger_service._RATES, "deepseek-flash", (Fraction(2), Fraction(8)))
        finish_attempt(ticket, {"input_tokens": 10, "output_tokens": 5})

    with Session() as db:
        reservation = db.query(BillingReservation).one()
        assert reservation.status == "settled"
        assert reservation.actual_milli == 30


def test_attempt_reservation_uses_request_bound_and_finish_checks_ticket_bound(billing_context):
    Session, wallet_id = billing_context
    payload = {
        "model": "deepseek-flash",
        "instructions": "请按要求回答。",
        "input": "x" * 40,
        "max_output_tokens": 10,
    }
    expected_limit = len(json.dumps(payload, ensure_ascii=False, sort_keys=True,
                                    separators=(",", ":")).encode("utf-8")) + 32 + 8
    with billing_scope(session_factory=Session, user_id=22, personal_wallet_id=wallet_id,
                       max_input_tokens=512, max_output_tokens=50):
        ticket = begin_attempt(model_name="deepseek-flash", payload=payload)
        assert ticket["input_limit"] == expected_limit
        with Session() as db:
            reservation = db.query(BillingReservation).one()
            assert reservation.requested_milli == quote_milli("deepseek-flash", expected_limit, 10)
        with pytest.raises(BillingError, match="超出预占"):
            finish_attempt(ticket, {"input_tokens": expected_limit + 1, "output_tokens": 1})
    with Session() as db:
        reservation = db.query(BillingReservation).one()
        assert reservation.status == "unknown" and reservation.actual_milli is None


def test_metered_chat_reserves_from_actual_request_bound(billing_context):
    Session, wallet_id = billing_context
    payload = {"model": "deepseek-flash", "messages": [{"role": "user", "content": "hello"}],
               "max_tokens": 50}
    sent = []
    with billing_scope(session_factory=Session, user_id=22, personal_wallet_id=wallet_id,
                       max_input_tokens=512, max_output_tokens=50):
        response, _ = metered_response_request(
            model_name="deepseek-flash", payload=payload,
            send=lambda: (sent.append(True) or (httpx.Response(200, json={
                "usage": {"prompt_tokens": 10, "completion_tokens": 5},
            }), 1)),
        )
    assert response.status_code == 200 and sent == [True]
    with Session() as db:
        reservation = db.query(BillingReservation).one()
        assert reservation.status == "settled"
        assert reservation.input_tokens == 10
        assert reservation.requested_milli > reservation.actual_milli


def test_metered_chat_uses_scope_prefix_for_attempt_idempotency(billing_context):
    Session, wallet_id = billing_context
    payload = {"model": "deepseek-flash", "messages": [{"role": "user", "content": "team task"}],
               "max_tokens": 50}
    prefix = "agent-team:7:task:9:attempt-1"

    def send():
        return httpx.Response(200, json={
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
        }), 1

    with billing_scope(session_factory=Session, user_id=22, personal_wallet_id=wallet_id,
                       max_input_tokens=512, max_output_tokens=50, request_key_prefix=prefix):
        metered_response_request(model_name="deepseek-flash", payload=payload, send=send)
        metered_response_request(model_name="deepseek-flash", payload=payload, send=send)

    with Session() as db:
        assert {row.request_key for row in db.query(BillingReservation).all()} == {
            f"{prefix}:1", f"{prefix}:2",
        }

    replayed = []
    with billing_scope(session_factory=Session, user_id=22, personal_wallet_id=wallet_id,
                       max_input_tokens=512, max_output_tokens=50, request_key_prefix=prefix):
        with pytest.raises(BillingError, match="禁止重复发送"):
            metered_response_request(
                model_name="deepseek-flash", payload=payload,
                send=lambda: replayed.append(True),
            )
    assert replayed == []


def test_request_key_counter_is_thread_safe_for_concurrent_calls():
    prefix = "parallel-agent-team:run-1"
    with billing_scope(
        session_factory=lambda: None, user_id=22, max_input_tokens=256,
        max_output_tokens=50, request_key_prefix=prefix,
    ):
        scope = billing_runtime._current_scope()
        with ThreadPoolExecutor(max_workers=16) as executor:
            keys = list(executor.map(lambda _index: _next_request_key(scope), range(200)))

    assert len(keys) == len(set(keys)) == 200
    assert {int(key.rsplit(":", 1)[1]) for key in keys} == set(range(1, 201))


def test_metered_chat_reserves_only_the_request_output_ceiling(billing_context):
    Session, wallet_id = billing_context
    payload = {"model": "deepseek-flash", "messages": [{"role": "user", "content": "bounded"}],
               "max_tokens": 10}
    with billing_scope(session_factory=Session, user_id=22, personal_wallet_id=wallet_id,
                       max_input_tokens=512, max_output_tokens=50):
        metered_response_request(
            model_name="deepseek-flash", payload=payload,
            send=lambda: (httpx.Response(200, json={
                "usage": {"prompt_tokens": 5, "completion_tokens": 2},
            }), 1),
        )
    with Session() as db:
        reservation = db.query(BillingReservation).one()
        assert reservation.requested_milli == quote_milli(
            "deepseek-flash", request_input_upper_bound(payload, 512), 10,
        )


@pytest.mark.parametrize("key,value", [
    ("previous_response_id", "resp_old"),
    ("response_id", "resp_old"),
    ("conversation", "conv_old"),
    ("conversation_id", "conv_old"),
])
def test_stateful_request_rejected_before_reservation(billing_context, key, value):
    Session, wallet_id = billing_context
    payload = {"max_output_tokens": 10, key: value}
    with billing_scope(session_factory=Session, user_id=22, personal_wallet_id=wallet_id,
                       max_input_tokens=512, max_output_tokens=50):
        with pytest.raises(BillingError, match="状态化"):
            begin_attempt(model_name="deepseek-flash", payload=payload)
    with Session() as db:
        assert db.query(BillingReservation).count() == 0


@pytest.mark.parametrize("item", [
    {"type": "message", "role": "user", "content": [{"type": "input_image", "image_url": "https://x/i"}]},
    {"type": "message", "role": "user", "content": [{"type": "input_audio", "data": "..."}]},
    {"type": "message", "role": "user", "content": [{"type": "input_file", "file_id": "f1"}]},
    {"type": "video", "url": "https://x/v"},
])
def test_multimodal_responses_input_rejected_before_reservation(billing_context, item):
    Session, wallet_id = billing_context
    with billing_scope(session_factory=Session, user_id=22, personal_wallet_id=wallet_id,
                       max_input_tokens=512, max_output_tokens=50):
        with pytest.raises(BillingError, match="未知|非文本|图片"):
            begin_attempt(model_name="deepseek-flash", payload={"input": [item], "max_output_tokens": 10})
    with Session() as db:
        assert db.query(BillingReservation).count() == 0


def test_plain_text_responses_message_content_has_an_input_upper_bound():
    payload = {
        "model": "deepseek-flash",
        "input": [{"type": "message", "role": "user", "content": "整理本次测试计划"}],
        "max_output_tokens": 10,
    }
    serialized_bytes = len(json.dumps(payload, ensure_ascii=False, sort_keys=True,
                                      separators=(",", ":")).encode("utf-8"))

    assert request_input_upper_bound(payload, 10_000, model_name="deepseek-flash") == (
        serialized_bytes + 32 + 8
    )


def test_priced_vision_reserves_provider_image_ceiling_without_counting_base64_bytes():
    small = _image_data_url()
    large = _image_data_url(64, 64)
    assert len(large) > len(small) * 5

    small_payload = _responses_image_payload(small)
    large_payload = _responses_image_payload(large)
    small_bound = request_input_upper_bound(small_payload, 10_000, model_name="deepseek-flash")
    large_bound = request_input_upper_bound(large_payload, 10_000, model_name="deepseek-flash")
    text_payload = {
        "model": "deepseek-flash",
        "input": [{"type": "message", "role": "user", "content": [
            {"type": "input_text", "text": "请描述这张图。"},
        ]}],
        "max_output_tokens": 10,
    }
    text_bound = request_input_upper_bound(text_payload, 10_000, model_name="deepseek-flash")

    assert small_bound == large_bound
    assert 1024 < small_bound - text_bound < 1200


@pytest.mark.parametrize("model_name", [None, "deepseek-v4-pro", "other-model"])
def test_priced_vision_rejects_models_without_an_approved_vision_route(model_name):
    payload = _responses_image_payload(_image_data_url())
    with pytest.raises(BillingError, match="视觉模型"):
        request_input_upper_bound(payload, 10_000, model_name=model_name)


@pytest.mark.parametrize("role", ["system", "assistant", "tool"])
def test_priced_vision_rejects_unsupported_message_roles(role):
    payload = _responses_image_payload(_image_data_url(), role=role)
    with pytest.raises(BillingError, match="角色|user|developer"):
        request_input_upper_bound(payload, 10_000, model_name="deepseek-flash")


@pytest.mark.parametrize("image_url", [
    "https://example.invalid/image.png",
    "data:image/png;base64,not-base64!",
    "data:image/jpeg;base64," + base64.b64encode(b"not an image").decode("ascii"),
    "data:image/png;base64," + base64.b64encode(b"x" * 1_500_001).decode("ascii"),
])
def test_priced_vision_rejects_unbounded_or_invalid_image_sources(image_url):
    with pytest.raises(BillingError, match="图片|image|格式|上限"):
        request_input_upper_bound(_responses_image_payload(image_url), 10_000, model_name="deepseek-flash")


def test_priced_vision_rejects_file_ids_and_unknown_details():
    file_payload = {
        "model": "deepseek-flash",
        "input": [{"type": "message", "role": "user", "content": [
            {"type": "input_image", "file_id": "file-api-unknown"},
        ]}],
        "max_output_tokens": 10,
    }
    with pytest.raises(BillingError, match="图片|字段"):
        request_input_upper_bound(file_payload, 10_000, model_name="deepseek-flash")

    with pytest.raises(BillingError, match="detail|级别|图片"):
        request_input_upper_bound(
            _responses_image_payload(_image_data_url(), detail="unbounded"), 10_000,
            model_name="deepseek-flash",
        )


def test_priced_vision_rejects_images_that_exceed_provider_dimension_limit():
    with pytest.raises(BillingError, match="尺寸|维度|图片"):
        request_input_upper_bound(
            _responses_image_payload(_image_data_url(8193, 1)), 10_000,
            model_name="deepseek-flash",
        )


def test_priced_vision_applies_smaller_provider_dimensions_at_fifteen_images():
    image = _image_data_url()
    wide_image = _image_data_url(5000, 1)
    payload = _responses_image_payload(image)
    payload["input"][0]["content"].extend(
        [{"type": "input_image", "image_url": image, "detail": "auto"} for _ in range(13)]
    )
    payload["input"][0]["content"].append(
        {"type": "input_image", "image_url": wide_image, "detail": "auto"}
    )
    with pytest.raises(BillingError, match="尺寸"):
        request_input_upper_bound(payload, 100_000, model_name="deepseek-flash")


def test_priced_vision_counts_provider_dimension_threshold_across_messages():
    image = _image_data_url()
    wide_image = _image_data_url(5000, 1)
    payload = {
        "model": "deepseek-flash",
        "input": [
            {"type": "message", "role": "user", "content": [
                {"type": "input_image", "image_url": image, "detail": "auto"} for _ in range(8)
            ]},
            {"type": "message", "role": "user", "content": [
                *[{"type": "input_image", "image_url": image, "detail": "auto"} for _ in range(6)],
                {"type": "input_image", "image_url": wide_image, "detail": "auto"},
            ]},
        ],
        "max_output_tokens": 10,
    }
    with pytest.raises(BillingError, match="尺寸"):
        request_input_upper_bound(payload, 100_000, model_name="deepseek-flash")


def test_priced_vision_enforces_provider_image_count_and_local_body_limit(monkeypatch):
    monkeypatch.setattr(billing_runtime, "decode_data_image_url", lambda *_args, **_kwargs: ("image/png", b"x"))
    monkeypatch.setattr(billing_runtime, "_image_dimensions", lambda _binary: (1, 1))
    image = {"type": "input_image", "image_url": "data:image/png;base64,eA=="}

    too_many = _responses_image_payload("data:image/png;base64,eA==")
    too_many["input"][0]["content"] = [image] * 601
    with pytest.raises(BillingError, match="图片数量"):
        request_input_upper_bound(too_many, 1_000_000, model_name="deepseek-flash")

    monkeypatch.setattr(
        billing_runtime, "decode_data_image_url",
        lambda *_args, **_kwargs: ("image/png", b"x" * 1_500_000),
    )
    too_large = _responses_image_payload("data:image/png;base64,eA==")
    too_large["input"][0]["content"] = [image] * 20
    with pytest.raises(BillingError, match="图片总量"):
        request_input_upper_bound(too_large, 100_000, model_name="deepseek-flash")


def test_priced_vision_exceeding_wallet_input_budget_fails_before_reservation(billing_context):
    Session, wallet_id = billing_context
    with billing_scope(session_factory=Session, user_id=22, personal_wallet_id=wallet_id,
                       max_input_tokens=512, max_output_tokens=50):
        with pytest.raises(BillingError, match="输入上界超过"):
            begin_attempt(model_name="deepseek-flash", payload=_responses_image_payload(_image_data_url()))
    with Session() as db:
        assert db.query(BillingReservation).count() == 0


def test_priced_vision_attempt_reserves_the_image_ceiling(billing_context):
    Session, wallet_id = billing_context
    with Session.begin() as db:
        grant(db, wallet_id=wallet_id, grant_key="vision-test-fund", kind="purchase", amount_milli=2_000)
    payload = _responses_image_payload(_image_data_url())
    expected_limit = request_input_upper_bound(payload, 10_000, model_name="deepseek-flash")
    with billing_scope(session_factory=Session, user_id=22, personal_wallet_id=wallet_id,
                       max_input_tokens=10_000, max_output_tokens=50):
        ticket = begin_attempt(model_name="deepseek-flash", payload=payload)
        with pytest.raises(BillingError, match="超出预占"):
            finish_attempt(ticket, {
                "input_tokens": expected_limit + 1,
                "output_tokens": 1,
            })
    assert ticket["input_limit"] == expected_limit
    with Session() as db:
        reservation = db.query(BillingReservation).one()
        assert reservation.requested_milli == quote_milli("deepseek-flash", expected_limit, 10)
        assert reservation.status == "unknown" and reservation.actual_milli is None
        assert_reservation_held(db, wallet_id, 3_000)


@pytest.mark.parametrize("payload", [
    {"max_output_tokens": 10, "future_request_field": "x"},
    {"input": [{"type": "message", "role": "user", "content": "hi", "future": "x"}],
     "max_output_tokens": 10},
    {"max_output_tokens": 10, "tools": [{"type": "function", "name": "lookup", "future": True}]},
])
def test_unknown_request_or_tool_fields_rejected_before_reservation(billing_context, payload):
    Session, wallet_id = billing_context
    with billing_scope(session_factory=Session, user_id=22, personal_wallet_id=wallet_id,
                       max_input_tokens=512, max_output_tokens=50):
        with pytest.raises(BillingError, match="未知"):
            begin_attempt(model_name="deepseek-flash", payload=payload)
    with Session() as db:
        assert db.query(BillingReservation).count() == 0


def test_payload_bound_over_scope_rejected_before_send_or_reservation(billing_context):
    Session, wallet_id = billing_context
    sent = []
    payload = {"model": "deepseek-flash", "messages": [{"role": "user", "content": "x" * 256}],
               "max_tokens": 10}
    with billing_scope(session_factory=Session, user_id=22, personal_wallet_id=wallet_id,
                       max_input_tokens=100, max_output_tokens=50):
        with pytest.raises(BillingError, match="输入上界超过"):
            metered_response_request(model_name="deepseek-flash", payload=payload,
                                     send=lambda: sent.append(True))
    assert sent == []
    with Session() as db:
        assert db.query(BillingReservation).count() == 0


def test_commercial_metering_ready_fails_closed_without_scope(monkeypatch):
    from app.services import billing_runtime_gate

    monkeypatch.setattr(billing_runtime_gate.settings, "billing_commercial_metering_ready", True)
    sent = []
    with pytest.raises(BillingError, match="未进入可信计费作用域"):
        begin_attempt(model_name="deepseek-flash", payload={"max_output_tokens": 10})
    with pytest.raises(BillingError, match="未进入可信计费作用域"):
        metered_response_request(model_name="deepseek-flash", payload={"max_tokens": 10},
                                 send=lambda: sent.append(True))
    with pytest.raises(BillingError, match="未进入可信计费作用域"):
        billing_runtime_gate.reject_unpriced_call("BYOK 外呼", caller_pays=True)
    assert sent == []


def test_unscoped_platform_model_call_is_blocked_but_byok_and_explicit_legacy_opt_in_work(monkeypatch):
    from app.services import billing_runtime_gate

    monkeypatch.setattr(billing_runtime_gate.settings, "billing_commercial_metering_ready", False)
    monkeypatch.setattr(billing_runtime_gate.settings, "billing_allow_unmetered_external_calls", False)
    sent = []
    chat_payload = {"model": "deepseek-flash", "messages": [{"role": "user", "content": "hello"}],
                    "max_tokens": 10}
    responses_payload = {"model": "deepseek-flash", "input": "hello", "max_output_tokens": 10}

    with pytest.raises(BillingError, match="平台模型调用.*默认关闭"):
        metered_response_request(
            model_name="deepseek-flash", payload=chat_payload,
            send=lambda: sent.append("platform-chat"), credential_source="system",
        )
    with pytest.raises(BillingError, match="平台模型调用.*默认关闭"):
        begin_attempt(
            model_name="deepseek-flash", payload=responses_payload, credential_source="global",
        )
    assert sent == []

    response = metered_response_request(
        model_name="deepseek-flash", payload=chat_payload,
        send=lambda: (sent.append("byok") or "response"), credential_source="user",
    )
    assert response == "response" and sent == ["byok"]
    assert begin_attempt(
        model_name="deepseek-flash", payload=responses_payload, credential_source="user",
    ) is None

    monkeypatch.setattr(billing_runtime_gate.settings, "billing_allow_unmetered_external_calls", True)
    assert metered_response_request(
        model_name="deepseek-flash", payload=chat_payload,
        send=lambda: (sent.append("legacy") or "legacy response"), credential_source="system",
    ) == "legacy response"
    assert sent == ["byok", "legacy"]


@pytest.mark.parametrize("tool_usage,expected_counts", [
    (None, (None, None)),
    ({}, (None, None)),
    ({"tool_search": 1, "tool_fetch": 0}, (1, 0)),
    ({"tool_search": 0, "tool_fetch": 2}, (0, 2)),
    ({"tool_search": False, "tool_fetch": 0}, (None, None)),
    ({"tool_search": 2**63, "tool_fetch": 0}, (None, None)),
    ({"tool_search": 0, "tool_fetch": 0, "future_count": 0}, (None, None)),
])
def test_unpriced_or_malformed_provider_tool_usage_remains_frozen(billing_context, tool_usage, expected_counts):
    Session, wallet_id = billing_context
    with billing_scope(session_factory=Session, user_id=22, personal_wallet_id=wallet_id,
                       max_input_tokens=100, max_output_tokens=50):
        ticket = begin_attempt(model_name="deepseek-flash", payload={"max_output_tokens": 50})
        with pytest.raises(BillingError, match="工具"):
            finish_attempt(ticket, {"input_tokens": 10, "output_tokens": 5, "server_tool_use": tool_usage})
    with Session() as db:
        record = db.query(BillingReservation).one()
        assert record.status == "unknown" and record.actual_milli is None
        assert (record.server_tool_search_calls, record.server_tool_fetch_calls) == expected_counts
        assert_reservation_held(db, wallet_id, 1000)


def test_zero_server_tool_usage_is_recorded_with_token_settlement(billing_context):
    Session, wallet_id = billing_context
    with billing_scope(session_factory=Session, user_id=22, personal_wallet_id=wallet_id,
                       max_input_tokens=100, max_output_tokens=50):
        ticket = begin_attempt(model_name="deepseek-flash", payload={"max_output_tokens": 50})
        finish_attempt(ticket, {"input_tokens": 10, "output_tokens": 5,
                               "server_tool_use": {"tool_search": 0, "tool_fetch": 0}})
    with Session() as db:
        record = db.query(BillingReservation).one()
        assert record.status == "settled"
        assert (record.server_tool_search_calls, record.server_tool_fetch_calls) == (0, 0)


def test_paid_transport_failure_is_not_exposed_as_retryable_httpx_error(billing_context, monkeypatch):
    Session, wallet_id = billing_context
    agent = DeepSeekAgent.__new__(DeepSeekAgent)
    agent.model = "deepseek-flash"
    def timeout(*args, **kwargs):
        raise httpx.ReadTimeout("upstream uncertain")
    monkeypatch.setattr(agent, "_request_without_billing", timeout)
    with billing_scope(session_factory=Session, user_id=22, personal_wallet_id=wallet_id,
                       max_input_tokens=100, max_output_tokens=50):
        with pytest.raises(BillingError, match="未知") as failure:
            agent._request_with_cancellation("url", {}, {"max_tokens": 50}, on_cancel=None)
    assert isinstance(failure.value.__cause__, httpx.ReadTimeout)
    with Session() as db:
        assert db.query(BillingReservation).one().status == "unknown"
        assert (db.query(BillingReservation).one().server_tool_search_calls,
                db.query(BillingReservation).one().server_tool_fetch_calls) == (None, None)
        assert_reservation_held(db, wallet_id, 1000)


def test_sync_model_unpriced_provider_usage_stays_unknown(billing_context, monkeypatch):
    Session, wallet_id = billing_context
    agent = DeepSeekAgent.__new__(DeepSeekAgent)
    agent.model = "deepseek-flash"
    monkeypatch.setattr(agent, "_request_without_billing", lambda *a, **kw: (httpx.Response(200, json={
        "usage": {"prompt_tokens": 10, "completion_tokens": 5,
                  "server_tool_use": {"tool_search": 1, "tool_fetch": 0}},
    }), 1))
    with billing_scope(session_factory=Session, user_id=22, personal_wallet_id=wallet_id,
                       max_input_tokens=100, max_output_tokens=50):
        with pytest.raises(BillingError, match="工具"):
            agent._request_with_cancellation("url", {}, {"max_tokens": 50}, on_cancel=None)
    with Session() as db:
        record = db.query(BillingReservation).one()
        assert record.status == "unknown"
        assert (record.server_tool_search_calls, record.server_tool_fetch_calls) == (1, 0)
        assert_reservation_held(db, wallet_id, 1000)


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
async def test_paid_responses_connection_failure_is_not_retryable_transport(billing_context, stream):
    Session, wallet_id = billing_context
    def timeout(_request):
        raise httpx.ReadTimeout("unknown upstream")
    service = DeepSeekResponsesService(base_url="https://unit.example/v1", client_factory=lambda: httpx.AsyncClient(
        transport=httpx.MockTransport(timeout)))
    payload = {"model": "deepseek-flash", "max_output_tokens": 50}
    with billing_scope(session_factory=Session, user_id=22, personal_wallet_id=wallet_id,
                       max_input_tokens=100, max_output_tokens=50):
        with pytest.raises(BillingError, match="平台计费仅允许"):
            if stream:
                await service._open_stream(payload, {}, credential_fingerprint="x", input_items=[], should_store=False)
            else:
                await service._request_buffered(payload, {}, credential_fingerprint="x", input_items=[],
                                                should_store=False)
    with Session() as db:
        assert db.query(BillingReservation).count() == 0
        assert available_milli(db, wallet_id) == 1000


@pytest.mark.asyncio
async def test_native_paid_connection_failure_is_not_retryable_transport(billing_context, monkeypatch):
    Session, wallet_id = billing_context
    transport = NativeResponsesTransport(ApiConfig(api_key="test", base_url="https://api.deepseek.com/v1",
                                                   model="deepseek-flash"))
    async def timeout(_):
        raise httpx.ReadTimeout("unknown upstream")
        yield {}  # pragma: no cover
    monkeypatch.setattr(transport, "_stream_without_billing", timeout)
    with billing_scope(session_factory=Session, user_id=22, personal_wallet_id=wallet_id,
                       max_input_tokens=100, max_output_tokens=50):
        with pytest.raises(BillingError, match="未知"):
            _ = [item async for item in transport._stream({"max_output_tokens": 50})]
    with Session() as db:
        assert db.query(BillingReservation).one().status == "unknown"


@pytest.mark.asyncio
async def test_native_paid_transport_rejects_user_owned_credentials_before_upstream(billing_context, monkeypatch):
    Session, wallet_id = billing_context
    transport = NativeResponsesTransport(ApiConfig(
        api_key="customer-owned-secret",
        base_url="https://api.deepseek.com/v1",
        model="deepseek-v4-flash",
        provider="deepseek",
        source="user",
    ))
    sent = []

    async def upstream(_payload):
        sent.append(True)
        yield {"type": "response.completed", "response": {"usage": {
            "input_tokens": 10, "output_tokens": 5,
        }}}

    monkeypatch.setattr(transport, "_stream_without_billing", upstream)
    with billing_scope(session_factory=Session, user_id=22, personal_wallet_id=wallet_id,
                       max_input_tokens=512, max_output_tokens=50):
        with pytest.raises(BillingError, match="平台托管"):
            _ = [event async for event in transport._stream({
                "model": "deepseek-v4-flash", "input": "must not be sent", "max_output_tokens": 50,
            })]

    assert sent == []
    with Session() as db:
        assert db.query(BillingReservation).count() == 0
        assert available_milli(db, wallet_id) == 1000


@pytest.mark.asyncio
async def test_native_paid_transport_rejects_custom_provider_endpoint_before_upstream(billing_context, monkeypatch):
    Session, wallet_id = billing_context
    transport = NativeResponsesTransport(ApiConfig(
        api_key="platform-managed-key", base_url="https://proxy.example/v1",
        model="deepseek-v4-flash", provider="deepseek", source="global",
    ))
    sent = []

    async def upstream(_payload):
        sent.append(True)
        yield {"type": "response.completed", "response": {"usage": {
            "input_tokens": 10, "output_tokens": 5,
        }}}

    monkeypatch.setattr(transport, "_stream_without_billing", upstream)
    with billing_scope(session_factory=Session, user_id=22, personal_wallet_id=wallet_id,
                       max_input_tokens=512, max_output_tokens=50):
        with pytest.raises(BillingError, match="官方 HTTPS API 端点"):
            _ = [event async for event in transport._stream({
                "model": "deepseek-v4-flash", "input": "must not be sent", "max_output_tokens": 50,
            })]

    assert sent == []
    with Session() as db:
        assert db.query(BillingReservation).count() == 0
        assert available_milli(db, wallet_id) == 1000


@pytest.mark.asyncio
async def test_native_paid_transport_rejects_unsafe_user_config_fallback_before_upstream(
    billing_context, monkeypatch,
):
    Session, wallet_id = billing_context
    transport = NativeResponsesTransport(ApiConfig(
        api_key="platform-managed-key", base_url="https://api.deepseek.com/v1",
        model="deepseek-v4-flash", provider="deepseek", source="system", billing_eligible=False,
    ))
    sent = []

    async def upstream(_payload):
        sent.append(True)
        yield {"type": "response.completed", "response": {"usage": {
            "input_tokens": 10, "output_tokens": 5,
        }}}

    monkeypatch.setattr(transport, "_stream_without_billing", upstream)
    with billing_scope(session_factory=Session, user_id=22, personal_wallet_id=wallet_id,
                       max_input_tokens=512, max_output_tokens=50):
        with pytest.raises(BillingError, match="模型凭据来源未列入平台计费白名单"):
            _ = [event async for event in transport._stream({
                "model": "deepseek-v4-flash", "input": "must not be sent", "max_output_tokens": 50,
            })]

    assert sent == []
    with Session() as db:
        assert db.query(BillingReservation).count() == 0
        assert available_milli(db, wallet_id) == 1000


def test_billing_model_must_match_actual_payload_before_reservation(billing_context):
    Session, wallet_id = billing_context
    sent = []
    with billing_scope(session_factory=Session, user_id=22, personal_wallet_id=wallet_id,
                       max_input_tokens=512, max_output_tokens=50):
        with pytest.raises(BillingError, match="实际请求模型不一致"):
            metered_response_request(
                model_name="deepseek-v4-flash",
                payload={"model": "deepseek-v4-pro", "messages": [{"role": "user", "content": "price"}],
                         "max_tokens": 10},
                send=lambda: sent.append(True),
            )
    assert sent == []
    with Session() as db:
        assert db.query(BillingReservation).count() == 0
        assert available_milli(db, wallet_id) == 1000


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
async def test_responses_rejects_tool_before_projection_or_upstream(billing_context, monkeypatch, stream):
    Session, wallet_id = billing_context
    service = DeepSeekResponsesService(base_url="https://unit.example/v1")
    invoked = []
    async def project(*args, **kwargs):
        invoked.append(True)
        raise AssertionError("不应先压缩或发起网络调用")
    monkeypatch.setattr(service, "_prepare_upstream_payload", project)
    with billing_scope(session_factory=Session, user_id=22, personal_wallet_id=wallet_id,
                       max_input_tokens=100, max_output_tokens=50):
        with pytest.raises(BillingError, match="工具"):
            await service.create({"model": "deepseek-flash", "max_output_tokens": 50,
                                  "stream": stream, "tools": [{"type": "web_search"}]}, "Bearer test")
    assert invoked == []


@pytest.mark.asyncio
async def test_native_responses_rejects_tool_before_upstream(billing_context, monkeypatch):
    Session, wallet_id = billing_context
    transport = NativeResponsesTransport(ApiConfig(api_key="test", base_url="https://api.deepseek.com/v1",
                                                   model="deepseek-flash"))
    called = []
    async def send(_):
        called.append(True)
        yield {}
    monkeypatch.setattr(transport, "_stream_without_billing", send)
    with billing_scope(session_factory=Session, user_id=22, personal_wallet_id=wallet_id,
                       max_input_tokens=100, max_output_tokens=50):
        with pytest.raises(BillingError, match="工具"):
            _ = [item async for item in transport._stream({"max_output_tokens": 50,
                                                            "tools": [{"type": "web_search"}]})]
    assert called == []


@pytest.mark.asyncio
async def test_native_responses_freezes_reported_server_tool_usage(billing_context, monkeypatch):
    Session, wallet_id = billing_context
    transport = NativeResponsesTransport(ApiConfig(api_key="test", base_url="https://api.deepseek.com/v1",
                                                   model="deepseek-flash"))

    async def send(_):
        yield {"type": "response.completed", "response": {"usage": {
            "input_tokens": 10, "output_tokens": 5,
            "server_tool_use": {"tool_search": 0, "tool_fetch": 1},
        }}}

    monkeypatch.setattr(transport, "_stream_without_billing", send)
    with billing_scope(session_factory=Session, user_id=22, personal_wallet_id=wallet_id,
                       max_input_tokens=512, max_output_tokens=50):
        with pytest.raises(BillingError, match="工具"):
            _ = [event async for event in transport._stream({
                "model": "deepseek-flash", "input": "lookup", "max_output_tokens": 50,
            })]
    with Session() as db:
        record = db.query(BillingReservation).one()
        assert record.status == "unknown"
        assert (record.server_tool_search_calls, record.server_tool_fetch_calls) == (0, 1)
        assert_reservation_held(db, wallet_id, 1000)


@pytest.mark.asyncio
async def test_responses_api_returns_business_error_for_unpriced_tool(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.api import responses

    class FailingService:
        async def create(self, payload, authorization):
            raise BillingError("服务端工具尚未计价")
    monkeypatch.setattr(responses, "get_responses_service", lambda: FailingService())
    app = FastAPI()
    app.include_router(responses.router)
    with TestClient(app) as client:
        response = client.post("/v1/responses", json={"model": "deepseek-flash"})
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "billing_error"


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
async def test_actual_responses_route_blocks_tool_before_any_upstream(billing_context, monkeypatch, stream):
    from fastapi import FastAPI

    from app.api import responses

    Session, wallet_id = billing_context
    service = DeepSeekResponsesService(base_url="https://unit.example/v1")
    touched = []
    async def prepare(*args, **kwargs):
        touched.append(True)
        raise AssertionError("上游调用不得触发")
    monkeypatch.setattr(service, "_prepare_upstream_payload", prepare)
    monkeypatch.setattr(responses, "get_responses_service", lambda: service)
    app = FastAPI()
    app.include_router(responses.router)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        with billing_scope(session_factory=Session, user_id=22, personal_wallet_id=wallet_id,
                           max_input_tokens=100, max_output_tokens=50):
            response = await client.post("/v1/responses", headers={"Authorization": "Bearer test"}, json={
                "model": "deepseek-flash", "max_output_tokens": 50, "stream": stream,
                "tools": [{"type": "web_search"}], "max_tool_calls": 1,
            })
    assert response.status_code == 409 and response.json()["error"]["code"] == "billing_error"
    assert touched == []


@pytest.mark.asyncio
async def test_stream_reports_unknown_tool_usage_as_error_and_keeps_reservation_frozen(billing_context):
    Session, wallet_id = billing_context
    sent = []
    def send(_request):
        sent.append(True)
        return httpx.Response(200, json={"id": "r-billing"})
    service = DeepSeekResponsesService(base_url="https://unit.example/v1", client_factory=lambda: httpx.AsyncClient(
        transport=httpx.MockTransport(send)))
    with billing_scope(session_factory=Session, user_id=22, personal_wallet_id=wallet_id,
                       max_input_tokens=100, max_output_tokens=50):
        with pytest.raises(BillingError, match="平台计费仅允许"):
            await service._open_stream({"model": "deepseek-flash", "max_output_tokens": 50}, {},
                                       credential_fingerprint="x", input_items=[], should_store=False)
    assert sent == []
    with Session() as db:
        assert db.query(BillingReservation).count() == 0
        assert available_milli(db, wallet_id) == 1000


def test_deepseek_http_path_preholds_and_settles_when_scoped(billing_context, monkeypatch):
    Session, wallet_id = billing_context
    agent = DeepSeekAgent.__new__(DeepSeekAgent)
    agent.model = "deepseek-flash"
    calls = []
    def send(*args, **kwargs):
        calls.append(True)
        with Session() as db:
            assert_reservation_held(db, wallet_id, 1000)
        return httpx.Response(200, json={"usage": {"prompt_tokens": 80, "completion_tokens": 30}}), 5
    monkeypatch.setattr(agent, "_request_without_billing", send)
    with billing_scope(session_factory=Session, user_id=22, personal_wallet_id=wallet_id,
                       max_input_tokens=256, max_output_tokens=50):
        response, _ = agent._request_with_cancellation("https://example.com", {}, {
            "max_tokens": 50,
            "messages": [{"role": "user", "content": "请对输入材料完成详细审查并给出结果。"}],
        }, on_cancel=None)
    assert response.status_code == 200 and calls == [True]
    with Session() as db:
        assert available_milli(db, wallet_id) == 800
        assert db.query(BillingReservation).one().status == "settled"


def test_deepseek_scope_rejects_unbounded_output_before_request(billing_context, monkeypatch):
    Session, wallet_id = billing_context
    agent = DeepSeekAgent.__new__(DeepSeekAgent)
    agent.model = "deepseek-flash"
    calls = []
    monkeypatch.setattr(agent, "_request_without_billing", lambda *a, **kw: calls.append(True))
    with billing_scope(session_factory=Session, user_id=22, personal_wallet_id=wallet_id,
                       max_input_tokens=100, max_output_tokens=50):
        with pytest.raises(BillingError):
            agent._request_with_cancellation("https://example.com", {}, {"max_tokens": 51}, on_cancel=None)
    assert not calls


def test_existing_free_flow_remains_unchanged_without_scope(monkeypatch):
    from app.core.config import settings

    agent = DeepSeekAgent.__new__(DeepSeekAgent)
    agent.model = "deepseek-flash"
    monkeypatch.setattr(settings, "billing_allow_unmetered_external_calls", True)
    monkeypatch.setattr(agent, "_request_without_billing", lambda *a, **kw: ("free", 10))
    assert agent._request_with_cancellation("url", {}, {}, on_cancel=None) == ("free", 10)


def test_base_agent_preholds_before_request(billing_context, monkeypatch):
    from app.agents import base

    Session, wallet_id = billing_context
    original_client = httpx.Client
    def send(request):
        with Session() as db:
            assert_reservation_held(db, wallet_id, 1000)
        return httpx.Response(200, json={"model": "deepseek-flash", "choices": [{"finish_reason": "stop",
                              "message": {"content": "ok"}}],
                              "usage": {"prompt_tokens": 80, "completion_tokens": 30}})
    monkeypatch.setattr(base.httpx, "Client", lambda **kw: original_client(transport=httpx.MockTransport(send), **kw))
    monkeypatch.setattr(base, "pin_public_http_url", lambda url: SimpleNamespace(
        request_url=url, host_header="api.deepseek.com", request_extensions={}))
    agent = BaseAgent(system_prompt="unit", model="deepseek-flash")
    agent._base_url, agent._api_key, agent._max_retries = "https://api.deepseek.com/v1", "unit", 0
    agent._emit = lambda *_a, **_kw: None
    with billing_scope(session_factory=Session, user_id=22, personal_wallet_id=wallet_id,
                       max_input_tokens=512, max_output_tokens=50):
        result = agent.call("unit", max_tokens=50)
    assert result.success
    with Session() as db:
        assert available_milli(db, wallet_id) == 800


def test_chat_agent_preholds_before_request(billing_context, monkeypatch):
    from app.agents import chat_agent

    Session, wallet_id = billing_context
    with Session.begin() as db:
        grant(db, wallet_id=wallet_id, grant_key="chat-topup", kind="purchase", amount_milli=100000)
    original_client = httpx.Client
    def send(request):
        with Session() as db:
            assert_reservation_held(db, wallet_id, 101000)
        return httpx.Response(200, json={"model": "deepseek-flash", "choices": [{"finish_reason": "stop",
                              "message": {"content": "ok"}}],
                              "usage": {"prompt_tokens": 80, "completion_tokens": 30}})
    monkeypatch.setattr(httpx, "Client", lambda **kw: original_client(transport=httpx.MockTransport(send), **kw))
    monkeypatch.setattr(chat_agent, "pin_public_http_url", lambda url: SimpleNamespace(
        request_url=url, host_header="api.deepseek.com", request_extensions={}))
    agent = ChatAssistantAgent()
    agent._base_url, agent._api_key, agent._max_retries, agent._model = (
        "https://api.deepseek.com/v1", "unit", 0, "deepseek-flash")
    with billing_scope(session_factory=Session, user_id=22, personal_wallet_id=wallet_id,
                       max_input_tokens=10000, max_output_tokens=4096):
        result = agent._handle_chat([{"role": "user", "content": "hello"}], None)
    assert result.success
    with Session() as db:
        assert available_milli(db, wallet_id) == 100800


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
async def test_responses_gateway_rejects_byok_inside_paid_scope(billing_context, stream):
    Session, wallet_id = billing_context
    sent = []
    def send(request):
        sent.append(True)
        return httpx.Response(200, json={"id": "r1", "usage": {"input_tokens": 80, "output_tokens": 30}})
    service = DeepSeekResponsesService(base_url="https://unit.example/v1", client_factory=lambda: httpx.AsyncClient(
        transport=httpx.MockTransport(send)))
    payload = {"model": "deepseek-flash", "max_output_tokens": 50}
    with billing_scope(session_factory=Session, user_id=22, personal_wallet_id=wallet_id,
                       max_input_tokens=100, max_output_tokens=50):
        with pytest.raises(BillingError, match="平台计费仅允许"):
            if stream:
                await service._open_stream(payload, {}, credential_fingerprint="x", input_items=[],
                                           should_store=False)
            else:
                await service._request_buffered(payload, {}, credential_fingerprint="x", input_items=[],
                                                should_store=False)
    assert sent == []
    with Session() as db:
        assert db.query(BillingReservation).count() == 0
        assert available_milli(db, wallet_id) == 1000


@pytest.mark.asyncio
async def test_native_responses_transport_preholds_before_stream(billing_context, monkeypatch):
    from app.services import agent_responses_service

    Session, wallet_id = billing_context
    original_client = httpx.AsyncClient
    def send(request):
        with Session() as db:
            assert_reservation_held(db, wallet_id, 1000)
        response = {"id": "r2", "usage": {"input_tokens": 80, "output_tokens": 30}}
        data = "event: response.completed\ndata: " + json.dumps(
            {"type": "response.completed", "response": response}) + "\n\n"
        class TestStream(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield data.encode()
        return httpx.Response(200, stream=TestStream(), headers={"content-type": "text/event-stream"})
    monkeypatch.setattr(agent_responses_service.httpx, "AsyncClient",
                        lambda **kw: original_client(transport=httpx.MockTransport(send), **kw))
    transport = NativeResponsesTransport(ApiConfig(api_key="unit", base_url="https://api.deepseek.com/v1",
                                                   model="deepseek-flash"))
    with billing_scope(session_factory=Session, user_id=22, personal_wallet_id=wallet_id,
                       max_input_tokens=128, max_output_tokens=50):
        events = [event async for event in transport._stream({
            "model": "deepseek-flash", "input": "x" * 20, "max_output_tokens": 50,
        })]
    assert events[-1]["type"] == "response.completed"
    with Session() as db:
        assert available_milli(db, wallet_id) == 800
        reservation = db.query(BillingReservation).one()
        assert reservation.status == "settled"
        assert (reservation.provider_identity_version, reservation.provider_name,
                reservation.provider_endpoint_host, reservation.credential_source) == (
            "deepseek-official-v1", "deepseek", "api.deepseek.com", "system",
        )


@pytest.mark.asyncio
async def test_responses_compactor_preholds_before_request(billing_context, monkeypatch):
    Session, wallet_id = billing_context
    monkeypatch.setattr("app.services.deepseek_responses_service.settings.deepseek_context_window_tokens", 20000)
    with Session.begin() as db:
        grant(db, wallet_id=wallet_id, grant_key="compactor-topup", kind="purchase", amount_milli=100000)
    sent = []
    def send(request):
        sent.append(True)
        return httpx.Response(200, json={"id": "r", "status": "completed"})
    service = DeepSeekResponsesService(base_url="https://unit.example/v1", client_factory=lambda: httpx.AsyncClient(
        transport=httpx.MockTransport(send)))
    with billing_scope(session_factory=Session, user_id=22, personal_wallet_id=wallet_id,
                       max_input_tokens=10000, max_output_tokens=50):
        with pytest.raises(BillingError, match="平台计费仅允许"):
            await service._call_compactor(model="deepseek-flash", authorization="Bearer a",
                                          source=[{"source_id": "s1", "content": "原文"}],
                                          expected_ids=["s1"], source_texts={"s1": "原文"},
                                          source_digest="x", output_budget=50, calls=[0])
    assert sent == []
    with Session() as db:
        assert db.query(BillingReservation).count() == 0
        assert available_milli(db, wallet_id) == 101000


def test_unpriced_embedding_and_api_test_block_before_any_network(billing_context, monkeypatch):
    from app.services import api_config_service, embedding_service

    Session, wallet_id = billing_context

    # The gate validates the destination before checking billing so an unpaid
    # request cannot use the billing error to bypass SSRF validation. Keep DNS
    # deterministic here and assert the HTTP client is still never constructed.
    monkeypatch.setattr("app.utils.api_resolver._resolve_host", lambda _host: {"8.8.8.8"})
    monkeypatch.setattr(embedding_service, "pin_public_http_url", lambda url: SimpleNamespace(
        request_url=url, host_header="unit.example", request_extensions={},
    ))

    def unexpected(*args, **kwargs):
        raise AssertionError("network must not be touched")
    monkeypatch.setattr(httpx, "Client", unexpected)
    with billing_scope(session_factory=Session, user_id=22, personal_wallet_id=wallet_id,
                       max_input_tokens=100, max_output_tokens=50):
        with pytest.raises(BillingError, match="远端向量化"):
            embedding_service._api_embed(["private"], {"base_url": "https://unit.example", "api_key": "x"})
        with pytest.raises(BillingError, match="模型连接测试"):
            api_config_service._request_with_retries("POST", "https://unit.example", api_key="x",
                                                     timeout_seconds=1, max_retries=0)


def test_unscoped_model_connectivity_post_is_blocked_before_network_by_default(monkeypatch):
    from app.core.config import settings
    from app.services import api_config_service

    def unexpected(*args, **kwargs):
        raise AssertionError("network must not be touched")

    monkeypatch.setattr(settings, "billing_commercial_metering_ready", False)
    monkeypatch.setattr(settings, "billing_allow_unmetered_external_calls", False)
    monkeypatch.setattr(httpx, "Client", unexpected)
    with pytest.raises(BillingError, match="模型连接测试.*未计量"):
        api_config_service._request_with_retries(
            "POST", "https://unit.example", api_key="x", timeout_seconds=1, max_retries=0,
        )


def test_unscoped_embedding_fails_closed_when_commercial_metering_is_ready(monkeypatch):
    from app.core.config import settings
    from app.services import embedding_service

    def unexpected(*args, **kwargs):
        raise AssertionError("network must not be touched")

    monkeypatch.setattr(settings, "billing_commercial_metering_ready", True)
    monkeypatch.setattr("app.utils.api_resolver._resolve_host", lambda _host: {"8.8.8.8"})
    monkeypatch.setattr(embedding_service, "pin_public_http_url", lambda url: SimpleNamespace(
        request_url=url, host_header="unit.example", request_extensions={},
    ))
    monkeypatch.setattr(httpx, "Client", unexpected)
    with pytest.raises(BillingError, match="未进入可信计费作用域"):
        embedding_service._api_embed(
            ["private"], {"base_url": "https://unit.example", "api_key": "x", "model": "embedding"}
        )


def test_unscoped_embedding_fails_closed_in_legacy_mode_by_default(monkeypatch):
    from app.core.config import settings
    from app.services import embedding_service

    def unexpected(*args, **kwargs):
        raise AssertionError("network must not be touched")

    monkeypatch.setattr(settings, "billing_commercial_metering_ready", False)
    monkeypatch.setattr(settings, "billing_allow_unmetered_external_calls", False)
    monkeypatch.setattr("app.utils.api_resolver._resolve_host", lambda _host: {"8.8.8.8"})
    monkeypatch.setattr(embedding_service, "pin_public_http_url", lambda url: SimpleNamespace(
        request_url=url, host_header="unit.example", request_extensions={},
    ))
    monkeypatch.setattr(httpx, "Client", unexpected)
    with pytest.raises(BillingError, match="远端向量化.*未计量"):
        embedding_service._api_embed(
            ["private"], {"base_url": "https://unit.example", "api_key": "x", "model": "embedding"}
        )
