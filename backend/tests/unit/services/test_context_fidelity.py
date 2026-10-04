import time

from app.services.context_fidelity import (
    extract_protected_facts,
    extract_user_fact_ledger,
    retrieve_relevant_user_facts,
)


def test_user_fact_ledger_keeps_unmarked_business_facts_verbatim() -> None:
    text = "统计时区是 Asia/Taipei，月末退款按原始交易月份记账。"

    assert extract_user_fact_ledger(text) == [text]


def test_user_fact_ledger_keeps_short_sentences_inside_longer_messages() -> None:
    fact = "月末退款按原始交易月份记账。"
    text = "旧背景" * 800 + "。" + fact + "补充背景" * 800

    assert extract_user_fact_ledger(text) == [fact]


def test_user_fact_ledger_does_not_copy_long_unpunctuated_filler_messages() -> None:
    assert extract_user_fact_ledger("历史-46-" + "x" * 180) == []


def test_retrieves_relevant_fact_from_long_unpunctuated_user_history() -> None:
    fact = "月末退款按原始交易月份记账，统计时区使用 Asia/Taipei"
    transcript = [
        {"role": "user", "content": "旧背景" * 500 + fact + "补充背景" * 500},
        {"role": "user", "content": "月末退款的统计时区和月份规则是什么？"},
    ]

    retrieved = retrieve_relevant_user_facts(
        transcript,
        [0],
        transcript[-1]["content"],
    )

    assert retrieved
    assert retrieved[0][0] == 0
    assert fact in retrieved[0][1]
    assert len(retrieved[0][1]) <= 1_200


def test_extracts_chinese_negation_scope_and_numeric_constraints() -> None:
    text = "不要跨账号读取聊天。仅管理员可审批发布。并发人数设为 2。"

    facts = extract_protected_facts(text)

    assert any("不要跨账号读取聊天" in fact for fact in facts)
    assert any("仅管理员可审批发布" in fact for fact in facts)
    assert any("并发人数设为 2" in fact for fact in facts)


def test_keeps_long_restricted_object_after_negation_marker() -> None:
    tail = "目标账号的会话历史、附件内容以及派生摘要"
    text = "不得" + ("任何其他说明文字" * 20) + tail + "。"

    facts = extract_protected_facts(text)

    assert facts
    assert any(tail in fact for fact in facts)


def test_very_long_unpunctuated_constraint_has_explicit_bounded_excerpt() -> None:
    text = "必须保持只读 " + "x" * 100_000 + " 最后按此检查"

    facts = extract_protected_facts(text)

    assert len(facts) == 1
    assert len(facts[0]) <= 2_000
    assert facts[0].startswith("必须保持只读")
    assert "最后按此检查" in facts[0]
    assert "原文过长" in facts[0] and "中段省略" in facts[0]
    assert len(text) > 100_000


def test_long_unpunctuated_constraint_keeps_middle_directive_and_endpoints() -> None:
    text = "开头线索" + "x" * 50_000 + "不得跨账号读取" + "y" * 50_000 + "结尾线索"

    facts = extract_protected_facts(text)

    assert len(facts) == 1 and len(facts[0]) <= 2_000
    assert "开头线索" in facts[0]
    assert "不得跨账号读取" in facts[0]
    assert "结尾线索" in facts[0]
    assert "中段省略" in facts[0]


def test_long_unpunctuated_text_keeps_every_separated_constraint_marker() -> None:
    clauses = [
        "必须保留规则A",
        "不得调用外网",
        "只允许当前账号访问",
        "绝不能发布",
        "必须先管理员审批",
        "不得删除数据",
        "只允许只读当前项目",
        "必须写明风险",
    ]
    text = ("x" * 2_500).join(clauses)

    facts = extract_protected_facts(text)

    assert len(facts) == 1
    assert all(clause in facts[0] for clause in clauses)


def test_short_constraint_remains_exact_original_sentence() -> None:
    text = "必须只使用当前账号的数据"

    assert extract_protected_facts(text) == [text]


def test_avoids_broad_final_and_coverage_word_false_positives() -> None:
    facts = extract_protected_facts("最终报告封面颜色为蓝色。代码覆盖率达到 80%。")

    assert not any("最终报告封面颜色" in fact for fact in facts)
    assert any("代码覆盖率达到 80%" in fact for fact in facts)


def test_extracts_explicit_correction_without_treating_final_as_a_rule() -> None:
    facts = extract_protected_facts("最终改为仅管理员可以审批发布。")

    assert any("最终改为仅管理员可以审批发布" in fact for fact in facts)


def test_extracts_english_scope_and_correction_constraints() -> None:
    facts = extract_protected_facts(
        "Do not show another user's chat. Change the maximum team size to 3. "
        "Ensure the chat remains isolated."
    )

    assert any("Do not show another user's chat" in fact for fact in facts)
    assert any("maximum team size to 3" in fact for fact in facts)
    assert any("Ensure the chat remains isolated" in fact for fact in facts)


def test_extracts_concurrency_count_and_avoids_embedded_english_words() -> None:
    facts = extract_protected_facts(
        "并发 2 人。Whenever the worker finishes, update the cache. "
        "OnlyOffice is installed. Not\n only include public files."
    )

    assert any("并发 2 人" in fact for fact in facts)
    assert not any("Whenever" in fact for fact in facts)
    assert not any("OnlyOffice" in fact for fact in facts)
    assert not any("Not" in fact or "only include" in fact for fact in facts)


def test_source_context_can_skip_broad_positive_goal_markers() -> None:
    text = "The old implementation ensures consistency across the service."

    assert extract_protected_facts(text, include_goals=False) == []


def test_extracts_chinese_role_permission_semantics() -> None:
    text = "管理员才能审批 Agent 发布。普通用户不该审批 Agent 发布。"

    facts = extract_protected_facts(text)

    assert any("管理员才能审批 Agent 发布" in fact for fact in facts)
    assert any("普通用户不该审批 Agent 发布" in fact for fact in facts)


def test_extracts_chinese_account_isolation_semantics() -> None:
    text = "当前账号只看自己的历史。不同账号互相隔离。"

    facts = extract_protected_facts(text)

    assert any("当前账号只看自己的历史" in fact for fact in facts)
    assert any("不同账号互相隔离" in fact for fact in facts)


def test_permission_and_account_patterns_skip_unrelated_descriptions() -> None:
    text = "管理员查看项目列表。普通用户介绍自己的工作经历。不同账号使用不同主题颜色。"

    assert extract_protected_facts(text) == []


def test_extracts_roundtable_retention_followup_and_silence_window() -> None:
    text = "圆桌结束后保留 5 分钟可继续追问，之后禁言。"

    facts = extract_protected_facts(text)

    assert any(text.rstrip("。") in fact for fact in facts)


def test_extracts_followup_window_without_spaces_or_sentence_boundary() -> None:
    text = "结束后保留5分钟可继续追问"

    assert extract_protected_facts(text) == [text]


def test_time_window_scan_stays_fast_on_long_sentence_without_boundary() -> None:
    started = time.perf_counter()
    facts = extract_protected_facts("x" * 100_000)

    assert facts == []
    assert time.perf_counter() - started < 2.0


def test_extracts_risk_tier_review_decisions() -> None:
    text = "低风险和中风险自动通过，高风险需要询问。"

    facts = extract_protected_facts(text)

    assert any("低风险和中风险自动通过" in fact for fact in facts)
    assert any("高风险需要询问" in fact for fact in facts)


def test_extracts_admin_only_approval_permission() -> None:
    text = "只给管理员审批权限。"

    facts = extract_protected_facts(text)

    assert any(text.rstrip("。") in fact for fact in facts)


def test_extracts_confirmation_gated_publication_and_creation() -> None:
    texts = ("用户确认后发布。", "管理员审批以后才创建 Agent。")

    for text in texts:
        facts = extract_protected_facts(text)
        assert any(text.rstrip("。") in fact for fact in facts)


def test_policy_specific_patterns_skip_adjacent_general_descriptions() -> None:
    text = (
        "会议结束后保留 5 分钟整理记录。"
        "演讲环节持续 5 分钟，发言人介绍议程。"
        "低风险案例数量多，高风险案例数量少。"
        "管理员查看审批权限说明。"
        "用户确认了发布计划。"
        "管理员审批 Agent 配置。"
    )

    assert extract_protected_facts(text) == []
