"""按现行规则回填存量沙箱 / 渗透报告分数（幂等，默认只预览）。

存量沙箱任务早于 sandbox-check-pass-rate-v1，渗透任务早于
pentest-confirmed-deduction-v1，报告因此一律“不出分”。本脚本从已保存的
执行回执（沙箱 environment.result_json、渗透阶段与发现）按与新任务完全相同
的规则函数复算；没有任何已执行检查项 / 已完成检测阶段的任务保持不出分。

每条被改写的任务把旧值写入 coverage["score_backfill"]，可据此逐条回滚。

用法：
    python scripts/backfill_rule_scores.py            # 预览
    python scripts/backfill_rule_scores.py --apply    # 写入
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.main import app as _app  # noqa: E402,F401 - 与生产进程一致地注册全部模型
from app.core.database import SessionLocal  # noqa: E402
from app.models.agent_capability import SandboxEnvironment  # noqa: E402
from app.models.pentest import PentestEngagement, PentestFinding  # noqa: E402
from app.models.review_task import ReviewTask  # noqa: E402
from app.services import pentest_service, sandbox_service  # noqa: E402

_TERMINAL = ("success", "partial", "failed", "cancelled", "expired", "stopped")


def _public_id(task_name: str | None) -> str:
    return str(task_name or "").rsplit("·", 1)[-1].strip()


def _record_backfill(task: ReviewTask, coverage: dict, basis: str) -> dict:
    return {
        **coverage,
        "score_backfill": {
            "basis": basis,
            "at": datetime.now(timezone.utc).isoformat(),
            "previous_score": task.score,
            "previous_score_version": task.score_version,
            "previous_score_available": coverage.get("score_available"),
        },
    }


def _sandbox(db, task: ReviewTask) -> tuple[str, int | None]:
    environment = db.query(SandboxEnvironment).filter(
        SandboxEnvironment.public_id == _public_id(task.task_name),
        SandboxEnvironment.owner_id == task.user_id,
        SandboxEnvironment.project_id == task.project_id,
    ).first()
    if environment is None:
        return "no_environment", None
    conclusion = sandbox_service._loads(environment.result_json, {})
    if not isinstance(conclusion, dict) or not conclusion:
        return "no_receipt", None
    result = sandbox_service._sandbox_result_score(conclusion)
    if not result["score_available"]:
        return "no_executed_checks", None
    coverage = dict(task.coverage) if isinstance(task.coverage, dict) else {}
    verification = sandbox_service._sandbox_verification_coverage(conclusion)
    coverage = _record_backfill(task, coverage, sandbox_service.SANDBOX_SCORE_VERSION)
    coverage.update({
        "score_available": True,
        "score_basis": result["score_basis"],
        "score_sample": result["sample"],
        "score_unavailable_reason": None,
    })
    coverage.setdefault("verification_status", verification.get("verification_status"))
    task.coverage = coverage
    task.score = result["score"]
    task.score_version = sandbox_service.SANDBOX_SCORE_VERSION
    task.score_breakdown = {
        "version": sandbox_service.SANDBOX_SCORE_VERSION,
        "score": result["score"],
        "score_available": True,
        "score_source": result["score_basis"],
        "sample": result["sample"],
        "coverage_status": coverage.get("coverage_status", "unknown"),
        "unavailable_reason": None,
    }
    return "scored", result["score"]


def _pentest(db, task: ReviewTask) -> tuple[str, int | None]:
    engagement = db.query(PentestEngagement).filter(
        PentestEngagement.public_id == _public_id(task.task_name),
    ).first()
    if engagement is None:
        return "no_engagement", None
    phase_coverage = pentest_service._pentest_coverage(db, int(engagement.id))
    if not pentest_service._PENTEST_SCORED_PHASES.intersection(phase_coverage.get("completed_phases") or []):
        return "no_detect_phase", None
    confirmed = {key: 0 for key in ("严重", "高", "中", "低", "提示")}
    for finding in db.query(PentestFinding).filter(PentestFinding.engagement_id == engagement.id).all():
        if finding.status == "confirmed":
            confirmed[finding.severity] = confirmed.get(finding.severity, 0) + 1
    score = pentest_service._pentest_rule_score(confirmed)
    complete = str(phase_coverage.get("coverage_status") or "") == "complete" and task.status == "success"
    coverage = dict(task.coverage) if isinstance(task.coverage, dict) else {}
    coverage = _record_backfill(task, coverage, pentest_service.PENTEST_SCORE_VERSION)
    pentest_service._mark_pentest_score_basis(coverage, confirmed, scope="complete" if complete else "partial")
    task.coverage = coverage
    task.score = score
    task.score_version = pentest_service.PENTEST_SCORE_VERSION
    return "scored", score


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--apply", action="store_true", help="写入数据库（默认只预览）")
    args = parser.parse_args()
    outcomes: Counter = Counter()
    db = SessionLocal()
    try:
        tasks = db.query(ReviewTask).filter(
            ReviewTask.review_type.in_(["sandbox_test", "pentest"]),
            ReviewTask.status.in_(_TERMINAL),
        ).all()
        for task in tasks:
            if task.review_type == "sandbox_test":
                if task.score_version == sandbox_service.SANDBOX_SCORE_VERSION:
                    outcomes["sandbox_test:already_rule"] += 1
                    continue
                outcome, score = _sandbox(db, task)
            else:
                if task.score_version == pentest_service.PENTEST_SCORE_VERSION:
                    outcomes["pentest:already_rule"] += 1
                    continue
                outcome, score = _pentest(db, task)
            outcomes[f"{task.review_type}:{outcome}"] += 1
            if outcome == "scored":
                print(f"#{task.id} {task.review_type} {task.status} -> {score}")
        if args.apply:
            db.commit()
        else:
            db.rollback()
    finally:
        db.close()
    print(("已写入" if args.apply else "预览（未写入）"), dict(outcomes))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
