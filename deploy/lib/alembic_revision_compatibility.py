"""Classify the order of two revisions in a pinned Alembic migration graph."""
from __future__ import annotations

import ast
import pathlib
import sys


def _literal_assignment(tree: ast.Module, name: str):
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == name for target in node.targets
        ):
            return ast.literal_eval(node.value)
        if (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id == name
            and node.value is not None
        ):
            return ast.literal_eval(node.value)
    raise ValueError(f"missing literal {name}")


def revision_relation(versions: pathlib.Path, target: str, current: str) -> str:
    parents: dict[str, tuple[str, ...]] = {}
    for path in versions.glob("*.py"):
        if path.name == "__init__.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        try:
            revision = _literal_assignment(tree, "revision")
        except ValueError:
            continue
        down_revision = _literal_assignment(tree, "down_revision")
        if not isinstance(revision, str) or not revision or revision in parents:
            raise ValueError("invalid or duplicate Alembic revision")
        if down_revision is None:
            dependencies: tuple[str, ...] = ()
        elif isinstance(down_revision, str):
            dependencies = (down_revision,)
        elif isinstance(down_revision, (tuple, list)) and all(isinstance(item, str) for item in down_revision):
            dependencies = tuple(down_revision)
        else:
            raise ValueError("unsupported down_revision shape")
        parents[revision] = dependencies
    if target not in parents:
        raise ValueError("missing target Alembic revision")
    if current not in parents:
        return "incompatible"

    def ancestors(revision: str) -> set[str]:
        pending = [revision]
        result: set[str] = set()
        while pending:
            item = pending.pop()
            if item in result:
                continue
            result.add(item)
            dependencies = parents.get(item)
            if dependencies is None:
                raise ValueError("broken Alembic revision graph")
            pending.extend(dependencies)
        return result

    if current in ancestors(target):
        return "compatible"
    if target in ancestors(current):
        return "descendant"
    return "incompatible"


def main(argv: list[str]) -> int:
    if len(argv) != 4:
        return 2
    versions, target, current = pathlib.Path(argv[1]), argv[2], argv[3]
    try:
        relation = revision_relation(versions, target, current)
    except (OSError, SyntaxError, ValueError):
        print("invalid Alembic revision graph", file=sys.stderr)
        return 1
    print(relation)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
