import { siteBrowserApi, type SiteBrowserRequiredEvent } from '@/api/siteBrowser'
import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import { nextTick, type Plugin } from 'vue'
import { createMemoryHistory, createRouter, createWebHistory } from 'vue-router'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { AgentTeamDetail, AgentTeamStatus } from '@/api/agentTeams'

const streams = vi.hoisted(() => ({
  start: vi.fn(),
  records: [] as Array<{
    body: Record<string, unknown>
    onEvent: (event: unknown) => void
    resolve: () => void
    reject: (error: unknown) => void
    aborted: boolean
  }>,
}))

const messages = vi.hoisted(() => ({ error: vi.fn(), info: vi.fn(), success: vi.fn(), warning: vi.fn() }))
const sessionApi = vi.hoisted(() => ({ get: vi.fn(), page: vi.fn(), cancel: vi.fn() }))
const meshApi = vi.hoisted(() => ({ heartbeat: vi.fn(), inbox: vi.fn(), list: vi.fn(), conversations: vi.fn(), acknowledge: vi.fn() }))
const teamApi = vi.hoisted(() => ({ list: vi.fn(), detail: vi.fn(), messages: vi.fn(), events: vi.fn() }))
const mountedCopilots = new Set<VueWrapper>()

vi.mock('@/utils/responsesStream', () => ({ streamResponses: streams.start }))
vi.mock('@/api/agentResponses', () => ({
  getAgentResponseSession: sessionApi.get,
  getAgentResponseSessionMessages: sessionApi.page,
  cancelAgentResponseRun: sessionApi.cancel,
}))
vi.mock('@/api/agentMesh', () => ({
  heartbeatAgentMesh: meshApi.heartbeat,
  pullAgentMeshInbox: meshApi.inbox,
  listAgentMeshAgents: meshApi.list,
  listAgentMeshConversations: meshApi.conversations,
  acknowledgeAgentMeshMessage: meshApi.acknowledge,
  archiveAgentMeshSession: vi.fn().mockResolvedValue({ session_id: '', status: 'archived' }),
}))
vi.mock('@/api/agentTeams', () => ({
  listAgentTeams: teamApi.list,
  getAgentTeam: teamApi.detail,
  listAgentTeamMessages: teamApi.messages,
  listAgentTeamEvents: teamApi.events,
}))
vi.mock('element-plus/es/components/message/index', () => ({ ElMessage: messages }))

import { createPinia, getActivePinia, setActivePinia } from 'pinia'

import AdminCopilot from './AdminCopilot.vue'
import AdminLayout from './AdminLayout.vue'
import AgentChatDrawer from '@/components/ai/AgentChatDrawer.vue'
import VirtualCursor from '@/components/ai/VirtualCursor.vue'
import { useAgentActivityStore } from '@/stores/agentActivity'
import { useUserStore } from '@/stores/user'
import { agentChatStorageKey, loadAgentChatSnapshot, saveActiveAgentChatSession, saveAgentChatSessions, saveAgentChatSnapshot } from '@/utils/agentChatSessions'

it.each([true, false])('本地导航持久化：管理端路由成功=%s，恢复保留结果且不重放', async allowed => {
  const router = createRouter({ history: createMemoryHistory(), routes: [
    { path: '/', component: {} }, { path: '/admin/audit', component: {}, meta: { title: '管理端审计' } },
  ] })
  await router.push('/')
  if (!allowed) router.beforeEach(to => to.path === '/admin/audit' ? false : true)
  sessionApi.get.mockImplementation(async (_surface, session_id) => ({ surface: 'admin', session_id, run: null, messages: [], pending: null }))
  const wrapper = mountCopilot([router])
  const user = useUserStore()
  user.profile = { id: 88, username: 'audit-admin', role: 'admin', status: 1 }
  user.token = 'audit-admin-token'
  await openCopilot(wrapper)
  await flushSessionRestore()
  const vm = wrapper.vm as any
  vm.inputText = '打开管理端审计'
  await vm.sendMessage()
  await flushSessionRestore()
  const sessionId = vm.sessionId as string, key = agentChatStorageKey('admin', 88)
  const saved = loadAgentChatSnapshot(sessionId, key)
  expect(saved?.messages.filter(message => message.localAction)).toHaveLength(2)
  expect(saved?.messages.at(-1)?.localAction?.status).toBe(allowed ? 'completed' : 'failed')
  expect(router.currentRoute.value.path).toBe(allowed ? '/admin/audit' : '/')
  expect(streams.start).not.toHaveBeenCalled()
  saveAgentChatSessions(key, [{ id: sessionId, title: '审计', createdAt: Date.now() }])
  saveActiveAgentChatSession(key, sessionId)
  const push = vi.spyOn(router, 'push')
  try {
    // 同组件重复恢复与真实会话读取路径一致；不会把导航转为Responses重试。
    await vm.restoreSession()
    await flushSessionRestore()
    expect(wrapper.text()).toContain(allowed ? '已打开「管理端审计」' : '未能确认目标页面')
    expect(push).not.toHaveBeenCalled()
    expect(streams.start).not.toHaveBeenCalled()
  } finally { wrapper.unmount(); push.mockRestore() }
})

it.each(['打开用户与权限', '打开管理员的用户与权限'])('实际管理菜单“%s”直接导航，冷启动保留结果且不发模型', async prompt => {
  const router = createRouter({ history: createMemoryHistory(), routes: [
    { path: '/admin/operations', component: {}, meta: { title: '运行与审计中心', role: 'admin' } },
    { path: '/admin/access', component: {}, meta: { title: '用户与权限中心', role: 'admin', roles: ['admin'] } },
  ] })
  await router.push('/admin/operations?section=overview')
  sessionApi.get.mockImplementation(async (surface, session_id) => ({ surface, session_id, run: null, messages: [], pending: null }))
  const profile = { id: 88, username: 'menu-admin', role: 'admin', status: 1 }
  const original = mountCopilot([router])
  const user = useUserStore()
  user.token = 'menu-admin-token'
  user.profile = profile
  await openCopilot(original)
  await flushSessionRestore()
  try {
    await original.get('textarea').setValue(prompt)
    await original.get('.send-button').trigger('click')
    await flushSessionRestore()
    expect(streams.start).not.toHaveBeenCalled()
    expect(router.currentRoute.value.fullPath).toBe('/admin/access')
    expect(original.text()).toContain('已打开「用户与权限中心」。')
  } catch (error) { original.unmount(); throw error }
  const sessionId = (original.vm as any).sessionId as string
  const key = agentChatStorageKey('admin', profile.id)
  expect(loadAgentChatSnapshot(sessionId, key)?.messages.filter(message => message.localAction)).toHaveLength(2)
  original.unmount()

  // mountCopilot 创建全新 Pinia，profile 由异步登录恢复后写入。
  const refreshed = mountCopilot([router])
  const restoredUser = useUserStore()
  restoredUser.token = 'menu-admin-token'
  await Promise.resolve()
  restoredUser.profile = profile
  const push = vi.spyOn(router, 'push')
  try {
    await openCopilot(refreshed)
    await flushSessionRestore()
    expect((refreshed.vm as any).sessionId).toBe(sessionId)
    expect(refreshed.text()).toContain('已打开「用户与权限中心」。')
    expect(loadAgentChatSnapshot(sessionId, key)?.messages.filter(message => message.localAction)).toHaveLength(2)
    expect(push).not.toHaveBeenCalled()
    expect(streams.start).not.toHaveBeenCalled()
  } finally { refreshed.unmount(); push.mockRestore() }
})

it.each(['completed', 'pending'] as const)('本地导航恢复断网：管理端%s记录在切回、卸载后仍保留且不重放', async status => {
  const wrapper = mountCopilot()
  await openCopilot(wrapper)
  await flushSessionRestore()
  const vm = wrapper.vm as any
  const sessionId = 'admin-offline-navigation', key = vm.chatStorageKey as string
  const localAction = { id: 'offline-navigation', kind: 'navigation' as const, status, route: '/admin/audit' }
  saveAgentChatSnapshot(sessionId, {
    messages: [{ role: 'user', content: '打开管理端审计', localAction },
      { role: 'assistant', content: status === 'completed' ? '已打开「管理端审计」。' : '正在打开「管理端审计」…', localAction }],
    runStatus: null, updatedAt: Date.now(),
  }, key)
  sessionApi.get.mockRejectedValue(new Error('会话读取暂时失败'))
  try {
    await vm.handleSessionSelect(sessionId)
    await flushSessionRestore()
    expect(wrapper.text()).toContain(status === 'completed' ? '已打开「管理端审计」。' : '尚未确认')
    expect(streams.start).not.toHaveBeenCalled()
  } finally { wrapper.unmount() }
  expect(loadAgentChatSnapshot(sessionId, key)?.messages.filter(message => message.localAction).map(message => message.localAction?.status)).toEqual([status, status])
})

it.each(['completed', 'cancelled'] as const)('本地导航恢复顺序：%s旧运行步骤与结论保持在导航之前且不重复', async status => {
  const wrapper = mountCopilot()
  await openCopilot(wrapper)
  await flushSessionRestore()
  const vm = wrapper.vm as any
  const sessionId = vm.sessionId as string
  const conclusion = status === 'cancelled' ? '已停止任务，保留已有结果。' : '系统检查完成。'
  const serverMessages = [{ role: 'user' as const, content: '检查系统' }, { role: 'assistant' as const, content: conclusion }]
  const localAction = { id: 'navigation-after-run', kind: 'navigation' as const, status: 'completed' as const }
  saveAgentChatSnapshot(sessionId, {
    messages: [...serverMessages, { role: 'user', content: '打开审计', localAction },
      { role: 'assistant', content: '已打开「操作审计」。', localAction }],
    runStatus: status, updatedAt: Date.now(),
  }, vm.chatStorageKey)
  try {
    vm.applySessionSnapshot({
      surface: 'admin', session_id: sessionId,
      run: { run_id: 'old-run', status, output_text: conclusion, model: '', rounds: 1, error: '', updated_at: '' },
      messages: serverMessages, pending: null,
      events: [{ type: 'response.tool.completed', sequence_number: 1, call_id: 'old-call', tool_name: 'get_agent_team', output: {} }],
    })
    await nextTick()
    const rows = vm.messages.slice(1)
    expect(rows.map((row: any) => row.toolCalls?.length ? '旧运行步骤' : row.payload.content))
      .toEqual(['检查系统', '旧运行步骤', conclusion, '打开审计', '已打开「操作审计」。'])
    expect(wrapper.findAll('.message-row')).toHaveLength(6)
    expect(streams.start).not.toHaveBeenCalled()
  } finally { wrapper.unmount() }
})

it.each(['completed', 'running'] as const)('真实新建对话入口清空%s旧运行步骤，并忽略迟到恢复与流事件', async status => {
  sessionApi.get.mockImplementation(async (_surface, session_id) => ({
    surface: 'admin', session_id, run: null, messages: [], events: [], pending: null,
  }))
  const wrapper = mountCopilot()
  try {
    await openCopilot(wrapper)
    await flushSessionRestore()
    await wrapper.get('textarea').setValue('检查用户权限配置')
    void wrapper.get('.send-button').trigger('click')
    await flushPromises()
    const oldSessionId = streams.records[0].body.session_id as string
    const run = { run_id: 'old-team-388', status, model: 'model', rounds: 1, error: '', updated_at: '' }
    const event = { type: 'response.tool.completed', call_id: 'old-users-list', tool_name: 'users.list', output: { total: 1 } }
    emit(0, { type: 'response.created', response: { id: run.run_id, model: run.model } })
    emit(0, event)
    if (status === 'completed') {
      emit(0, { type: 'response.completed', response: { id: run.run_id } })
      await finish(0)
    }
    await flushPromises()
    expect(wrapper.get('.xl-steps-summary').text()).toContain('1 步完成')
    const vm = wrapper.vm as any
    const delayed = deferred<unknown>()
    sessionApi.get.mockReturnValueOnce(delayed.promise)
    const restoring = vm.restoreSession()
    await wrapper.get('.session-new').trigger('click')
    await flushSessionRestore()
    const newSessionId = vm.sessionId as string
    expect(newSessionId).not.toBe(oldSessionId)
    delayed.resolve({ surface: 'admin', session_id: oldSessionId, run,
      messages: [{ role: 'user', content: '检查用户权限配置' }], events: [event], pending: null })
    await restoring
    emit(0, event)
    emit(0, { type: 'response.completed', response: { id: run.run_id } })
    if (status === 'running') await finish(0)
    await flushSessionRestore()
    expect(vm.sessionId).toBe(newSessionId)
    expect(vm.sessionRun).toBeNull()
    expect(vm.messages).toHaveLength(1)
    expect(wrapper.text()).toContain('我是小菱，可以查看系统、处理审批、协作审查。')
    expect(wrapper.find('.xl-steps').exists()).toBe(false)
    expect(wrapper.text()).not.toContain('检查用户权限配置')
    expect(vm.lastActiveToolName).toBe('')
    expect(vm.loading).toBe(false)
    expect(vm.showTyping).toBe(false)
    expect(loadAgentChatSnapshot(newSessionId, vm.chatStorageKey)?.messages).toHaveLength(1)
    expect(streams.start).toHaveBeenCalledTimes(1)
  } finally { wrapper.unmount() }
})

it('新建对话立即清空旧运行计时、沙箱进度和会话详情状态', async () => {
  sessionApi.get.mockImplementation(async (surface, session_id) => ({
    surface, session_id, run: null, messages: [], events: [], pending: null,
  }))
  const wrapper = mountCopilot()
  try {
    await openCopilot(wrapper)
    await flushSessionRestore()
    await wrapper.get('textarea').setValue('检查用户权限配置')
    await wrapper.get('.send-button').trigger('click')
    await flushPromises()
    const vm = wrapper.vm as any
    const oldSessionId = vm.sessionId
    emit(0, { type: 'response.created', response: { id: 'old-admin-runtime', model: 'model' } })
    emit(0, { type: 'response.tool.started', call_id: 'old-users', tool_name: 'users.list', arguments: {} })
    vm.sandboxProgress = '旧会话正在部署'
    vm.openTeamPanel(388)
    vm.toggleTable('old-table')
    vm.thinkingCityOpen = true
    const activity = useAgentActivityStore()
    activity.begin('旧会话页面操作', 'old-page-action')
    activity.complete('old-page-action')
    await nextTick()
    expect(wrapper.find('.xl-steps').exists()).toBe(true)
    expect(vm.runningSinceMs).toEqual(expect.any(Number))
    expect(vm.sandboxProgress).toBe('旧会话正在部署')
    const delayed = deferred<unknown>()
    sessionApi.get.mockReturnValueOnce(delayed.promise)
    await wrapper.get('.session-new').trigger('click')
    await flushPromises()
    expect(vm.sessionId).not.toBe(oldSessionId)
    expect(vm.sessionRestoring).toBe(true)
    expect(wrapper.find('.xl-steps').exists()).toBe(false)
    expect(vm.messages).toHaveLength(1)
    expect(vm.runningSinceMs).toBeNull()
    expect(vm.sandboxProgress).toBe('')
    expect(vm.lastActiveToolName).toBe('')
    expect(vm.selectedTeamId).toBeNull()
    expect(vm.expandedTables.size).toBe(0)
    expect(vm.thinkingCityOpen).toBe(false)
    expect(activity.count).toBe(0)
    delayed.resolve({ surface: 'admin', session_id: vm.sessionId, run: null, messages: [], events: [], pending: null })
    await flushSessionRestore()
    emit(0, { type: 'response.tool.completed', call_id: 'late-old-call', tool_name: 'users.list', output: {} })
    await finish(0)
    expect(wrapper.find('.xl-steps').exists()).toBe(false)
    expect(vm.sessionRun).toBeNull()
    expect(streams.records[0].aborted).toBe(true)
    expect(streams.start).toHaveBeenCalledTimes(1)
  } finally { wrapper.unmount() }
})

it.each(['恢复快照', '流式事件'])('权威空恢复移除%s留下的旧两步完成卡，并清空运行进度', async source => {
  sessionApi.get.mockImplementation(async (surface, session_id) => ({
    surface, session_id, run: null, messages: [], events: [], pending: null,
  }))
  const wrapper = mountCopilot()
  try {
    await openCopilot(wrapper)
    await flushSessionRestore()
    const vm = wrapper.vm as any
    const sessionId = vm.sessionId
    const completedSnapshot = {
      surface: 'admin', session_id: sessionId,
      run: { run_id: 'old-admin-run', status: 'completed', model: 'model', rounds: 1, error: '', updated_at: '' },
      messages: [], pending: null,
      events: [
        { type: 'response.tool.completed', call_id: 'old-list', tool_name: 'users.list', output: {} },
        { type: 'response.tool.completed', call_id: 'old-team', tool_name: 'get_agent_team', output: {} },
      ],
    }
    if (source === '恢复快照') {
      sessionApi.get.mockResolvedValueOnce(completedSnapshot)
      await vm.restoreSession()
    } else {
      const running = vm.runResponse({ action: 'start', surface: 'admin', session_id: sessionId, messages: [] })
      emit(0, { type: 'response.created', response: { id: 'old-admin-run', model: 'model' } })
      for (const event of completedSnapshot.events) emit(0, event)
      emit(0, { type: 'response.completed', response: { id: 'old-admin-run' } })
      await finish(0)
      await running
    }
    await flushSessionRestore()
    expect(wrapper.get('.xl-steps-summary').text()).toContain('2 步完成')
    expect(wrapper.findAll('.message-bubble')).toHaveLength(1)
    vm.runningSinceMs = Date.now()
    vm.sandboxProgress = '旧运行进度'
    vm.lastActiveToolName = 'users.list'
    const activity = useAgentActivityStore()
    activity.begin('旧运行活动', 'old-empty-restore')
    // 上述旧运行已 completed，其活动按正常完成流程结束；活跃活动保留由下方 P1 用例覆盖。
    activity.complete('old-empty-restore', 0)
    await flushSessionRestore()
    await vm.restoreSession()
    await flushSessionRestore()
    expect(sessionApi.get).toHaveBeenLastCalledWith('admin', sessionId)
    expect(wrapper.find('.xl-steps').exists()).toBe(false)
    expect(vm.messages).toHaveLength(1)
    expect(wrapper.text()).toContain('我是小菱，可以查看系统、处理审批、协作审查。')
    expect(vm.sessionRun).toBeNull()
    expect(vm.runningSinceMs).toBeNull()
    expect(vm.sandboxProgress).toBe('')
    expect(vm.lastActiveToolName).toBe('')
    expect(activity.count).toBe(0)
    expect(loadAgentChatSnapshot(sessionId, vm.chatStorageKey)?.messages).toHaveLength(1)
    expect(streams.start).toHaveBeenCalledTimes(source === '流式事件' ? 1 : 0)
  } finally { wrapper.unmount() }
})

it('P1回归：权威空快照清理旧步骤和进度，同时保留其他 surface 的全局活动', async () => {
  sessionApi.get.mockImplementation(async (surface, session_id) => ({
    surface, session_id, run: null, messages: [], events: [], pending: null,
  }))
  const wrapper = mountCopilot()
  try {
    await openCopilot(wrapper)
    await flushSessionRestore()
    const vm = wrapper.vm as any
    sessionApi.get.mockResolvedValueOnce({
      surface: 'admin', session_id: vm.sessionId,
      run: { run_id: 'old-admin-steps', status: 'completed', model: 'model', rounds: 1, error: '', updated_at: '' },
      messages: [], pending: null,
      events: [
        { type: 'response.tool.completed', call_id: 'old-list', tool_name: 'users.list', output: {} },
        { type: 'response.tool.completed', call_id: 'old-team', tool_name: 'get_agent_team', output: {} },
      ],
    })
    await vm.restoreSession()
    await flushSessionRestore()
    expect(wrapper.get('.xl-steps-summary').text()).toContain('2 步完成')
    vm.runningSinceMs = Date.now()
    vm.sandboxProgress = '旧管理会话进度'
    vm.lastActiveToolName = 'users.list'
    const activity = useAgentActivityStore()
    const activityKey = activity.begin('用户端仍在执行页面操作', 'user-surface-live-tool', '/projects')
    const clear = vi.spyOn(activity, 'clear')

    await vm.restoreSession()
    await flushSessionRestore()

    expect(wrapper.find('.xl-steps').exists()).toBe(false)
    expect(vm.messages).toHaveLength(1)
    expect(vm.sessionRun).toBeNull()
    expect(vm.runningSinceMs).toBeNull()
    expect(vm.sandboxProgress).toBe('')
    expect(vm.lastActiveToolName).toBe('')
    expect(clear).not.toHaveBeenCalled()
    expect(activity.active.has(activityKey)).toBe(true)
    expect(activity.count).toBe(1)
    expect(streams.start).not.toHaveBeenCalled()
  } finally { wrapper.unmount() }
}, 15000)

it.each(['管理端当前会话', '收起管理面板后的用户端'] as const)(
  'P1回归：管理空会话后台轮询不取消%s的本地导航和持久化回执', async surface => {
    const originalPath = `${window.location.pathname}${window.location.search}${window.location.hash}`
    const router = createRouter({ history: createWebHistory(), routes: [
      { path: '/', component: {} },
      { path: '/admin/access', component: {}, meta: { title: '用户与权限中心', role: 'admin', roles: ['admin'] } },
      { path: '/projects', component: {}, meta: { title: '项目' } },
    ] })
    await router.push('/')
    sessionApi.get.mockImplementation(async (surface, session_id) => ({
      surface, session_id, run: null, messages: [], events: [], pending: null,
    }))
    const wrapper = mountCopilot([router])
    let drawer: VueWrapper | undefined
    let cursor: VueWrapper | undefined
    let target: HTMLButtonElement | undefined
    try {
      const user = useUserStore()
      user.profile = { id: 88, username: 'navigation-admin', role: 'admin', status: 1 }
      user.token = 'navigation-admin-token'
      await openCopilot(wrapper)
      await flushSessionRestore()
      const adminVm = wrapper.vm as any
      let sender = adminVm
      const crossSurface = surface === '收起管理面板后的用户端'
      const route = crossSurface ? '/projects' : '/admin/access'
      const label = crossSurface ? '项目' : '用户与权限'
      const pinia = getActivePinia()!
      if (crossSurface) {
        adminVm.closePanel()
        await nextTick()
        expect(wrapper.find('.copilot-panel').exists()).toBe(false)
        drawer = mount(AgentChatDrawer, {
          props: { visible: true, showLauncher: false },
          global: { plugins: [pinia, router], stubs: { 'el-icon': true } },
        })
        await flushSessionRestore()
        sender = drawer.vm as any
      }
      cursor = mount(VirtualCursor, { global: { plugins: [pinia], stubs: { Transition: false } } })
      target = document.createElement('button')
      target.dataset.route = route
      target.textContent = label
      target.getBoundingClientRect = () => ({ left: 20, top: 30, right: 140, bottom: 70, width: 120, height: 40 } as DOMRect)
      target.addEventListener('click', () => { void router.push(route) })
      document.body.appendChild(target)
      vi.useFakeTimers()
      // 保留双帧异步顺序，并将窗口 RAF 纳入同一个可控时钟。
      vi.spyOn(window, 'requestAnimationFrame').mockImplementation(callback => window.setTimeout(() => callback(performance.now()), 16))
      vi.spyOn(window, 'cancelAnimationFrame').mockImplementation(handle => window.clearTimeout(handle))
      const pendingPoll = deferred<unknown>()
      sessionApi.get.mockReturnValueOnce(pendingPoll.promise)
      adminVm.scheduleSessionPoll(true)
      await vi.advanceTimersByTimeAsync(0)
      expect(sessionApi.get).toHaveBeenLastCalledWith('admin', adminVm.sessionId, true)
      const push = vi.spyOn(router, 'push')
      const activity = useAgentActivityStore()
      const clear = vi.spyOn(activity, 'clear')
      sender.inputText = crossSurface ? '打开项目' : '打开用户与权限'
      const navigation = sender.sendMessage()
      await flushPromises()
      const navigationKey = activity.current?.key
      expect(navigationKey).toMatch(/^xiaoling-nav-/)
      expect(activity.isActing).toBe(true)
      expect(push).not.toHaveBeenCalled()
      expect(loadAgentChatSnapshot(sender.sessionId, sender.chatStorageKey)?.messages.at(-1)?.localAction?.status).toBe('pending')

      // 真实后台轮询回包落在虚拟鼠标接管后、实际菜单点击前。
      pendingPoll.resolve({ surface: 'admin', session_id: adminVm.sessionId,
        run: null, messages: [], events: [], pending: null })
      await flushPromises()

      expect(clear).not.toHaveBeenCalled()
      expect(activity.active.has(navigationKey!)).toBe(true)
      expect(push).not.toHaveBeenCalled()
      expect(loadAgentChatSnapshot(sender.sessionId, sender.chatStorageKey)?.messages.at(-1)?.localAction?.status).toBe('pending')
      // 双 RAF + 抵达 700ms + 点击 650ms + 路由确认 50ms。
      await vi.advanceTimersByTimeAsync(1600)
      await navigation
      await flushPromises()
      expect(router.currentRoute.value.path).toBe(route)
      expect(push).toHaveBeenCalledExactlyOnceWith(route)
      const saved = loadAgentChatSnapshot(sender.sessionId, sender.chatStorageKey)
      expect(saved?.messages.filter(message => message.localAction)).toHaveLength(2)
      expect(saved?.messages.filter(message => message.localAction).every(message => message.localAction?.status === 'completed')).toBe(true)
      expect(streams.start).not.toHaveBeenCalled()
    } finally {
      cursor?.unmount()
      drawer?.unmount()
      wrapper.unmount()
      target?.remove()
      router.options.history.destroy()
      window.history.replaceState({}, '', originalPath)
    }
  }, 15000,
)

function flushSessionRestore(): Promise<void> {
  return new Promise((resolve) => {
    setTimeout(() => {
      void flushPromises().then(() => setTimeout(resolve, 0))
    }, 0)
  })
}

function mountCopilot(extraPlugins: Plugin[] = []): VueWrapper {
  const pinia = createPinia()
  setActivePinia(pinia)
  const wrapper = mount(AdminCopilot, {
    global: {
      plugins: [pinia, ...extraPlugins],
      stubs: {
        'el-icon': { template: '<span class="el-icon-stub"><slot /></span>' },
        ChatDotRound: true,
        Close: true,
        DocumentCopy: true,
        Promotion: true,
        WarningFilled: true,
      },
    },
  })
  mountedCopilots.add(wrapper)
  const unmount = wrapper.unmount.bind(wrapper)
  wrapper.unmount = () => { mountedCopilots.delete(wrapper); unmount() }
  return wrapper
}

function deferred<T>() {
  let resolve!: (value: T) => void
  const promise = new Promise<T>((done) => { resolve = done })
  return { promise, resolve }
}

async function openCopilot(wrapper: VueWrapper): Promise<void> {
  const trigger = document.querySelector('.copilot-trigger') as HTMLButtonElement | null
  if (trigger) trigger.click()
  else await wrapper.find('.copilot-trigger').trigger('click')
  await flushPromises()
  expect(wrapper.find('.copilot-panel').exists()).toBe(true)
}

function emit(index: number, event: Record<string, unknown>): void {
  streams.records[index].onEvent(event)
}

async function finish(index: number): Promise<void> {
  streams.records[index].resolve()
  await flushPromises()
}

/** 整块调用链默认折叠,断言前先展开。 */
/** 调用链默认折叠;需要断言展开详情的用例自行点击 .xl-step-line。 */
async function expandTimeline(_wrapper: VueWrapper): Promise<void> {
  // no-op:保留给历史用例的兼容入口
}

beforeEach(() => {
  window.localStorage.clear()
  window.sessionStorage.clear()
  meshApi.heartbeat.mockReset().mockResolvedValue({})
  meshApi.inbox.mockReset().mockResolvedValue([])
  meshApi.list.mockReset().mockResolvedValue({ items: [], total: 0, by_kind: {} })
  meshApi.conversations.mockReset().mockRejectedValue(new Error('会话目录未配置'))
  meshApi.acknowledge.mockReset().mockResolvedValue({ message_id: 'terminal', status: 'completed' })
  teamApi.list.mockReset().mockResolvedValue({ items: [], total: 0 })
  teamApi.detail.mockReset()
  teamApi.events.mockReset().mockResolvedValue({
    items: [], has_more: false, next_after_id: 0, page_size: 200, team_status: 'running',
  })
  sessionApi.get.mockReset()
  sessionApi.page.mockReset()
  sessionApi.cancel.mockReset().mockResolvedValue({ run_id: 'run-admin-active', status: 'cancelled', output_text: '', error: '' })
  sessionApi.get.mockResolvedValue({
    surface: 'admin', session_id: 'admin-test', run: null, messages: [], pending: null,
  })
  streams.start.mockReset()
  streams.records.splice(0)
  streams.start.mockImplementation((
    body: Record<string, unknown>,
    options: { onEvent: (event: unknown) => void },
  ) => {
    const controller = new AbortController()
    let resolve = (): void => undefined
    let reject = (_error: unknown): void => undefined
    const done = new Promise<void>((doneResolve, doneReject) => { resolve = doneResolve; reject = doneReject })
    done.catch(() => undefined)
    const record = { body, onEvent: options.onEvent, resolve, reject, aborted: false }
    streams.records.push(record)
    return {
      abort: () => {
        record.aborted = true
        controller.abort()
      },
      signal: controller.signal,
      done,
    }
  })
})

afterEach(async () => {
  // 部分流式用例没有显式卸载；其 Teleport 入口和全局监听会截获后续用例的点击。
  for (const wrapper of mountedCopilots) wrapper.unmount()
  await flushPromises()
  expect(document.querySelector('.copilot-trigger')).toBeNull()
  vi.useRealTimers()
})

describe('团队轮询复核', () => {
  const terminalStatuses: AgentTeamStatus[] = ['completed', 'failed', 'cancelled', 'expired']
  function fixture(teamId: number, status: AgentTeamStatus): AgentTeamDetail {
    return {
      team_id: teamId, title: `持久团队 ${teamId}`, surface: 'admin', session_id: 'admin-test',
      status, max_active_children: 3, trace_id: `trace-${teamId}`,
      updated_at: '2026-10-14T05:00:00Z',
      counts: { total: 1, completed: status === 'completed' ? 1 : 0, running: 0, queued: 0, failed: status === 'failed' ? 1 : 0, blocked: 0 },
      members: [], tasks: [], events: [], messages: [],
    }
  }

  it.each([1, 2, 3])('第 %i 轮：16 个已终态管理团队恢复后不再周期拉完整详情', async () => {
    vi.useFakeTimers()
    const teams = Array.from({ length: 16 }, (_, index) => fixture(index + 1, terminalStatuses[index % 4]))
    teamApi.list.mockResolvedValue({ items: teams, total: teams.length })
    teamApi.detail.mockImplementation((id: number) => Promise.resolve(teams.find(team => team.team_id === id)))
    const wrapper = mountCopilot()
    try {
      await openCopilot(wrapper)
      await vi.advanceTimersByTimeAsync(0)
      await flushPromises()
      expect((wrapper.vm as any).agentTeams).toHaveLength(16)
      expect(teamApi.detail).toHaveBeenCalledTimes(16)
      teamApi.detail.mockClear()
      await vi.advanceTimersByTimeAsync(9000)
      expect(teamApi.list.mock.calls.length).toBeGreaterThan(1)
      expect(teamApi.detail).not.toHaveBeenCalled()
      expect((wrapper.vm as any).agentTeams).toHaveLength(16)
      expect(teamApi.events).not.toHaveBeenCalled()
    } finally {
      wrapper.unmount()
    }
  })

  it('活动状态、首次读取失败与终态转换仍同步，稳定终态结果复用', async () => {
    let teams = terminalStatuses.map((status, index) => fixture(index + 1, status))
    teamApi.list.mockImplementation(() => Promise.resolve({ items: teams, total: teams.length }))
    teamApi.detail.mockImplementation((id: number) => Promise.resolve(teams.find(team => team.team_id === id)))
    const wrapper = mountCopilot()
    try {
      const vm = wrapper.vm as any
      vm.sessionId = 'admin-test'
      await vm.refreshAgentTeam()
      teams = [...teams, fixture(5, 'completed'), fixture(6, 'running'), fixture(7, 'verifying'), fixture(8, 'queued'), fixture(9, 'draft')]
      teamApi.detail.mockImplementation((id: number) => id === 5
        ? Promise.reject(new Error('temporary detail outage'))
        : Promise.resolve(teams.find(team => team.team_id === id)))
      await vm.refreshAgentTeam()
      teamApi.detail.mockClear()
      await vm.refreshAgentTeam()
      expect(teamApi.detail.mock.calls.map(([id]) => id)).toEqual([5, 6, 7, 8, 9])
      expect(vm.agentTeams.map((team: AgentTeamDetail) => team.team_id)).toEqual([1, 2, 3, 4, 6, 7, 8, 9])
      teams[5] = fixture(6, 'failed')
      teamApi.detail.mockImplementation((id: number) => Promise.resolve(teams.find(team => team.team_id === id)))
      await vm.refreshAgentTeam()
      expect(vm.agentTeams.find((team: AgentTeamDetail) => team.team_id === 6).status).toBe('failed')
      teamApi.detail.mockClear()
      await vm.refreshAgentTeam()
      expect(teamApi.detail.mock.calls.map(([id]) => id)).toEqual([7, 8, 9])
    } finally {
      wrapper.unmount()
    }
  })

  it('终态团队被重试及版本更新后重新拉取，未变版本不会重复读取', async () => {
    let team = fixture(51, 'completed')
    teamApi.list.mockImplementation(() => Promise.resolve({ items: [team], total: 1 }))
    teamApi.detail.mockImplementation(() => Promise.resolve(team))
    const wrapper = mountCopilot()
    try {
      const vm = wrapper.vm as any
      vm.sessionId = 'admin-test'
      await vm.refreshAgentTeam()
      teamApi.detail.mockClear()
      team = { ...team, updated_at: '2026-10-14T05:01:00Z', summary: { evidence: '更新的管理结果' } }
      await vm.refreshAgentTeam()
      await vm.refreshAgentTeam()
      expect(teamApi.detail).toHaveBeenCalledTimes(1)
      expect(vm.agentTeams[0].summary.evidence).toBe('更新的管理结果')
      team = { ...team, status: 'queued' }
      await vm.refreshAgentTeam()
      expect(vm.agentTeams[0].status).toBe('queued')
      expect(vm.activeThinkingTeamIds).toEqual([51])
    } finally {
      wrapper.unmount()
    }
  })

  it('打开终态管理团队侧面板仍取详情，后台轮询不拉其他终态', async () => {
    const teams = [fixture(61, 'completed'), fixture(62, 'failed')]
    teamApi.list.mockResolvedValue({ items: teams, total: teams.length })
    teamApi.detail.mockImplementation((id: number) => Promise.resolve(teams.find(team => team.team_id === id)))
    const wrapper = mountCopilot()
    try {
      await openCopilot(wrapper)
      const vm = wrapper.vm as any
      teamApi.detail.mockClear()
      vm.openTeamPanel(61)
      await flushPromises()
      expect(teamApi.detail.mock.calls.map(([id]) => id)).toEqual([61])
      await vm.refreshAgentTeam()
      expect(teamApi.detail.mock.calls.map(([id]) => id)).toEqual([61])
      vm.selectedTeamId = null
      await flushPromises()
      vm.openTeamPanel(61)
      await flushPromises()
      expect(teamApi.detail.mock.calls.map(([id]) => id)).toEqual([61, 61])
    } finally {
      wrapper.unmount()
    }
  })
})

it('管理端按游标加载更早消息，不把当前页误当全部历史', async () => {
  sessionApi.get.mockImplementation((_surface: string, id: string) => Promise.resolve({
    surface: 'admin', session_id: id, run: null, pending: null,
    messages: [{ role: 'user', content: '最近的问题' }],
    history_page: { total: 2, oldest_message_index: 1, has_more: true },
  }))
  sessionApi.page.mockResolvedValue({
    messages: [{ role: 'user', content: '更早的问题' }],
    total: 2, oldest_message_index: 0, has_more: false,
  })
  const wrapper = mountCopilot()
  await openCopilot(wrapper)
  await flushSessionRestore()
  expect(wrapper.find('.history-load-button').exists()).toBe(true)
  await wrapper.find('.history-load-button').trigger('click')
  await flushPromises()
  expect(sessionApi.page).toHaveBeenCalledWith('admin', expect.any(String), 1)
  expect(wrapper.findAll('.message-row.is-user')).toHaveLength(2)
  expect(wrapper.find('.copilot-messages').text()).toContain('更早的问题')
  expect(wrapper.find('.history-load-button').exists()).toBe(false)
  wrapper.unmount()
})

it.each([
  { width: 390, path: '/admin/overview' },
  { width: 1280, path: '/admin/operations' },
  { width: 1280, path: '/admin/security-center' },
])('$width px从非管理页切入管理页时先保留入口，页头槽位挂载后自动迁移', async ({ width, path }) => {
  const originalWidth = window.innerWidth
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: '/projects', component: { template: '<main />' } },
      { path: '/admin/:pathMatch(.*)*', component: { template: '<main />' } },
    ],
  })
  const teleportWarnings: string[] = []
  const warn = vi.spyOn(console, 'warn').mockImplementation((...args) => {
    const message = args.map(String).join(' ')
    if (message.includes('Teleport')) teleportWarnings.push(message)
  })
  let wrapper: VueWrapper | undefined
  let slot: HTMLDivElement | undefined

  try {
    await router.push('/projects')
    Object.defineProperty(window, 'innerWidth', { configurable: true, value: width })
    wrapper = mount(AdminCopilot, {
      attachTo: document.body,
      global: {
        plugins: [createPinia(), router],
        stubs: {
          'el-icon': { template: '<span class="el-icon-stub"><slot /></span>' },
          ChatDotRound: true,
          Close: true,
          DocumentCopy: true,
          Promotion: true,
          WarningFilled: true,
        },
      },
    })
    await flushPromises()
    expect(document.body.querySelector('.copilot-trigger')?.classList.contains('is-floating-trigger')).toBe(true)

    await router.push(path)
    await flushPromises()
    expect(document.body.querySelector('.copilot-trigger')?.classList.contains('is-floating-trigger')).toBe(true)

    slot = document.createElement('div')
    slot.id = 'admin-copilot-trigger-slot'
    document.body.append(slot)
    await new Promise((resolve) => setTimeout(resolve, 0))
    await nextTick()

    expect(slot.querySelector('.copilot-trigger')?.classList.contains('is-header-trigger')).toBe(true)
    expect(document.body.querySelector(':scope > .copilot-trigger')).toBeNull()
    expect(teleportWarnings).toEqual([])
  } finally {
    wrapper?.unmount()
    slot?.remove()
    Object.defineProperty(window, 'innerWidth', { configurable: true, value: originalWidth })
    warn.mockRestore()
  }
})

describe('安全中心小菱页头入口', () => {
  async function mountSecurityCenter(width: number) {
    const originalWidth = window.innerWidth
    Object.defineProperty(window, 'innerWidth', { configurable: true, value: width })
    const pinia = createPinia()
    const router = createRouter({
      history: createMemoryHistory(),
      routes: [{ path: '/admin/:pathMatch(.*)*', component: { template: '<main />' } }],
    })
    await router.push('/admin/security-center')
    const layout = mount(AdminLayout, {
      attachTo: document.body,
      global: {
        plugins: [pinia, router],
        stubs: {
          ProactivePageGuide: true,
          UserAvatar: true,
          'el-dropdown': { template: '<div><slot /></div>' },
          'el-dropdown-menu': true,
          'el-dropdown-item': true,
          'el-icon': { template: '<span><slot /></span>' },
        },
      },
    })
    layout.get<HTMLElement>('.admin-content').element.scrollTo = vi.fn()
    const copilot = mount(AdminCopilot, {
      attachTo: document.body,
      global: { plugins: [pinia, router], stubs: { 'el-icon': { template: '<span><slot /></span>' } } },
    })
    await flushPromises()
    return {
      layout,
      copilot,
      router,
      cleanup() {
        copilot.unmount()
        layout.unmount()
        Object.defineProperty(window, 'innerWidth', { configurable: true, value: originalWidth })
      },
    }
  }

  it.each([1280, 1440, 768, 375, 320])('%dpx安全中心入口挂在页头，可打开聊天并继续使用快捷键', async (width) => {
    const fixture = await mountSecurityCenter(width)
    try {
      const slot = fixture.layout.get('#admin-copilot-trigger-slot')
      const trigger = slot.element.querySelector<HTMLButtonElement>('.copilot-trigger')
      expect(trigger).not.toBeNull()
      expect(document.body.querySelector(':scope > .copilot-trigger')).toBeNull()
      expect(trigger!.classList.contains('is-header-trigger')).toBe(true)
      expect(trigger!.type).toBe('button')
      expect(trigger!.getAttribute('aria-label')).toBe('打开小菱 · 管理助手')
      trigger!.focus()
      expect(document.activeElement).toBe(trigger)
      trigger!.click()
      await flushSessionRestore()
      expect(fixture.copilot.find('[role="dialog"]').exists()).toBe(true)
      await fixture.copilot.get('textarea').setValue('只保留草稿，不发送')

      document.body.dispatchEvent(new KeyboardEvent('keydown', { key: '/', bubbles: true, cancelable: true }))
      expect(document.activeElement).toBe(fixture.copilot.get('textarea').element)
      window.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' }))
      await flushPromises()
      expect(fixture.copilot.find('[role="dialog"]').exists()).toBe(false)
      expect(slot.element.querySelector('.copilot-trigger')).not.toBeNull()

      await openCopilot(fixture.copilot)
      await flushSessionRestore()
      expect(fixture.copilot.get<HTMLTextAreaElement>('textarea').element.value).toBe('只保留草稿，不发送')
      expect(streams.start).not.toHaveBeenCalled()
    } finally {
      fixture.cleanup()
    }
  })

  it('桌面在管理页间切换保持页头入口，保留运行状态和未读回复', async () => {
    const fixture = await mountSecurityCenter(1280)
    try {
      await openCopilot(fixture.copilot)
      await flushSessionRestore()
      await fixture.copilot.get('textarea').setValue('查看当前状态')
      void fixture.copilot.get('.send-button').trigger('click')
      await flushPromises()
      emit(0, { type: 'response.created', response: { id: 'header-route-run' } })
      window.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' }))
      await flushPromises()
      emit(0, { type: 'response.output_text.delta', delta: '后台运行回复仍保留' })
      await flushPromises()

      const slot = fixture.layout.get('#admin-copilot-trigger-slot')
      expect(slot.find('.copilot-trigger.is-busy .unread-dot').exists()).toBe(true)
      await fixture.router.push('/admin/operations')
      await flushPromises()
      expect(slot.find('.copilot-trigger.is-busy .unread-dot').exists()).toBe(true)
      expect(document.body.querySelector(':scope > .copilot-trigger')).toBeNull()
      expect(streams.records[0].aborted).toBe(false)

      await fixture.router.push('/admin/security-center')
      await flushPromises()
      expect(slot.find('.copilot-trigger.is-busy .unread-dot').exists()).toBe(true)
      expect(document.body.querySelector(':scope > .copilot-trigger')).toBeNull()
      await openCopilot(fixture.copilot)
      expect(fixture.copilot.text()).toContain('后台运行回复仍保留')
      expect(slot.find('.unread-dot').exists()).toBe(false)
      emit(0, { type: 'response.completed', response: { id: 'header-route-run' } })
      await finish(0)
    } finally {
      fixture.cleanup()
    }
  })
})

describe('AdminCopilot Responses stream', () => {
  it('审批详情引导回原始管理员会话时恢复服务端验证的指定会话', async () => {
    meshApi.conversations.mockResolvedValue({
      items: [{
        kind: 'session', session_id: 'admin-approval-session', surface: 'admin',
        status: 'active', title: '全局模型配置审批', active_run_status: 'waiting_approval',
      }],
      total: 1,
    })
    sessionApi.get.mockImplementation(async (_surface: string, sessionId: string) => ({
      surface: 'admin', session_id: sessionId, run: null,
      messages: [{ role: 'user', content: '核对模型配置申请' }], pending: null,
    }))
    const wrapper = mountCopilot()
    await flushPromises()

    window.dispatchEvent(new CustomEvent('prism:open-admin-copilot', {
      detail: { sessionId: 'admin-approval-session' },
    }))
    await flushSessionRestore()

    expect(sessionApi.get.mock.calls.some(([surface, sessionId]) => (
      surface === 'admin' && sessionId === 'admin-approval-session'
    ))).toBe(true)
    expect(meshApi.conversations).toHaveBeenCalledWith(expect.objectContaining({ surface: 'admin', limit: 100 }))
    expect(wrapper.text()).toContain('核对模型配置申请')
    expect(streams.start).not.toHaveBeenCalled()
    wrapper.unmount()
  })

  it.each([true, false])('取消晚到completed时按真实助手payload判重（已有最终回复=%s）', async (hasAssistantReply) => {
    const wrapper = mountCopilot()
    await openCopilot(wrapper)
    await flushSessionRestore()
    await wrapper.find('textarea').setValue('唯一最终答案')
    void wrapper.find('.send-button').trigger('click')
    await flushPromises()
    emit(0, { type: 'response.created', response: { id: 'run-admin-dedupe', model: 'deepseek-v4-pro' } })
    if (hasAssistantReply) emit(0, { type: 'response.output_text.delta', delta: '唯一最终答案' })
    await flushPromises()
    sessionApi.cancel.mockResolvedValueOnce({ run_id: 'run-admin-dedupe', status: 'completed', output_text: '唯一最终答案', error: '' })
    await wrapper.find('.stop-button').trigger('click')
    await flushPromises()
    ;(document.querySelector('.cancel-confirm-stop') as HTMLButtonElement).click()
    await flushPromises()
    const matches = wrapper.findAll('.message-row.is-assistant').filter((row) => row.text().includes('唯一最终答案'))
    expect(matches).toHaveLength(1)
    expect(wrapper.text()).toContain('任务已完成，无需停止')
    expect(wrapper.text()).not.toContain('已停止任务')
    wrapper.unmount()
  })

  it.each(['completed', 'failed', 'cancelled'])('停止返回%s时按服务端实态反馈', async (status) => {
    const wrapper = mountCopilot()
    await openCopilot(wrapper)
    await flushSessionRestore()
    await wrapper.find('textarea').setValue('检查运行状态')
    void wrapper.find('.send-button').trigger('click')
    await flushPromises()
    emit(0, { type: 'response.created', response: { id: 'run-admin-late', model: 'deepseek-v4-pro' } })
    await flushPromises()
    sessionApi.cancel.mockResolvedValueOnce({ run_id: 'run-admin-late', status, output_text: '最终答案', error: '执行失败原因' })
    await wrapper.find('.stop-button').trigger('click')
    await flushPromises()
    const confirm = document.querySelector('.cancel-confirm-stop') as HTMLButtonElement
    confirm.click()
    await flushPromises()
    expect(wrapper.text()).toContain(status === 'completed' ? '任务已完成，无需停止' : status === 'failed' ? '任务已失败' : '已停止任务')
    if (status !== 'cancelled') {
      expect(wrapper.text()).not.toContain('已停止任务')
      expect(wrapper.text()).not.toContain('已停止本次回答')
    }
    expect(wrapper.find('.stop-button').exists()).toBe(false)
    expect(sessionApi.cancel).toHaveBeenCalledOnce()
    wrapper.unmount()
  })

  it('管理端停止请求处理中收到关联团队回执时不会启动第二次模型运行', async () => {
    const wrapper = mountCopilot()
    await openCopilot(wrapper)
    await flushSessionRestore()
    await wrapper.find('textarea').setValue('管理端查询后停止')
    void wrapper.find('.send-button').trigger('click')
    await flushPromises()
    emit(0, { type: 'response.created', response: { id: 'run-admin-stop-before-receipt', model: 'deepseek-v4-pro' } })
    await flushPromises()

    const cancellation = deferred<{ run_id: string; status: string; error: string; output_text: string }>()
    sessionApi.cancel.mockReturnValueOnce(cancellation.promise)
    await wrapper.find('.stop-button').trigger('click')
    await flushPromises()
    const confirm = document.querySelector('.cancel-confirm-stop') as HTMLButtonElement
    confirm.click()
    await flushPromises()
    const currentSessionId = streams.records[0].body.session_id as string

    const receipt = {
      schema_version: '1.0', message_id: 'admin-late-team-result-after-stop', idempotency_key: 'admin-late-team-result-after-stop',
      trace_id: 'admin-team-trace-after-stop', correlation_id: 'team:89', causation_id: '',
      sent_from: 'system:agent_team', send_to: `session:admin:${currentSessionId}`, message_type: 'task.result',
      priority: 'normal', subject: '管理端团队任务完成', status: 'delivered', payload: { team_id: 89 },
      context: { team_id: 89, team_terminal: true, session_delivery: true, parent_run_id: 'run-admin-stop-before-receipt' },
      artifacts: [], errors: [], requires_ack: true, max_attempts: 3, attempt_count: 1,
      expires_at: '', create_time: '', update_time: '',
    }
    const vm = wrapper.vm as any
    expect(await vm.handleMeshMessage(receipt, currentSessionId)).toBe(false)
    expect(streams.records).toHaveLength(1)

    cancellation.resolve({
      run_id: 'run-admin-stop-before-receipt', status: 'cancelled', error: '', output_text: '',
    })
    await flushPromises()
    await flushPromises()
    expect(streams.records).toHaveLength(1)
    expect(wrapper.text()).toContain('已停止任务，未执行剩余操作；如需继续可重新发起。')
    wrapper.unmount()
  })

  it('停止请求未确认时中断本地响应并阻止关联团队回执自动续跑', async () => {
    const wrapper = mountCopilot()
    await openCopilot(wrapper)
    await flushSessionRestore()
    await wrapper.find('textarea').setValue('继续执行生产检查')
    void wrapper.find('.send-button').trigger('click')
    await flushPromises()
    emit(0, { type: 'response.created', response: { id: 'run-admin-active', model: 'deepseek-v4-pro' } })
    await flushPromises()
    sessionApi.cancel.mockRejectedValueOnce({
      message: '执行器连接中断', request_id: 'trace-admin-cancel', retryable: true,
    })

    await wrapper.find('.stop-button').trigger('click')
    await flushPromises()
    const confirm = document.querySelector('.cancel-confirm-stop') as HTMLButtonElement | null
    expect(confirm).not.toBeNull()
    confirm!.click()
    await flushPromises()

    expect(streams.records[0].aborted).toBe(true)
    expect(wrapper.find('.stop-button').exists()).toBe(true)
    expect(messages.error).toHaveBeenCalledWith('停止请求尚未确认，已阻止关联团队回执自动续跑。请查看任务状态：执行器连接中断')
    expect(wrapper.find('.copilot-error-card').text()).toContain('已阻止关联团队回执自动续跑')
    expect(wrapper.find('.copilot-error-card').text()).toContain('trace-admin-cancel')
    expect(wrapper.text()).not.toContain('已停止任务，未执行剩余操作')

    const currentSessionId = streams.records[0].body.session_id as string
    const receipt = {
      schema_version: '1.0', message_id: 'admin-late-result-after-unconfirmed-stop',
      idempotency_key: 'admin-late-result-after-unconfirmed-stop', trace_id: 'trace-admin-late-result',
      correlation_id: 'team:90', causation_id: '', sent_from: 'system:agent_team',
      send_to: `session:admin:${currentSessionId}`, message_type: 'task.result', priority: 'normal',
      subject: '停止后迟到的团队回执', status: 'delivered', payload: { team_id: 90 },
      context: { team_id: 90, team_terminal: true, session_delivery: true, parent_run_id: 'run-admin-active' },
      artifacts: [], errors: [], requires_ack: true, max_attempts: 3, attempt_count: 1,
      expires_at: '', create_time: '', update_time: '',
    }
    const vm = wrapper.vm as any
    expect(await vm.handleMeshMessage(receipt, currentSessionId)).toBe(true)
    expect(streams.records).toHaveLength(1)
    expect(meshApi.acknowledge).toHaveBeenCalledWith(
      'admin', currentSessionId, receipt.message_id, 'completed',
      '停止请求尚未确认；团队回执仅保留审计，不触发新的模型运行',
    )

    const backgroundReceipt = {
      ...receipt,
      message_id: 'admin-old-session-result-after-unconfirmed-stop',
      idempotency_key: 'admin-old-session-result-after-unconfirmed-stop',
      send_to: 'session:admin:previous-admin-session',
    }
    expect(await vm.handleMeshMessage(backgroundReceipt, 'previous-admin-session')).toBe(true)
    expect(streams.records).toHaveLength(1)
    expect(meshApi.acknowledge).toHaveBeenCalledWith(
      'admin', 'previous-admin-session', backgroundReceipt.message_id, 'completed',
      '停止请求尚未确认；团队回执仅保留审计，不触发新的模型运行',
    )
    wrapper.unmount()
  })

  it('创建团队后主响应结束也持续展示真实子 Agent 协作城市', async () => {
    const detail = {
      team_id: 88, title: '管理端实时协作', surface: 'admin', session_id: 'admin-test', status: 'running',
      max_active_children: 3, trace_id: 'trace-admin-88',
      counts: { total: 1, completed: 0, running: 1, queued: 0, failed: 0, blocked: 0 },
      members: [{ member_id: 1, member_key: 'ops', display_name: '运维检查', address: 'agent:operations', kind: 'registered', role: 'worker', status: 'running' }],
      tasks: [{ task_id: 1, task_key: 'ops-read', member_id: 1, title: '读取健康状态', depends_on: [], status: 'running', priority: 1, attempt_count: 1, max_attempts: 1 }], events: [], messages: [],
    }
    teamApi.detail.mockResolvedValue(detail)
    teamApi.list.mockResolvedValue({ items: [detail], total: 1 })
    const wrapper = mountCopilot()
    await openCopilot(wrapper)
    await flushSessionRestore()

    await wrapper.find('textarea').setValue('请创建子 Agent 团队')
    void wrapper.find('.send-button').trigger('click')
    await flushPromises()
    emit(0, { type: 'response.created', response: { id: 'run-admin-team', model: 'deepseek-v4-pro' } })
    emit(0, { type: 'response.tool.completed', call_id: 'call-admin-team', tool_name: 'create_agent_team', output: { team_id: 88 } })
    await flushPromises()

    const graph = wrapper.get('[aria-label="实时团队星图"]')
    expect(graph.text()).toContain('1 个子 Agent · 1 个执行中')
    expect(graph.text()).toContain('读取健康状态')
    expect(graph.element.closest('details')).toBeNull()
    expect(wrapper.find('.thinking-city').exists()).toBe(false)
    await wrapper.get('.thinking-city-toggle').trigger('click')
    expect(wrapper.findComponent({ name: 'ThinkingCity' }).props('animated')).toBe(false)
    expect(wrapper.find('.thinking-city').exists()).toBe(true)
    expect(wrapper.text()).toContain('子 Agent 正在协作')
    wrapper.unmount()
  })

  it('后台历史会话完成 Mesh 任务后不抢占当前管理新对话', async () => {
    window.localStorage.setItem('prism-agent-sessions:admin', JSON.stringify([
      { id: 'admin-current', title: '新对话', createdAt: 2 },
      { id: 'admin-history', title: '历史运维', createdAt: 1 },
    ]))
    window.localStorage.setItem('prism-agent-active-session:admin', 'admin-current')
    meshApi.list.mockResolvedValue({
      items: [
        { kind: 'session', session_id: 'admin-current', surface: 'admin', name: '新对话', last_seen_at: '2026-08-24T00:00:00Z' },
        { kind: 'session', session_id: 'admin-history', surface: 'admin', name: '历史运维', last_seen_at: '2026-08-23T00:00:00Z' },
      ],
      total: 2,
      by_kind: { session: 2 },
    })
    meshApi.inbox.mockImplementation((_surface: string, sessionId: string) => Promise.resolve(
      sessionId === 'admin-history'
        ? [{
            schema_version: '1.0', message_id: 'msg-admin-background', idempotency_key: 'idem-admin-background',
            trace_id: 'trace-admin-background', correlation_id: 'corr-admin-background', causation_id: '',
            sent_from: 'agent:ops', send_to: 'session:admin:admin-history', message_type: 'task.result',
            priority: 'normal', subject: '后台运维完成', status: 'delivered', payload: {}, context: {},
            artifacts: [], errors: [], requires_ack: true, max_attempts: 3, attempt_count: 1,
            expires_at: '2026-08-24T01:00:00Z', create_time: '2026-08-24T00:00:00Z', update_time: '2026-08-24T00:00:01Z',
          }]
        : [],
    ))

    const wrapper = mountCopilot()
    await openCopilot(wrapper)
    await flushSessionRestore()
    await flushPromises()
    expect(streams.records).toHaveLength(1)
    expect(streams.records[0].body.session_id).toBe('admin-history')
    expect(wrapper.find('.session-current').text()).toContain('新对话')

    emit(0, { type: 'response.completed', response: { id: 'run-admin-background' } })
    await finish(0)
    expect(wrapper.find('.session-current').text()).toContain('新对话')
    wrapper.unmount()
  })

  it('管理端也会在团队终态汇总模型失败时自动展示子 Agent 部分结果', async () => {
    const receipt = {
      schema_version: '1.0', message_id: 'team-89-terminal-1-result', idempotency_key: 'team-89-terminal-1-result',
      trace_id: 'trace-89', correlation_id: 'team:89', causation_id: 'request-89',
      sent_from: 'system:agent_team', send_to: 'session:admin:admin-test', message_type: 'task.error',
      priority: 'normal', subject: '运维审查团队终态', status: 'delivered', payload: { team_id: 89 },
      context: { team_id: 89, team_terminal: true, session_delivery: true }, artifacts: [], errors: [],
      requires_ack: true, max_attempts: 3, attempt_count: 1,
      expires_at: '2026-10-15T13:00:00Z', create_time: '2026-10-15T12:00:00Z', update_time: '2026-10-15T12:00:01Z',
    }
    let deliveredSessionId = ''
    meshApi.inbox.mockImplementation((_surface: string, sessionId: string) => {
      deliveredSessionId = sessionId
      return Promise.resolve([{ ...receipt, send_to: `session:admin:${sessionId}` }])
    })
    const teamDetail: AgentTeamDetail = {
      team_id: 89, title: '管理端自动审查', surface: 'admin', session_id: 'admin-test', status: 'completed',
      max_active_children: 3, trace_id: 'trace-89',
      counts: { total: 1, completed: 1, running: 0, queued: 0, failed: 0, blocked: 0 },
      summary: { final_result: { summary: '代码质量 Agent 保存一项证据。' } },
      members: [], events: [], messages: [],
      tasks: [{
        task_id: 1, task_key: 'review', member_id: 1, member_key: 'reviewer', title: '只读审查',
        depends_on: [], status: 'completed', priority: 0, attempt_count: 1, max_attempts: 3,
        result: { summary: '发现风险。', findings: [{ severity: '中', title: '缺少边界检查', file_name: 'api.py', line_number: 9 }] },
      }],
    }
    teamApi.detail.mockImplementation(() => Promise.resolve({ ...teamDetail, session_id: deliveredSessionId }))
    const wrapper = mountCopilot()
    await openCopilot(wrapper)
    await flushSessionRestore()
    await flushPromises()

    expect(streams.records).toHaveLength(1)
    emit(0, { type: 'response.created', response: { id: 'run-team-89' } })
    emit(0, { type: 'response.failed', response: { id: 'run-team-89', error: { message: 'HTTP 402: quota refused' } } })
    await finish(0)
    await flushPromises()

    expect(wrapper.text()).toContain('团队审查部分结果（自动整理）')
    expect(wrapper.text()).toContain('缺少边界检查')
    expect(wrapper.text()).toContain('未经小菱最终模型复核')
    expect(meshApi.acknowledge).toHaveBeenCalledWith('admin', deliveredSessionId, receipt.message_id, 'processing', expect.any(String))
    expect(meshApi.acknowledge).toHaveBeenCalledWith('admin', deliveredSessionId, receipt.message_id, 'completed', expect.stringContaining('自动整理'))
    wrapper.unmount()
  })

  it('管理端刷新后从失败运行绑定的团队回执恢复部分结果且不重跑模型', async () => {
    let restoredSessionId = ''
    const receipt = {
      schema_version: '1.0', message_id: 'admin-team-90-terminal', idempotency_key: 'admin-team-90-terminal',
      trace_id: 'trace-admin-90', correlation_id: 'team:90', causation_id: 'request-admin-90',
      sent_from: 'system:agent_team', send_to: '', message_type: 'task.error',
      priority: 'normal' as const, subject: '管理端团队审查结束', status: 'completed',
      payload: { team_id: 90, status: 'failed' },
      context: { team_id: 90, team_terminal: true, session_delivery: true },
      artifacts: [], errors: [], requires_ack: true, max_attempts: 3, attempt_count: 1,
      expires_at: '', create_time: '', update_time: '',
    }
    sessionApi.get.mockImplementation((_surface: string, id: string) => {
      restoredSessionId = id
      return Promise.resolve({
        surface: 'admin', session_id: id,
        run: { run_id: 'run-admin-team-90', mesh_message_id: '', status: 'failed', model: 'deepseek-v4-pro', rounds: 1, error: 'Responses HTTP 402: quota refused', updated_at: '' },
        messages: [{ role: 'user', content: '只读检查管理 API' }],
        events: [{ type: 'response.tool.completed', call_id: 'create-admin-team-90', tool_name: 'create_agent_team', output: { team_id: 90 } }],
        mesh_messages: [{ ...receipt, send_to: `session:admin:${id}` }], pending: null,
      })
    })
    teamApi.list.mockImplementation((_input: { surface: string; session_id: string }) => Promise.resolve({
      items: [{ team_id: 90, title: '管理端自动审查', surface: 'admin', session_id: restoredSessionId, status: 'failed', max_active_children: 3, trace_id: 'trace-admin-90', counts: { total: 1, completed: 1, running: 0, queued: 0, failed: 0, blocked: 0 } }],
      total: 1,
    }))
    teamApi.detail.mockImplementation(() => Promise.resolve({
      team_id: 90, title: '管理端自动审查', surface: 'admin', session_id: restoredSessionId, status: 'completed',
      max_active_children: 3, trace_id: 'trace-admin-90',
      counts: { total: 1, completed: 1, running: 0, queued: 0, failed: 0, blocked: 0 },
      summary: { final_result: { summary: '已保存一条管理 API 证据。' } }, members: [], events: [], messages: [],
      tasks: [{
        task_id: 1, task_key: 'review', member_id: 1, member_key: 'reviewer', title: '只读管理 API 检查',
        depends_on: [], status: 'completed', priority: 0, attempt_count: 1, max_attempts: 3,
        result: { findings: [{ severity: '中', title: '缺少对象级权限校验', file_name: 'admin_api.py', line_number: 20 }] },
      }],
    } satisfies AgentTeamDetail))

    const wrapper = mountCopilot()
    await openCopilot(wrapper)
    await flushSessionRestore()

    expect(streams.records).toHaveLength(0)
    expect(wrapper.text()).toContain('团队审查部分结果（自动整理）')
    expect(wrapper.text()).toContain('缺少对象级权限校验')
    expect(wrapper.text()).toContain('未经小菱最终模型复核')
    expect(meshApi.acknowledge).not.toHaveBeenCalled()

    await (wrapper.vm as any).restoreSession()
    await flushSessionRestore()
    expect(wrapper.text().match(/团队审查部分结果（自动整理）/g)).toHaveLength(1)
    wrapper.unmount()
  })

  it('恢复当前管理会话时拉取服务端团队并默认折叠展示协作过程', async () => {
    teamApi.list.mockResolvedValue({
      items: [{ team_id: 42, title: '管理端验证', surface: 'admin', session_id: 'admin-test', status: 'verifying', max_active_children: 3, trace_id: 'trace-admin-42', counts: { total: 1, completed: 0, running: 1, queued: 0, failed: 0, blocked: 0 } }],
      total: 1,
    })
    teamApi.detail.mockResolvedValue({
      team_id: 42, title: '管理端验证', surface: 'admin', session_id: 'admin-test', status: 'verifying', max_active_children: 3, trace_id: 'trace-admin-42',
      counts: { total: 1, completed: 0, running: 1, queued: 0, failed: 0, blocked: 0 }, members: [], tasks: [], events: [], messages: [],
    })
    const wrapper = mountCopilot()
    await flushPromises()
    await openCopilot(wrapper)
    await flushSessionRestore()

    expect(teamApi.list).toHaveBeenCalledWith(expect.objectContaining({ surface: 'admin', limit: 20 }))
    expect(wrapper.find('.team-card').exists()).toBe(true)
    // 空成员时不再显示「已创建 0 个子Agent」,退化为「创建中」语义
    expect(wrapper.find('.team-card').text()).toContain('子Agent创建中')
    expect(wrapper.find('.team-side-panel').exists()).toBe(false)
    wrapper.unmount()
  })

  it('管理员端迟到的旧团队详情不得覆盖新请求的完成状态', async () => {
    const wrapper = mountCopilot()
    await openCopilot(wrapper)
    await flushSessionRestore()
    const vm = wrapper.vm as any
    vm.sessionId = 'admin-test'
    teamApi.list.mockResolvedValue({
      items: [{ team_id: 91, title: '管理端状态竞态', status: 'running' }], total: 1,
    })
    const old = deferred<Record<string, unknown>>()
    let detailCalls = 0
    teamApi.detail.mockImplementation(() => ++detailCalls === 1
      ? old.promise
      : Promise.resolve({
          team_id: 91, title: '管理端状态竞态', surface: 'admin', session_id: 'admin-test', status: 'completed',
          max_active_children: 1, trace_id: 'trace-91',
          counts: { total: 1, completed: 1, running: 0, queued: 0, failed: 0, blocked: 0 },
          members: [], tasks: [], events: [], messages: [],
        }))

    const staleRequest = vm.refreshAgentTeam()
    await flushPromises()
    await vm.refreshAgentTeam()
    expect(vm.agentTeams[0].status).toBe('completed')
    old.resolve({
      team_id: 91, title: '管理端状态竞态', surface: 'admin', session_id: 'admin-test', status: 'running',
      max_active_children: 1, trace_id: 'trace-91',
      counts: { total: 1, completed: 0, running: 1, queued: 0, failed: 0, blocked: 0 },
      members: [], tasks: [], events: [], messages: [],
    })
    await staleRequest
    expect(vm.agentTeams[0].status).toBe('completed')
    wrapper.unmount()
  })

  it('成员追问按钮关闭面板并预填管理输入框', async () => {
    teamApi.list.mockResolvedValue({
      items: [{
        team_id: 88, title: '管理端成员追问', surface: 'admin', session_id: 'admin-test',
        status: 'running', max_active_children: 3, trace_id: 'trace-admin-88',
        counts: { total: 1, completed: 0, running: 1, queued: 0, failed: 0, blocked: 0 },
      }],
      total: 1,
    })
    teamApi.detail.mockResolvedValue({
      team_id: 88, title: '管理端成员追问', surface: 'admin', session_id: 'admin-test',
      status: 'running', max_active_children: 3, trace_id: 'trace-admin-88',
      objective: '继续处理告警', started_at: '2026-08-13T10:00:00Z',
      counts: { total: 1, completed: 0, running: 1, queued: 0, failed: 0, blocked: 0 },
      members: [
        { member_id: 1, member_key: 'm1', display_name: '安全审计员', address: 'agent:security_auditor', kind: 'runtime', role: 'worker', status: 'running', started_at: '2026-08-13T10:00:00Z' },
      ],
      tasks: [
        { task_id: 1, task_key: 't1', member_id: 1, member_key: 'm1', title: '复核告警', status: 'running', depends_on: [], priority: 1, attempt_count: 0, max_attempts: 3 },
      ],
      events: [
        { event_id: 1, team_id: 88, task_id: 1, member_id: 1, event_type: 'task.claimed', from_status: 'queued', to_status: 'running', created_at: '2026-08-13T10:00:00Z' },
      ],
      messages: [],
    })
    const wrapper = mountCopilot()
    await flushPromises()
    await openCopilot(wrapper)
    await flushSessionRestore()

    const card = wrapper.find('.team-card-open')
    expect(card.exists()).toBe(true)
    await card.trigger('click')
    await flushPromises()
    expect(document.querySelector('.team-side-panel')).not.toBeNull()

    const memberTab = Array.from(document.querySelectorAll('.tab-btn'))
      .find((tab) => tab.textContent?.includes('成员'))
    expect(memberTab).toBeDefined()
    ;(memberTab as HTMLElement).click()
    await flushPromises()

    const askButton = document.querySelector('.member-work-ask') as HTMLButtonElement | null
    expect(askButton).not.toBeNull()
    askButton!.click()
    await flushPromises()

    expect(document.querySelector('.team-side-panel')).toBeNull()
    const input = wrapper.find('textarea').element as HTMLTextAreaElement
    expect(input.value).toContain('团队 #88')
    expect(input.value).toContain('安全审计员')
    expect(input.value).toContain('agent:security_auditor')
    expect(input.value).toContain('发送补充要求')

    // 新会话不能继承上一会话的成员追问草稿,避免误把旧指令发到新会话。
    await wrapper.find('.session-new').trigger('click')
    await flushSessionRestore()
    expect((wrapper.find('textarea').element as HTMLTextAreaElement).value).toBe('')

    await wrapper.find('.session-current').trigger('click')
    const previousSession = wrapper.findAll('.session-item').find((item) => !item.classes('is-active'))
    expect(previousSession).toBeDefined()
    await previousSession!.trigger('click')
    await flushSessionRestore()
    expect((wrapper.find('textarea').element as HTMLTextAreaElement).value).toContain('团队 #88')
    wrapper.unmount()
  })

  it('刷新恢复后仍先显示工具调用，再显示最终结论', async () => {
    sessionApi.get.mockResolvedValueOnce({
      surface: 'admin',
      session_id: 'admin-test',
      run: {
        run_id: 'run-restored-tools', status: 'completed', model: '', rounds: 1,
        error: '', updated_at: '2026-08-01T12:00:00Z',
      },
      messages: [
        { role: 'user', content: '查询用户列表' },
        { role: 'assistant', content: '共找到 3 个用户。\n\n<wbr>\n    •已完成。' },
      ],
      events: [
        {
          type: 'response.tool.completed', sequence_number: 2, call_id: 'call-users',
          tool_name: 'admin_list_users', agent_code: 'admin_copilot', output_summary: '返回 3 个用户',
        },
        {
          type: 'response.tool.started', sequence_number: 1, call_id: 'call-users',
          tool_name: 'admin_list_users', agent_code: 'admin_copilot', arguments: { page: 1 },
        },
      ],
      last_sequence_number: 2,
      pending: null,
    })
    const wrapper = mountCopilot()
    await flushPromises()
    await openCopilot(wrapper)
    await flushSessionRestore()
    const rows = wrapper.findAll('.message-row')

    // 首条为吉祥物欢迎语,其后才是恢复的历史消息
    expect(rows).toHaveLength(4)
    expect(rows[0].text()).toContain('我是小菱')
    expect(rows[1].classes()).toContain('is-user')
    await expandTimeline(wrapper)
    expect(rows[2].find('.xl-steps').text()).toContain('小菱的工作')
    expect(rows[2].find('.xl-steps').text()).toContain('查看管理数据')
    expect(rows[2].text()).toContain('做好了')
    expect(rows[3].find('.markdown-body').text()).toContain('共找到 3 个用户')
    expect(rows[3].find('.markdown-body').text()).toContain('已完成。')
    expect(rows[3].find('.markdown-body').text()).not.toMatch(/<wbr>|•/)
    wrapper.unmount()
  })

  it('初始会话恢复完成前禁止发送', async () => {
    let resolveSession!: (value: unknown) => void
    sessionApi.get.mockReturnValueOnce(new Promise((resolve) => { resolveSession = resolve }))
    const wrapper = mountCopilot()
    await openCopilot(wrapper)
    await flushSessionRestore()

    expect(wrapper.find('textarea').attributes('disabled')).toBeDefined()
    await wrapper.find('textarea').setValue('不应提前发送')
    await wrapper.find('.send-button').trigger('click')
    expect(streams.start).not.toHaveBeenCalled()

    resolveSession({ surface: 'admin', session_id: 'admin-test', run: null, messages: [], pending: null })
    await flushPromises()
    expect(wrapper.find('textarea').attributes('disabled')).toBeUndefined()
    wrapper.unmount()
  })

  it('恢复运行态后轮询到待审批仍按低频间隔继续刷新', async () => {
    vi.useFakeTimers()
    sessionApi.get
      .mockResolvedValueOnce({
        surface: 'admin', session_id: 'admin-test',
        run: { run_id: 'run-active', status: 'running', model: '', rounds: 1, error: '', updated_at: '' },
        messages: [{ role: 'user', content: '处理中' }], pending: null,
      })
      .mockResolvedValueOnce({
        surface: 'admin', session_id: 'admin-test',
        run: { run_id: 'run-active', status: 'waiting_approval', model: '', rounds: 1, error: '', updated_at: '' },
        messages: [{ role: 'user', content: '处理中' }],
        pending: {
          type: 'response.approval.required', run_id: 'run-active', call_id: 'call-active',
          tool_name: 'admin_delete_user', arguments: { user_id: 901 }, operation: '删除用户',
          impact: '账号将被软删除', danger: true,
        },
      })
    const wrapper = mountCopilot()
    await flushPromises()
    await openCopilot(wrapper)
    await vi.advanceTimersByTimeAsync(0)
    await flushPromises()
    expect(sessionApi.get).toHaveBeenCalledTimes(1)

    await vi.advanceTimersByTimeAsync(1000)
    await flushPromises()
    expect(sessionApi.get).toHaveBeenCalledTimes(2)
    expect(wrapper.text()).toContain('删除用户')

    await vi.advanceTimersByTimeAsync(2999)
    await flushPromises()
    expect(sessionApi.get).toHaveBeenCalledTimes(2)

    await vi.advanceTimersByTimeAsync(1)
    await flushPromises()
    expect(sessionApi.get).toHaveBeenCalledTimes(3)
    wrapper.unmount()
    vi.useRealTimers()
  })

  it('空闲时实时拉取子 Agent 交流过程并默认折叠详情', async () => {
    vi.useFakeTimers()
    const meshMessage = {
      schema_version: '1.0', message_id: 'msg-admin-live', idempotency_key: 'idem-admin-live',
      trace_id: 'trace-admin-live', correlation_id: 'corr-admin-live', causation_id: '',
      sent_from: 'agent:error-handler', send_to: 'session:admin:admin-test',
      message_type: 'task.result', priority: 'normal', subject: '报错根因已定位',
      status: 'completed', payload: { root_cause: '数据库连接耗尽' }, context: {},
      artifacts: [], errors: [], requires_ack: true, max_attempts: 3, attempt_count: 1,
      expires_at: '2026-08-10T12:10:00Z', create_time: '2026-08-10T12:00:00Z',
      update_time: '2026-08-10T12:00:01Z',
    }
    sessionApi.get
      .mockResolvedValueOnce({
        surface: 'admin', session_id: 'admin-test', run: null, messages: [],
        mesh_messages: [], pending: null,
      })
      .mockResolvedValue({
        surface: 'admin', session_id: 'admin-test', run: null, messages: [],
        mesh_messages: [meshMessage], pending: null,
      })
    const wrapper = mountCopilot()
    await vi.advanceTimersByTimeAsync(0)
    await flushPromises()
    await openCopilot(wrapper)

    expect(wrapper.find('.xl-steps').exists()).toBe(false)
    await vi.advanceTimersByTimeAsync(3000)
    await flushPromises()

    const timeline = wrapper.find('.xl-steps')
    expect(timeline.text()).toContain('报错根因已定位')
    expect(timeline.text()).not.toContain('receive_message')
    expect(timeline.text()).not.toContain('agent:error-handler')
    // 新设计:无技术折叠头,完成步骤无明细行,且不暴露内部错误原文
    expect(timeline.find('.xl-step-error').exists()).toBe(false)
    expect(timeline.text()).not.toContain('数据库连接耗尽')
    wrapper.unmount()
    vi.useRealTimers()
  })

  it('轮询失败时显示同步中断并在下一轮恢复', async () => {
    vi.useFakeTimers()
    sessionApi.get
      .mockResolvedValueOnce({ surface: 'admin', session_id: 'admin-test', run: null, messages: [], pending: null })
      .mockRejectedValueOnce(new Error('network down'))
      .mockResolvedValue({ surface: 'admin', session_id: 'admin-test', run: null, messages: [], pending: null })
    const wrapper = mountCopilot()
    await vi.advanceTimersByTimeAsync(0)
    await flushPromises()
    await openCopilot(wrapper)

    await vi.advanceTimersByTimeAsync(3000)
    await flushPromises()
    expect(wrapper.text()).toContain('同步暂时中断,正在重试')

    // 失败后指数退避:下一次轮询间隔翻倍为 6000ms。
    await vi.advanceTimersByTimeAsync(6000)
    await flushPromises()
    expect(wrapper.text()).not.toContain('同步暂时中断,正在重试')
    expect(wrapper.text()).toContain('已同步')
    wrapper.unmount()
    vi.useRealTimers()
  })

  it('刷新后恢复待审批内容并在高危确认后续跑', async () => {
    sessionApi.get.mockResolvedValueOnce({
      surface: 'admin',
      session_id: 'admin-test',
      run: { run_id: 'run-restored', status: 'waiting_approval', model: '', rounds: 1, error: '', updated_at: '' },
      messages: [{ role: 'user', content: '删除两个测试用户' }],
      pending: {
        type: 'response.approval.required',
        run_id: 'run-restored',
        call_id: 'call-restored',
        tool_name: 'admin_delete_users',
        arguments: { user_ids: [901, 902] },
        operation: '批量删除用户',
        impact: '两个用户将无法登录',
        danger: true,
        preview: { affected_count: 2, targets: ['qa-a (#901)', 'qa-b (#902)'] },
      },
    })
    const wrapper = mountCopilot()
    await flushPromises()
    await openCopilot(wrapper)
    await flushSessionRestore()

    expect(wrapper.text()).toContain('批量删除用户')
    expect(wrapper.text()).toContain('qa-a (#901)')
    expect(wrapper.find('textarea').attributes('disabled')).toBeDefined()
    // 等待审批时按“取消任务”设计显示停止按钮，发送按钮被替换。
    expect(wrapper.find('.stop-button').exists()).toBe(true)
    expect(wrapper.find('.send-button').exists()).toBe(false)
    expect(wrapper.find<HTMLButtonElement>('.response-approval .primary-action').element.disabled).toBe(false)
    expect(wrapper.find('.danger-input').exists()).toBe(false)
    await wrapper.find('.response-approval .primary-action').trigger('click')
    await flushPromises()
    expect(streams.records[0].body).toMatchObject({
      action: 'approve',
      run_id: 'run-restored',
      call_id: 'call-restored',
      confirmation: '确认执行',
      messages: [],
    })
    await finish(0)
  })

  it('opens empty and renders only the first meaningful streamed delta', async () => {
    const wrapper = mountCopilot()
    await openCopilot(wrapper)
    await flushSessionRestore()
    await flushPromises()

    expect(wrapper.findAll('.message-row')).toHaveLength(1)
    expect(wrapper.text()).toContain('我是小菱')
    expect(wrapper.find('.quick-commands').exists()).toBe(false)

    await wrapper.find('textarea').setValue('检查生产状态')
    void wrapper.find('textarea').trigger('keydown', { key: 'Enter' })
    await flushPromises()

    expect(streams.records[0].body).toMatchObject({
      action: 'start',
      surface: 'admin',
      messages: [{ role: 'user', content: '检查生产状态' }],
    })

    emit(0, { type: 'response.output_text.delta', delta: '' })
    emit(0, { type: 'response.output_text.delta', delta: '\n\n' })
    await flushPromises()
    expect(wrapper.findAll('.is-assistant .message-bubble')).toHaveLength(1)

    emit(0, { type: 'response.output_text.delta', delta: '第一段\n\n<wbr>\n    •第二段' })
    await flushPromises()
    const assistant = wrapper.findAll('.is-assistant .message-bubble')[1]
    expect(assistant.exists()).toBe(true)
    expect(assistant.text()).toContain('第一段')
    expect(assistant.text()).toContain('第二段')
    expect(assistant.text()).not.toContain('<wbr>')
    expect(wrapper.findAll('.is-assistant .message-bubble')).toHaveLength(2)

    emit(0, { type: 'response.completed', response: { id: 'run-admin', status: 'completed' } })
    await finish(0)

    await wrapper.find('textarea').setValue('继续检查')
    void wrapper.find('textarea').trigger('keydown', { key: 'Enter' })
    await flushPromises()
    const history = streams.records[1].body.messages as Array<{ role: string; content: string }>
    expect(history).toEqual([{ role: 'user', content: '继续检查' }])
    expect(streams.records[1].body.use_server_history).toBe(true)
    await finish(1)
  })

  it('keeps a streamed Markdown user table complete inside the chat bubble', async () => {
    const wrapper = mountCopilot()
    await openCopilot(wrapper)
    await flushSessionRestore()
    await wrapper.find('textarea').setValue('查看用户列表')
    void wrapper.find('textarea').trigger('keydown', { key: 'Enter' })
    await flushPromises()

    emit(0, {
      type: 'response.output_text.delta',
      delta: '已查询到平台用户列表：\n\n| ID | 用户名 | 昵称 | 角色 | 状态 |\n| --- | --- | --- | --- | --- |\n',
    })
    emit(0, {
      type: 'response.output_text.delta',
      delta: '| 5 | lijiadong-long-account | outle | user | 正常 |\n| 7 | linruoxi | 林若曦 | reviewer | 正常 |',
    })
    await flushPromises()

    const table = wrapper.find('.markdown-body table')
    expect(table.exists()).toBe(true)
    expect(table.findAll('th')).toHaveLength(5)
    expect(table.findAll('td')).toHaveLength(10)
    expect(table.text()).toContain('lijiadong-long-account')
    expect(table.text()).toContain('reviewer')
    expect(table.text()).toContain('正常')

    emit(0, { type: 'response.completed', response: { id: 'run-user-table', status: 'completed' } })
    await finish(0)
  })

  it('approves by click and resumes without adding a fake user message', async () => {
    const wrapper = mountCopilot()
    await openCopilot(wrapper)
    await flushSessionRestore()
    await wrapper.find('textarea').setValue('删除测试用户')
    void wrapper.find('textarea').trigger('keydown', { key: 'Enter' })
    await flushPromises()

    emit(0, {
      type: 'response.approval.required',
      run_id: 'run-admin-approval',
      call_id: 'call-delete',
      confirmation: '确认执行',
      tool_name: 'admin_delete_user',
      arguments: { user_id: 9 },
      operation: '删除用户',
      impact: '用户将无法登录',
      danger: true,
      preview: { target: 'test-user (#9)', affected_count: 1 },
    })
    await finish(0)

    expect(wrapper.find('.response-approval').exists()).toBe(true)
    expect(wrapper.find('.xl-steps').text()).toContain('删除管理数据')
    expect(wrapper.find('.xl-steps').text()).toContain('等你确认后继续')
    await wrapper.find('.response-approval-detail-toggle').trigger('click')
    expect(wrapper.find('.response-approval-arguments').text()).toContain('"user_id": 9')
    expect(wrapper.find('.response-approval-preview').text()).toContain('test-user (#9)')
    expect(wrapper.find('.danger-input').exists()).toBe(false)
    expect(wrapper.findAll('.is-user')).toHaveLength(1)
    expect(wrapper.find<HTMLButtonElement>('.response-approval .primary-action').element.disabled).toBe(false)
    await wrapper.find('.response-approval .primary-action').trigger('click')
    await flushPromises()

    expect(streams.records[1].body).toMatchObject({
      action: 'approve',
      surface: 'admin',
      run_id: 'run-admin-approval',
      call_id: 'call-delete',
      confirmation: '确认执行',
    })
    const approveMessages = streams.records[1].body.messages as Array<{ role: string; content: string }>
    expect(approveMessages).toEqual([])
    expect(wrapper.findAll('.is-user')).toHaveLength(1)

    emit(1, {
      type: 'response.tool.completed',
      call_id: 'call-delete',
      tool_name: 'admin_delete_user',
      status: 'success',
    })
    emit(1, { type: 'response.output_text.delta', delta: '操作已完成' })
    emit(1, { type: 'response.completed', response: { id: 'run-admin-approval' } })
    await finish(1)
    expect(wrapper.text()).toContain('操作已完成')
    expect(wrapper.text()).toContain('已批准')
    await expandTimeline(wrapper)
    const timelineText = wrapper.findAll('.xl-steps').map((node) => node.text()).join('\n')
    expect(timelineText).toContain('做好了')
  })

  it('marks a resumed approval as failed when the terminal response has no tool result', async () => {
    const wrapper = mountCopilot()
    await openCopilot(wrapper)
    await flushSessionRestore()
    await wrapper.find('textarea').setValue('删除测试用户')
    void wrapper.find('textarea').trigger('keydown', { key: 'Enter' })
    await flushPromises()

    emit(0, {
      type: 'response.approval.required',
      run_id: 'run-admin-resume-missing-tool',
      call_id: 'call-delete-missing-tool',
      tool_name: 'admin_delete_user',
      arguments: { user_id: 9 },
      operation: '删除用户',
      impact: '用户将无法登录',
      danger: true,
    })
    await finish(0)

    await wrapper.find('.response-approval .primary-action').trigger('click')
    await flushPromises()
    expect(streams.records[1].body).toMatchObject({
      action: 'approve',
      run_id: 'run-admin-resume-missing-tool',
      call_id: 'call-delete-missing-tool',
    })

    emit(1, { type: 'response.completed', response: { id: 'run-admin-resume-missing-tool' } })
    await finish(1)

    await expandTimeline(wrapper)
    // 新设计:失败步骤默认展开原因
    const timelineText = wrapper.find('.xl-steps').text()
    expect(timelineText).toContain('没做成')
    expect(timelineText).toMatch(/响应已结束|删除管理数据/)
    expect(timelineText).not.toContain('做好了')
  })

  it('does not mark a tool completed without response.tool.completed evidence', async () => {
    const wrapper = mountCopilot()
    await openCopilot(wrapper)
    await flushSessionRestore()
    await wrapper.find('textarea').setValue('删除测试模板')
    void wrapper.find('textarea').trigger('keydown', { key: 'Enter' })
    await flushPromises()

    emit(0, {
      type: 'response.tool.started',
      call_id: 'call-unproven',
      tool_name: 'admin_execute_capability',
      arguments: { capability: 'report_templates.delete' },
      page_action: { page: '/report/templates', capability: 'report_templates.delete', phase: 'started', mutation: true },
    })
    expect(useAgentActivityStore().isActing).toBe(true)
    emit(0, { type: 'response.completed', response: { id: 'run-unproven', status: 'completed' } })
    expect(useAgentActivityStore().isActing).toBe(false)
    await finish(0)

    await expandTimeline(wrapper)
    const timeline = wrapper.find('.xl-steps')
    expect(timeline.text()).toContain('执行管理操作')
    expect(timeline.text()).toContain('没做成')
    // 新设计:失败默认展开,无需点击
    expect(wrapper.find('.xl-steps').text()).toMatch(/响应已结束|执行管理操作/)
    expect(wrapper.find('.xl-steps').text()).not.toContain('已完成')
  })

  it('shows one-time sensitive results without sending them into the next model request', async () => {
    const writeText = vi.fn().mockResolvedValue(undefined)
    Object.defineProperty(navigator, 'clipboard', {
      configurable: true,
      value: { writeText },
    })
    const wrapper = mountCopilot()
    await openCopilot(wrapper)
    await flushSessionRestore()
    await wrapper.find('textarea').setValue('生成一个内测码')
    void wrapper.find('textarea').trigger('keydown', { key: 'Enter' })
    await flushPromises()

    emit(0, {
      type: 'response.sensitive.result',
      run_id: 'run-sensitive',
      call_id: 'call-sensitive',
      capability: 'beta_codes.generate',
      title: '新生成的内测码',
      notice: '仅当前页面会话显示，请立即妥善保存；刷新后无法恢复明文。',
      values: ['BETA-ONE-TIME-123'],
    })
    emit(0, { type: 'response.output_text.delta', delta: '已生成 1 个内测码' })
    emit(0, { type: 'response.completed', response: { id: 'run-sensitive' } })
    await finish(0)

    expect(wrapper.find('.sensitive-result').text()).toContain('BETA-ONE-TIME-123')
    expect(wrapper.find('.sensitive-result').text()).toContain('刷新后无法恢复明文')
    await wrapper.find('.sensitive-copy').trigger('click')
    await flushPromises()
    expect(writeText).toHaveBeenCalledWith('BETA-ONE-TIME-123')
    expect(messages.success).toHaveBeenCalledWith('已复制')

    await wrapper.find('textarea').setValue('查询内测码列表')
    void wrapper.find('textarea').trigger('keydown', { key: 'Enter' })
    await flushPromises()
    const serialized = JSON.stringify(streams.records[1].body)
    expect(serialized).not.toContain('BETA-ONE-TIME-123')
    const nextMessages = streams.records[1].body.messages as Array<{ role: string; content: string }>
    expect(nextMessages).toEqual([{ role: 'user', content: '查询内测码列表' }])
    expect(streams.records[1].body.use_server_history).toBe(true)
    await finish(1)
  })

  it('streams function arguments and submits a structured option immediately', async () => {
    const wrapper = mountCopilot()
    await openCopilot(wrapper)
    await flushSessionRestore()
    await wrapper.find('textarea').setValue('查找李家栋')
    void wrapper.find('textarea').trigger('keydown', { key: 'Enter' })
    await flushPromises()

    emit(0, {
      type: 'response.output_item.added',
      output_index: 0,
      item: { type: 'function_call', id: 'item-search', call_id: 'call-search', name: 'admin_search_users' },
    })
    emit(0, {
      type: 'response.function_call_arguments.delta',
      output_index: 0,
      item_id: 'item-search',
      delta: '{"query":"李',
    })
    emit(0, {
      type: 'response.function_call_arguments.done',
      output_index: 0,
      item_id: 'item-search',
      arguments: '{"query":"李家栋"}',
    })
    emit(0, {
      type: 'response.input.required',
      run_id: 'run-search',
      call_id: 'call-search',
      question: '你指的是下面哪位用户？',
      options: [
        { label: '家栋（账号 lijiadong）', value: '用户 #5', description: '昵称近似匹配' },
        { label: '李佳栋（账号 lijiadong2）', value: '用户 #18' },
      ],
    })
    await finish(0)

    // 新设计:参数永不上屏,只保留通俗状态
    expect(wrapper.find('.response-tool-arguments').exists()).toBe(false)
    expect(wrapper.text()).toContain('在等你的回答')
    expect(wrapper.text()).not.toContain('"query"')
    expect(wrapper.findAll('.response-input-option')).toHaveLength(2)
    expect(wrapper.text()).toContain('其他（自定义输入）')
    await wrapper.findAll('.response-input-option')[0].trigger('click')
    await flushPromises()
    await wrapper.find('.response-answer-submit').trigger('click')
    await flushPromises()
    expect(streams.records[1].body).toMatchObject({
      action: 'answer',
      surface: 'admin',
      run_id: 'run-search',
      call_id: 'call-search',
      answer: '用户 #5',
    })
    expect(wrapper.findAll('.is-user')[1].text()).toContain('用户 #5')
    emit(1, { type: 'response.completed', response: { id: 'run-search' } })
    await finish(1)
  })

  it('does not submit Enter while the Chinese IME is composing', async () => {
    const wrapper = mountCopilot()
    await openCopilot(wrapper)
    await flushSessionRestore()
    const textarea = wrapper.find('textarea')
    await textarea.setValue('删除用户')
    await textarea.trigger('keydown', { key: 'Enter', isComposing: true })
    await flushPromises()
    expect(streams.start).not.toHaveBeenCalled()

    void textarea.trigger('keydown', { key: 'Enter' })
    await flushPromises()
    expect(streams.start).toHaveBeenCalledTimes(1)
  })
})

describe('AdminCopilot 历史bug回归(本PR补齐项)', () => {
  it('【修复】协议错误必须留在消息流(旧版只有几秒即逝的Toast,回来后零痕迹)', async () => {
    const wrapper = mountCopilot()
    await flushPromises()
    await openCopilot(wrapper)
    await flushSessionRestore()

    await wrapper.find('textarea').setValue('查询系统状态')
    void wrapper.find('.send-button').trigger('click')
    await flushPromises()
    emit(0, { type: 'response.failed', response: { error: { message: '模型超时' } } })
    await finish(0)

    // Toast 之外,消息流里必须有错误卡片与操作入口
    const card = wrapper.find('.copilot-error-card')
    expect(card.exists()).toBe(true)
    expect(card.text()).toContain('这次没有完成')
    expect(card.text()).toContain('模型超时')
    expect(card.find('.copilot-error-btn.is-retry').exists()).toBe(true)
    expect(card.find('.copilot-error-btn:not(.is-retry)').text()).toContain('新建对话')
    wrapper.unmount()
  })

  it('【修复】助手消息必须有一键复制(旧版只能手动拖选悬浮窗文本)', async () => {
    sessionApi.get.mockResolvedValueOnce({
      surface: 'admin', session_id: 'admin-test',
      run: { run_id: 'r', status: 'completed', model: '', rounds: 1, error: '', updated_at: '2026-08-01T12:00:00Z' },
      messages: [{ role: 'assistant', content: '巡检结论:一切正常' }],
      pending: null,
    })
    const writeText = vi.fn().mockResolvedValue(undefined)
    Object.defineProperty(navigator, 'clipboard', { value: { writeText }, configurable: true })
    const wrapper = mountCopilot()
    await flushPromises()
    await openCopilot(wrapper)
    await flushSessionRestore()

    // 欢迎语也有复制按钮;精确定位承载目标文本的气泡对应的按钮
    const buttons = wrapper.findAll('.message-copy-btn')
    expect(buttons.length).toBeGreaterThan(0)
    const target = buttons.find((btn) => btn.element.closest('.message-row')?.textContent?.includes('巡检结论:一切正常'))
    expect(target).toBeDefined()
    await target!.trigger('click')
    await flushPromises()
    expect(writeText).toHaveBeenCalledWith('巡检结论:一切正常')
    expect(messages.success).toHaveBeenCalled()
    wrapper.unmount()
  })

  it('【修复】面板可见时按 / 必须聚焦输入框(旧版只支持 Escape)', async () => {
    const wrapper = mountCopilot()
    await flushPromises()
    await openCopilot(wrapper)
    await flushSessionRestore()

    const textarea = wrapper.find('textarea').element as HTMLTextAreaElement
    const focusSpy = vi.spyOn(textarea, 'focus')
    // 真实浏览器里 keydown 的 target 是聚焦元素(如 body),不是 window;显式设定
    const event = new KeyboardEvent('keydown', { key: '/', bubbles: true, cancelable: true })
    Object.defineProperty(event, 'target', { value: document.body })
    window.dispatchEvent(event)
    await flushPromises()
    expect(focusSpy).toHaveBeenCalled()
    focusSpy.mockRestore()
    wrapper.unmount()
  })
})

describe('AdminCopilot 窄桌面布局回归', () => {
  function setViewport(width: number, height: number): () => void {
    const previousWidth = window.innerWidth
    const previousHeight = window.innerHeight
    Object.defineProperty(window, 'innerWidth', { configurable: true, value: width })
    Object.defineProperty(window, 'innerHeight', { configurable: true, value: height })
    return () => {
      Object.defineProperty(window, 'innerWidth', { configurable: true, value: previousWidth })
      Object.defineProperty(window, 'innerHeight', { configurable: true, value: previousHeight })
    }
  }

  function mockPanelSize(width: number, height: number): () => void {
    const widthSpy = vi.spyOn(HTMLElement.prototype, 'offsetWidth', 'get')
      .mockImplementation(function (this: HTMLElement) {
        return this.classList.contains('copilot-panel') ? Number.parseFloat(this.style.width) || width : 0
      })
    const heightSpy = vi.spyOn(HTMLElement.prototype, 'offsetHeight', 'get')
      .mockImplementation(function (this: HTMLElement) {
        return this.classList.contains('copilot-panel') ? Number.parseFloat(this.style.height) || height : 0
      })
    return () => {
      widthSpy.mockRestore()
      heightSpy.mockRestore()
    }
  }

  it('1280x800 默认展开时不覆盖用户管理表格右侧操作区', async () => {
    const restoreViewport = setViewport(1280, 800)
    const restorePanelSize = mockPanelSize(400, 620)
    const wrapper = mountCopilot()

    try {
      await openCopilot(wrapper)
      await flushSessionRestore()

      const panel = wrapper.find<HTMLElement>('.copilot-panel').element
      const panelLeft = Number.parseFloat(panel.style.left)
      const panelRight = panelLeft + panel.offsetWidth
      // 1280px 线上实测中，用户表格首个右侧操作按钮从 x=933 开始。
      // 浮层不与该命中区相交，elementFromPoint 才会命中表格按钮。
      expect(panelRight).toBeLessThanOrEqual(933)
      expect(window.localStorage.getItem('prism-floating-chat-position:admin')).toBeNull()
    } finally {
      wrapper.unmount()
      restorePanelSize()
      restoreViewport()
    }
  })

  it('1280x800 安全停靠后仍可拖动并保存位置', async () => {
    const restoreViewport = setViewport(1280, 800)
    const restorePanelSize = mockPanelSize(400, 620)
    const wrapper = mountCopilot()

    try {
      await openCopilot(wrapper)
      await flushSessionRestore()
      const panel = wrapper.find<HTMLElement>('.copilot-panel')
      const handle = wrapper.find<HTMLElement>('.panel-drag-handle')
      const initialLeft = Number.parseFloat(panel.element.style.left)

      await handle.trigger('pointerdown', {
        button: 0,
        clientX: initialLeft + 12,
        clientY: 180,
        pointerId: 1,
      })
      await panel.trigger('pointermove', {
        clientX: initialLeft - 68,
        clientY: 180,
        pointerId: 1,
      })
      await panel.trigger('pointerup', { pointerId: 1 })

      expect(Number.parseFloat(panel.element.style.left)).toBe(initialLeft - 80)
      expect(window.localStorage.getItem('prism-floating-chat-position:admin')).toContain(`"left":${initialLeft - 80}`)
    } finally {
      wrapper.unmount()
      restorePanelSize()
      restoreViewport()
    }
  })

  it('桌面缩放柄同时支持鼠标拖动与键盘，输入区仍独立于消息区', async () => {
    const restoreViewport = setViewport(1280, 800)
    const restorePanelSize = mockPanelSize(400, 620)
    const wrapper = mountCopilot()

    try {
      await openCopilot(wrapper)
      await flushSessionRestore()
      const panel = wrapper.find<HTMLElement>('.copilot-panel')
      const resizeHandle = wrapper.find<HTMLButtonElement>('.panel-resize-handle')

      expect(resizeHandle.attributes('aria-label')).toContain('方向键')
      await resizeHandle.trigger('keydown', { key: 'ArrowRight' })
      expect(panel.attributes('style')).toContain('width: 416px')

      await resizeHandle.trigger('pointerdown', {
        button: 0,
        clientX: 300,
        clientY: 300,
        pointerId: 2,
      })
      expect(panel.classes()).toContain('is-resizing')
      await panel.trigger('pointermove', { clientX: 340, clientY: 330, pointerId: 2 })
      await panel.trigger('pointerup', { pointerId: 2 })
      expect(panel.attributes('style')).toContain('width: 456px')
      expect(panel.attributes('style')).toContain('height: 632px') // 800px 视口减去 top=156px 与 12px 下边距
      expect(panel.classes()).not.toContain('is-resizing')
      expect(wrapper.findAll('.panel-resize-handle')).toHaveLength(8)
      await resizeHandle.trigger('pointerdown', { button: 0, clientX: 450, clientY: 650, pointerId: 3 })
      await panel.trigger('lostpointercapture', { pointerId: 3 })
      await panel.trigger('pointermove', { clientX: 500, clientY: 650, pointerId: 3 })
      expect(panel.classes()).not.toContain('is-resizing')
      expect(panel.attributes('style')).toContain('width: 456px')

      const messages = panel.find('.copilot-messages').element
      const input = panel.find('.copilot-input-area').element
      expect(messages.parentElement).toBe(panel.element)
      expect(input.parentElement).toBe(panel.element)
      expect(messages.contains(input)).toBe(false)
    } finally {
      wrapper.unmount()
      restorePanelSize()
      restoreViewport()
    }
  })

  it('520px 以下仍由移动端 inset 布局接管且禁止拖动', async () => {
    const restoreViewport = setViewport(390, 844)
    const restorePanelSize = mockPanelSize(366, 620)
    const wrapper = mountCopilot()

    try {
      await openCopilot(wrapper)
      await flushSessionRestore()
      const panel = wrapper.find<HTMLElement>('.copilot-panel')

      expect(panel.element.style.left).toBe('')
      await wrapper.find('.panel-drag-handle').trigger('pointerdown', {
        button: 0,
        clientX: 24,
        clientY: 24,
        pointerId: 1,
      })
      await panel.trigger('pointermove', { clientX: 100, clientY: 100, pointerId: 1 })
      expect(panel.classes()).not.toContain('is-dragging')
      expect(panel.element.style.left).toBe('')
    } finally {
      wrapper.unmount()
      restorePanelSize()
      restoreViewport()
    }
  })
})

it.each(['completed', 'failed', 'cancelled'])('管理审计阶段属于真实助手运行，%s 后立即停止活动标记', async (terminal) => {
  const wrapper = mountCopilot()
  try {
    await openCopilot(wrapper)
    await flushSessionRestore()
    await wrapper.find('textarea').setValue('审计阶段核验')
    void wrapper.find('.send-button').trigger('click')
    await flushPromises()
    emit(0, { type: 'response.created', response: { id: 'run-audit' } })
    emit(0, { type: 'response.audit.progress', phase: 'scout', label: '侦察员', message: '读取项目' })
    await flushPromises()
    expect(wrapper.find('.is-user .xl-steps').exists()).toBe(false)
    expect(wrapper.get('.is-assistant .xl-audit-now').text()).toBe('进行中')
    emit(0, { type: 'response.audit.progress', phase: 'analysis', label: '分析师', message: '分析证据' })
    await flushPromises()
    expect(wrapper.findAll('.xl-audit-phase')).toHaveLength(2)
    expect(wrapper.findAll('.xl-audit-now')).toHaveLength(1)
    emit(0, { type: `response.${terminal}`, response: { id: 'run-audit' } })
    await finish(0)
    expect(wrapper.find('.xl-audit-now').exists()).toBe(false)
    expect(wrapper.text()).toContain('分析师')
  } finally { wrapper.unmount() }
})


it('管理端可选择图片单独发送，移除附件后恢复禁用', async () => {
  const wrapper = mountCopilot()
  await openCopilot(wrapper)
  await flushSessionRestore()
  const input = wrapper.find('input[aria-label="选择图片附件"]')
  Object.defineProperty(input.element, 'files', {value:[new File(['image'],'sample.png',{type:'image/png'})], configurable:true})
  await input.trigger('change')
  await vi.waitFor(() => expect(wrapper.find('.chat-image-chip').exists()).toBe(true))
  expect(wrapper.find('.send-button').attributes('disabled')).toBeUndefined()
  await wrapper.find('.chip-remove').trigger('click')
  expect(wrapper.find('.send-button').attributes('disabled')).toBeDefined()
  wrapper.unmount()
})


it('首发图片在创建运行前失败可核对服务端后重试，保留原图', async () => {
  const wrapper = mountCopilot()
  await openCopilot(wrapper)
  await flushSessionRestore()
  const input = wrapper.find('input[aria-label="选择图片附件"]')
  Object.defineProperty(input.element, 'files', {value:[new File(['image'],'sample.png',{type:'image/png'})], configurable:true})
  await input.trigger('change')
  await vi.waitFor(() => expect(wrapper.find('.chat-image-chip').exists()).toBe(true))
  void wrapper.find('.send-button').trigger('click')
  await flushPromises()
  const firstBody = streams.records[0].body
  streams.records[0].reject(new Error('网络断开'))
  await flushPromises()
  expect(wrapper.find('.copilot-error-btn.is-retry').attributes('disabled')).toBeUndefined()
  void wrapper.find('.copilot-error-btn.is-retry').trigger('click')
  await flushPromises()
  expect(streams.records).toHaveLength(2)
  expect(streams.records[1].body).toEqual(firstBody)
  wrapper.unmount()
})


async function failFirstAdminMessage(wrapper: VueWrapper): Promise<Record<string, unknown>> {
  await openCopilot(wrapper)
  await flushSessionRestore()
  await wrapper.find('textarea').setValue('测试断网后的提交确认')
  void wrapper.find('.send-button').trigger('click')
  await flushPromises()
  const payload = streams.records[0].body
  streams.records[0].reject(new Error('网络断开'))
  await flushPromises()
  return payload
}

it('未确认提交后的空恢复保留原提问与断网提示，不自动重放', async () => {
  const wrapper = mountCopilot()
  try {
    const payload = await failFirstAdminMessage(wrapper)
    const vm = wrapper.vm as any
    const messageIds = vm.messages.map((entry: { id: string }) => entry.id)
    expect(wrapper.text()).toContain('测试断网后的提交确认')
    expect(wrapper.text()).toContain('网络断开')
    sessionApi.get.mockResolvedValue({ surface: 'admin', session_id: payload.session_id,
      run: null, messages: [], events: [], pending: null })
    await vm.restoreSession()
    await flushSessionRestore()
    expect(vm.messages.map((entry: { id: string }) => entry.id)).toEqual(messageIds)
    expect(wrapper.text()).toContain('测试断网后的提交确认')
    expect(wrapper.text()).toContain('网络断开')
    expect(vm.unconfirmedStart?.payload).toEqual(payload)
    expect(wrapper.find('.copilot-error-btn.is-retry').attributes('disabled')).toBeUndefined()
    expect(streams.start).toHaveBeenCalledTimes(1)
  } finally { wrapper.unmount() }
})

it('首发断网但服务端已创建运行时恢复快照，不重复发起 start', async () => {
  const wrapper = mountCopilot()
  try {
    const payload = await failFirstAdminMessage(wrapper)
    sessionApi.get.mockResolvedValue({ surface: 'admin', session_id: payload.session_id,
      run: { run_id: 'server-accepted', status: 'running', model: 'model', rounds: 1, error: '', updated_at: new Date().toISOString() },
      messages: [{ role: 'user', content: '测试断网后的提交确认' }], events: [], pending: null })
    const requestsBeforeRetry = sessionApi.get.mock.calls.length
    void wrapper.find('.copilot-error-btn.is-retry').trigger('click')
    await flushPromises()
    expect(sessionApi.get.mock.calls.length).toBeGreaterThan(requestsBeforeRetry)
    expect(sessionApi.get.mock.calls.slice(requestsBeforeRetry)).toContainEqual(['admin', payload.session_id])
    expect(streams.records).toHaveLength(1)
    expect(messages.info).toHaveBeenCalledWith(expect.stringContaining('已恢复'))
  } finally { wrapper.unmount() }
})

it('提交确认接口失败时保留可重试原请求，不冒险重复 start', async () => {
  const wrapper = mountCopilot()
  try {
    await failFirstAdminMessage(wrapper)
    sessionApi.get.mockRejectedValueOnce(new Error('无法核对'))
    void wrapper.find('.copilot-error-btn.is-retry').trigger('click')
    await flushPromises()
    expect(streams.records).toHaveLength(1)
    expect(wrapper.text()).toContain('未能确认上次提交状态')
    expect(wrapper.findAll('.copilot-error-btn.is-retry').some(button => button.attributes('disabled') === undefined)).toBe(true)
  } finally { wrapper.unmount() }
})

it('核对请求返回前切换会话，不将旧图片或旧请求发往新会话', async () => {
  const wrapper = mountCopilot()
  try {
    const payload = await failFirstAdminMessage(wrapper)
    const readsBeforeRetry = sessionApi.get.mock.calls.length
    let resolveCheck!: (value: unknown) => void
    sessionApi.get.mockReturnValueOnce(new Promise(resolve => { resolveCheck = resolve }))
    void wrapper.find('.copilot-error-btn.is-retry').trigger('click')
    await flushPromises()
    expect(sessionApi.get.mock.calls.slice(readsBeforeRetry)).toContainEqual(['admin', payload.session_id])
    expect(wrapper.text()).toContain('正在核对上次提交状态')
    const vm = wrapper.vm as unknown as { handleSessionSelect: (id: string) => Promise<void> }
    await vm.handleSessionSelect('different-session')
    await flushSessionRestore()
    resolveCheck({ surface: 'admin', session_id: payload.session_id, run: null, messages: [], pending: null })
    await flushPromises()
    expect(streams.records).toHaveLength(1)
    expect(wrapper.find('.copilot-error-btn.is-retry').exists()).toBe(false)
  } finally { wrapper.unmount() }
})


it('核对仅返回旧的已完成运行时重发原提交，不把旧run当成本次已接收', async () => {
  sessionApi.get.mockResolvedValue({ surface: 'admin', session_id: 'admin-test',
    run: { run_id: 'previous-completed', status: 'completed', model: 'model', rounds: 1, error: '', updated_at: new Date().toISOString() },
    messages: [], events: [], pending: null })
  const wrapper = mountCopilot()
  try {
    const payload = await failFirstAdminMessage(wrapper)
    void wrapper.find('.copilot-error-btn.is-retry').trigger('click')
    await flushPromises()
    expect(streams.records).toHaveLength(2)
    expect(streams.records[1].body).toEqual(payload)
  } finally { wrapper.unmount() }
})

it('重复点击提交核对只发一个GET，卸载后的核对结果不再创建运行', async () => {
  const wrapper = mountCopilot()
  await failFirstAdminMessage(wrapper)
  let resolveCheck!: (value: unknown) => void
  sessionApi.get.mockReturnValueOnce(new Promise(resolve => { resolveCheck = resolve }))
  const before = sessionApi.get.mock.calls.length
  const retry = wrapper.find('.copilot-error-btn.is-retry')
  void retry.trigger('click')
  void retry.trigger('click')
  await flushPromises()
  expect(sessionApi.get).toHaveBeenCalledTimes(before + 1)
  wrapper.unmount()
  resolveCheck({ surface: 'admin', session_id: 'admin-test', run: null, messages: [], pending: null })
  await flushPromises()
  expect(streams.records).toHaveLength(1)
})

it.each(['描述合成蓝圆', ''])('管理员恢复图片历史可显示附件，且不伪造消息时间（正文=%s）', async (content) => {
  sessionApi.get.mockResolvedValue({
    surface: 'admin', session_id: 'admin-test',
    run: {
      run_id: 'run-admin-history-image', status: 'completed', model: 'vision',
      rounds: 1, error: '', updated_at: '2026-09-12T16:33:00',
    },
    messages: [
      { role: 'user', content, image_assets: [{ id: 12, mime: 'image/png', sha256: 'a'.repeat(64) }] },
      { role: 'assistant', content: '图中是蓝色圆形' },
    ],
    pending: null,
  })
  const wrapper = mountCopilot()
  await openCopilot(wrapper)
  await flushSessionRestore()

  const image = wrapper.findComponent({ name: 'AuthenticatedChatImage' })
  expect(image.exists()).toBe(true)
  expect(image.props('assetId')).toBe(12)
  expect(wrapper.findAll('.message-row time')).toHaveLength(0)
  expect(wrapper.text()).not.toContain('[图片]')

  await wrapper.find('textarea').setValue('只回复验收完成')
  void wrapper.find('.send-button').trigger('click')
  await flushPromises()
  const history = streams.records[0].body.messages as Array<Record<string, unknown>>
  expect(history.at(-1)?.content).toBe('只回复验收完成')
  expect(history.every(message => message.images === undefined && message.image_assets === undefined)).toBe(true)
  expect(JSON.stringify(history)).not.toContain('blob:')
  wrapper.unmount()
})

describe('AdminCopilot 图片入口布局', () => {
  it('添加图片按钮收进 composer 行动区,与发送按钮并列', async () => {
    const wrapper = mountCopilot()
    await openCopilot(wrapper)
    await flushSessionRestore()

    const composer = wrapper.find('.composer')
    expect(composer.exists()).toBe(true)
    const uploadBtn = composer.find('.image-upload-button')
    expect(uploadBtn.exists()).toBe(true)
    expect(uploadBtn.attributes('aria-label')).toBe('添加图片')
    expect(uploadBtn.text()).toContain('添加图片')
    expect(uploadBtn.element.parentElement).toBe(composer.element)
    expect(composer.find('.send-button').exists() || composer.find('.stop-button').exists()).toBe(true)
    wrapper.unmount()
  })
})


it('管理端切换账号立即清空私密消息及未发送草稿', async () => {
  const wrapper = mountCopilot()
  const user = useUserStore()
  user.profile = { id: 101, username: 'admin-a', role: 'admin', status: 1 }
  await openCopilot(wrapper)
  await flushSessionRestore()
  await wrapper.find('textarea').setValue('管理账号 A 私密问题')
  await wrapper.find('.send-button').trigger('click')
  await flushPromises()
  emit(0, { type: 'response.output_text.delta', delta: '管理账号 A 私密答复' })
  await flushPromises()
  user.profile = { id: 202, username: 'admin-b', role: 'admin', status: 1 }
  await flushPromises()
  expect(wrapper.text()).not.toContain('管理账号 A 私密')
  expect(streams.records[0].aborted).toBe(true)
  emit(0, { type: 'response.sensitive.result', values: ['旧账号密钥'], title: '迟到敏感结果' })
  await finish(0)
  expect(wrapper.text()).not.toContain('旧账号密钥')
  wrapper.unmount()
})

it('管理员surface打开时收起成员surface的小菱会话，反向切换也能关闭', async () => {
  const opened = vi.fn()
  window.addEventListener('prism:admin-copilot-opened', opened)
  const wrapper = mountCopilot()
  await openCopilot(wrapper)
  expect(opened).toHaveBeenCalledOnce()
  expect(wrapper.find('.copilot-panel').exists()).toBe(true)

  window.dispatchEvent(new Event('prism:close-admin-copilot'))
  await flushPromises()
  expect(wrapper.find('.copilot-panel').exists()).toBe(false)
  expect(document.querySelector('.copilot-trigger')).not.toBeNull()
  wrapper.unmount()
  window.removeEventListener('prism:admin-copilot-opened', opened)
})


describe('实时星图滚动跟随', () => {
  it('靠近底部时跟随账本更新，向上阅读时保留位置', async () => {
    const wrapper = mountCopilot()
    const vm = wrapper.vm as any
    vm.visible = true
    await flushPromises()
    await flushSessionRestore()
    await flushPromises()
    try {
      const area = (wrapper.vm as any).messageArea as HTMLElement
      expect(area).toBeInstanceOf(HTMLElement)
      Object.defineProperty(area, 'scrollHeight', { configurable: true, value: 1000 })
      Object.defineProperty(area, 'clientHeight', { configurable: true, value: 400 })
      area.scrollTop = 600
      vm.agentTeams = [...vm.agentTeams]
      await flushPromises()
      expect(area.scrollTop).toBe(1000)
      area.scrollTop = 100
      vm.agentTeams = [...vm.agentTeams]
      await flushPromises()
      expect(area.scrollTop).toBe(100)
    } finally {
      wrapper.unmount()
    }
  })
})


it('精简欢迎语后仍识别旧版欢迎语，历史回答保持完整', async () => {
  sessionApi.get.mockResolvedValue({
    surface: 'admin', session_id: 'admin-test', run: null, pending: null,
    messages: [
      { role: 'assistant', content: `你好,我是小菱,Prism 的唯一主控 Agent。我会按当前登录账号权限处理代码审查、安全审计、Agent 团队协作、审批与管理运维;子 Agent 只负责我派发的专项任务,写操作仍走现有审批流程。点击「+」可开新对话,多个任务并行处理。` },
      { role: 'user', content: '查看我的项目' },
      { role: 'assistant', content: '历史回答：共找到两个项目' },
    ],
  })
  const wrapper = mountCopilot()
    await openCopilot(wrapper)
    await flushSessionRestore()
  try {
    expect(wrapper.text()).not.toContain('Prism 的唯一主控 Agent')
    expect(wrapper.text()).toContain('历史回答：共找到两个项目')
    expect(wrapper.findAll('.message-row')).toHaveLength(3)
  } finally { wrapper.unmount() }
})


it.each(['pending', 'claim'] as const)('停止真实接线：%s 迟到及停止失败不执行页面动作，显式重试才恢复', async (stage) => {
  vi.useFakeTimers()
  const { createRouter, createMemoryHistory } = await import('vue-router')
  const router = createRouter({ history: createMemoryHistory(), routes: [{ path: '/', component: { template: '<main />' } }] })
  await router.push('/')
  await router.isReady()
  const pending = vi.spyOn(siteBrowserApi, 'pending').mockResolvedValue({ actions: [] })
  const claim = vi.spyOn(siteBrowserApi, 'claim').mockImplementation(async id => ({ action_id: id, claim_token: 'claim-token' }))
  const ack = vi.spyOn(siteBrowserApi, 'ack').mockResolvedValue({})
  let vm: any
  sessionApi.get.mockImplementation(async () => ({
    surface: 'admin', session_id: vm?.sessionId || 'admin-test', run: vm?.sessionRun || null,
    messages: [], pending: null,
  }))
  const wrapper = mountCopilot([router])
  await openCopilot(wrapper)
  await vi.advanceTimersByTimeAsync(0)
  await flushPromises()
  try {
    vm = wrapper.vm as any
    const user = useUserStore()
    user.profile = { id: 707, username: 'bridge-owner', role: 'admin', status: 1 }
    user.token = 'bridge-component-test-token'
    await flushPromises()
    await vi.advanceTimersByTimeAsync(0)
    await flushPromises()
    const action: SiteBrowserRequiredEvent = {
      type: 'response.browser.required', action_id: 81, run_id: 'run-component-stop',
      session_id: vm.sessionId, surface: 'admin', account_id: 707, client_nonce: 'nonce-stop',
      action: 'inspect', args: {}, observation_snapshot_id: 'snapshot-stop',
      expires_at: new Date(Date.now() + 60000).toISOString(),
    }
    const delayed = deferred<any>()
    if (stage === 'pending') pending.mockReturnValueOnce(delayed.promise)
    else {
      pending.mockResolvedValueOnce({ actions: [action] })
      claim.mockReturnValueOnce(delayed.promise)
    }
    vm.sessionRun = { run_id: action.run_id, status: 'running', model: '', rounds: 1, error: '', updated_at: '' }
    await flushPromises()
    expect(pending).toHaveBeenCalled()
    if (stage === 'claim') expect(claim).toHaveBeenCalledOnce()

    let rejectCancel!: (reason: Error) => void
    sessionApi.cancel.mockReturnValueOnce(new Promise((_resolve, reject) => { rejectCancel = reject }))
    const cancelling = vm.cancelResponse('人工停止页面操作')
    delayed.resolve(stage === 'pending' ? { actions: [action] } : { action_id: 81, claim_token: 'claim-token' })
    await flushPromises()
    await vi.advanceTimersByTimeAsync(100)
    expect(claim).toHaveBeenCalledTimes(stage === 'claim' ? 1 : 0)
    expect(ack).not.toHaveBeenCalled()

    rejectCancel(new Error('取消接口暂时断开'))
    await cancelling
    await flushPromises()
    expect(vm.sessionRun.status).toBe('running')
    const callsAfterStop = pending.mock.calls.length
    vm.siteBrowser.handle({ ...action, action_id: 82 })
    await vi.advanceTimersByTimeAsync(2100)
    expect(pending).toHaveBeenCalledTimes(callsAfterStop)
    expect(claim).toHaveBeenCalledTimes(stage === 'claim' ? 1 : 0)
    expect(ack).not.toHaveBeenCalled()

    // 后续同步失败终态也不自动撤销停止；只有用户重试可以恢复此 run。
    vm.sessionRun = { ...vm.sessionRun, status: 'failed' }
    await flushPromises()
    expect(vm.isResponseRunResumeBlocked(action.run_id)).toBe(true)
    void vm.retryRun()
    await flushPromises()
    expect(streams.records.at(-1)?.body.action).toBe('retry')
    expect(vm.isResponseRunResumeBlocked(action.run_id)).toBe(false)
    const streamIndex = streams.records.length - 1
    emit(streamIndex, { type: 'response.created', response: { id: action.run_id } })
    await flushPromises()
    vm.siteBrowser.handle({ ...action, action_id: 83 })
    await vi.advanceTimersByTimeAsync(100)
    await flushPromises()
    expect(claim).toHaveBeenLastCalledWith(83, expect.objectContaining({ surface: 'admin' }))
    expect(ack).toHaveBeenCalledWith(83, expect.objectContaining({ outcome: 'completed' }))
    emit(streamIndex, { type: 'response.completed', response: { id: action.run_id } })
    await finish(streamIndex)
  } finally {
    wrapper.unmount()
    vi.useRealTimers()
  }
})
