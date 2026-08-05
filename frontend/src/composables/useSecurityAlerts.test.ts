import { flushPromises } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'

const hoisted = vi.hoisted(() => ({
  fetchUnread: vi.fn(),
  markRead: vi.fn(),
  subscribe: vi.fn(),
  notify: vi.fn(),
  records: [] as Array<{ onEvent: (ev: unknown) => void; close: () => void }>,
}))

vi.mock('@/api/securityAlerts', () => ({
  fetchUnreadAlerts: hoisted.fetchUnread,
  markAlertRead: hoisted.markRead,
}))
vi.mock('@/utils/agentEventStream', () => ({
  subscribeAgentEvents: hoisted.subscribe,
}))
vi.mock('element-plus/es/components/notification/index', () => ({
  ElNotification: hoisted.notify,
}))

import { useUserStore } from '@/stores/user'
import { resetSecurityAlertsState, setupSecurityAlerts } from '@/composables/useSecurityAlerts'

/** 构造一条最小告警 */
function makeAlert(overrides: Record<string, unknown>): Record<string, unknown> {
  return {
    id: 1,
    alert_type: 'abnormal_login',
    severity: 'warning',
    status: 'open',
    title: '异常登录告警',
    detail_json: null,
    created_at: '2026-08-05T08:00:00Z',
    ...overrides,
  }
}

/** 构造一条 admin_alert SSE 事件 */
function makeAdminAlertEvent(overrides: Record<string, unknown>): Record<string, unknown> {
  return {
    type: 'admin_alert',
    agent: 'operations',
    trace_id: 'trace-1',
    message: '检测到异常登录',
    payload: {
      alert_id: 10,
      severity: 'high',
      category: 'auth',
      title: '异常登录告警',
      suggestion: '建议锁定来源 IP',
    },
    timestamp: '2026-08-05T12:00:00Z',
    ...overrides,
  }
}

function emitAdminAlert(overrides: Record<string, unknown>): void {
  hoisted.records[hoisted.records.length - 1].onEvent(makeAdminAlertEvent(overrides))
}

beforeEach(() => {
  hoisted.fetchUnread.mockReset()
  hoisted.markRead.mockReset()
  hoisted.notify.mockReset()
  hoisted.subscribe.mockReset()
  hoisted.records.splice(0)
  hoisted.markRead.mockResolvedValue(undefined)
  hoisted.subscribe.mockImplementation((onEvent: (ev: unknown) => void) => {
    const record = { onEvent, close: vi.fn() }
    hoisted.records.push(record)
    return { close: record.close }
  })
  setActivePinia(createPinia())
  resetSecurityAlertsState()
  window.sessionStorage.clear()
})

describe('setupSecurityAlerts', () => {
  it('非管理员不启动,不拉取未读也不订阅 SSE', async () => {
    const handle = setupSecurityAlerts()
    await flushPromises()

    expect(handle.active.value).toBe(false)
    expect(handle.unreadCount.value).toBe(0)
    expect(hoisted.fetchUnread).not.toHaveBeenCalled()
    expect(hoisted.subscribe).not.toHaveBeenCalled()
    expect(hoisted.notify).not.toHaveBeenCalled()

    handle.dispose()
  })

  it('延迟加载的 admin 状态(刷新/登录场景)会响应式启动弹窗', async () => {
    const store = useUserStore()
    // 初始 profile/roles 未加载,isAdmin() 为 false
    const handle = setupSecurityAlerts()
    await flushPromises()
    expect(handle.active.value).toBe(false)
    expect(hoisted.fetchUnread).not.toHaveBeenCalled()

    // 路由守卫异步加载角色后变为 admin,应自动启动
    store.roles = ['admin']
    await flushPromises()
    expect(handle.active.value).toBe(true)
    expect(hoisted.subscribe).toHaveBeenCalledTimes(1)

    // 登出/角色清空后应关闭订阅
    store.roles = []
    await flushPromises()
    expect(handle.active.value).toBe(false)
    expect(hoisted.records[0].close).toHaveBeenCalled()
  })

  it('管理员按 created_at/id 顺序逐个弹窗并标记已读', async () => {
    const store = useUserStore()
    store.roles = ['admin']
    hoisted.fetchUnread.mockResolvedValue([
      makeAlert({ id: 3, severity: 'warning', title: '告警3', created_at: '2026-08-05T10:00:00Z' }),
      makeAlert({
        id: 1,
        severity: 'high',
        title: '告警1',
        created_at: '2026-08-05T08:00:00Z',
        detail_json: JSON.stringify({ suggestion: '请立即排查来源 IP', ip: '1.2.3.4' }),
      }),
      makeAlert({ id: 2, severity: 'critical', title: '告警2', created_at: '2026-08-05T09:00:00Z' }),
    ])

    const handle = setupSecurityAlerts()
    await flushPromises()

    expect(handle.active.value).toBe(true)
    expect(hoisted.notify).toHaveBeenCalledTimes(3)
    // 弹窗顺序与 created_at 升序一致:告警1(high) → 告警2(critical) → 告警3(warning)
    const titles = hoisted.notify.mock.calls.map((call) => call[0].title)
    expect(titles).toEqual(['告警1', '告警2', '告警3'])
    // critical 用 error,high/warning 用 warning
    const types = hoisted.notify.mock.calls.map((call) => call[0].type)
    expect(types).toEqual(['warning', 'error', 'warning'])
    // 弹窗 message 含 suggestion 摘要
    expect(hoisted.notify.mock.calls[0][0].message).toContain('请立即排查来源 IP')
    // 弹窗后按相同顺序标记已读
    expect(hoisted.markRead.mock.calls.map((call) => call[0])).toEqual([1, 2, 3])
    expect(handle.unreadCount.value).toBe(3)

    handle.dispose()
  })

  it('SSE 实时 admin_alert 达到阈值时弹窗并标记已读', async () => {
    const store = useUserStore()
    store.roles = ['admin']
    hoisted.fetchUnread.mockResolvedValue([])

    const handle = setupSecurityAlerts()
    await flushPromises()
    expect(hoisted.subscribe).toHaveBeenCalledTimes(1)

    emitAdminAlert({ payload: { alert_id: 10, severity: 'critical', title: '实时告警', suggestion: '立即处置' } })
    await flushPromises()

    expect(hoisted.notify).toHaveBeenCalledTimes(1)
    expect(hoisted.notify.mock.calls[0][0]).toMatchObject({
      type: 'error',
      title: '实时告警',
      message: '立即处置',
    })
    expect(hoisted.markRead).toHaveBeenCalledWith(10)
    expect(handle.unreadCount.value).toBe(1)

    handle.dispose()
  })

  it('info 级别不弹窗也不标记已读(未读列表与 SSE 均如此)', async () => {
    const store = useUserStore()
    store.roles = ['admin']
    hoisted.fetchUnread.mockResolvedValue([
      makeAlert({ id: 1, severity: 'info', title: '低优先级' }),
    ])

    const handle = setupSecurityAlerts()
    await flushPromises()

    expect(hoisted.notify).not.toHaveBeenCalled()
    expect(hoisted.markRead).not.toHaveBeenCalled()
    expect(handle.unreadCount.value).toBe(0)

    // SSE 的 info 事件同样不弹
    emitAdminAlert({ payload: { alert_id: 11, severity: 'info', title: '低优先级实时' } })
    await flushPromises()
    expect(hoisted.notify).not.toHaveBeenCalled()
    expect(hoisted.markRead).not.toHaveBeenCalled()
    expect(handle.unreadCount.value).toBe(0)

    handle.dispose()
  })

  it('已处理过的告警去重,不重复弹窗', async () => {
    const store = useUserStore()
    store.roles = ['admin']
    hoisted.fetchUnread.mockResolvedValueOnce([
      makeAlert({ id: 1, severity: 'warning', title: '首次弹窗' }),
    ])

    const first = setupSecurityAlerts()
    await flushPromises()
    expect(hoisted.notify).toHaveBeenCalledTimes(1)

    // 第二次启动,未读列表仍包含 id=1,但不应再次弹窗
    first.dispose()
    hoisted.fetchUnread.mockResolvedValueOnce([
      makeAlert({ id: 1, severity: 'warning', title: '不应再弹' }),
      makeAlert({ id: 2, severity: 'high', title: '新告警' }),
    ])
    hoisted.notify.mockClear()

    const second = setupSecurityAlerts()
    await flushPromises()

    expect(hoisted.notify).toHaveBeenCalledTimes(1)
    expect(hoisted.notify.mock.calls[0][0].title).toBe('新告警')
    expect(hoisted.markRead).toHaveBeenCalledWith(2)

    second.dispose()
  })

  it('dispose 关闭 SSE 后不再弹窗', async () => {
    const store = useUserStore()
    store.roles = ['admin']
    hoisted.fetchUnread.mockResolvedValue([])

    const handle = setupSecurityAlerts()
    await flushPromises()
    const record = hoisted.records[hoisted.records.length - 1]

    handle.dispose()
    expect(record.close).toHaveBeenCalled()

    emitAdminAlert({ payload: { alert_id: 12, severity: 'critical', title: '已关闭' } })
    await flushPromises()
    expect(hoisted.notify).not.toHaveBeenCalled()
  })
})
