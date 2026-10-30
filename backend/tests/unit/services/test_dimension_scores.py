"""8 维度评分：由已保存的问题集（类型 x 严重度）在读取时派生。

背景：报告详情页的「8 维度评分雷达」读取 ``stats.dim_scores``，但后端任何审查路径（标准审查、
圆桌讨论）从未写出过这个字段，所以雷达在所有非领域报告上都显示「暂无 8 维度数据」。
规则：维度分与整体评分、逐文件分使用同一条严重度扣分规则；没有依据的维度是“无数据”，
不是 0 分，也不凭空给满分。
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from app.ai.scoring import compute_score_breakdown
from app.services.dimension_scores import (
    DIMENSION_KEYS,
    compute_dimension_scores,
)

ALL_KEYS = ["style", "naming", "comment", "maintain", "performance", "exception", "logic", "security"]


def _by_key(result: dict) -> dict:
    return {item["key"]: item for item in result["items"]}


def test_there_are_exactly_eight_dimensions_in_the_documented_order():
    assert list(DIMENSION_KEYS) == ALL_KEYS
    assert [item["key"] for item in compute_dimension_scores([])["items"]] == ALL_KEYS


def test_each_issue_type_maps_to_its_dimension_with_the_overall_deduction_rule():
    rows = [
        ("安全漏洞", "严重", 1), ("安全漏洞", "高", 2),   # 100 - 15 - 16 = 69
        ("异常处理", "中", 3),                            # 100 - 9 = 91
        ("代码规范", "低", 4),                            # 100 - 4 = 96
    ]

    result = compute_dimension_scores(rows)
    items = _by_key(result)

    assert items["security"]["score"] == 69
    assert items["exception"]["score"] == 91
    assert items["style"]["score"] == 96
    assert items["security"]["severity_counts"] == {"严重": 1, "高": 2, "中": 0, "低": 0}
    assert items["security"]["status"] == "scored"
    assert result["scores"] == {"security": 69, "exception": 91, "style": 96}
    assert result["scored_count"] == 3 and result["no_data_count"] == 5


def test_dimension_score_is_exactly_the_overall_rule_applied_to_the_dimensions_issues():
    counts = {"严重": 1, "高": 1, "中": 2, "低": 3}
    rows = [("可维护性", severity, count) for severity, count in counts.items()]

    item = _by_key(compute_dimension_scores(rows))["maintain"]

    assert item["score"] == compute_score_breakdown(counts)["score"]
    assert item["total_deduction"] == compute_score_breakdown(counts)["total_deduction"]


def test_dimensions_without_recorded_issues_are_no_data_never_zero_and_never_full_marks():
    result = compute_dimension_scores([("安全漏洞", "高", 1)])

    for key in ALL_KEYS:
        item = _by_key(result)[key]
        if key == "security":
            assert item["score"] == 92
            continue
        assert item["status"] == "no_data"
        assert item["score"] is None
        assert item["issue_count"] == 0
        assert "没有记录" in item["reason"]
        assert key not in result["scores"]


def test_a_real_zero_is_a_score_and_stays_distinct_from_no_data():
    result = compute_dimension_scores([("安全漏洞", "严重", 7)])  # 100 - 105 -> 下限 0

    security = _by_key(result)["security"]
    assert security["score"] == 0
    assert security["status"] == "scored"
    assert result["scores"]["security"] == 0
    assert _by_key(result)["style"]["status"] == "no_data"


def test_issue_types_outside_the_eight_dimensions_are_counted_but_not_plotted():
    result = compute_dimension_scores([("其他", "中", 2), ("汇总修复", "低", 1), (None, "低", 1), ("", "低", 1)])

    assert result["scored_count"] == 0
    assert result["scores"] == {}
    assert result["unmapped_issue_count"] == 5


def test_ungraded_severity_makes_only_that_dimension_no_data():
    result = compute_dimension_scores([("安全漏洞", "未分级", 1), ("安全漏洞", "高", 1), ("代码规范", "低", 1)])

    security = _by_key(result)["security"]
    assert security["status"] == "no_data" and security["score"] is None
    assert "未分级" in security["reason"]
    assert _by_key(result)["style"]["score"] == 99


@pytest.mark.parametrize("alias,key", [
    ("maintainability", "maintain"), ("bug", "logic"), ("perf", "performance"),
    ("security", "security"), ("style", "style"),
])
def test_english_keys_and_historical_aliases_map_like_the_frontend(alias, key):
    result = compute_dimension_scores([(alias, "低", 1)])

    assert _by_key(result)[key]["score"] == 99


def test_counts_of_the_same_dimension_from_different_spellings_are_merged():
    result = compute_dimension_scores([("可维护性", "低", 1), ("maintainability", "低", 2), ("maintain", "中", 1)])

    item = _by_key(result)["maintain"]
    assert item["issue_count"] == 4
    assert item["score"] == 100 - 3 - 1 * 3


def test_unavailable_overall_score_hides_every_dimension_with_the_reason():
    result = compute_dimension_scores(
        [("安全漏洞", "高", 1)], available=False, unavailable_reason="整体评分未生成",
    )

    assert result["available"] is False
    assert result["scores"] == {}
    assert all(item["status"] == "no_data" for item in result["items"])
    assert all("整体评分未生成" in item["reason"] for item in result["items"])


def test_zero_and_negative_counts_are_ignored():
    result = compute_dimension_scores([("安全漏洞", "高", 0), ("安全漏洞", "高", -3)])

    assert result["scored_count"] == 0


def test_result_does_not_expose_internal_enumerations_in_english_labels():
    for item in compute_dimension_scores([])["items"]:
        assert re.search(r"[一-龥]", item["label"])
    assert "严重度" in compute_dimension_scores([])["basis"]


def test_backend_dimension_mapping_matches_the_frontend_constants():
    """前后端各有一份 issue_type -> 维度 的映射，漂移会让同一问题在两端归到不同维度。"""
    from app.services.dimension_scores import ISSUE_TYPE_TO_DIMENSION

    dim_ts = Path(__file__).resolve().parents[4] / "frontend" / "src" / "constants" / "dim.ts"
    if not dim_ts.exists():
        pytest.skip("前端源码不在当前运行环境")
    source = dim_ts.read_text(encoding="utf-8")
    alias_block = re.search(r"export const DIM_ALIAS[^{]*\{(.*?)\n\}", source, re.S).group(1)
    frontend = dict(re.findall(r"^\s*([\w一-龥]+):\s*'(\w+)'", alias_block, re.M))
    keys_block = re.search(r"export const DIM_KEYS = \[(.*?)\] as const", source, re.S).group(1)
    frontend.update({key: key for key in re.findall(r"'(\w+)'", keys_block)})

    assert frontend, "未能从 dim.ts 解析出映射"
    assert frontend == ISSUE_TYPE_TO_DIMENSION
