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

vi.mock('@/utils/responsesStream', () => ({ streamResponses: streams.start }))
vi.mock('@/api/agentResponses', () => ({ getAgentResponseSession: sessionApi.get }))
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

describe('AgentChatDrawer Responses stream', () => {
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

  it('恢复运行态后轮询到动态追问即停止', async () => {
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

    await vi.advanceTimersByTimeAsync(5000)
    await flushPromises()
    expect(sessionApi.get).toHaveBeenCalledTimes(2)
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

  it('keeps the empty view clean and incrementally updates one assistant bubble', async () => {
    const wrapper = await mountReadyDrawer()
    expect(wrapper.findAll('.msg-row')).toHaveLength(0)

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
    expect(wrapper.findAll('.msg-row.assistant .markdown-body')).toHaveLength(0)

    emit(0, { type: 'response.output_text.delta', delta: '发现 1 个问题\n\n正在定位' })
    await flushPromises()
    expect(wrapper.findAll('.msg-row.assistant .markdown-body')).toHaveLength(1)
    expect(wrapper.find('.msg-row.assistant .markdown-body').html()).toContain('发现 1 个问题<br>')

    emit(0, { type: 'response.output_text.delta', delta: '\n```ts\n\nconst ok = true\n```' })
    await flushPromises()

    expect(wrapper.findAll('.msg-row.assistant .markdown-body')).toHaveLength(1)
    const html = wrapper.find('.msg-row.assistant .markdown-body').html()
    expect(html).not.toContain('<br><br>')
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
    await finish(1)
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

    const timeline = wrapper.find('.response-tool-timeline')
    expect(timeline.text()).toContain('read_file')
    expect(timeline.text()).toContain('project_agent')
    expect(timeline.text()).toContain('失败')
    expect(timeline.text()).toContain('文件不存在')
  })
})
