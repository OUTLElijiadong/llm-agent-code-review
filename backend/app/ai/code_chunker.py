"""
代码分片与跨分片符号上下文。

对于可解析代码优先用 AST 建立符号和调用图，语法不完整时回退到可见的词法索引。
"""

from __future__ import annotations

import ast
import hashlib
import re
from dataclasses import dataclass, field
from typing import Iterable


@dataclass
class CodeChunk:
    """Code slice. Line ranges remain 0-based and half-open."""

    text: str
    start_line: int
    end_line: int
    context: str = ""
    context_fingerprint: str = ""
    symbol_names: tuple[str, ...] = ()
    diagnostics: tuple[str, ...] = ()


@dataclass(frozen=True)
class Symbol:
    name: str
    kind: str
    line_number: int = 0
    end_line: int = 0
    bases: tuple[str, ...] = ()


@dataclass(frozen=True)
class SymbolIndex:
    mode: str
    symbols: dict[str, Symbol] = field(default_factory=dict)
    call_edges: tuple[tuple[str, str], ...] = ()
    reference_edges: tuple[tuple[str, str], ...] = ()
    inheritance_edges: tuple[tuple[str, str], ...] = ()
    diagnostics: tuple[str, ...] = ()


_FUNC_PATTERNS = {
    "python": re.compile(r"^[ \t]*(def|class)[ \t]+\w+", re.MULTILINE),
    "java": re.compile(
        r"^[ \t]*(public|private|protected|static|final|class)[ \t]+.+[{(]?[ \t]*$",
        re.MULTILINE,
    ),
    "javascript": re.compile(
        r"^[ \t]*(function[ \t]+\w+|const[ \t]+\w+[ \t]*=[ \t]*\(|class[ \t]+\w+)",
        re.MULTILINE,
    ),
    "typescript": re.compile(
        r"^[ \t]*(function[ \t]+\w+|const[ \t]+\w+[ \t]*=[ \t]*\(|class[ \t]+\w+)",
        re.MULTILINE,
    ),
    "go": re.compile(r"^[ \t]*func[ \t]+\w+", re.MULTILINE),
    "cpp": re.compile(r"^[ \t]*\w[\w \t*&]*[ \t]+\w+[ \t]*\(.*\)[ \t]*\{?", re.MULTILINE),
}


def chunk_code(content: str, language: str, threshold: int = 6000) -> list[CodeChunk]:
    """Split at definitions and split a single oversized definition by lines."""
    threshold = max(1, int(threshold or 1))
    if len(content) <= threshold:
        return [CodeChunk(text=content, start_line=0, end_line=_line_count(content))]

    lines = content.splitlines(keepends=True)
    pattern = _FUNC_PATTERNS.get((language or "").lower())
    if pattern:
        boundaries = _find_function_boundaries(content, pattern)
        if boundaries:
            return _split_by_boundaries(lines, boundaries, threshold)
    return _enforce_threshold(_split_by_lines(lines, lines_per_chunk=200), threshold)


def _find_function_boundaries(content: str, pattern: re.Pattern) -> list[int]:
    """Find definition start lines (0-based)."""
    return [content.count("\n", 0, match.start()) for match in pattern.finditer(content)]


def _split_by_boundaries(lines: list[str], boundaries: list[int], threshold: int) -> list[CodeChunk]:
    """Keep headers and split any single oversized definition without data loss."""
    starts = sorted(set(boundaries)) + [len(lines)]
    chunks: list[CodeChunk] = []
    if starts[0] > 0:
        head = "".join(lines[: starts[0]])
        if head.strip():
            chunks.extend(_split_text_window(head, 0, threshold))
    cursor = 0
    while cursor < len(starts) - 1:
        start_line, end_line = starts[cursor], starts[cursor + 1]
        text = "".join(lines[start_line:end_line])
        if len(text) <= threshold:
            while cursor + 2 < len(starts):
                next_text = "".join(lines[end_line: starts[cursor + 2]])
                if len(text) + len(next_text) > threshold:
                    break
                text += next_text
                cursor += 1
                end_line = starts[cursor + 1]
            chunks.append(CodeChunk(text=text, start_line=start_line, end_line=end_line))
        else:
            chunks.extend(_split_text_window(text, start_line, threshold))
        cursor += 1
    return chunks


def _split_text_window(text: str, start_line: int, threshold: int) -> list[CodeChunk]:
    """Split a large definition by complete lines, allowing one long line."""
    if len(text) <= threshold:
        return [CodeChunk(text=text, start_line=start_line, end_line=start_line + _line_count(text))]
    lines = text.splitlines(keepends=True)
    out: list[CodeChunk] = []
    offset = 0
    while offset < len(lines):
        if len(lines[offset]) > threshold:
            # 单行超过模型预算时只能字符切分；所有片段仍指向同一源码行。
            line = lines[offset]
            for start in range(0, len(line), threshold):
                out.append(CodeChunk(
                    line[start: start + threshold],
                    start_line + offset,
                    start_line + offset + 1,
                ))
            offset += 1
            continue
        size, end = 0, offset
        while end < len(lines) and (size + len(lines[end]) <= threshold or end == offset):
            size += len(lines[end])
            end += 1
        part = "".join(lines[offset:end])
        out.append(CodeChunk(part, start_line + offset, start_line + end))
        offset = end
    return out


def _enforce_threshold(chunks: list[CodeChunk], threshold: int) -> list[CodeChunk]:
    """对词法回退片段应用同样的字符上限。"""
    out: list[CodeChunk] = []
    for chunk in chunks:
        out.extend(_split_text_window(chunk.text, chunk.start_line, threshold))
    return out


def _split_by_lines(lines: list[str], lines_per_chunk: int) -> list[CodeChunk]:
    """Fallback fixed line windows."""
    out: list[CodeChunk] = []
    for i in range(0, len(lines), lines_per_chunk):
        chunk_lines = lines[i: i + lines_per_chunk]
        out.append(CodeChunk("".join(chunk_lines), i, i + len(chunk_lines)))
    return out


def _line_count(text: str) -> int:
    return len(text.splitlines()) if text else 0


def build_symbol_index(content: str, language: str = "python") -> SymbolIndex:
    """Build deterministic symbols, call edges and inheritance edges."""
    if (language or "").lower() not in {"python", "py", "python3"}:
        return _build_lexical_index(content, language)
    try:
        tree = ast.parse(content)
    except (SyntaxError, ValueError, TypeError) as exc:
        return _build_lexical_index(content, language, diagnostics=(f"AST parse failed: {exc}",))

    symbols: dict[str, Symbol] = {}
    function_nodes: list[tuple[str, ast.FunctionDef | ast.AsyncFunctionDef]] = []
    inheritance: set[tuple[str, str]] = set()
    aliases = _python_import_aliases(tree)
    _DefinitionCollector(symbols, function_nodes, inheritance).visit(tree)
    calls: set[tuple[str, str]] = set()
    references: set[tuple[str, str]] = set()
    for owner, node in function_nodes:
        visitor = _FunctionUseCollector(owner, symbols, calls, references, aliases)
        for statement in node.body:
            visitor.visit(statement)
    return SymbolIndex(
        mode="ast",
        symbols=dict(sorted(symbols.items())),
        call_edges=tuple(sorted(calls)),
        reference_edges=tuple(sorted(references)),
        inheritance_edges=tuple(sorted(inheritance)),
    )


class _DefinitionCollector(ast.NodeVisitor):
    """First pass: collect every definition before resolving forward calls."""

    def __init__(
        self,
        symbols: dict[str, Symbol],
        function_nodes: list[tuple[str, ast.FunctionDef | ast.AsyncFunctionDef]],
        inheritance: set[tuple[str, str]],
    ) -> None:
        self.symbols = symbols
        self.function_nodes = function_nodes
        self.inheritance = inheritance
        self.scope: list[str] = []

    def _qualified(self, name: str) -> str:
        return ".".join((*self.scope, name))

    def _add(self, name: str, kind: str, node: ast.AST, bases: tuple[str, ...] = ()) -> None:
        qualified = self._qualified(name)
        self.symbols[qualified] = Symbol(
            qualified,
            kind,
            int(getattr(node, "lineno", 0) or 0),
            int(getattr(node, "end_lineno", getattr(node, "lineno", 0)) or 0),
            bases,
        )

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        bases = tuple(_expr_name(base) for base in node.bases if _expr_name(base))
        name = self._qualified(node.name)
        self._add(node.name, "class", node, bases)
        self.inheritance.update((name, base) for base in bases)
        self.scope.append(node.name)
        for statement in node.body:
            self.visit(statement)
        self.scope.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._visit_function(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._visit_function(node)

    def _visit_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        owner = self._qualified(node.name)
        self._add(node.name, "function", node)
        self.function_nodes.append((owner, node))
        self.scope.append(node.name)
        arguments = (*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs)
        if node.args.vararg:
            arguments = (*arguments, node.args.vararg)
        if node.args.kwarg:
            arguments = (*arguments, node.args.kwarg)
        for argument in arguments:
            self._add(argument.arg, "parameter", argument)
        for statement in node.body:
            self.visit(statement)
        self.scope.pop()

    def visit_Assign(self, node: ast.Assign) -> None:
        for target in node.targets:
            for name in _assignment_names(target):
                self._add(name, "variable", target)
        self.generic_visit(node.value)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        for name in _assignment_names(node.target):
            self._add(name, "variable", node.target)
        if node.value:
            self.visit(node.value)

    def visit_For(self, node: ast.For) -> None:
        for name in _assignment_names(node.target):
            self._add(name, "variable", node.target)
        self.generic_visit(node)

    def visit_AsyncFor(self, node: ast.AsyncFor) -> None:
        self.visit_For(node)

    def visit_Import(self, node: ast.Import) -> None:
        for item in node.names:
            self._add(item.asname or item.name.split(".", 1)[0], "import", node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        for item in node.names:
            self._add(item.asname or item.name, "import", node)


class _FunctionUseCollector(ast.NodeVisitor):
    """Second pass: collect only the current function's calls and references."""

    def __init__(
        self,
        owner: str,
        symbols: dict[str, Symbol],
        calls: set[tuple[str, str]],
        references: set[tuple[str, str]],
        aliases: dict[str, str],
    ) -> None:
        self.owner = owner
        self.symbols = symbols
        self.calls = calls
        self.references = references
        self.aliases = aliases

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        return

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        return

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        return

    def visit_Call(self, node: ast.Call) -> None:
        raw = _expr_name(node.func)
        if raw:
            canonical = _canonical_name(raw, self.aliases)
            self.calls.add((self.owner, _resolve_symbol(canonical, self.owner, self.symbols) or canonical))
        self.generic_visit(node)

    def visit_Name(self, node: ast.Name) -> None:
        if not isinstance(node.ctx, ast.Load):
            return
        target = _resolve_symbol(
            node.id,
            self.owner,
            self.symbols,
            kinds={"variable", "parameter", "import"},
        )
        if target:
            self.references.add((self.owner, target))


def _assignment_names(node: ast.AST) -> tuple[str, ...]:
    if isinstance(node, ast.Name):
        return (node.id,)
    if isinstance(node, (ast.Tuple, ast.List)):
        return tuple(name for child in node.elts for name in _assignment_names(child))
    return ()


def _resolve_symbol(
    raw: str,
    owner: str,
    symbols: dict[str, Symbol],
    kinds: set[str] | None = None,
) -> str:
    candidates = {
        name for name, symbol in symbols.items()
        if (kinds is None or symbol.kind in kinds) and (name == raw or name.endswith(f".{raw}"))
    }
    if not candidates:
        return ""
    owner_parts = owner.split(".")
    scope_candidates = [
        ".".join((*owner_parts[:depth], raw))
        for depth in range(len(owner_parts), -1, -1)
    ]
    for candidate in scope_candidates:
        if candidate in candidates:
            return candidate
    return sorted(candidates, key=lambda value: (value.count("."), value))[0]


def _python_import_aliases(tree: ast.AST) -> dict[str, str]:
    aliases: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for item in node.names:
                aliases[item.asname or item.name.split(".", 1)[0]] = item.name
        elif isinstance(node, ast.ImportFrom):
            prefix = node.module or ""
            for item in node.names:
                aliases[item.asname or item.name] = f"{prefix}.{item.name}".strip(".")
    return aliases


def _expr_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        parent = _expr_name(node.value)
        return f"{parent}.{node.attr}" if parent else node.attr
    return ""


def _canonical_name(name: str, aliases: dict[str, str]) -> str:
    if not name:
        return name
    head, *rest = name.split(".")
    return ".".join([aliases.get(head, head), *rest])


def _build_lexical_index(content: str, language: str, diagnostics: tuple[str, ...] = ()) -> SymbolIndex:
    symbols: dict[str, Symbol] = {}
    pattern = _FUNC_PATTERNS.get((language or "").lower(), _FUNC_PATTERNS["python"])
    for line_no, line in enumerate(content.splitlines(), 1):
        if not pattern.match(line):
            continue
        match = re.search(r"(?:def|class|function|func)\s+([A-Za-z_]\w*)", line)
        if match:
            name = match.group(1)
            kind = "class" if "class" in line else "function"
            symbols.setdefault(name, Symbol(name, kind, line_no, line_no))
    return SymbolIndex(mode="lexical", symbols=dict(sorted(symbols.items())), diagnostics=diagnostics)


def chunk_code_with_context(content: str, language: str, threshold: int = 6000) -> list[CodeChunk]:
    """Split code and attach bounded, relation-aware cross-slice context."""
    chunks = chunk_code(content, language, threshold)
    index = build_symbol_index(content, language)
    result: list[CodeChunk] = []
    for chunk in chunks:
        names = _symbols_in_range(index.symbols.values(), chunk.start_line + 1, chunk.end_line)
        context = _render_index_context(
            index,
            local_names=tuple(names),
            max_chars=max(512, min(6000, threshold // 2)),
        )
        fingerprint = hashlib.sha256(context.encode("utf-8")).hexdigest()
        result.append(CodeChunk(
            text=chunk.text,
            start_line=chunk.start_line,
            end_line=chunk.end_line,
            context=context,
            context_fingerprint=fingerprint,
            symbol_names=tuple(names),
            diagnostics=index.diagnostics,
        ))
    return result


def _symbols_in_range(symbols: Iterable[Symbol], start_line: int, end_line: int) -> list[str]:
    return sorted(symbol.name for symbol in symbols if symbol.line_number <= end_line and symbol.end_line >= start_line)


def _render_index_context(
    index: SymbolIndex,
    local_names: tuple[str, ...] = (),
    max_chars: int = 4000,
) -> str:
    related = set(local_names)
    edges = (*index.call_edges, *index.reference_edges, *index.inheritance_edges)
    for owner, target in edges:
        if any(_symbol_related(value, owner) or _symbol_related(value, target) for value in local_names):
            related.update((owner, target))
    if not related:
        related.update(name for name in index.symbols if "." not in name)
    lines = [
        f"symbol_index_mode: {index.mode}",
        (
            f"symbol_index_counts: symbols={len(index.symbols)} "
            f"calls={len(index.call_edges)} refs={len(index.reference_edges)} "
            f"inheritance={len(index.inheritance_edges)}"
        ),
    ]

    def symbol_line(symbol: Symbol) -> str:
        if symbol.kind == "class":
            suffix = f"({', '.join(symbol.bases)})" if symbol.bases else ""
            return f"{symbol.name}{suffix}"
        if symbol.kind in {"variable", "parameter", "import"}:
            return f"{symbol.kind} {symbol.name}@L{symbol.line_number}"
        return symbol.name

    visible_symbols = [
        symbol for symbol in sorted(index.symbols.values(), key=lambda item: item.name)
        if any(_symbol_related(symbol.name, value) for value in related)
    ]
    lines.extend(
        symbol_line(symbol)
        for symbol in visible_symbols
        if symbol.kind in {"class", "function"}
        and any(_symbol_related(symbol.name, value) for value in local_names)
    )
    lines.extend(
        f"{owner} -> {target}"
        for owner, target in index.call_edges
        if any(_symbol_related(value, owner) or _symbol_related(value, target) for value in local_names)
    )
    lines.extend(
        f"{owner} => {target}"
        for owner, target in index.reference_edges
        if any(_symbol_related(value, owner) or _symbol_related(value, target) for value in local_names)
    )
    lines.extend(
        symbol_line(symbol)
        for symbol in visible_symbols
        if not (
            symbol.kind in {"class", "function"}
            and any(_symbol_related(symbol.name, value) for value in local_names)
        )
    )
    rendered: list[str] = []
    used = 0
    for line in lines:
        addition = len(line) + (1 if rendered else 0)
        if rendered and used + addition > max_chars:
            rendered.append("symbol_index_truncated: true")
            break
        rendered.append(line)
        used += addition
    return "\n".join(rendered)


def _symbol_related(left: str, right: str) -> bool:
    return left == right or left.startswith(f"{right}.") or right.startswith(f"{left}.")


__all__ = [
    "CodeChunk",
    "Symbol",
    "SymbolIndex",
    "build_symbol_index",
    "chunk_code",
    "chunk_code_with_context",
]
