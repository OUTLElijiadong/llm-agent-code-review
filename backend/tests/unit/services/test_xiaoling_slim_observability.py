"""小菱系统指令瘦身与进程内可观测性计数的回归测试。"""

from __future__ import annotations

import pytest

from app.core.observability import (
    event_label_counts,
    metrics_snapshot,
    observe_event,
    reset_metrics,
)
from app.services.agent_responses_service import _instructions
from app.services.page_guide_service import admin_guide_block, user_guide_block


@pytest.fixture(autouse=True)
def _isolated_metrics():
    """每个用例独立起算并清理进程内计数，避免测试顺序干扰。"""
    reset_metrics()
    yield
    reset_metrics()


def test_user_instructions_drop_fixed_guide_block() -> None:
    """用户侧不再每轮注入固定页面指南，但保留页面能力工具契约。"""
    text = _instructions("user", None, False)

    assert text.strip()
    assert user_guide_block() not in text
    assert "# 页面引导协议" not in text
    assert "你可引导的普通用户页面" not in text
    assert "涉及具体页面操作步骤时，先调用 recall_knowledge 检索对应页面指南。" in text
    assert "涉及具体页面操作步骤时，先调用 recall_knowledge 检索对应页面指南。" in text


def test_admin_instructions_drop_fixed_guide_block() -> None:
    """管理侧同样移除固定页面指南，只保留 recall_knowledge 召回提示。"""
    text = _instructions("admin", None, False)

    assert text.strip()
    assert admin_guide_block() not in text
    assert "# 页面引导协议" not in text
    assert "你可引导的管理页面" not in text
    assert "涉及具体页面操作步骤时，先调用 recall_knowledge 检索对应页面指南。" in text
    assert "涉及具体页面操作步骤时，先调用 recall_knowledge 检索对应页面指南。" in text


def test_instructions_keep_identity_permission_and_tool_contract() -> None:
    """瘦身不得破坏身份、权限、工具契约与安全约束。"""
    user_text = _instructions("user", None, False)
    admin_text = _instructions("admin", None, False)

    assert "棱镜小助" in user_text
    assert "小菱" in admin_text
    for text in (user_text, admin_text):
        assert "权限" in text
        assert "工具" in text
        assert "不得假设或冒充其他身份" in text
        assert "不要编造工具结果" in text
        assert "recall_knowledge" in text
        assert "使用中文直接给出结果" in text
    assert "user_describe_capabilities" in user_text
    assert "admin_describe_capabilities" in admin_text


def test_observe_event_accumulates_known_categories() -> None:
    """计数可按事件类别累计，快照对未观测类别补零。"""
    observe_event("xiaoling_compaction")
    observe_event("xiaoling_compaction", count=2)
    observe_event("xiaoling_cancel", labels={"surface": "user"})
    observe_event("sandbox_heartbeat", count=3)

    snapshot = metrics_snapshot()
    assert snapshot["xiaoling_compaction"] == 3
    assert snapshot["xiaoling_cancel"] == 1
    assert snapshot["sandbox_heartbeat"] == 3
    assert snapshot["sandbox_stuck_recovered"] == 0
    assert snapshot["team_created"] == 0


def test_observe_event_keeps_label_dimension() -> None:
    """labels 以稳定维度键保留细分计数。"""
    observe_event("xiaoling_cancel", labels={"surface": "admin"})
    observe_event("xiaoling_cancel", labels={"surface": "admin"})
    observe_event("xiaoling_cancel", labels={"surface": "user"})

    labels = event_label_counts()
    assert labels[("xiaoling_cancel", '{"surface": "admin"}')] == 2
    assert labels[("xiaoling_cancel", '{"surface": "user"}')] == 1


def test_reset_metrics_clears_counts_and_labels() -> None:
    """reset_metrics 必须清空计数与标签维度。"""
    observe_event("team_created")
    observe_event("sandbox_stuck_recovered", labels={"worker": "w1"})

    reset_metrics()
    assert metrics_snapshot() == {
        "xiaoling_compaction": 0,
        "xiaoling_cancel": 0,
        "sandbox_heartbeat": 0,
        "sandbox_stuck_recovered": 0,
        "team_created": 0,
    }
    assert event_label_counts() == {}
