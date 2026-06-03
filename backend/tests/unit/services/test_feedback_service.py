"""单元测试: 反馈聚合服务(自进化 L0)"""
from app.services import feedback_service


def test_acceptance_and_false_positive_rates(db, mk_issue):
    """采纳率/假阳性率以已决数为分母,未处理问题不稀释信号"""
    # 安全漏洞: 3 fixed + 1 ignored → fp=0.25
    for i in range(3):
        mk_issue(db, issue_type="安全漏洞", status="fixed", task_id=1, handled_by=1)
    mk_issue(db, issue_type="安全漏洞", status="ignored", task_id=2, handled_by=2)
    # 未处理的不计入
    mk_issue(db, issue_type="安全漏洞", status="unfixed", task_id=1, handled_by=None)

    stats = {s["issue_type"]: s for s in feedback_service.aggregate_by_issue_type(db)}
    sec = stats["安全漏洞"]
    assert sec["fixed"] == 3 and sec["ignored"] == 1
    assert sec["decided"] == 4
    assert sec["acceptance_rate"] == 0.75
    assert sec["false_positive_rate"] == 0.25
    assert sec["rule_type"] == "security"


def test_cross_task_user_counts_for_anti_gaming(db, mk_issue):
    """跨任务/跨用户去偏计数,用于防翻车双门槛"""
    mk_issue(db, issue_type="性能问题", status="ignored", task_id=1, handled_by=1)
    mk_issue(db, issue_type="性能问题", status="ignored", task_id=2, handled_by=2)
    mk_issue(db, issue_type="性能问题", status="ignored", task_id=3, handled_by=1)
    mk_issue(db, issue_type="性能问题", status="fixed", task_id=1, handled_by=1)

    stat = next(s for s in feedback_service.aggregate_by_issue_type(db)
                if s["issue_type"] == "性能问题")
    assert stat["distinct_ignored_tasks"] == 3
    assert stat["distinct_ignored_users"] == 2
    assert stat["false_positive_rate"] == 0.75


def test_sorted_by_false_positive_desc(db, mk_issue):
    """结果按假阳性率降序,便于优先发现噪声"""
    mk_issue(db, issue_type="安全漏洞", status="fixed", task_id=1)
    mk_issue(db, issue_type="性能问题", status="ignored", task_id=1)
    mk_issue(db, issue_type="性能问题", status="ignored", task_id=2)
    rows = feedback_service.aggregate_by_issue_type(db)
    assert rows[0]["issue_type"] == "性能问题"  # fp=1.0 排最前


def test_summary_overall(db, mk_issue):
    mk_issue(db, issue_type="安全漏洞", status="fixed")
    mk_issue(db, issue_type="安全漏洞", status="ignored", task_id=2)
    s = feedback_service.summary(db)
    assert s["total_decided"] == 2
    assert s["overall_acceptance_rate"] == 0.5
    assert s["window_days"] == 90
