"""可信聚合汇总和人工复核闭环测试。"""
import pytest

from app.core.exceptions import NotFoundError
from app.models.project import Project
from app.models.review_issue import ReviewIssue
from app.models.review_task import ReviewTask
from app.models.user import User
from app.services import issue_service, review_service


def _user(db, username: str) -> User:
    user = User(username=username, password="x", role="user", status=1)
    db.add(user)
    db.commit()
    return user


def _task(db, owner: User) -> ReviewTask:
    project = Project(
        user_id=owner.id,
        project_name=f"{owner.username}-project",
        language="python",
        status="active",
    )
    db.add(project)
    db.flush()
    task = ReviewTask(
        user_id=owner.id,
        project_id=project.id,
        task_name="aggregation",
        review_type="standard",
        status="success",
        total_files=1,
        processed_files=1,
        total_issues=2,
        severe_issues=0,
        high_issues=1,
        medium_issues=1,
        low_issues=0,
        score=70,
        duration_ms=10,
    )
    db.add(task)
    db.commit()
    return task


def _issue(db, task: ReviewTask, **overrides) -> ReviewIssue:
    values = {
        "task_id": task.id,
        "file_name": "app.py",
        "line_number": 8,
        "issue_type": "安全漏洞",
        "severity": "高",
        "title": "命令注入",
        "description": "外部输入进入命令执行",
        "status": "pending_review",
        "confirmation_count": 2,
        "aggregation_version": "finding-aggregation-v1",
        "evidence_quality": "inferred",
        "conflict_status": "unresolved",
        "human_review_status": "pending",
        "aggregation_json": {"claims": [{"claim_id": "claim-1"}]},
    }
    values.update(overrides)
    issue = ReviewIssue(**values)
    db.add(issue)
    db.commit()
    return issue


def test_task_detail_summarizes_aggregation_and_review_queue(db):
    owner = _user(db, "owner-summary")
    task = _task(db, owner)
    _issue(db, task)
    _issue(
        db,
        task,
        line_number=20,
        severity="中",
        confirmation_count=1,
        evidence_quality="unsupported",
        conflict_status="none",
        human_review_status="evidence_requested",
    )

    detail = review_service.get_task_detail(db, owner, task.id)

    assert detail["aggregation_summary"] == {
        "aggregated": 2,
        "independently_confirmed": 1,
        "pending_human_review": 2,
        "unresolved_conflicts": 1,
        "insufficient_evidence": 1,
    }


@pytest.mark.parametrize(
    ("decision", "expected_status", "expected_conflict"),
    [
        ("accepted", "unfixed", "resolved"),
        ("rejected", "ignored", "resolved"),
        ("evidence_requested", "pending_review", "unresolved"),
    ],
)
def test_human_review_preserves_claims_and_records_history(
    db, decision: str, expected_status: str, expected_conflict: str,
):
    owner = _user(db, f"owner-{decision}")
    task = _task(db, owner)
    issue = _issue(db, task)

    updated = issue_service.review_decision(db, owner, issue.id, decision, "人工依据")

    assert updated.status == expected_status
    assert updated.human_review_status == decision
    assert updated.conflict_status == expected_conflict
    assert updated.aggregation_json["claims"] == [{"claim_id": "claim-1"}]
    assert updated.aggregation_json["human_review"]["decision"] == decision
    assert updated.aggregation_json["human_review"]["note"] == "人工依据"
    assert updated.aggregation_json["human_review"]["reviewer_id"] == owner.id


def test_human_review_rejects_non_member_without_mutation(db):
    owner = _user(db, "owner-auth")
    outsider = _user(db, "outsider-auth")
    task = _task(db, owner)
    issue = _issue(db, task)

    with pytest.raises(NotFoundError):
        issue_service.review_decision(db, outsider, issue.id, "accepted", "")

    db.refresh(issue)
    assert issue.human_review_status == "pending"
    assert "human_review" not in issue.aggregation_json
