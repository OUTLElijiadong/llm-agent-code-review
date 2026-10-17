"""只修复已确认 Worker 清理完成但旧报告摘要仍待重试的终态沙箱报告。

默认只读预览。只有显式传入 --apply 且所有账号、项目、报告和终态证据
逐项匹配时，才调用确定性终态报告发布器；此脚本不会请求模型或重新运行测试。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.database import SessionLocal  # noqa: E402
from app.models.agent_capability import SandboxEnvironment  # noqa: E402
from app.models.review_report import ReviewReport  # noqa: E402
from app.models.review_task import ReviewTask  # noqa: E402
from app.models.user import User  # noqa: E402
from app.services import sandbox_service  # noqa: E402

STALE_CLEANUP_TEXT = "Worker 资源回收待重试"
CONFIRMED_CLEANUP_TEXT = "Worker 已确认资源终止"


def repair_rejection_reason(
    environment: Any,
    owner: Any,
    task: Any,
    report: Any,
    *,
    expected_public_id: str,
    expected_username: str,
) -> str | None:
    """Return a reason when a target does not meet every narrow repair guard."""
    if str(getattr(environment, "public_id", "")) != expected_public_id:
        return "沙箱标识不匹配"
    if getattr(owner, "username", None) != expected_username:
        return "报告所有者账号不匹配"
    if getattr(owner, "id", None) != getattr(environment, "owner_id", None):
        return "沙箱所有者关联不匹配"
    if getattr(environment, "purpose", None) != "test" or getattr(environment, "test_mode", None) != "whitebox":
        return "仅允许白盒测试环境"
    if getattr(environment, "status", None) != "failed":
        return "沙箱必须处于 failed 终态"
    if CONFIRMED_CLEANUP_TEXT not in str(getattr(environment, "error", "") or ""):
        return "环境错误字段未确认 Worker 终止"

    try:
        result = json.loads(getattr(environment, "result_json", "") or "{}")
    except (TypeError, ValueError):
        return "沙箱结果不是有效 JSON"
    if not isinstance(result, dict) or result.get("cleanup_confirmed") is not True:
        return "沙箱结果未确认清理完成"
    if STALE_CLEANUP_TEXT not in str(result.get("summary") or ""):
        return "沙箱摘要不含待修复的旧文案"
    ai_review = result.get("ai_review")
    report_ai_review = getattr(report, "content_json", None)
    report_ai_review = report_ai_review.get("ai_review") if isinstance(report_ai_review, dict) else None
    if result.get("ai_review_status") == "complete" or (
        isinstance(ai_review, dict) and ai_review.get("status") == "complete"
    ) or (isinstance(report_ai_review, dict) and report_ai_review.get("status") == "complete"):
        return "已有完成的 AI 报告，拒绝以确定性报告覆盖"

    if (
        getattr(task, "review_type", None) != "sandbox_test"
        or getattr(task, "status", None) != "failed"
        or getattr(task, "user_id", None) != getattr(environment, "owner_id", None)
        or getattr(task, "project_id", None) != getattr(environment, "project_id", None)
    ):
        return "报告任务与沙箱账号、项目或终态不匹配"
    content = getattr(report, "content_json", None)
    if (
        getattr(report, "user_id", None) != getattr(environment, "owner_id", None)
        or not isinstance(content, dict)
        or content.get("source") != "sandbox_test"
        or content.get("public_id") != expected_public_id
    ):
        return "报告快照来源或所有者不匹配"
    return None


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--public-id", required=True)
    parser.add_argument("--expected-username", required=True)
    parser.add_argument("--apply", action="store_true", help="通过全部保护条件后重新发布确定性报告")
    args = parser.parse_args()

    with SessionLocal() as db:
        environment = (
            db.query(SandboxEnvironment)
            .filter(SandboxEnvironment.public_id == args.public_id)
            .with_for_update()
            .one_or_none()
        )
        if environment is None:
            raise SystemExit("拒绝：找不到指定沙箱环境")
        owner = db.query(User).filter(User.id == environment.owner_id).one_or_none()
        tasks = (
            db.query(ReviewTask)
            .filter(
                ReviewTask.task_name.in_(sandbox_service._sandbox_report_task_names(environment)),
                ReviewTask.review_type == "sandbox_test",
            )
            .with_for_update()
            .all()
        )
        if len(tasks) != 1:
            raise SystemExit(f"拒绝：预期恰好一个沙箱报告任务，实际为 {len(tasks)} 个")
        task = tasks[0]
        report = (
            db.query(ReviewReport)
            .filter(ReviewReport.task_id == task.id)
            .with_for_update()
            .one_or_none()
        )
        if report is None:
            raise SystemExit("拒绝：找不到对应报告快照")

        rejection = repair_rejection_reason(
            environment,
            owner,
            task,
            report,
            expected_public_id=args.public_id,
            expected_username=args.expected_username,
        )
        if rejection:
            raise SystemExit(f"拒绝：{rejection}")

        original_status = task.status
        original_score = task.score
        if not args.apply:
            print(json.dumps({"status": "eligible", "mode": "dry_run", "public_id": args.public_id,
                              "report_task_id": task.id, "owner": owner.username}, ensure_ascii=False))
            return

        published = sandbox_service._publish_terminal_sandbox_report(db, environment)
        if not isinstance(published, dict) or not published.get("report_persisted"):
            raise SystemExit("修复失败：确定性报告未能持久化")
        db.refresh(task)
        db.refresh(report)
        repaired_content = report.content_json if isinstance(report.content_json, dict) else {}
        report_md = str(repaired_content.get("report_md") or "")
        if (
            task.status != original_status
            or task.score != original_score
            or STALE_CLEANUP_TEXT in task.summary
            or CONFIRMED_CLEANUP_TEXT not in task.summary
            or STALE_CLEANUP_TEXT in report_md
            or CONFIRMED_CLEANUP_TEXT not in report_md
        ):
            raise SystemExit("修复后核验失败：摘要、报告正文或原有失败结论不一致")
        print(json.dumps({"status": "repaired", "public_id": args.public_id,
                          "report_task_id": task.id, "execution_status": task.status,
                          "score_preserved": task.score == original_score,
                          "stale_text_removed": True, "cleanup_confirmation_present": True,
                          "artifact_id": published.get("artifact_id")}, ensure_ascii=False))


if __name__ == "__main__":
    main()
