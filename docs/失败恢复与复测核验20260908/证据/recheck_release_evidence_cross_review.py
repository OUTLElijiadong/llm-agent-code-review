"""第二位审阅人直接重读归档证据；不调用原审阅器、SSH、HTTP或数据库。"""
from collections import Counter
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
HERE = Path(__file__).resolve().parent
source_sha = {}


def sha(value):
    return hashlib.sha256(value).hexdigest()


def read(name):
    value = (HERE / name).read_bytes()
    source_sha[name] = sha(value)
    return json.loads(value)


review = read("3.8.7发布后数据与备份独立复核.json")
before = read("发布前生产版本与外键核验.json")
after = read("发布后生产版本与外键核验.json")
assets = read("发布后前端资源与备份核验.json")
history_before = read("发布前历史账本只读.json")
history_after = read("发布后历史账本只读.json")
project_before = read("发布前项目161八字段核验.json")
project_after = read("发布后项目161八字段核验.json")
task_before = read("发布前审查任务161报告核验.json")
task_after = read("发布后审查任务161报告核验.json")
env = read("生产默认发布环境校准.json")
ops = read("发布后生产运维门禁.json")
checks = {}
checks["原独审35结论与22个来源指纹"] = len(review["检查"]) == 35 and all(review["检查"].values()) and all(
    sha((ROOT / name).read_bytes()) == digest for name, digest in review["来源指纹"].items()
)
checks["版本与048只读迁移"] = after["expected_release"] == assets["release"] == review["发布提交"] == "4035d1902342ab88357b5bb6071545d1a0572dd1" and after["version"] == assets["version"] == "3.8.7" and before["alembic_revision"] == after["alembic_revision"] == "048_ai_usage_attribution" and after["business_writes"] is False and after["readonly_transaction"]["read_only"] == 1
fks = after["foreign_keys"]
targets = {"root_agent_run_id": "agent_response_run", "agent_run_id": "agent_response_run", "tool_execution_id": "agent_tool_execution", "agent_team_id": "agent_team", "agent_team_task_id": "agent_team_task", "agent_execution_event_id": "agent_team_event"}
tables = {"agent_response_run", "agent_team", "review_task", "ai_call_log", "agent_mesh_message", "pentest_engagement", "sandbox_environment"}
checks["42外键逐项关系前后保持"] = len(fks) == 42 and fks == before["foreign_keys"] and {(row["table"], row["column"]) for row in fks} == {(table, column) for table in tables for column in targets} and all(
    row["target"] == targets[row["column"]] and row["target_column"] == "id" and row["target_schema"] == "code_review" and row["nullable"] and row["delete_rule"] == "SET NULL" for row in fks
)
checks["五运行源码独立重算"] = len(after["running_source_sha256"]) == 5 and after["running_source_sha256"] == before["running_source_sha256"] and all(
    sha((ROOT / "backend" / name).read_bytes()) == digest for name, digest in after["running_source_sha256"].items()
)
stable = ["historical_task", "historical_team_tasks", "historical_event", "orphan_tool", "unresolved_tool", "relationship", "tool_integrity", "owner_mismatch", "task_member_team_mismatch"]
checks["历史九集合完整相等且七孤儿三执行保持"] = all(history_before[key] == history_after[key] for key in stable) and len(history_after["orphan_tool"]) == 7 and len(history_after["unresolved_tool"]) == 3 and [(row["task_id"], row["team_id"], row["task_status"], row["team_status"]) for row in history_after["historical_task"]] == [(11, 5, "queued", "failed"), (35, 12, "waiting_dependency", "failed")]
checks["项目161八字段及行hash相等"] = len(project_after["field_comparison"]) == 8 and project_after["field_comparison"] == project_before["field_comparison"] and all(row["equals_before_release"] for row in project_after["field_comparison"]) and project_after["row_sha256"] == project_before["row_sha256"] == review["项目行摘要"] and project_after["sql_write_executed"] is False
ignore = {"utc", "collected_at_utc"}
checks["任务161全部非时间字段相等和未分级语义"] = {k: v for k, v in task_before.items() if k not in ignore} == {k: v for k, v in task_after.items() if k not in ignore} and task_after["stored_total"] == 16 and task_after["summary"]["total"] == task_after["summary"]["unclassified"] == 4 and sum(task_after["summary"]["severity_counts"].values()) == 0 and task_after["business_writes"] is False
front = assets["frontend"]
expected, served = front["expected_sha256"], front["served_expected_sha256"]
manifest = sha(json.dumps(expected, sort_keys=True, separators=(",", ":")).encode())
checks["270双map及canonical摘要和index重算"] = len(expected) == len(served) == front["expected_count"] == after["frontend_files"]["expected_count"] == 270 and expected == served and manifest == front["manifest_sha256"] == after["frontend_files"]["manifest_sha256"] == review["前端"]["独立重算规范摘要"] and expected["./index.html"] == front["index_sha256"] == after["frontend_files"]["index_sha256"]
frontend_container = next(row for row in after["containers"] if row["name"] == "cr_frontend")
checks["资源容器与发布账本绑定"] = all(front[key] == frontend_container[key] for key in ["container_id", "image_id", "started_at"]) and front["image_tag"] == frontend_container["image"] == "prism-frontend:" + after["expected_release"] and assets["ledger_sha256"] == after["ledger_sha256"] and front["served_total_count"] == after["frontend_files"]["served_count"] == 3725
backup = assets["backup"]
meta = backup["metadata"]
sidecar_sha = sha((backup["sha256"] + "  " + backup["file"] + "\n").encode())
checks["本轮备份048八十九表与sidecar重算"] = backup["bytes"] == review["备份"]["bytes"] == 405224409 and backup["sha256"] == meta["sha256"] == review["备份"]["sha256"] and meta["table_count"] == 89 and meta["alembic_revision"] == after["alembic_revision"] and meta["git_sha"] == after["expected_release"] and backup["file"] == meta["file"] == review["备份"]["file"] and sidecar_sha == backup["checksum_sidecar_sha256"] and backup["actual_file_meta_and_sidecar_sha_equal"] is True and backup["release_ledger_backup_file_matches"] is True
log = (HERE / "生产正式发布.log").read_bytes()
checks["发布原日志保持与环境警告闭环"] = len(log) == 32334 and sha(log) == review["来源指纹"][str((HERE / "生产正式发布.log").relative_to(ROOT))] and b"ERROR Compose default" in log and env["status"] == "aligned" and env["release"] == after["expected_release"] and env["unrelated_bytes_preserved"] is True and ops["status"] == "ok" and ops["can_continue"] is True and ops["blocking_checks"] == []
result = {
    "审阅人": "permission_matrix", "状态": "通过" if all(checks.values()) else "打回", "检查": checks,
    "来源指纹": source_sha, "原来源指纹重算数": len(review["来源指纹"]),
    "外键逐表": dict(Counter(row["table"] for row in fks)), "前端数量": len(expected),
    "前端规范摘要": manifest, "备份校验旁文件摘要": sidecar_sha,
    "生产请求数": 0, "生产写入": False, "测试或迁移重跑": False,
    "证据边界": "重新读取原始归档JSON并独立计算相等关系和SHA；未执行原审阅脚本、生产采集器或读取SQL正文。备份gzip字节及3725全目录只验证已采集证据的一致性，不声称本地重读原始备份或全部公开文件。UI通过数不由这些证据推导。",
}
target = HERE / "3.8.7发布后数据二次交叉复核.json"
if target.exists():
    raise SystemExit("目标已存在，拒绝覆盖")
target.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
print(json.dumps({"状态": result["状态"], "检查数": len(checks), "失败": [key for key, ok in checks.items() if not ok]}, ensure_ascii=False))
raise SystemExit(0 if all(checks.values()) else 1)
