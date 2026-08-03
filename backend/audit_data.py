"""
前后端数据一致性核验脚本

通过 pymysql 直连 MySQL 获取数据库基准数据, 再通过 httpx 调用后端 API 获取接口返回值,
逐字段对比, 以表格形式输出差异。

使用方式:
    cd backend && python audit_data.py

依赖: pip install pymysql httpx
"""

import json
import os
import sys
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple, Union

import httpx
import pymysql

# ======================== 配置 ========================

DB_HOST = "127.0.0.1"
DB_PORT = 3307
DB_USER = "root"
DB_PASSWORD = os.environ.get("AUDIT_DB_PASSWORD", "")
DB_NAME = "code_review"
DB_CHARSET = "utf8mb4"

API_BASE = "http://localhost:8000/api"
LOGIN_URL = f"{API_BASE}/auth/login"
LOGIN_USERNAME = "admin"
LOGIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")

# 各 API 分页大小
PROJECT_PAGE_SIZE = 20
CODE_FILE_PAGE_SIZE = 100
REVIEW_PAGE_SIZE = 20
ISSUE_PAGE_SIZE = 50
REPORT_PAGE_SIZE = 20

COMPARE_PASS = "   ✅"
COMPARE_FAIL = "❌"

# ======================== 数据库工具 ========================


def _get_db_connection() -> pymysql.Connection:
    """
    创建并返回 MySQL 数据库连接

    Returns:
        pymysql.Connection: 数据库连接对象
    """
    return pymysql.connect(
        host=DB_HOST,
        port=DB_PORT,
        user=DB_USER,
        password=DB_PASSWORD,
        database=DB_NAME,
        charset=DB_CHARSET,
        cursorclass=pymysql.cursors.DictCursor,
    )


def _db_query(conn: pymysql.Connection, sql: str, params: tuple = ()) -> List[Dict]:
    """
    执行 SQL 查询, 返回字典列表

    Args:
        conn: 数据库连接
        sql: SQL 查询语句
        params: 查询参数

    Returns:
        List[Dict]: 查询结果列表
    """
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchall()


def _db_scalar(conn: pymysql.Connection, sql: str, params: tuple = ()) -> Any:
    """
    执行 SQL 查询, 返回单个标量值

    Args:
        conn: 数据库连接
        sql: SQL 查询语句
        params: 查询参数

    Returns:
        Any: 标量值, 无结果时返回 None
    """
    with conn.cursor() as cur:
        cur.execute(sql, params)
        row = cur.fetchone()
        if row is None:
            return None
        return list(row.values())[0]


# ======================== HTTP 工具 ========================


class ApiClient:
    """
    封装 httpx 客户端, 自动附加 Bearer Token
    """

    def __init__(self, base_url: str):
        """
        初始化客户端

        Args:
            base_url: API 基础 URL
        """
        self.base_url = base_url
        self._token: str = ""
        self._client: Optional[httpx.Client] = None

    def login(self, username: str, password: str) -> str:
        """
        登录获取 access_token

        Args:
            username: 用户名
            password: 密码

        Returns:
            str: access_token

        Raises:
            RuntimeError: 登录失败时抛出
        """
        resp = httpx.post(
            LOGIN_URL,
            json={"username": username, "password": password},
            timeout=10.0,
        )
        if resp.status_code != 200:
            raise RuntimeError(f"登录失败, HTTP {resp.status_code}: {resp.text}")
        body = resp.json()
        if body.get("code") != 0:
            raise RuntimeError(f"登录失败: {body.get('message')} (code={body.get('code')})")
        self._token = body["data"]["access_token"]
        self._client = httpx.Client(
            base_url=self.base_url,
            headers={"Authorization": f"Bearer {self._token}"},
            timeout=30.0,
        )
        return self._token

    def get(self, path: str, params: Optional[Dict] = None) -> Dict:
        """
        发送 GET 请求

        Args:
            path: API 路径(相对 base_url)
            params: 查询参数

        Returns:
            Dict: 响应 JSON body
        """
        if self._client is None:
            raise RuntimeError("尚未登录, 请先调用 login()")
        resp = self._client.get(path, params=params)
        resp.raise_for_status()
        return resp.json()

    def close(self):
        """关闭 HTTP 客户端"""
        if self._client:
            self._client.close()
            self._client = None


def _ensure_token(api: ApiClient):
    """
    确保 ApiClient 已登录

    Args:
        api: ApiClient 实例
    """
    if api._token:
        return
    print(f"正在登录 {LOGIN_URL} (user={LOGIN_USERNAME})...")
    token = api.login(LOGIN_USERNAME, LOGIN_PASSWORD)
    print(f"登录成功, token 前8位: {token[:8]}...")


# ======================== 输出格式化 ========================


def _print_section_header(title: str):
    """
    打印核验区块标题

    Args:
        title: 标题文本
    """
    print()
    print("=" * 80)
    print(f"  {title}")
    print("=" * 80)
    print()


def _print_compare_table(
    title: str,
    rows: List[Tuple[str, Any, Any, str]],
):
    """
    以表格形式打印字段对比结果

    Args:
        title: 表格标题
        rows: [(字段名, API值, DB值, 状态标记), ...]
    """
    if not rows:
        print(f"  [{title}] 无数据")
        return
    print(f"  [{title}]")
    print(f"  {'字段名':<30} | {'API 返回值':<20} | {'数据库值':<20} | 状态")
    print(f"  {'-'*30}-+-{'-'*20}-+-{'-'*20}-+------")
    for field, api_val, db_val, status in rows:
        api_str = _fmt(api_val)
        db_str = _fmt(db_val)
        print(f"  {field:<30} | {api_str:<20} | {db_str:<20} | {status}")
    print()


def _fmt(val: Any) -> str:
    """
    格式化值为字符串用于表格展示

    Args:
        val: 任意值

    Returns:
        str: 格式化后的字符串
    """
    if val is None:
        return "(null)"
    if isinstance(val, float):
        return f"{val:.2f}"
    if isinstance(val, datetime):
        return val.isoformat()
    return str(val)[:20]


def _compare_value(api_val: Any, db_val: Any) -> str:
    """
    比较两个值是否一致

    Args:
        api_val: API 返回的值
        db_val: 数据库查询的值

    Returns:
        str: 一致返回 COMPARE_PASS 标记, 不一致返回 COMPARE_FAIL 标记
    """
    if isinstance(db_val, datetime) and isinstance(api_val, str):
        try:
            db_val = db_val.isoformat()
        except Exception:
            pass
    if api_val == db_val:
        return COMPARE_PASS
    if isinstance(api_val, float) and isinstance(db_val, float):
        if abs(api_val - db_val) < 0.01:
            return COMPARE_PASS
    return COMPARE_FAIL


# ======================== 核验函数 ========================


def verify_dashboard_summary(api: ApiClient, conn: pymysql.Connection):
    """核验仪表盘汇总接口 GET /api/dashboard/summary"""
    _print_section_header("仪表盘汇总: GET /api/dashboard/summary")

    # --- 数据库基准 ---
    db_project_count = _db_scalar(
        conn, "SELECT COUNT(*) FROM project WHERE status != 'deleted'"
    )
    db_file_count = _db_scalar(
        conn,
        "SELECT COUNT(*) FROM code_file cf "
        "JOIN project p ON p.id = cf.project_id "
        "WHERE cf.status = 'active' AND p.status != 'deleted'",
    )
    db_review_count = _db_scalar(
        conn, "SELECT COUNT(*) FROM review_task WHERE status = 'success'"
    )
    db_total_issues = _db_scalar(conn, "SELECT COUNT(*) FROM review_issue")
    db_severe_issues = _db_scalar(
        conn, "SELECT COUNT(*) FROM review_issue WHERE severity = '严重'"
    )
    avg_row = _db_scalar(
        conn, "SELECT AVG(score) FROM review_task WHERE status = 'success'"
    )
    db_avg_score = round(avg_row or 0, 1)

    db_data = {
        "project_count": db_project_count,
        "file_count": db_file_count,
        "review_count": db_review_count,
        "total_issues": db_total_issues,
        "severe_issues": db_severe_issues,
        "avg_score": db_avg_score,
    }

    # --- API 数据 ---
    resp = api.get("/dashboard/summary", params={"scope": "mine"})
    api_data = resp.get("data", {})
    if not api_data:
        print("  [错误] API 返回的 data 为空")
        return

    fields: List[Tuple[str, str, Any]] = [
        ("project_count", "project_count", int),
        ("file_count", "file_count", int),
        ("review_count", "review_count", int),
        ("total_issues", "total_issues", int),
        ("severe_issues", "severe_issues", int),
        ("avg_score", "avg_score", float),
    ]

    rows = []
    for field, api_key, cast_fn in fields:
        api_val = api_data.get(api_key)
        db_val = db_data[field]
        if isinstance(api_val, (int, float)) and not isinstance(api_val, bool):
            api_val = cast_fn(api_val)
        if isinstance(db_val, (int, float)) and not isinstance(db_val, bool):
            db_val = cast_fn(db_val)
        status = _compare_value(api_val, db_val)
        rows.append((field, api_val, db_val, status))
    _print_compare_table("汇总指标", rows)


def verify_dashboard_risk(api: ApiClient, conn: pymysql.Connection):
    """核验风险分布接口 GET /api/dashboard/risk-distribution"""
    _print_section_header("风险分布: GET /api/dashboard/risk-distribution")

    # --- 数据库基准 ---
    db_rows = _db_query(
        conn,
        "SELECT severity, COUNT(*) AS cnt FROM review_issue GROUP BY severity",
    )
    db_risk: Dict[str, int] = {"严重": 0, "高": 0, "中": 0, "低": 0}
    for row in db_rows:
        if row["severity"] in db_risk:
            db_risk[row["severity"]] = row["cnt"]

    # --- API 数据 ---
    resp = api.get("/dashboard/risk-distribution", params={"days": 3650})
    api_risk_list = resp.get("data", [])
    api_risk: Dict[str, int] = {}
    for item in api_risk_list:
        api_risk[item["severity"]] = item.get("count", 0)

    rows = []
    for severity in ["严重", "高", "中", "低"]:
        api_val = api_risk.get(severity, 0)
        db_val = db_risk.get(severity, 0)
        status = _compare_value(api_val, db_val)
        rows.append((f"severity={severity}", api_val, db_val, status))
    _print_compare_table("issue severity 分布", rows)


def verify_projects_list(api: ApiClient, conn: pymysql.Connection):
    """核验项目列表接口 GET /api/projects"""
    _print_section_header("项目列表: GET /api/projects?page=1&page_size=20")

    # --- 数据库基准 ---
    db_total = _db_scalar(
        conn, "SELECT COUNT(*) FROM project WHERE status != 'deleted'"
    )

    db_projects = _db_query(
        conn,
        "SELECT "
        "  p.id, p.project_name, p.status, "
        "  (SELECT COUNT(*) FROM code_file cf "
        "   WHERE cf.project_id = p.id AND cf.status = 'active') AS file_count, "
        "  (SELECT MAX(rt.create_time) FROM review_task rt "
        "   WHERE rt.project_id = p.id AND rt.status = 'success') AS last_review_at, "
        "  (SELECT rt.score FROM review_task rt "
        "   WHERE rt.project_id = p.id AND rt.status = 'success' "
        "   ORDER BY rt.create_time DESC LIMIT 1) AS score "
        "FROM project p WHERE p.status != 'deleted' "
        "ORDER BY p.create_time DESC LIMIT %s",
        (PROJECT_PAGE_SIZE,),
    )

    # --- API 数据 ---
    resp = api.get(
        "/projects",
        params={"page": 1, "page_size": PROJECT_PAGE_SIZE, "status": "active"},
    )
    page_data = resp.get("data", {})
    api_total = page_data.get("total", 0)
    api_items = page_data.get("items", [])

    rows = [("列表总数", api_total, db_total, _compare_value(api_total, db_total))]
    _print_compare_table("项目数量", rows)

    if not api_items and not db_projects:
        print("  [项目列表] 双方均为空, 跳过逐项对比")
        return

    api_by_id = {p["id"]: p for p in api_items if "id" in p}
    db_by_id = {p["id"]: p for p in db_projects if "id" in p}
    all_ids = sorted(set(list(api_by_id.keys()) + list(db_by_id.keys())))

    print(f"  [项目逐项对比] 共 {len(all_ids)} 个项目")
    print(f"  {'项目ID':<8} | {'字段':<18} | {'API 值':<22} | {'DB 值':<22} | 状态")
    print(f"  {'-'*8}-+-{'-'*18}-+-{'-'*22}-+-{'-'*22}-+------")

    for pid in all_ids:
        api_p = api_by_id.get(pid)
        db_p = db_by_id.get(pid)

        if api_p is None:
            print(f"  {pid:<8} | {'(仅DB有)':<18} | {'':<22} | {db_p['project_name']:<22} | {COMPARE_FAIL}")
            continue
        if db_p is None:
            print(f"  {pid:<8} | {'(仅API有)':<18} | {api_p['project_name']:<22} | {'':<22} | {COMPARE_FAIL}")
            continue

        for field, api_key in [
            ("project_name", "project_name"),
            ("status", "status"),
            ("score", "score"),
        ]:
            api_val = api_p.get(api_key)
            db_val = db_p.get(field)
            if isinstance(api_val, float) and field == "score":
                api_val = int(api_val) if api_val == int(api_val) else api_val
            status = _compare_value(api_val, db_val)
            print(f"  {pid:<8} | {field:<18} | {_fmt(api_val):<22} | {_fmt(db_val):<22} | {status}")
    print()


def verify_project_detail(api: ApiClient, conn: pymysql.Connection):
    """核验项目详情接口 GET /api/projects/{id}"""
    project_ids = _db_query(
        conn, "SELECT id FROM project WHERE status != 'deleted' ORDER BY id LIMIT 1"
    )
    if not project_ids:
        print("  [项目详情] 数据库无项目, 跳过")
        return

    pid = project_ids[0]["id"]
    _print_section_header(f"项目详情: GET /api/projects/{pid}")

    db_project = _db_query(
        conn,
        "SELECT "
        "  id, project_name, description, language, status, "
        "  (SELECT COUNT(*) FROM code_file cf WHERE cf.project_id = p.id AND cf.status = 'active') AS file_count, "
        "  create_time, update_time "
        "FROM project p WHERE id = %s",
        (pid,),
    )
    if not db_project:
        print(f"  [错误] 数据库查询不到项目 id={pid}")
        return
    db_p = db_project[0]

    resp = api.get(f"/projects/{pid}")
    api_p = resp.get("data", {})
    if not api_p:
        print(f"  [错误] API 返回的项目详情 data 为空 (id={pid})")
        return

    rows = []
    for field in [
        "id", "project_name", "description", "language", "status", "file_count",
    ]:
        api_val = api_p.get(field)
        db_val = db_p.get(field)
        if field == "description":
            api_val = api_val or None
            db_val = db_val or None
        status = _compare_value(api_val, db_val)
        rows.append((field, api_val, db_val, status))
    _print_compare_table(f"项目详情 (id={pid})", rows)


def verify_code_files(api: ApiClient, conn: pymysql.Connection):
    """核验代码文件接口 GET /api/code-files?project_id=1"""
    project_ids = _db_query(
        conn, "SELECT id FROM project WHERE status != 'deleted' ORDER BY id LIMIT 1"
    )
    if not project_ids:
        print("  [代码文件] 数据库无项目, 跳过")
        return

    pid = project_ids[0]["id"]
    _print_section_header(f"代码文件: GET /api/code-files?project_id={pid}")

    db_total = _db_scalar(
        conn,
        "SELECT COUNT(*) FROM code_file WHERE project_id = %s AND status = 'active'",
        (pid,),
    )

    resp = api.get(
        "/code-files",
        params={
            "project_id": pid,
            "page": 1,
            "page_size": CODE_FILE_PAGE_SIZE,
        },
    )
    page_data = resp.get("data", {})
    api_total = page_data.get("total", 0)
    api_items = page_data.get("items", [])

    rows = [
        ("文件总数", api_total, db_total, _compare_value(api_total, db_total)),
        ("API返回条数", len(api_items), min(db_total, CODE_FILE_PAGE_SIZE),
         _compare_value(len(api_items), min(db_total, CODE_FILE_PAGE_SIZE))),
    ]
    _print_compare_table(f"文件数量 (project_id={pid})", rows)


def verify_review_list(api: ApiClient, conn: pymysql.Connection):
    """核验审查任务列表接口 GET /api/review/tasks"""
    _print_section_header("审查任务列表: GET /api/review/tasks?page=1&page_size=20")

    db_total = _db_scalar(conn, "SELECT COUNT(*) FROM review_task")

    resp = api.get(
        "/review/tasks",
        params={"page": 1, "page_size": REVIEW_PAGE_SIZE},
    )
    page_data = resp.get("data", {})
    api_total = page_data.get("total", 0)
    api_items = page_data.get("items", [])

    rows = [
        ("任务总数", api_total, db_total, _compare_value(api_total, db_total)),
        ("返回条数", len(api_items), min(db_total, REVIEW_PAGE_SIZE),
         _compare_value(len(api_items), min(db_total, REVIEW_PAGE_SIZE))),
    ]
    _print_compare_table("审查任务数量", rows)

    if not api_items:
        print("  [审查任务列表] API 无数据, 跳过逐项对比")
        return

    api_by_id = {t["id"]: t for t in api_items if "id" in t}
    task_ids = list(api_by_id.keys())[:5]
    if not task_ids:
        return

    placeholders = ",".join(["%s"] * len(task_ids))
    db_tasks = _db_query(
        conn,
        f"SELECT id, status, review_type, total_issues, score FROM review_task WHERE id IN ({placeholders}) "
        f"ORDER BY id",
        tuple(task_ids),
    )
    db_by_id = {t["id"]: t for t in db_tasks}

    print(f"  [审查任务逐项对比] 取前 {len(task_ids)} 条")
    print(f"  {'任务ID':<8} | {'字段':<18} | {'API 值':<22} | {'DB 值':<22} | 状态")
    print(f"  {'-'*8}-+-{'-'*18}-+-{'-'*22}-+-{'-'*22}-+------")

    for tid in task_ids:
        api_t = api_by_id.get(tid)
        db_t = db_by_id.get(tid)
        if db_t is None:
            print(f"  {tid:<8} | {'(DB无此记录)':<18} | {'':<22} | {'':<22} | {COMPARE_FAIL}")
            continue
        for field in ["status", "review_type", "total_issues", "score"]:
            api_val = api_t.get(field)
            db_val = db_t.get(field)
            status = _compare_value(api_val, db_val)
            print(f"  {tid:<8} | {field:<18} | {_fmt(api_val):<22} | {_fmt(db_val):<22} | {status}")
    print()


def verify_review_detail(api: ApiClient, conn: pymysql.Connection):
    """核验审查任务详情接口 GET /api/review/tasks/{id}"""
    task_ids = _db_query(conn, "SELECT id FROM review_task ORDER BY id LIMIT 1")
    if not task_ids:
        print("  [审查任务详情] 数据库无任务, 跳过")
        return

    tid = task_ids[0]["id"]
    _print_section_header(f"审查任务详情: GET /api/review/tasks/{tid}")

    db_task = _db_query(
        conn,
        "SELECT id, task_name, review_type, status, total_files, processed_files, "
        "total_issues, severe_issues, high_issues, medium_issues, low_issues, "
        "score, summary, model_name, duration_ms "
        "FROM review_task WHERE id = %s",
        (tid,),
    )
    if not db_task:
        print(f"  [错误] 数据库查询不到审查任务 id={tid}")
        return
    db_t = db_task[0]

    resp = api.get(f"/review/tasks/{tid}")
    api_t = resp.get("data", {})
    if not api_t:
        print(f"  [错误] API 返回的任务详情 data 为空 (id={tid})")
        return

    rows = []
    for field in [
        "id", "task_name", "review_type", "status",
        "total_files", "processed_files", "total_issues",
        "severe_issues", "high_issues", "medium_issues", "low_issues",
        "score", "summary", "model_name", "duration_ms",
    ]:
        api_val = api_t.get(field)
        db_val = db_t.get(field)
        if field in ("summary", "task_name", "model_name"):
            api_val = api_val or None
            db_val = db_val or None
        status = _compare_value(api_val, db_val)
        rows.append((field, api_val, db_val, status))
    _print_compare_table(f"审查任务详情 (id={tid})", rows)


def verify_issues(api: ApiClient, conn: pymysql.Connection):
    """核验问题列表接口 GET /api/issues"""
    _print_section_header("问题列表: GET /api/issues?page=1&page_size=50")

    db_total = _db_scalar(conn, "SELECT COUNT(*) FROM review_issue")

    resp = api.get(
        "/issues",
        params={"page": 1, "page_size": ISSUE_PAGE_SIZE},
    )
    page_data = resp.get("data", {})
    api_total = page_data.get("total", 0)
    api_items = page_data.get("items", [])

    rows = [
        ("问题总数", api_total, db_total, _compare_value(api_total, db_total)),
        ("返回条数", len(api_items), min(db_total, ISSUE_PAGE_SIZE),
         _compare_value(len(api_items), min(db_total, ISSUE_PAGE_SIZE))),
    ]
    _print_compare_table("issue 数量", rows)

    # severity 分布对比
    db_severity_rows = _db_query(
        conn,
        "SELECT severity, COUNT(*) AS cnt FROM review_issue GROUP BY severity",
    )
    db_severity = {r["severity"]: r["cnt"] for r in db_severity_rows}

    api_severity: Dict[str, int] = {}
    for item in api_items:
        sev = item.get("severity", "")
        api_severity[sev] = api_severity.get(sev, 0) + 1

    all_sevs = sorted(set(list(db_severity.keys()) + list(api_severity.keys())))
    sev_rows = []
    for sev in all_sevs:
        api_val = api_severity.get(sev, 0)
        db_val = db_severity.get(sev, 0)
        sev_rows.append((f"severity={sev}", api_val, db_val, _compare_value(api_val, db_val)))
    _print_compare_table("返回数据中 severity 计数", sev_rows)

    # status 分布
    api_status: Dict[str, int] = {}
    for item in api_items:
        st = item.get("status", "")
        api_status[st] = api_status.get(st, 0) + 1

    all_st = sorted(api_status.keys())
    if all_st:
        st_rows = []
        for st in all_st:
            st_rows.append((f"status={st}", api_status.get(st, 0), "N/A (已展示)", "_"))
        _print_compare_table("返回数据中 status 分布", st_rows)

    # issue_type 分布
    api_itype: Dict[str, int] = {}
    for item in api_items:
        it = item.get("issue_type", "")
        api_itype[it] = api_itype.get(it, 0) + 1

    all_it = sorted(api_itype.keys())
    if all_it:
        it_rows = []
        for it in all_it[:10]:
            it_rows.append((f"issue_type={it}", api_itype.get(it, 0), "N/A (已展示)", "_"))
        if len(all_it) > 10:
            it_rows.append(("... 更多类型", f"共{len(all_it)}种", "", "_"))
        _print_compare_table("返回数据中 issue_type 分布", it_rows)


def verify_reports(api: ApiClient, conn: pymysql.Connection):
    """核验报告列表接口 GET /api/reports"""
    _print_section_header("报告列表: GET /api/reports?page=1&page_size=20")

    db_total = _db_scalar(conn, "SELECT COUNT(*) FROM review_report")

    resp = api.get(
        "/reports",
        params={"page": 1, "page_size": REPORT_PAGE_SIZE},
    )
    page_data = resp.get("data", {})
    api_total = page_data.get("total", 0)
    api_items = page_data.get("items", [])

    rows = [
        ("报告总数", api_total, db_total, _compare_value(api_total, db_total)),
        ("返回条数", len(api_items), min(db_total, REPORT_PAGE_SIZE),
         _compare_value(len(api_items), min(db_total, REPORT_PAGE_SIZE))),
    ]
    _print_compare_table("报告数量", rows)


def verify_agents_runtime(api: ApiClient, conn: pymysql.Connection):
    """核验 Agent 运行时接口 GET /api/agents/runtime"""
    _print_section_header("Agent 运行时: GET /api/agents/runtime (预期 12)")

    # --- 数据库基准 ---
    db_agent_categories = _db_query(
        conn,
        "SELECT COUNT(*) AS cnt FROM ("
        "  SELECT DISTINCT TRIM(SUBSTRING_INDEX(SUBSTRING_INDEX(model_name, '/', -1), '-', 1)) "
        "  FROM ai_call_log WHERE model_name IS NOT NULL AND model_name != ''"
        ") AS t",
    )
    _ = db_agent_categories  # 仅为参考

    # --- API 数据 ---
    resp = api.get("/agents/runtime")
    api_agents = resp.get("data", [])
    api_count = len(api_agents) if isinstance(api_agents, list) else 0

    expected = 12
    rows = [
        ("Agent 总数", api_count, expected, _compare_value(api_count, expected)),
    ]
    _print_compare_table("Agent 数量", rows)

    if isinstance(api_agents, list) and api_agents:
        print(f"  [Agent 列表] 共 {api_count} 个 Agent:")
        status_count: Dict[str, int] = {}
        for agent in api_agents:
            code = agent.get("code", "?")
            ag_status = agent.get("status", "?")
            name = agent.get("name", "?")
            print(f"    - {code:<25} | {name:<20} | status={ag_status}")
            status_count[ag_status] = status_count.get(ag_status, 0) + 1
        print(f"  status 分布: {status_count}")
        print()


def verify_agents_situation(api: ApiClient, conn: pymysql.Connection):
    """核验 Agent 态势接口 GET /api/agents/situation"""
    _print_section_header("Agent 态势: GET /api/agents/situation (online 预期 12)")

    resp = api.get("/agents/situation", params={"minutes": 60})
    situation = resp.get("data", {})

    online = situation.get("online", 0)
    working = situation.get("working", 0)
    idle_val = situation.get("idle", 0)
    today_calls = situation.get("today_calls", 0)

    expected = 12
    rows = [
        ("online", online, expected, _compare_value(online, expected)),
        ("working", working, 0, _compare_value(working, 0)),
        ("idle", idle_val, expected, _compare_value(idle_val, expected)),
    ]
    _print_compare_table("态势核心数据", rows)

    print(f"  today_calls = {today_calls}")
    hotspots = situation.get("hotspots", [])
    if hotspots:
        print(f"  hotspots = {json.dumps(hotspots[:5], ensure_ascii=False)}")
    print()


# ======================== 主流程 ========================


def main():
    """
    核验脚本主入口:
    1. 连接 MySQL 并获取所有核心表数据作为基准
    2. 通过 HTTP API 获取对应的接口返回值
    3. 逐字段对比并输出差异表格
    """
    print("=" * 80)
    print("  前后端数据一致性核验脚本")
    print(f"  DB: mysql://{DB_USER}@{DB_HOST}:{DB_PORT}/{DB_NAME}")
    print(f"  API: {API_BASE}")
    print("=" * 80)

    if not DB_PASSWORD or not LOGIN_PASSWORD:
        print("[fatal] 必须设置 AUDIT_DB_PASSWORD 和 ADMIN_PASSWORD")
        sys.exit(2)

    conn: Optional[pymysql.Connection] = None
    api = ApiClient(API_BASE)
    total_fail = 0

    try:
        conn = _get_db_connection()
        print("数据库连接成功\n")
    except pymysql.MySQLError as e:
        print(f"\n[致命错误] 数据库连接失败: {e}")
        sys.exit(1)

    try:
        _ensure_token(api)
    except Exception as e:
        print(f"\n[致命错误] 登录失败: {e}")
        conn.close()
        sys.exit(1)

    # ---- 依次执行各项核验 ----
    verifications = [
        ("仪表盘汇总", verify_dashboard_summary),
        ("仪表盘风险分布", verify_dashboard_risk),
        ("项目列表", verify_projects_list),
        ("项目详情", verify_project_detail),
        ("代码文件", verify_code_files),
        ("审查任务列表", verify_review_list),
        ("审查任务详情", verify_review_detail),
        ("问题列表", verify_issues),
        ("报告列表", verify_reports),
        ("Agent 运行时", verify_agents_runtime),
        ("Agent 态势", verify_agents_situation),
    ]

    for name, verify_fn in verifications:
        try:
            verify_fn(api, conn)
        except Exception as e:
            print(f"\n  [错误] 核验 [{name}] 时发生异常: {type(e).__name__}: {e}")
            import traceback
            traceback.print_exc()
            total_fail += 1

    # ---- 汇总 ----
    print("\n" + "=" * 80)
    print("  核验完成")
    print("=" * 80)
    if total_fail > 0:
        print(f"  有 {total_fail} 项核验发生异常(未完成), 请检查上方错误信息")
    else:
        print("  所有核验项均已执行, 请检查上方各表格中的 ❌ 标记定位不一致数据")

    api.close()
    if conn:
        conn.close()


if __name__ == "__main__":
    main()
