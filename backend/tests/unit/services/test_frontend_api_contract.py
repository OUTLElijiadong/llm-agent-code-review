"""前端 API 封装与后端真实路由契约测试。"""
import re
from pathlib import Path

from app.main import app

HTTP_METHODS = {"GET", "POST", "PUT", "DELETE"}
HELPER_METHOD_MAP = {
    "get": "GET",
    "post": "POST",
    "put": "PUT",
    "del": "DELETE",
    "download": "GET",
    "delete": "DELETE",
}


def _repo_root() -> Path:
    """获取仓库根目录。

    Returns:
        Path: 当前测试文件所在仓库根目录。
    """
    return Path(__file__).resolve().parents[4]


def _normalize_path(path: str) -> str:
    """归一化动态路由参数。

    Args:
        path: 前端或后端路由路径。

    Returns:
        str: 将所有动态参数替换为统一占位符后的路径。
    """
    return re.sub(r"(\$\{[^}]+\}|\{[^}]+\})", "{param}", path)


def _extract_file_constants(source: str) -> dict[str, str]:
    """提取前端 API 文件中的字符串常量定义(如 const BASE = '/rbac')。

    用于在路径归一化时把 ${BASE} 等变量引用替换为实际字符串值,
    避免变量引用导致契约测试无法匹配后端真实路由。

    Args:
        source: 前端文件源代码文本。

    Returns:
        dict[str, str]: 变量名到字符串值的映射;无定义时返回空字典。
    """
    constants: dict[str, str] = {}
    # 匹配 const NAME = 'value' 或 const NAME = "value"
    pattern = re.compile(
        r"\bconst\s+([A-Z][A-Z0-9_]*)\s*=\s*(['\"])([^'\"]+)\2",
    )
    for match in pattern.finditer(source):
        constants[match.group(1)] = match.group(3)
    return constants


def _resolve_template_variables(path: str, constants: dict[str, str]) -> str:
    """将路径中的 ${VAR} 模板变量替换为文件级常量值。

    Args:
        path: 前端调用路径,可能含 ${BASE} 等变量引用。
        constants: 文件级常量映射(由 _extract_file_constants 提供)。

    Returns:
        str: 变量被实际值替换后的路径;未找到常量则保留 {param} 占位符。
    """

    def _replace(match: re.Match) -> str:
        var_name = match.group(1)
        if var_name in constants:
            return constants[var_name]
        return "{param}"

    return re.sub(r"\$\{([A-Z][A-Z0-9_]*)\}", _replace, path)


def _backend_http_routes() -> set[tuple[str, str]]:
    """读取 FastAPI 已注册的 HTTP API 路由。

    Returns:
        set[tuple[str, str]]: 方法与归一化路径集合，路径不包含 `/api` 前缀。
    """
    routes: set[tuple[str, str]] = set()
    for route in app.routes:
        path = getattr(route, "path", "")
        methods = getattr(route, "methods", set()) or set()
        if not path.startswith("/api"):
            continue
        normalized_path = _normalize_path(path[4:])
        for method in methods:
            if method in HTTP_METHODS:
                routes.add((method, normalized_path))
    return routes


def _frontend_http_calls() -> set[tuple[str, str, str]]:
    """扫描前端 API 调用封装和直接业务调用。

    Returns:
        set[tuple[str, str, str]]: 方法、归一化路径、来源文件集合。
    """
    src_root = _repo_root() / "frontend/src"
    files = list((src_root / "api").glob("*.ts")) + [
        src_root / "components/ai/AgentChatDrawer.vue",
    ]
    patterns = [
        re.compile(
            r"(?<!function\s)(?<!async\sfunction\s)\b(get|post|put|del|download)"
            r"(?:<[^;]*?>)?\s*\(\s*(`[^`]+`|'[^']+'|\"[^\"]+\")",
            re.S,
        ),
        re.compile(
            r"\bhttpClient\.(get|post|put|delete)\s*\(\s*(`[^`]+`|'[^']+'|\"[^\"]+\")",
            re.S,
        ),
    ]
    calls: set[tuple[str, str, str]] = set()
    for file_path in files:
        if file_path.name == "http.ts":
            continue
        source = file_path.read_text(encoding="utf-8")
        # 提取文件级字符串常量(如 const BASE = '/rbac'),用于变量替换
        constants = _extract_file_constants(source)
        for pattern in patterns:
            for match in pattern.finditer(source):
                method = HELPER_METHOD_MAP[match.group(1)]
                raw_path = match.group(2)[1:-1]
                # 先把 ${BASE} 等变量替换为实际字符串值,再归一化动态参数
                resolved_path = _resolve_template_variables(raw_path, constants)
                path = _normalize_path(resolved_path)
                source_file = str(file_path.relative_to(src_root))
                calls.add((method, path, source_file))
    return calls


def test_frontend_http_api_calls_match_backend_routes():
    """验证前端 HTTP API 调用均接入后端真实端点。"""
    backend_routes = _backend_http_routes()
    frontend_calls = _frontend_http_calls()

    missing = sorted(
        (method, path, source_file)
        for method, path, source_file in frontend_calls
        if (method, path) not in backend_routes
    )

    assert not missing


def test_frontend_stream_endpoints_match_backend_routes():
    """验证 SSE 和 WebSocket 端点在前后端保持一致。"""
    src_root = _repo_root() / "frontend/src"
    agent_stream = (src_root / "utils/agentEventStream.ts").read_text(encoding="utf-8")
    discussion_stream = (src_root / "utils/discussionStream.ts").read_text(encoding="utf-8")

    assert "/agents/events" in agent_stream
    assert ("GET", "/agents/events") in _backend_http_routes()
    assert "/api/ws/discuss/" in discussion_stream
    assert any(getattr(route, "path", "") == "/api/ws/discuss/{session_id}" for route in app.routes)
