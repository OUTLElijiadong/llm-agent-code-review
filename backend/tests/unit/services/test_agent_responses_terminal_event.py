"""小菱 Responses 终态事件契约测试。"""

from app.services.agent_responses_service import terminal_event
from app.services.deepseek_responses_runtime import CANCELLED, RuntimeResult


def test_cancelled_run_emits_cancelled_terminal_event_with_team_count() -> None:
    event = terminal_event(
        RuntimeResult(
            run_id="run-cancelled",
            status=CANCELLED,
            cancelled_teams=2,
        )
    )

    assert event["type"] == "response.cancelled"
    assert event["response"]["status"] == CANCELLED
    assert event["response"]["cancelled_teams"] == 2
