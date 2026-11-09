"""群小菱运行前置条件和防循环策略；不替代真实账本或 Agent 集成测试。"""
from dataclasses import replace

import pytest

from app.services.enterprise_assistant_policy import (
    DiscussionBudget,
    RuntimeGuards,
    TriggerDecision,
    decide_trigger,
    resolve_conflict,
)


@pytest.fixture
def ready():
    return RuntimeGuards(authenticated_scope=True, confined_context=True, enterprise_ledger=True,
                         budget_reserved=True, real_team_runtime=True)


@pytest.mark.parametrize("field", ["authenticated_scope", "confined_context", "enterprise_ledger",
                                   "budget_reserved", "real_team_runtime"])
def test_every_guard_must_be_ready(ready, field):
    assert decide_trigger(kind="company", sender_type="human", mentioned=True, discussion_active=False,
                          subscription_usable=True, guards=replace(ready, **{field: False})) == TriggerDecision.DISABLED


def test_private_requires_mention_and_group_proactive(ready):
    assert decide_trigger(kind="direct", sender_type="human", mentioned=False, discussion_active=False,
                          subscription_usable=True, guards=ready) == TriggerDecision.NO_TRIGGER
    assert decide_trigger(kind="direct", sender_type="human", mentioned=True, discussion_active=False,
                          subscription_usable=True, guards=ready) == TriggerDecision.MANUAL
    assert decide_trigger(kind="group", sender_type="human", mentioned=False, discussion_active=False,
                          subscription_usable=True, guards=ready) == TriggerDecision.PROACTIVE


def test_agent_discussion_cannot_recursively_create_new_topic(ready):
    assert decide_trigger(kind="company", sender_type="agent", mentioned=True, discussion_active=False,
                          subscription_usable=True, guards=ready) == TriggerDecision.NO_TRIGGER
    assert decide_trigger(kind="company", sender_type="agent", mentioned=False, discussion_active=True,
                          subscription_usable=True, guards=ready) == TriggerDecision.CONTINUE_DISCUSSION
    assert decide_trigger(kind="company", sender_type="agent", mentioned=False, discussion_active=True,
                          subscription_usable=False, guards=ready) == TriggerDecision.DISABLED


def test_budget_is_limited_by_remaining_period_and_rounds():
    budget = DiscussionBudget(discussion_limit_milli=100_000, period_remaining_milli=30_000,
                              max_turns=10, spent_milli=80_000, turns=9)
    assert budget.can_reserve(20_000)
    assert not budget.can_reserve(20_001)
    assert not replace(budget, turns=10).can_reserve(1)
    assert not replace(budget, period_remaining_milli=0).can_reserve(1)


def test_priority_never_substitutes_resource_authorization():
    assert resolve_conflict(previous_authorized=True, new_authorized=False, previous_rank=1,
                            new_rank=4) == "reject_new"
    assert resolve_conflict(previous_authorized=True, new_authorized=True, previous_rank=1,
                            new_rank=4) == "accept_new"
    assert resolve_conflict(previous_authorized=True, new_authorized=True, previous_rank=3,
                            new_rank=3) == "pause_for_resolution"
