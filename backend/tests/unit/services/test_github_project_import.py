"""GitHub 公开仓库页面网址导入的单元测试。

覆盖 URL 规范化、默认分支候选、main→master 404 回退，以及导入结果保留用户原始网址。
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from app.core.exceptions import ExternalServiceError, ValidationError
from app.models.project import Project
from app.models.user import User
from app.services import project_source_service

MAIN_ARCHIVE = (
    "https://codeload.github.com/octocat/hello-world/tar.gz/refs/heads/main"
)
MASTER_ARCHIVE = (
    "https://codeload.github.com/octocat/hello-world/tar.gz/refs/heads/master"
)


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://github.com/octocat/hello-world", ("octocat", "hello-world", None)),
        ("https://github.com/octocat/hello-world/", ("octocat", "hello-world", None)),
        ("https://GITHUB.COM/octocat/hello-world", ("octocat", "hello-world", None)),
        ("https://www.github.com/octocat/hello-world", ("octocat", "hello-world", None)),
        (
            "https://github.com/octocat/hello-world/tree/main",
            ("octocat", "hello-world", "main"),
        ),
        (
            "https://github.com/octocat/hello-world/tree/feature/ci",
            ("octocat", "hello-world", "feature/ci"),
        ),
    ],
)
def test_normalize_github_project_url_accepts_valid_pages(url, expected):
    assert project_source_service._normalize_github_project_url(url) == expected


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com/octocat/hello-world/blob/main/README.md",
        "https://github.com/octocat/hello-world/raw/main/README.md",
        "https://github.com/octocat/hello-world/releases",
        "https://github.com/octocat/hello-world/releases/tag/v1.0.0",
        "https://github.com/octocat/hello-world/issues/1",
        "https://github.com/octocat/hello-world/pull/1",
        "https://github.com/octocat/hello-world/actions",
        "https://github.com/octocat/hello-world/commit/abcdef",
        "https://github.com/octocat/hello-world/wiki",
        "https://github.com/octocat/hello-world/settings",
        "https://github.com/octocat/hello-world.git",
        "https://github.com/octocat/hello-world.GIT",
        "https://user:secret@github.com/octocat/hello-world",
        "https://github.com/octocat/hello-world?token=secret",
        "https://github.com/octocat/hello-world#access_token=secret",
        "https://github.com:444/octocat/hello-world",
        "https://github.com.evil.com/octocat/hello-world",
        "http://github.com/octocat/hello-world",
        "https://example.com/octocat/hello-world",
        "https://github.com@evil.com/octocat/hello-world",
        "https://github.com/octocat/hello-world/tree",
        "https://github.com/octocat",
        "https://github.com/octocat/hello-world/tree/../../etc",
        "https://github.com/octocat/hello-world/tree/.",
    ],
)
def test_normalize_github_project_url_rejects_unsafe_urls(url):
    with pytest.raises(ValidationError):
        project_source_service._normalize_github_project_url(url)


def test_github_archive_candidates_use_main_then_master_by_default():
    assert project_source_service._github_archive_candidates(
        "https://github.com/octocat/hello-world",
    ) == [MAIN_ARCHIVE, MASTER_ARCHIVE]


def test_github_archive_candidates_use_only_explicit_branch():
    assert project_source_service._github_archive_candidates(
        "https://github.com/octocat/hello-world/tree/feature/ci",
    ) == [
        "https://codeload.github.com/octocat/hello-world/tar.gz/refs/heads/feature/ci"
    ]


def _mock_import_pipeline(monkeypatch, download):
    """把下载、建项目与归档入库替换为内存桩，保留导入入口的编排逻辑。"""
    monkeypatch.setattr(project_source_service, "download_remote_archive", download)

    created: list = []
    project = Project(id=77, user_id=1, project_name="hello-world", status="active")

    def create_project(db, user, payload):
        created.append(payload)
        return project

    monkeypatch.setattr(project_source_service.project_service, "create_project", create_project)

    uploaded: list = []

    def upload_archive(db, user, project_id, raw, archive_name):
        uploaded.append((project_id, raw, archive_name))
        return 33, "unused", 1

    monkeypatch.setattr(
        project_source_service.code_file_service,
        "_upload_archive",
        upload_archive,
    )

    db = MagicMock()
    db.query.return_value.filter.return_value.count.return_value = 5
    return db, created, uploaded


def test_import_github_project_retries_master_after_main_404(monkeypatch):
    calls: list = []

    def download(url):
        calls.append(url)
        if url.endswith("/refs/heads/main"):
            raise project_source_service.RemoteArchiveNotFoundError(
                "远程源码归档不存在(HTTP 404)",
                code=50201,
            )
        return b"PK00", "hello-world.tar.gz"

    db, created, uploaded = _mock_import_pipeline(monkeypatch, download)
    user = User(id=1, role="user", username="octocat", status=1)

    result = project_source_service.import_remote_project(
        db,
        user,
        url="https://github.com/octocat/hello-world",
        project_name="hello-world",
    )

    assert calls == [MAIN_ARCHIVE, MASTER_ARCHIVE]
    assert result["source_url"] == "https://github.com/octocat/hello-world"
    assert result["id"] == 77
    assert result["file_count"] == 5
    assert result["first_file_id"] == 33
    assert created[0].project_name == "hello-world"
    assert uploaded == [(77, b"PK00", "hello-world.tar.gz")]


def test_import_github_project_does_not_retry_when_main_succeeds(monkeypatch):
    calls: list = []

    def download(url):
        calls.append(url)
        return b"PK00", "hello-world.tar.gz"

    db, created, uploaded = _mock_import_pipeline(monkeypatch, download)
    user = User(id=1, role="user", username="octocat", status=1)

    result = project_source_service.import_remote_project(
        db,
        user,
        url="https://github.com/octocat/hello-world",
        project_name="hello-world",
    )

    assert calls == [MAIN_ARCHIVE]
    assert result["source_url"] == "https://github.com/octocat/hello-world"
    assert result["file_count"] == 5


def test_import_github_project_stops_on_non_404_error(monkeypatch):
    calls: list = []

    def download(url):
        calls.append(url)
        raise ExternalServiceError("远程源码下载失败(HTTP 403)", code=50201)

    monkeypatch.setattr(project_source_service, "download_remote_archive", download)
    user = User(id=1, role="user", username="octocat", status=1)

    with pytest.raises(ExternalServiceError, match="HTTP 403"):
        project_source_service.import_remote_project(
            MagicMock(),
            user,
            url="https://github.com/octocat/hello-world",
            project_name="hello-world",
        )

    assert calls == [MAIN_ARCHIVE]


def test_import_remote_project_keeps_direct_archive_behavior(monkeypatch):
    calls: list = []

    def download(url):
        calls.append(url)
        return b"PK00", "source.zip"

    db, created, uploaded = _mock_import_pipeline(monkeypatch, download)
    user = User(id=1, role="user", username="octocat", status=1)

    result = project_source_service.import_remote_project(
        db,
        user,
        url="https://example.com/source.zip",
        project_name="remote-source",
    )

    assert calls == ["https://example.com/source.zip"]
    assert result["source_url"] == "https://example.com/source.zip"
    assert uploaded == [(77, b"PK00", "source.zip")]
