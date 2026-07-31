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

import AdminCopilot from './AdminCopilot.vue'

function mountCopilot(): VueWrapper {
  return mount(AdminCopilot, {
    global: {
      stubs: {
        'el-icon': { template: '<span class="el-icon-stub"><slot /></span>' },
        ChatDotRound: true,
        Close: true,
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

beforeEach(() => {
  sessionApi.get.mockReset()
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
    streams.records.push({ body, onEvent: options.onEvent, resolve })
    return { abort: () => controller.abort(), signal: controller.signal, done }
  })
})

afterEach(() => {
  vi.useRealTimers()
})

describe('AdminCopilot Responses stream', () => {
  it('初始会话恢复完成前禁止发送', async () => {
    let resolveSession!: (value: unknown) => void
    sessionApi.get.mockReturnValueOnce(new Promise((resolve) => { resolveSession = resolve }))
    const wrapper = mountCopilot()
    await openCopilot(wrapper)

    expect(wrapper.find('textarea').attributes('disabled')).toBeDefined()
    await wrapper.find('textarea').setValue('不应提前发送')
    await wrapper.find('.send-button').trigger('click')
    expect(streams.start).not.toHaveBeenCalled()

    resolveSession({ surface: 'admin', session_id: 'admin-test', run: null, messages: [], pending: null })
    await flushPromises()
    expect(wrapper.find('textarea').attributes('disabled')).toBeUndefined()
    wrapper.unmount()
  })

  it('恢复运行态后轮询到待审批即停止', async () => {
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
    expect(sessionApi.get).toHaveBeenCalledTimes(1)

    await vi.advanceTimersByTimeAsync(1000)
    await flushPromises()
    expect(sessionApi.get).toHaveBeenCalledTimes(2)
    expect(wrapper.text()).toContain('admin_delete_user')

    await vi.advanceTimersByTimeAsync(5000)
    await flushPromises()
    expect(sessionApi.get).toHaveBeenCalledTimes(2)
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

    expect(wrapper.text()).toContain('admin_delete_users')
    expect(wrapper.text()).toContain('qa-a (#901)')
    expect(wrapper.find('textarea').attributes('disabled')).toBeDefined()
    expect(wrapper.find('.send-button').attributes('disabled')).toBeDefined()
    expect(wrapper.find<HTMLButtonElement>('.response-approval .primary-action').element.disabled).toBe(true)
    await wrapper.find('.danger-input').setValue('确认执行')
    await wrapper.find('.response-approval .primary-action').trigger('click')
    await flushPromises()
    expect(streams.records[0].body).toMatchObject({
      action: 'approve',
      run_id: 'run-restored',
      call_id: 'call-restored',
      messages: [{ role: 'user', content: '删除两个测试用户' }],
    })
    await finish(0)
  })

  it('opens empty and renders only the first meaningful streamed delta', async () => {
    const wrapper = mountCopilot()
    await openCopilot(wrapper)

    expect(wrapper.findAll('.message-row')).toHaveLength(0)
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
    expect(wrapper.findAll('.is-assistant .message-bubble')).toHaveLength(0)

    emit(0, { type: 'response.output_text.delta', delta: '第一段\n\n第二段' })
    await flushPromises()
    const assistant = wrapper.find('.is-assistant .message-bubble')
    expect(assistant.exists()).toBe(true)
    expect(assistant.text()).toContain('第一段\n第二段')
    expect(wrapper.findAll('.is-assistant .message-bubble')).toHaveLength(1)

    emit(0, { type: 'response.completed', response: { id: 'run-admin', status: 'completed' } })
    await finish(0)

    await wrapper.find('textarea').setValue('继续检查')
    void wrapper.find('textarea').trigger('keydown', { key: 'Enter' })
    await flushPromises()
    expect(streams.records[1].body).toMatchObject({
      messages: [
        { role: 'user', content: '检查生产状态' },
        { role: 'assistant', content: '第一段\n第二段' },
        { role: 'user', content: '继续检查' },
      ],
    })
    await finish(1)
  })

  it('keeps a streamed Markdown user table complete inside the chat bubble', async () => {
    const wrapper = mountCopilot()
    await openCopilot(wrapper)
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
    await wrapper.find('textarea').setValue('删除测试用户')
    void wrapper.find('textarea').trigger('keydown', { key: 'Enter' })
    await flushPromises()

    emit(0, {
      type: 'response.approval.required',
      run_id: 'run-admin-approval',
      call_id: 'call-delete',
      tool_name: 'admin_delete_user',
      arguments: { user_id: 9 },
      operation: '删除用户',
      impact: '用户将无法登录',
      danger: true,
      preview: { target: 'test-user (#9)', affected_count: 1 },
    })
    await finish(0)

    expect(wrapper.find('.response-approval').exists()).toBe(true)
    expect(wrapper.find('.response-tool-timeline').text()).toContain('admin_delete_user')
    expect(wrapper.find('.response-tool-timeline').text()).toContain('等待批准')
    expect(wrapper.find('.response-approval-arguments').text()).toContain('"user_id": 9')
    expect(wrapper.find('.response-approval-preview').text()).toContain('test-user (#9)')
    expect(wrapper.find('.danger-input').exists()).toBe(true)
    expect(wrapper.findAll('.is-user')).toHaveLength(1)
    expect(wrapper.find<HTMLButtonElement>('.response-approval .primary-action').element.disabled).toBe(true)
    await wrapper.find('.danger-input').setValue('确认执行')
    await wrapper.find('.response-approval .primary-action').trigger('click')
    await flushPromises()

    expect(streams.records[1].body).toMatchObject({
      action: 'approve',
      surface: 'admin',
      run_id: 'run-admin-approval',
      call_id: 'call-delete',
      messages: [{ role: 'user', content: '删除测试用户' }],
    })
    expect(wrapper.findAll('.is-user')).toHaveLength(1)

    emit(1, { type: 'response.output_text.delta', delta: '操作已完成' })
    emit(1, { type: 'response.completed', response: { id: 'run-admin-approval' } })
    await finish(1)
    expect(wrapper.text()).toContain('操作已完成')
    expect(wrapper.text()).toContain('已批准')
    expect(wrapper.find('.response-tool-timeline').text()).toContain('已完成')
  })

  it('streams function arguments and submits a structured option immediately', async () => {
    const wrapper = mountCopilot()
    await openCopilot(wrapper)
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

    expect(wrapper.find('.response-tool-arguments').text()).toContain('"query": "李家栋"')
    expect(wrapper.findAll('.response-input-option')).toHaveLength(2)
    expect(wrapper.text()).toContain('其他（自定义输入）')
    await wrapper.findAll('.response-input-option')[0].trigger('click')
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
