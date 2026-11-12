from __future__ import annotations

from tempfile import SpooledTemporaryFile
from time import time
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from starlette.datastructures import FormData, Headers, UploadFile

from app.api.v1 import code_files
from app.core.exceptions import ValidationError


@pytest.fixture
def folder_inputs():
    engine = create_engine('sqlite://')
    db = SimpleNamespace(get_bind=lambda: engine)
    user = SimpleNamespace(id=1, _authenticated_token_version=0, _authenticated_token_expires_at=int(time()) + 60)
    yield db, user
    engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("project_id", "field_name", "message"),
    [
        ("not-an-integer", "files", "project_id 必须是整数"),
        ("0", "files", "project_id 必须大于 0"),
        ("7", "wrong_files", "至少上传一个文件"),
    ],
)
async def test_folder_upload_closes_parsed_temp_files_on_validation_error(
    monkeypatch,
    project_id: str,
    field_name: str,
    message: str,
    folder_inputs,
):
    temporary_file = SpooledTemporaryFile()
    upload = UploadFile(temporary_file, filename="source.py")
    form = FormData([("project_id", project_id), (field_name, upload)])

    async def fake_parse(*args, **kwargs):
        return form

    monkeypatch.setattr(code_files, "parse_limited_multipart", fake_parse)
    request = SimpleNamespace(headers=Headers(), stream=lambda: None)

    with pytest.raises(ValidationError, match=message):
        await code_files.upload_folder(request, db=folder_inputs[0], user=folder_inputs[1])

    assert temporary_file.closed is True


@pytest.mark.asyncio
@pytest.mark.parametrize('complete', [False, True])
async def test_folder_upload_never_partially_commits_truncated_multipart(monkeypatch, complete, folder_inputs):
    calls = []
    def upload_items(_db, _user, project_id, files, **_kwargs):
        calls.append((project_id, [item.file.read() for item in files]))
        return {'success': len(files), 'failed': 0, 'files': [], 'errors': []}
    monkeypatch.setattr(code_files, '_upload_folder_items', upload_items)
    body = (b'--abc\r\nContent-Disposition: form-data; name="project_id"\r\n\r\n7\r\n'
            b'--abc\r\nContent-Disposition: form-data; name="files"; filename="a.py"\r\n\r\na\r\n'
            b'--abc\r\nContent-Disposition: form-data; name="files"; filename="b.py"\r\n\r\nb\r\n')
    if complete:
        body += b'--abc--\r\n'
    async def stream():
        yield body
    request = SimpleNamespace(headers=Headers({'content-type': 'multipart/form-data; boundary=abc'}), stream=stream)
    if complete:
        await code_files.upload_folder(request, db=folder_inputs[0], user=folder_inputs[1])
        assert calls == [(7, [b'a', b'b'])]
    else:
        with pytest.raises(ValidationError, match='未完整结束'):
            await code_files.upload_folder(request, db=folder_inputs[0], user=folder_inputs[1])
        assert calls == []
