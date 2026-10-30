"""接口时间口径契约：库内 naive UTC 时间在所有出口都带 Z，同一任务各页面拿到同一时刻。

线上事故：审查任务 #231 在任务列表/报告详情显示 16:20，在报告列表/项目详情“最近任务”显示 08:20。
库内时间不带时区（存的是 UTC），接口原样输出不带 Z 的 ISO 字符串，浏览器按本地时区解析，
各页面又各自补 Z 或不补，于是差 8 小时。本测试固定“接口一律带 UTC 标记”这条后端契约。
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any, Generic, Optional, TypeVar

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import BaseModel
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.database import Base, get_db
from app.core.dependencies import get_current_user
from app.main import app
from app.models.pentest import PentestEngagement
from app.models.project import Project
from app.models.remote_pentest import RemotePentestEngagement
from app.models.review_issue import ReviewIssue
from app.models.review_task import ReviewTask
from app.models.user import User
from app.schemas.common import PageOut, Resp

# 任务 #231 的库内时间：naive，数值是 UTC。北京时间应显示 2026-10-22 16:20。
STORED = datetime(2026, 10, 22, 8, 20, 0)
EXPECTED = "2026-10-22T08:20:00Z"
TASK_ID = 231
NAIVE_ISO = re.compile(r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(\.\d+)?$")


def _walk_strings(value: Any, path: str = ""):
    """遍历响应 JSON，产出 (路径, 字符串)；用于扫描遗漏的 naive 时间文本。"""
    if isinstance(value, dict):
        for key, item in value.items():
            yield from _walk_strings(item, f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from _walk_strings(item, f"{path}[{index}]")
    elif isinstance(value, str):
        yield path, value


@pytest.fixture
def api():
    """真实 app + 内存 SQLite；管理员身份；播种任务 #231 及渗透委托。"""
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)()
    admin = User(id=1, username="admin", password="x", email="a@b.c", nickname="管理员", role="admin", status=1)
    session.add(admin)
    session.add(Project(
        id=1, user_id=1, project_name="时间口径项目", status="active",
        create_time=STORED, update_time=STORED,
    ))
    session.add(ReviewTask(
        id=TASK_ID, user_id=1, project_id=1, task_name="任务231", review_type="security",
        status="success", total_files=1, processed_files=1, total_issues=1, severe_issues=0,
        high_issues=1, medium_issues=0, low_issues=0, score=80, summary="完成",
        model_name="deepseek", duration_ms=1000,
        start_time=STORED, end_time=STORED, create_time=STORED, update_time=STORED,
        coverage={"coverage_status": "complete", "verification_status": "complete", "score_available": True},
    ))
    session.add(ReviewIssue(
        task_id=TASK_ID, file_id=1, file_name="a.py", line_number=1, issue_type="安全漏洞", severity="高",
        title="问题", description="d", suggestion="s", status="unfixed",
        create_time=STORED, update_time=STORED,
    ))
    session.add(PentestEngagement(
        public_id="pt_time_1", user_id=1, project_id=1, target_type="web", status="authorized",
        rules_version="pentest-rules-v2", rules_ack=1, authorized_by_user_id=1,
        authorized_at=STORED, window_start=STORED, window_end=STORED + timedelta(hours=4),
        started_at=STORED, completed_at=None, create_time=STORED, update_time=STORED,
    ))
    session.add(RemotePentestEngagement(
        public_id="rp_time_1", owner_id=1, task_name="远程", target_url="https://example.test",
        target_host="example.test", authorization_basis="书面授权", scope_notes="", scope_digest="0" * 64,
        rules_version="remote-rules-v1", rules_ack=1, authorized_by_user_id=1,
        authorized_at=STORED, window_start=STORED, window_end=STORED + timedelta(hours=4),
        status="authorized", create_time=STORED, update_time=STORED,
    ))
    session.commit()

    def override_db():
        yield session

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_current_user] = lambda: admin
    try:
        yield TestClient(app), session
    finally:
        app.dependency_overrides.pop(get_db, None)
        app.dependency_overrides.pop(get_current_user, None)
        session.close()
        engine.dispose()


def _data(client: TestClient, url: str) -> Any:
    response = client.get(url)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["code"] == 0, body
    return body["data"]


def _task_item(items: list[dict], key: str) -> dict:
    return next(item for item in items if item[key] == TASK_ID)


# 同一个任务在各页面读到的时间：(接口, 取值函数)
TASK_TIME_SOURCES = [
    ("审查任务列表", f"/api/review/tasks", lambda d: _task_item(d["items"], "id")["create_time"]),
    ("审查任务详情-创建", f"/api/review/tasks/{TASK_ID}", lambda d: d["create_time"]),
    ("审查任务详情-开始", f"/api/review/tasks/{TASK_ID}", lambda d: d["start_time"]),
    ("审查任务详情-结束", f"/api/review/tasks/{TASK_ID}", lambda d: d["end_time"]),
    ("报告列表", "/api/reports", lambda d: _task_item(d["items"], "task_id")["create_time"]),
    ("报告详情", f"/api/reports/{TASK_ID}", lambda d: d["task"]["create_time"]),
    ("项目详情-最近任务", "/api/projects/1", lambda d: d["recent_tasks"][0]["create_time"]),
    ("项目详情-创建时间", "/api/projects/1", lambda d: d["create_time"]),
    ("仪表盘-最近任务", "/api/dashboard/summary", lambda d: d["recent_tasks"][0]["create_time"]),
    ("仪表盘-评分趋势", "/api/dashboard/score-trend", lambda d: d[0]["create_time"]),
    ("问题列表", f"/api/review/tasks/{TASK_ID}/issues", lambda d: d["items"][0]["create_time"]),
]


@pytest.mark.parametrize(("label", "url", "pick"), TASK_TIME_SOURCES, ids=[s[0] for s in TASK_TIME_SOURCES])
def test_same_task_time_is_utc_tagged_on_every_page_source(api, label, url, pick):
    client, _ = api
    assert pick(_data(client, url)) == EXPECTED, f"{label} 必须输出带 Z 的 UTC 时间"


def test_pentest_dict_payload_with_raw_datetimes_is_utc_tagged(api):
    """渗透列表/详情是 Resp[dict]，服务层直接放 datetime 对象，也必须带 UTC 标记。"""
    client, _ = api
    listing = _data(client, "/api/pentest/engagements")
    row = next(item for item in listing["items"] if item["public_id"] == "pt_time_1")
    for field in ("create_time", "authorized_at", "window_start", "started_at"):
        assert row[field] == EXPECTED, field
    assert row["window_end"] == "2026-10-22T12:20:00Z"
    detail = _data(client, "/api/pentest/engagements/pt_time_1")
    assert detail["window_start"] == EXPECTED and detail["create_time"] == EXPECTED


def test_remote_pentest_list_and_detail_are_utc_tagged(api):
    client, _ = api
    listing = _data(client, "/api/remote-pentest/tasks")
    row = next(item for item in listing["items"] if item["public_id"] == "rp_time_1")
    assert row["create_time"] == EXPECTED and row["window_start"] == EXPECTED
    detail = _data(client, "/api/remote-pentest/tasks/rp_time_1")
    assert detail["create_time"] == EXPECTED and detail["window_end"] == "2026-10-22T12:20:00Z"


@pytest.mark.parametrize(
    "url",
    [
        "/api/review/tasks",
        f"/api/review/tasks/{TASK_ID}",
        "/api/reports",
        f"/api/reports/{TASK_ID}",
        "/api/projects/1",
        "/api/dashboard/summary",
        "/api/pentest/engagements",
        "/api/remote-pentest/tasks",
    ],
)
def test_no_response_leaks_a_naive_iso_timestamp_string(api, url):
    """兜底扫描：整份响应里不允许出现不带时区的 ISO 时间文本。"""
    client, _ = api
    leaked = [(path, text) for path, text in _walk_strings(_data(client, url)) if NAIVE_ISO.match(text)]
    assert leaked == []


def test_wire_value_round_trips_to_the_stored_instant(api):
    client, _ = api
    wire = _data(client, f"/api/review/tasks/{TASK_ID}")["create_time"]
    parsed = datetime.fromisoformat(wire.replace("Z", "+00:00"))
    assert parsed.replace(tzinfo=None) == STORED
    assert parsed.astimezone(timezone(timedelta(hours=8))).strftime("%H:%M") == "16:20"


# ---------------------------------------------------------------------------
# Resp 信封本身：任意 Resp[...] 载荷里的 naive datetime 都被标成 UTC
# ---------------------------------------------------------------------------

T = TypeVar("T")


class _Typed(BaseModel):
    at: datetime
    maybe: Optional[datetime] = None
    label: str = "2026-10-22T08:20:00"


def _mini_app() -> TestClient:
    mini = FastAPI()

    @mini.get("/typed", response_model=Resp[_Typed])
    def typed():
        return Resp(data=_Typed(at=STORED))

    @mini.get("/page", response_model=Resp[PageOut[_Typed]])
    def page():
        return Resp(data=PageOut(items=[_Typed(at=STORED)], total=1, page=1, page_size=20, pages=1))

    @mini.get("/dict", response_model=Resp[dict])
    def as_dict():
        return Resp(data={"at": STORED, "nested": {"rows": [{"at": STORED}]}, "day": STORED.date()})

    @mini.get("/list", response_model=Resp[list[dict]])
    def as_list():
        return Resp(data=[{"at": STORED}, {"at": None}])

    return TestClient(mini)


def test_resp_envelope_tags_typed_models_pages_dicts_and_lists():
    client = _mini_app()
    typed = client.get("/typed").json()["data"]
    assert typed["at"] == EXPECTED and typed["maybe"] is None
    assert typed["label"] == "2026-10-22T08:20:00", "普通字符串字段不能被误改"
    assert client.get("/page").json()["data"]["items"][0]["at"] == EXPECTED
    plain = client.get("/dict").json()["data"]
    assert plain["at"] == EXPECTED and plain["nested"]["rows"][0]["at"] == EXPECTED
    assert plain["day"] == "2026-10-22", "纯日期不是时刻，不加 Z"
    assert [row["at"] for row in client.get("/list").json()["data"]] == [EXPECTED, None]


def test_resp_envelope_leaves_python_objects_untouched_and_normalizes_other_offsets():
    payload = Resp(data={"at": STORED})
    assert payload.data["at"].tzinfo is None, "只改 JSON 输出，不改调用方手里的对象"
    beijing = datetime(2026, 10, 22, 16, 20, tzinfo=timezone(timedelta(hours=8)))
    assert Resp(data={"at": beijing}).model_dump(mode="json")["data"]["at"] == EXPECTED


class _Box(BaseModel, Generic[T]):
    value: T


def test_openapi_contract_keeps_datetime_fields_as_date_time_strings():
    """不改契约形状：时间字段在 OpenAPI 里仍是 string/date-time。"""
    schema = app.openapi()["components"]["schemas"]
    for model, field in (("TaskOut", "create_time"), ("ReportListItem", "create_time"), ("ProjectDetailOut", "create_time")):
        prop = schema[model]["properties"][field]
        assert prop.get("format") == "date-time" or any(
            item.get("format") == "date-time" for item in prop.get("anyOf", [])
        ), (model, field, prop)
