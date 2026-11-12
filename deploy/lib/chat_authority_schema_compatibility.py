#!/usr/bin/env python3
"""Fail-closed comparison of installed MySQL trigger bodies with migration 119."""
from __future__ import annotations

import argparse
import ast
import json
import pathlib
import re
import sys
from typing import Any

TRIGGER_NAMES = {
    "trg_ent_chat_authority_validate": "INSERT",
    "trg_ent_chat_authority_immutable": "UPDATE",
    "trg_ent_chat_authority_delete": "DELETE",
}
_SQL_TOKEN = re.compile(
    r"'(?:''|[^'])*'|\"(?:\"\"|[^\"])*\"|`(?:``|[^`])*`|"
    r"<=>|<=|>=|<>|!=|:=|[A-Za-z_][A-Za-z0-9_$]*|[0-9]+(?:\.[0-9]+)?|[^\s]",
)
_TRIGGER_HEADER = re.compile(
    r"^CREATE\s+TRIGGER\s+(?P<name>[A-Za-z0-9_]+)\s+BEFORE\s+"
    r"(?P<event>INSERT|UPDATE|DELETE)\s+ON\s+enterprise_chat_message\s+"
    r"FOR\s+EACH\s+ROW\s+(?P<body>.+)$",
    re.IGNORECASE | re.DOTALL,
)


def _literal_string(node: ast.AST) -> str:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return _literal_string(node.left) + _literal_string(node.right)
    raise ValueError("migration trigger SQL must be a literal string")


def expected_triggers(migration_path: pathlib.Path) -> dict[str, dict[str, str]]:
    tree = ast.parse(migration_path.read_text(encoding="utf-8"))
    upgrade = next(
        node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "upgrade"
    )
    expected: dict[str, dict[str, str]] = {}
    for node in ast.walk(upgrade):
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "op"
            and node.func.attr == "execute"
            and node.args
        ):
            continue
        statement = _literal_string(node.args[0])
        match = _TRIGGER_HEADER.match(statement)
        if not match:
            continue
        name = match.group("name")
        if name not in TRIGGER_NAMES or match.group("event").upper() != TRIGGER_NAMES[name]:
            continue
        expected[name] = {
            "action_timing": "BEFORE",
            "event_manipulation": TRIGGER_NAMES[name],
            "action_statement": match.group("body").strip(),
        }
    if set(expected) != set(TRIGGER_NAMES):
        raise ValueError("migration 119 does not define the expected MySQL trigger set")
    return expected


def _canonical_sql(statement: str) -> tuple[str, ...]:
    # Ignore formatting and keyword case while preserving quoted SQL literals exactly.
    return tuple(
        token if token.startswith(("'", '"', "`")) else token.casefold()
        for token in _SQL_TOKEN.findall(statement)
    )


def classify_schema(actual: Any, expected: dict[str, dict[str, str]]) -> str:
    if not isinstance(actual, dict):
        raise ValueError("database schema snapshot must be an object")
    column_count = actual.get("column_count")
    triggers = actual.get("triggers")
    if type(column_count) is not int or not isinstance(triggers, list):
        raise ValueError("database schema snapshot is incomplete")

    if column_count == 0 and not triggers:
        return "absent"
    if column_count != 1:
        return "partial"

    by_name: dict[str, dict[str, str]] = {}
    for trigger in triggers:
        if not isinstance(trigger, dict):
            return "partial"
        name = trigger.get("trigger_name")
        if name not in expected or name in by_name:
            return "partial"
        timing = trigger.get("action_timing")
        event = trigger.get("event_manipulation")
        statement = trigger.get("action_statement")
        if not all(isinstance(value, str) for value in (timing, event, statement)):
            return "partial"
        by_name[name] = {
            "action_timing": timing.upper(),
            "event_manipulation": event.upper(),
            "action_statement": statement,
        }
    if set(by_name) != set(expected):
        return "partial"

    for name, expected_definition in expected.items():
        actual_definition = by_name[name]
        if (actual_definition["action_timing"] != expected_definition["action_timing"]
                or actual_definition["event_manipulation"] != expected_definition["event_manipulation"]
                or _canonical_sql(actual_definition["action_statement"])
                != _canonical_sql(expected_definition["action_statement"])):
            return "partial"
    return "complete"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("migration", type=pathlib.Path)
    parser.add_argument("--fixture", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    try:
        expected = expected_triggers(args.migration)
        if args.fixture:
            print(json.dumps({"column_count": 1, "triggers": [
                {"trigger_name": name, **definition} for name, definition in expected.items()
            ]}, ensure_ascii=False))
            return 0
        snapshot = json.load(sys.stdin)
        print(classify_schema(snapshot, expected))
        return 0
    except (OSError, StopIteration, SyntaxError, ValueError, json.JSONDecodeError) as exc:
        print(f"chat authority schema probe failed: {type(exc).__name__}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
