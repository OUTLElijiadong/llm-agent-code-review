import { flushPromises, mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { beforeEach, describe, expect, it, vi } from 'vitest'

const mocks = vi.hoisted(() => ({
  retry: vi.fn(),
  preview: vi.fn(),
  cancel: vi.fn(),
  confirm: vi.fn(),
  prompt: vi.fn(),
  success: vi.fn(),
  error: vi.fn(),
}))

vi.mock('@/api/agentTeams', async (importOriginal) => ({
  ...await importOriginal<typeof import('@/api/agentTeams')>(),
  retryAgentTeam: mocks.retry,
  previewRetryAgentTeam: mocks.preview,
  cancelAgentTeam: mocks.cancel,
}))
vi.mock('element-plus/es/components/message-box/index', () => ({ ElMessageBox: { confirm: mocks.confirm, prompt: mocks.prompt } }))
vi.mock('element-plus/es/components/message/index', () => ({ ElMessage: { success: mocks.success, error: mocks.error } }))

import type { AgentTeamDetail, AgentTeamTask } from '@/api/agentTeams'
import { useUserStore } from '@/stores/user'
import AgentTeamWindow from './AgentTeamWindow.vue'

function failedTask(taskKey: string, taskId: number): AgentTeamTask {
  return {
    task_id: taskId,
    task_key: taskKey,
    member_id: 10,
    member_key: 'reviewer',
    title: `失败任务 ${taskKey}`,
    depends_on: [],
    status: 'failed',
    priority: 1,
    attempt_count: 1,
    max_attempts: 2,
  }
}

function makeTeam(teamId: number, sessionId: string, taskKeys: string[], status: AgentTeamDetail['status'] = 'failed'): AgentTeamDetail {
  const tasks = taskKeys.map((key, index) => failedTask(key, index + 1))
  return {
    team_id: teamId,
    title: `团队 ${teamId}`,
    surface: 'user',
    session_id: sessionId,
    status,
    max_active_children: 2,
    trace_id: `trace-${teamId}`,
    created_at: '2026-10-01T00:00:00Z',
    members: [],
    tasks,
    counts: { total: tasks.length, completed: 0, running: 0, queued: 0, failed: tasks.length, blocked: 0 },
    events: [],
    messages: [],
  }
}

function deferred<T>() {
  let resolve!: (value: T) => void
  let reject!: (reason?: unknown) => void
  const promise = new Promise<T>((resolvePromise, rejectPromise) => {
    resolve = resolvePromise
    reject = rejectPromise
  })
  return { promise, resolve, reject }
}

const global = { stubs: { Teleport: true, Transition: false, 'el-icon': true } }
let pinia: ReturnType<typeof createPinia>

describe('AgentTeamWindow retry target freeze', () => {
  beforeEach(() => {
    pinia = createPinia()
    setActivePinia(pinia)
    useUserStore().profile = { id: 101 } as never
    useUserStore().token = 'token-101'
    mocks.retry.mockReset().mockResolvedValue({ team_id: 1, status: 'queued' })
    mocks.preview.mockReset().mockResolvedValue({
      team_id: 1, requires_confirmation: false, risk_level: 'low', plan_sha256: 'a'.repeat(64), tasks: [],
    })
    mocks.cancel.mockReset().mockResolvedValue({ team_id: 1, status: 'cancelled' })
    mocks.confirm.mockReset()
    mocks.prompt.mockReset().mockResolvedValue({ value: '先按新的证据分片顺序重新审查并核对回归样本' })
    mocks.success.mockReset()
    mocks.error.mockReset()
  })

  it('同团队确认期间新增失败任务时，只重试弹窗确认时冻结的 task_keys', async () => {
    const confirmation = deferred<{ value: string }>()
    mocks.prompt.mockReturnValueOnce(confirmation.promise)
    const wrapper = mount(AgentTeamWindow, {
      props: { visible: true, team: makeTeam(1, 'session-a', ['failure-a']) },
      global: { ...global, plugins: [pinia] },
    })

    await wrapper.get('.is-retry').trigger('click')
    await wrapper.setProps({ team: makeTeam(1, 'session-a', ['failure-a', 'failure-b']) })
    confirmation.resolve({ value: '先按新的证据分片顺序重新审查并核对回归样本' })
    await flushPromises()

    expect(mocks.retry).toHaveBeenCalledWith(1, ['failure-a'], {
      'failure-a': '先按新的证据分片顺序重新审查并核对回归样本',
    }, 'a'.repeat(64))
    wrapper.unmount()
  })

  it('确认期间切换团队后不把确认动作漂移到新团队', async () => {
    const confirmation = deferred<{ value: string }>()
    mocks.prompt.mockReturnValueOnce(confirmation.promise)
    const wrapper = mount(AgentTeamWindow, {
      props: { visible: true, team: makeTeam(1, 'session-a', ['failure-a']) },
      global: { ...global, plugins: [pinia] },
    })

    await wrapper.get('.is-retry').trigger('click')
    await wrapper.setProps({ team: makeTeam(2, 'session-b', ['failure-b']) })
    confirmation.resolve({ value: '先按新的证据分片顺序重新审查并核对回归样本' })
    await flushPromises()

    expect(mocks.retry).not.toHaveBeenCalled()
    wrapper.unmount()
  })

  it('确认期间切换账号后不提交旧账号弹窗对应的团队操作', async () => {
    const confirmation = deferred<{ value: string }>()
    mocks.prompt.mockReturnValueOnce(confirmation.promise)
    const user = useUserStore()
    user.profile = { id: 101 } as never
    const wrapper = mount(AgentTeamWindow, {
      props: { visible: true, team: makeTeam(1, 'session-a', ['failure-a']) },
      global: { ...global, plugins: [pinia] },
    })

    await wrapper.get('.is-retry').trigger('click')
    user.profile = { id: 202 } as never
    confirmation.resolve({ value: '先按新的证据分片顺序重新审查并核对回归样本' })
    await flushPromises()

    expect(mocks.retry).not.toHaveBeenCalled()
    wrapper.unmount()
  })

  it('同账号重新登录导致认证会话变化后取消旧确认', async () => {
    const confirmation = deferred<{ value: string }>()
    mocks.prompt.mockReturnValueOnce(confirmation.promise)
    const user = useUserStore()
    const wrapper = mount(AgentTeamWindow, {
      props: { visible: true, team: makeTeam(1, 'session-a', ['failure-a']) },
      global: { ...global, plugins: [pinia] },
    })

    await wrapper.get('.is-retry').trigger('click')
    user.token = 'token-101-refreshed'
    confirmation.resolve({ value: '先按新的证据分片顺序重新审查并核对回归样本' })
    await flushPromises()

    expect(mocks.retry).not.toHaveBeenCalled()
    wrapper.unmount()
  })

  it('同团队会话变化后取消确认中的重试', async () => {
    const confirmation = deferred<{ value: string }>()
    mocks.prompt.mockReturnValueOnce(confirmation.promise)
    const wrapper = mount(AgentTeamWindow, {
      props: { visible: true, team: makeTeam(1, 'session-a', ['failure-a']) },
      global: { ...global, plugins: [pinia] },
    })

    await wrapper.get('.is-retry').trigger('click')
    await wrapper.setProps({ team: makeTeam(1, 'session-b', ['failure-a']) })
    confirmation.resolve({ value: '先按新的证据分片顺序重新审查并核对回归样本' })
    await flushPromises()

    expect(mocks.retry).not.toHaveBeenCalled()
    wrapper.unmount()
  })

  it('重试输入缺少新方案时不调用服务', async () => {
    mocks.prompt.mockResolvedValueOnce({ value: '重跑' })
    const wrapper = mount(AgentTeamWindow, {
      props: { visible: true, team: makeTeam(1, 'session-a', ['failure-a']) },
      global: { ...global, plugins: [pinia] },
    })

    await wrapper.get('.is-retry').trigger('click')
    await flushPromises()

    expect(mocks.retry).not.toHaveBeenCalled()
    expect(mocks.error).toHaveBeenCalledWith('新方案无效或与原执行指令重复，请重新核对后再试')
    wrapper.unmount()
  })

  it('高风险预览展示精确影响任务并要求用户确认后才提交摘要', async () => {
    mocks.preview.mockResolvedValueOnce({
      team_id: 1,
      requires_confirmation: true,
      risk_level: 'high',
      plan_sha256: 'b'.repeat(64),
      tasks: [{
        task_key: 'failure-a', member_key: 'reviewer', title: '读取外部目标', status: 'failed',
        depends_on: ['source-a'],
        risk_level: 'high', reason: '任务包含外部目标，等待当前账号确认',
        classification: 'external_target', needs_confirmation: true, fingerprint: 'c'.repeat(64),
      }],
    })
    mocks.confirm.mockResolvedValueOnce(true)
    const wrapper = mount(AgentTeamWindow, {
      props: { visible: true, team: makeTeam(1, 'session-a', ['failure-a']) },
      global: { ...global, plugins: [pinia] },
    })

    await wrapper.get('.is-retry').trigger('click')
    await flushPromises()

    expect(mocks.confirm).toHaveBeenCalledOnce()
    expect(mocks.confirm.mock.calls[0]?.[0]?.children).toContain('当前失败；依赖：source-a')
    expect(mocks.retry).toHaveBeenCalledWith(1, ['failure-a'], {
      'failure-a': '先按新的证据分片顺序重新审查并核对回归样本',
    }, 'b'.repeat(64))
    wrapper.unmount()
  })

  it('用户取消高风险重试确认时不提交任务', async () => {
    mocks.preview.mockResolvedValueOnce({
      team_id: 1,
      requires_confirmation: true,
      risk_level: 'critical',
      plan_sha256: 'd'.repeat(64),
      tasks: [{
        task_key: 'failure-a', member_key: 'reviewer', title: '生产操作', status: 'failed',
        depends_on: [],
        risk_level: 'critical', reason: '高危任务', classification: 'privileged_agent',
        needs_confirmation: true, fingerprint: 'e'.repeat(64),
      }],
    })
    mocks.confirm.mockRejectedValueOnce(new Error('cancelled'))
    const wrapper = mount(AgentTeamWindow, {
      props: { visible: true, team: makeTeam(1, 'session-a', ['failure-a']) },
      global: { ...global, plugins: [pinia] },
    })

    await wrapper.get('.is-retry').trigger('click')
    await flushPromises()

    expect(mocks.confirm).toHaveBeenCalledOnce()
    expect(mocks.retry).not.toHaveBeenCalled()
    wrapper.unmount()
  })

  it('最终确认期间切换团队后不把预览授权提交给新团队', async () => {
    const confirmation = deferred<void>()
    mocks.confirm.mockReturnValueOnce(confirmation.promise)
    const wrapper = mount(AgentTeamWindow, {
      props: { visible: true, team: makeTeam(1, 'session-a', ['failure-a']) },
      global: { ...global, plugins: [pinia] },
    })

    await wrapper.get('.is-retry').trigger('click')
    await flushPromises()
    expect(mocks.confirm).toHaveBeenCalledOnce()
    await wrapper.setProps({ team: makeTeam(2, 'session-b', ['failure-b']) })
    confirmation.resolve()
    await flushPromises()

    expect(mocks.retry).not.toHaveBeenCalled()
    wrapper.unmount()
  })

  it('取消确认期间切换团队后不取消新团队', async () => {
    const confirmation = deferred<void>()
    mocks.confirm.mockReturnValueOnce(confirmation.promise)
    const wrapper = mount(AgentTeamWindow, {
      props: { visible: true, team: makeTeam(1, 'session-a', [], 'running') },
      global: { ...global, plugins: [pinia] },
    })

    await wrapper.get('.is-cancel').trigger('click')
    await wrapper.setProps({ team: makeTeam(2, 'session-b', [], 'running') })
    confirmation.resolve()
    await flushPromises()

    expect(mocks.cancel).not.toHaveBeenCalled()
    wrapper.unmount()
  })

  it('重试方案确认期间卸载窗口后不再请求预览', async () => {
    const confirmation = deferred<{ value: string }>()
    mocks.prompt.mockReturnValueOnce(confirmation.promise)
    const wrapper = mount(AgentTeamWindow, {
      props: { visible: true, team: makeTeam(1, 'session-a', ['failure-a']) },
      global: { ...global, plugins: [pinia] },
    })
    await wrapper.get('.is-retry').trigger('click')
    wrapper.unmount()
    confirmation.resolve({ value: '先按新的证据分片顺序重新审查并核对回归样本' })
    await flushPromises()
    expect(mocks.preview).not.toHaveBeenCalled()
    expect(mocks.retry).not.toHaveBeenCalled()
    expect(mocks.error).not.toHaveBeenCalled()
  })

  it('最终重试确认期间卸载窗口后不提交并保持安静', async () => {
    const confirmation = deferred<void>()
    mocks.confirm.mockReturnValueOnce(confirmation.promise)
    const wrapper = mount(AgentTeamWindow, {
      props: { visible: true, team: makeTeam(1, 'session-a', ['failure-a']) },
      global: { ...global, plugins: [pinia] },
    })
    await wrapper.get('.is-retry').trigger('click')
    await flushPromises()
    expect(mocks.confirm).toHaveBeenCalledOnce()
    wrapper.unmount()
    confirmation.resolve()
    await flushPromises()
    expect(mocks.retry).not.toHaveBeenCalled()
    expect(mocks.error).not.toHaveBeenCalled()
  })

  it('取消确认期间卸载窗口后不再发出取消请求', async () => {
    const confirmation = deferred<void>()
    mocks.confirm.mockReturnValueOnce(confirmation.promise)
    const wrapper = mount(AgentTeamWindow, {
      props: { visible: true, team: makeTeam(1, 'session-a', [], 'running') },
      global: { ...global, plugins: [pinia] },
    })
    await wrapper.get('.is-cancel').trigger('click')
    wrapper.unmount()
    confirmation.resolve()
    await flushPromises()
    expect(mocks.cancel).not.toHaveBeenCalled()
    expect(mocks.success).not.toHaveBeenCalled()
    expect(mocks.error).not.toHaveBeenCalled()
  })

  it('复制成功回调在窗口卸载后不再提示旧窗口内容', async () => {
    const copied = deferred<void>()
    Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText: vi.fn(() => copied.promise) } })
    const team = makeTeam(1, 'session-a', [])
    team.messages = [{
      message_id: 'copy-after-unmount', trace_id: 'trace-1', correlation_id: 'correlation-1', sent_from: 'manager',
      send_to: 'user', message_type: 'task.result', subject: '完整结果', status: 'completed',
      payload: { report: '需要完整保存的证据'.repeat(40) }, create_time: '2026-10-04T00:00:00Z',
    }]
    const wrapper = mount(AgentTeamWindow, { props: { visible: true, team }, global: { ...global, plugins: [pinia] } })
    await wrapper.get('.team-chat-payload-copy').trigger('click')
    wrapper.unmount()
    copied.resolve()
    await flushPromises()
    expect(mocks.success).not.toHaveBeenCalled()
  })

})
