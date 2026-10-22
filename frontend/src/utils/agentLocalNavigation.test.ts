import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createMemoryHistory, createRouter } from 'vue-router'
import { agentChatStorageKey, loadAgentChatSnapshot, saveAgentChatSnapshot, type AgentChatSnapshotMessage } from './agentChatSessions'
import { mergeLocalNavigationMessages, performLocalNavigation, settleLocalNavigationSnapshot } from './agentLocalNavigation'

const local = (id: string, role: 'user' | 'assistant', content: string): AgentChatSnapshotMessage => ({
  role, content, localAction: { id, kind: 'navigation', status: 'completed' },
})
const merge = (remote: AgentChatSnapshotMessage[], cached: AgentChatSnapshotMessage[]) => (
  mergeLocalNavigationMessages(remote, cached, row => row, row => ({ ...row }))
)

beforeEach(() => localStorage.clear())

describe('本地导航的独立会话记录', () => {
  it('服务端无run或消息时保留导航问答，重复恢复不重复追加', () => {
    const cached = [local('nav-1', 'user', '打开审计'), local('nav-1', 'assistant', '已打开操作审计')]
    expect(merge([], cached)).toEqual(cached)
    expect(merge(merge([], cached), cached)).toEqual(cached)
  })

  it('导航保留在服务端历史之间，相同文字的两次导航保持独立', () => {
    const first = { role: 'assistant' as const, content: '第一轮结果' }
    const second = { role: 'user' as const, content: '继续审查' }
    const cached = [first, local('nav-1', 'user', '打开审计'), local('nav-1', 'assistant', '已打开'),
      second, local('nav-2', 'user', '打开审计'), local('nav-2', 'assistant', '已打开')]
    expect(merge([first, second], cached)).toEqual(cached)
  })

  it('缺失的普通模型提问不会被伪装成本地导航记录', () => {
    expect(merge([], [{ role: 'user', content: '请分析代码' }])).toEqual([])
  })

  it('未确认动作恢复为未确认说明，不冒充成功且不重放', () => {
    const cached = [local('nav-pending', 'assistant', '正在打开')]
    cached[0]!.localAction!.status = 'pending'
    expect(merge([], cached)[0]!.content).toContain('尚未确认')
    expect(merge([], cached)[0]!.localAction!.status).toBe('pending')
  })

  it('跳转结算只更新捕获的账号、surface、session与action', () => {
    const key = agentChatStorageKey('user', 7)
    saveAgentChatSnapshot('session-one', { messages: [local('nav-1', 'assistant', '等待')], runStatus: null, updatedAt: 1 }, key)
    settleLocalNavigationSnapshot('session-one', key, 'nav-1', 'failed', '页面未打开')
    expect(loadAgentChatSnapshot('session-one', key)?.messages[0]).toMatchObject({ content: '页面未打开', localAction: { status: 'failed' } })
    expect(loadAgentChatSnapshot('session-one', agentChatStorageKey('user', 8))).toBeNull()
    expect(loadAgentChatSnapshot('session-one', agentChatStorageKey('admin', 7))).toBeNull()
    expect(loadAgentChatSnapshot('session-two', key)).toBeNull()
    settleLocalNavigationSnapshot('session-two', key, 'nav-1', 'completed', '已打开')
    expect(loadAgentChatSnapshot('session-two', key)).toBeNull()
  })
})

describe('真实导航结果确认', () => {
  function router() {
    return createRouter({ history: createMemoryHistory(), routes: [
      { path: '/', component: {} }, { path: '/audit', component: {} },
      { path: '/admin/audit', redirect: '/audit' }, { path: '/403', component: {} },
    ] })
  }

  it('真实跳转及已有重定向入口确认后才报告成功', async () => {
    const target = router()
    await target.push('/')
    const settled = vi.fn()
    await performLocalNavigation(target, '/admin/audit', '审计', () => true, settled)
    expect(target.currentRoute.value.path).toBe('/audit')
    expect(settled).toHaveBeenCalledExactlyOnceWith('completed', '已打开「审计」。')
  })

  it('守卫拒绝、跳到403和账号切换都不能报告成功', async () => {
    for (const failure of ['abort', 'redirect', 'scope']) {
      const target = router()
      await target.push('/')
      if (failure !== 'scope') target.beforeEach(to => to.path === '/audit' ? (failure === 'abort' ? false : '/403') : true)
      const settled = vi.fn()
      await performLocalNavigation(target, '/audit', '审计', () => failure !== 'scope', settled)
      expect(settled).toHaveBeenCalledExactlyOnceWith('failed', expect.any(String))
      expect(settled.mock.calls[0]![1]).not.toMatch(/^已打开/)
    }
  })
})
