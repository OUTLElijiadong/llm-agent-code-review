"""公共准入 fixture 只隔离审查额度，保留真实 HTTP 限流器及其余作用域。"""

from unittest.mock import Mock

import pytest
from limits import parse
from limits.util import WindowStats
from slowapi import Limiter

from app.core import rate_limit


def test_shared_admission_fixture_preserves_slowapi_wrapper():
    assert isinstance(rate_limit.limiter, Limiter)
    assert callable(rate_limit.limiter.limit)
    assert callable(rate_limit.limiter.limiter.test)


@pytest.mark.parametrize("sample", range(3))
def test_shared_admission_fixture_starts_fresh_and_keeps_actor_buckets_separate(sample, monkeypatch):
    backend = rate_limit.limiter.limiter
    incr = Mock(side_effect=AssertionError("审查额度不应访问真实存储"))
    monkeypatch.setattr(backend.storage, "incr", incr)
    limit = parse("2/minute")
    first_actor = ("prism:api", "user:7", "review_start_shared")
    second_actor = ("prism:api", "user:8", "review_start_shared")
    assert backend.hit(limit, *first_actor, cost=2)
    assert not backend.hit(limit, *first_actor)
    assert backend.hit(limit, *second_actor)
    stats = backend.get_window_stats(limit, *first_actor)
    assert isinstance(stats, WindowStats)
    assert stats.remaining == 0
    assert not incr.called


@pytest.mark.parametrize("identifiers", [("prism:api", "user:7", "login"), ()])
def test_shared_admission_fixture_delegates_other_scopes_to_original_backend(identifiers, monkeypatch):
    backend = rate_limit.limiter.limiter
    incr = Mock(return_value=3)
    get = Mock(return_value=3)
    get_expiry = Mock(return_value=123.0)
    monkeypatch.setattr(backend.storage, "incr", incr)
    monkeypatch.setattr(backend.storage, "get", get)
    monkeypatch.setattr(backend.storage, "get_expiry", get_expiry)
    limit = parse("5/minute")
    assert backend.hit(limit, *identifiers, cost=3)
    incr.assert_called_once_with(limit.key_for(*identifiers), limit.get_expiry(), amount=3)
    assert backend.get_window_stats(limit, *identifiers) == WindowStats(123.0, 2)
    get.assert_called_once_with(limit.key_for(*identifiers))
    get_expiry.assert_called_once_with(limit.key_for(*identifiers))
