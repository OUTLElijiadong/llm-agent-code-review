"""Check the pinned image's migration-119 chat authority contract without side effects.

119_chat_message_authority compatibility probe.
The probe reads source files from the target image and verifies the send-time rank
write plus fail-closed handling of legacy messages in both assistant paths. It does
not import the application, access a database, or use a network connection.
"""
from __future__ import annotations

import ast
import os
import pathlib
import signal
import sys


class ProbeTimeout(Exception):
    pass


def _timeout(_signum, _frame):
    raise ProbeTimeout("chat authority probe deadline exceeded")


def _find_class(tree: ast.Module, name: str) -> ast.ClassDef:
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == name:
            return node
    raise ValueError(f"missing class: {name}")


def _find_function(tree: ast.Module, name: str) -> ast.FunctionDef:
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    raise ValueError(f"missing function: {name}")


def _has_name(node: ast.AST, name: str) -> bool:
    return any(isinstance(item, ast.Name) and item.id == name for item in ast.walk(node))


def _has_string(node: ast.AST, value: str) -> bool:
    return any(isinstance(item, ast.Constant) and item.value == value for item in ast.walk(node))


def _checks_is_none(node: ast.AST, attribute: str) -> bool:
    return any(
        isinstance(item, ast.Compare)
        and isinstance(item.left, ast.Attribute)
        and item.left.attr == attribute
        and len(item.ops) == 1
        and isinstance(item.ops[0], ast.Is)
        and len(item.comparators) == 1
        and isinstance(item.comparators[0], ast.Constant)
        and item.comparators[0].value is None
        for item in ast.walk(node)
    )


def _is_rank_unknown_guard(node: ast.AST) -> bool:
    if not isinstance(node, ast.If):
        return False
    test = node.test
    if not (
        isinstance(test, ast.Compare)
        and isinstance(test.left, ast.Attribute)
        and isinstance(test.left.value, ast.Name)
        and test.left.value.id == "message"
        and test.left.attr == "sender_authority_rank"
        and len(test.ops) == 1
        and isinstance(test.ops[0], ast.Is)
        and len(test.comparators) == 1
        and isinstance(test.comparators[0], ast.Constant)
        and test.comparators[0].value is None
    ):
        return False
    sets_superseded = any(
        isinstance(statement, ast.Assign)
        and any(isinstance(target, ast.Name) and target.id == "superseded" for target in statement.targets)
        and isinstance(statement.value, ast.Constant)
        and statement.value.value is True
        for statement in node.body
    )
    return sets_superseded and any(isinstance(statement, ast.Break) for statement in node.body)


def _blocks_when_superseded(node: ast.AST) -> bool:
    return any(
        isinstance(item, ast.If)
        and isinstance(item.test, ast.Name)
        and item.test.id == "superseded"
        and any(
            isinstance(statement, ast.Raise)
            and isinstance(statement.exc, ast.Call)
            and isinstance(statement.exc.func, ast.Name)
            and statement.exc.func.id == "SupersededByAuthorityDirectiveError"
            for statement in item.body
        )
        for item in node.body
    )


def _calls_guard_directly(node: ast.AST, name: str) -> bool:
    return any(
        isinstance(statement, ast.Expr)
        and isinstance(statement.value, ast.Call)
        and isinstance(statement.value.func, ast.Name)
        and statement.value.func.id == name
        for statement in getattr(node, "body", [])
    )


def _probe(root: pathlib.Path) -> bool:
    models = ast.parse((root / "models/enterprise_chat.py").read_text(encoding="utf-8"))
    message = _find_class(models, "EnterpriseChatMessage")
    if not any(
        isinstance(item, ast.Assign)
        and any(isinstance(target, ast.Name) and target.id == "sender_authority_rank" for target in item.targets)
        for item in message.body
    ):
        return False

    chat = ast.parse((root / "services/enterprise_chat_service.py").read_text(encoding="utf-8"))
    send = _find_function(chat, "send_message")
    writes_rank = any(
        isinstance(item, ast.Call)
        and isinstance(item.func, ast.Name)
        and item.func.id == "EnterpriseChatMessage"
        and any(keyword.arg == "sender_authority_rank" and _has_name(keyword.value, "authority_rank")
                for keyword in item.keywords)
        for item in ast.walk(send)
    )
    if not writes_rank or not _has_string(send, "@小菱"):
        return False

    runtime = ast.parse((root / "services/enterprise_assistant_runtime_service.py").read_text(encoding="utf-8"))
    live_snapshot = _find_function(runtime, "_live_role_snapshot")
    runtime_guard = _find_function(runtime, "_assert_no_newer_authority_directive")
    if not _calls_guard_directly(live_snapshot, "_assert_no_newer_authority_directive"):
        return False
    if not any(
        isinstance(item, ast.For) and any(_is_rank_unknown_guard(child) for child in item.body)
        for item in runtime_guard.body
    ):
        return False
    if not _blocks_when_superseded(runtime_guard):
        return False

    clarification = ast.parse((root / "services/enterprise_assistant_clarification_service.py").read_text(
        encoding="utf-8",
    ))
    context = _find_function(clarification, "_context")
    if not _checks_is_none(context, "sender_authority_rank"):
        return False
    return _has_name(context, "superseding_directives") and _has_string(context, "authority_unknown")


def main() -> int:
    signal.signal(signal.SIGALRM, _timeout)
    signal.alarm(5)
    root = pathlib.Path(os.environ.get("PRISM_APP_ROOT", "/app/app"))
    try:
        supported = _probe(root)
    except (OSError, SyntaxError, ValueError, ProbeTimeout):
        supported = False
    finally:
        signal.alarm(0)
    print("supported" if supported else "unsupported")
    return 0


if __name__ == "__main__":
    sys.exit(main())
