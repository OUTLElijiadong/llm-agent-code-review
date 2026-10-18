"""沙箱报告条目与字段行不能混计；历史读取不回写。"""
from datetime import datetime
from types import SimpleNamespace

import pytest

from app.models.project import Project
from app.models.review_report import ReviewReport
from app.models.review_task import ReviewTask
from app.services.review_service import get_task_detail, list_tasks
from app.services.sandbox_report_summary import (
    scope_sandbox_findings,
    scope_sandbox_report_markdown,
    summarize_sandbox_report,
)


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


@pytest.mark.parametrize("heading", ["## 问题清单", "## 4. 问题清单", "## 4、问题清单"])
def test_numbered_issue_heading_keeps_empty_and_findings_count_consistent(heading):
    assert summarize_sandbox_report(f"{heading}\n- 暂无问题。\n")['total'] == 0
    assert summarize_sandbox_report(f"{heading}\n### [高] 有证据的问题\n位置：x.py:1\n")['total'] == 1


def test_list_findings_exclude_nested_fields():
    result = summarize_sandbox_report('## 问题清单\n- [严重] 发现A\n  - 位置：x\n- 未分级发现B\n')
    assert result['total'] == 2
    assert result['severity_counts']['严重'] == 1
    assert result['unclassified'] == 1


def test_mixed_root_list_and_heading_findings_are_both_counted():
    report = ('## 问题清单\n- [低] 列表候选A\n### [高] 标题候选B\n'
              '- 证据：source code\n')
    result = summarize_sandbox_report(report)
    assert result['total'] == 2
    assert result['severity_counts']['低'] == 1
    assert result['severity_counts']['高'] == 1


def test_fenced_headings_and_other_sections_are_not_findings():
    result = summarize_sandbox_report('## 问题清单\n### 1. 发现A\n- 证据：\n~~~~python\n'
                                      '### 2. 假标题\n## 下一节\n- 假问题\n~~~~\n'
                                      '## 证据附录\n### 不属于问题清单\n')
    assert result['total'] == result['unclassified'] == 1


def test_fenced_h2_is_not_selected_as_the_issue_list():
    report = ('## 黑盒结果\n示例：\n```md\n## 问题清单\n### [高] 伪标题\n```\n'
              '## 4. 问题清单\n- 暂无问题。\n## 下一步建议\n复核\n')
    assert summarize_sandbox_report(report)['total'] == 0


def test_blackbox_report_does_not_turn_missing_whitebox_evidence_into_findings():
    report = (
        "## 总体结论\n\n黑盒路由未通过。\n\n"
        "## 白盒结果\n\n结论：未通过（无有效白盒证据）。\n\n"
        "## 证据\n\nrecon_facts 为空。\n\n"
        "## 发现\n\n### [高] 无任何白盒证据，白盒维度不可判定为通过。\n\n"
        "### [中] 项目未暴露白盒测试入口。\n\n"
        "## 建议\n\n修复白盒测试配置。\n\n"
        "## 黑盒结果\n\n结论：未通过（黑盒路由探测失败）。\n\n"
        "## 问题清单（按严重度）\n\n"
        "### 高\n1. 黑盒路由探测未发现 2xx 路由。\n"
        "2. 无任何白盒证据，白盒维度不可判定为通过。\n"
        "  证据：本次请求模式为 blackbox。\n"
        "## 下一步建议\n\n检查黑盒路径。\n"
    )

    scoped = scope_sandbox_report_markdown(report, "blackbox")

    assert "白盒验证未纳入范围" in scoped
    assert "不作白盒通过或失败判断" in scoped
    assert "黑盒路由探测未发现 2xx 路由" in scoped
    assert "无任何白盒证据" not in scoped
    assert "项目未暴露白盒测试入口" not in scoped
    assert "修复白盒测试配置" not in scoped
    assert summarize_sandbox_report(scoped)["total"] == 1


def test_blackbox_scope_keeps_in_scope_findings_in_mixed_general_section():
    report = (
        "## 总体结论\n\n黑盒路径探测失败；白盒测试失败，因为没有白盒证据。\n\n"
        "## 白盒结果\n\n白盒未运行。\n\n"
        "## 发现\n\n"
        "### [高] 白盒证据不足，白盒维度不可判定。\n\n该结论不属于本次范围。\n\n"
        "### [中] 黑盒暴露调试端点\n\n访问 /debug 可读到运行信息。\n\n"
        "## 黑盒结果\n\n/debug 返回敏感运行信息。\n"
    )

    scoped = scope_sandbox_report_markdown(report, "blackbox")

    assert "白盒验证未纳入范围" in scoped
    assert "黑盒路径探测失败" in scoped
    assert "白盒测试失败" not in scoped
    assert "白盒证据不足" not in scoped
    assert "该结论不属于本次范围" not in scoped
    assert "本次范围内发现" not in scoped
    assert "黑盒暴露调试端点" in scoped
    assert "访问 /debug 可读到运行信息" in scoped
    assert "## 黑盒结果" in scoped


def test_blackbox_scope_drops_neutral_finding_whose_only_evidence_is_unrun_whitebox():
    report = (
        "## 总体结论\n黑盒探测发现 /debug 暴露内部信息（白盒未执行）。\n"
        "## 下一步建议\n修复黑盒 /debug 信息泄露（白盒未执行）。\n"
        "## 问题清单\n### [高] 测试覆盖缺口\n"
        "证据：白盒测试未执行，因此缺少白盒证据。\n"
    )

    scoped = scope_sandbox_report_markdown(report, "blackbox")

    assert "黑盒探测发现 /debug 暴露内部信息" in scoped
    assert "修复黑盒 /debug 信息泄露" in scoped
    assert "白盒测试未执行，因此缺少白盒证据" not in scoped
    assert "### [高] 测试覆盖缺口" not in scoped
    summary = summarize_sandbox_report(scoped)
    assert summary["total"] == 0
    assert summary["severity_counts"]["高"] == 0


def test_blackbox_scope_removes_empty_nested_evidence_heading_and_keeps_group_severity():
    report = (
        "## 问题清单\n"
        "### 高\n"
        "#### 1. 测试覆盖缺口\n"
        "##### 证据\n"
        "白盒测试未执行，因此缺少白盒证据。\n"
        "#### 2. 黑盒端点可匿名访问\n"
        "##### 证据\n"
        "未授权请求返回敏感数据。\n"
    )

    scoped = scope_sandbox_report_markdown(report, "blackbox")
    summary = summarize_sandbox_report(scoped)

    assert "测试覆盖缺口" not in scoped
    assert "白盒测试未执行" not in scoped
    assert scoped.count("##### 证据") == 1
    assert "#### 2. 黑盒端点可匿名访问" in scoped
    assert summary["total"] == 1
    assert summary["severity_counts"]["高"] == 1
    assert summary["unclassified"] == 0


def test_blackbox_title_with_only_whitebox_absence_is_not_kept_as_a_finding():
    report = (
        "## 问题清单\n"
        "#### 1. 黑盒覆盖缺口（白盒测试未执行）\n"
        "##### 证据\n"
        "白盒测试未执行，因此缺少白盒证据。\n"
    )

    scoped = scope_sandbox_report_markdown(report, "blackbox")

    assert "黑盒覆盖缺口" not in scoped
    assert "##### 证据" not in scoped
    assert summarize_sandbox_report(scoped)["total"] == 0


def test_blackbox_scope_drops_explicit_whitebox_titles_but_keeps_parenthetical_scope_notes():
    report = (
        "## 问题清单\n"
        "### 高\n"
        "#### 白盒路径穿越\n"
        "证据：本地代码校验缺失。\n"
        "#### 黑盒 /debug 可匿名读取（白盒测试未执行）\n"
        "证据：未授权请求返回敏感数据。\n"
    )

    scoped = scope_sandbox_report_markdown(report, "blackbox")
    summary = summarize_sandbox_report(scoped)

    assert "白盒路径穿越" not in scoped
    assert "黑盒 /debug 可匿名读取" in scoped
    assert "白盒测试未执行" not in scoped
    assert summary["total"] == 1
    assert summary["severity_counts"]["高"] == 1


def test_blackbox_scope_drops_off_scope_mode_in_parenthetical_finding_title():
    report = (
        "## 问题清单\n"
        "### 高\n"
        "#### 路径穿越（白盒）\n"
        "本地代码校验缺失。\n"
    )

    scoped = scope_sandbox_report_markdown(report, "blackbox")

    assert "路径穿越" not in scoped
    assert summarize_sandbox_report(scoped)["total"] == 0
    assert summarize_sandbox_report(scoped)["severity_counts"]["高"] == 0


def test_blackbox_structured_findings_drop_explicit_whitebox_title_only():
    findings = [
        {
            "title": "白盒路径穿越",
            "severity": "高",
            "description": "用户路径未经规范化。",
        },
        {
            "title": "黑盒 /debug 可匿名读取（白盒测试未执行）",
            "severity": "高",
            "description": "未授权请求返回敏感数据。",
        },
    ]

    scoped = scope_sandbox_findings(findings, "blackbox")

    assert len(scoped) == 1
    assert scoped[0]["title"] == "黑盒 /debug 可匿名读取"
    assert scoped[0]["description"] == "未授权请求返回敏感数据。"


def test_blackbox_scope_keeps_actual_finding_when_whitebox_is_only_parenthetical_note():
    report = (
        "## 问题清单\n### [高] 黑盒 /debug 暴露内部信息\n"
        "证据：黑盒探测确认可匿名读取（白盒未执行）。\n"
    )

    scoped = scope_sandbox_report_markdown(report, "blackbox")

    assert "黑盒 /debug 暴露内部信息" in scoped
    assert "匿名读取" in scoped
    assert "白盒未执行" not in scoped
    assert summarize_sandbox_report(scoped)["total"] == 1


def test_blackbox_scope_filters_legacy_structured_off_scope_absence_findings():
    findings = [
        {
            "title": "测试覆盖缺口",
            "severity": "高",
            "description": "白盒测试未执行，因此缺少白盒证据。",
        },
        {
            "title": "黑盒 /debug 暴露内部信息",
            "severity": "高",
            "description": "可匿名读取运行信息（白盒未执行）。",
        },
    ]

    scoped = scope_sandbox_findings(findings, "blackbox")

    assert len(scoped) == 1
    assert scoped[0]["title"] == "黑盒 /debug 暴露内部信息"
    assert scoped[0]["description"] == "可匿名读取运行信息。"


def test_combined_sandbox_report_keeps_both_modes_unchanged():
    report = "## 白盒结果\n\n白盒发现。\n\n## 黑盒结果\n\n黑盒发现。\n"
    assert scope_sandbox_report_markdown(report, "combined") == report


def test_severity_group_heading_is_not_an_extra_finding():
    result = summarize_sandbox_report('## 问题清单\n### 高\n#### 1. 发现A\n- 位置：x\n'
                                      '#### 2. 发现B\n- 位置：y\n')
    assert result['total'] == 2
    assert result['severity_counts']['高'] == 2
    assert result['unclassified'] == 0


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


def test_publish_persists_ai_review_incomplete_status_and_failure_reason(db):
    from app.services.sandbox_service import _publish_sandbox_report

    environment = SimpleNamespace(owner_id=1, project_id=1, public_id='unit-ai-review-failure', started_at=None,
                                  stopped_at=None)
    conclusion = {
        'passed': True,
        'ai_review_status': 'incomplete',
        'ai_review_failure_reason': '模型未配置',
        'evidence': {'worker_result': {'exit_code': 0}},
    }

    published = _publish_sandbox_report(db, environment, conclusion, '## 总体结论\n执行通过')

    report = db.query(ReviewReport).filter_by(task_id=published['report_task_id']).one()
    assert report.content_json['ai_review'] == {'status': 'incomplete', 'failure_reason': '模型未配置'}


def test_history_161_reads_four_report_entries_without_mutating_task(db, admin_user):
    from app.services import dashboard_service, report_service
    project = Project(user_id=admin_user.id, project_name='验收项目')
    db.add(project)
    db.flush()
    task = ReviewTask(id=161, user_id=admin_user.id, project_id=project.id, review_type='sandbox_test',
                      status='success', total_files=1, processed_files=1, total_issues=16)
    db.add(task)
    db.add(ReviewReport(task_id=161, user_id=admin_user.id,
                        content_json={
                            'source': 'sandbox_test',
                            'report_md': report_with_four_findings(),
                            'ai_review': {'status': 'incomplete', 'failure_reason': '模型未配置'},
                        },
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
    assert report_detail['source']['ai_review'] == {
        'status': 'incomplete', 'failure_reason': '模型未配置',
    }
    assert report_service.list_reports(db, admin_user)['items'][0]['total_issues'] == 4
    assert report_service.list_reports(db, admin_user)['items'][0]['source']['ai_review']['status'] == 'incomplete'
    assert report_service.get_domain_report_export(db, admin_user, task, 'json')['statistics']['total_issues'] == 4
    assert dashboard_service.get_summary(db, admin_user)['total_issues'] == 4
    assert not db.dirty
    db.expire_all()
    assert (task.total_issues, task.status, task.coverage, task.update_time) == before


def test_owner_detail_derives_legacy_failure_reason_from_saved_runner_evidence(db, admin_user):
    project = Project(user_id=admin_user.id, project_name='失败回执项目')
    db.add(project)
    db.flush()
    task = ReviewTask(
        user_id=admin_user.id, project_id=project.id, task_name='沙箱黑白盒测试 · legacy',
        review_type='sandbox_test', status='failed', total_files=1, processed_files=1,
        total_issues=0, score=0,
        coverage={'verification_status': 'failed', 'requested_mode': None, 'reason': None},
    )
    db.add(task)
    db.flush()
    db.add(ReviewReport(
        task_id=task.id, user_id=admin_user.id,
        content_json={
            'source': 'sandbox_test', 'report_md': '## 总体结论\n未通过',
            'evidence': {
                'deterministic_test_execution': {'requested_mode': 'blackbox', 'status': 'failed'},
                'blackbox_execution': {
                    'status': 'failed', 'route': '/', 'status_code': 404,
                    'route_origin': 'none', 'failure_kind': 'route_or_dynamic_assertion',
                },
            },
        },
        create_time=datetime.now(),
    ))
    db.commit()

    detail = get_task_detail(db, admin_user, task.id)

    assert detail['can_view_report'] is True
    assert detail['coverage']['requested_mode'] == 'blackbox'
    assert 'HTTP 404' in detail['coverage']['reason']
    assert 'HTTP 404' in detail['error_message']


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
