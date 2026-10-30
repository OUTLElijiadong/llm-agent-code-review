"""一致性判定函数 claims_are_coherent 的单元测试（确定性规则，不调用模型）。"""
from __future__ import annotations

import pytest

from app.ai.finding_coherence import (
    TOPIC_MIN_OVERLAP,
    TOPIC_MIN_SHARED_WEIGHT,
    ClaimView,
    claims_are_coherent,
    has_security_content,
    normalize_cwe,
    topic_overlap,
    view_from_mapping,
)


def _view(title: str, description: str = "", issue_type: str = "安全漏洞", cwe: str = "", **extra) -> ClaimView:
    return ClaimView(issue_type=issue_type, cwe=cwe, title=title, description=description, **extra)


def test_normalize_cwe_accepts_common_spellings():
    assert normalize_cwe("cwe-89") == "CWE-89"
    assert normalize_cwe("CWE_502") == "CWE-502"
    assert normalize_cwe(" 79 ") == "CWE-79"
    assert normalize_cwe("") == "" and normalize_cwe(None) == ""


def test_same_cwe_is_coherent_even_with_different_wording():
    verdict = claims_are_coherent(
        _view("反序列化", "d1", cwe="CWE-502"), _view("完全不同的措辞", "d2", cwe="cwe-502"),
    )
    assert verdict.ok and verdict.reason == "same_cwe"


def test_different_cwe_splits_by_default_and_defers_for_the_aggregator():
    left = _view("反序列化外部数据", "pickle.loads 反序列化外部输入", cwe="CWE-502")
    right = _view("载荷缺少完整性校验", "载荷可被篡改", cwe="CWE-345")
    assert claims_are_coherent(left, right).reason == "cwe_conflict"
    # 聚合器保留“归类意见不一”的声明交人工复核，但主题完全无关的两条仍然拆开。
    deferred = claims_are_coherent(left, right, cwe_conflict_splits=False)
    assert not deferred.ok and deferred.reason == "unrelated_topic"
    related = claims_are_coherent(
        _view("反序列化外部数据", "pickle.loads 反序列化外部输入", cwe="CWE-502"),
        _view("不可信数据的反序列化", "外部数据被 pickle 反序列化", cwe="CWE-94"),
        cwe_conflict_splits=False,
    )
    assert related.ok and related.reason == "topic_overlap"


def test_security_content_is_judged_by_content_not_by_label():
    mislabelled = _view("文档示例跨代码块引用变量", "示例引用了前一个代码块的变量")
    assert mislabelled.issue_type == "安全漏洞"
    assert not has_security_content(mislabelled)
    real = _view("不可信反序列化风险", "pickle.loads 反序列化外部输入", issue_type="其他")
    assert has_security_content(real)
    assert has_security_content(_view("x", cwe="CWE-79"))
    assert has_security_content(_view("x", owasp="A05:2025-Injection"))


def test_documentation_claim_is_not_coherent_with_a_security_claim():
    verdict = claims_are_coherent(
        _view("两个类的命名及文档衔接", "命名不清晰，文档缺少衔接说明", issue_type="命名规范"),
        _view("不可信反序列化风险", "pickle.loads 反序列化外部输入", cwe="CWE-502"),
    )
    assert not verdict.ok and verdict.reason == "security_vs_non_security"


def test_label_only_security_claims_need_topic_overlap():
    verdict = claims_are_coherent(
        _view("文档示例跨代码块引用变量", "文档里的示例引用了前一个代码块定义的变量"),
        _view("签名载荷可读性", "载荷签名后仍可被直接阅读"),
    )
    assert not verdict.ok and verdict.reason == "unrelated_topic"


def test_mislabelled_issue_with_matching_security_language_is_coherent():
    verdict = claims_are_coherent(
        _view("SQL 注入", "第 10 行检测到 SQL 注入", cwe="CWE-89"),
        _view("用户输入拼接进 SQL 导致注入", "uid 直接拼接进查询语句", issue_type="潜在Bug"),
    )
    assert verdict.ok and verdict.reason == "topic_overlap"


@pytest.mark.parametrize(
    ("left", "right", "expected"),
    [
        (("函数命名不清晰", "load_payload 命名过于宽泛，看不出载荷类型"),
         ("load_payload 命名太宽泛", "函数名没有体现载荷类型，建议改名"), True),
        (("缺少参数说明", "docstring 没有说明 blob 的格式"),
         ("缺少参数说明", "docstring 未说明 blob 格式与来源"), True),
        (("函数命名过于宽泛", "load_payload 命名过于宽泛，看不出载荷类型"),
         ("文档字符串缺少参数说明", "docstring 没有说明 blob 的格式与来源"), False),
        (("魔法数字", "超时时间 30 直接写在调用处，建议定义常量"),
         ("重复代码", "两处解析逻辑重复，建议抽取公共函数"), False),
    ],
)
def test_non_security_claims_also_need_topic_overlap(left, right, expected):
    verdict = claims_are_coherent(
        _view(*left, issue_type="命名规范"), _view(*right, issue_type="命名规范"),
    )
    assert verdict.ok is expected


def test_generic_words_alone_do_not_make_two_claims_the_same_topic():
    weight, overlap = topic_overlap(
        _view("存在风险", "建议使用更好的方法处理数据"), _view("可能导致问题", "需要进行检测发现代码问题"),
    )
    assert weight == 0 and overlap == 0.0


def test_thresholds_are_documented_constants():
    assert TOPIC_MIN_SHARED_WEIGHT == 2
    assert TOPIC_MIN_OVERLAP == pytest.approx(0.2)


def test_view_from_mapping_ignores_location_fields():
    view = view_from_mapping({
        "issue_type": "命名规范", "title": "标题", "description": "描述", "cwe": None,
        "source_anchor": "py:pickle.loads(blob)#1", "line_number": 6, "evidence": "pickle.loads(blob)",
    })
    assert view == ClaimView(issue_type="命名规范", title="标题", description="描述")
