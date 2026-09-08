"""独立离线核验生产UI台账；不运行生成器，不连接生产，不增补观察。"""
from collections import Counter
import csv
import hashlib
import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
INPUTS = {}


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def load(name):
    raw = (HERE / name).read_bytes()
    INPUTS[name] = sha(raw)
    return json.loads(raw)


base = load("本轮全部控件逐角色验收台账.json")
data = load("生产3.8.7-全部控件逐角色验收台账.json")
summary = load("生产3.8.7-全部控件逐角色汇总.json")
inventory = load("全部按钮与交互源码清单-最终绑定.json")
supplement = load("生产浏览器精确动作补充.json")
roles = {"owner_a": 107, "member_a": 108, "owner_b": 109, "no_permission": 110, "administrator": None}
raw_groups = {role: load(f"生产浏览器{role}观察.json") for role in roles if role != "administrator"}
raw_observations = {(role, observation["id"]): observation for role, group in raw_groups.items() for observation in group["observations"]}
rows, old_rows = data["controls"], base["controls"]
checks = {}
checks["初始化未覆盖与新文件摘要"] = INPUTS["本轮全部控件逐角色验收台账.json"] == summary["initialization_sha256"] == "b0627b17ea3b6a7ba6960a4a9c8a8c36e013b35ddc9c6628ff150a5e3902cb01" and INPUTS["生产3.8.7-全部控件逐角色验收台账.json"] == summary["ledger_sha256"]
checks["616乘5唯一行及定位保持"] = len(rows) == len({row["row_id"] for row in rows}) == 3080 and [row["row_id"] for row in rows] == [row["row_id"] for row in old_rows] and all(
    len({row["control_id"] for row in rows if row["account_role"] == role}) == 616 for role in roles
)
checks["104份源码SHA及452按钮链接"] = len(inventory["sources"]) == inventory["vue_files"] == 104 and inventory["action_count"] == len(inventory["actions"]) == 616 and inventory["button_count"] == sum(row["is_button_or_link"] for row in rows if row["account_role"] == "owner_a") == 452 and data["application_sources"] == base["application_sources"] == inventory["sources"] and all(
    sha((ROOT / "frontend" / item["file"]).read_bytes()) == item["sha256"] for item in inventory["sources"]
)
checks["当前八份来源与原来源SHA一致"] = len(data["current_source_sha256"]) == 8 and data["source_sha256"] == base["source_sha256"] and all(
    sha((ROOT / name).read_bytes()) == digest for name, digest in data["current_source_sha256"].items()
)
checks["准确账号ID与四份原始版本"] = all(row["account_id"] == roles[row["account_role"]] for row in rows) and all(account["id"] == roles[account["role"]] for account in data["accounts"]) and all(
    group["account"] == role and group["account_id"] == roles[role] and group["version"] == "3.8.7" and group["release"] == "4035d1902342ab88357b5bb6071545d1a0572dd1" for role, group in raw_groups.items()
)
counts = Counter(row["current_release_status"] for row in rows)
expected_counts = {"untested": 2430, "blocked": 616, "passed": 14, "partially_tested": 9, "observed": 11}
per_role = {role: dict(Counter(row["current_release_status"] for row in rows if row["account_role"] == role)) for role in roles}
checks["状态计数及逐角色汇总准确"] = counts == expected_counts and dict(counts) == data["summary"]["current_release_statuses"] == summary["current_release_statuses"] and per_role == summary["per_role"] == data["summary"]["per_role"]
csv_path = HERE / "生产3.8.7-全部控件逐角色验收台账.csv"
INPUTS[csv_path.name] = sha(csv_path.read_bytes())
with csv_path.open(encoding="utf-8-sig", newline="") as stream:
    reader = csv.DictReader(stream)
    csv_columns = reader.fieldnames
    csv_rows = list(reader)
def serialized(value):
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return "" if value is None else str(value)
checks["JSON与CSV所有字段逐格相同"] = len(csv_rows) == 3080 and csv_columns == list(rows[0]) and all(
    output == {key: serialized(value) for key, value in row.items()} for output, row in zip(csv_rows, rows)
) and INPUTS[csv_path.name] == summary["csv_sha256"]

# 依据原始观察、主代理精确动作补充以及实际模板逐条裁定；不读取作者SPEC作为预期。
# 三元组为原始观察ID、经源码复核的清单序号、仅该场景可用的状态。
expected_links = {
    "owner_a": [
        ("ui01", 315, "passed"), ("ui03", 339, "partially_tested"), ("ui04", 126, "passed"),
        ("ui06", 130, "partially_tested"), ("ui08", 454, "passed"), ("ui08", 450, "observed"),
        ("ui09", 449, "passed"), ("ui10", 466, "partially_tested"), ("ui11", 449, "partially_tested"),
        ("ui12", 465, "partially_tested"), ("ui14", 329, "partially_tested"),
        ("ui16", 533, "passed"), ("ui17", 534, "passed"),
    ],
    "member_a": [
        ("m01", 315, "passed"), ("m02", 454, "observed"), ("m02", 466, "observed"),
        ("m02", 467, "observed"), ("m03", 465, "partially_tested"), ("m04", 436, "observed"),
        ("m04", 437, "observed"), ("m04", 438, "observed"), ("m05", 330, "observed"),
        ("m06", 329, "partially_tested"), ("m06", 324, "observed"), ("m07", 129, "passed"),
    ],
    "owner_b": [("b01", 315, "passed"), ("b04", 429, "passed"), ("b05", 424, "passed"), ("b06", 129, "passed")],
    "no_permission": [("n01", 315, "passed"), ("n02", 340, "observed"), ("n03", 339, "passed"), ("n04", 348, "observed"), ("n04", 349, "observed"), ("n05", 348, "passed"), ("n06", 129, "partially_tested")],
}
actual_links = []
references_exact = True
aggregation_exact = True
for row in rows:
    role = row["account_role"]
    observations = row["current_release_observations"]
    for item, evidence in zip(observations, row["current_release_evidence"]):
        key = (role, item["observation_id"])
        actual_links.append((role, item["observation_id"], int(row["control_id"].split(":")[-1]), item["status"]))
        references_exact &= item["original"] == raw_observations[key] and item["source_file"] == f"生产浏览器{role}观察.json" and evidence["file"] == str((HERE / item["source_file"]).relative_to(ROOT)) and evidence["observation_id"] == item["observation_id"] and evidence["observer"] == "/root"
    references_exact &= len(observations) == len(row["current_release_evidence"])
    if observations:
        aggregation_exact &= row["current_release_status"] == next(status for status in ["failed", "blocked", "partially_tested", "passed", "observed"] if status in {item["status"] for item in observations})
        references_exact &= row["current_release_actual_result"] == "；".join(item["original"]["result"] for item in observations)
    else:
        aggregation_exact &= row["current_release_status"] == ("blocked" if role == "administrator" else "untested") and row["current_release_actual_result"] is None and row["current_release_evidence"] == []
checks["36条映射逐条对原件及保守状态"] = len(actual_links) == 36 and sorted(actual_links) == sorted((role, *item) for role, items in expected_links.items() for item in items) and references_exact and aggregation_exact
index = {(item["account_role"], item["original"]["id"]): item for item in data["current_browser_observation_index"]}
mapped_keys = {(role, observation_id) for role, observation_id, _ordinal, _status in actual_links}
checks["36原观察29映射7补充全保留"] = len(index) == len(raw_observations) == 36 and len(mapped_keys) == 29 and len(data["current_supplemental_observations"]) == 7 and sum(bool(row["current_release_observations"]) for row in rows) == 34 and all(
    index[key]["original"] == original and bool(index[key]["mapped_control_ids"]) == (key in mapped_keys) for key, original in raw_observations.items()
) and all(item == index[(item["account_role"], item["original"]["id"])] and not item["mapped_control_ids"] and item["unmapped_reason"] for item in data["current_supplemental_observations"])
immutable_fields = ["row_id", "account_role", "account_label", "control_id", "file", "line", "tag", "is_button_or_link", "label", "dynamic_accessible_label", "events", "source_conditions", "expected_behavior", "historical_observations", "previous_release_observations", "previous_acceptance_account_id"]
checks["源码定位及旧34格66观察24补充保留"] = all(all(row[field] == old[field] for field in immutable_fields) for row, old in zip(rows, old_rows)) and sum(bool(row["previous_release_observations"]) for row in rows) == 34 and sum(len(group["observations"]) for row in rows for group in row["previous_release_observations"]) == 66 and data["previous_supplemental_observations"] == base["previous_supplemental_observations"] and len(data["previous_supplemental_observations"]) == 24
checks["精确按钮补充版本及映射边界存在"] = supplement["版本"] == "3.8.7" and supplement["发布提交"] == "4035d1902342ab88357b5bb6071545d1a0572dd1" and len(supplement["观察补充"]) == 4 and all(row["coverage_boundary"] for row in rows) and all(
    row["current_release_environment"]["version"] == "3.8.7" and row["current_release_environment"]["release"] == supplement["发布提交"] for row in rows
)
INPUTS[Path(__file__).name] = sha(Path(__file__).read_bytes())
report = {
    "审阅人": "permission_matrix", "状态": "通过" if all(checks.values()) else "打回", "检查": checks,
    "总格数": len(rows), "状态重数": dict(counts), "逐角色重数": per_role,
    "来源SHA256": INPUTS, "本轮映射格数": 34, "本轮映射观察链接": len(actual_links),
    "原始观察数": len(raw_observations), "唯一映射原观察数": len(mapped_keys), "未映射补充数": 7,
    "生产请求数": 0, "生产写入": False, "生成器执行": False,
    "证据边界": [
        "四账号正常登录仅证明所列正常按钮场景；不推定错误密码、网络失败、限流冷却生产实测。",
        "owner_a刷新只点未收完整结果、首次紧连编辑与编辑取消结果不完整、no_permission退出末态不完整均保持partial。",
        "项目表格行、搜索结果和文本文件查看仅覆盖QA实例，保持partial；成员上传/下载描述无法唯一定位者不猜按钮。",
        "成员保存按钮隐藏仅observed；没有用AX的settable推导Monaco只读编辑实测。",
        "616管理员格仍blocked；未用92个API夹具请求、权限矩阵、本地挂载测试或旧版观察新增UI passed。",
        "这是原始CUA观察转录与台账的离线独审，不是第二轮浏览器点击。",
    ],
}
output = HERE / "生产3.8.7-UI台账独立复核.json"
if output.exists():
    raise SystemExit("目标已存在，拒绝覆盖")
output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
print(json.dumps({"状态": report["状态"], "检查数": len(checks), "失败": [key for key, ok in checks.items() if not ok], "状态重数": report["状态重数"]}, ensure_ascii=False))
raise SystemExit(0 if all(checks.values()) else 1)
