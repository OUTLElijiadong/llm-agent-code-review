"""在解析前限制上传与普通 JSON 请求体大小。"""
from __future__ import annotations

import re
from typing import Final

from starlette.datastructures import Headers
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core.exceptions import PayloadTooLargeError

MULTIPART_OVERHEAD_BYTES: Final = 1024 * 1024
ENTERPRISE_KNOWLEDGE_ATTACHMENT_MAX_BYTES: Final = 8 * 1024 * 1024
STATEMENT_FILE_MAX_BYTES: Final = 12 * 1024 * 1024
STATEMENT_IMPORT_REQUEST_MAX_BYTES: Final = STATEMENT_FILE_MAX_BYTES + MULTIPART_OVERHEAD_BYTES
_SOURCE_UPLOAD_PATH: Final = "/api/code-files/upload"
_FOLDER_UPLOAD_PATH: Final = "/api/code-files/upload-folder"
_AVATAR_UPLOAD_PATH: Final = "/api/me/avatar/image"
_STATEMENT_IMPORT_PATH: Final = "/api/billing/admin/statements/import"
_FUND_STATEMENT_IMPORT_PATH: Final = "/api/billing/admin/fund-statements/import"
_AUDIT_ARCHIVE_PATH: Final = re.compile(r"^/api/projects/[1-9][0-9]*/audit-source-archive$")
_ENTERPRISE_KNOWLEDGE_ATTACHMENT_PATH: Final = re.compile(
    r"^/api/enterprises/[1-9][0-9]*/knowledge/attachments$"
)
JSON_REQUEST_METHODS: Final = frozenset({"POST", "PUT", "PATCH"})


def is_limited_upload_path(path: str) -> bool:
    """返回路径是否属于必须在 multipart 解析前限流的源码上传入口。"""
    return path in {_SOURCE_UPLOAD_PATH, _FOLDER_UPLOAD_PATH, _AVATAR_UPLOAD_PATH, _STATEMENT_IMPORT_PATH,
                    _FUND_STATEMENT_IMPORT_PATH} or bool(
        _AUDIT_ARCHIVE_PATH.fullmatch(path) or _ENTERPRISE_KNOWLEDGE_ATTACHMENT_PATH.fullmatch(path)
    )


class UploadRequestSizeLimitMiddleware:
    """按请求总大小提前拒绝源码上传，避免超限文件先写入临时磁盘。"""

    def __init__(
        self,
        app: ASGIApp,
        *,
        max_source_bytes: int,
        max_folder_bytes: int,
        max_avatar_bytes: int,
        max_knowledge_attachment_bytes: int | None = None,
        max_statement_bytes: int = STATEMENT_IMPORT_REQUEST_MAX_BYTES,
        max_json_bytes: int = 4 * 1024 * 1024,
    ) -> None:
        if max_knowledge_attachment_bytes is None:
            max_knowledge_attachment_bytes = max_source_bytes
        if min(max_source_bytes, max_folder_bytes, max_avatar_bytes, max_json_bytes) <= 0:
            raise ValueError("请求体上限必须大于 0")
        if max_knowledge_attachment_bytes <= 0:
            raise ValueError("企业知识附件请求体上限必须大于 0")
        if max_statement_bytes <= 0:
            raise ValueError("渠道账单请求体上限必须大于 0")
        self.app = app
        self.max_source_bytes = max_source_bytes
        self.max_folder_bytes = max_folder_bytes
        self.max_avatar_bytes = max_avatar_bytes
        self.max_knowledge_attachment_bytes = max_knowledge_attachment_bytes
        self.max_statement_bytes = max_statement_bytes
        self.max_json_bytes = max_json_bytes

    def _limit_for_path(self, path: str) -> int:
        if path in {_STATEMENT_IMPORT_PATH, _FUND_STATEMENT_IMPORT_PATH}:
            return self.max_statement_bytes
        if path == _FOLDER_UPLOAD_PATH:
            return self.max_folder_bytes
        if path == _AVATAR_UPLOAD_PATH:
            return self.max_avatar_bytes
        if _ENTERPRISE_KNOWLEDGE_ATTACHMENT_PATH.fullmatch(path):
            return self.max_knowledge_attachment_bytes
        return self.max_source_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        # lifespan 没有 HTTP headers；必须先透传协议消息，避免应用启动被
        # Uvicorn 判成不支持生命周期，导致后台调度器始终未启动。
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = str(scope.get("path", ""))
        method = str(scope.get("method", ""))
        headers = Headers(scope=scope)
        content_type = headers.get("content-type", "").lower()
        upload_path = method == "POST" and is_limited_upload_path(path)
        json_path = (
            method in JSON_REQUEST_METHODS
            and content_type.startswith("application/json")
        )
        if not upload_path and not json_path:
            await self.app(scope, receive, send)
            return

        max_bytes = self._limit_for_path(path) if upload_path else self.max_json_bytes
        content_length = headers.get("content-length")
        if content_length:
            try:
                declared_size = int(content_length)
            except ValueError:
                declared_size = max_bytes + 1
            if declared_size < 0 or declared_size > max_bytes:
                await self._reject(scope, receive, send)
                return

        received = 0
        response_started = False

        async def limited_receive() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > max_bytes:
                    raise PayloadTooLargeError("上传请求超过容量上限")
            return message

        async def tracked_send(message: Message) -> None:
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
            await send(message)

        try:
            await self.app(scope, limited_receive, tracked_send)
        except PayloadTooLargeError:
            if response_started:
                raise
            await self._reject(scope, receive, send)

    async def _reject(self, scope: Scope, receive: Receive, send: Send) -> None:
        request_id = str(scope.get("state", {}).get("request_id", ""))
        response = JSONResponse(
            status_code=413,
            content={
                "code": 41300,
                "message": "请求体超过容量上限，请减小内容后重新提交",
                "request_id": request_id,
                "retryable": False,
                "next_action": "请减小内容后重新提交",
            },
        )
        await response(scope, receive, send)
