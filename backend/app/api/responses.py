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

from app.services.billing_ledger_service import BillingError
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

_PUBLIC_ERROR_RESPONSES = {
    "billing_error": (
        409,
        "invalid_request_error",
        "Billing could not be completed. Check usage status before retrying.",
    ),
    "context_compaction_failed": (
        413,
        "invalid_request_error",
        "Context compaction failed; input could not be processed safely.",
    ),
    "gateway_error": (500, "server_error", "The request could not be completed."),
    "invalid_api_key": (401, "authentication_error", "The Bearer credential is invalid."),
    "invalid_cursor": (400, "invalid_request_error", "The requested input item cursor was not found."),
    "invalid_json": (400, "invalid_request_error", "Request body must be a valid JSON object."),
    "invalid_request": (400, "invalid_request_error", "Request body must be a JSON object."),
    "invalid_value": (400, "invalid_request_error", "A request parameter has an invalid value."),
    "permission_denied": (403, "permission_error", "The credential does not have permission for this request."),
    "response_not_found": (404, "invalid_request_error", "The requested response was not found."),
    "storage_unavailable": (503, "server_error", "The response store is temporarily unavailable."),
    "upstream_payment_required": (402, "billing_error", "The upstream API requires payment or additional quota."),
    "upstream_rate_limited": (429, "rate_limit_error", "The upstream API is rate limited."),
    "upstream_timeout": (504, "server_error", "The upstream API request timed out."),
    "upstream_unavailable": (502, "server_error", "The upstream API is unavailable."),
}


def get_responses_service() -> DeepSeekResponsesService:
    """Return the process-level gateway service (replaceable in tests)."""

    return _service


def _error_response(error: ResponsesGatewayError) -> JSONResponse:
    code = (
        error.code
        if isinstance(error.code, str) and error.code in _PUBLIC_ERROR_RESPONSES
        else "gateway_error"
    )
    status_code, error_type, message = _PUBLIC_ERROR_RESPONSES[code]
    if code == "upstream_unavailable" and error.status_code == 503:
        status_code = 503
    param = None
    if code == "invalid_value" and isinstance(error.param, str) and error.param in {"limit", "order"}:
        if error.param == "limit":
            param = "limit"
            message = "Invalid limit; expected an integer from 1 to 100."
        else:
            param = "order"
            message = "Invalid order; expected 'asc' or 'desc'."
    elif code == "invalid_cursor":
        param = "after"
    elif code == "response_not_found":
        param = "response_id"
    elif code == "context_compaction_failed":
        param = "input"
    return JSONResponse(
        status_code=status_code,
        content={
            "error": {"message": message, "type": error_type, "param": param, "code": code}
        },
    )


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
    except BillingError:
        return _error_response(
            ResponsesGatewayError(
                "Billing could not be completed. Check usage status before retrying.",
                status_code=409,
                code="billing_error",
                error_type="invalid_request_error",
            )
        )

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
