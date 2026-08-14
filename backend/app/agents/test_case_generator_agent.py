"""沙箱黑白盒测试用例生成 Agent。

在测试执行前调用:基于项目源码摘要,为指定语言生成可直接执行的
自包含断言测试文件(不生成 shell 命令,只注入数据文件),由沙箱镜像
内置 runner 确定性执行,保持 fail-closed 隔离。
"""

from __future__ import annotations

import json
import re
import time
from typing import Any, Optional

from app.agents.base import AgentContext, BaseAgent

MAX_FILES = 5
MAX_FILE_BYTES = 60_000
# 测试脚本允许引用的常见标准库/内建属性，避免把合法的 urllib/json/os 使用误判为幻觉。
_SAFE_ATTRIBUTES = frozenset({
    "append", "add", "get", "setdefault", "update", "keys", "values", "items",
    "startswith", "endswith", "strip", "split", "join", "replace", "encode",
    "decode", "read", "write", "close", "content", "status_code", "headers",
    "text", "json", "load", "loads", "dumps", "dump", "request", "urlopen",
    "urlencode", "quote", "quote_plus", "unquote", "unquote_plus", "urljoin",
    "urlsplit", "urlunsplit", "parse_qs", "urlparse", "parse", "path", "name",
    "suffix", "stem", "parent", "resolve", "exists", "mkdir", "makedirs",
    "listdir", "getcwd", "environ", "getenv", "connect", "cursor", "execute",
    "fetchall", "fetchone", "commit", "rollback", "sleep", "time", "now",
    "utcnow", "strftime", "strptime", "timestamp", "total_seconds", "timedelta",
    "timezone", "date", "today", "fromisoformat", "check_output", "run", "Popen",
    "call", "check_call", "communicate", "wait", "poll", "returncode", "stdout",
    "stderr", "stdin", "assertEqual", "assertTrue", "assertFalse", "assertIn",
    "assertRaises", "main", "exit", "mock", "patch", "Mock", "MagicMock",
    "TestCase", "ArgumentParser", "Namespace", "parse_args", "add_argument",
    "compile", "match", "search", "findall", "finditer", "sub", "subn", "escape",
    "defaultdict", "OrderedDict", "Counter", "deque", "namedtuple", "chain",
    "product", "combinations", "permutations", "islice", "groupby", "accumulate",
    "partial", "reduce", "lru_cache", "wraps", "ceil", "floor", "sqrt", "log",
    "exp", "isclose", "nan", "inf", "isfinite", "isnan", "b64encode",
    "b64decode", "urlsafe_b64encode", "urlsafe_b64decode", "hexdigest", "digest",
    "sha1", "sha256", "sha512", "md5", "send", "recv", "bind", "listen", "accept",
    "settimeout", "getsockname", "logger", "getLogger", "basicConfig", "info",
    "debug", "warning", "error", "exception", "critical", "start", "daemon",
    "is_alive", "Lock", "Thread", "abspath", "basename", "dirname", "realpath",
    "relpath", "expanduser", "walk", "remove", "rmtree", "copyfile", "move",
    "extract", "extractall", "read_text", "write_text", "read_bytes",
    "write_bytes", "iterdir", "glob", "rglob", "is_file", "is_dir", "open",
    "with_suffix", "with_name", "with_stem", "is_absolute", "format_map",
    "casefold", "partition", "rpartition", "rstrip", "lstrip", "title", "zfill",
    "bit_length", "to_bytes", "from_bytes", "insert", "extend", "index", "count",
    "sort", "reverse", "difference", "union", "intersection", "symmetric_difference",
    "issubset", "issuperset", "isdisjoint", "discard", "pop", "popitem", "clear",
    "copy", "fromkeys", "asdict", "astuple", "replace", "field", "fields",
    "itemgetter", "attrgetter", "methodcaller", "Optional", "List", "Dict", "Set",
    "Tuple", "Any", "Union", "Callable", "Iterable", "Sequence", "Mapping",
    "Generator", "TypeVar", "Generic", "Literal", "Protocol", "NamedTuple",
    "TypedDict", "cast", "ClassVar", "Final", "NoReturn", "etree", "abc",
})


_IMPORT_LINE_RE = re.compile(r"^\s*from\s+([A-Za-z0-9_.]+)\s+import\s+(.+)$")
_ATTR_RE = re.compile(r"(?<![A-Za-z0-9_])([A-Za-z_][A-Za-z0-9_]*)\.[A-Za-z_][A-Za-z0-9_]*")
_STDLIB_MODULES = frozenset({
    "argparse", "asyncio", "atexit", "base64", "binascii", "calendar", "codecs",
    "collections", "collections.abc", "configparser", "contextlib", "copy", "csv",
    "dataclasses", "datetime", "decimal", "difflib", "enum", "fractions", "functools",
    "gc", "glob", "gzip", "hashlib", "heapq", "hmac", "html", "http", "http.client",
    "http.cookies", "http.server", "importlib", "inspect", "io", "itertools", "json",
    "logging", "math", "mimetypes", "multiprocessing", "os", "os.path", "pathlib",
    "pickle", "platform", "pprint", "queue", "random", "re", "secrets", "select",
    "selectors", "shutil", "signal", "socket", "sqlite3", "ssl", "statistics",
    "string", "struct", "subprocess", "sys", "tarfile", "tempfile", "textwrap",
    "threading", "time", "traceback", "types", "typing", "unicodedata", "unittest",
    "unittest.mock", "urllib", "urllib.error", "urllib.parse", "urllib.request",
    "urllib.response", "uuid", "warnings", "weakref", "xml", "xml.etree",
    "xml.etree.ElementTree", "zipfile", "zlib", "email", "email.message",
})
_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def _source_text(source_summary: dict[str, Any]) -> str:
    snippets = source_summary.get("snippets")
    snippets = snippets if isinstance(snippets, dict) else {}
    parts = [str(value) for value in snippets.values() if isinstance(value, str)]
    return "\n".join(parts)


def _grounding_feedback(files: list[dict[str, str]], source_summary: dict[str, Any]) -> list[str]:
    """返回测试脚本引用了、但源码摘要中不存在的符号（防止凭空造 API）。"""
    source_text = _source_text(source_summary)
    unsupported: set[str] = set()
    for item in files:
        content = str(item.get("content") or "")
        for match in _ATTR_RE.finditer(content):
            token = match.group(0).split(".", 1)[1]
            if token in _SAFE_ATTRIBUTES or token in source_text:
                continue
            unsupported.add(token)
        for line in content.splitlines():
            import_match = _IMPORT_LINE_RE.match(line)
            if import_match is None:
                continue
            module_name = import_match.group(1)
            if module_name in _STDLIB_MODULES:
                continue
            for name in import_match.group(2).split(","):
                name = name.strip().lstrip("*")
                if not name or name in source_text:
                    continue
                unsupported.add(name)
    return sorted(unsupported)


def _with_grounding_feedback(source_summary: dict[str, Any], unsupported: list[str]) -> dict[str, Any]:
    summary = dict(source_summary or {})
    summary["previous_generation_feedback"] = (
        "上一轮生成的白盒测试引用了源码摘要中不存在的符号："
        + ", ".join(unsupported)
        + "。本轮禁止使用这些符号；只允许使用摘要里真实出现的类/函数/属性，"
        "没有对应逻辑时改为对现有模块的导入/语法/常量做冒烟断言。"
    )
    return summary

_LANGUAGE_EXTENSION = {
    "python": ".py",
    "node": ".js",
    "php": ".php",
    "go": ".go",
    "java": ".java",
}


class TestCaseGeneratorAgent(BaseAgent):
    """根据源码摘要生成白盒/黑盒自包含断言测试文件。"""

    name = "test_case_generator"
    description = "为沙箱黑白盒测试动态生成自包含断言测试文件"
    icon = "test_case_generator"
    color = "#2F7D6D"
    category = "review"

    def __init__(self) -> None:
        from app.core.config import settings

        super().__init__(
            system_prompt=(
                "你是代码测试用例生成器。根据给定源码摘要与语言,生成可直接执行的自包含断言测试脚本。"
                "只输出 JSON,不要输出其他内容。"
            ),
            temperature=0.2,
            max_tokens=min(65_536, int(settings.deepseek_max_output_tokens)),
        )

    def generate(
        self,
        *,
        language: str,
        test_mode: str,
        source_summary: dict[str, Any],
        db_type: str = "none",
        ctx: Optional[AgentContext] = None,
        deadline: Optional[float] = None,
    ) -> dict[str, Any]:
        """生成测试用例文件列表。

        Returns:
            dict: {"files": [{"path": "test_ai_xxx", "content": "..."}]} 或 {"error": "..."}
        """
        db_instruction = ""
        if db_type == "mysql":
            db_instruction = (
                "\n数据库: mysql(独立沙箱测试库,只读环境变量连接,禁止硬编码凭据)。用环境变量:\n"
                "  PRISM_DB_HOST / PRISM_DB_PORT / PRISM_DB_USER / PRISM_DB_PASSWORD / PRISM_DB_NAME\n"
                "  python: import pymysql, os; conn=pymysql.connect(host=os.environ['PRISM_DB_HOST'],\n"
                "    port=int(os.environ.get('PRISM_DB_PORT','3306')), user=os.environ['PRISM_DB_USER'],\n"
                "    password=os.environ['PRISM_DB_PASSWORD'], database=os.environ['PRISM_DB_NAME'],\n"
                "    autocommit=True); cur=conn.cursor(); cur.execute('CREATE TABLE IF NOT EXISTS users(...)');\n"
                "  php: \\$pdo=new PDO('mysql:host='.getenv('PRISM_DB_HOST').';port='.getenv('PRISM_DB_PORT').\n"
                "    ';dbname='.getenv('PRISM_DB_NAME'), getenv('PRISM_DB_USER'), getenv('PRISM_DB_PASSWORD'));\n"
                "  SQL 注入探测必须针对真实 MySQL 语法(如 OR '1'='1 绕过登录、UNION SELECT),验证是否可利用。\n"
                "  【强制】当数据库为 mysql 时,必须额外生成至少 1 个数据库安全测试文件(计入 2-4 个配额),\n"
                "  文件名建议 test_db_security.<项目语言扩展名>:连接测试库后执行\n"
                "  (a)建表+插入数据+参数化查询 CRUD 验证连接可用;(b)SQL 注入防护验证:用 ' OR '1'='1、\n"
                "  UNION SELECT、'-- 等 payload 构造查询,断言注入被拦截(不返回额外行/不报错泄露);\n"
                "  失败以 AssertionError/抛出异常表示。\n"
            )
        if db_type == "sqlite":
            db_instruction = (
                "\n数据库: sqlite(独立沙箱内置,无需外部服务)。测试用例可自建 SQLite 库:\n"
                "  python: import sqlite3; c=sqlite3.connect('/workspace/.prism-db/app.db');\n"
                "    c.execute('CREATE TABLE IF NOT EXISTS users(\n"
                "      id INTEGER PRIMARY KEY, username TEXT, password TEXT)'); c.commit()\n"
                "  php: \\$pdo=new PDO('sqlite:/workspace/.prism-db/app.db');\n"
                "    \\$pdo->exec('CREATE TABLE IF NOT EXISTS users(\n"
                "      id INTEGER PRIMARY KEY, username TEXT, password TEXT)');\n"
                "  SQL 注入探测必须针对真实 SQL 语法(如 OR '1'='1 绕过登录、UNION SELECT),验证是否可利用。\n"
            )
        user_message = (
            f"语言: {language}\n"
            f"测试模式: {test_mode}\n"
            f"数据库: {db_type or 'none'}{db_instruction}\n"
            "源码摘要(JSON):\n"
            f"{json.dumps(source_summary, ensure_ascii=False, default=str)[:60000]}\n\n"
            "要求:\n"
            "1. whitebox 模式生成 2-4 个白盒断言文件；blackbox 模式只生成 1 个 blackbox 文件；\n"
            "   combined 模式生成 2-4 个白盒断言文件并额外生成 1 个 blackbox 文件。\n"
            "   白盒用例覆盖核心逻辑、边界与关键入口;\n"
            "   每个文件控制在 10-50 行,精简断言,避免超长输出。\n"
            "2. 所有测试文件必须使用项目语言(上方「语言」字段)编写,禁止使用其他语言:\n"
            "   测试文件会放在 /workspace/_agent_tests，执行工作目录固定为 /workspace；项目源码根目录是\n"
            "   /workspace。定位源码必须使用 Path.cwd()/process.cwd()/getcwd() 或绝对 /workspace 路径，\n"
            "   禁止用 __file__ 所在的 _agent_tests 目录拼接 main.py、app.py 等项目文件。\n"
            "   - python 项目: 文件顶层或 __main__ 里调用断言,失败用 raise AssertionError;退出码非 0 表示失败。\n"
            "   - node 项目: 必须用 node:assert/throw 抛错，禁止 console.assert；失败必须以非 0 退出。\n"
            "   - php 项目: 用 assert() 或抛出异常,非 0 退出表示失败。\n"
            "   - go 项目: 单个 main 包文件,用 panic/fmt 后 os.Exit(1) 表示失败。\n"
            "   - java 项目: 单个 public class 含 main,失败 System.exit(1)。\n"
            "3. 黑盒模式额外生成一个探测脚本,文件扩展名与项目语言一致:php→blackbox.php、\n"
            "   python→blackbox.py(urllib)、node→blackbox.js;探测脚本请求 http://127.0.0.1:{port}\n"
            "   (端口必须用环境变量 PRISM_PREVIEW_PORT 动态获取,禁止写死端口),在应用稳定运行后\n"
            "   执行,必须包含 DAST 注入渗透探测:SQL 注入(带 ' OR '1'='1 等 payload)、\n"
            "   XSS(payload 回显检测)、SSRF(仅限 127.0.0.1 回环探测)、越权(未授权访问受保护\n"
            "   路径)、目录/错误页泄露探测;每个探测记录状态码与响应片段,断言安全基线(如\n"
            "   敏感错误不泄露),失败以 AssertionError/抛出异常表示。\n"
            "   所有查询参数必须先做 URL 编码:Python urllib 使用 urllib.parse.urlencode/quote,\n"
            "   Node 使用 URLSearchParams/encodeURIComponent,PHP 使用 http_build_query/rawurlencode;\n"
            "   禁止把含空格、引号或控制字符的 payload 直接拼接进 URL。\n"
            "   php 项目黑盒脚本模板(必须照此结构,URL 用 getenv 动态拼接,不能写死):\n"
            "   <?php\n"
            "   $port = getenv('PRISM_PREVIEW_PORT') ?: '8080';\n"
            "   $base = 'http://127.0.0.1:' . $port;\n"
            "   function req($url) { $ch = curl_init($url); "
            "curl_setopt_array($ch, [CURLOPT_RETURNTRANSFER=>true, "
            "CURLOPT_FOLLOWLOCATION=>false, CURLOPT_TIMEOUT=>5]); "
            "$body = curl_exec($ch); $code = curl_getinfo($ch, CURLINFO_HTTP_CODE); "
            "curl_close($ch); return [$code, (string)$body]; }\n"
            "   list($code, $body) = req($base . '/');\n"
            "   if (!$code) throw new RuntimeException('app unreachable');\n"
            "   // SQL注入/XSS/路径探测等,用 $base 拼接目标\n"
            "   // 全部断言通过则 exit(0),否则抛出异常 exit(1)\n"
            "   ?>\n"
            "4. 预期状态码、响应正文、响应头和长度必须由源码常量、计算表达式或实际响应推导;\n"
            "   禁止硬编码猜测 Content-Length 等数值。需要校验长度时必须使用 len(expected_body)\n"
            "   或等价计算,不得写死未经源码事实支持的数字。\n"
            "5. 若「源码摘要」包含 previous_generation_feedback 或 previous_execution_feedback,\n"
            "   必须改变本轮生成方案并逐条消除已知失败,禁止重复上一轮的无效用例。\n"
            "6. 只生成测试代码,不生成 shell 命令;不读取环境密钥;不做网络外联(黑盒只访问本机回环端口)。\n"
            '7. 输出 JSON 格式: {"files": [{"path": "test_ai_1.<项目语言扩展名>", "content": "..."}]}\n'
        )
        # LLM 偶发返回空/限流:重试最多 3 次,提升动态用例生成成功率
        import time as _time

        agent_result = None
        last_error = "生成失败"
        for _attempt in range(3):
            if deadline is not None and time.monotonic() >= deadline:
                return {"error": "测试用例生成超时"}
            try:
                agent_result = self.call_json(user_message, ctx=ctx)
                if getattr(agent_result, "success", False):
                    break
                last_error = str(getattr(agent_result, "error", "生成失败"))[:300]
            except Exception as exc:  # noqa: BLE001 - 重试后仍失败由调用方降级
                last_error = str(exc)[:300]
            _time.sleep(2.0)
        if agent_result is None or not getattr(agent_result, "success", False):
            return {"error": last_error}
        data = getattr(agent_result, "data", None)
        files = data.get("files") if isinstance(data, dict) else None
        if not isinstance(files, list):
            return {"error": "生成结果缺少 files 数组"}
        expected_extension = _LANGUAGE_EXTENSION.get(language)
        if expected_extension is None:
            return {"error": "不支持的测试语言"}
        cleaned: list[dict[str, str]] = []
        seen_paths: set[str] = set()
        unsupported = _grounding_feedback(files, source_summary)
        if unsupported and "previous_generation_feedback" not in source_summary:
            # 只纠错一次：用反馈重生成，避免无限循环。
            return self.generate(
                language=language,
                test_mode=test_mode,
                source_summary=_with_grounding_feedback(source_summary, unsupported),
                db_type=db_type,
                ctx=ctx,
                deadline=deadline,
            )
        if unsupported:
            return {"error": "生成测试引用了源码摘要中不存在的符号: " + ", ".join(unsupported)}
        if len(files) > MAX_FILES:
            return {"error": f"测试文件超过上限 {MAX_FILES}"}
        for item in files:
            if not isinstance(item, dict):
                return {"error": "测试文件项格式无效"}
            path = str(item.get("path") or "").strip()
            content = str(item.get("content") or "")
            if not path or not content or ".." in path or "/" in path:
                return {"error": "测试文件路径或内容无效"}
            if not re.fullmatch(r"[A-Za-z0-9_.-]+", path):
                return {"error": "测试文件名包含非法字符"}
            if not path.endswith(expected_extension):
                return {"error": "测试文件扩展名与项目语言不一致"}
            if path in seen_paths:
                return {"error": "测试文件名重复"}
            if len(content.encode("utf-8", errors="ignore")) > MAX_FILE_BYTES:
                return {"error": "测试文件内容超过大小上限"}
            seen_paths.add(path)
            cleaned.append({"path": path, "content": content})
        blackbox_name = f"blackbox{expected_extension}"
        blackbox_count = sum(1 for item in cleaned if item["path"] == blackbox_name)
        whitebox_count = len(cleaned) - blackbox_count
        if test_mode == "whitebox":
            if blackbox_count or not 2 <= whitebox_count <= 4:
                return {"error": "whitebox 模式必须包含 2-4 个白盒文件且不能包含 blackbox"}
        elif test_mode == "blackbox":
            if blackbox_count != 1 or whitebox_count:
                return {"error": f"blackbox 模式必须且只能包含 {blackbox_name}"}
        elif test_mode == "combined":
            if blackbox_count != 1 or not 2 <= whitebox_count <= 4:
                return {"error": "combined 模式必须包含 2-4 个白盒文件和 1 个 blackbox 文件"}
        else:
            return {"error": "不支持的测试模式"}
        return {"files": cleaned}
