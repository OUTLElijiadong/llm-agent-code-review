"""企业群小菱执行前策略。此模块不调模型、不更改权限或钱款。

运行时尚须接入受控上下文、真实团队和企业账本，只有全部就绪才能启用。
"""
from dataclasses import dataclass
from enum import Enum


class TriggerDecision(str, Enum):
    DISABLED = "disabled"
    NO_TRIGGER = "no_trigger"
    MANUAL = "manual"
    PROACTIVE = "proactive"
    CONTINUE_DISCUSSION = "continue_discussion"


@dataclass(frozen=True)
class RuntimeGuards:
    """实际运行前的独立门禁；不得仅凭配置声称已对接。"""

    authenticated_scope: bool = False
    confined_context: bool = False
    enterprise_ledger: bool = False
    budget_reserved: bool = False
    real_team_runtime: bool = False

    def ready(self) -> bool:
        return all((self.authenticated_scope, self.confined_context, self.enterprise_ledger,
                    self.budget_reserved, self.real_team_runtime))


def decide_trigger(*, kind: str, sender_type: str, mentioned: bool, discussion_active: bool,
                   subscription_usable: bool, guards: RuntimeGuards) -> TriggerDecision:
    """私聊仅 @；群聊允许主动；Agent 发言只能延续现有讨论，不递归启动新话题。"""
    if kind not in {"company", "group", "direct"} or sender_type not in {"human", "agent"}:
        return TriggerDecision.NO_TRIGGER
    if sender_type == "agent":
        candidate = TriggerDecision.CONTINUE_DISCUSSION if discussion_active and kind != "direct" \
            else TriggerDecision.NO_TRIGGER
    elif mentioned:
        candidate = TriggerDecision.MANUAL
    elif kind in {"company", "group"}:
        candidate = TriggerDecision.PROACTIVE
    else:
        candidate = TriggerDecision.NO_TRIGGER
    if candidate is TriggerDecision.NO_TRIGGER:
        return candidate
    if not subscription_usable or not guards.ready():
        return TriggerDecision.DISABLED
    return candidate


@dataclass(frozen=True)
class DiscussionBudget:
    """毫积分、发言次数和企业周期余额同时约束；不会预占或结算资金。"""

    discussion_limit_milli: int
    period_remaining_milli: int
    max_turns: int
    spent_milli: int = 0
    turns: int = 0

    def can_reserve(self, amount_milli: int) -> bool:
        return (
            amount_milli > 0 and self.discussion_limit_milli > 0 and self.period_remaining_milli >= 0
            and self.max_turns > 0 and self.turns >= 0 and self.spent_milli >= 0
            and self.turns < self.max_turns
            and self.spent_milli + amount_milli <= self.discussion_limit_milli
            and amount_milli <= self.period_remaining_milli
        )


def resolve_conflict(*, previous_authorized: bool, new_authorized: bool,
                     previous_rank: int, new_rank: int) -> str:
    """仅用于同一事项中已完成资源与管理范围验证的指令比较。"""
    if not new_authorized:
        return "reject_new"
    if not previous_authorized:
        return "accept_new"
    if previous_rank == new_rank:
        return "pause_for_resolution"
    return "accept_new" if new_rank > previous_rank else "retain_previous"
