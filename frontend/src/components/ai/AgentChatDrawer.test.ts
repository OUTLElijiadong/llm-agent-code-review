import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

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

const messages = vi.hoisted(() => ({ error: vi.fn(), warning: vi.fn() }))
const sessionApi = vi.hoisted(() => ({ get: vi.fn() }))
const meshApi = vi.hoisted(() => ({ heartbeat: vi.fn(), inbox: vi.fn(), list: vi.fn() }))
const teamApi = vi.hoisted(() => ({ list: vi.fn(), detail: vi.fn(), messages: vi.fn(), events: vi.fn() }))

vi.mock('@/utils/responsesStream', () => ({ streamResponses: streams.start }))
vi.mock('@/api/agentResponses', () => ({ getAgentResponseSession: sessionApi.get }))
vi.mock('@/api/agentMesh', () => ({
  heartbeatAgentMesh: meshApi.heartbeat,
  pullAgentMeshInbox: meshApi.inbox,
  listAgentMeshAgents: meshApi.list,
}))
vi.mock('@/api/agentTeams', () => ({
  listAgentTeams: teamApi.list,
  getAgentTeam: teamApi.detail,
  listAgentTeamMessages: teamApi.messages,
  listAgentTeamEvents: teamApi.events,
}))
vi.mock('element-plus/es/components/message/index', () => ({ ElMessage: messages }))

import AgentChatDrawer from './AgentChatDrawer.vue'

function mountDrawer(prefill?: string): VueWrapper {
  return mount(AgentChatDrawer, {
    props: { visible: true, prefill },
    global: {
      stubs: {
        Teleport: true,
        Transition: false,
        'el-icon': { template: '<span class="el-icon-stub"><slot /></span>' },
        'el-button': { template: '<button><slot /></button>' },
        'el-input': true,
        'el-input-number': true,
        'el-option': true,
        'el-select': true,
        AgentAvatar: true,
        AgentNavLink: { props: ['href', 'label', 'hint', 'prominent'], template: '<button class="agent-nav-link-stub">{{ label }}</button>' },
        Check: true,
        CircleCheck: true,
        CircleCloseFilled: true,
        Close: true,
        Connection: true,
        CopyDocument: true,
        Loading: true,
        Promotion: true,
        WarningFilled: true,
      },
    },
  })
}

function emit(index: number, event: Record<string, unknown>): void {
  streams.records[index].onEvent(event)
}

async function finish(index: number): Promise<void> {
  streams.records[index].resolve()
  await flushPromises()
}

/** 模拟流异常中断(网络错误等),复现 fetch 层的 AbortError 拒绝。 */
async function failStream(index: number, error: unknown): Promise<void> {
  streams.records[index].reject(error)
  await flushPromises()
  // 等待 runResponse 的 finally 收尾(loading 复位、错误卡片入流)
  await flushPromises()
}

/** 等所有微任务与一轮宏任务跑完(setTimeout 回调、watch 后置刷新等)。 */
async function settleAll(): Promise<void> {
  await flushPromises()
  await new Promise<void>((resolve) => { setTimeout(resolve, 0) })
  await flushPromises()
}

beforeEach(() => {
  window.localStorage.clear()
  window.sessionStorage.clear()
  // 组件在 setup 顶层使用 useAgentActivityStore,测试环境需先激活 Pinia
  setActivePinia(createPinia())
  meshApi.heartbeat.mockReset().mockResolvedValue({})
  meshApi.inbox.mockReset().mockResolvedValue([])
  meshApi.list.mockReset().mockResolvedValue({ items: [], total: 0, by_kind: {} })
  teamApi.list.mockReset().mockResolvedValue({ items: [], total: 0 })
  teamApi.detail.mockReset()
  teamApi.events.mockReset().mockResolvedValue({
    items: [], has_more: false, next_after_id: 0, page_size: 200, team_status: 'running',
  })
  sessionApi.get.mockReset()
  sessionApi.get.mockResolvedValue({
    surface: 'user', session_id: 'user-test', run: null, messages: [], pending: null,
  })
  streams.start.mockReset()
  streams.records.splice(0)
  streams.start.mockImplementation((
    body: Record<string, unknown>,
    options: { onEvent: (event: unknown) => void },
  ) => {
    const record = {
      body,
      onEvent: options.onEvent,
      resolve: (): void => undefined,
      reject: (): void => undefined,
      aborted: false,
    }
    const done = new Promise<void>((doneResolve, doneReject) => {
      record.resolve = doneResolve
      record.reject = doneReject
    })
    // 测试自行处理拒绝场景,避免未处理的 Promise 告警
    done.catch(() => undefined)
    streams.records.push(record)
    return {
      abort: () => {
        record.aborted = true
        const error = new Error('The operation was aborted')
        error.name = 'AbortError'
        ;(record as { reject: (error: unknown) => void }).reject(error)
      },
      signal: new AbortController().signal,
      done,
    }
  })
})

afterEach(() => {
  vi.useRealTimers()
})

async function mountReadyDrawer(prefill?: string): Promise<VueWrapper> {
  const wrapper = mountDrawer(prefill)
  await flushPromises()
  return wrapper
}

/** 整块调用链默认折叠,断言前先展开。 */
/** 调用链默认折叠;需要断言展开详情的用例自行点击 .xl-step-line。 */
async function expandTimeline(_wrapper: VueWrapper): Promise<void> {
  // no-op:保留给历史用例的兼容入口
}

describe('AgentChatDrawer Responses stream', () => {
  it('后台历史会话完成 Mesh 任务后不抢占当前新对话', async () => {
    window.localStorage.setItem('prism-agent-sessions:user', JSON.stringify([
      { id: 'user-current', title: '新对话', createdAt: 2 },
      { id: 'user-history', title: '历史审计', createdAt: 1 },
    ]))
    window.localStorage.setItem('prism-agent-active-session:user', 'user-current')
    meshApi.list.mockResolvedValue({
      items: [
        { kind: 'session', session_id: 'user-current', surface: 'user', name: '新对话', last_seen_at: '2026-08-24T00:00:00Z' },
        { kind: 'session', session_id: 'user-history', surface: 'user', name: '历史审计', last_seen_at: '2026-08-23T00:00:00Z' },
      ],
      total: 2,
      by_kind: { session: 2 },
    })
    meshApi.inbox.mockImplementation((_surface: string, sessionId: string) => Promise.resolve(
      sessionId === 'user-history'
        ? [{
            schema_version: '1.0', message_id: 'msg-user-background', idempotency_key: 'idem-user-background',
            trace_id: 'trace-user-background', correlation_id: 'corr-user-background', causation_id: '',
            sent_from: 'agent:security', send_to: 'session:user:user-history', message_type: 'task.result',
            priority: 'normal', subject: '后台审计完成', status: 'delivered', payload: {}, context: {},
            artifacts: [], errors: [], requires_ack: true, max_attempts: 3, attempt_count: 1,
            expires_at: '2026-08-24T01:00:00Z', create_time: '2026-08-24T00:00:00Z', update_time: '2026-08-24T00:00:01Z',
          }]
        : [],
    ))

    const wrapper = await mountReadyDrawer()
    await settleAll()
    expect(streams.records).toHaveLength(1)
    expect(streams.records[0].body.session_id).toBe('user-history')
    expect(wrapper.find('.session-current').text()).toContain('新对话')

    emit(0, { type: 'response.completed', response: { id: 'run-user-background' } })
    await finish(0)
    expect(wrapper.find('.session-current').text()).toContain('新对话')
    wrapper.unmount()
  })

  it('运行中调用 create_agent_team 时立即显示团队卡片', async () => {
    const wrapper = await mountReadyDrawer()
    teamApi.list.mockResolvedValue({
      items: [{ team_id: 44, title: '实时白盒团队', surface: 'user', session_id: 'user-test', status: 'running', max_active_children: 3, trace_id: 'trace-44', counts: { total: 2, completed: 0, running: 2, queued: 0, failed: 0, blocked: 0 } }],
      total: 1,
    })
    teamApi.detail.mockResolvedValue({
      team_id: 44, title: '实时白盒团队', surface: 'user', session_id: 'user-test', status: 'running', max_active_children: 3, trace_id: 'trace-44',
      counts: { total: 2, completed: 0, running: 2, queued: 0, failed: 0, blocked: 0 }, members: [], tasks: [], events: [], messages: [],
    })

    await wrapper.find('.chat-input').setValue('请组建团队检查白盒测试')
    void wrapper.find('.send-btn').trigger('click')
    await flushPromises()
    emit(0, { type: 'response.created', response: { id: 'run-live-team', model: 'deepseek-v4-flash' } })
    emit(0, { type: 'response.tool.started', call_id: 'call-team', tool_name: 'create_agent_team', arguments: { objective: '检查白盒测试' } })
    await settleAll()

    expect(wrapper.find('.agent-team-trace').exists()).toBe(true)
    expect(wrapper.text()).toContain('实时白盒团队')
    expect(wrapper.text()).not.toContain('最终结论')

    emit(0, { type: 'response.tool.completed', call_id: 'call-team', tool_name: 'create_agent_team', output_summary: '团队已创建' })
    await settleAll()
    expect(wrapper.find('.thinking-city').exists()).toBe(true)
    expect(wrapper.text()).toContain('子 Agent 正在协作')
    emit(0, { type: 'response.output_text.delta', delta: '最终结论' })
    emit(0, { type: 'response.completed', response: { id: 'run-live-team' } })
    await finish(0)
    wrapper.unmount()
  })

  it('团队卡片锚定在调用时间线,不会排到最终结论之后', async () => {
    sessionApi.get.mockResolvedValueOnce({
      surface: 'user',
      session_id: 'user-test',
      run: { run_id: 'run-team-order', status: 'completed', model: 'deepseek-v4-flash', rounds: 1, error: '', updated_at: '2026-08-01T12:00:00Z' },
      messages: [
        { role: 'user', content: '启动协作审查' },
        { role: 'assistant', content: '最终结论已经生成' },
      ],
      events: [],
      mesh_messages: [],
      pending: null,
    })
    teamApi.list.mockResolvedValueOnce({
      items: [{ team_id: 43, title: '白盒核验团队', surface: 'user', session_id: 'user-test', status: 'completed', max_active_children: 3, trace_id: 'trace-43', counts: { total: 1, completed: 1, running: 0, queued: 0, failed: 0, blocked: 0 } }],
      total: 1,
    })
    teamApi.detail.mockResolvedValueOnce({
      team_id: 43, title: '白盒核验团队', surface: 'user', session_id: 'user-test', status: 'completed', max_active_children: 3, trace_id: 'trace-43',
      counts: { total: 1, completed: 1, running: 0, queued: 0, failed: 0, blocked: 0 }, members: [], tasks: [], events: [], messages: [],
    })

    const wrapper = await mountReadyDrawer()
    const rows = wrapper.findAll('.msg-row')
    const teamRowIndex = rows.findIndex((row) => row.find('.agent-team-trace').exists())
    const conclusionIndex = rows.findIndex((row) => row.text().includes('最终结论已经生成'))

    expect(teamRowIndex).toBeGreaterThanOrEqual(0)
    expect(conclusionIndex).toBeGreaterThan(teamRowIndex)
    wrapper.unmount()
  })

  it('恢复当前会话时拉取服务端团队并默认折叠展示协作过程', async () => {
    teamApi.list.mockResolvedValue({
      items: [{ team_id: 42, title: '发布前验证', surface: 'user', session_id: 'user-test', status: 'running', max_active_children: 3, trace_id: 'trace-42', counts: { total: 1, completed: 0, running: 1, queued: 0, failed: 0, blocked: 0 } }],
      total: 1,
    })
    teamApi.detail.mockResolvedValue({
      team_id: 42, title: '发布前验证', surface: 'user', session_id: 'user-test', status: 'running', max_active_children: 3, trace_id: 'trace-42',
      counts: { total: 1, completed: 0, running: 1, queued: 0, failed: 0, blocked: 0 }, members: [], tasks: [], events: [], messages: [],
    })
    const wrapper = await mountReadyDrawer()

    expect(teamApi.list).toHaveBeenCalledWith(expect.objectContaining({ surface: 'user', limit: 20 }))
    expect(wrapper.find('.agent-team-trace').exists()).toBe(true)
    expect(wrapper.find('.agent-team-trace-body').exists()).toBe(false)
    wrapper.unmount()
  })

  it('刷新恢复后仍先显示工具调用，再显示最终结论', async () => {
    sessionApi.get.mockResolvedValueOnce({
      surface: 'user',
      session_id: 'user-test',
      run: {
        run_id: 'run-restored-tools', status: 'completed', model: 'deepseek-v4-flash',
        rounds: 1, error: '', updated_at: '2026-08-01T12:00:00Z',
      },
      messages: [
        { role: 'user', content: '列出项目' },
        { role: 'assistant', content: '共找到 2 个项目。\n\n<wbr>\n    •已完成。' },
      ],
      events: [
        {
          type: 'response.tool.completed', sequence_number: 2, call_id: 'call-projects',
          tool_name: 'list_projects', agent_code: 'project_agent', output_summary: '返回 2 个项目',
        },
        {
          type: 'response.tool.started', sequence_number: 1, call_id: 'call-projects',
          tool_name: 'list_projects', agent_code: 'project_agent', arguments: { page: 1 },
        },
      ],
      last_sequence_number: 2,
      pending: null,
    })

    const wrapper = await mountReadyDrawer()
    const rows = wrapper.findAll('.msg-row')

    // 首条为吉祥物欢迎语,其后才是恢复的历史消息
    expect(rows).toHaveLength(4)
    expect(rows[0].text()).toContain('我是小菱')
    expect(rows[1].classes()).toContain('user')
    await expandTimeline(wrapper)
    expect(rows[2].find('.xl-steps').text()).toContain('查看项目列表')
    expect(rows[2].text()).toContain('做好了')
    expect(rows[3].find('.markdown-body').text()).toContain('共找到 2 个项目')
    expect(rows[3].find('.markdown-body').text()).toContain('已完成。')
    expect(rows[3].find('.markdown-body').text()).not.toMatch(/<wbr>|•/)
    wrapper.unmount()
  })

  it('初始会话恢复完成前禁止发送', async () => {
    let resolveSession!: (value: unknown) => void
    sessionApi.get.mockReturnValueOnce(new Promise((resolve) => { resolveSession = resolve }))
    const wrapper = mountDrawer()

    expect(wrapper.find('.chat-input').attributes('disabled')).toBeDefined()
    await wrapper.find('.chat-input').setValue('不应提前发送')
    await wrapper.find('.send-btn').trigger('click')
    expect(streams.start).not.toHaveBeenCalled()

    resolveSession({ surface: 'user', session_id: 'user-test', run: null, messages: [], pending: null })
    await flushPromises()
    expect(wrapper.find('.chat-input').attributes('disabled')).toBeUndefined()
    wrapper.unmount()
  })

  it('恢复运行态后轮询到动态追问仍按低频间隔继续刷新', async () => {
    vi.useFakeTimers()
    sessionApi.get
      .mockResolvedValueOnce({
        surface: 'user', session_id: 'user-test',
        run: { run_id: 'run-active', status: 'running', model: 'deepseek-v4-flash', rounds: 1, error: '', updated_at: '' },
        messages: [{ role: 'user', content: '查找 Agent' }], pending: null,
      })
      .mockResolvedValueOnce({
        surface: 'user', session_id: 'user-test',
        run: { run_id: 'run-active', status: 'waiting_input', model: 'deepseek-v4-flash', rounds: 1, error: '', updated_at: '' },
        messages: [{ role: 'user', content: '查找 Agent' }],
        pending: {
          type: 'response.input.required', run_id: 'run-active', call_id: 'call-active',
          question: '你指的是哪个 Agent？', options: ['安全审查', '代码质量'], allow_free_text: false,
        },
      })
    const wrapper = await mountReadyDrawer()
    await flushPromises()
    expect(sessionApi.get).toHaveBeenCalledTimes(1)

    await vi.advanceTimersByTimeAsync(1000)
    await flushPromises()
    expect(sessionApi.get).toHaveBeenCalledTimes(2)
    expect(wrapper.text()).toContain('你指的是哪个 Agent？')

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
      schema_version: '1.0', message_id: 'msg-user-live', idempotency_key: 'idem-user-live',
      trace_id: 'trace-user-live', correlation_id: 'corr-user-live', causation_id: '',
      sent_from: 'agent:data-analysis', send_to: 'session:user:user-test',
      message_type: 'task.result', priority: 'normal', subject: '数据分析完成',
      status: 'completed', payload: { anomaly_count: 2 }, context: {}, artifacts: [], errors: [],
      requires_ack: true, max_attempts: 3, attempt_count: 1,
      expires_at: '2026-08-10T12:10:00Z', create_time: '2026-08-10T12:00:00Z',
      update_time: '2026-08-10T12:00:01Z',
    }
    sessionApi.get
      .mockResolvedValueOnce({
        surface: 'user', session_id: 'user-test', run: null, messages: [],
        mesh_messages: [], pending: null,
      })
      .mockResolvedValue({
        surface: 'user', session_id: 'user-test', run: null, messages: [],
        mesh_messages: [meshMessage], pending: null,
      })
    const wrapper = mountDrawer()
    await vi.advanceTimersByTimeAsync(0)
    await flushPromises()

    expect(wrapper.find('.xl-steps').exists()).toBe(false)
    await vi.advanceTimersByTimeAsync(3000)
    await flushPromises()

    const timeline = wrapper.find('.xl-steps')
    expect(timeline.text()).not.toContain('receive_message')
    expect(timeline.text()).not.toContain('agent:data-analysis')
    expect(timeline.text()).toContain('数据分析完成')
    // 新设计:无技术折叠头;通俗明细直接可见且不含内部标识
    expect(timeline.find('.response-tool-detail').exists()).toBe(false)

    // 完成的步骤不渲染明细行(新设计只在失败时展示原因)
    expect(timeline.find('.xl-step-error').exists()).toBe(false)
    wrapper.unmount()
    vi.useRealTimers()
  })

  it('轮询失败时显示同步中断并在下一轮恢复', async () => {
    vi.useFakeTimers()
    sessionApi.get
      .mockResolvedValueOnce({ surface: 'user', session_id: 'user-test', run: null, messages: [], pending: null })
      .mockRejectedValueOnce(new Error('network down'))
      .mockResolvedValue({ surface: 'user', session_id: 'user-test', run: null, messages: [], pending: null })
    const wrapper = await mountReadyDrawer()

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

  it('刷新后恢复动态追问,点选候选后再点提交才续跑', async () => {
    sessionApi.get.mockResolvedValueOnce({
      surface: 'user',
      session_id: 'user-test',
      run: {
        run_id: 'run-user-restored', status: 'waiting_input', model: 'deepseek-v4-flash',
        rounds: 1, error: '', updated_at: '2026-07-31T12:00:00Z',
      },
      messages: [{ role: 'user', content: '调用安全审查 agent' }],
      pending: {
        type: 'response.input.required',
        run_id: 'run-user-restored',
        call_id: 'call-choice',
        question: '你指的是哪个 Agent？',
        options: [
          { label: '安全审查 Agent', value: 'agent-safe' },
          { label: '代码质量 Agent', value: 'agent-quality' },
        ],
        allow_free_text: false,
      },
    })
    const wrapper = await mountReadyDrawer()
    await flushPromises()

    expect(wrapper.text()).toContain('你指的是哪个 Agent？')
    expect(wrapper.findAll('.response-input-option')).toHaveLength(2)
    expect(wrapper.find('.response-answer').exists()).toBe(false)
    expect(wrapper.find('.chat-input').attributes('disabled')).toBeDefined()
    const submit = wrapper.find('.response-answer-submit')
    expect(submit.exists()).toBe(true)
    expect(submit.attributes('disabled')).toBeDefined()

    // 点选只高亮选中态,不立即提交;可改选,再点一次取消选择
    await wrapper.findAll('.response-input-option')[0].trigger('click')
    await flushPromises()
    expect(streams.start).not.toHaveBeenCalled()
    expect(wrapper.findAll('.response-input-option')[0].classes()).toContain('is-selected')
    await wrapper.findAll('.response-input-option')[1].trigger('click')
    await flushPromises()
    expect(wrapper.findAll('.response-input-option')[0].classes()).not.toContain('is-selected')
    expect(wrapper.findAll('.response-input-option')[1].classes()).toContain('is-selected')
    expect(streams.start).not.toHaveBeenCalled()
    await wrapper.findAll('.response-input-option')[1].trigger('click')
    await flushPromises()
    expect(wrapper.findAll('.response-input-option')[1].classes()).not.toContain('is-selected')
    expect(wrapper.find('.response-answer-submit').attributes('disabled')).toBeDefined()
    await wrapper.findAll('.response-input-option')[1].trigger('click')
    await flushPromises()

    await submit.trigger('click')
    await flushPromises()
    expect(streams.records[0].body).toMatchObject({
      action: 'answer',
      run_id: 'run-user-restored',
      call_id: 'call-choice',
      answer: 'agent-quality',
    })
    await finish(0)
  })

  it('keeps the existing prefill workflow', () => {
    const wrapper = mountDrawer('检查这段代码')
    expect((wrapper.find('.chat-input').element as HTMLTextAreaElement).value).toBe('检查这段代码')
    expect(wrapper.emitted('consumed-prefill')).toHaveLength(1)
  })

  it('keeps the empty view with only the mascot greeting, then incrementally updates one assistant bubble', async () => {
    const wrapper = await mountReadyDrawer()
    expect(wrapper.findAll('.msg-row')).toHaveLength(1)
    expect(wrapper.text()).toContain('我是小菱')

    await wrapper.find('.chat-input').setValue('审查当前项目')
    void wrapper.find('.send-btn').trigger('click')
    await flushPromises()

    expect(streams.records[0].body).toMatchObject({
      action: 'start',
      surface: 'user',
      messages: [{ role: 'user', content: '审查当前项目' }],
    })
    emit(0, { type: 'response.output_text.delta', delta: '' })
    emit(0, { type: 'response.output_text.delta', delta: '\n' })
    await flushPromises()
    expect(wrapper.findAll('.msg-row.assistant .markdown-body')).toHaveLength(1)

    emit(0, { type: 'response.output_text.delta', delta: '发现 1 个问题\n\n<wbr>\n    •正在定位' })
    await flushPromises()
    expect(wrapper.findAll('.msg-row.assistant .markdown-body')).toHaveLength(2)
    const streamed = wrapper.findAll('.msg-row.assistant .markdown-body')[1]
    expect(streamed.html()).toContain('发现 1 个问题<br>')
    expect(streamed.text()).not.toContain('<wbr>')

    emit(0, { type: 'response.output_text.delta', delta: '\n```ts\n\nconst ok = true\n```' })
    await flushPromises()

    expect(wrapper.findAll('.msg-row.assistant .markdown-body')).toHaveLength(2)
    const html = wrapper.findAll('.msg-row.assistant .markdown-body')[1].html()
    expect(html).not.toContain('<br><br>')
    expect(html).not.toContain('&lt;wbr&gt;')
    expect(html).toContain('const ok = true')

    emit(0, { type: 'response.completed', response: { id: 'run-user' } })
    await finish(0)

    await wrapper.find('.chat-input').setValue('继续检查')
    void wrapper.find('.send-btn').trigger('click')
    await flushPromises()
    expect(streams.records[1].body).toMatchObject({
      messages: [
        { role: 'user', content: '审查当前项目' },
        { role: 'assistant', content: expect.stringContaining('发现 1 个问题') },
        { role: 'user', content: '继续检查' },
      ],
    })
    expect(JSON.stringify(streams.records[1].body)).not.toContain('<wbr>')
    await finish(1)
  })

  it('导航指令完成后渲染为确认按钮,不自动跳转', async () => {
    const wrapper = await mountReadyDrawer()
    await wrapper.find('.chat-input').setValue('帮我看看审查记录')
    void wrapper.find('.send-btn').trigger('click')
    await flushPromises()

    emit(0, {
      type: 'response.output_text.delta',
      delta: '审查结果在这里,你可以点下面按钮去查看。\n\n<!--PRISM_NAVIGATE {"action":"navigate","route":"/reviews","label":"查看审查记录"}-->',
    })
    await flushPromises()
    emit(0, { type: 'response.completed', response: { id: 'run-nav' } })
    await finish(0)

    // 不自动跳转:悬浮窗不关闭、router 未被调用(组件内无 router mock,跳转会报错),
    // 指令被剥离正文并渲染为导航确认按钮
    expect(wrapper.emitted('update:visible')).toBeFalsy()
    expect(wrapper.find('.nav-directives').exists()).toBe(true)
    const navText = wrapper.find('.nav-directives').text()
    expect(navText).toContain('查看审查记录')
    // 正文里不残留 PRISM_NAVIGATE 指令
    expect(wrapper.find('.msg-row.assistant .markdown-body').text()).not.toContain('PRISM_NAVIGATE')
    wrapper.unmount()
  })

  it('resumes a non-danger approval directly without a confirmation phrase or fake reply', async () => {
    const wrapper = await mountReadyDrawer()
    await wrapper.find('.chat-input').setValue('修改项目配置')
    void wrapper.find('.send-btn').trigger('click')
    await flushPromises()

    emit(0, {
      type: 'response.approval.required',
      run_id: 'run-user-approval',
      call_id: 'call-write',
      tool_name: 'update_project',
      arguments: { project_id: 3 },
      operation: '修改项目',
      impact: '更新项目配置',
      danger: false,
      preview: { project: '代码审查平台', change: '更新默认分支' },
    })
    await finish(0)

    expect(wrapper.find('.approval-card').exists()).toBe(true)
    await expandTimeline(wrapper)
    expect(wrapper.find('.xl-steps').text()).toContain('更新项目')
    // 调用参数默认折叠为技术细节,点击后展示
    await wrapper.find('.response-approval-detail-toggle').trigger('click')
    await flushPromises()
    expect(wrapper.find('.response-approval-arguments').text()).toContain('"project_id": 3')
    expect(wrapper.find('.response-approval-preview').text()).toContain('更新默认分支')
    expect(wrapper.findAll('.msg-row.user')).toHaveLength(1)
    await wrapper.find('.response-approve').trigger('click')
    await flushPromises()

    expect(streams.records[1].body).toMatchObject({
      action: 'approve',
      surface: 'user',
      run_id: 'run-user-approval',
      call_id: 'call-write',
      messages: [{ role: 'user', content: '修改项目配置' }],
    })
    expect(wrapper.findAll('.msg-row.user')).toHaveLength(1)

    emit(1, {
      type: 'response.tool.completed',
      call_id: 'call-write',
      tool_name: 'update_project',
      output_summary: { status: 'success', updated_count: 1 },
    })
    emit(1, { type: 'response.output_text.delta', delta: '配置已更新' })
    emit(1, { type: 'response.completed', response: { id: 'run-user-approval' } })
    await finish(1)
    expect(wrapper.text()).toContain('配置已更新')
    expect(wrapper.text()).toContain('已批准')
    await expandTimeline(wrapper)
    const timelineText = wrapper.findAll('.xl-steps').map((node) => node.text()).join('\n')
    expect(timelineText).toContain('做好了')
  })

  it('marks a resumed approval as failed when the terminal response has no tool result', async () => {
    const wrapper = await mountReadyDrawer()
    await wrapper.find('.chat-input').setValue('修改项目配置')
    void wrapper.find('.send-btn').trigger('click')
    await flushPromises()

    emit(0, {
      type: 'response.approval.required',
      run_id: 'run-user-resume-missing-tool',
      call_id: 'call-write-missing-tool',
      tool_name: 'update_project',
      arguments: { project_id: 3 },
      operation: '修改项目',
      impact: '更新项目配置',
      danger: false,
    })
    await finish(0)

    await wrapper.find('.response-approve').trigger('click')
    await flushPromises()
    expect(streams.records[1].body).toMatchObject({
      action: 'approve',
      run_id: 'run-user-resume-missing-tool',
      call_id: 'call-write-missing-tool',
    })

    emit(1, { type: 'response.completed', response: { id: 'run-user-resume-missing-tool' } })
    await finish(1)

    await expandTimeline(wrapper)
    // 新设计:失败步骤默认展开原因,无需点击
    const timelineText = wrapper.find('.xl-steps').text()
    expect(timelineText).toContain('没做成')
    expect(timelineText).toMatch(/响应已结束|更新项目/)
    expect(timelineText).not.toContain('做好了')
  })

  it('submits the model generated question as an answer continuation', async () => {
    const wrapper = await mountReadyDrawer()
    await wrapper.find('.chat-input').setValue('运行审查')
    void wrapper.find('.send-btn').trigger('click')
    await flushPromises()

    emit(0, {
      type: 'response.input.required',
      run_id: 'run-user-input',
      call_id: 'call-question',
      question: '要审查哪个分支？',
    })
    await finish(0)
    expect(wrapper.text()).toContain('要审查哪个分支？')
    expect(wrapper.findAll('.msg-row.user')).toHaveLength(1)

    await wrapper.find('.response-answer').setValue('release/2026')
    await wrapper.find('.response-answer-submit').trigger('click')
    await flushPromises()
    expect(streams.records[1].body).toMatchObject({
      action: 'answer',
      surface: 'user',
      run_id: 'run-user-input',
      call_id: 'call-question',
      answer: 'release/2026',
    })
    expect(wrapper.findAll('.msg-row.user')).toHaveLength(2)
    expect(wrapper.findAll('.msg-row.user')[1].text()).toContain('release/2026')
    await finish(1)
  })

  it('renders structured candidates and submits only after clicking the submit button', async () => {
    const wrapper = await mountReadyDrawer()
    await wrapper.find('.chat-input').setValue('审查登录项目')
    void wrapper.find('.send-btn').trigger('click')
    await flushPromises()

    emit(0, {
      type: 'response.input.required',
      run_id: 'run-project-choice',
      call_id: 'call-project-choice',
      questions: [{
        key: 'project',
        label: '你指的是下面哪个项目？',
        options: [
          { label: '#3 用户登录服务', value: '项目 #3', description: '名称近似匹配' },
          { label: '#8 登录审计', value: '项目 #8' },
        ],
      }],
    })
    await finish(0)

    expect(wrapper.text()).toContain('你指的是下面哪个项目？')
    expect(wrapper.findAll('.response-input-option')).toHaveLength(2)
    expect(wrapper.find('.response-answer').exists()).toBe(true)
    expect(wrapper.find('.response-answer-submit').attributes('disabled')).toBeDefined()

    // 点选不再立即提交
    await wrapper.findAll('.response-input-option')[1].trigger('click')
    await flushPromises()
    expect(streams.records).toHaveLength(1)
    expect(wrapper.findAll('.response-input-option')[1].classes()).toContain('is-selected')

    await wrapper.find('.response-answer-submit').trigger('click')
    await flushPromises()
    expect(streams.records[1].body).toMatchObject({
      action: 'answer',
      surface: 'user',
      run_id: 'run-project-choice',
      call_id: 'call-project-choice',
      answer: '项目 #8',
    })
    emit(1, { type: 'response.completed', response: { id: 'run-project-choice' } })
    await finish(1)
  })

  it('keeps an explicit tool failure visible after the response completes', async () => {
    const wrapper = await mountReadyDrawer()
    await wrapper.find('.chat-input').setValue('读取缺失文件')
    void wrapper.find('.send-btn').trigger('click')
    await flushPromises()

    emit(0, {
      type: 'response.output_item.added',
      output_index: 0,
      item: { type: 'function_call', id: 'item-read', call_id: 'call-read', name: 'read_file' },
    })
    emit(0, {
      type: 'response.function_call_arguments.done',
      output_index: 0,
      item_id: 'item-read',
      arguments: '{"path":"missing.ts"}',
    })
    emit(0, {
      type: 'response.tool.failed',
      item_id: 'item-read',
      call_id: 'call-read',
      tool_name: 'read_file',
      agent_code: 'project_agent',
      error: '文件不存在',
    })
    emit(0, { type: 'response.completed', response: { id: 'run-tool-failed' } })
    await finish(0)

    await expandTimeline(wrapper)
    const timeline = wrapper.find('.xl-steps')
    expect(timeline.text()).toContain('read file')
    expect(timeline.text()).not.toContain('read_file')
    expect(timeline.text()).not.toContain('project_agent')
    expect(timeline.text()).toContain('没做成')
    // 新设计:失败步骤默认展开,直接可见错误详情
    expect(wrapper.find('.xl-steps').text()).toContain('文件不存在')
  })

  it('运行中显示「停止响应」,点击后中止流并留下可重试的取消卡片', async () => {
    const wrapper = await mountReadyDrawer()
    expect(wrapper.find('.stop-btn').exists()).toBe(false)

    await wrapper.find('.chat-input').setValue('帮我分析这个项目')
    void wrapper.find('.send-btn').trigger('click')
    await flushPromises()

    emit(0, { type: 'response.created', response: { id: 'run-stop', model: 'deepseek-v4-flash' } })
    emit(0, { type: 'response.output_text.delta', delta: '我先看一下项目结构' })
    await flushPromises()

    const stopBtn = wrapper.find('.stop-btn')
    expect(stopBtn.exists()).toBe(true)
    expect(wrapper.find('.send-btn').exists()).toBe(false)

    await stopBtn.trigger('click')
    await settleAll()
    // 先弹原因确认层,确认后调用服务端取消并中止本地流。
    expect(wrapper.find('.cancel-confirm-panel').exists()).toBe(true)
    await wrapper.find('.cancel-confirm-stop').trigger('click')
    await settleAll()

    // 已中止当前流;已生成的部分内容保留在消息流里(首条为欢迎语)
    expect(streams.records[0].aborted).toBe(true)
    const bubbles = wrapper.findAll('.msg-row.assistant .markdown-body')
    expect(bubbles.some((bubble) => bubble.text().includes('我先看一下项目结构'))).toBe(true)
    // 取消后错误卡片留在消息流,带重试与新建对话入口
    const errorCard = wrapper.find('.msg-error-card')
    expect(errorCard.exists()).toBe(true)
    expect(errorCard.text()).toContain('已停止本次回答')
    expect(wrapper.find('.msg-error-btn.is-retry').exists()).toBe(true)
    // 运行结束,停止按钮自动隐藏
    expect(wrapper.find('.stop-btn').exists()).toBe(false)
    expect(wrapper.find('.send-btn').exists()).toBe(true)
    expect(messages.error).not.toHaveBeenCalled()

    // 点「重试」重新续跑(上下文包含部分输出,让模型知道说到哪了)
    await wrapper.find('.msg-error-btn.is-retry').trigger('click')
    await settleAll()
    expect(streams.records[1].body).toMatchObject({
      action: 'start',
      surface: 'user',
      messages: [
        { role: 'user', content: '帮我分析这个项目' },
        { role: 'assistant', content: '我先看一下项目结构' },
        { role: 'assistant', content: expect.stringContaining('已停止任务') },
      ],
    })
    emit(1, { type: 'response.output_text.delta', delta: '这次顺利完成了' })
    emit(1, { type: 'response.completed', response: { id: 'run-stop-retry' } })
    await finish(1)
    expect(wrapper.text()).toContain('这次顺利完成了')
    wrapper.unmount()
  })

  it('团队悬浮窗追问成员:点击后预填输入框', async () => {
    teamApi.list.mockResolvedValue({
      items: [{
        team_id: 52, title: '只读核验', surface: 'user', session_id: 'user-test',
        status: 'completed', max_active_children: 3, trace_id: 't52',
        counts: { total: 1, completed: 1, running: 0, queued: 0, failed: 0, blocked: 0 },
      }],
      total: 1,
    })
    teamApi.detail.mockResolvedValue({
      team_id: 52, title: '只读核验', surface: 'user', session_id: 'user-test',
      status: 'completed', max_active_children: 3, trace_id: 't52',
      counts: { total: 1, completed: 1, running: 0, queued: 0, failed: 0, blocked: 0 },
      members: [{
        member_id: 1, member_key: 'm1', display_name: '安全哨兵',
        address: 'agent:security_sentinel', kind: 'runtime', role: 'worker',
        status: 'completed', capabilities: {},
      }],
      tasks: [], events: [], messages: [],
    })
    const wrapper = await mountReadyDrawer()
    await settleAll()
    expect(wrapper.find('.agent-team-trace').exists()).toBe(true)
    await wrapper.find('.agent-team-open-detail').trigger('click')
    await settleAll()
    expect(wrapper.find('.agent-team-window').exists()).toBe(true)
    expect(wrapper.find('.team-window-ask').exists()).toBe(true)
    await wrapper.find('.team-window-ask').trigger('click')
    await settleAll()
    const value = (wrapper.find('textarea.chat-input').element as HTMLTextAreaElement).value
    expect(value).toContain('团队 #52')
    expect(value).toContain('安全哨兵')
    expect(value).toContain('agent:security_sentinel')
    expect(value).toContain('发送补充要求')
  })

  it('网络错误留在消息流:错误卡片 + Toast,重试可恢复', async () => {
    const wrapper = await mountReadyDrawer()
    await wrapper.find('.chat-input').setValue('查一下漏洞')
    void wrapper.find('.send-btn').trigger('click')
    await flushPromises()

    await failStream(0, new Error('网络连接中断'))
    // 等待 canSend 等计算属性完成重算(loading 复位),重试按钮才可点
    await settleAll()

    const errorCard = wrapper.find('.msg-error-card')
    expect(errorCard.exists()).toBe(true)
    expect(errorCard.text()).toContain('网络连接中断')
    expect(messages.error).toHaveBeenCalledWith('网络连接中断')
    expect(wrapper.find('.stop-btn').exists()).toBe(false)

    await wrapper.find('.msg-error-btn.is-retry').trigger('click')
    await settleAll()
    expect(streams.records[1].body).toMatchObject({
      action: 'start',
      messages: [{ role: 'user', content: '查一下漏洞' }],
    })
    emit(1, { type: 'response.completed', response: { id: 'run-net-retry' } })
    await finish(1)
    wrapper.unmount()
  })

  it('协议错误(回答没说完)同样沉淀为错误卡片,且不进入后续上下文', async () => {
    const wrapper = await mountReadyDrawer()
    await wrapper.find('.chat-input').setValue('开始审查')
    void wrapper.find('.send-btn').trigger('click')
    await flushPromises()

    emit(0, { type: 'response.incomplete', response: { id: 'run-incomplete' } })
    await finish(0)

    expect(wrapper.find('.msg-error-card').exists()).toBe(true)
    expect(wrapper.find('.msg-error-card').text()).toContain('小菱的回答没说完')
    expect(messages.error).toHaveBeenCalledWith('小菱的回答没说完,重发一次试试')

    // 后续正常发送时,错误卡片不进模型上下文
    await wrapper.find('.chat-input').setValue('重新审查')
    void wrapper.find('.send-btn').trigger('click')
    await flushPromises()
    expect(streams.records[1].body).toMatchObject({
      messages: [
        { role: 'user', content: '开始审查' },
        { role: 'user', content: '重新审查' },
      ],
    })
    await finish(1)
    wrapper.unmount()
  })

  it('助手消息可复制:写入剪贴板并短暂显示对勾', async () => {
    const writeText = vi.fn().mockResolvedValue(undefined)
    const originalClipboard = Object.getOwnPropertyDescriptor(window.navigator, 'clipboard')
    Object.defineProperty(window.navigator, 'clipboard', { value: { writeText }, configurable: true })
    try {
      const wrapper = await mountReadyDrawer()
      await wrapper.find('.chat-input').setValue('总结一下')
      void wrapper.find('.send-btn').trigger('click')
      await flushPromises()
      emit(0, { type: 'response.output_text.delta', delta: '这里是总结内容' })
      emit(0, { type: 'response.completed', response: { id: 'run-copy' } })
      await finish(0)

      const bubbles = wrapper.findAll('.msg-row.assistant .msg-copy-btn')
      const copyBtn = bubbles[bubbles.length - 1]
      expect(copyBtn.exists()).toBe(true)
      expect(copyBtn.classes()).not.toContain('is-copied')
      await copyBtn.trigger('click')
      await flushPromises()
      expect(writeText).toHaveBeenCalledWith('这里是总结内容')
      expect(wrapper.findAll('.msg-row.assistant .msg-copy-btn')[1].classes()).toContain('is-copied')
      wrapper.unmount()
    } finally {
      if (originalClipboard) Object.defineProperty(window.navigator, 'clipboard', originalClipboard)
    }
  })
})

it('恢复进行中会话时展示调用链、部分输出与运行状态', async () => {
  sessionApi.get.mockResolvedValue({
    surface: 'user',
    session_id: 'user-inflight',
    run: {
      run_id: 'run-inflight',
      status: 'running',
      model: 'deepseek-v4-flash',
      rounds: 2,
      error: '',
      output_text: '正在处理第 1 个告警',
      updated_at: new Date().toISOString(),
    },
    messages: [{ role: 'user', content: '处理告警' }],
    events: [
      {
        type: 'response.tool.started',
        run_id: 'run-inflight',
        call_id: 'call_inflight_1',
        tool_name: 'admin_execute_capability',
        arguments: { capability: 'observability.alerts.list' },
        status: 'running',
        sequence_number: 1,
      },
    ],
    pending: null,
  })
  const wrapper = await mountReadyDrawer()
  // 调用链(进行中工具,通俗化展示)可见
  expect(wrapper.text()).toContain('执行管理操作')
  expect(wrapper.text()).not.toContain('admin_execute_capability')
  // 部分输出可见
  expect(wrapper.text()).toContain('正在处理第 1 个告警')
  // 运行状态徽标:运行中
  expect(wrapper.text()).toContain('运行中')
  wrapper.unmount()
})

it('运行中展示小菱执行进度条(已完成 X/Y 步 + 当前动作),完成后收起', async () => {
  const wrapper = await mountReadyDrawer()
  // 空闲且没有工具调用时不显示
  expect(wrapper.find('.chat-progress').exists()).toBe(false)

  await wrapper.find('.chat-input').setValue('列出项目')
  void wrapper.find('.send-btn').trigger('click')
  await flushPromises()

  emit(0, {
    type: 'response.output_item.added',
    output_index: 0,
    item: { type: 'function_call', id: 'item-projects', call_id: 'call-projects', name: 'list_projects' },
  })
  emit(0, {
    type: 'response.function_call_arguments.done',
    output_index: 0,
    item_id: 'item-projects',
    arguments: '{"page":1}',
  })
  await flushPromises()

  // 进度条:0/1 步,当前动作为「查看项目列表」(进度轨道为 FluidProgress 流体进度)
  const progress = wrapper.find('.chat-progress')
  expect(progress.exists()).toBe(true)
  expect(progress.text()).toContain('0/1 步')
  expect(progress.text()).toContain('查看项目列表')
  expect(progress.find('.chat-progress-fluid').exists()).toBe(true)

  emit(0, {
    type: 'response.tool.completed',
    call_id: 'call-projects',
    tool_name: 'list_projects',
    output_summary: '返回 2 个项目',
  })
  emit(0, { type: 'response.completed', response: { id: 'run-progress' } })
  await finish(0)

  // 全部完成后进度条收起
  expect(wrapper.find('.chat-progress').exists()).toBe(false)
  wrapper.unmount()
})
