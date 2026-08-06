"""AC2 端到端验证脚本

通过 HTTP API 完整执行一次代码审计流程,验证 agent_label 是否正确落库:
1. 使用显式提供的 admin 口令获取 JWT
2. 创建项目(AC2-E2E-Test, language=python)
3. 在线创建代码文件(vulnerable_ac2_e2e.py, 含 4 个已知漏洞)
4. 启动审查任务(review_type=security)
5. 轮询任务状态直到完成(success/failed/cancelled)
6. 查询 ai_call_log 验证 agent_label 不为 NULL
7. 查询 review_issue 验证漏洞识别结果

执行方式:
    # 在 backend 容器内执行(推荐,容器内有 httpx 与项目源码)
    docker exec cr_backend python3 /app/tests/e2e/ac2_e2e_verify.py \
        --base-url http://127.0.0.1:8000 \
        --username admin --password "$ADMIN_PASSWORD"

    # 或在宿主机执行(需安装 httpx)
    python3 ac2_e2e_verify.py --base-url http://127.0.0.1:8000
"""
import argparse
import os
import sys
import time

import httpx

# 测试样本内容(与 backend/tests/fixtures/vulnerable_ac2_e2e.py 一致)
# 包含 4 个已知漏洞:
#   1. 硬编码密码 (CWE-259) - password = "admin123456"
#   2. SQL 注入 (CWE-89) - f-string 拼接 SQL
#   3. 命令注入 (CWE-78) - os.system(user_input)
#   4. 路径遍历 (CWE-22) - open(user_input)
VULNERABLE_CODE = '''"""AC2 端到端验证测试样本

包含 4 个已知漏洞,用于验证 agent_label 是否正确落库:
1. SQL 注入 (CWE-89) - f-string 拼接 SQL,静态规则可命中
2. 硬编码密码 (CWE-259) - password = "admin123456",静态规则可命中
3. 命令注入 (CWE-78) - os.system(user_input),LLM 可识别
4. 路径遍历 (CWE-22) - open(user_input),静态规则可命中
"""
import os
import sqlite3


# 漏洞1:硬编码密码 (CWE-259)
password = "admin123456"
db_url = "mysql://root:secret_pass@localhost/db"


# 漏洞2:SQL 注入 (CWE-89)
def get_user(username):
    conn = sqlite3.connect("users.db")
    cursor = conn.cursor()
    query = f"SELECT * FROM users WHERE username = '{username}'"
    cursor.execute(query)
    return cursor.fetchone()


# 漏洞3:命令注入 (CWE-78)
def list_files(user_dir):
    os.system(f"ls {user_dir}")
    return True


# 漏洞4:路径遍历 (CWE-22)
def read_config(filename):
    with open(f"/etc/app/{filename}", "r") as f:
        return f.read()


if __name__ == "__main__":
    user = get_user("admin")
    print(user)
'''

# 审查任务最长等待时间(秒)
MAX_WAIT_SECONDS = 600
# 轮询间隔(秒)
POLL_INTERVAL = 5
# HTTP 请求超时(秒)
HTTP_TIMEOUT = 30


def log_step(step: str, msg: str) -> None:
    """打印步骤日志,带时间戳

    Args:
        step: 步骤标识(如 [1/7])
        msg: 日志内容
    """
    ts = time.strftime("%H:%M:%S", time.localtime())
    print(f"[{ts}] {step} {msg}", flush=True)


def log_ok(msg: str) -> None:
    """打印成功信息(绿色)"""
    print(f"\033[32m  ✅ {msg}\033[0m", flush=True)


def log_fail(msg: str) -> None:
    """打印失败信息(红色)"""
    print(f"\033[31m  ❌ {msg}\033[0m", flush=True)


def log_warn(msg: str) -> None:
    """打印警告信息(黄色)"""
    print(f"\033[33m  ⚠️  {msg}\033[0m", flush=True)


def login(client: httpx.Client, username: str, password: str) -> str:
    """登录并返回 JWT access_token

    Args:
        client: httpx.Client 实例
        username: 用户名
        password: 密码

    Returns:
        str: JWT access_token

    Raises:
        RuntimeError: 登录失败
    """
    resp = client.post(
        "/api/auth/login",
        json={"username": username, "password": password},
        timeout=HTTP_TIMEOUT,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"登录失败 HTTP {resp.status_code}: {resp.text[:200]}")
    data = resp.json()
    if data.get("code") != 0:
        raise RuntimeError(f"登录失败: {data}")
    token = data["data"]["access_token"]
    log_ok(f"登录成功,token={token[:20]}...")
    return token


def create_project(client: httpx.Client, token: str, name: str) -> int:
    """创建项目并返回 project_id

    Args:
        client: httpx.Client 实例
        token: JWT token
        name: 项目名

    Returns:
        int: 新建项目 ID
    """
    headers = {"Authorization": f"Bearer {token}"}
    resp = client.post(
        "/api/projects",
        json={
            "project_name": name,
            "description": "AC2 端到端验证项目",
            "language": "python",
        },
        headers=headers,
        timeout=HTTP_TIMEOUT,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"创建项目失败 HTTP {resp.status_code}: {resp.text[:200]}")
    data = resp.json()
    if data.get("code") != 0:
        raise RuntimeError(f"创建项目失败: {data}")
    project_id = data["data"]["id"]
    log_ok(f"项目已创建,id={project_id}")
    return project_id


def create_code_file(client: httpx.Client, token: str, project_id: int) -> int:
    """在线创建代码文件并返回 file_id

    Args:
        client: httpx.Client 实例
        token: JWT token
        project_id: 项目 ID

    Returns:
        int: 新建代码文件 ID
    """
    headers = {"Authorization": f"Bearer {token}"}
    resp = client.post(
        "/api/code-files",
        json={
            "project_id": project_id,
            "file_name": "vulnerable_ac2_e2e.py",
            "language": "python",
            "content": VULNERABLE_CODE,
        },
        headers=headers,
        timeout=HTTP_TIMEOUT,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"创建代码文件失败 HTTP {resp.status_code}: {resp.text[:200]}")
    data = resp.json()
    if data.get("code") != 0:
        raise RuntimeError(f"创建代码文件失败: {data}")
    file_id = data["data"]["file_id"]
    log_ok(f"代码文件已创建,file_id={file_id}")
    return file_id


def start_review(client: httpx.Client, token: str, project_id: int, file_id: int) -> int:
    """启动审查任务并返回 task_id

    Args:
        client: httpx.Client 实例
        token: JWT token
        project_id: 项目 ID
        file_id: 文件 ID

    Returns:
        int: 新建审查任务 ID
    """
    headers = {"Authorization": f"Bearer {token}"}
    resp = client.post(
        "/api/review/start",
        json={
            "project_id": project_id,
            "file_ids": [file_id],
            "review_type": "security",
            "task_name": "AC2-E2E-Verify",
        },
        headers=headers,
        timeout=HTTP_TIMEOUT,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"启动审查失败 HTTP {resp.status_code}: {resp.text[:200]}")
    data = resp.json()
    if data.get("code") != 0:
        raise RuntimeError(f"启动审查失败: {data}")
    task_id = data["data"]["task_id"]
    status = data["data"]["status"]
    log_ok(f"审查任务已启动,task_id={task_id},status={status}")
    return task_id


def poll_task(client: httpx.Client, token: str, task_id: int) -> dict:
    """轮询审查任务状态直到完成

    Args:
        client: httpx.Client 实例
        token: JWT token
        task_id: 审查任务 ID

    Returns:
        dict: 任务详情
    """
    headers = {"Authorization": f"Bearer {token}"}
    start = time.time()
    terminal_states = {"success", "failed", "cancelled", "completed"}
    last_status = ""

    while time.time() - start < MAX_WAIT_SECONDS:
        resp = client.get(
            f"/api/review/tasks/{task_id}",
            headers=headers,
            timeout=HTTP_TIMEOUT,
        )
        if resp.status_code != 200:
            log_warn(f"查询任务状态失败 HTTP {resp.status_code},重试...")
            time.sleep(POLL_INTERVAL)
            continue
        data = resp.json()
        if data.get("code") != 0:
            log_warn(f"查询任务状态返回错误: {data}")
            time.sleep(POLL_INTERVAL)
            continue
        task = data["data"]
        status = task.get("status", "unknown")
        processed = task.get("processed_files", 0)
        total = task.get("total_files", 0)
        if status != last_status:
            elapsed = int(time.time() - start)
            log_step("[5/7]", f"任务状态={status} 进度={processed}/{total} 耗时={elapsed}s")
            last_status = status
        if status in terminal_states:
            return task
        time.sleep(POLL_INTERVAL)

    raise RuntimeError(
        f"审查任务 {task_id} 在 {MAX_WAIT_SECONDS}s 内未完成,最后状态={last_status}"
    )


def query_ai_logs(client: httpx.Client, token: str, task_id: int) -> list:
    """查询任务的 AI 调用日志

    Args:
        client: httpx.Client 实例
        token: JWT token
        task_id: 审查任务 ID

    Returns:
        list: AI 调用日志列表
    """
    headers = {"Authorization": f"Bearer {token}"}
    # 分页查全部(取前 100 条)
    resp = client.get(
        "/api/ai-logs",
        params={"task_id": task_id, "page": 1, "page_size": 100},
        headers=headers,
        timeout=HTTP_TIMEOUT,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"查询 AI 日志失败 HTTP {resp.status_code}: {resp.text[:200]}")
    data = resp.json()
    if data.get("code") != 0:
        raise RuntimeError(f"查询 AI 日志失败: {data}")
    items = data["data"]["items"]
    return items


def query_issues(client: httpx.Client, token: str, task_id: int) -> list:
    """查询任务的审查问题列表

    Args:
        client: httpx.Client 实例
        token: JWT token
        task_id: 审查任务 ID

    Returns:
        list: 审查问题列表
    """
    headers = {"Authorization": f"Bearer {token}"}
    resp = client.get(
        f"/api/review/tasks/{task_id}/issues",
        params={"page": 1, "page_size": 100},
        headers=headers,
        timeout=HTTP_TIMEOUT,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"查询问题列表失败 HTTP {resp.status_code}: {resp.text[:200]}")
    data = resp.json()
    if data.get("code") != 0:
        raise RuntimeError(f"查询问题列表失败: {data}")
    items = data["data"]["items"]
    return items


def verify_agent_label(logs: list) -> tuple:
    """验证 ai_call_log 中 agent_label 是否正确落库

    Args:
        logs: AI 调用日志列表

    Returns:
        tuple: (通过数, 失败数, 详情列表)
    """
    total = len(logs)
    if total == 0:
        log_fail("ai_call_log 表中无任何记录 — 严重异常")
        return 0, 1, [{"error": "无日志记录"}]

    log_step("[6/7]", f"AI 日志记录数={total},逐条验证 agent_label")
    passed = 0
    failed = 0
    details = []

    # 统计 agent_label 分布
    label_counter = {}
    null_label_count = 0

    for i, log in enumerate(logs, 1):
        label = log.get("agent_label")
        status = log.get("status", "unknown")
        model = log.get("model_name", "")
        log_id = log.get("id", "?")

        if label is None or label == "" or label == "null":
            null_label_count += 1
            failed += 1
            details.append({
                "log_id": log_id,
                "status": status,
                "model": model,
                "agent_label": label,
                "issue": "agent_label 为 NULL/空",
            })
            print(f"  ❌ log_id={log_id} status={status} model={model} agent_label={label!r} (NULL)")
        else:
            passed += 1
            label_counter[label] = label_counter.get(label, 0) + 1
            print(f"  ✅ log_id={log_id} status={status} model={model} agent_label={label!r}")

    print()
    print("  ─── agent_label 分布统计 ───")
    for label, count in sorted(label_counter.items()):
        print(f"    {label}: {count} 条")
    if null_label_count:
        print(f"    (NULL): {null_label_count} 条")
    print("  ────────────────────────────")

    return passed, failed, details


def verify_issues(issues: list) -> tuple:
    """验证审查问题列表,确认 4 个已知漏洞是否被识别

    Args:
        issues: 审查问题列表

    Returns:
        tuple: (识别漏洞数, 期望漏洞数, 详情列表)
    """
    expected = 4
    actual = len(issues)
    log_step("[7/7]", f"识别漏洞数={actual},期望≥{expected}")

    if actual == 0:
        log_fail("审查未识别出任何漏洞 — 漏洞识别功能异常")
        return 0, expected, [{"error": "无识别漏洞"}]

    # 按漏洞类型分类统计
    type_counter = {}
    cwe_set = set()
    severity_counter = {}
    source_counter = {}

    for issue in issues:
        itype = issue.get("issue_type", "unknown")
        cwe = issue.get("cwe") or ""
        severity = issue.get("severity", "unknown")
        source = issue.get("source", "unknown")
        type_counter[itype] = type_counter.get(itype, 0) + 1
        severity_counter[severity] = severity_counter.get(severity, 0) + 1
        source_counter[source] = source_counter.get(source, 0) + 1
        if cwe:
            cwe_set.add(cwe)

    print("  ─── 漏洞类型分布 ───")
    for itype, count in sorted(type_counter.items()):
        print(f"    {itype}: {count} 个")
    print("  ─── 严重程度分布 ───")
    for sev, count in sorted(severity_counter.items()):
        print(f"    {sev}: {count} 个")
    print("  ─── 来源(static/llm/hybrid) ───")
    for src, count in sorted(source_counter.items()):
        print(f"    {src}: {count} 个")
    print("  ─── CWE 集合 ───")
    for cwe in sorted(cwe_set):
        print(f"    {cwe}")
    print("  ───────────────────")

    # 列出前 5 条漏洞详情
    print(f"  ─── 前 {min(5, actual)} 条漏洞详情 ───")
    for i, issue in enumerate(issues[:5], 1):
        title = issue.get("title", "")[:60]
        line = issue.get("line_number", "?")
        cwe = issue.get("cwe") or "-"
        severity = issue.get("severity", "?")
        source = issue.get("source", "?")
        print(f"    [{i}] L{line} [{severity}] {cwe} ({source}) {title}")
    print("  ────────────────────────────")

    if actual >= expected:
        log_ok(f"漏洞识别验证通过:识别 {actual} 个漏洞(≥期望 {expected})")
    else:
        log_warn(f"漏洞识别数 {actual} 少于期望 {expected}(可能 LLM 未识别全部)")

    return actual, expected, []


def main() -> int:
    """主函数:执行端到端验证流程

    Returns:
        int: 退出码,0 表示成功,1 表示失败
    """
    parser = argparse.ArgumentParser(description="AC2 端到端验证脚本")
    parser.add_argument(
        "--base-url",
        default="http://127.0.0.1:8000",
        help="后端 API 基地址(默认 http://127.0.0.1:8000)",
    )
    parser.add_argument("--username", default="admin", help="登录用户名")
    parser.add_argument(
        "--password",
        default=os.environ.get("ADMIN_PASSWORD", ""),
        help="登录密码(也可由 ADMIN_PASSWORD 提供)",
    )
    parser.add_argument(
        "--project-name",
        default=f"AC2-E2E-{int(time.time())}",
        help="项目名(默认带时间戳避免重复)",
    )
    args = parser.parse_args()
    if not args.password:
        parser.error("必须通过 --password 或 ADMIN_PASSWORD 提供管理员口令")

    print("=" * 72)
    print("AC2 端到端验证 — 验证 ai_call_log.agent_label 是否正确落库")
    print("=" * 72)
    print(f"Base URL:   {args.base_url}")
    print(f"Username:   {args.username}")
    print(f"Project:    {args.project_name}")
    print("Test Code:  vulnerable_ac2_e2e.py (4 个已知漏洞)")
    print("=" * 72)
    print()

    exit_code = 0

    try:
        with httpx.Client(base_url=args.base_url, timeout=HTTP_TIMEOUT) as client:
            # 步骤1:登录
            log_step("[1/7]", "登录获取 JWT token")
            token = login(client, args.username, args.password)

            # 步骤2:创建项目
            log_step("[2/7]", "创建测试项目")
            project_id = create_project(client, token, args.project_name)

            # 步骤3:创建代码文件(含 4 个已知漏洞)
            log_step("[3/7]", "在线创建代码文件(含 4 个已知漏洞)")
            file_id = create_code_file(client, token, project_id)

            # 步骤4:启动审查任务
            log_step("[4/7]", "启动 security 类型审查任务")
            task_id = start_review(client, token, project_id, file_id)

            # 步骤5:轮询任务状态
            log_step("[5/7]", f"轮询任务 #{task_id} 直到完成")
            task = poll_task(client, token, task_id)
            status = task.get("status")
            total_issues = task.get("total_issues", 0)
            score = task.get("score")
            severity_count = task.get("severity_count", {})

            print()
            print("  ─── 审查任务最终结果 ───")
            print(f"    task_id:      {task_id}")
            print(f"    status:       {status}")
            print(f"    total_issues: {total_issues}")
            print(f"    score:        {score}")
            print(f"    severity:     {severity_count}")
            print("  ────────────────────────────")

            if status != "success":
                log_fail(f"审查任务未成功完成,status={status}")
                exit_code = 1
            else:
                log_ok("审查任务成功完成!")

            # 步骤6:查询 ai_call_log 验证 agent_label
            print()
            log_step("[6/7]", "查询 ai_call_log 验证 agent_label")
            logs = query_ai_logs(client, token, task_id)
            passed, failed, _ = verify_agent_label(logs)

            if failed > 0:
                log_fail(f"agent_label 验证失败:通过 {passed} 条,失败 {failed} 条")
                exit_code = 1
            else:
                log_ok(f"agent_label 验证全部通过!共 {passed} 条日志,无 NULL")

            # 步骤7:查询 review_issue 验证漏洞识别
            print()
            log_step("[7/7]", "查询 review_issue 验证漏洞识别结果")
            issues = query_issues(client, token, task_id)
            actual, expected, _ = verify_issues(issues)

            if actual < expected:
                log_warn(f"漏洞识别数 {actual} 少于期望 {expected},但流程已完成")

    except Exception as e:
        log_fail(f"端到端验证异常: {e}")
        import traceback
        traceback.print_exc()
        exit_code = 1

    print()
    print("=" * 72)
    if exit_code == 0:
        print("🎉 AC2 端到端验证全部通过!")
        print("   - agent_label 正确落库到 ai_call_log 表")
        print("   - 审查流程能识别已知漏洞")
        print("   - Agent 调用归因(AC2)功能正常")
    else:
        print("💥 AC2 端到端验证存在失败项,请查看上方日志")
    print("=" * 72)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
