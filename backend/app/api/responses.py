"""Top-level OpenAI-compatible Responses API routes.

Integration intentionally lives outside the existing ``/api`` business router.
Mount ``router`` directly on the FastAPI application to expose
``/v1/responses`` without a second prefix.
"""

from __future__ import annotations

import json
from typing import Dict, List, Optional

from fastapi import APIRouter, Header, Query, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from app.services.deepseek_responses_service import (
    BufferedGatewayResponse,
    DeepSeekResponsesService,
    ResponsesGatewayError,
    StreamingGatewayResponse,
)

router = APIRouter(prefix="/v1/responses", tags=["Responses API"])
_service = DeepSeekResponsesService()

_EXCLUDED_RELAY_HEADERS = {
    "connection",
    "content-encoding",
    "content-length",
    "date",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "server",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
}


def get_responses_service() -> DeepSeekResponsesService:
    """Return the process-level gateway service (replaceable in tests)."""

    return _service


def _error_response(error: ResponsesGatewayError) -> JSONResponse:
    return JSONResponse(status_code=error.status_code, content=error.as_dict())


def _relay_headers(headers: Dict[str, str]) -> Dict[str, str]:
    relayed = {key: value for key, value in headers.items() if key.lower() not in _EXCLUDED_RELAY_HEADERS}
    content_type = next((value for key, value in relayed.items() if key.lower() == "content-type"), "")
    if content_type.lower().startswith("text/event-stream"):
        lower_names = {key.lower() for key in relayed}
        if "cache-control" not in lower_names:
            relayed["cache-control"] = "no-cache"
        if "x-accel-buffering" not in lower_names:
            relayed["x-accel-buffering"] = "no"
    return relayed


def _parse_limit(value: Optional[str]) -> int:
    if value is None:
        return 20
    try:
        return int(value)
    except ValueError as exc:
        raise ResponsesGatewayError(
            "Invalid limit; expected an integer from 1 to 100.",
            status_code=400,
            code="invalid_value",
            param="limit",
        ) from exc


@router.post("")
async def create_response(
    request: Request,
    authorization: Optional[str] = Header(default=None, alias="Authorization"),
) -> Response:
    """Create a native DeepSeek response and relay JSON or SSE unchanged."""

    try:
        payload = json.loads(await request.body())
    except (UnicodeDecodeError, json.JSONDecodeError):
        return _error_response(
            ResponsesGatewayError(
                "Request body must be a valid JSON object.",
                status_code=400,
                code="invalid_json",
            )
        )
    if not isinstance(payload, dict):
        return _error_response(
            ResponsesGatewayError(
                "Request body must be a JSON object.",
                status_code=400,
                code="invalid_request",
            )
        )

    try:
        upstream = await get_responses_service().create(payload, authorization)
    except ResponsesGatewayError as error:
        return _error_response(error)

    headers = _relay_headers(dict(upstream.headers))
    if isinstance(upstream, StreamingGatewayResponse):
        return StreamingResponse(
            upstream.body,
            status_code=upstream.status_code,
            headers=headers,
        )
    if not isinstance(upstream, BufferedGatewayResponse):
        return _error_response(
            ResponsesGatewayError(
                "Unexpected gateway response type.",
                status_code=500,
                code="gateway_error",
                error_type="server_error",
            )
        )
    return Response(content=upstream.content, status_code=upstream.status_code, headers=headers)


@router.get("/{response_id}")
async def retrieve_response(
    response_id: str,
    authorization: Optional[str] = Header(default=None, alias="Authorization"),
) -> Response:
    """Retrieve a locally persisted response for the same Bearer credential."""

    try:
        payload = await get_responses_service().retrieve(response_id, authorization)
    except ResponsesGatewayError as error:
        return _error_response(error)
    return JSONResponse(content=payload)


@router.delete("/{response_id}")
async def delete_response(
    response_id: str,
    authorization: Optional[str] = Header(default=None, alias="Authorization"),
) -> Response:
    """Delete a locally persisted response for the same Bearer credential."""

    try:
        payload = await get_responses_service().delete(response_id, authorization)
    except ResponsesGatewayError as error:
        return _error_response(error)
    return JSONResponse(content=payload)


@router.get("/{response_id}/input_items")
async def list_response_input_items(
    response_id: str,
    after: Optional[str] = Query(default=None),
    include: Optional[List[str]] = Query(default=None),
    limit: Optional[str] = Query(default=None),
    order: str = Query(default="desc"),
    authorization: Optional[str] = Header(default=None, alias="Authorization"),
) -> Response:
    """List the concrete items sent upstream, with OpenAI cursor semantics."""

    del include  # Include fields are already retained in the complete stored items.
    try:
        parsed_limit = _parse_limit(limit)
        payload = await get_responses_service().list_input_items(
            response_id,
            authorization,
            after=after,
            limit=parsed_limit,
            order=order,
        )
    except ResponsesGatewayError as error:
        return _error_response(error)
    return JSONResponse(content=payload)
