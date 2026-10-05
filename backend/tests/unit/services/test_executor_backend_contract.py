"""后端与宿主机执行器两份契约必须一致。

`ops_service.ACTION_PARAM_KEYS`（API 进程）与 `deploy/prism_ops_executor.ACTION_PARAM_KEYS`
（root 执行器）由两个进程各自加载，历史上出现过"后端已放行、执行器仍拒绝"的漂移，
表现为安全中心保存策略时报"包含未允许参数"。本文件用可执行断言锁死两者的一致性。
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from app.services import ops_service

# .../backend/tests/unit/services/x.py → parents[3] 是仓库根
_EXECUTOR_PATH = Path(__file__).resolve().parents[3] / "deploy" / "prism_ops_executor.py"
if not _EXECUTOR_PATH.is_file():  # 兼容 backend 作为工作目录运行的情形
    _EXECUTOR_PATH = Path(__file__).resolve().parents[4] / "deploy" / "prism_ops_executor.py"


def _load_executor():
    spec = importlib.util.spec_from_file_location("contract_prism_ops_executor", _EXECUTOR_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


EXECUTOR = _load_executor()


def test_executor_file_exists():
    assert _EXECUTOR_PATH.is_file(), f"找不到宿主机执行器: {_EXECUTOR_PATH}"


def test_action_param_keys_are_identical_on_both_sides():
    """逐动作比较参数白名单；任何一侧漏加字段都必须在这里失败。"""
    backend = {action: set(keys) for action, keys in ops_service.ACTION_PARAM_KEYS.items()}
    executor = {action: set(keys) for action, keys in EXECUTOR.ACTION_PARAM_KEYS.items()}
    only_backend = {action: sorted(keys - executor.get(action, set())) for action, keys in backend.items()
                    if keys - executor.get(action, set())}
    only_executor = {action: sorted(keys - backend.get(action, set())) for action, keys in executor.items()
                     if keys - backend.get(action, set())}
    assert only_backend == {}, f"后端放行但执行器会拒绝: {only_backend}"
    assert only_executor == {}, f"执行器放行但后端未登记: {only_executor}"


def test_action_sets_match():
    assert set(ops_service.ACTION_RISKS) == set(EXECUTOR.ACTION_PARAM_KEYS)
    assert set(ops_service.ACTION_PARAM_KEYS) == set(EXECUTOR.ACTION_PARAM_KEYS)


def test_security_block_configure_carries_auto_escalate_on_both_sides():
    """回归：v4.0.53 只改了后端白名单，导致安全中心保存策略被执行器拒绝。"""
    assert "auto_escalate" in ops_service.ACTION_PARAM_KEYS["security_block_configure"]
    assert "auto_escalate" in EXECUTOR.ACTION_PARAM_KEYS["security_block_configure"]


def test_read_only_and_auto_action_sets_match():
    assert set(ops_service.READ_ONLY_ACTIONS) == set(EXECUTOR.READ_ONLY_ACTIONS)
    assert set(ops_service.AUTO_ACTIONS) <= set(EXECUTOR.READ_ONLY_ACTIONS)


def test_required_and_bounded_ranges_agree_for_configure():
    """两侧对封禁时长的上限必须一致，否则会出现"后端放行、执行器仍拒绝"。"""
    from app.schemas.security_center import AutomaticBlockingPolicyIn

    block_module = Path(__file__).resolve().parents[3] / "deploy" / "prism_security_block.py"
    if not block_module.is_file():
        block_module = Path(__file__).resolve().parents[4] / "deploy" / "prism_security_block.py"
    source = block_module.read_text(encoding="utf-8")
    assert "ESCALATION_MAX_SECONDS = 3600" in source

    bounds = {}
    exec("ESCALATION_MAX_SECONDS = 3600", bounds)  # noqa: S102 - 只读本仓库常量
    field = AutomaticBlockingPolicyIn.model_fields["duration_seconds"]
    limits = [getattr(item, "le", None) for item in field.metadata]
    assert 3600 in limits, limits
    assert bounds["ESCALATION_MAX_SECONDS"] == max(item for item in limits if item is not None)
