"""DeepSeek native Responses API transport and local conversation replay.

DeepSeek's Responses endpoint is currently stateless even when ``store=true``.
This module keeps the wire protocol native while persisting response transcripts
locally so ``previous_response_id`` can be translated into explicit item replay.
"""

from __future__ import annotations

import copy
import hashlib
import json
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, AsyncIterator, Callable, Dict, List, Mapping, Optional, Protocol, Tuple, Union

import httpx
from loguru import logger

from app.core.config import settings
from app.utils.public_http import pin_public_http_url

TRANSCRIPT_TTL_SECONDS = 30 * 24 * 60 * 60
MEMORY_TRANSCRIPT_LIMIT = 512
_STORE_SCHEMA_VERSION = 1
_TERMINAL_RESPONSE_EVENTS = {
    "response.completed",
    "response.failed",
    "response.incomplete",
    "response.cancelled",
}


class ResponsesGatewayError(Exception):
    """OpenAI-compatible local gateway error."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int,
        code: str,
        error_type: str = "invalid_request_error",
        param: Optional[str] = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.code = code
        self.error_type = error_type
        self.param = param

    def as_dict(self) -> Dict[str, Any]:
        return {
            "error": {
                "message": self.message,
                "type": self.error_type,
                "param": self.param,
                "code": self.code,
            }
        }


class ResponseNotFoundError(ResponsesGatewayError):
    """The requested response is absent for the current credential."""

    def __init__(self, response_id: str) -> None:
        super().__init__(
            f"Response '{response_id}' not found.",
            status_code=404,
            code="response_not_found",
            param="response_id",
        )


class TranscriptStoreUnavailableError(RuntimeError):
    """An authoritative transcript mutation cannot be completed safely."""


@dataclass(frozen=True)
class ResponseTranscript:
    """Complete replay state for one stored response."""

    response_id: str
    response: Dict[str, Any]
    input_items: List[Dict[str, Any]]
    transcript: List[Any]
    created_at: float

    def to_json(self) -> str:
        return json.dumps(
            {
                "schema_version": _STORE_SCHEMA_VERSION,
                "response_id": self.response_id,
                "response": self.response,
                "input_items": self.input_items,
                "transcript": self.transcript,
                "created_at": self.created_at,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )

    @classmethod
    def from_json(cls, raw: str) -> "ResponseTranscript":
        data = json.loads(raw)
        if not isinstance(data, dict) or data.get("schema_version") != _STORE_SCHEMA_VERSION:
            raise ValueError("unsupported response transcript schema")
        response_id = data.get("response_id")
        response = data.get("response")
        input_items = data.get("input_items")
        transcript = data.get("transcript")
        created_at = data.get("created_at")
        if not isinstance(response_id, str) or not isinstance(response, dict):
            raise ValueError("invalid response transcript identity")
        if not isinstance(input_items, list) or not isinstance(transcript, list):
            raise ValueError("invalid response transcript items")
        if not isinstance(created_at, (int, float)):
            raise ValueError("invalid response transcript timestamp")
        return cls(
            response_id=response_id,
            response=response,
            input_items=input_items,
            transcript=transcript,
            created_at=float(created_at),
        )


class TranscriptStore(Protocol):
    """Asynchronous storage contract used by the transport service."""

    async def save(self, credential_fingerprint: str, record: ResponseTranscript) -> None: ...

    async def load(self, credential_fingerprint: str, response_id: str) -> Optional[ResponseTranscript]: ...

    async def delete(self, credential_fingerprint: str, response_id: str) -> bool: ...


class MemoryTranscriptStore:
    """Bounded process-local fallback safe across threads and event loops."""

    def __init__(
        self,
        *,
        ttl_seconds: int = TRANSCRIPT_TTL_SECONDS,
        max_entries: int = MEMORY_TRANSCRIPT_LIMIT,
    ) -> None:
        self._ttl_seconds = max(1, ttl_seconds)
        self._max_entries = max(1, max_entries)
        self._records: "OrderedDict[Tuple[str, str], Tuple[float, ResponseTranscript]]" = OrderedDict()
        self._lock = threading.RLock()

    async def save(self, credential_fingerprint: str, record: ResponseTranscript) -> None:
        key = (credential_fingerprint, record.response_id)
        with self._lock:
            self._purge_expired_locked()
            self._records[key] = (time.monotonic() + self._ttl_seconds, copy.deepcopy(record))
            self._records.move_to_end(key)
            while len(self._records) > self._max_entries:
                self._records.popitem(last=False)

    async def load(self, credential_fingerprint: str, response_id: str) -> Optional[ResponseTranscript]:
        key = (credential_fingerprint, response_id)
        with self._lock:
            self._purge_expired_locked()
            value = self._records.get(key)
            if value is None:
                return None
            self._records.move_to_end(key)
            return copy.deepcopy(value[1])

    async def delete(self, credential_fingerprint: str, response_id: str) -> bool:
        key = (credential_fingerprint, response_id)
        with self._lock:
            self._purge_expired_locked()
            return self._records.pop(key, None) is not None

    def _purge_expired_locked(self) -> None:
        now = time.monotonic()
        expired = [key for key, (expires_at, _) in self._records.items() if expires_at <= now]
        for key in expired:
            self._records.pop(key, None)


class RedisTranscriptStore:
    """Redis-backed response transcript storage."""

    _prefix = "prism:responses:v1"

    def __init__(
        self,
        redis_url: str,
        *,
        ttl_seconds: int = TRANSCRIPT_TTL_SECONDS,
        client: Any = None,
    ) -> None:
        if client is None:
            import redis.asyncio as redis_async

            client = redis_async.Redis.from_url(
                redis_url,
                decode_responses=True,
                socket_connect_timeout=1,
                socket_timeout=2,
            )
        self._client = client
        self._ttl_seconds = max(1, ttl_seconds)

    @classmethod
    def _key(cls, credential_fingerprint: str, response_id: str) -> str:
        response_digest = hashlib.sha256(response_id.encode("utf-8")).hexdigest()
        return f"{cls._prefix}:{credential_fingerprint}:{response_digest}"

    async def save(self, credential_fingerprint: str, record: ResponseTranscript) -> None:
        await self._client.set(
            self._key(credential_fingerprint, record.response_id),
            record.to_json(),
            ex=self._ttl_seconds,
        )

    async def load(self, credential_fingerprint: str, response_id: str) -> Optional[ResponseTranscript]:
        raw = await self._client.get(self._key(credential_fingerprint, response_id))
        if raw is None:
            return None
        record = ResponseTranscript.from_json(raw)
        if record.response_id != response_id:
            raise ValueError("response transcript key mismatch")
        return record

    async def delete(self, credential_fingerprint: str, response_id: str) -> bool:
        return bool(await self._client.delete(self._key(credential_fingerprint, response_id)))


class ResilientTranscriptStore:
    """Redis-first store with a synchronized in-memory degradation path."""

    def __init__(self, primary: TranscriptStore, fallback: Optional[MemoryTranscriptStore] = None) -> None:
        self._primary = primary
        self._fallback = fallback or MemoryTranscriptStore()
        self._fallback_only: "OrderedDict[Tuple[str, str], None]" = OrderedDict()
        self._marker_lock = threading.RLock()

    async def save(self, credential_fingerprint: str, record: ResponseTranscript) -> None:
        key = (credential_fingerprint, record.response_id)
        primary_saved = False
        try:
            await self._primary.save(credential_fingerprint, record)
            primary_saved = True
        except Exception as exc:
            logger.warning(f"[ResponsesAPI] Redis transcript save failed; using memory fallback: {exc}")
        await self._fallback.save(credential_fingerprint, record)
        if primary_saved:
            self._discard_marker(self._fallback_only, key)
        else:
            self._mark(self._fallback_only, key)

    async def load(self, credential_fingerprint: str, response_id: str) -> Optional[ResponseTranscript]:
        key = (credential_fingerprint, response_id)
        try:
            record = await self._primary.load(credential_fingerprint, response_id)
        except Exception as exc:
            logger.warning(f"[ResponsesAPI] Redis transcript load failed; using memory fallback: {exc}")
            return await self._fallback.load(credential_fingerprint, response_id)
        if record is not None:
            await self._fallback.save(credential_fingerprint, record)
            self._discard_marker(self._fallback_only, key)
            return record
        if not self._has_marker(self._fallback_only, key):
            # A reachable Redis miss is authoritative (remote delete or TTL).
            await self._fallback.delete(credential_fingerprint, response_id)
            return None

        record = await self._fallback.load(credential_fingerprint, response_id)
        if record is None:
            self._discard_marker(self._fallback_only, key)
            return None
        try:
            await self._primary.save(credential_fingerprint, record)
        except Exception as exc:
            logger.warning(f"[ResponsesAPI] Redis transcript repair deferred: {exc}")
        else:
            self._discard_marker(self._fallback_only, key)
        return record

    async def delete(self, credential_fingerprint: str, response_id: str) -> bool:
        key = (credential_fingerprint, response_id)
        try:
            primary_deleted = await self._primary.delete(credential_fingerprint, response_id)
        except Exception as exc:
            logger.warning(f"[ResponsesAPI] Redis transcript delete failed; refusing an inconsistent delete: {exc}")
            raise TranscriptStoreUnavailableError("authoritative transcript store is unavailable") from exc

        fallback_deleted = await self._fallback.delete(credential_fingerprint, response_id)
        self._discard_marker(self._fallback_only, key)
        return primary_deleted or fallback_deleted

    @staticmethod
    def _marker_limit() -> int:
        return MEMORY_TRANSCRIPT_LIMIT

    def _mark(self, markers: "OrderedDict[Tuple[str, str], None]", key: Tuple[str, str]) -> None:
        with self._marker_lock:
            markers[key] = None
            markers.move_to_end(key)
            while len(markers) > self._marker_limit():
                markers.popitem(last=False)

    def _discard_marker(self, markers: "OrderedDict[Tuple[str, str], None]", key: Tuple[str, str]) -> None:
        with self._marker_lock:
            markers.pop(key, None)

    def _has_marker(self, markers: "OrderedDict[Tuple[str, str], None]", key: Tuple[str, str]) -> bool:
        with self._marker_lock:
            return key in markers


def build_transcript_store(redis_url: str) -> TranscriptStore:
    """Build the configured Redis store without requiring Redis at import time."""

    memory = MemoryTranscriptStore()
    if not redis_url.strip():
        return memory
    try:
        return ResilientTranscriptStore(RedisTranscriptStore(redis_url), memory)
    except Exception as exc:
        logger.warning(f"[ResponsesAPI] Redis transcript client unavailable; using memory fallback: {exc}")
        return memory


@dataclass(frozen=True)
class BufferedGatewayResponse:
    """Fully-read upstream response preserving status, headers and bytes."""

    status_code: int
    headers: Mapping[str, str]
    content: bytes


@dataclass(frozen=True)
class StreamingGatewayResponse:
    """Open upstream stream plus its already-received response metadata."""

    status_code: int
    headers: Mapping[str, str]
    body: AsyncIterator[bytes]


GatewayResponse = Union[BufferedGatewayResponse, StreamingGatewayResponse]


class _SSEFinalResponseCapture:
    """Incrementally parse terminal SSE data without modifying relayed bytes."""

    def __init__(self) -> None:
        self._buffer = bytearray()
        self._data_lines: List[bytes] = []
        self._event_name = ""
        self.response: Optional[Dict[str, Any]] = None

    def feed(self, chunk: bytes) -> None:
        self._buffer.extend(chunk)
        while True:
            newline = self._buffer.find(b"\n")
            if newline < 0:
                return
            line = bytes(self._buffer[:newline])
            del self._buffer[: newline + 1]
            self._consume_line(line.rstrip(b"\r"))

    def finish(self) -> None:
        if self._buffer:
            self._consume_line(bytes(self._buffer).rstrip(b"\r"))
            self._buffer.clear()
        self._dispatch_event()

    def _consume_line(self, line: bytes) -> None:
        if not line:
            self._dispatch_event()
            return
        if line.startswith(b"data:"):
            value = line[5:]
            if value.startswith(b" "):
                value = value[1:]
            self._data_lines.append(value)
        elif line.startswith(b"event:"):
            self._event_name = line[6:].lstrip().decode("utf-8", errors="replace")

    def _dispatch_event(self) -> None:
        if not self._data_lines:
            self._event_name = ""
            return
        raw = b"\n".join(self._data_lines)
        self._data_lines = []
        try:
            payload = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._event_name = ""
            return
        event_type = payload.get("type") if isinstance(payload, dict) else None
        terminal = event_type in _TERMINAL_RESPONSE_EVENTS or self._event_name in _TERMINAL_RESPONSE_EVENTS
        candidate = payload.get("response") if isinstance(payload, dict) else None
        if terminal and isinstance(candidate, dict):
            self.response = candidate
        self._event_name = ""


class DeepSeekResponsesService:
    """Transparent native Responses transport with local state emulation."""

    def __init__(
        self,
        *,
        base_url: Optional[str] = None,
        timeout_seconds: Optional[float] = None,
        storage: Optional[TranscriptStore] = None,
        client_factory: Optional[Callable[[], httpx.AsyncClient]] = None,
    ) -> None:
        self._responses_url = self._resolve_responses_url(base_url or settings.deepseek_base_url)
        self._timeout_seconds = float(timeout_seconds or settings.deepseek_timeout)
        self._storage = storage or build_transcript_store(settings.redis_url)
        self._pin_upstream = client_factory is None
        self._client_factory = client_factory or self._new_http_client

    @property
    def responses_url(self) -> str:
        return self._responses_url

    async def create(self, payload: Dict[str, Any], authorization: Optional[str]) -> GatewayResponse:
        bearer, fingerprint = self._bearer_identity(authorization)
        upstream_payload, input_items = await self._prepare_upstream_payload(payload, fingerprint)
        should_store = payload.get("store") is not False
        headers = self._upstream_headers(bearer, stream=payload.get("stream") is True)

        if payload.get("stream") is True:
            return await self._open_stream(
                upstream_payload,
                headers,
                credential_fingerprint=fingerprint,
                input_items=input_items,
                should_store=should_store,
            )
        return await self._request_buffered(
            upstream_payload,
            headers,
            credential_fingerprint=fingerprint,
            input_items=input_items,
            should_store=should_store,
        )

    async def retrieve(self, response_id: str, authorization: Optional[str]) -> Dict[str, Any]:
        _, fingerprint = self._bearer_identity(authorization)
        record = await self._storage.load(fingerprint, response_id)
        if record is None:
            raise ResponseNotFoundError(response_id)
        return copy.deepcopy(record.response)

    async def delete(self, response_id: str, authorization: Optional[str]) -> Dict[str, Any]:
        _, fingerprint = self._bearer_identity(authorization)
        try:
            deleted = await self._storage.delete(fingerprint, response_id)
        except TranscriptStoreUnavailableError as exc:
            raise ResponsesGatewayError(
                "The response store is temporarily unavailable.",
                status_code=503,
                code="storage_unavailable",
                error_type="server_error",
            ) from exc
        if not deleted:
            raise ResponseNotFoundError(response_id)
        return {"id": response_id, "object": "response", "deleted": True}

    async def list_input_items(
        self,
        response_id: str,
        authorization: Optional[str],
        *,
        after: Optional[str] = None,
        limit: int = 20,
        order: str = "desc",
    ) -> Dict[str, Any]:
        _, fingerprint = self._bearer_identity(authorization)
        record = await self._storage.load(fingerprint, response_id)
        if record is None:
            raise ResponseNotFoundError(response_id)
        if order not in {"asc", "desc"}:
            raise ResponsesGatewayError(
                "Invalid order; expected 'asc' or 'desc'.",
                status_code=400,
                code="invalid_value",
                param="order",
            )
        if limit < 1 or limit > 100:
            raise ResponsesGatewayError(
                "Invalid limit; expected an integer from 1 to 100.",
                status_code=400,
                code="invalid_value",
                param="limit",
            )

        ordered = list(record.input_items)
        if order == "desc":
            ordered.reverse()
        if after:
            cursor_index = next((index for index, item in enumerate(ordered) if item.get("id") == after), None)
            if cursor_index is None:
                raise ResponsesGatewayError(
                    f"Cursor '{after}' not found.",
                    status_code=400,
                    code="invalid_cursor",
                    param="after",
                )
            ordered = ordered[cursor_index + 1 :]

        page = copy.deepcopy(ordered[:limit])
        return {
            "object": "list",
            "data": page,
            "first_id": page[0].get("id") if page else None,
            "last_id": page[-1].get("id") if page else None,
            "has_more": len(ordered) > limit,
        }

    async def _prepare_upstream_payload(
        self,
        payload: Dict[str, Any],
        credential_fingerprint: str,
    ) -> Tuple[Dict[str, Any], List[Any]]:
        upstream_payload = copy.deepcopy(payload)
        previous_response_id = payload.get("previous_response_id")
        if isinstance(previous_response_id, str) and previous_response_id:
            previous = await self._storage.load(credential_fingerprint, previous_response_id)
            if previous is None:
                raise ResponseNotFoundError(previous_response_id)
            current_items = self._normalise_input(payload.get("input"))
            upstream_payload.pop("previous_response_id", None)
            upstream_payload["input"] = copy.deepcopy(previous.transcript) + current_items
        input_items = self._normalise_input(upstream_payload.get("input"))
        return upstream_payload, input_items

    async def _request_buffered(
        self,
        payload: Dict[str, Any],
        headers: Dict[str, str],
        *,
        credential_fingerprint: str,
        input_items: List[Any],
        should_store: bool,
    ) -> BufferedGatewayResponse:
        client = self._client_factory()
        try:
            request_url = self._responses_url
            request_headers = dict(headers)
            extensions = None
            if self._pin_upstream:
                target = pin_public_http_url(self._responses_url)
                request_url = target.request_url
                request_headers["Host"] = target.host_header
                extensions = target.request_extensions
            response = await client.post(
                request_url,
                headers=request_headers,
                json=payload,
                extensions=extensions,
            )
            content = await response.aread()
        except httpx.TimeoutException as exc:
            raise ResponsesGatewayError(
                "The upstream Responses API timed out.",
                status_code=504,
                code="upstream_timeout",
                error_type="server_error",
            ) from exc
        except httpx.HTTPError as exc:
            raise ResponsesGatewayError(
                "The upstream Responses API is unavailable.",
                status_code=502,
                code="upstream_unavailable",
                error_type="server_error",
            ) from exc
        finally:
            await client.aclose()

        if 200 <= response.status_code < 300 and should_store:
            try:
                response_payload = json.loads(content)
            except (UnicodeDecodeError, json.JSONDecodeError):
                response_payload = None
            if isinstance(response_payload, dict):
                await self._store_response_safely(credential_fingerprint, response_payload, input_items)
        return BufferedGatewayResponse(
            status_code=response.status_code,
            headers=dict(response.headers),
            content=content,
        )

    async def _open_stream(
        self,
        payload: Dict[str, Any],
        headers: Dict[str, str],
        *,
        credential_fingerprint: str,
        input_items: List[Any],
        should_store: bool,
    ) -> StreamingGatewayResponse:
        client = self._client_factory()
        try:
            request_url = self._responses_url
            request_headers = dict(headers)
            extensions = None
            if self._pin_upstream:
                target = pin_public_http_url(self._responses_url)
                request_url = target.request_url
                request_headers["Host"] = target.host_header
                extensions = target.request_extensions
            request = client.build_request(
                "POST",
                request_url,
                headers=request_headers,
                json=payload,
                extensions=extensions,
            )
            response = await client.send(request, stream=True)
        except httpx.TimeoutException as exc:
            await client.aclose()
            raise ResponsesGatewayError(
                "The upstream Responses API timed out.",
                status_code=504,
                code="upstream_timeout",
                error_type="server_error",
            ) from exc
        except httpx.HTTPError as exc:
            await client.aclose()
            raise ResponsesGatewayError(
                "The upstream Responses API is unavailable.",
                status_code=502,
                code="upstream_unavailable",
                error_type="server_error",
            ) from exc

        capture = _SSEFinalResponseCapture()

        async def relay() -> AsyncIterator[bytes]:
            stream_finished = False
            try:
                async for chunk in response.aiter_raw():
                    capture.feed(chunk)
                    yield chunk
                stream_finished = True
                capture.finish()
                if (
                    stream_finished
                    and 200 <= response.status_code < 300
                    and should_store
                    and capture.response is not None
                ):
                    await self._store_response_safely(
                        credential_fingerprint,
                        capture.response,
                        input_items,
                    )
            finally:
                await response.aclose()
                await client.aclose()

        return StreamingGatewayResponse(
            status_code=response.status_code,
            headers=dict(response.headers),
            body=relay(),
        )

    async def _store_response_safely(
        self,
        credential_fingerprint: str,
        response: Dict[str, Any],
        input_items: List[Any],
    ) -> None:
        response_id = response.get("id")
        if not isinstance(response_id, str) or not response_id:
            return
        output_items = response.get("output")
        if not isinstance(output_items, list):
            output_items = []
        record = ResponseTranscript(
            response_id=response_id,
            response=copy.deepcopy(response),
            input_items=self._input_items_for_listing(input_items, response_id),
            transcript=copy.deepcopy(input_items) + copy.deepcopy(output_items),
            created_at=time.time(),
        )
        try:
            await self._storage.save(credential_fingerprint, record)
        except Exception as exc:
            # Persistence must never corrupt an otherwise valid upstream response.
            logger.error(f"[ResponsesAPI] transcript persistence failed: {exc}")

    def _new_http_client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            timeout=httpx.Timeout(self._timeout_seconds),
            trust_env=False,
        )

    @staticmethod
    def _resolve_responses_url(base_url: str) -> str:
        normalised = base_url.strip().rstrip("/")
        if normalised.endswith("/v1/responses"):
            return normalised
        if normalised.endswith("/v1"):
            return f"{normalised}/responses"
        return f"{normalised}/v1/responses"

    @staticmethod
    def _bearer_identity(authorization: Optional[str]) -> Tuple[str, str]:
        if not authorization:
            raise ResponsesGatewayError(
                "Missing Authorization Bearer credential.",
                status_code=401,
                code="invalid_api_key",
                error_type="authentication_error",
            )
        scheme, separator, token = authorization.strip().partition(" ")
        token = token.strip()
        if not separator or scheme.lower() != "bearer" or not token or any(character.isspace() for character in token):
            raise ResponsesGatewayError(
                "Invalid Authorization Bearer credential.",
                status_code=401,
                code="invalid_api_key",
                error_type="authentication_error",
            )
        fingerprint = hashlib.sha256(token.encode("utf-8")).hexdigest()
        return authorization.strip(), fingerprint

    @staticmethod
    def _upstream_headers(authorization: str, *, stream: bool) -> Dict[str, str]:
        return {
            "Authorization": authorization,
            "Content-Type": "application/json",
            "Accept": "text/event-stream" if stream else "application/json",
            "Accept-Encoding": "identity",
        }

    @staticmethod
    def _normalise_input(value: Any) -> List[Any]:
        if isinstance(value, str):
            return [
                {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": value}],
                }
            ]
        if isinstance(value, list):
            return copy.deepcopy(value)
        if value is None:
            return []
        return [copy.deepcopy(value)]

    @staticmethod
    def _input_items_for_listing(items: List[Any], response_id: str) -> List[Dict[str, Any]]:
        listed: List[Dict[str, Any]] = []
        for index, item in enumerate(items):
            if isinstance(item, dict):
                listed_item = copy.deepcopy(item)
            else:
                listed_item = {"type": "input_text", "text": str(item)}
            if not isinstance(listed_item.get("id"), str) or not listed_item["id"]:
                item_type = str(listed_item.get("type") or "item")
                prefix = {
                    "message": "msg",
                    "function_call": "fc",
                    "function_call_output": "fco",
                    "reasoning": "rs",
                }.get(item_type, "item")
                digest_source = json.dumps(listed_item, sort_keys=True, ensure_ascii=False, default=str)
                digest = hashlib.sha256(f"{response_id}:{index}:{digest_source}".encode("utf-8")).hexdigest()[:24]
                listed_item["id"] = f"{prefix}_{digest}"
            listed.append(listed_item)
        return listed
