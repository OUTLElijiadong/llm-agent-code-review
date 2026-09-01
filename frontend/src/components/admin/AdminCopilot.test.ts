import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const streams = vi.hoisted(() => ({
  start: vi.fn(),
  records: [] as Array<{
    body: Record<string, unknown>
    onEvent: (event: unknown) => void
    resolve: () => void
    aborted: boolean
  }>,
}))

const messages = vi.hoisted(() => ({ error: vi.fn(), info: vi.fn(), success: vi.fn(), warning: vi.fn() }))
const sessionApi = vi.hoisted(() => ({ get: vi.fn(), cancel: vi.fn() }))
const meshApi = vi.hoisted(() => ({ heartbeat: vi.fn(), inbox: vi.fn(), list: vi.fn() }))
const teamApi = vi.hoisted(() => ({ list: vi.fn(), detail: vi.fn(), messages: vi.fn(), events: vi.fn() }))

vi.mock('@/utils/responsesStream', () => ({ streamResponses: streams.start }))
vi.mock('@/api/agentResponses', () => ({ getAgentResponseSession: sessionApi.get, cancelAgentResponseRun: sessionApi.cancel }))
vi.mock('@/api/agentMesh', () => ({
  heartbeatAgentMesh: meshApi.heartbeat,
  pullAgentMeshInbox: meshApi.inbox,
  listAgentMeshAgents: meshApi.list,
  archiveAgentMeshSession: vi.fn().mockResolvedValue({ session_id: '', status: 'archived' }),
}))
vi.mock('@/api/agentTeams', () => ({
  listAgentTeams: teamApi.list,
  getAgentTeam: teamApi.detail,
  listAgentTeamMessages: teamApi.messages,
  listAgentTeamEvents: teamApi.events,
}))
vi.mock('element-plus/es/components/message/index', () => ({ ElMessage: messages }))

import { createPinia, setActivePinia } from 'pinia'

import AdminCopilot from './AdminCopilot.vue'
import { useAgentActivityStore } from '@/stores/agentActivity'

function flushSessionRestore(): Promise<void> {
  return new Promise((resolve) => {
    setTimeout(() => {
      void flushPromises().then(() => setTimeout(resolve, 0))
    }, 0)
  })
}

function mountCopilot(): VueWrapper {
  const pinia = createPinia()
  setActivePinia(pinia)
  return mount(AdminCopilot, {
    global: {
      plugins: [pinia],
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
}

async function openCopilot(wrapper: VueWrapper): Promise<void> {
  await wrapper.find('.copilot-trigger').trigger('click')
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
  teamApi.list.mockReset().mockResolvedValue({ items: [], total: 0 })
  teamApi.detail.mockReset()
  teamApi.events.mockReset().mockResolvedValue({
    items: [], has_more: false, next_after_id: 0, page_size: 200, team_status: 'running',
  })
  sessionApi.get.mockReset()
  sessionApi.cancel.mockReset().mockResolvedValue(undefined)
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
    const done = new Promise<void>((doneResolve) => { resolve = doneResolve })
    const record = { body, onEvent: options.onEvent, resolve, aborted: false }
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

afterEach(() => {
  vi.useRealTimers()
})

describe('AdminCopilot Responses stream', () => {
  it('停止请求失败时保持服务端运行并给出同步提示', async () => {
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

    expect(streams.records[0].aborted).toBe(false)
    expect(wrapper.find('.stop-button').exists()).toBe(true)
    expect(messages.error).toHaveBeenCalledWith('停止请求未确认：执行器连接中断')
    expect(wrapper.find('.copilot-error-card').text()).toContain('停止请求未确认')
    expect(wrapper.find('.copilot-error-card').text()).toContain('trace-admin-cancel')
    expect(wrapper.text()).not.toContain('已停止任务，未执行剩余操作')
    wrapper.unmount()
  })

  it('创建团队后主响应结束也持续展示真实子 Agent 协作城市', async () => {
    teamApi.detail.mockResolvedValue({
      team_id: 88, title: '管理端实时协作', surface: 'admin', session_id: 'admin-test', status: 'running',
      max_active_children: 3, trace_id: 'trace-admin-88',
      counts: { total: 1, completed: 0, running: 1, queued: 0, failed: 0, blocked: 0 },
      members: [], tasks: [], events: [], messages: [],
    })
    const wrapper = mountCopilot()
    await openCopilot(wrapper)
    await flushSessionRestore()

    await wrapper.find('textarea').setValue('请创建子 Agent 团队')
    void wrapper.find('.send-button').trigger('click')
    await flushPromises()
    emit(0, { type: 'response.created', response: { id: 'run-admin-team', model: 'deepseek-v4-pro' } })
    emit(0, { type: 'response.tool.completed', call_id: 'call-admin-team', tool_name: 'create_agent_team', output: { team_id: 88 } })
    await flushPromises()

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
    expect(rows[0].text()).toContain('我是贾维斯')
    expect(rows[1].classes()).toContain('is-user')
    await expandTimeline(wrapper)
    expect(rows[2].find('.xl-steps').text()).toContain('贾维斯的工作')
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
      messages: [{ role: 'user', content: '删除两个测试用户' }],
    })
    await finish(0)
  })

  it('opens empty and renders only the first meaningful streamed delta', async () => {
    const wrapper = mountCopilot()
    await openCopilot(wrapper)
    await flushSessionRestore()
    await flushPromises()

    expect(wrapper.findAll('.message-row')).toHaveLength(1)
    expect(wrapper.text()).toContain('我是贾维斯')
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
    expect(history.slice(-3)).toEqual([
      { role: 'user', content: '检查生产状态' },
      { role: 'assistant', content: '第一段\n第二段' },
      { role: 'user', content: '继续检查' },
    ])
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
    expect(approveMessages[approveMessages.length - 1]).toEqual({ role: 'user', content: '删除测试用户' })
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
    expect(nextMessages.slice(-3)).toEqual([
      { role: 'user', content: '生成一个内测码' },
      { role: 'assistant', content: '已生成 1 个内测码' },
      { role: 'user', content: '查询内测码列表' },
    ])
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
        return this.classList.contains('copilot-panel') ? width : 0
      })
    const heightSpy = vi.spyOn(HTMLElement.prototype, 'offsetHeight', 'get')
      .mockImplementation(function (this: HTMLElement) {
        return this.classList.contains('copilot-panel') ? height : 0
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
