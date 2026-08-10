import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const streams = vi.hoisted(() => ({
  start: vi.fn(),
  records: [] as Array<{
    body: Record<string, unknown>
    onEvent: (event: unknown) => void
    resolve: () => void
  }>,
}))

const messages = vi.hoisted(() => ({ error: vi.fn(), warning: vi.fn() }))
const sessionApi = vi.hoisted(() => ({ get: vi.fn() }))
const meshApi = vi.hoisted(() => ({ heartbeat: vi.fn(), inbox: vi.fn() }))

vi.mock('@/utils/responsesStream', () => ({ streamResponses: streams.start }))
vi.mock('@/api/agentResponses', () => ({ getAgentResponseSession: sessionApi.get }))
vi.mock('@/api/agentMesh', () => ({
  heartbeatAgentMesh: meshApi.heartbeat,
  pullAgentMeshInbox: meshApi.inbox,
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
        CircleCheck: true,
        Close: true,
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

beforeEach(() => {
  meshApi.heartbeat.mockReset().mockResolvedValue({})
  meshApi.inbox.mockReset().mockResolvedValue([])
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
    const controller = new AbortController()
    let resolve = (): void => undefined
    const done = new Promise<void>((doneResolve) => { resolve = doneResolve })
    streams.records.push({ body, onEvent: options.onEvent, resolve })
    return { abort: () => controller.abort(), signal: controller.signal, done }
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
/** 调用链默认折叠;需要断言展开详情的用例自行点击 .response-tool-call-head。 */
async function expandTimeline(_wrapper: VueWrapper): Promise<void> {
  // no-op:保留给历史用例的兼容入口
}

describe('AgentChatDrawer Responses stream', () => {
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
    expect(rows[2].find('.response-tool-timeline').text()).toContain('list_projects')
    expect(rows[2].text()).toContain('已完成')
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

    expect(wrapper.find('.response-tool-timeline').exists()).toBe(false)
    await vi.advanceTimersByTimeAsync(3000)
    await flushPromises()

    const timeline = wrapper.find('.response-tool-timeline')
    expect(timeline.text()).toContain('receive_message')
    expect(timeline.text()).toContain('agent:data-analysis')
    const head = timeline.find('.response-tool-call-head')
    expect(head.attributes('aria-expanded')).toBe('false')
    expect(timeline.find('.response-tool-detail').exists()).toBe(false)

    await head.trigger('click')
    expect(timeline.find('.response-tool-detail').text()).toContain('数据分析完成')
    expect(timeline.find('.response-tool-detail').text()).toContain('anomaly_count')
    wrapper.unmount()
    vi.useRealTimers()
  })

  it('刷新后恢复动态追问并点击选项自动续跑', async () => {
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
    expect(wrapper.find('.send-btn').attributes('disabled')).toBeDefined()
    await wrapper.findAll('.response-input-option')[0].trigger('click')
    await flushPromises()
    expect(streams.records[0].body).toMatchObject({
      action: 'answer',
      run_id: 'run-user-restored',
      call_id: 'call-choice',
      answer: 'agent-safe',
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
    expect(wrapper.find('.response-tool-timeline').text()).toContain('update_project')
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
    const timelineText = wrapper.findAll('.response-tool-timeline').map((node) => node.text()).join('\n')
    expect(timelineText).toContain('已完成')
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
    // 调用链默认折叠:点击调用头展开后断言失败详情
    await wrapper.find('.response-tool-call-head').trigger('click')
    await flushPromises()
    const timelineText = wrapper.find('.response-tool-timeline').text()
    expect(timelineText).toContain('失败')
    expect(timelineText).toContain('响应已结束，但工具未返回完成事件')
    expect(timelineText).not.toContain('已完成')
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

  it('renders structured candidates and submits a click without requiring typed text', async () => {
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
    await wrapper.findAll('.response-input-option')[1].trigger('click')
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
    const timeline = wrapper.find('.response-tool-timeline')
    expect(timeline.text()).toContain('read_file')
    expect(timeline.text()).toContain('project_agent')
    expect(timeline.text()).toContain('失败')
    // 已完成/失败的调用默认折叠,点击后展示错误详情
    await wrapper.find('.response-tool-call-head').trigger('click')
    await flushPromises()
    expect(wrapper.find('.response-tool-timeline').text()).toContain('文件不存在')
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
  // 调用链(进行中工具)可见
  expect(wrapper.text()).toContain('admin_execute_capability')
  // 部分输出可见
  expect(wrapper.text()).toContain('正在处理第 1 个告警')
  // 运行状态徽标:运行中
  expect(wrapper.text()).toContain('运行中')
  wrapper.unmount()
})
