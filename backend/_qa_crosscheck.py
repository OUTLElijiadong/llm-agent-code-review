"""QA: 全站 API 聚合 ↔ 数据库直查 交叉核对(临时脚本)"""
import httpx
from sqlalchemy import func

from app.core.database import SessionLocal
from app.models.ai_call_log import AiCallLog
from app.models.audit_log import AuditLog
from app.models.code_file import CodeFile
from app.models.project import Project
from app.models.review_issue import ReviewIssue
from app.models.review_task import ReviewTask
from app.models.user import User

BASE = "http://localhost:8000"
CLIENT = httpx.Client(trust_env=False)


def login():
    """登录管理员账号并返回访问令牌。"""
    r = CLIENT.post(f"{BASE}/api/auth/login",
                    json={"username": "admin", "password": "admin123"}, timeout=30)
    return r.json()["data"]["access_token"]


def api(path, tok, **params):
    """请求本地 API 并返回 JSON 响应。"""
    r = CLIENT.get(f"{BASE}{path}", headers={"Authorization": f"Bearer {tok}"},
                   params=params, timeout=30)
    return r.json()


def chk(name, api_val, db_val):
    """比对 API 和数据库值，打印核对结果并返回是否一致。"""
    ok = api_val == db_val
    flag = "✅" if ok else "❌"
    print(f"{flag} {name:42} API={api_val!s:>8}  DB={db_val!s:>8}")
    return ok


def main():
    """执行全站聚合接口与数据库真值交叉核对。"""
    tok = login()
    db = SessionLocal()
    fails = []
    try:
        # ---- 全局计数(admin 视角=全部) ----
        # 报告列表 = status==success 的任务
        rep = api("/api/reports", tok, page=1, page_size=1)
        db_success = db.query(func.count(ReviewTask.id)).filter(
            ReviewTask.status == "success").scalar()
        if not chk("报告列表 total == DB任务(success)", rep["data"]["total"], db_success):
            fails.append("reports")

        # 审查任务列表 = status != deleted
        rv = api("/api/review/tasks", tok, page=1, page_size=1)
        db_tasks = db.query(func.count(ReviewTask.id)).filter(
            ReviewTask.status != "deleted").scalar()
        if not chk("审查任务 total == DB任务(!=deleted)", rv["data"]["total"], db_tasks):
            fails.append("review")

        # AI 调用日志
        al = api("/api/ai-logs", tok, page=1, page_size=1)
        db_logs = db.query(func.count(AiCallLog.id)).scalar()
        if not chk("AI调用日志 total == DB AiCallLog", al["data"]["total"], db_logs):
            fails.append("ai-logs")

        # 用户管理
        us = api("/api/users", tok, page=1, page_size=1)
        db_users = db.query(func.count(User.id)).scalar()
        if not chk("用户管理 total == DB User", us["data"]["total"], db_users):
            fails.append("users")

        # 系统审计
        au = api("/api/admin/audit", tok, page=1, page_size=1)
        db_audit = db.query(func.count(AuditLog.id)).scalar()
        if not chk("系统审计 total == DB AuditLog", au["data"]["total"], db_audit):
            fails.append("audit")

        # 项目列表(admin 看全部活跃)
        pj = api("/api/projects", tok, page=1, page_size=1)
        db_proj = db.query(func.count(Project.id)).filter(
            Project.status == "active").scalar()
        if not chk("项目列表 total == DB项目(active)", pj["data"]["total"], db_proj):
            fails.append("projects")

        # ---- 仪表盘 summary 字段逐项核对(admin scope=all == 全库) ----
        s = api("/api/dashboard/summary", tok, scope="all")["data"]
        db_files = db.query(func.count(CodeFile.id)).filter(
            CodeFile.status == "active").scalar()
        # 非删除任务的问题总数
        valid_task_ids = [t.id for t in db.query(ReviewTask.id).filter(
            ReviewTask.status != "deleted").all()]
        db_total_issues = db.query(func.count(ReviewIssue.id)).filter(
            ReviewIssue.task_id.in_(valid_task_ids)).scalar() if valid_task_ids else 0
        db_severe = db.query(func.count(ReviewIssue.id)).filter(
            ReviewIssue.task_id.in_(valid_task_ids),
            ReviewIssue.severity == "严重").scalar() if valid_task_ids else 0
        if not chk("仪表盘 file_count == DB文件(active)", s["file_count"], db_files):
            fails.append("dash-files")
        if not chk("仪表盘 review_count == DB任务(success)", s["review_count"], db_success):
            fails.append("dash-review")
        if not chk("仪表盘 total_issues == DB问题(非删除任务)", s["total_issues"], db_total_issues):
            fails.append("dash-issues")
        if not chk("仪表盘 severe_issues == DB严重问题", s["severe_issues"], db_severe):
            fails.append("dash-severe")

        # ---- 仪表盘图表端点内部一致性 ----
        risk = api("/api/dashboard/risk-distribution", tok, days=3650)["data"]
        rsum = sum(x.get("count", x.get("value", 0)) for x in risk)
        print(f"  [risk-distribution] 项={len(risk)} 计数和={rsum}")
        itype = api("/api/dashboard/issue-type-statistics", tok, days=3650)["data"]
        isum = sum(x.get("count", x.get("value", 0)) for x in itype)
        print(f"  [issue-type-stats] 项={len(itype)} 计数和={isum}")
        trend = api("/api/dashboard/score-trend", tok, limit=20)["data"]
        print(f"  [score-trend] 点数={len(trend)}")
        freq = api("/api/dashboard/review-frequency", tok, days=3650)["data"]
        print(f"  [review-frequency] 点数={len(freq)} 和={sum(x.get('count',0) for x in freq)}")

        # ---- 报告详情 stats 内部一致性(取最新讨论报告) ----
        items = api("/api/reports", tok, page=1, page_size=5)["data"]["items"]
        if items:
            tid = items[0]["task_id"]
            d = api(f"/api/reports/{tid}", tok)["data"]
            st = d["stats"]
            db_issue_n = db.query(func.count(ReviewIssue.id)).filter(
                ReviewIssue.task_id == tid).scalar()
            sev_sum = (st.get("severe", 0) + st.get("high", 0)
                       + st.get("medium", 0) + st.get("low", 0))
            if not chk(f"报告#{tid} stats.total==DB问题数", st["total_issues"], db_issue_n):
                fails.append("report-detail-total")
            if not chk(f"报告#{tid} 严重度之和==total", sev_sum, st["total_issues"]):
                fails.append("report-detail-sevsum")
            by_type_sum = sum(st.get("by_type", {}).values())
            if not chk(f"报告#{tid} by_type之和==total", by_type_sum, st["total_issues"]):
                fails.append("report-detail-bytype")

        # ---- Agent 调用统计 vs AiCallLog(关系合理性) ----
        rt = api("/api/agents/runtime", tok)["data"]
        sum_calls = sum(a.get("call_count", 0) for a in rt)
        print(f"  [agents] sum(call_count)={sum_calls}  DB AiCallLog={db_logs}  "
              f"(多代理日志一条计多代理,sum>=logs 合理)")
        if sum_calls < db_logs:
            print("  ⚠️ sum(call_count) < AiCallLog,可能有日志未被任何代理归因")
            fails.append("agent-attribution")

        # situation 在岗数 == runtime 中 status!=offline
        sit = api("/api/agents/situation", tok, **{"": ""})
        print(f"  [situation] {sit.get('data')}")

        print("\n=== 结论 ===")
        print("全部通过 ✅" if not fails else f"存在不一致: {fails}")
    finally:
        db.close()


if __name__ == "__main__":
    main()
