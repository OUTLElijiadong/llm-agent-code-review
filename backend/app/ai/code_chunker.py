"""
代码分片模块: 超长文件按函数/行数拆分
"""
import re
from dataclasses import dataclass


@dataclass
class CodeChunk:
    """代码分片"""
    text: str
    start_line: int
    end_line: int


_FUNC_PATTERNS = {
    "python": re.compile(r"^\s*(def|class)\s+\w+", re.MULTILINE),
    "java": re.compile(r"^\s*(public|private|protected|static|final|class)\s.+[{(]?\s*$", re.MULTILINE),
    "javascript": re.compile(r"^\s*(function\s+\w+|const\s+\w+\s*=\s*\(|class\s+\w+)", re.MULTILINE),
    "typescript": re.compile(r"^\s*(function\s+\w+|const\s+\w+\s*=\s*\(|class\s+\w+)", re.MULTILINE),
    "go": re.compile(r"^\s*func\s+\w+", re.MULTILINE),
    "cpp": re.compile(r"^\s*\w[\w\s*&]*\s+\w+\s*\(.*\)\s*\{?", re.MULTILINE),
}


def chunk_code(content: str, language: str, threshold: int = 6000) -> list[CodeChunk]:
    """将代码按函数/类边界拆分为多个分片

    Args:
        content: 原始代码文本
        language: 编程语言标识
        threshold: 分片字符数阈值

    Returns:
        list[CodeChunk]: 分片列表,每个分片含文本和起始/结束行号
    """
    if len(content) <= threshold:
        return [CodeChunk(text=content, start_line=0, end_line=content.count("\n"))]

    lines = content.splitlines(keepends=True)
    pattern = _FUNC_PATTERNS.get(language)
    if pattern:
        boundaries = _find_function_boundaries(content, pattern)
        if boundaries:
            return _split_by_boundaries(lines, boundaries, threshold)

    return _split_by_lines(lines, lines_per_chunk=200)


def _find_function_boundaries(content: str, pattern: re.Pattern) -> list[int]:
    """查找函数/类定义的起始行号(0-based)"""
    positions = []
    for m in pattern.finditer(content):
        line_no = content.count("\n", 0, m.start())
        positions.append(line_no)
    return positions


def _split_by_boundaries(lines: list[str], boundaries: list[int], threshold: int) -> list[CodeChunk]:
    """按函数边界拆分,尽量合并不超过threshold的相邻函数"""
    chunks: list[CodeChunk] = []
    starts = boundaries + [len(lines)]
    cursor = 0
    while cursor < len(starts) - 1:
        start_line = starts[cursor]
        end_line = starts[cursor + 1]
        text = "".join(lines[start_line:end_line])
        while cursor + 2 < len(starts):
            next_text = "".join(lines[end_line:starts[cursor + 2]])
            if len(text) + len(next_text) > threshold:
                break
            text += next_text
            cursor += 1
            end_line = starts[cursor + 1]
        chunks.append(CodeChunk(text=text, start_line=start_line, end_line=end_line))
        cursor += 1
    if boundaries and boundaries[0] > 0:
        head_text = "".join(lines[0:boundaries[0]])
        if head_text.strip():
            chunks.insert(0, CodeChunk(text=head_text, start_line=0, end_line=boundaries[0]))
    return chunks


def _split_by_lines(lines: list[str], lines_per_chunk: int) -> list[CodeChunk]:
    """按行数均匀拆分(兜底策略)"""
    out: list[CodeChunk] = []
    for i in range(0, len(lines), lines_per_chunk):
        chunk_lines = lines[i: i + lines_per_chunk]
        text = "".join(chunk_lines)
        out.append(CodeChunk(text=text, start_line=i, end_line=i + len(chunk_lines)))
    return out
