from __future__ import annotations

from tempfile import SpooledTemporaryFile
from types import SimpleNamespace

import pytest
from starlette.datastructures import FormData, Headers, UploadFile

from app.api.v1 import code_files
from app.core.exceptions import ValidationError


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
):
    temporary_file = SpooledTemporaryFile()
    upload = UploadFile(temporary_file, filename="source.py")
    form = FormData([("project_id", project_id), (field_name, upload)])

    async def fake_parse(*args, **kwargs):
        return form

    monkeypatch.setattr(code_files, "parse_limited_multipart", fake_parse)
    request = SimpleNamespace(headers=Headers(), stream=lambda: None)

    with pytest.raises(ValidationError, match=message):
        await code_files.upload_folder(request, db=object(), user=object())

    assert temporary_file.closed is True
