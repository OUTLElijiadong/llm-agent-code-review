from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.agents.base import AgentResult
from app.ai.multi_agent import get_agent_profiles
from app.core.exceptions import ValidationError
from app.models.code_file import CodeFile
from app.models.code_version import CodeVersion
from app.models.project import Project
from app.models.review_task import ReviewTask
from app.models.review_task_file import ReviewTaskFile
from app.schemas.review import ReviewStartIn
from app.services import review_service


@pytest.fixture
def scan_rows(db, admin_user):
    project = Project(user_id=admin_user.id, project_name="隔离生命周期测试", status="active")
    db.add(project)
    db.flush()
    code_file = CodeFile(project_id=project.id, file_name="source.py", file_path="source.py",
                         content="value = 1\n", language="python", status="active",
                         size_bytes=10, line_count=1, version_no=1, is_binary=0)
    db.add(code_file)
    db.flush()
    db.add(CodeVersion(file_id=code_file.id, version_no=1, content=code_file.content,
                       create_time=project.create_time))
    task = ReviewTask(user_id=admin_user.id, project_id=project.id, review_type="standard",
                      status="running", total_files=1, execution_token="lease")
    db.add(task)
    db.commit()
    return project, code_file, task


@pytest.mark.parametrize("content,binary", [("", 0), (" \t\n\u3000", 0), ("encoded", 1)])
def test_start_rejects_unreviewable_input_without_task(db, admin_user, scan_rows, monkeypatch, content, binary):
    project, code_file, _task = scan_rows
    code_file.content = content
    code_file.is_binary = binary
    db.commit()
    dispatch = Mock()
    monkeypatch.setattr(review_service.threading, "Thread", dispatch)
    before = db.query(ReviewTask).count()
    with pytest.raises(ValidationError, match="有效|非空|二进制"):
        review_service.start(db, admin_user, ReviewStartIn(project_id=project.id, file_ids=[code_file.id]))
    assert db.query(ReviewTask).count() == before
    dispatch.assert_not_called()


@pytest.mark.parametrize("mode", ["standard", "full"])
def test_all_model_failures_do_not_become_success(db, admin_user, scan_rows, monkeypatch, mode):
    _project, code_file, task = scan_rows
    task.review_type = mode
    db.commit()
    monkeypatch.setattr(review_service, "_emit_review_event", Mock())
    monkeypatch.setattr(review_service, "static_scan_file", lambda _file: [])
    monkeypatch.setattr(review_service, "_log_sequential_call", Mock())
    agent = Mock()
    agent.execute_review.return_value = AgentResult(success=False, error="isolated model failure")
    monkeypatch.setattr(review_service, "_get_agent_for_profile", lambda _code: agent)
    monkeypatch.setattr(review_service, "_call_single_agent", Mock(side_effect=RuntimeError("isolated model failure")))
    review_service._execute_review(db, Mock(), None, task, admin_user, [code_file], [],
                                  get_agent_profiles(mode), "", execution_token="lease")
    db.refresh(task)
    assert task.status == "failed"
    assert task.score != 100
    assert task.error_message
    assert task.end_time is not None


def test_deleted_task_cannot_be_resurrected_or_call_next_chunk(db, admin_user, scan_rows, monkeypatch):
    from sqlalchemy.orm import Session

    _project, code_file, task = scan_rows
    calls = []
    monkeypatch.setattr(review_service, "_emit_review_event", Mock())
    monkeypatch.setattr(review_service, "static_scan_file", lambda _file: [])
    monkeypatch.setattr(review_service, "chunk_code_with_context", lambda *args, **kwargs: [
        SimpleNamespace(text="value = 1", start_line=0), SimpleNamespace(text="value = 2", start_line=1)])

    def finish_first_chunk(*args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            with Session(db.get_bind()) as deleting:
                review_service.delete_task(deleting, admin_user, task.id)
        return []

    monkeypatch.setattr(review_service, "_review_chunk_sequential", finish_first_chunk)
    review_service._execute_review(db, Mock(), None, task, admin_user, [code_file], [],
                                  get_agent_profiles("standard"), "", execution_token="lease")
    db.expire_all()
    assert db.get(ReviewTask, task.id).status == "deleted"
    assert len(calls) == 1


def test_task_and_input_snapshot_are_atomic_and_immutable(db, admin_user, scan_rows, monkeypatch):
    project, code_file, _task = scan_rows
    monkeypatch.setattr(review_service.threading, "Thread", Mock())
    monkeypatch.setattr(review_service, "get_enabled_rules", lambda *args, **kwargs: [])
    monkeypatch.setattr("app.utils.api_resolver.resolve_api_config", lambda *args: None)
    monkeypatch.setattr(
        "app.services.agent_model_service.resolve_subagent_config", lambda _db, config, **kwargs: config,
    )
    monkeypatch.setattr(review_service, "DeepSeekAgent", lambda **kwargs: SimpleNamespace(model="isolated"))
    task = review_service.start(db, admin_user, ReviewStartIn(project_id=project.id, file_ids=[code_file.id]))
    link = db.query(ReviewTaskFile).filter_by(task_id=task.id).one()
    assert link.version_no == 1
    assert len(link.content_sha256) == 64
    code_file.content = "value = 2\n"
    code_file.file_name = "renamed.py"
    code_file.version_no = 2
    db.commit()
    from app.services.review_input_service import load_task_inputs
    snapshot = load_task_inputs(db, task.id)[0]
    assert snapshot.content == "value = 1\n"
    assert snapshot.file_name == "source.py"
    assert snapshot.version_no == 1


def test_missing_version_rolls_back_task_and_links(db, admin_user, scan_rows, monkeypatch):
    project, code_file, _task = scan_rows
    db.query(CodeVersion).filter_by(file_id=code_file.id).delete()
    db.commit()
    before = db.query(ReviewTask).count()
    monkeypatch.setattr(review_service.threading, "Thread", Mock())
    monkeypatch.setattr(review_service, "get_enabled_rules", lambda *args, **kwargs: [])
    monkeypatch.setattr("app.utils.api_resolver.resolve_api_config", lambda *args: None)
    monkeypatch.setattr(
        "app.services.agent_model_service.resolve_subagent_config", lambda _db, config, **kwargs: config,
    )
    monkeypatch.setattr(review_service, "DeepSeekAgent", lambda **kwargs: SimpleNamespace(model="isolated"))
    with pytest.raises(ValidationError, match="版本记录不一致"):
        review_service.start(db, admin_user, ReviewStartIn(project_id=project.id, file_ids=[code_file.id]))
    assert db.query(ReviewTask).count() == before
    assert db.query(ReviewTaskFile).count() == 0


@pytest.mark.parametrize("mode", ["standard", "full"])
def test_successful_zero_findings_is_not_failure(db, admin_user, scan_rows, monkeypatch, mode):
    _project, code_file, task = scan_rows
    task.review_type = mode
    db.commit()
    monkeypatch.setattr(review_service, "_emit_review_event", Mock())
    monkeypatch.setattr(review_service, "static_scan_file", lambda _file: [])
    monkeypatch.setattr(review_service, "_log_sequential_call", Mock())
    agent = Mock()
    agent.execute_review.return_value = AgentResult(success=True, data={"issues": []})
    monkeypatch.setattr(review_service, "_get_agent_for_profile", lambda _code: agent)
    monkeypatch.setattr(review_service, "_call_single_agent", lambda *args, **kwargs: ('{"issues": []}', {}))
    review_service._execute_review(db, Mock(), None, task, admin_user, [code_file], [],
                                  get_agent_profiles(mode), "", execution_token="lease")
    db.refresh(task)
    assert task.status == "success"
    assert task.total_issues == 0
    assert task.score == 100
    assert task.coverage["stage"] == "complete"
    detail = review_service.get_task_detail(db, admin_user, task.id)
    assert detail["score_version"] == task.score_version
    assert detail["score_breakdown"] == task.score_breakdown


def test_partial_failure_preserves_completed_findings_and_counters(db, admin_user, scan_rows, monkeypatch):
    from app.ai.static_analyzer import Finding
    from app.models.review_issue import ReviewIssue

    _project, code_file, task = scan_rows
    monkeypatch.setattr(review_service, "_emit_review_event", Mock())
    monkeypatch.setattr(review_service, "static_scan_file", lambda _file: [
        Finding(line_number=1, title="真实静态证据", severity="高", issue_type="安全漏洞", description="静态证据")])
    monkeypatch.setattr(review_service, "_review_chunk_sequential", Mock(
        side_effect=review_service.ReviewCoverageError(["isolated failure"])))
    review_service._execute_review(db, Mock(), None, task, admin_user, [code_file], [],
                                  get_agent_profiles("standard"), "", execution_token="lease")
    db.refresh(task)
    assert task.status == "failed"
    assert task.total_issues == db.query(ReviewIssue).filter_by(task_id=task.id).count() == 1
    assert task.high_issues == 1
    assert task.processed_files == 0
    assert task.score != 100


def test_legacy_and_tampered_input_are_not_silently_replaced(db, scan_rows):
    from app.services.review_input_service import freeze_task_inputs, load_task_inputs

    _project, code_file, task = scan_rows
    db.add(ReviewTaskFile(task_id=task.id, file_id=code_file.id))
    db.commit()
    with pytest.raises(ValidationError, match="历史任务"):
        load_task_inputs(db, task.id)
    db.query(ReviewTaskFile).filter_by(task_id=task.id).delete()
    freeze_task_inputs(db, task.id, [code_file])
    db.commit()
    db.query(CodeVersion).filter_by(file_id=code_file.id).one().content = "tampered"
    db.commit()
    with pytest.raises(ValidationError, match="校验失败"):
        load_task_inputs(db, task.id)


def test_cancel_records_terminal_time_and_revokes_execution(db, admin_user, scan_rows):
    _project, _code_file, task = scan_rows
    review_service.cancel_task(db, admin_user, task.id)
    db.refresh(task)
    assert task.status == "cancelled"
    assert task.execution_token != "lease"
    assert task.end_time is not None
    assert task.coverage["stage"] == "cancelled"
