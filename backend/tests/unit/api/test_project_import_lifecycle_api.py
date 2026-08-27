"""远程导入取消 API 与同步兼容接口退役契约。"""

from fastapi import Response

from app.api.v1 import projects as projects_api
from app.models.user import User
from app.schemas.project import RemoteProjectImportCancelIn, RemoteProjectImportIn


def _user(db, username: str) -> User:
    row = User(username=username, password="x", role="user", status=1)
    db.add(row)
    db.commit()
    return row


def test_legacy_sync_import_sets_deprecation_headers_and_metric(db, monkeypatch) -> None:
    user = _user(db, "legacy-import-owner")
    observed: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        projects_api.project_source_service,
        "import_remote_project",
        lambda *_args, **_kwargs: {"id": 7, "file_count": 2},
    )
    monkeypatch.setattr(
        projects_api,
        "observe_event",
        lambda category, *, labels=None, **_kwargs: observed.append((category, labels or {})),
    )
    response = Response()

    result = projects_api.import_remote_project(
        RemoteProjectImportIn(
            url="https://example.com/source.zip",
            project_name="legacy-project",
        ),
        response,
        db,
        user,
    )

    assert result.data == {"id": 7, "file_count": 2}
    assert response.headers["Deprecation"] == "true"
    assert response.headers["Sunset"]
    assert 'rel="successor-version"' in response.headers["Link"]
    assert observed == [
        ("project_remote_import_legacy_sync_called", {"role": "user", "surface": "api"})
    ]


def test_cancel_route_returns_service_state(db, monkeypatch) -> None:
    user = _user(db, "cancel-route-owner")
    expected = {
        "task_id": "task-1",
        "status": "cancelled",
        "attempt_count": 1,
        "max_attempts": 3,
        "project_id": None,
        "result": {},
        "error": {"code": "cancelled", "message": "用户取消"},
        "cancel_reason": "用户取消",
        "next_attempt_at": None,
        "started_at": None,
        "completed_at": None,
        "create_time": user.create_time,
        "update_time": user.update_time,
    }
    monkeypatch.setattr(
        projects_api.project_import_service,
        "cancel_import_task",
        lambda *_args, **_kwargs: expected,
    )

    result = projects_api.cancel_remote_project_import(
        "task-1",
        RemoteProjectImportCancelIn(reason="用户取消"),
        db,
        user,
    )

    assert result.data.status == "cancelled"
    assert result.data.cancel_reason == "用户取消"
