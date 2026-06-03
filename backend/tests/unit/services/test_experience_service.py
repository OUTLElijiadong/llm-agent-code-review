"""单元测试: 经验记忆服务(自进化 L1)"""
from datetime import datetime, timedelta, timezone

from app.services import experience_service as es

# ── 纯函数 ──

def test_make_fingerprint_normalizes_title():
    """大小写/数字/标点不同但同类问题应聚到同一指纹"""
    a = es.make_fingerprint("python", "安全漏洞", "SQL 注入风险 (第10行)")
    b = es.make_fingerprint("Python", "安全漏洞", "sql注入风险(第 99 行)")
    assert a == b


def test_make_fingerprint_language_sensitive():
    a = es.make_fingerprint("python", "安全漏洞", "SQL注入")
    b = es.make_fingerprint("java", "安全漏洞", "SQL注入")
    assert a != b


def test_decay_weight_no_last_seen_is_base():
    assert es.decay_weight(5, 1, None, lam=1.0) == 4.0


def test_decay_weight_halves_after_one_halflife():
    now = datetime(2026, 1, 31, tzinfo=timezone.utc)
    last = now - timedelta(days=30)
    w = es.decay_weight(4, 0, last, now=now, halflife_days=30)
    assert abs(w - 2.0) < 1e-6  # 一个半衰期后权重减半


def test_decay_weight_penalizes_rejections():
    assert es.decay_weight(3, 3, None, lam=1.0) == 0.0


# ── DB 路径 ──

def test_harvest_is_idempotent_and_retrieve_filters(db, mk_issue):
    """harvest 幂等(重复运行不翻倍),retrieve 只取高权重且 accepted≥1"""
    now = datetime.now(timezone.utc)
    # 同一类问题被确认 3 次(同标题聚到一指纹)
    for i in range(3):
        mk_issue(db, issue_type="安全漏洞", status="fixed", task_id=i,
                 title="SQL注入风险", suggestion="用参数化查询",
                 fixed_code='cur.execute("...", (x,))', handled_at=now)
    # 一类纯噪声(只被忽略)
    mk_issue(db, issue_type="代码规范", status="ignored", task_id=1, title="缩进不一致")

    r1 = es.harvest(db, window_days=90, now=now)
    r2 = es.harvest(db, window_days=90, now=now)  # 再跑一次
    assert r1["clusters"] == r2["clusters"]  # 幂等

    # 直接查库验证计数未翻倍
    from app.models.review_experience import ReviewExperience
    sec = db.query(ReviewExperience).filter(
        ReviewExperience.issue_type == "安全漏洞").one()
    assert sec.accepted_count == 3  # 不是 6
    assert sec.canonical_suggestion == "用参数化查询"

    # 检索:高权重 SQL 经验应命中;纯噪声(accepted=0)不应注入
    hits = es.retrieve(db, language="python", min_weight=0.5, top_k=5)
    titles = [h.title for h in hits]
    assert "SQL注入风险" in titles
    assert all(h.accepted_count >= 1 for h in hits)


def test_retrieve_respects_language(db, mk_issue):
    now = datetime.now(timezone.utc)
    for i in range(3):
        mk_issue(db, issue_type="安全漏洞", status="fixed", task_id=i,
                 title="SQL注入", handled_at=now)
    es.harvest(db, now=now)
    # file_id=None → 语言记为 '*',对任何语言都应可检索
    hits = es.retrieve(db, language="go", min_weight=0.5)
    assert any(h.title == "SQL注入" for h in hits)
