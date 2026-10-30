"""任务级「已记录的片段处理数」在所有出口口径一致（生产任务 #231 的形态）。

覆盖：报告详情、报告列表、任务详情、JSON/HTML/PDF/Word 导出以及导出共用的范围说明行。
每个出口都必须给出逐文件回执合计 54/54，不得再出现顶层覆盖计数留下的「1/1」，
也不得把缺回执的文件算成 0 或悄悄忽略。
"""
from __future__ import annotations

import io
import json

import pytest
from docx import Document

from app.models.project import Project
from app.models.review_task import ReviewTask
from app.services import report_pdf_exporter, review_service
from app.services.report_exporter import (
    export_to_dict,
    export_to_html,
    export_to_json,
    load_builtin_template,
)
from app.services.report_pdf_exporter import export_to_pdf
from app.services.report_service import get_report_detail, list_reports
from app.services.report_word_exporter import export_to_word

LABEL_54 = "已记录的片段处理数 54/54（48 个文件均有回执）"


def _ledger(missing: tuple[int, ...] = ()) -> dict:
    ledger: dict = {}
    for index in range(48):
        if index in missing:
            continue
        total = 2 if index < 6 else 1
        ledger[str(1000 + index)] = {
            "file_name": f"src/file_{index:02d}.php", "status": "complete",
            "total_chunks": total, "completed_chunks": total,
            "completed_chunk_indexes": list(range(total)), "failed_chunks": [],
        }
    return ledger


def _coverage(**overrides) -> dict:
    coverage = {
        "stage": "complete", "coverage_status": "complete", "completed_files": 48, "total_files": 48,
        # 顶层计数被最后一个文件覆盖：这是 #231 页面上「代码片段处理 1/1」的来源。
        "completed_chunks": 1, "total_chunks": 1, "current_file": "src/file_47.php",
        "files": _ledger(),
    }
    coverage.update(overrides)
    return coverage


def _make_task(db, admin_user, *, coverage=None, status="success", review_type="standard") -> ReviewTask:
    project = db.query(Project).first()
    if project is None:
        project = Project(user_id=admin_user.id, project_name="回执项目", language="php")
        db.add(project)
        db.commit()
    task = ReviewTask(
        user_id=admin_user.id, project_id=project.id, task_name="任务231", review_type=review_type,
        status=status, total_files=48, processed_files=48, total_issues=0, score=100,
        coverage=coverage if coverage is not None else _coverage(),
    )
    db.add(task)
    db.commit()
    return task


def _assert_aggregated(coverage: dict, label: str = LABEL_54) -> None:
    receipts = coverage["chunk_receipts"]
    assert receipts["known_total_chunks"] == 54
    assert receipts["known_completed_chunks"] == 54
    assert receipts["files_missing_receipt"] == 0
    assert receipts["label"] == label
    assert "completed_chunks" not in coverage and "total_chunks" not in coverage


def test_report_detail_reports_the_per_file_sum_not_the_last_file_counter(db, admin_user):
    task = _make_task(db, admin_user)

    detail = get_report_detail(db, admin_user, task.id)

    _assert_aggregated(detail["task"]["coverage"])
    assert "1/1" not in detail["task"]["coverage"]["chunk_receipts"]["label"]


def test_report_detail_names_files_without_receipt_instead_of_counting_them_as_zero(db, admin_user):
    coverage = _coverage(files=_ledger(missing=(3, 40)))
    task = _make_task(db, admin_user, coverage=coverage)

    receipts = get_report_detail(db, admin_user, task.id)["task"]["coverage"]["chunk_receipts"]

    assert receipts["files_missing_receipt"] == 2
    assert receipts["files_with_receipt"] == 46
    # 缺回执的文件是下标 3(2 片) 和 40(1 片)，已知合计 54 - 2 - 1 = 51。
    assert receipts["known_total_chunks"] == 51
    assert "另有 2 个文件缺少回执，片段数未知" in receipts["label"]


def test_legacy_task_without_coverage_reports_unknown_chunks(db, admin_user):
    task = _make_task(db, admin_user, coverage={"stage": "complete"})
    task.coverage = None
    db.commit()

    receipts = get_report_detail(db, admin_user, task.id)["task"]["coverage"]["chunk_receipts"]

    assert receipts["known_total_chunks"] is None
    assert "未知" in receipts["label"]
    assert "0/0" not in receipts["label"]


def test_report_list_uses_the_same_aggregate_as_the_detail(db, admin_user):
    task = _make_task(db, admin_user)

    item = next(row for row in list_reports(db, admin_user)["items"] if row["task_id"] == task.id)

    _assert_aggregated(item["coverage"])


def test_task_detail_uses_the_same_aggregate_when_finished(db, admin_user):
    task = _make_task(db, admin_user)

    coverage = review_service.get_task_detail(db, admin_user, task.id)["coverage"]

    _assert_aggregated(coverage)
    assert "current_file_chunks" not in coverage


def test_task_detail_keeps_current_file_progress_separate_while_running(db, admin_user):
    running = _coverage(
        stage="analyzing", completed_files=2, current_file="src/file_02.php",
        completed_chunks=1, total_chunks=2, files={k: v for k, v in list(_ledger().items())[:3]},
    )
    task = _make_task(db, admin_user, coverage=running, status="running")

    coverage = review_service.get_task_detail(db, admin_user, task.id)["coverage"]

    assert coverage["current_file_chunks"] == {"completed": 1, "total": 2}
    assert "completed_chunks" not in coverage and "total_chunks" not in coverage
    # 台账里的前 3 个文件（下标 0~2）各 2 片，合计 6。
    assert coverage["chunk_receipts"]["known_total_chunks"] == 6
    assert "进行中" in coverage["chunk_receipts"]["label"]


def test_roundtable_task_has_no_file_chunk_receipts(db, admin_user):
    task = _make_task(
        db, admin_user, review_type="discuss",
        coverage={"stage": "complete", "coverage_status": "complete", "valid_speeches": 5},
    )

    coverage = get_report_detail(db, admin_user, task.id)["task"]["coverage"]

    assert "chunk_receipts" not in coverage


def test_persisted_task_coverage_is_not_rewritten_by_reading(db, admin_user):
    task = _make_task(db, admin_user)
    before = json.dumps(task.coverage, sort_keys=True, ensure_ascii=False)

    get_report_detail(db, admin_user, task.id)
    review_service.get_task_detail(db, admin_user, task.id)
    db.expire_all()

    assert json.dumps(db.get(ReviewTask, task.id).coverage, sort_keys=True, ensure_ascii=False) == before


# ── 导出：JSON / HTML / PDF / Word 共用同一行范围说明 ──────────────────────────


def _export_task(**overrides) -> dict:
    task = {
        "id": 231, "task_name": "任务231", "project_name": "回执项目", "review_type": "standard",
        "status": "success", "total_files": 48, "processed_files": 48, "total_issues": 0,
        "severe_issues": 0, "high_issues": 0, "medium_issues": 0, "low_issues": 0, "score": 100,
        "coverage": _coverage(),
    }
    task.update(overrides)
    return task


def test_scope_lines_carry_the_aggregate_for_every_export_format():
    context = export_to_dict(_export_task(), [], "", 100)

    lines = "\n".join(context["scope_lines"])
    assert LABEL_54 in lines
    assert "1/1" not in lines
    assert "未展开的覆盖内部元数据" in lines or "chunk_receipts" not in lines


def test_json_export_has_the_aggregate_and_drops_the_ambiguous_counters():
    payload = json.loads(export_to_json(_export_task(), [], "", 100))

    coverage = payload["task_info"]["coverage"]
    _assert_aggregated(coverage)
    assert payload["task_info"]["coverage"]["files"], "逐文件回执本身仍随 JSON 导出，便于核对"


def test_html_word_and_pdf_exports_show_the_same_sentence(monkeypatch):
    task = _export_task()
    htmls = [
        export_to_html(task, [], "", 100, load_builtin_template(kind))
        for kind in ("simple", "detailed", "compliance")
    ]
    word = export_to_word(task, [], "", 100)
    pdf_paragraphs: list[str] = []
    original_paragraph = report_pdf_exporter.Paragraph

    def record(text, style, *args, **kwargs):
        paragraph = original_paragraph(text, style, *args, **kwargs)
        pdf_paragraphs.append(paragraph.getPlainText())
        return paragraph

    monkeypatch.setattr(report_pdf_exporter, "Paragraph", record)
    assert export_to_pdf(task, [], "", 100).startswith(b"%PDF")
    word_text = "\n".join(p.text for p in Document(io.BytesIO(word)).paragraphs)
    pdf_text = "\n".join(pdf_paragraphs)

    for rendered in (*htmls, word_text, pdf_text):
        assert LABEL_54 in rendered
        assert "代码片段处理" not in rendered
        assert "completed_chunks" not in rendered


def test_exports_state_unknown_for_files_without_receipt():
    task = _export_task(coverage=_coverage(files=_ledger(missing=(10,))))

    lines = "\n".join(export_to_dict(task, [], "", 100)["scope_lines"])

    assert "已记录的片段处理数 53/53" in lines
    assert "另有 1 个文件缺少回执，片段数未知" in lines


def test_export_normalization_is_idempotent_for_already_shaped_coverage():
    first = export_to_dict(_export_task(), [], "", 100)["task_info"]

    second = export_to_dict({**_export_task(), "coverage": first["coverage"]}, [], "", 100)["task_info"]

    assert second["coverage"]["chunk_receipts"] == first["coverage"]["chunk_receipts"]


@pytest.mark.parametrize("review_type", ["discuss", "security_scan", "fullchain_audit"])
def test_exports_of_types_without_file_chunk_ledger_have_no_chunk_line(review_type):
    task = _export_task(review_type=review_type, coverage={"coverage_status": "complete"})

    lines = "\n".join(export_to_dict(task, [], "", 100)["scope_lines"])

    assert "片段处理数" not in lines
