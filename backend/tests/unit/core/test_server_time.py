"""服务端时间口径助手：naive 一律按 UTC 解释，对外输出带 Z，导出显示北京时间并标注。"""
from datetime import date, datetime, timedelta, timezone

import pytest
from pydantic import BaseModel

from app.core.server_time import (
    BEIJING_LABEL,
    format_beijing,
    iso_beijing,
    iso_utc,
    json_default,
    parse_server_time,
    tag_naive_utc,
)

NAIVE = datetime(2026, 10, 22, 8, 20, 0)
UTC_INSTANT = datetime(2026, 10, 22, 8, 20, 0, tzinfo=timezone.utc)


def test_iso_utc_marks_naive_datetime_as_utc_without_changing_the_wall_clock():
    """任务 #231 的库内值 08:20（UTC）输出后仍是 08:20，只是明确标成 UTC。"""
    assert iso_utc(NAIVE) == "2026-10-22T08:20:00Z"


def test_iso_utc_round_trip_keeps_value_and_microseconds():
    original = datetime(2026, 10, 22, 8, 20, 0, 123456)
    text = iso_utc(original)
    assert text == "2026-10-22T08:20:00.123456Z"
    restored = datetime.fromisoformat(text.replace("Z", "+00:00"))
    assert restored.replace(tzinfo=None) == original
    assert restored.utcoffset() == timedelta(0)


def test_iso_utc_converts_other_offsets_to_the_same_instant_in_utc():
    beijing = datetime(2026, 10, 22, 16, 20, 0, tzinfo=timezone(timedelta(hours=8)))
    assert iso_utc(beijing) == "2026-10-22T08:20:00Z"


def test_iso_utc_is_idempotent_for_already_tagged_values():
    assert iso_utc(UTC_INSTANT) == "2026-10-22T08:20:00Z"
    assert iso_utc("2026-10-22T08:20:00Z") == "2026-10-22T08:20:00Z"
    assert iso_utc(iso_utc(NAIVE)) == "2026-10-22T08:20:00Z"


def test_iso_utc_passes_none_and_unparseable_text_through_unchanged():
    assert iso_utc(None) is None
    assert iso_utc("not-a-time") == "not-a-time"


@pytest.mark.parametrize(
    "raw",
    [
        "2026-10-22T08:20:00",
        "2026-10-22T08:20:00Z",
        "2026-10-22T08:20:00+00:00",
        "2026-10-22 08:20:00",
        "2026-10-22T16:20:00+08:00",
        NAIVE,
        UTC_INSTANT,
    ],
)
def test_parse_server_time_reads_every_historical_shape_as_the_same_utc_instant(raw):
    parsed = parse_server_time(raw)
    assert parsed == UTC_INSTANT
    assert parsed.utcoffset() == timedelta(0)


def test_parse_server_time_returns_none_for_empty_or_invalid_input():
    assert parse_server_time(None) is None
    assert parse_server_time("") is None
    assert parse_server_time("昨天") is None


def test_format_beijing_shows_wall_clock_plus_eight_with_explicit_label():
    assert BEIJING_LABEL == "（UTC+8）"
    assert format_beijing(NAIVE) == "2026-10-22 16:20:00（UTC+8）"
    assert format_beijing("2026-10-22T08:20:00Z") == "2026-10-22 16:20:00（UTC+8）"
    assert format_beijing("2026-10-22 08:20:00") == "2026-10-22 16:20:00（UTC+8）"
    assert format_beijing(NAIVE, label=False) == "2026-10-22 16:20:00"


def test_format_beijing_crosses_the_date_boundary_and_handles_blank_values():
    assert format_beijing(datetime(2026, 10, 22, 20, 30, 5)) == "2026-10-23 04:30:05（UTC+8）"
    assert format_beijing(None) == "-"
    assert format_beijing("") == "-"
    assert format_beijing("未记录") == "未记录"


def test_iso_beijing_is_machine_readable_with_explicit_offset():
    assert iso_beijing(NAIVE) == "2026-10-22T16:20:00+08:00"
    assert iso_beijing("2026-10-22T08:20:00Z") == "2026-10-22T16:20:00+08:00"
    assert iso_beijing(None) is None


class _Row(BaseModel):
    created: datetime
    note: str = "2026-10-22T08:20:00"


def test_tag_naive_utc_walks_dicts_lists_and_models_but_never_touches_text_or_dates():
    payload = {
        "naive": NAIVE,
        "aware": UTC_INSTANT,
        "day": date(2026, 10, 22),
        "text": "2026-10-22T08:20:00",
        "nested": [{"at": NAIVE}, (NAIVE,)],
        "model": _Row(created=NAIVE),
    }
    tagged = tag_naive_utc(payload)
    assert tagged["naive"] == UTC_INSTANT and tagged["naive"].tzinfo is not None
    assert tagged["aware"] is UTC_INSTANT
    assert tagged["day"] == date(2026, 10, 22) and not isinstance(tagged["day"], datetime)
    assert tagged["text"] == "2026-10-22T08:20:00"
    assert tagged["nested"][0]["at"].tzinfo is not None
    assert tagged["nested"][1][0].tzinfo is not None
    assert tagged["model"].created.tzinfo is not None
    assert tagged["model"].note == "2026-10-22T08:20:00"
    # 原对象不被改写：调用方的 ORM/缓存字典仍是 naive。
    assert payload["naive"].tzinfo is None
    assert payload["model"].created.tzinfo is None


def test_tag_naive_utc_returns_the_same_object_when_nothing_needs_tagging():
    payload = {"a": 1, "b": ["x", {"c": UTC_INSTANT}]}
    assert tag_naive_utc(payload) is payload


def test_json_default_formats_datetimes_as_utc_and_falls_back_to_text():
    assert json_default(NAIVE) == "2026-10-22T08:20:00Z"
    assert json_default(UTC_INSTANT) == "2026-10-22T08:20:00Z"
    assert json_default(date(2026, 10, 22)) == "2026-10-22"
    assert json_default(object).startswith("<class 'object'")
