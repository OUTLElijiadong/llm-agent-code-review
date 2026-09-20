from types import SimpleNamespace
from unittest.mock import Mock

from app.agents.base import AgentResult
from app.agents.orchestrator import Orchestrator
from app.models.code_file import CodeFile


def test_default_review_does_not_fail_on_images_and_empty_files(db):
    for file_id, content, binary in [(11, "print(1)", 0), (12, "image", 1), (13, " \n\u3000", 0)]:
        db.add(CodeFile(id=file_id, project_id=7, file_name=f"{file_id}.txt", content=content,
                        is_binary=binary, status="active", language="plaintext", size_bytes=8, line_count=1))
    db.commit()
    orch = Orchestrator.__new__(Orchestrator)
    orch._db = db
    orch._disabled_result = Mock(return_value=None)
    orch.review_orch = SimpleNamespace(start_review=Mock(return_value=AgentResult(success=True, data={"task_id": 1})))
    result = orch.start_review(7)
    assert result.success
    assert orch.review_orch.start_review.call_args.args[1] == [11]
    excluded = orch.review_orch.start_review.call_args.kwargs["input_exclusions"]
    assert [(i["file_id"], i["reason"]) for i in excluded] == [(12, "binary"), (13, "empty_text")]


def test_default_selector_is_project_scoped_and_explicit_selection_is_unchanged(db):
    from app.services.review_input_service import select_project_review_inputs

    db.add(CodeFile(id=11, project_id=7, file_name="source.py", content="x=1", is_binary=0,
                    status="active", language="python", size_bytes=3, line_count=1))
    db.add(CodeFile(id=12, project_id=8, file_name="private.py", content="x=2", is_binary=0,
                    status="active", language="python", size_bytes=3, line_count=1))
    db.commit()
    assert select_project_review_inputs(db, 7) == ([11], [])
    orch = Orchestrator.__new__(Orchestrator)
    orch._db = db
    orch._disabled_result = Mock(return_value=None)
    orch.review_orch = SimpleNamespace(start_review=Mock(return_value=AgentResult(success=True)))
    orch.start_review(7, file_ids=[12])
    assert orch.review_orch.start_review.call_args.args[1] == [12]
