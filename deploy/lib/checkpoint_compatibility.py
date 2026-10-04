"""无网检查目标镜像的真实检查点 reader；不 import 应用或连接数据库。

只提取镜像中的类定义并运行 load 的固定 fixture。账本读取替换为本地哨兵，
不以发布版本、镜像标签或 helper 名称推断能力。stdout 仅输出能力枚举。
"""
from __future__ import annotations

import ast
import asyncio
import copy
import hashlib
import hmac
import json
import pathlib
import signal
import sys
import types
import typing
from dataclasses import dataclass, field


def _classes(path: pathlib.Path, names: tuple[str, ...]) -> list[ast.ClassDef]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    classes = {node.name: node for node in tree.body if isinstance(node, ast.ClassDef)}
    return [classes[name] for name in names]


def _reader_namespace(app_root: pathlib.Path) -> dict:
    module = types.ModuleType("_prism_checkpoint_reader_probe")
    sys.modules[module.__name__] = module  # dataclass 解析延迟注解需要类所属模块。
    namespace = vars(module)
    namespace.update(vars(typing))
    namespace.update({
        "__name__": module.__name__, "copy": copy, "json": json,
        "hashlib": hashlib, "hmac": hmac, "dataclass": dataclass,
        "field": field, "RUNNING": "running",
    })
    services = app_root / "services"
    definitions = _classes(
        services / "deepseek_responses_runtime.py",
        ("InvalidRunStateError", "ToolCall", "PendingAction", "RunCheckpoint"),
    ) + _classes(services / "agent_responses_service.py", ("DatabaseCheckpointStore",))
    tree = ast.Module(
        body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0), *definitions],
        type_ignores=[],
    )
    exec(compile(ast.fix_missing_locations(tree), "<target-image-reader>", "exec"), namespace)
    return namespace


async def _probe(app_root: pathlib.Path) -> bool:
    namespace = _reader_namespace(app_root)
    messages = [
        {"role": "user", "content": "ledger-probe-source-中文"},
        {"role": "assistant", "content": "ledger-probe-persistent-answer"},
    ]
    digest = hashlib.sha256(json.dumps(
        messages, ensure_ascii=False, separators=(",", ":"), default=str,
    ).encode("utf-8")).hexdigest()
    payload = {
        "run_id": "ledger-probe-run", "model": "local-only", "tools": [],
        "status": "completed",
        "_transcript_ref": {"version": 2, "start": 0, "end": 2, "sha256": digest},
    }
    row = types.SimpleNamespace(checkpoint_json="")

    class Column:
        def __eq__(self, value):
            return True

    class Query:
        def filter(self, *conditions):
            return self

        def first(self):
            return row

    class Database:
        def query(self, model):
            return Query()

    namespace["AgentResponseRun"] = type("AgentResponseRun", (), {
        name: Column() for name in ("run_id", "user_id", "surface", "session_key")
    })
    store = namespace["DatabaseCheckpointStore"](
        Database(), user_id=17, surface="chat", session_key="ledger-probe-session",
    )
    ledger_reads = []

    def read_ledger(start, end):
        if (start, end) != (0, 2):
            raise RuntimeError("unexpected ledger range")
        ledger_reads.append((start, end))
        return copy.deepcopy(messages)

    store._read_ledger = read_ledger
    # 原内嵌格式也应继续读取；错误/未知执行路径不当作兼容能力。
    legacy = {key: value for key, value in payload.items() if key != "_transcript_ref"}
    legacy["transcript"] = messages
    row.checkpoint_json = json.dumps(legacy, ensure_ascii=False)
    loaded = await store.load(payload["run_id"])
    if loaded is None or loaded.transcript != messages:
        return False
    row.checkpoint_json = json.dumps(payload, ensure_ascii=False)
    try:
        loaded = await store.load(payload["run_id"])
    except namespace["InvalidRunStateError"]:
        return False
    if loaded is None or loaded.transcript != messages or ledger_reads != [(0, 2)]:
        return False
    corrupt = copy.deepcopy(payload)
    corrupt["_transcript_ref"]["sha256"] = "0" * 64
    row.checkpoint_json = json.dumps(corrupt, ensure_ascii=False)
    try:
        await store.load(payload["run_id"])
    except namespace["InvalidRunStateError"]:
        return True
    return False


def main() -> int:
    def expired(signum, frame):
        raise TimeoutError("checkpoint capability probe deadline")

    signal.signal(signal.SIGALRM, expired)
    signal.alarm(15)
    try:
        root = pathlib.Path(sys.argv[1]) if len(sys.argv) == 2 else pathlib.Path("/app/app")
        print("supported" if asyncio.run(_probe(root)) else "unsupported")
        return 0
    except Exception as exc:
        # 不打印镜像源码、环境、路径或可能的配置值。
        print(f"checkpoint probe failed: {type(exc).__name__}", file=sys.stderr)
        return 2
    finally:
        signal.alarm(0)


if __name__ == "__main__":
    raise SystemExit(main())
