"""独立复算已脱敏证据，并用合成响应验证项目161比较器；不连接生产。"""
import collections
import copy
import hashlib
import importlib.util
import json
import pathlib
import types
import xml.etree.ElementTree as ET

ROOT = pathlib.Path(__file__).resolve().parent


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_module(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main():
    result = {"生产数据库连接次数": 0, "生产业务写入次数": 0, "检查": {}}
    checks = result["检查"]
    project_path = ROOT / "项目161完整字段比较.json"
    project = json.loads(project_path.read_text())
    collector_path = ROOT / "compare_project161_backup_readonly.py"
    assert project["collector_sha256"] == sha(collector_path)
    assert project["backup"]["bytes"] == 402726075
    assert project["backup"]["sha256"] == "7bb99942cc0e571008743fc66b0d1cdd88fcf13640579b32fb493c0679d7dc6f"
    assert project["backup"]["expected_sha_matches"] and project["backup"]["stable_during_read"]
    fields = [item["field"] for item in project["field_comparison"]]
    assert fields == ["id", "user_id", "project_name", "description", "language", "status", "create_time", "update_time"]
    assert len(set(fields)) == 8
    assert all(row["equal"] and row["backup_normalized_sha256"] == row["current_normalized_sha256"]
               for row in project["field_comparison"])
    fingerprints = project["row_fingerprint"]
    assert fingerprints["equal"] and fingerprints["backup_normalized_sha256"] == fingerprints["current_normalized_sha256"]
    assert project["current"]["read_only"] == 1 and project["current"]["target_rows"] == 1
    assert project["parser"]["target_rows"] == 1 and project["parser"]["project_rows"] == 100
    checks["项目161摘要与八字段重数"] = "通过"

    module = load_module(collector_path, "project_compare_review")
    module.selftest()
    raw = [161, 1, "合成验收", None, "python", "active", "2026-01-01 01:02:03", "2026-01-02 01:02:03"]
    before = module.normalize(raw)
    columns = project["parser"]["complete_columns"]
    schema = [dict(kind="schema", name=c["name"], type=c["type"], nullable=c["nullable"],
                   charset="utf8mb4" if c["type"].startswith("varchar") else None,
                   key="PRI" if c["name"] == "id" else "", precision=0 if c["type"] == "datetime" else None)
              for c in columns]
    counts = {"kind": "counts", "project_rows": 101, "target_rows": 1, "read_only": 1}
    target = {"kind": "target", **dict(zip(fields, raw))}
    observations = []

    def run_case(records):
        def fake_run(_command, **kwargs):
            sql = kwargs["input"]
            statements = [part.strip() for part in sql.split(";") if part.strip()]
            assert all(s.startswith(("SELECT ", "SET SESSION ", "START TRANSACTION ", "ROLLBACK")) for s in statements)
            assert "DATE_FORMAT(`create_time`, '%Y-%m-%d %H:%i:%s')" in sql
            assert "DATE_FORMAT(`update_time`, '%Y-%m-%d %H:%i:%s')" in sql
            observations.append(len(statements))
            return types.SimpleNamespace(returncode=0, stdout="\n".join(json.dumps(row) for row in records))
        original_run = module.subprocess.run
        module.subprocess.run = fake_run
        output = {}
        try:
            module.current_project(output, before, columns)
        finally:
            module.subprocess.run = original_run
        return output

    assert run_case(schema + [counts, target])["all_eight_fields_equal"]
    bad_schema = copy.deepcopy(schema)
    bad_schema[-1]["precision"] = 6
    try:
        run_case(bad_schema + [counts, target])
        raise AssertionError("精度丢失没有阻断")
    except module.Blocked as exc:
        assert str(exc) == "current_datetime_precision"
    changed_target = dict(target, description="NULL")
    changed = run_case(schema + [counts, changed_target])
    assert not changed["all_eight_fields_equal"]
    assert [row["field"] for row in changed["field_comparison"] if not row["equal"]] == ["description"]
    original = raw.copy()
    original[-1] += ".000000"
    try:
        module.normalize(original)
        raise AssertionError("原始JSON的小数秒表现未复现严格门控")
    except module.Blocked as exc:
        assert str(exc) == "datetime_format_or_precision"
    checks["日期规范化与精度门控合成反例"] = {"状态": "通过", "比较器调用数": len(observations)}

    initial = json.loads((ROOT / "历史任务与孤儿账本.json").read_text())
    final = json.loads((ROOT / "历史任务与孤儿账本复核.json").read_text())
    for name, data in (("collect_historical_ledger_initial.py", initial), ("collect_historical_ledger.py", final)):
        path = ROOT / name
        assert data["collector_sha256"] == sha(path)
        sql = load_module(path, "ledger_" + name).SQL
        assert data["query_sha256"] == hashlib.sha256(sql.encode()).hexdigest()
        statements = [part.strip() for part in sql.split(";") if part.strip()]
        assert all(s.startswith(("SELECT ", "SET SESSION ", "START TRANSACTION ", "ROLLBACK")) for s in statements)
        assert data["business_writes"] is False
        assert data["transaction"][0]["read_only"] == data["snapshot_end"][0]["read_only"] == 1
    for key in ("status_counts", "relationship", "tool_integrity", "owner_mismatch", "task_member_team_mismatch",
                "historical_team_tasks", "historical_event", "orphan_tool", "unresolved_tool"):
        assert initial[key] == final[key], key
    initial_tasks = {row["task_id"]: row for row in initial["historical_task"]}
    final_tasks = {row["task_id"]: row for row in final["historical_task"]}
    assert set(final_tasks) == {11, 35}
    assert all(initial_tasks[key] == value for key, value in final_tasks.items())
    assert {(row["task_id"], row["team_id"], row["task_status"], row["team_status"])
            for row in final_tasks.values()} == {(11, 5, "queued", "failed"), (35, 12, "waiting_dependency", "failed")}
    terminal = {"completed", "failed", "blocked", "cancelled", "dead_letter", "expired"}
    recalculated_anomalies = [row["task_id"] for row in initial_tasks.values() if row["task_status"] not in terminal]
    assert recalculated_anomalies == [11, 35]
    totals = collections.Counter()
    for row in final["status_counts"]:
        totals[row["table"]] += row["count"]
    assert totals["agent_tool_execution"] == final["tool_integrity"][0]["total"] == 2576
    missing = {row["name"]: row["missing"] for row in final["relationship"]}
    orphan_ids = [row["id"] for row in final["orphan_tool"]]
    assert len(orphan_ids) == len(set(orphan_ids)) == missing["tool_run"] == 7
    assert orphan_ids == [125, 126, 127, 128, 129, 2580, 2581]
    assert all(row["status"] == row["result_status"] == "success" and row["request_binding_matches"]
               and row["arguments_valid"] and row["result_valid"] for row in final["orphan_tool"])
    assert [row["id"] for row in final["unresolved_tool"]] == [472, 634, 2286]
    assert all(row["status"] == "executing" and row["run_status"] == "failed" and row["result_sha256"] is None
               for row in final["unresolved_tool"])
    events = final["historical_event"]
    assert len(events) == len({event["id"] for event in events}) == 23
    checks["两次历史快照内容与采集SQL复算"] = {"状态": "通过", "表总数": dict(totals),
        "真实非终态子任务": recalculated_anomalies, "工具孤儿": orphan_ids, "未决工具": [472, 634, 2286],
        "相同事件数": len(events), "初次宽筛选结果数": len(initial_tasks), "修正筛选结果数": len(final_tasks)}
    approvals = json.loads((ROOT / "孤儿能力审批补证.json").read_text())["rows"]
    approval_rows = [row for row in approvals if row["kind"] == "approval"]
    assert {(row["tool_id"], row["status"], row["risk_level"]) for row in approval_rows} == {
        (2580, "approved", "critical"), (2581, "approved", "critical")}
    outcome_rows = json.loads((ROOT / "孤儿能力实际结果关联.json").read_text())["rows"]
    by_tool = collections.defaultdict(list)
    for row in outcome_rows:
        assert row["existing_id"] == row["returned_item_id"] and row["created_by_matches"]
        by_tool[row["tool_id"]].append(row)
    assert set(by_tool) == {2580, 2581}
    assert [row["existing_id"] for row in by_tool[2580]] == [23, 24, 25]
    assert [row["existing_id"] for row in by_tool[2581]] == [26, 27]
    assert all(len(rows) == rows[0]["result_reported_count"] for rows in by_tool.values())
    assert len({row["existing_id"] for row in outcome_rows}) == 5
    checks["两个管理动作审批补证交叉核对"] = {
        "状态": "通过", "审批": "2580/2581均critical approved",
        "返回对象实际存在且创建者匹配": {str(key): len(rows) for key, rows in by_tool.items()},
        "边界": "重新计数并核对已采脱敏关联；不凭记录重建缺失根运行，不将active字段等同于未过期有效。"}
    for name in ("权限矩阵-最终.xml", "路由与报告按钮矩阵-最终.xml"):
        cases = list(ET.parse(ROOT / name).iter("testcase"))
        assert not any(case.find("failure") is not None or case.find("error") is not None or case.find("skipped") is not None
                       for case in cases)
        checks[name] = {"通过": len(cases), "失败": 0, "跳过": 0}
    actions = json.loads((ROOT / "全部按钮与交互源码清单.json").read_text())
    assert len(actions["actions"]) == actions["action_count"]
    assert sum(item["button"] for item in actions["actions"]) == actions["button_count"]
    checks["交互源码清单重数"] = {"交互节点": actions["action_count"], "按钮或链接": actions["button_count"],
                              "生产点击证据": "此清单未记录；不能转为通过"}
    result["证据边界"] = ["原始161字段未导出，本复核重数并核对已保存摘要及采集器，未重新读取备份或计算原值哈希。",
                         "只读SQL、事务标记和两次相同历史快照证明这两个采集器没有业务UPDATE；不代表全生产历史从未被任何操作修改。"]
    result["输入摘要"] = {name: sha(ROOT / name) for name in (
        "项目161完整字段比较.json", "compare_project161_backup_readonly.py", "历史任务与孤儿账本.json",
        "历史任务与孤儿账本复核.json", "collect_historical_ledger_initial.py", "collect_historical_ledger.py",
        "孤儿能力审批补证.json", "孤儿能力实际结果关联.json", "权限矩阵-最终.xml",
        "路由与报告按钮矩阵-最终.xml", "全部按钮与交互源码清单.json")}
    (ROOT / "独立复算结果.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"独立检查组": len(checks), "状态": "通过"}, ensure_ascii=False))


if __name__ == "__main__":
    main()
