"""进程维护只释放过期缓存；未过期数据和活跃讨论保持可用。"""
import asyncio
import time

from app.agents.discussion_bus import DiscussionBus
from app.services import dashboard_service, runtime_cache_service, sandbox_service


def test_cache_sweep_releases_expired_copies_and_keeps_live_values(monkeypatch):
    now = time.monotonic()
    monkeypatch.setattr(dashboard_service.settings, 'dashboard_stats_cache_seconds', 60)
    monkeypatch.setattr(dashboard_service, '_stats_cache', {
        ('old',): (now - 61, {'large': 'expired'}),
        ('fresh',): (now, {'value': 'live'}),
    })
    monkeypatch.setattr(sandbox_service, '_AGENT_TEST_CACHE', {
        ('old', 'py', 'full'): (now - 1, [{'content': 'expired'}]),
        ('fresh', 'py', 'full'): (now + 60, [{'content': 'live'}]),
    })
    bus = DiscussionBus()
    bus.create_session('expired', task_id=1, file_name='old.py')
    bus.close_session('expired')
    bus.create_session('running', task_id=2, file_name='live.py')
    bus.get_session('expired').closed_at = time.time() - 4000
    monkeypatch.setattr(DiscussionBus, '_instance', bus)

    counts = runtime_cache_service.sweep_runtime_caches()

    assert counts == {'dashboard': 1, 'sandbox_tests': 1, 'roundtable_sessions': 1}
    assert set(dashboard_service._stats_cache) == {('fresh',)}
    assert set(sandbox_service._AGENT_TEST_CACHE) == {('fresh', 'py', 'full')}
    assert bus.get_session('running') is not None
    assert runtime_cache_service.sweep_runtime_caches() == {
        'dashboard': 0, 'sandbox_tests': 0, 'roundtable_sessions': 0,
    }


def test_test_file_cache_is_bounded_and_returns_independent_copies(monkeypatch):
    monkeypatch.setattr(sandbox_service, '_AGENT_TEST_CACHE', {})
    monkeypatch.setattr(sandbox_service, '_AGENT_TEST_CACHE_LIMIT', 2)
    source = [{'content': 'retained'}]
    for key in ('one', 'two', 'three'):
        result = sandbox_service._store_agent_test_files((key, 'py', 'full'), source)
    source[0]['content'] = 'changed source'
    result[0]['content'] = 'changed result'
    assert len(sandbox_service._AGENT_TEST_CACHE) == 2
    assert sandbox_service._get_cached_agent_test_files(('three', 'py', 'full')) == [{'content': 'retained'}]


def test_maintenance_lifecycle_is_idempotent():
    async def run():
        runtime_cache_service.start_runtime_cache_sweeper()
        task = runtime_cache_service._sweeper
        runtime_cache_service.start_runtime_cache_sweeper()
        assert runtime_cache_service._sweeper is task
        await runtime_cache_service.stop_runtime_cache_sweeper()
        assert task.cancelled()
        assert runtime_cache_service._sweeper is None
    asyncio.run(run())
