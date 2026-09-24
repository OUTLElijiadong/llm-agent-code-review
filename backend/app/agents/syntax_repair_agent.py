"""沙箱 PHP 语法修复 Agent。

在沙箱白盒测试发现 Fatal/Parse error 后,由该 Agent 基于错误信息与文件内容
生成修复后的 PHP 文件,后端将修复结果写回源码 zip 后重跑沙箱,直到语法通过
或达到迭代上限。修复只作用于隔离沙箱的源码副本,不修改生产项目归档。
"""
from __future__ import annotations

import json
import re
from contextvars import copy_context
from typing import Any, Optional

from app.agents.base import AgentContext, BaseAgent

MAX_FILE_BYTES = 40_000  # 单次模型输入源码上限；超出时以唯一原文锚定补丁重建。
MAX_FILES_PER_ROUND = 8  # 每轮最多修复文件数,控制成本与超时


class SyntaxRepairAgent(BaseAgent):
    """针对 PHP 语法错误生成修复文件内容。"""

    name = "syntax_repair"
    description = "修复沙箱源码的 PHP 语法错误,支持错误文件内容重写"
    icon = "syntax_repair"
    color = "#7A5AF8"
    category = "deploy"

    def __init__(self) -> None:
        from app.core.config import settings
        super().__init__(
            system_prompt=(
                "你是 PHP 语法修复专家。给定文件内容与 php -l 报告的语法错误,输出修复后"
                "的完整文件内容。只输出 JSON,不要输出其他内容。"
            ),
            temperature=0.1,
            max_tokens=min(65_536, int(settings.deepseek_max_output_tokens)),
        )

    @staticmethod
    def _clip(content: str, line: Optional[int] = None) -> str:
        """保留错误附近的源代码；超出窗口时由调用方使用锚定补丁重建。"""
        if len(content.encode("utf-8")) <= MAX_FILE_BYTES:
            return content
        lines = content.split("\n")
        try:
            line = int(line) if line is not None else None
        except (TypeError, ValueError):
            line = None
        if line is not None and 1 <= line <= len(lines):
            start = max(0, line - 20)
            end = min(len(lines), line + 20)
            clipped = lines[start:end]
            prefix = f"# 已裁剪:原始第 1-{start} 行省略" if start > 0 else ""
            suffix = f"# 已裁剪:原始第 {end+1}-{len(lines)} 行省略" if end < len(lines) else ""
            text = "\n".join(clipped)
            window = f"{prefix}\n{text}\n{suffix}" if prefix or suffix else text
        else:
            window = content
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
            files: {path: 完整文件内容}
        Returns:
            {"files": {path: 修复后内容}} 或 {"error": "..."}
        """
        if language != "php" or not errors or not files:
            return {"error": "当前仅支持 PHP 语法修复"}
        # 大文件修复耗时,临时把单次模型超时提到 300s
        old_timeout = self._timeout
        self._timeout = max(float(old_timeout or 0), 300.0)
        # 按文件聚合错误
        by_file: dict[str, list[dict[str, Any]]] = {}
        for err in errors:
            f = str(err.get("file") or "").strip()
            if f:
                by_file.setdefault(f, []).append(err)
        # 每个文件单独一次 LLM 调用,避免多文件输出超长被截断;
        # 文件间并发(上限 2)缩短整体修复耗时,同时避免触发模型限流。
        from concurrent.futures import ThreadPoolExecutor

        def _repair_one(path: str, content: str) -> str | None:
            file_errors = by_file.get(path, [])
            if not file_errors:
                return None
            windowed = len(content.encode("utf-8")) > MAX_FILE_BYTES
            if windowed:
                # PHP lint 通常只报第一处；多个错误必须都展示，否则不能声称完整修复。
                windows = [self._clip(content, err.get("line")) for err in file_errors]
                clipped = "\n\n--- 错误窗口 ---\n\n".join(windows)
            else:
                clipped = content
            errors_json = json.dumps(file_errors, ensure_ascii=False)
            if len(clipped.encode("utf-8")) > MAX_FILE_BYTES or len(errors_json) > 4_000:
                return None
            output_contract = (
                "4. 当前只提供错误行附近的源码窗口。只输出 JSON: "
                '{"edits":[{"old":"原文件中唯一的原始片段","new":"替换片段"}]}。'
                "每条 old 必须是原文件中唯一出现的非空连续原文；不要输出整个文件。\n"
                if windowed else
                '4. 输出 JSON 格式: {"content": "<修复后完整内容>"}\n'
            )
            user_message = (
                f"语言: {language}\n"
                f"目标文件: {path}\n"
                f"php -l 语法错误(行号+消息):\n{errors_json}\n\n"
                "文件内容(省略标记仅表示未展示原文，禁止凭空重建省略区域):\n"
                f"{clipped}\n\n"
                "要求:\n"
                "1. 只修改语法错误，保留业务逻辑。\n"
                "2. 兼容 PHP 8:如 'continue' 用于 switch/循环上下文、可选参数位置、大括号配对、\n"
                "   token 错误等,按 PHP 8 语法修正。\n"
                "3. 不要改动业务语义,不要新增/删除功能,不要引入外部依赖。\n"
                f"{output_contract}"
            )
            agent_result = self.call_json(user_message, ctx=ctx)
            if not getattr(agent_result, "success", False):
                return None
            data = agent_result.data if isinstance(agent_result.data, dict) else {}
            if windowed:
                edits = data.get("edits")
                if not isinstance(edits, list) or not edits or len(edits) > 12:
                    return None
                repaired = content
                for edit in edits:
                    if not isinstance(edit, dict):
                        return None
                    old = edit.get("old")
                    new = edit.get("new")
                    if not isinstance(old, str) or not old or not isinstance(new, str):
                        return None
                    if (
                        old not in clipped or repaired.count(old) != 1
                        or len(old.encode("utf-8")) > MAX_FILE_BYTES
                        or len(new.encode("utf-8")) > MAX_FILE_BYTES
                    ):
                        return None
                    repaired = repaired.replace(old, new, 1)
                return repaired if repaired != content else None
            new_content = data.get("content")
            if isinstance(new_content, str) and new_content.strip():
                return new_content
            return None

        cleaned: dict[str, str] = {}
        last_error = ""
        try:
            with ThreadPoolExecutor(max_workers=min(2, len(files))) as pool:
                futures = {
                    pool.submit(copy_context().run, _repair_one, path, content): path
                    for path, content in files.items()
                }
                for fut in futures:
                    path = futures[fut]
                    try:
                        repaired_content = fut.result()
                    except Exception as exc:  # noqa: BLE001
                        last_error = f"{path}: {str(exc)[:120]}"
                        continue
                    if repaired_content:
                        cleaned[path] = repaired_content
                    else:
                        last_error = f"{path}: 未返回有效内容或补丁无法唯一定位"
        finally:
            self._timeout = old_timeout
        if len(cleaned) != len(files):
            return {"error": last_error or "部分文件未修复，本轮不写回"}
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
        message = m.group("msg").strip()
        key = (file, line, message[:80])
        if key in seen:
            continue
        seen.add(key)
        errors.append({"file": file, "line": line, "message": message})
    return errors
