import json
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from app.agents.base import AgentResult
from app.ai import finding_aggregator as aggregator_module
from app.ai.multi_agent import get_agent_profiles
from app.core.database import Base
from app.core.exceptions import ValidationError
from app.models.api_config import UserApiConfig as UserApiConfig
from app.models.code_file import CodeFile
from app.models.code_version import CodeVersion
from app.models.project import Project
from app.models.review_issue import ReviewIssue
from app.models.review_task import ReviewTask
from app.models.review_task_file import ReviewTaskFile
from app.models.user import User
from app.schemas.review import ReviewStartIn, TaskDetailOut
from app.services import review_service
from app.services.review_input_service import freeze_task_inputs, load_task_inputs


@pytest.fixture
def isolated_rows(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'independent.sqlite'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    monkeypatch.setattr(review_service, "SessionLocal", sessions)
    monkeypatch.setattr(review_service, "_emit_review_event", Mock())
    monkeypatch.setattr(review_service, "static_scan_file", lambda code_file: [])
    monkeypatch.setattr(review_service, "_log_sequential_call", Mock())
    monkeypatch.setattr(review_service, "DeepSeekAgent", lambda **kwargs: SimpleNamespace(model="isolated"))
    with sessions() as database:
        user = User(username="independent", password="isolated", role="admin", status=1)
        database.add(user)
        database.flush()
        project = Project(
            user_id=user.id,
            project_name="independent fixture",
            status="active",
            language="python",
        )
        database.add(project)
        database.flush()
        code_file = CodeFile(
            project_id=project.id,
            file_name="source.py",
            file_path="source.py",
            content="value = 1\n",
            language="python",
            status="active",
            size_bytes=10,
            line_count=1,
            version_no=1,
            is_binary=0,
        )
        database.add(code_file)
        database.flush()
        database.add(CodeVersion(
            file_id=code_file.id,
            version_no=1,
            content=code_file.content,
            create_time=datetime.now(timezone.utc),
        ))
        task = ReviewTask(
            user_id=user.id,
            project_id=project.id,
            status="running",
            review_type="standard",
            total_files=1,
            execution_token="initial-lease",
            start_time=datetime.now(timezone.utc),
        )
        database.add(task)
        database.commit()
        yield SimpleNamespace(
            database=database,
            sessions=sessions,
            user=user,
            project=project,
            code_file=code_file,
            task=task,
            engine=engine,
        )
    engine.dispose()


def add_existing_evidence(rows):
    issue = ReviewIssue(
        task_id=rows.task.id,
        file_id=rows.code_file.id,
        file_name="source.py",
        title="already committed evidence",
        issue_type="安全漏洞",
        severity="高",
        description="independently generated fixture",
        line_number=1,
    )
    rows.database.add(issue)
    rows.database.commit()
    return issue.id


def start_without_dispatch(rows, monkeypatch):
    with monkeypatch.context() as context:
        context.setattr(review_service.threading, "Thread", Mock())
        return review_service.start(rows.database, rows.user, ReviewStartIn(
            project_id=rows.project.id,
            file_ids=[rows.code_file.id],
        ))


def test_recovery_failure_after_claim_has_terminal_database_state(isolated_rows, monkeypatch):
    rows = isolated_rows
    add_existing_evidence(rows)
    freeze_task_inputs(rows.database, rows.task.id, [rows.code_file])
    rows.database.execute(text(
        "CREATE TRIGGER fail_cleanup BEFORE DELETE ON review_issue "
        "BEGIN SELECT RAISE(ABORT, 'isolated cleanup failure'); END"
    ))
    rows.database.commit()
    dispatch = Mock()
    monkeypatch.setattr(review_service, "_run_review_task", dispatch)
    review_service._resume_interrupted_task(rows.task.id, rows.user.id, "initial-lease")
    with rows.sessions() as observer:
        persisted = observer.get(ReviewTask, rows.task.id)
        evidence_count = observer.query(ReviewIssue).filter_by(task_id=rows.task.id).count()
        actual = {
            "status": persisted.status,
            "token": persisted.execution_token,
            "error": persisted.error_message,
            "end_time": str(persisted.end_time),
            "issues": evidence_count,
            "dispatches": dispatch.call_count,
        }
        assert persisted.status == "failed", actual
        assert evidence_count == 1
        assert persisted.end_time is not None
        dispatch.assert_not_called()


def test_legacy_recovery_preserves_previous_evidence_when_input_cannot_resume(isolated_rows):
    rows = isolated_rows
    add_existing_evidence(rows)
    rows.database.add(ReviewTaskFile(task_id=rows.task.id, file_id=rows.code_file.id))
    rows.database.commit()
    review_service._resume_interrupted_task(rows.task.id, rows.user.id, "initial-lease")
    with rows.sessions() as observer:
        persisted = observer.get(ReviewTask, rows.task.id)
        evidence_count = observer.query(ReviewIssue).filter_by(task_id=rows.task.id).count()
        actual = {"status": persisted.status, "error": persisted.error_message, "issues": evidence_count}
        assert persisted.status == "failed", actual
        assert "历史任务缺少可信版本快照" in persisted.error_message, actual
        assert evidence_count == 1, actual


def test_recovery_keeps_completed_file_evidence_and_retries_only_incomplete_files(
    isolated_rows, monkeypatch,
):
    rows = isolated_rows
    second_file = CodeFile(
        project_id=rows.project.id,
        file_name="second.py",
        file_path="second.py",
        content="value = 2\n",
        language="python",
        status="active",
        size_bytes=10,
        line_count=1,
        version_no=1,
        is_binary=0,
    )
    rows.database.add(second_file)
    rows.database.flush()
    rows.database.add(CodeVersion(
        file_id=second_file.id,
        version_no=1,
        content=second_file.content,
        create_time=datetime.now(timezone.utc),
    ))
    rows.task.total_files = 2
    rows.task.coverage = {
        "stage": "analyzing",
        "files": {str(rows.code_file.id): {"file_name": "source.py", "status": "complete"}},
    }
    evidence_id = add_existing_evidence(rows)
    freeze_task_inputs(rows.database, rows.task.id, [rows.code_file, second_file])
    rows.database.commit()
    dispatch = Mock()
    monkeypatch.setattr(review_service, "_run_review_task", dispatch)

    review_service._resume_interrupted_task(rows.task.id, rows.user.id, "initial-lease")

    with rows.sessions() as observer:
        persisted = observer.get(ReviewTask, rows.task.id)
        evidence = observer.get(ReviewIssue, evidence_id)
        assert evidence is not None
        assert persisted.processed_files == 1
        assert persisted.coverage["completed_files"] == 1
        assert persisted.coverage["files"][str(rows.code_file.id)]["status"] == "complete"
    dispatch.assert_called_once()


@pytest.mark.parametrize("mode", ["standard", "full"])
def test_partial_invalid_model_output_is_not_complete_success(isolated_rows, monkeypatch, mode):
    rows = isolated_rows
    rows.task.review_type = mode
    rows.database.commit()
    raw = json.dumps({
        "issues": [
            {
                "title": "valid retained finding",
                "severity": "高",
                "issue_type": "安全漏洞",
                "line_number": 1,
                "description": "independent evidence",
                "evidence": "value = 1",
            },
            None,
        ],
    })
    monkeypatch.setattr(review_service, "_call_single_agent", lambda *args, **kwargs: (raw, {}))
    if mode == "standard":
        from app.agents.review_agent import CodeReviewerAgent

        agent = CodeReviewerAgent()
        monkeypatch.setattr(agent, "call", lambda *args, **kwargs: AgentResult(success=True, data=raw))
        monkeypatch.setattr(review_service, "_get_agent_for_profile", lambda code: agent)
    review_service._execute_review(
        rows.database, Mock(), None, rows.task, rows.user, [rows.code_file], [],
        get_agent_profiles(mode), "", execution_token="initial-lease",
    )
    with rows.sessions() as observer:
        persisted = observer.get(ReviewTask, rows.task.id)
        actual = {
            "status": persisted.status,
            "coverage": persisted.coverage,
            "score": persisted.score,
            "issues": observer.query(ReviewIssue).filter_by(task_id=rows.task.id).count(),
        }
        assert persisted.status == "failed", actual
        assert actual["issues"] > 0


def test_api_does_not_claim_verified_for_corrupted_input(isolated_rows):
    rows = isolated_rows
    freeze_task_inputs(rows.database, rows.task.id, [rows.code_file])
    rows.database.commit()
    with rows.sessions() as corrupting:
        corrupting.query(CodeVersion).filter_by(file_id=rows.code_file.id).one().content = "corrupted content"
        corrupting.commit()
    with rows.sessions() as observer:
        user = observer.get(User, rows.user.id)
        with pytest.raises(ValidationError, match="校验失败"):
            load_task_inputs(observer, rows.task.id)
        detail = TaskDetailOut.model_validate(review_service.get_task_detail(observer, user, rows.task.id))
        actual = detail.files[0].model_dump()
        assert detail.files[0].snapshot_verified is False, actual


def test_real_session_snapshot_commit_failure_is_atomic(isolated_rows):
    rows = isolated_rows
    rows.database.execute(text(
        "CREATE TRIGGER fail_snapshot BEFORE INSERT ON review_task_file "
        "BEGIN SELECT RAISE(ABORT, 'isolated snapshot failure'); END"
    ))
    rows.database.commit()
    with pytest.raises(IntegrityError, match="isolated snapshot failure"):
        review_service.start(rows.database, rows.user, ReviewStartIn(
            project_id=rows.project.id,
            file_ids=[rows.code_file.id],
        ))
    with rows.sessions() as observer:
        assert observer.query(ReviewTask).count() == 1
        assert observer.query(ReviewTaskFile).count() == 0


def test_start_worker_detail_preserve_frozen_content_across_sessions(isolated_rows, monkeypatch):
    rows = isolated_rows
    created = start_without_dispatch(rows, monkeypatch)
    captured = []

    def analyze(**kwargs):
        captured.append(kwargs["code"])
        return AgentResult(success=True, data={"issues": []})

    monkeypatch.setattr(review_service, "_get_agent_for_profile", lambda code: SimpleNamespace(execute_review=analyze))
    with rows.sessions() as editing:
        current = editing.get(CodeFile, rows.code_file.id)
        current.content = "value = 2\n"
        current.version_no = 2
        current.file_name = "renamed.py"
        editing.add(CodeVersion(
            file_id=current.id,
            version_no=2,
            content=current.content,
            create_time=datetime.now(timezone.utc),
        ))
        editing.commit()
    review_service._run_review_task(created.id, rows.user.id, created.execution_token)
    with rows.sessions() as observer:
        persisted = observer.get(ReviewTask, created.id)
        assert persisted.status == "success", persisted.error_message
        assert persisted.processed_files == 1
        assert persisted.score == 100
        assert captured == ["value = 1\n"]
        detail = TaskDetailOut.model_validate(review_service.get_task_detail(
            observer, observer.get(User, rows.user.id), created.id,
        ))
        assert detail.files[0].version_no == 1
        assert detail.files[0].file_name == "source.py"
        assert detail.coverage["stage"] == "complete"


@pytest.mark.parametrize(
    "operation,terminal", [("cancel_task", "cancelled"), ("delete_task", "deleted")],
)
def test_late_worker_cannot_revive_terminal_task_across_real_connections(
    isolated_rows, monkeypatch, operation, terminal,
):
    rows = isolated_rows
    created = start_without_dispatch(rows, monkeypatch)
    captured = []
    monkeypatch.setattr(review_service, "chunk_code_with_context", lambda *args, **kwargs: [
        SimpleNamespace(text="value = 1", start_line=0),
        SimpleNamespace(text="value = 2", start_line=1),
    ])

    def analyze(**kwargs):
        captured.append(kwargs["code"])
        with rows.sessions() as cancelling:
            getattr(review_service, operation)(cancelling, cancelling.get(User, rows.user.id), created.id)
        return AgentResult(success=True, data={"issues": []})

    monkeypatch.setattr(review_service, "_get_agent_for_profile", lambda code: SimpleNamespace(execute_review=analyze))
    review_service._run_review_task(created.id, rows.user.id, created.execution_token)
    with rows.sessions() as observer:
        persisted = observer.get(ReviewTask, created.id)
        assert persisted.status == terminal
        assert persisted.execution_token != created.execution_token
        assert persisted.end_time is not None
        assert captured == ["value = 1"]
        assert observer.query(ReviewIssue).filter_by(task_id=created.id).count() == 0


def test_dispatch_failure_cannot_revive_concurrently_deleted_task(isolated_rows, monkeypatch):
    rows = isolated_rows
    submitted_ids = []

    class FailingDispatch:
        def __init__(self, **kwargs):
            self.task_id = kwargs["args"][0]
            submitted_ids.append(self.task_id)

        def start(self):
            with rows.sessions() as deleting:
                review_service.delete_task(deleting, deleting.get(User, rows.user.id), self.task_id)
            raise RuntimeError("isolated cannot start new thread")

    monkeypatch.setattr(review_service.threading, "Thread", FailingDispatch)
    with pytest.raises(ValidationError, match="后台任务提交失败"):
        review_service.start(rows.database, rows.user, ReviewStartIn(
            project_id=rows.project.id,
            file_ids=[rows.code_file.id],
        ))
    with rows.sessions() as observer:
        persisted = observer.get(ReviewTask, submitted_ids[0])
        actual = {"status": persisted.status, "coverage": persisted.coverage, "error": persisted.error_message}
        assert persisted.status == "deleted", actual


def test_new_file_progress_does_not_inherit_completed_chunks(isolated_rows, monkeypatch):
    rows = isolated_rows
    second_file = CodeFile(
        project_id=rows.project.id,
        file_name="second.py",
        file_path="second.py",
        content="value = 2\n",
        language="python",
        status="active",
        size_bytes=10,
        line_count=1,
        version_no=1,
        is_binary=0,
    )
    rows.database.add(second_file)
    rows.task.total_files = 2
    rows.database.commit()
    observed = []

    def analyze(**kwargs):
        with rows.sessions() as observer:
            observed.append({
                "calling_file": kwargs["file_name"],
                "coverage": observer.get(ReviewTask, rows.task.id).coverage,
            })
        return AgentResult(success=True, data={"issues": []})

    monkeypatch.setattr(review_service, "_get_agent_for_profile", lambda code: SimpleNamespace(execute_review=analyze))
    review_service._execute_review(
        rows.database, Mock(), None, rows.task, rows.user,
        [rows.code_file, second_file], [], get_agent_profiles("standard"), "",
        execution_token="initial-lease",
    )
    assert observed[1]["calling_file"] == "second.py"
    assert observed[1]["coverage"]["current_file"] == "second.py"
    assert observed[1]["coverage"].get("completed_chunks", 0) == 0, observed[1]


def test_failed_chunk_does_not_prevent_later_chunks_from_running(isolated_rows, monkeypatch):
    rows = isolated_rows
    chunks = [
        SimpleNamespace(text="value = 1", start_line=0),
        SimpleNamespace(text="value = 2", start_line=1),
    ]
    invoked = []
    monkeypatch.setattr(review_service, "chunk_code_with_context", lambda *args, **kwargs: chunks)

    def review_chunk(*args, **kwargs):
        chunk_index = args[7]
        invoked.append(chunk_index)
        if chunk_index == 0:
            raise review_service.ReviewCoverageError(["finish_reason=length"])
        return []

    monkeypatch.setattr(review_service, "_review_chunk_sequential", review_chunk)
    review_service._execute_review(
        rows.database, Mock(), None, rows.task, rows.user, [rows.code_file], [],
        get_agent_profiles("standard"), "", execution_token="initial-lease",
    )

    rows.database.refresh(rows.task)
    ledger = rows.task.coverage["files"][str(rows.code_file.id)]
    assert invoked == [0, 1]
    assert rows.task.status == "failed"
    assert ledger["completed_chunk_indexes"] == [1]
    assert ledger["failed_chunks"][0]["index"] == 0


def test_failed_file_does_not_prevent_later_files_from_running(isolated_rows, monkeypatch):
    rows = isolated_rows
    second_file = CodeFile(
        id=999,
        project_id=rows.project.id,
        file_name="second.py",
        file_path="second.py",
        content="value = 2\n",
        language="python",
        status="active",
        size_bytes=10,
        line_count=1,
        version_no=1,
        is_binary=0,
    )
    rows.task.total_files = 2
    invoked = []

    def review_file(*args, **kwargs):
        code_file = args[4]
        invoked.append(code_file.file_name)
        if code_file.id == rows.code_file.id:
            raise review_service.ReviewCoverageError(["finish_reason=length"])
        return []

    monkeypatch.setattr(review_service, "_review_one_file", review_file)
    review_service._execute_review(
        rows.database, Mock(), None, rows.task, rows.user, [rows.code_file, second_file], [],
        get_agent_profiles("standard"), "", execution_token="initial-lease",
    )

    assert invoked == ["source.py", "second.py"]
    assert rows.task.status == "failed"
    assert rows.task.processed_files == 1


def test_one_collaborative_failure_keeps_other_agents_valid_evidence(isolated_rows, monkeypatch):
    rows = isolated_rows
    raw = json.dumps({
        "issues": [{
            "title": "retained result",
            "severity": "高",
            "line_number": 1,
            "issue_type": "安全漏洞",
            "description": "independent evidence",
            "evidence": "value = 1",
        }],
    })

    def analyze(profile, *args, **kwargs):
        if profile.code == get_agent_profiles("full")[0].code:
            return raw, {}
        raise RuntimeError("isolated unavailable agent")

    monkeypatch.setattr(review_service, "_call_single_agent", analyze)
    review_service._execute_review(
        rows.database, Mock(), None, rows.task, rows.user, [rows.code_file], [],
        get_agent_profiles("full"), "", execution_token="initial-lease",
    )
    with rows.sessions() as observer:
        persisted = observer.get(ReviewTask, rows.task.id)
        assert persisted.status == "failed"
        assert persisted.total_issues == observer.query(ReviewIssue).filter_by(task_id=rows.task.id).count() == 1
        assert persisted.score != 100


def test_invalid_aggregation_claim_marks_formal_review_incomplete(isolated_rows, monkeypatch):
    """聚合时被隔离的无效声明不能从完成口径中无声消失。"""
    rows = isolated_rows
    rows.task.review_type = "full"
    rows.database.commit()
    raw = json.dumps({
        "issues": [{
            "title": "retained result", "severity": "高", "line_number": 1,
            "issue_type": "安全漏洞", "description": "independent evidence",
            "evidence": "value = 1",
        }],
    })
    monkeypatch.setattr(review_service, "_call_single_agent", lambda *args, **kwargs: (raw, {}))
    aggregate = review_service.aggregate_agent_findings_safely

    def add_invalid_claim(findings, names, **kwargs):
        with_invalid = {code: list(items) for code, items in findings.items()}
        first_agent = next(iter(with_invalid))
        with_invalid[first_agent].append({
            "title": "discarded result", "severity": "invalid", "line_start": 1,
            "issue_type": "安全漏洞", "description": "must not vanish silently",
        })
        result = aggregate(with_invalid, names, **kwargs)
        assert result.coverage["invalid_input_count"] == 1
        return result

    monkeypatch.setattr(review_service, "aggregate_agent_findings_safely", add_invalid_claim)
    review_service._execute_review(
        rows.database, Mock(), None, rows.task, rows.user, [rows.code_file], [],
        get_agent_profiles("full"), "", execution_token="initial-lease",
    )

    with rows.sessions() as observer:
        persisted = observer.get(ReviewTask, rows.task.id)
        assert persisted.status == "failed"
        assert "聚合" in (persisted.error_message or "")
        assert persisted.total_issues == observer.query(ReviewIssue).filter_by(task_id=rows.task.id).count() == 1


def test_aggregation_fallback_marks_formal_review_incomplete(isolated_rows, monkeypatch):
    """聚合内部异常的人工待复核兜底不能伪装为协同审查已完成。"""
    rows = isolated_rows
    rows.task.review_type = "full"
    rows.database.commit()
    raw = json.dumps({
        "issues": [{
            "title": "retained fallback evidence", "severity": "高", "line_number": 1,
            "issue_type": "安全漏洞", "description": "independent evidence",
            "evidence": "value = 1",
        }],
    })
    monkeypatch.setattr(review_service, "_call_single_agent", lambda *args, **kwargs: (raw, {}))

    def aggregation_error(*args, **kwargs):
        raise RuntimeError("aggregation unavailable")

    monkeypatch.setattr(aggregator_module, "aggregate_agent_findings", aggregation_error)
    review_service._execute_review(
        rows.database, Mock(), None, rows.task, rows.user, [rows.code_file], [],
        get_agent_profiles("full"), "", execution_token="initial-lease",
    )

    with rows.sessions() as observer:
        persisted = observer.get(ReviewTask, rows.task.id)
        assert persisted.status == "failed"
        assert "聚合" in (persisted.error_message or "")
        assert persisted.total_issues == observer.query(ReviewIssue).filter_by(task_id=rows.task.id).count() == 1


def test_single_custom_profile_uses_collaborative_execution(isolated_rows, monkeypatch):
    from app.ai.multi_agent import ReviewAgentProfile

    rows = isolated_rows
    custom = ReviewAgentProfile(
        code="custom:isolated",
        name="隔离自定义代理",
        focus="只读",
        issue_types=(),
        instruction="isolated",
        is_custom=True,
    )
    invoked = []

    def analyze(profile, *args, **kwargs):
        invoked.append(profile.code)
        return '{"issues": []}', {}

    monkeypatch.setattr(review_service, "_call_single_agent", analyze)
    review_service._execute_review(
        rows.database, Mock(), None, rows.task, rows.user, [rows.code_file], [],
        (custom,), "", execution_token="initial-lease",
    )
    with rows.sessions() as observer:
        assert observer.get(ReviewTask, rows.task.id).status == "success"
        assert invoked == [custom.code]
