"""沙箱 PHP 语法修复 Agent。

在沙箱白盒测试发现 Fatal/Parse error 后,由该 Agent 基于错误信息与文件内容
生成修复后的 PHP 文件,后端将修复结果写回源码 zip 后重跑沙箱,直到语法通过
或达到迭代上限。修复只作用于隔离沙箱的源码副本,不修改生产项目归档。
"""
from __future__ import annotations

import json
import re
from typing import Any, Optional

from app.agents.base import AgentContext, BaseAgent

MAX_FILE_BYTES = 40_000  # 单文件送入 LLM 的最大字节数(超出截断,保留错误行附近)
MAX_FILES_PER_ROUND = 8  # 每轮最多修复文件数,控制成本与超时


class SyntaxRepairAgent(BaseAgent):
    """针对 PHP 语法错误生成修复文件内容。"""

    name = "syntax_repair"
    description = "修复沙箱源码的 PHP 语法错误,支持错误文件内容重写"
    icon = "syntax_repair"
    color = "#7A5AF8"
    category = "deploy"

    def __init__(self) -> None:
        super().__init__(
            system_prompt=(
                "你是 PHP 语法修复专家。给定文件内容与 php -l 报告的语法错误,输出修复后"
                "的完整文件内容。只输出 JSON,不要输出其他内容。"
            ),
            temperature=0.1,
            max_tokens=12_000,
        )

    @staticmethod
    def _clip(content: str, line: Optional[int] = None) -> str:
        """保留错误行附近的代码窗口,同时控制总长度。"""
        lines = content.split("\n")
        if line is not None and 1 <= line <= len(lines):
            start = max(0, line - 40)
            end = min(len(lines), line + 40)
            clipped = lines[start:end]
            prefix = f"# 已裁剪:原始第 1-{start} 行省略" if start > 0 else ""
            suffix = f"# 已裁剪:原始第 {end+1}-{len(lines)} 行省略" if end < len(lines) else ""
            text = "\n".join(clipped)
            window = f"{prefix}\n{text}\n{suffix}" if prefix or suffix else text
        else:
            window = content
        if len(window.encode("utf-8", errors="ignore")) > MAX_FILE_BYTES:
            window = window[:MAX_FILE_BYTES]
        return window

    def repair(
        self,
        *,
        language: str,
        errors: list[dict[str, Any]],
        files: dict[str, str],
        ctx: Optional[AgentContext] = None,
    ) -> dict[str, Any]:
        """生成修复后的文件内容。

        Args:
            language: 项目语言(当前仅 php)
            errors: [{"file": "api/foo.php", "line": 454, "message": "..."}]
            files: {path: 文件内容(可裁剪)}
        Returns:
            {"files": {path: 修复后内容}} 或 {"error": "..."}
        """
        if language != "php" or not errors or not files:
            return {"error": "当前仅支持 PHP 语法修复"}
        # 按文件聚合错误
        by_file: dict[str, list[dict[str, Any]]] = {}
        for err in errors:
            f = str(err.get("file") or "").strip()
            if f:
                by_file.setdefault(f, []).append(err)
        # 只修复本轮文件列表中的(后端已按优先级选择)
        payload_files: dict[str, str] = {}
        payload_errors: list[dict[str, Any]] = []
        for path, content in files.items():
            clipped = self._clip(content, by_file.get(path, [{}])[0].get("line") if by_file.get(path) else None)
            payload_files[path] = clipped
            payload_errors.extend(by_file.get(path, []))
        user_message = (
            f"语言: {language}\n"
            f"php -l 语法错误(文件+行号+消息):\n{json.dumps(payload_errors, ensure_ascii=False)[:8000]}\n\n"
            "文件内容(错误行附近已窗口化):\n"
            f"{json.dumps(payload_files, ensure_ascii=False)[:60000]}\n\n"
            "要求:\n"
            "1. 对每个文件输出修复后的完整 PHP 文件内容(保留原文件业务逻辑,只修语法)。\n"
            "2. 兼容 PHP 8:如 'continue' 用于 switch/循环上下文、可选参数位置、大括号配对、\n"
            "   token 错误等,按 PHP 8 语法修正。\n"
            "3. 不要改动文件业务语义,不要新增删除功能,不要引入外部依赖。\n"
            "4. 若某文件无法确定修复,输出该文件的原始内容并注明(仍保持 JSON 结构)。\n"
            "5. 输出 JSON 格式: {\"files\": {\"<path>\": \"<修复后完整内容>\"}}\n"
        )
        agent_result = self.call_json(user_message, ctx=ctx)
        if not getattr(agent_result, "success", False):
            return {"error": str(getattr(agent_result, "error", "修复生成失败"))[:300]}
        data = agent_result.data if isinstance(agent_result.data, dict) else {}
        repaired = data.get("files") if isinstance(data.get("files"), dict) else {}
        cleaned: dict[str, str] = {}
        for path in files:
            content = repaired.get(path)
            if isinstance(content, str) and content.strip():
                cleaned[path] = content
        if not cleaned:
            return {"error": "未生成有效修复文件"}
        return {"files": cleaned}


def collect_php_lint_errors(log_text: str) -> list[dict[str, Any]]:
    """从沙箱日志提取 php -l 的 Fatal/Parse error。

    兼容两种输出:
      Parse error: syntax error, ... in ./api/foo.php on line 454
      Fatal error: ... in ./api/foo.php on line 343
    以及 'Errors parsing ./api/foo.php' 的二次定位。
    """
    errors: list[dict[str, Any]] = []
    seen: set[tuple[str, int, str]] = set()
    pattern = re.compile(
        r"(?:Parse error|Fatal error|Warning):\s*(?P<msg>.*?)\s+in\s+(?P<file>\S+?\.php)\s+on line\s+(?P<line>\d+)",
        re.I,
    )
    for m in pattern.finditer(log_text):
        file = m.group("file").lstrip("./")
        line = int(m.group("line"))
        message = m.group("msg").strip()[:300]
        key = (file, line, message[:80])
        if key in seen:
            continue
        seen.add(key)
        errors.append({"file": file, "line": line, "message": message})
    return errors
