import { flushPromises, mount } from '@vue/test-utils'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const api = vi.hoisted(() => ({
  list: vi.fn(), detail: vi.fn(), cancel: vi.fn(), retry: vi.fn(), artifact: vi.fn(), access: vi.fn(),
}))
const session = vi.hoisted(() => ({ token: 'enterprise-session-token' as string | null }))

vi.mock('@/api/enterpriseAgentTeams', () => ({
  listEnterpriseAgentTeams: api.list,
  getEnterpriseAgentTeam: api.detail,
  cancelEnterpriseAgentTeam: api.cancel,
  retryEnterpriseAgentTeamTask: api.retry,
  getEnterpriseTeamSecurityArtifact: api.artifact,
  checkEnterpriseTeamSecurityArtifactAccess: api.access,
}))
vi.mock('@/utils/token', () => ({ getToken: () => session.token }))

import EnterpriseAgentTeamProgress from './EnterpriseAgentTeamProgress.vue'

const summary = {
  team_id: 12, enterprise_id: 10, conversation_id: 9, group_id: null,
  initiator_user_id: 2, title: '代码双检', status: 'running', task_count: 2,
  completed_task_count: 1, running_task_count: 1, blocked_task_count: 0,
  created_at: null, started_at: null, completed_at: null, can_read_conversation: true,
}
const detail = {
  team_id: 12, enterprise_id: 10, conversation_id: 9, group_id: null,
  title: '代码双检', objective: '审查短代码片段', status: 'running',
  created_at: null, started_at: null, completed_at: null,
  tasks: [
    { task_id: 31, task_key: 'review', title: '质量审查', agent_code: 'code_reviewer',
      agent_name: '代码审查', status: 'completed', attempt_count: 1,
      result: { status: 'completed', summary: '发现一项可空值访问风险。' }, errors: [], started_at: null, completed_at: null },
    { task_id: 32, task_key: 'language', title: '语言识别', agent_code: 'language_detector',
      agent_name: '语言识别', status: 'retryable', attempt_count: 1,
      result: {}, errors: [{ code: 'temporary_error' }], started_at: null, completed_at: null },
  ],
}

function makeWrapper(overrides: Record<string, unknown> = {}) {
  return mount(EnterpriseAgentTeamProgress, {
    props: {
      enterpriseId: 10, conversationId: 9, currentUserId: 1, roomKind: 'company',
      canManageCompany: true, canManageGroup: false, ...overrides,
    },
  })
}

beforeEach(() => {
  session.token = 'enterprise-session-token'
  Object.defineProperty(document, 'visibilityState', { configurable: true, value: 'visible' })
  Object.values(api).forEach(fn => fn.mockReset())
  api.list.mockResolvedValue({ items: [summary], has_more: false, next_before_id: null })
  api.detail.mockResolvedValue(detail)
  api.cancel.mockResolvedValue({ team_id: 12, status: 'cancelled' })
  api.retry.mockResolvedValue({ team_id: 12, task_id: 32, status: 'queued' })
  api.artifact.mockResolvedValue({
    artifact_id: 91, team_id: 12, task_id: 33, attempt_no: 1, lease_generation: 1,
    enterprise_id: 10, project_id: 701, audit_run_id: 'run_91',
    source_sha256: 'a'.repeat(64), report_sha256: 'b'.repeat(64),
    coverage_status: 'complete', termination_reason: '',
    summary: { summary: '扫描完成', finding_count: 1, provider_request_count: 2 },
    report: {
      findings: [{ title: '未校验输入', severity: '高', file_path: 'src/input.ts', line: 18, description: '存在注入风险' }],
      compliance: { scan_mode: 'full', provider_request_count: 2, verification: { enabled: true, complete: true } },
    },
    created_at: '2026-11-07T00:00:00',
  })
  api.access.mockResolvedValue(undefined)
})
afterEach(() => { vi.useRealTimers() })

describe('企业 Agent 团队进度', () => {
  it('其他标签页清空存储时立即清除旧团队结果', async () => {
    const wrapper = makeWrapper()
    await flushPromises()
    expect(wrapper.text()).toContain('发现一项可空值访问风险。')
    session.token = null
    window.dispatchEvent(new StorageEvent('storage', { key: null }))
    await flushPromises()
    expect(wrapper.text()).not.toContain('代码双检')
    expect(wrapper.text()).not.toContain('发现一项可空值访问风险。')
    wrapper.unmount()
  })

  it('存储通知未改变凭据时保留当前结果', async () => {
    const wrapper = makeWrapper()
    await flushPromises()
    window.dispatchEvent(new StorageEvent('storage', { key: null }))
    await flushPromises()
    expect(wrapper.text()).toContain('发现一项可空值访问风险。')
    wrapper.unmount()
  })

  it('刷新请求一直未返回时轮询仍检测凭据变更并清屏', async () => {
    vi.useFakeTimers({ toFake: ['setInterval', 'clearInterval'] })
    const wrapper = makeWrapper()
    await flushPromises()
    expect(wrapper.text()).toContain('发现一项可空值访问风险。')
    api.list.mockReturnValueOnce(new Promise(() => {}))
    await wrapper.get('.progress-actions button').trigger('click')
    session.token = 'other-account-token'
    await vi.advanceTimersByTimeAsync(5000)
    expect(wrapper.text()).not.toContain('代码双检')
    expect(wrapper.text()).not.toContain('发现一项可空值访问风险。')
    wrapper.unmount()
  })

  it('其他标签页更换账号时立即清除已显示的团队结果', async () => {
    const wrapper = makeWrapper()
    await flushPromises()
    expect(wrapper.text()).toContain('发现一项可空值访问风险。')
    session.token = 'other-account-token'
    window.dispatchEvent(new StorageEvent('storage', {
      key: 'review_token', oldValue: 'enterprise-session-token', newValue: session.token,
    }))
    await flushPromises()
    expect(wrapper.text()).not.toContain('代码双检')
    expect(wrapper.text()).not.toContain('发现一项可空值访问风险。')
    wrapper.unmount()
  })

  it('凭据变化后丢弃旧账号迟到的团队结果', async () => {
    let resolveDetail!: (value: unknown) => void
    api.detail.mockReturnValueOnce(new Promise(resolve => { resolveDetail = resolve }))
    const wrapper = makeWrapper()
    await flushPromises()
    session.token = 'other-account-token'
    resolveDetail(detail)
    await flushPromises()
    expect(wrapper.text()).not.toContain('发现一项可空值访问风险。')
    wrapper.unmount()
  })

  it('旧账号操作迟到失败不覆盖新会话状态', async () => {
    let rejectCancel!: (reason: Error) => void
    api.cancel.mockReturnValueOnce(new Promise((_resolve, reject) => { rejectCancel = reject }))
    const wrapper = makeWrapper()
    await flushPromises()
    await wrapper.get('.team-control button').trigger('click')
    session.token = 'other-account-token'
    await wrapper.setProps({ currentUserId: 2 })
    await flushPromises()
    rejectCancel(new Error('old request failed'))
    await flushPromises()
    expect(wrapper.text()).not.toContain('停止请求未确认')
    expect(wrapper.text()).toContain('代码双检')
    wrapper.unmount()
  })

  it('同标签页认证失效时清除已显示的团队结果', async () => {
    const wrapper = makeWrapper()
    await flushPromises()
    window.dispatchEvent(new Event('prism:auth-expired'))
    await flushPromises()
    expect(wrapper.text()).not.toContain('代码双检')
    expect(wrapper.text()).not.toContain('发现一项可空值访问风险。')
    wrapper.unmount()
  })

  it('卸载后迟到的列表响应不会读取团队详情', async () => {
    let resolveList!: (value: unknown) => void
    api.list.mockReturnValueOnce(new Promise(resolve => { resolveList = resolve }))
    const wrapper = makeWrapper()
    wrapper.unmount()
    resolveList({ items: [summary], has_more: false, next_before_id: null })
    await flushPromises()
    expect(api.detail).not.toHaveBeenCalled()
  })

  it('旧账号的重试响应不会刷新或覆盖新账号面板', async () => {
    let rejectRetry!: (reason: Error) => void
    api.retry.mockReturnValueOnce(new Promise((_resolve, reject) => { rejectRetry = reject }))
    const wrapper = makeWrapper()
    await flushPromises()
    await wrapper.get('.retry-task').trigger('click')
    session.token = 'other-account-token'
    await wrapper.setProps({ currentUserId: 2 })
    await flushPromises()
    const readCount = api.list.mock.calls.length
    rejectRetry(new Error('old request failed'))
    await flushPromises()
    expect(wrapper.text()).not.toContain('重试未提交')
    expect(api.list).toHaveBeenCalledTimes(readCount)
    expect(wrapper.text()).toContain('代码双检')
    wrapper.unmount()
  })

  it('凭据变化后不展示迟到的安全报告也不继续请求报告权限', async () => {
    api.detail.mockResolvedValueOnce({
      ...detail,
      tasks: [...detail.tasks, {
        task_id: 33, task_key: 'security', title: '项目安全扫描', agent_code: 'security_sentinel',
        agent_name: '安全哨兵', status: 'completed', attempt_count: 1,
        result: { security_artifact_id: 91 }, errors: [], started_at: null, completed_at: null,
      }],
    })
    let resolveArtifact!: (value: unknown) => void
    api.artifact.mockReturnValueOnce(new Promise(resolve => { resolveArtifact = resolve }))
    const wrapper = makeWrapper()
    await flushPromises()
    await wrapper.get('.security-report-trigger').trigger('click')
    session.token = 'other-account-token'
    resolveArtifact({ artifact_id: 91, enterprise_id: 10, team_id: 12, task_id: 33,
      report: { findings: [{ title: '旧账号报告秘密' }] } })
    await flushPromises()
    expect(wrapper.text()).not.toContain('旧账号报告秘密')
    expect(api.access).not.toHaveBeenCalled()
    wrapper.unmount()
  })
  it('加载第 21 条以后并在刷新期间保留有权查看的旧团队', async () => {
    let ids = Array.from({ length: 21 }, (_, index) => 40 - index)
    api.list.mockImplementation(async (_enterprise, _room, _token, before?: number) => {
      const candidates = ids.filter(id => before === undefined || id < before)
      const page = candidates.slice(0, 20)
      return { items: page.map(team_id => ({ ...summary, team_id, title: `团队 ${team_id}` })),
        has_more: candidates.length > 20, next_before_id: candidates.length > 20 ? page.at(-1) : null }
    })
    api.detail.mockImplementation(async (_enterprise, team_id) => ({ ...detail, team_id, title: `团队 ${team_id}` }))
    const wrapper = makeWrapper()
    await flushPromises()
    expect(wrapper.text()).not.toContain('团队 20')
    await wrapper.get('.load-more-teams').trigger('click')
    await flushPromises()
    expect(api.list).toHaveBeenCalledWith(10, 9, 'enterprise-session-token', 21)
    await wrapper.findAll('.team-choice').find(item => item.text().includes('团队 20'))!.trigger('click')
    await flushPromises()
    expect(wrapper.get('.team-detail h4').text()).toBe('团队 20')
    ids = [42, 41, ...ids]
    await wrapper.get('.progress-actions button').trigger('click')
    await flushPromises()
    expect(wrapper.get('.team-detail h4').text()).toBe('团队 20')
    expect(wrapper.text()).toContain('团队 42')
    wrapper.unmount()
  })

  it('切换会话时忽略旧会话迟到的分页结果', async () => {
    let resolveOld!: (value: unknown) => void
    api.list.mockImplementation((_enterprise, room, _token, before?: number) => {
      if (room === 9 && before === 12) return new Promise(resolve => { resolveOld = resolve })
      if (room === 9) return Promise.resolve({ items: [summary], has_more: true, next_before_id: 12 })
      return Promise.resolve({ items: [], has_more: false, next_before_id: null })
    })
    const wrapper = makeWrapper()
    await flushPromises()
    await wrapper.get('.load-more-teams').trigger('click')
    await wrapper.setProps({ conversationId: 99 })
    await flushPromises()
    resolveOld({ items: [{ ...summary, team_id: 11, title: '旧会话秘密' }], has_more: false, next_before_id: null })
    await flushPromises()
    expect(wrapper.text()).not.toContain('旧会话秘密')
    expect(wrapper.text()).not.toContain('代码双检')
    wrapper.unmount()
  })

  it('分页请求较慢时轮询不会反复中断加载', async () => {
    vi.useFakeTimers({ toFake: ['setInterval', 'clearInterval'] })
    let resolvePage!: (value: unknown) => void
    api.list.mockResolvedValueOnce({ items: [summary], has_more: true, next_before_id: 12 })
      .mockReturnValueOnce(new Promise(resolve => { resolvePage = resolve }))
    const wrapper = makeWrapper()
    await flushPromises()
    await wrapper.get('.load-more-teams').trigger('click')
    await vi.advanceTimersByTimeAsync(10000)
    expect(api.list).toHaveBeenCalledTimes(2)
    resolvePage({ items: [{ ...summary, team_id: 11, title: '较早的团队' }],
      has_more: false, next_before_id: null })
    await flushPromises()
    expect(wrapper.text()).toContain('较早的团队')
    wrapper.unmount()
  })

  it('分页读取权限被撤销时清除已加载的列表和详情', async () => {
    api.list.mockResolvedValueOnce({ items: [summary], has_more: true, next_before_id: 12 })
      .mockRejectedValueOnce(new Error('403'))
    const wrapper = makeWrapper()
    await flushPromises()
    expect(wrapper.text()).toContain('发现一项可空值访问风险。')
    await wrapper.get('.load-more-teams').trigger('click')
    await flushPromises()
    expect(wrapper.text()).not.toContain('代码双检')
    expect(wrapper.text()).not.toContain('发现一项可空值访问风险。')
    expect(wrapper.text()).toContain('当前账号无法读取此会话的团队进度。')
    wrapper.unmount()
  })

  it('已选旧团队撤权后刷新会清除旧正文并重新选择仍可看的团队', async () => {
    api.list.mockResolvedValueOnce({ items: [summary], has_more: true, next_before_id: 12 })
      .mockResolvedValueOnce({ items: [{ ...summary, team_id: 11, title: '旧团队' }], has_more: false, next_before_id: null })
      .mockResolvedValueOnce({ items: [summary], has_more: false, next_before_id: null })
    api.detail.mockImplementation(async (_enterprise, team_id) => ({ ...detail, team_id, title: `团队 ${team_id}`,
      objective: team_id === 11 ? '已撤权的内容' : '当前团队' }))
    const wrapper = makeWrapper()
    await flushPromises()
    await wrapper.get('.load-more-teams').trigger('click')
    await flushPromises()
    await wrapper.findAll('.team-choice').find(item => item.text().includes('旧团队'))!.trigger('click')
    await flushPromises()
    expect(wrapper.text()).toContain('已撤权的内容')
    let resolveDetail!: (value: unknown) => void
    api.detail.mockReturnValueOnce(new Promise(resolve => { resolveDetail = resolve }))
    await wrapper.get('.progress-actions button').trigger('click')
    await flushPromises()
    expect(wrapper.text()).not.toContain('已撤权的内容')
    resolveDetail({ ...detail, team_id: 12 })
    await flushPromises()
    expect(wrapper.text()).toContain('代码双检')
    wrapper.unmount()
  })

  it('点击旧团队时权限被撤销则清除整个面板的旧数据', async () => {
    api.list.mockResolvedValueOnce({ items: [summary, { ...summary, team_id: 11, title: '旧团队' }],
      has_more: false, next_before_id: null })
    const wrapper = makeWrapper()
    await flushPromises()
    api.detail.mockRejectedValueOnce(new Error('403'))
    await wrapper.findAll('.team-choice').find(item => item.text().includes('旧团队'))!.trigger('click')
    await flushPromises()
    expect(wrapper.text()).not.toContain('旧团队')
    expect(wrapper.text()).not.toContain('代码双检')
    expect(wrapper.text()).toContain('当前账号无法读取此会话的团队进度。')
    wrapper.unmount()
  })

  it('按会话范围读取任务结果并允许有权管理员停止团队', async () => {
    const wrapper = makeWrapper()
    await flushPromises()

    expect(api.list).toHaveBeenCalledWith(10, 9, 'enterprise-session-token')
    expect(api.detail).toHaveBeenCalledWith(10, 12, 'enterprise-session-token')
    expect(wrapper.text()).toContain('发现一项可空值访问风险。')
    expect(wrapper.text()).toContain('已结束 1/2 项')

    await wrapper.get('.team-control button').trigger('click')
    await flushPromises()
    expect(api.cancel).toHaveBeenCalledWith(10, 12, '从企业会话进度面板停止', 'enterprise-session-token')
    wrapper.unmount()
  })

  it('只允许发起人或当前会话管理者重试无费用失败任务', async () => {
    const wrapper = makeWrapper({ roomKind: 'direct', canManageCompany: true, canManageGroup: true })
    await flushPromises()

    expect(wrapper.find('.retry-task').exists()).toBe(false)
    expect(wrapper.find('.team-control').exists()).toBe(false)
    wrapper.unmount()

    const initiator = makeWrapper({ currentUserId: 2, roomKind: 'direct' })
    await flushPromises()
    await initiator.get('.retry-task').trigger('click')
    await flushPromises()
    expect(api.retry).toHaveBeenCalledWith(10, 12, 32, 'enterprise-session-token')
    initiator.unmount()
  })

  it('小组长仅在小组群中可以控制本组任务，组员只能查看', async () => {
    const groupSummary = { ...summary, group_id: 70 }
    const groupDetail = { ...detail, group_id: 70 }
    api.list.mockResolvedValue({ items: [groupSummary], has_more: false, next_before_id: null })
    api.detail.mockResolvedValue(groupDetail)

    const leader = makeWrapper({ roomKind: 'group', canManageGroup: true })
    await flushPromises()
    expect(leader.find('.team-control button').exists()).toBe(true)
    await leader.get('.team-control button').trigger('click')
    await flushPromises()
    expect(api.cancel).toHaveBeenCalled()
    leader.unmount()

    api.list.mockResolvedValue({ items: [groupSummary], has_more: false, next_before_id: null })
    api.detail.mockResolvedValue(groupDetail)
    const member = makeWrapper({ roomKind: 'group', canManageGroup: false, canManageCompany: false })
    await flushPromises()
    expect(member.text()).toContain('发现一项可空值访问风险。')
    expect(member.find('.team-control').exists()).toBe(false)
    expect(member.find('.retry-task').exists()).toBe(false)
    member.unmount()
  })

  it('前端再次过滤跨会话或无读取授权的摘要', async () => {
    api.list.mockResolvedValue({ items: [
      summary,
      { ...summary, team_id: 13, can_read_conversation: false },
      { ...summary, team_id: 14, conversation_id: 88 },
    ], has_more: false, next_before_id: null })
    const wrapper = makeWrapper({ canManageCompany: false })
    await flushPromises()

    expect(wrapper.text()).toContain('代码双检')
    expect(wrapper.text()).not.toContain('协作团队 13')
    expect(wrapper.text()).not.toContain('协作团队 14')
    wrapper.unmount()
  })

  it('详情读取被撤权并返回 404 后清除旧团队结果', async () => {
    const wrapper = makeWrapper()
    await flushPromises()
    expect(wrapper.text()).toContain('发现一项可空值访问风险。')

    api.detail.mockRejectedValueOnce(new Error('404'))
    await wrapper.get('.progress-actions button').trigger('click')
    await flushPromises()

    expect(wrapper.text()).not.toContain('发现一项可空值访问风险。')
    expect(wrapper.text()).toContain('当前账号无法读取此会话的团队进度。')
    wrapper.unmount()
  })

  it('忽略其他会话的过期响应和结果', async () => {
    let resolveOld!: (value: unknown) => void
    api.list.mockReturnValueOnce(new Promise(resolve => { resolveOld = resolve }))
    const wrapper = makeWrapper()
    await wrapper.setProps({ conversationId: 99 })
    await flushPromises()
    resolveOld({ items: [summary], has_more: false, next_before_id: null })
    await flushPromises()

    expect(wrapper.text()).not.toContain('代码双检')
    expect(api.list).toHaveBeenLastCalledWith(10, 99, 'enterprise-session-token')
    wrapper.unmount()
  })

  it('仅在有安全报告引用时按需读取并展示范围、覆盖情况和发现项', async () => {
    api.detail.mockResolvedValueOnce({
      ...detail,
      tasks: [...detail.tasks, {
        task_id: 33, task_key: 'security', title: '项目安全扫描', agent_code: 'security_sentinel',
        agent_name: '安全哨兵', status: 'completed', attempt_count: 1,
        result: {
          status: 'completed', summary: '扫描完成', security_artifact_id: 91,
          coverage_status: 'complete',
        }, errors: [], started_at: null, completed_at: null,
      }],
    })
    const wrapper = makeWrapper()
    await flushPromises()

    expect(wrapper.find('.security-report-trigger').exists()).toBe(true)
    expect(api.artifact).not.toHaveBeenCalled()
    await wrapper.get('.security-report-trigger').trigger('click')
    await flushPromises()

    expect(api.artifact).toHaveBeenCalledWith(10, 12, 91, 'enterprise-session-token')
    expect(api.access).toHaveBeenCalledWith(10, 12, 91, 'enterprise-session-token')
    expect(wrapper.text()).toContain('项目安全报告')
    expect(wrapper.text()).toContain('覆盖完整')
    expect(wrapper.text()).toContain('src/input.ts:18')
    expect(wrapper.text()).toContain('存在注入风险')
    expect(wrapper.text()).toContain('a'.repeat(64))
    wrapper.unmount()
  })

  it('打开报告时 HEAD 权限检查拒绝则不显示报告正文', async () => {
    api.detail.mockResolvedValueOnce({
      ...detail,
      tasks: [...detail.tasks, {
        task_id: 33, task_key: 'security', title: '项目安全扫描', agent_code: 'security_sentinel',
        agent_name: '安全哨兵', status: 'completed', attempt_count: 1,
        result: { security_artifact_id: 91, coverage_status: 'complete' },
        errors: [], started_at: null, completed_at: null,
      }],
    })
    api.access.mockRejectedValueOnce(new Error('403'))
    const wrapper = makeWrapper()
    await flushPromises()
    await wrapper.get('.security-report-trigger').trigger('click')
    await flushPromises()

    expect(wrapper.text()).toContain('安全报告读取失败')
    expect(wrapper.text()).not.toContain('存在注入风险')
    wrapper.unmount()
  })

  it('三分流模式明确标注为风险优先子集，不宣称完整项目覆盖', async () => {
    api.detail.mockResolvedValueOnce({
      ...detail,
      tasks: [...detail.tasks, {
        task_id: 33, task_key: 'security', title: '项目安全扫描', agent_code: 'security_sentinel',
        agent_name: '安全哨兵', status: 'completed', attempt_count: 1,
        result: { security_artifact_id: 91, coverage_status: 'complete' },
        errors: [], started_at: null, completed_at: null,
      }],
    })
    api.artifact.mockResolvedValueOnce({
      artifact_id: 91, team_id: 12, task_id: 33, attempt_no: 1, lease_generation: 1,
      enterprise_id: 10, project_id: 701, audit_run_id: 'run_91',
      source_sha256: 'a'.repeat(64), report_sha256: 'b'.repeat(64),
      coverage_status: 'complete', termination_reason: '',
      summary: { summary: '风险优先扫描完成', finding_count: 0, provider_request_count: 0 },
      report: { findings: [], compliance: { scan_mode: 'triage', provider_request_count: 0 } },
      created_at: null,
    })
    const wrapper = makeWrapper()
    await flushPromises()
    await wrapper.get('.security-report-trigger').trigger('click')
    await flushPromises()

    expect(wrapper.text()).toContain('风险优先扫描已完成')
    expect(wrapper.text()).toContain('风险优先子集')
    expect(wrapper.text()).toContain('仅扫描风险优先子集，不表示完整项目覆盖')
    expect(wrapper.text()).not.toContain('覆盖完整')
    wrapper.unmount()
  })

  it('刷新时项目权限失效会清除已显示的报告正文', async () => {
    api.detail.mockResolvedValue({
      ...detail,
      tasks: [...detail.tasks, {
        task_id: 33, task_key: 'security', title: '项目安全扫描', agent_code: 'security_sentinel',
        agent_name: '安全哨兵', status: 'completed', attempt_count: 1,
        result: { security_artifact_id: 91, coverage_status: 'complete' },
        errors: [], started_at: null, completed_at: null,
      }],
    })
    const wrapper = makeWrapper()
    await flushPromises()
    await wrapper.get('.security-report-trigger').trigger('click')
    await flushPromises()
    expect(wrapper.text()).toContain('存在注入风险')

    api.access.mockRejectedValueOnce(new Error('403'))
    await wrapper.get('.progress-actions button').trigger('click')
    await flushPromises()

    expect(api.access).toHaveBeenLastCalledWith(10, 12, 91, 'enterprise-session-token')
    expect(wrapper.text()).not.toContain('存在注入风险')
    expect(wrapper.text()).not.toContain('未校验输入')
    expect(wrapper.text()).toContain('安全报告正文已清除')
    wrapper.unmount()
  })

  it('同一报告的慢速 HEAD 跨过多个轮询周期仍会在撤权时清屏', async () => {
    vi.useFakeTimers({ toFake: ['setInterval', 'clearInterval'] })
    const securityDetail = {
      ...detail,
      tasks: [...detail.tasks, {
        task_id: 33, task_key: 'security', title: '项目安全扫描', agent_code: 'security_sentinel',
        agent_name: '安全哨兵', status: 'completed', attempt_count: 1,
        result: { security_artifact_id: 91, coverage_status: 'complete' },
        errors: [], started_at: null, completed_at: null,
      }],
    }
    api.detail.mockResolvedValue(securityDetail)
    const wrapper = makeWrapper()
    await flushPromises()
    await wrapper.get('.security-report-trigger').trigger('click')
    await flushPromises()
    expect(wrapper.text()).toContain('存在注入风险')

    let rejectAccess!: (reason: Error) => void
    api.access.mockImplementation(() => new Promise((_resolve, reject) => { rejectAccess = reject }))
    await vi.advanceTimersByTimeAsync(5000)
    await flushPromises()
    const callsAfterFirstCheck = api.access.mock.calls.length
    await vi.advanceTimersByTimeAsync(10000)
    await flushPromises()
    expect(api.access).toHaveBeenCalledTimes(callsAfterFirstCheck)
    expect(callsAfterFirstCheck).toBe(2)

    rejectAccess(new Error('403'))
    await flushPromises()
    expect(wrapper.text()).not.toContain('存在注入风险')
    expect(wrapper.text()).not.toContain('未校验输入')
    expect(wrapper.text()).toContain('安全报告正文已清除')
    wrapper.unmount()
  })

  it('旧报告迟到的 HEAD 拒绝不会清除已切换并加载的新报告', async () => {
    vi.useFakeTimers({ toFake: ['setInterval', 'clearInterval'] })
    const securityDetail = {
      ...detail,
      tasks: [...detail.tasks,
        {
          task_id: 33, task_key: 'security-a', title: '旧项目扫描', agent_code: 'security_sentinel',
          agent_name: '安全哨兵', status: 'completed', attempt_count: 1,
          result: { security_artifact_id: 91, coverage_status: 'complete' },
          errors: [], started_at: null, completed_at: null,
        },
        {
          task_id: 34, task_key: 'security-b', title: '新项目扫描', agent_code: 'security_sentinel',
          agent_name: '安全哨兵', status: 'completed', attempt_count: 1,
          result: { security_artifact_id: 92, coverage_status: 'complete' },
          errors: [], started_at: null, completed_at: null,
        },
      ],
    }
    const report = (artifactId: number, taskId: number, title: string) => ({
      artifact_id: artifactId, team_id: 12, task_id: taskId, attempt_no: 1, lease_generation: 1,
      enterprise_id: 10, project_id: 701, audit_run_id: `run_${artifactId}`,
      source_sha256: 'a'.repeat(64), report_sha256: 'b'.repeat(64),
      coverage_status: 'complete', termination_reason: '',
      summary: { summary: title, finding_count: 1, provider_request_count: 0 },
      report: { findings: [{ title }], compliance: { scan_mode: 'full', provider_request_count: 0 } },
      created_at: null,
    })
    api.detail.mockResolvedValue(securityDetail)
    api.artifact.mockResolvedValueOnce(report(91, 33, '旧报告内容'))
      .mockResolvedValueOnce(report(92, 34, '新报告内容'))
    const wrapper = makeWrapper()
    await flushPromises()
    await wrapper.findAll('.security-report-trigger')[0].trigger('click')
    await flushPromises()
    expect(wrapper.text()).toContain('旧报告内容')

    let rejectOldAccess!: (reason: Error) => void
    api.access.mockReturnValueOnce(new Promise((_resolve, reject) => { rejectOldAccess = reject }))
    await vi.advanceTimersByTimeAsync(5000)
    await flushPromises()

    await wrapper.findAll('.security-report-trigger')[1].trigger('click')
    await flushPromises()
    expect(wrapper.text()).toContain('新报告内容')
    rejectOldAccess(new Error('403'))
    await flushPromises()

    expect(wrapper.text()).toContain('新报告内容')
    expect(wrapper.text()).not.toContain('安全报告正文已清除')
    wrapper.unmount()
  })

  it('报告读取失败可重试，关闭后清除正文', async () => {
    api.detail.mockResolvedValueOnce({
      ...detail,
      tasks: [...detail.tasks, {
        task_id: 33, task_key: 'security', title: '项目安全扫描', agent_code: 'security_sentinel',
        agent_name: '安全哨兵', status: 'completed', attempt_count: 1,
        result: { security_artifact_id: 91, coverage_status: 'incomplete' },
        errors: [], started_at: null, completed_at: null,
      }],
    })
    api.artifact.mockRejectedValueOnce(new Error('403'))
    const wrapper = makeWrapper()
    await flushPromises()
    await wrapper.get('.security-report-trigger').trigger('click')
    await flushPromises()

    expect(wrapper.text()).toContain('安全报告读取失败')
    await wrapper.get('.security-report-retry').trigger('click')
    await flushPromises()
    expect(api.artifact).toHaveBeenCalledTimes(2)
    expect(wrapper.text()).toContain('存在注入风险')
    await wrapper.get('.security-report-close').trigger('click')
    expect(wrapper.text()).not.toContain('存在注入风险')
    wrapper.unmount()
  })

  it('切换企业后丢弃尚未返回的旧报告', async () => {
    api.detail.mockResolvedValueOnce({
      ...detail,
      tasks: [...detail.tasks, {
        task_id: 33, task_key: 'security', title: '项目安全扫描', agent_code: 'security_sentinel',
        agent_name: '安全哨兵', status: 'completed', attempt_count: 1,
        result: { security_artifact_id: 91, coverage_status: 'complete' },
        errors: [], started_at: null, completed_at: null,
      }],
    })
    let resolveArtifact!: (value: unknown) => void
    api.artifact.mockReturnValueOnce(new Promise(resolve => { resolveArtifact = resolve }))
    const wrapper = makeWrapper()
    await flushPromises()
    await wrapper.get('.security-report-trigger').trigger('click')
    await wrapper.setProps({ enterpriseId: 11 })
    await flushPromises()
    resolveArtifact({ artifact_id: 91, enterprise_id: 10, team_id: 12, report: { findings: [] } })
    await flushPromises()

    expect(wrapper.text()).not.toContain('项目安全报告')
    expect(wrapper.text()).not.toContain('未校验输入')
    wrapper.unmount()
  })

  it('隐藏标签页暂停团队轮询，重新显示后立即刷新', async () => {
    vi.useFakeTimers({ toFake: ['setInterval', 'clearInterval'] })
    Object.defineProperty(document, 'visibilityState', { configurable: true, value: 'hidden' })
    const wrapper = makeWrapper()
    await flushPromises()
    const initialReads = api.list.mock.calls.length

    await vi.advanceTimersByTimeAsync(5000)
    expect(api.list).toHaveBeenCalledTimes(initialReads)

    Object.defineProperty(document, 'visibilityState', { configurable: true, value: 'visible' })
    document.dispatchEvent(new Event('visibilitychange'))
    await flushPromises()
    expect(api.list).toHaveBeenCalledTimes(initialReads + 1)
    wrapper.unmount()
  })
})
