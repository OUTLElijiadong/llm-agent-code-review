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
        super().__init__(
            system_prompt=(
                "你是代码测试用例生成器。根据给定源码摘要与语言,生成可直接执行的自包含断言测试脚本。"
                "只输出 JSON,不要输出其他内容。"
            ),
            temperature=0.2,
            max_tokens=16_000,
        )

    def generate(
        self,
        *,
        language: str,
        test_mode: str,
        source_summary: dict[str, Any],
        ctx: Optional[AgentContext] = None,
    ) -> dict[str, Any]:
        """生成测试用例文件列表。

        Returns:
            dict: {"files": [{"path": "test_ai_xxx", "content": "..."}]} 或 {"error": "..."}
        """
        user_message = (
            f"语言: {language}\n"
            f"测试模式: {test_mode}\n"
            "源码摘要(JSON):\n"
            f"{json.dumps(source_summary, ensure_ascii=False, default=str)[:12000]}\n\n"
            "要求:\n"
            "1. 生成 2-5 个自包含断言测试文件,覆盖核心逻辑、边界与关键入口。\n"
            "2. 每个文件必须可直接执行(不是库文件):\n"
            "   - python: 文件顶层或 __main__ 里调用断言,失败用 raise AssertionError;退出码非 0 表示失败。\n"
            "   - node: 用 node:assert 或 console.assert,非 0 退出表示失败。\n"
            "   - php: 用 assert() 或抛出异常,非 0 退出表示失败。\n"
            "   - go: 单个 main 包文件,用 panic/fmt 后 os.Exit(1) 表示失败。\n"
            "   - java: 单个 public class 含 main,失败 System.exit(1)。\n"
            "3. 黑盒模式额外生成一个 blackbox.py(用 urllib 请求 http://127.0.0.1:{port},\n"
            "   端口取环境变量 PRISM_PREVIEW_PORT)。黑盒脚本在应用稳定运行后执行,\n"
            "   必须包含 DAST 注入渗透探测:SQL 注入(带 ' OR '1'='1 等 payload)、\n"
            "   XSS(payload 回显检测)、SSRF(仅限 127.0.0.1 回环探测)、越权(未授权访问\n"
            "   受保护路径)、目录/错误页泄露探测;每个探测记录状态码与响应片段,断言安全\n"
            "   基线(如敏感错误不泄露),失败以 AssertionError 表示。\n"
            "4. 只生成测试代码,不生成 shell 命令;不读取环境密钥;不做网络外联(黑盒只访问本机回环端口)。\n"
            "5. 输出 JSON 格式: {\"files\": [{\"path\": \"test_ai_1.py\", \"content\": \"...\"}]}\n"
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
