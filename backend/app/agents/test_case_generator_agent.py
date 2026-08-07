"""沙箱黑白盒测试用例生成 Agent。

在测试执行前调用:基于项目源码摘要,为指定语言生成可直接执行的
自包含断言测试文件(不生成 shell 命令,只注入数据文件),由沙箱镜像
内置 runner 确定性执行,保持 fail-closed 隔离。
"""
from __future__ import annotations

import json
import re
from typing import Any, Optional

from app.agents.base import AgentContext, BaseAgent

MAX_FILES = 8
MAX_FILE_BYTES = 60_000


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
            f"{json.dumps(source_summary, ensure_ascii=False, default=str)[:12000]}\n\n"
            "要求:\n"
            "1. 生成 2-4 个自包含断言测试文件,覆盖核心逻辑、边界与关键入口;\n"
            "   每个文件控制在 10-50 行,精简断言,避免超长输出。\n"
            "2. 所有测试文件必须使用项目语言(上方「语言」字段)编写,禁止使用其他语言:\n"
            "   - python 项目: 文件顶层或 __main__ 里调用断言,失败用 raise AssertionError;退出码非 0 表示失败。\n"
            "   - node 项目: 用 node:assert 或 console.assert,非 0 退出表示失败。\n"
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
            "   php 项目黑盒脚本模板(必须照此结构,URL 用 getenv 动态拼接,不能写死):\n"
            "   <?php\n"
            "   $port = getenv('PRISM_PREVIEW_PORT') ?: '8080';\n"
            "   $base = 'http://127.0.0.1:' . $port;\n"
            "   function req($url) { $ch = curl_init($url); curl_setopt_array($ch, [CURLOPT_RETURNTRANSFER=>true, CURLOPT_FOLLOWLOCATION=>false, CURLOPT_TIMEOUT=>5]); $body = curl_exec($ch); $code = curl_getinfo($ch, CURLINFO_HTTP_CODE); curl_close($ch); return [$code, (string)$body]; }\n"
            "   list(\$code, \$body) = req(\$base . '/');\n"
            "   if (!\$code) throw new RuntimeException('app unreachable');\n"
            "   // SQL注入/XSS/路径探测等,用 \$base 拼接目标\n"
            "   // 全部断言通过则 exit(0),否则抛出异常 exit(1)\n"
            "   ?>\n"
            "4. 只生成测试代码,不生成 shell 命令;不读取环境密钥;不做网络外联(黑盒只访问本机回环端口)。\n"
            "5. 输出 JSON 格式: {\"files\": [{\"path\": \"test_ai_1.<项目语言扩展名>\", \"content\": \"...\"}]}\n"
        )
        try:
            agent_result = self.call_json(user_message, ctx=ctx)
        except Exception as exc:  # noqa: BLE001 - 生成失败由调用方降级
            return {"error": str(exc)[:300]}
        if not getattr(agent_result, "success", False):
            return {"error": str(getattr(agent_result, "error", "生成失败"))[:300]}
        data = getattr(agent_result, "data", None)
        files = data.get("files") if isinstance(data, dict) else None
        if not isinstance(files, list):
            return {"error": "生成结果缺少 files 数组"}
        cleaned: list[dict[str, str]] = []
        for item in files[:MAX_FILES]:
            if not isinstance(item, dict):
                continue
            path = str(item.get("path") or "").strip()
            content = str(item.get("content") or "")
            if not path or not content or ".." in path or "/" in path:
                continue
            if not re.fullmatch(r"[A-Za-z0-9_.-]+", path):
                continue
            if len(content.encode("utf-8", errors="ignore")) > MAX_FILE_BYTES:
                continue
            cleaned.append({"path": path, "content": content})
        if not cleaned:
            return {"error": "没有可用的测试用例文件"}
        return {"files": cleaned}
