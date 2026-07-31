"""管理 Agent 固定能力的真实 API 执行层。"""

from __future__ import annotations

from typing import Any, Mapping
from urllib.parse import quote

import httpx

from app.core.security import create_access_token
from app.models.user import User
from app.services.admin_capability_registry import AdminCapabilitySpec, operation_contract


class AdminCapabilityError(ValueError):
    """能力契约或真实 API 执行失败。"""


def prepare_request(
    spec: AdminCapabilitySpec,
    params: Mapping[str, Any],
    openapi: Mapping[str, Any],
) -> tuple[str, dict[str, Any], dict[str, Any] | None]:
    contract = operation_contract(spec, openapi)
    arguments = dict(params)
    allowed = set(contract.schema.get("properties", {}))
    unknown = sorted(set(arguments) - allowed)
    if unknown:
        raise AdminCapabilityError(f"能力 {spec.code} 不接受参数: {', '.join(unknown)}")
    missing = [name for name in contract.schema.get("required", []) if name not in arguments]
    if missing:
        raise AdminCapabilityError(f"能力 {spec.code} 缺少必填参数: {', '.join(missing)}")

    path = spec.path
    for name in contract.path_names:
        if name not in arguments:
            raise AdminCapabilityError(f"能力 {spec.code} 缺少路径参数: {name}")
        path = path.replace("{" + name + "}", quote(str(arguments[name]), safe=""))
    if "{" in path or "}" in path:
        raise AdminCapabilityError(f"能力 {spec.code} 路径参数未完整替换")

    query = {name: arguments[name] for name in contract.query_names if name in arguments}
    body = {name: arguments[name] for name in contract.body_names if name in arguments}
    return path, query, body if contract.body_names else None


async def execute_api(
    user: User,
    spec: AdminCapabilitySpec,
    params: Mapping[str, Any],
    *,
    request_id: str,
) -> dict[str, Any]:
    """以当前管理员身份重入 FastAPI，复用原路由的权限与业务 Service。"""

    # 延迟导入避免 app.main -> router -> Responses service 的循环导入。
    from app.main import app

    openapi = app.openapi()
    path, query, body = prepare_request(spec, params, openapi)
    token = create_access_token(
        int(user.id),
        str(user.role),
        int(getattr(user, "token_version", 0) or 0),
    )
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://prism.internal", timeout=120.0) as client:
        response = await client.request(
            spec.method,
            path,
            params=query or None,
            json=body,
            headers={
                "Authorization": f"Bearer {token}",
                "X-Request-Id": request_id,
                "X-Prism-Agent-Capability": spec.code,
            },
        )
    try:
        payload = response.json()
    except ValueError as exc:
        raise AdminCapabilityError(f"能力 {spec.code} 返回非 JSON 响应(HTTP {response.status_code})") from exc
    if response.status_code >= 400:
        message = payload.get("message") if isinstance(payload, Mapping) else None
        detail = payload.get("detail") if isinstance(payload, Mapping) else None
        raise AdminCapabilityError(
            f"能力 {spec.code} 执行失败(HTTP {response.status_code}): " f"{message or detail or '未知错误'}"
        )
    if isinstance(payload, Mapping) and int(payload.get("code", 0) or 0) != 0:
        raise AdminCapabilityError(
            f"能力 {spec.code} 执行失败: {payload.get('message') or payload.get('detail') or '未知错误'}"
        )
    data = payload.get("data") if isinstance(payload, Mapping) and "data" in payload else payload
    return {
        "capability": spec.code,
        "page": spec.page,
        "status_code": response.status_code,
        "data": data,
    }
