"""沙箱报告条目与字段行不能混计；历史读取不回写。"""
from datetime import datetime
from types import SimpleNamespace

import pytest

from app.models.project import Project
from app.models.review_report import ReviewReport
from app.models.review_task import ReviewTask
from app.services.review_service import get_task_detail, list_tasks
from app.services.sandbox_report_summary import summarize_sandbox_report


def report_with_four_findings():
    return '## 问题清单\n' + '\n'.join(
        f'### {i}. 发现条目\n- 漏洞位置：代码位置\n- vulnerable code：\n```python\n- fake\n```\n'
        '- POC/复现：已有证据\n- 修复建议：修复方案\n'
        for i in range(1, 5)
    ) + '\n## 下一步建议\n- 不属于问题清单\n'


def test_field_bullets_and_code_are_not_findings():
    result = summarize_sandbox_report(report_with_four_findings())
    assert result['total'] == 4
    assert result['unclassified'] == 4
    assert result['severity_counts'] == {'严重': 0, '高': 0, '中': 0, '低': 0}
    assert result['basis'] == 'report_headings'


def test_explicit_levels_are_labels_not_inferred_from_description():
    report = ('## 问题清单\n### 1. [高] 发现A\n- 描述：影响低频操作\n'
              '### 2. 发现B\n- 严重程度：中\n### 3. 未分级项\n- 描述：高频操作\n')
    result = summarize_sandbox_report(report)
    assert result['total'] == 3
    assert result['severity_counts']['高'] == 1
    assert result['severity_counts']['中'] == 1
    assert result['unclassified'] == 1


def test_no_failure_placeholder_is_zero_and_missing_section_unknown():
    assert summarize_sandbox_report('## 问题清单\n- 未发现确定性执行失败。')['total'] == 0
    assert summarize_sandbox_report('## 总体结论\n报告缺少清单')['total'] is None
    assert summarize_sandbox_report('## 问题清单\n## 下一步建议')['total'] is None


def test_list_findings_exclude_nested_fields():
    result = summarize_sandbox_report('## 问题清单\n- [严重] 发现A\n  - 位置：x\n- 未分级发现B\n')
    assert result['total'] == 2
    assert result['severity_counts']['严重'] == 1
    assert result['unclassified'] == 1


def test_fenced_headings_and_other_sections_are_not_findings():
    result = summarize_sandbox_report('## 问题清单\n### 1. 发现A\n- 证据：\n~~~~python\n'
                                      '### 2. 假标题\n## 下一节\n- 假问题\n~~~~\n'
                                      '## 证据附录\n### 不属于问题清单\n')
    assert result['total'] == result['unclassified'] == 1


def test_severity_group_heading_is_not_an_extra_finding():
    result = summarize_sandbox_report('## 问题清单\n### 高\n#### 1. 发现A\n- 位置：x\n'
                                      '#### 2. 发现B\n- 位置：y\n')
    assert result['total'] == 2


def test_field_subheadings_belong_to_parent_finding():
    report = '## 问题清单\n' + '\n'.join(
        f'### {i}. 发现\n#### 漏洞位置\n代码位置\n#### vulnerable code\n代码证据\n'
        '#### POC/复现\n实测\n#### 修复建议\n建议\n' for i in range(1, 5)
    )
    result = summarize_sandbox_report(report)
    assert result['total'] == result['unclassified'] == 4
    assert summarize_sandbox_report('## 问题清单\n### 漏洞位置\n位置\n### 修复建议\n建议')['total'] is None


@pytest.mark.parametrize('fields', [
    '#### 1. 漏洞位置\n示例\n#### 2. 修复建议\n示例',
    '#### 漏洞位置：示例路径\n#### 修复建议：示例写法',
    '#### **1. 漏洞位置**：示例路径\n#### **2. 修复建议**：示例写法',
])
def test_numbered_and_inline_field_headings_are_not_findings(fields):
    result = summarize_sandbox_report('## 问题清单\n### 发现A\n' + fields)
    assert result['total'] == result['unclassified'] == 1


def test_publish_reuses_summary_for_new_and_republished_sandbox_reports(db):
    from app.services.sandbox_service import _publish_sandbox_report

    environment = SimpleNamespace(owner_id=1, project_id=1, public_id='unit-report', started_at=None, stopped_at=None)
    published = _publish_sandbox_report(db, environment, {'passed': True}, report_with_four_findings())
    task = db.get(ReviewTask, published['report_task_id'])
    assert task.total_issues == 4
    report = db.query(ReviewReport).filter_by(task_id=task.id).one()
    assert report.content_json['report_issue_summary']['unclassified'] == 4
    republished = _publish_sandbox_report(db, environment, {'passed': True}, '## 问题清单\n- [高] 一个发现\n')
    assert republished['report_task_id'] == task.id
    assert (task.total_issues, task.high_issues, task.severe_issues) == (1, 1, 0)
    assert db.query(ReviewTask).count() == 1


def test_history_161_reads_four_report_entries_without_mutating_task(db, admin_user):
    from app.services import dashboard_service, report_service
    project = Project(user_id=admin_user.id, project_name='验收项目')
    db.add(project)
    db.flush()
    task = ReviewTask(id=161, user_id=admin_user.id, project_id=project.id, review_type='sandbox_test',
                      status='success', total_files=1, processed_files=1, total_issues=16)
    db.add(task)
    db.add(ReviewReport(task_id=161, user_id=admin_user.id,
                        content_json={'source':'sandbox_test','report_md':report_with_four_findings()},
                        create_time=datetime.now()))
    db.commit()
    db.refresh(task)
    before = (task.total_issues, task.status, task.coverage, task.update_time)
    detail = get_task_detail(db, admin_user, 161)
    assert detail['total_issues'] == 4
    assert detail['report_issue_summary']['total'] == 4
    assert detail['report_issue_summary']['unclassified'] == 4
    assert detail['report_issue_summary']['structured_issues'] == 0
    assert list_tasks(db, admin_user)['items'][0]['total_issues'] == 4
    report_detail = report_service.get_report_detail(db, admin_user, 161)
    assert report_detail['stats']['total_issues'] == 4
    assert report_detail['stats']['severity'] == {'未分级': 4}
    assert report_service.list_reports(db, admin_user)['items'][0]['total_issues'] == 4
    assert report_service.get_domain_report_export(db, admin_user, task, 'json')['statistics']['total_issues'] == 4
    assert dashboard_service.get_summary(db, admin_user)['total_issues'] == 4
    assert not db.dirty
    db.expire_all()
    assert (task.total_issues, task.status, task.coverage, task.update_time) == before


def test_standard_review_keeps_structured_contract(db, admin_user):
    project = Project(user_id=admin_user.id, project_name='普通审查')
    db.add(project)
    db.flush()
    task = ReviewTask(user_id=admin_user.id, project_id=project.id, review_type='full',status='success',
                      total_issues=2, severe_issues=1, high_issues=1)
    db.add(task)
    db.commit()
    detail = get_task_detail(db, admin_user, task.id)
    assert detail['total_issues'] == 2
    assert detail['severe_issues'] == detail['high_issues'] == 1
    assert detail['report_issue_summary'] is None
