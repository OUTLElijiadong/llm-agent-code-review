import fs from 'node:fs'
import path from 'node:path'
import { NodeTypes, parse as parseTemplate, type RootNode, type TemplateChildNode } from '@vue/compiler-dom'
import { parse as parseSfc } from '@vue/compiler-sfc'
import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import ElementPlus, { ElPagination, ElTable } from 'element-plus'
import zhCn from 'element-plus/es/locale/lang/zh-cn'
import { nextTick } from 'vue'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const api = vi.hoisted(() => ({
  getGovernanceOverview: vi.fn(),
  listGovernanceAgents: vi.fn(),
  listApprovals: vi.fn(),
  listAlerts: vi.fn(),
  listAlertsPage: vi.fn(),
  listToolCalls: vi.fn(),
  listPolicies: vi.fn(),
  listPolicyDecisions: vi.fn(),
  listToolPermissions: vi.fn(),
  listAgentMemory: vi.fn(),
  listAgentKnowledge: vi.fn(),
  listAgentKnowledgeSources: vi.fn(),
  listJobs: vi.fn(),
  getObservabilityOverview: vi.fn(),
  listRewardEvents: vi.fn(),
  listArtifactVersions: vi.fn(),
}))

vi.mock('@/api/adminGovernance', () => api)
vi.mock('@/stores/user', () => ({
  useUserStore: () => ({ isSuperAdmin: () => true }),
}))
vi.mock('vue-router', () => ({ useRouter: () => ({ push: vi.fn() }) }))

import AgentGovernance from './AgentGovernance.vue'
import GovernanceWorkstation from './GovernanceWorkstation.vue'

type TestedMode = 'policies' | 'overview' | 'agents' | 'approvals' | 'tools' | 'knowledge' | 'jobs' | 'observability' | 'rewards' | 'rollback'
const wrappers: VueWrapper[] = []
const renderErrors: unknown[] = []

function makeAgent(category: unknown = 'governance') {
  return {
    code: 'review_orchestrator', name: '审查编排测试', description: '隔离回归样例', category,
    status: 'idle', icon: '', color: '', budget_tokens_daily: 0, priority: 52,
    auto_approval_threshold: 0.74, is_enabled: 1, skills: ['knowledge_read'],
    tool_count: 1, memory_count: 1, knowledge_count: 1,
  }
}

function deferred<Value>() {
  let resolve!: (value: Value) => void
  let reject!: (reason: unknown) => void
  const promise = new Promise<Value>((resolvePromise, rejectPromise) => {
    resolve = resolvePromise
    reject = rejectPromise
  })
  return { promise, resolve, reject }
}

function mountMode(mode: TestedMode) {
  const global = {
    plugins: [[ElementPlus, { locale: zhCn }] as [typeof ElementPlus, { locale: typeof zhCn }]],
    config: { errorHandler: (error: unknown) => { renderErrors.push(error) } },
  }
  const wrapper = mode === 'agents'
    ? mount(AgentGovernance, { attachTo: document.body, global })
    : mount(GovernanceWorkstation, { props: { mode }, attachTo: document.body, global })
  wrappers.push(wrapper)
  return wrapper
}

async function settle() {
  await flushPromises()
  await nextTick()
  await flushPromises()
}

function tableCells(wrapper: VueWrapper, tableIndex = 0, rowIndex = 0) {
  const table = wrapper.findAllComponents(ElTable)[tableIndex]
  expect(table, `real Element Plus table ${tableIndex}`).toBeDefined()
  const rows = table!.findAll('tbody tr.el-table__row')
  expect(rows[rowIndex], `real table row ${rowIndex}`).toBeDefined()
  return rows[rowIndex]!.findAll('td').map(cell => cell.text())
}

beforeEach(() => {
  vi.resetAllMocks()
  renderErrors.length = 0
  api.getGovernanceOverview.mockResolvedValue({
    agents_total: 1, agents_enabled: 1, approvals_pending: 1, tool_calls_today: 1,
    alerts_open: 0, knowledge_docs_total: 1,
  })
  api.listGovernanceAgents.mockResolvedValue([makeAgent()])
  api.listApprovals.mockResolvedValue([{
    id: 1, title: '隔离审批样例', agent_code: 'review_orchestrator', action: 'knowledge.read',
    resource: 'fixture', risk_level: 'high', status: 'pending',
  }])
  api.listAlerts.mockResolvedValue([])
  api.listAlertsPage.mockResolvedValue({ items: [], total: 0, page: 1, page_size: 20, pages: 0 })
  api.listPolicies.mockResolvedValue([])
  api.listPolicyDecisions.mockResolvedValue([])
  api.listToolPermissions.mockResolvedValue([{
    id: 2, agent_code: 'manager', tool_code: 'shell', permission: 'escalate', risk_level: 'high', enabled: 1,
  }])
  api.listToolCalls.mockResolvedValue([{
    id: 3, agent_code: 'review_orchestrator', tool_code: 'knowledge_read', action: 'knowledge.read',
    resource: 'fixture', decision: 'allow', status: 'success', risk_level: 'low', duration_ms: 12,
  }])
  api.listAgentMemory.mockResolvedValue([{
    id: 4, agent_code: 'review_orchestrator', title: '隔离记忆样例', memory_type: 'long_term',
    content: '仅用于本地测试', weight: 1, status: 'active',
  }])
  api.listAgentKnowledge.mockResolvedValue([{
    id: 5, agent_code: 'review_orchestrator', title: '隔离知识样例', source_type: 'manual',
    risk_level: 'low', confidence: 1, status: 'active', char_count: 8, chunk_count: 1,
  }])
  api.listAgentKnowledgeSources.mockResolvedValue([{
    id: 6, agent_code: 'review_orchestrator', source_type: 'inline', source_uri: 'fixture-only',
    whitelist: 1, enabled: 1,
  }])
  api.getObservabilityOverview.mockResolvedValue({})
  api.listJobs.mockResolvedValue([])
  api.listRewardEvents.mockResolvedValue([{
    id: 7, agent_code: 'review_orchestrator', event_type: 'reward', score: 2, reason: '隔离回归样例',
  }])
  api.listArtifactVersions.mockResolvedValue([{
    id: 8, agent_code: 'review_orchestrator', artifact_type: 'policy', version: 'fixture-v1', status: 'draft',
  }])
})

afterEach(() => {
  for (const wrapper of wrappers.splice(0)) wrapper.unmount()
  document.body.innerHTML = ''
  expect(renderErrors).toEqual([])
})

describe('admin governance interpolation in real cells and agent cards', () => {
  it('总览与执行审批页使用相同的可见待办口径，排除专用发布审批', async () => {
    const overview = mountMode('overview')
    await settle()
    expect(api.listApprovals).toHaveBeenLastCalledWith('pending', 'agent_package.publish')
    expect(api.listAlerts).not.toHaveBeenCalled()
    expect(overview.text()).toContain('执行审批待办')
    overview.unmount()

    const approvals = mountMode('approvals')
    await settle()
    expect(api.listApprovals).toHaveBeenLastCalledWith('pending', 'agent_package.publish')
    approvals.unmount()
  })

  it('has no single-brace function-call text in any admin Vue template', () => {
    const directory = path.resolve('src/views/admin')
    const candidates: string[] = []
    for (const name of fs.readdirSync(directory).filter(name => name.endsWith('.vue'))) {
      const descriptor = parseSfc(fs.readFileSync(path.join(directory, name), 'utf8')).descriptor
      if (!descriptor.template) continue
      const visit = (node: RootNode | TemplateChildNode) => {
        if (node.type === NodeTypes.TEXT) {
          for (const match of node.content.matchAll(/\{\s*\w+\([^{}]*\)\s*\}/g)) candidates.push(`${name}: ${match[0]}`)
        }
        if (node.type === NodeTypes.ROOT || node.type === NodeTypes.ELEMENT) node.children.forEach(visit)
      }
      visit(parseTemplate(descriptor.template.content))
    }
    expect(candidates).toEqual([])
  })

  const cases: Array<{ mode: TestedMode; cells: Array<[number, number, string]> }> = [
    { mode: 'overview', cells: [[0, 1, '治理']] },
    { mode: 'approvals', cells: [[0, 1, '审查编排'], [0, 2, '读取知识']] },
    { mode: 'tools', cells: [
      [0, 0, '小菱·管理权限兼容模块（系统）'], [0, 1, '命令执行'], [0, 2, '升级审批'], [0, 3, '高风险'],
      [1, 0, '审查编排'], [1, 1, '读取知识'], [1, 2, '读取知识'], [1, 3, '允许'], [1, 4, '成功'], [1, 5, '低风险'],
    ] },
    { mode: 'knowledge', cells: [[0, 0, '内联'], [1, 1, '长期记忆'], [2, 1, '手动录入'], [2, 3, '生效']] },
    { mode: 'rewards', cells: [[0, 0, '审查编排']] },
    { mode: 'rollback', cells: [[0, 1, '策略']] },
  ]

  it.each(cases)('renders translated cells for $mode without shallow table stubs', async ({ mode, cells }) => {
    const wrapper = mountMode(mode)
    await settle()
    for (const [table, column, text] of cells) expect(tableCells(wrapper, table)[column]).toBe(text)
    for (const cell of wrapper.findAll('tbody td')) expect(cell.text()).not.toMatch(/\{\s*\w+\([^{}]*\)\s*\}/)
  })

  it('translates known operations job names, types, and schedules while preserving unknown expressions', async () => {
    api.listJobs.mockResolvedValue([
      { id: 31, job_code: 'ops health check', job_type: 'ops_health_check', agent_code: 'operations', schedule: 'interval@5m', status: 'enabled' },
      { id: 32, job_code: 'new_future_job', job_type: 'future_type', agent_code: null, schedule: '0 3 * * *', status: 'enabled' },
      { id: 33, job_code: 'sandbox heartbeat', job_type: 'sandbox_heartbeat', agent_code: null, schedule: 'daily@02:30', status: 'enabled' },
      { id: 34, job_code: 'security monitor', job_type: 'security_monitor', agent_code: null, schedule: 'hourly@*:00', status: 'enabled' },
      { id: 35, job_code: 'hourly_skill_proactive_code_reviewer', job_type: 'skill_proactive', agent_code: 'code_reviewer', schedule: 'hourly@*:00', status: 'enabled' },
      { id: 36, job_code: 'quarter_hour_check', job_type: 'manual', agent_code: null, schedule: 'hourly@*:15', status: 'enabled' },
      { id: 37, job_code: 'invalid_interval', job_type: 'manual', agent_code: null, schedule: 'interval@0m', status: 'enabled' },
    ])
    const wrapper = mountMode('jobs')
    await settle()

    const firstRow = wrapper.findAll('tbody tr.el-table__row')[0]!
    const firstCells = firstRow.findAll('td')
    expect(firstCells[0]!.text()).toBe('运维健康检查')
    expect(firstCells[1]!.text()).toBe('运维健康检查')
    expect(firstCells[2]!.text()).toBe('全服管理')
    expect(firstCells[3]!.text()).toBe('每 5 分钟')
    expect((firstCells[3]!.find('input').element as HTMLInputElement).value).toBe('interval@5m')
    expect(tableCells(wrapper, 0, 1)[0]).toBe('new future job')
    expect(tableCells(wrapper, 0, 1)[1]).toBe('future_type')
    const unknownScheduleCell = wrapper.findAll('tbody tr.el-table__row')[1]!.findAll('td')[3]!
    expect((unknownScheduleCell.find('input').element as HTMLInputElement).value).toBe('0 3 * * *')
    expect(unknownScheduleCell.find('.job-schedule-human').exists()).toBe(false)
    expect(tableCells(wrapper, 0, 2)[0]).toBe('沙箱心跳检查')
    expect(tableCells(wrapper, 0, 2)[3]).toBe('每天 02:30')
    expect(tableCells(wrapper, 0, 3)[0]).toBe('安全监控')
    expect(tableCells(wrapper, 0, 3)[3]).toBe('每小时整点')
    expect(tableCells(wrapper, 0, 4)[0]).toBe('每小时·主动技能·代码审查员')
    expect(tableCells(wrapper, 0, 4)[1]).toBe('主动技能检查')
    expect(tableCells(wrapper, 0, 5)[3]).toBe('每小时的第 15 分钟')
    expect(tableCells(wrapper, 0, 6)[3]).toBe('')
    const invalidScheduleCell = wrapper.findAll('tbody tr.el-table__row')[6]!.findAll('td')[3]!
    const invalidScheduleInput = invalidScheduleCell.find('.el-input')
    expect((invalidScheduleInput.find('input').element as HTMLInputElement).value).toBe('interval@0m')
    expect(invalidScheduleInput.classes()).toContain('is-cron-invalid')
    expect(wrapper.findAll('tbody tr.el-table__row')[6]!.find('.job-schedule-human').exists()).toBe(false)
  })

  it('explains that execution totals are cumulative while alerts are current open items', async () => {
    api.getObservabilityOverview.mockResolvedValue({
      open_alerts: 99,
      job_runs: 130788,
      tool_status: [{ status: 'failed', count: 43188 }],
      approval_status: [],
    })
    api.listAlertsPage.mockResolvedValue({
      items: Array.from({ length: 20 }, (_, index) => ({
        id: 105 - index, alert_type: 'fixture', severity: 'warning', status: 'open', title: `告警 ${index + 1}`,
      })),
      total: 105,
      page: 1,
      page_size: 20,
      pages: 6,
    })
    const wrapper = mountMode('observability')
    await settle()

    expect(wrapper.text()).toContain('工具调用、调度执行和奖惩均为累计记录；审批按事项当前状态分组；开放告警只统计当前未关闭项。')
    expect(wrapper.text()).toContain('当前开放告警')
    expect(wrapper.text()).toContain('开放告警（105）')
    expect(wrapper.text()).toContain('共 105 条')
    expect(wrapper.findAllComponents(ElTable)[2]!.findAll('tbody tr.el-table__row')).toHaveLength(20)
    expect(wrapper.text()).toContain('调度执行次数（累计）')
    expect(wrapper.text()).toContain('工具执行结果（累计）')
    expect(wrapper.text()).toContain('审批事项当前状态分布')
    expect(wrapper.text()).toContain('事项数')
    expect(wrapper.text()).toContain('暂无审批事项状态记录')
    expect(wrapper.text()).toContain('状态')
    expect(wrapper.text()).not.toContain('审批渠道状态为空')
  })

  it('retains the empty state when the paginated alert result is empty', async () => {
    api.listAlertsPage.mockResolvedValue({ items: [], total: 0, page: 1, page_size: 20, pages: 0 })
    const wrapper = mountMode('observability')
    await settle()

    expect(wrapper.text()).toContain('当前没有未关闭告警。历史失败记录请查看工具执行统计。')
    expect(wrapper.text()).toContain('共 0 条')
  })

  it('does not present stale alert rows or a false zero when pagination fails', async () => {
    api.listAlertsPage.mockRejectedValue(new Error('告警接口暂不可用'))
    const wrapper = mountMode('observability')
    await settle()

    expect(wrapper.text()).toContain('告警接口暂不可用')
    expect(wrapper.text()).toContain('开放告警（—）')
    expect(wrapper.findAllComponents(ElTable)[2]!.findAll('tbody tr.el-table__row')).toHaveLength(0)
    expect(wrapper.get('.alert-list-error button').text()).toBe('重试')
  })

  it('changes alert page through the UI and requests that page instead of truncating the list', async () => {
    api.listAlertsPage.mockImplementation(async (_status: string, page: number, pageSize: number) => ({
      items: [{ id: 106 - page, alert_type: 'fixture', severity: 'warning', status: 'open', title: `第 ${page} 页告警` }],
      total: 105,
      page,
      page_size: pageSize,
      pages: Math.ceil(105 / pageSize),
    }))
    const wrapper = mountMode('observability')
    await settle()

    expect(api.listAlertsPage).toHaveBeenLastCalledWith('open', 1, 20)
    const pagination = wrapper.findComponent(ElPagination)
    expect(pagination.exists()).toBe(true)
    const secondPage = wrapper.find('.el-pager li.number:nth-child(2)')
    expect(secondPage.exists()).toBe(true)
    await secondPage.trigger('click')
    await settle()

    expect(api.listAlertsPage).toHaveBeenLastCalledWith('open', 2, 20)
    expect(wrapper.text()).toContain('第 2 页告警')
  })

  it('returns to the last valid page when the alert count shrinks', async () => {
    let countShrank = false
    api.listAlertsPage.mockImplementation(async (_status: string, page: number, pageSize: number) => {
      if (page === 6) {
        countShrank = true
        return { items: [], total: 25, page, page_size: pageSize, pages: 2 }
      }
      return {
        items: [{ id: 200 - page, alert_type: 'fixture', severity: 'warning', status: 'open', title: `有效页 ${page}` }],
        total: countShrank ? 25 : 105,
        page,
        page_size: pageSize,
        pages: countShrank ? 2 : 6,
      }
    })
    const wrapper = mountMode('observability')
    await settle()

    await wrapper.find('.el-pager li.number:last-child').trigger('click')
    await settle()

    expect(api.listAlertsPage).toHaveBeenLastCalledWith('open', 2, 20)
    expect(wrapper.text()).toContain('有效页 2')
    expect(wrapper.text()).toContain('开放告警（25）')
  })

  it('renders all 35 agent cards without presenting the meta category as a second main agent', async () => {
    const categories = [
      ['meta', '系统支撑'], ['frontline', '前台'], ['governance', '治理'], ['operations', '运维'],
      ['security', '安全'], ['knowledge', '知识'], ['quality', '质量'], ['general', '通用'],
    ]
    api.listGovernanceAgents.mockResolvedValue(Array.from({ length: 35 }, (_, index) => ({
      ...makeAgent(categories[index % categories.length]![0]), code: `fixture-${index}`, name: `隔离样例 ${index}`,
    })))
    const wrapper = mountMode('agents')
    await settle()
    const cards = wrapper.findAll('.agent-card')
    expect(cards).toHaveLength(35)
    for (let index = 0; index < 35; index++) expect(cards[index]!.get('.agent-category').text()).toBe(categories[index % categories.length]![1])
  })

  it('默认收起小菱的内部调度与兼容条目，保留监督子 Agent 并可展开查看系统项', async () => {
    api.listGovernanceAgents.mockResolvedValue(['chat_assistant', 'code_reviewer', 'manager', 'orchestrator', 'supervisor'].map((code) => ({
      ...makeAgent('meta'), code, name: code,
    })))
    const wrapper = mountMode('agents')
    await settle()
    expect(wrapper.findAll('.agent-card')).toHaveLength(3)
    expect(wrapper.text()).toContain('小菱是唯一主控')
    expect(wrapper.text()).not.toContain('小菱·管理权限兼容模块（系统）')
    expect(wrapper.text()).toContain('小菱监督子 Agent')

    await wrapper.get('.agent-system-disclosure button').trigger('click')
    expect(wrapper.findAll('.agent-card')).toHaveLength(5)
    expect(wrapper.text()).toContain('小菱·内部调度模块（系统）')
    expect(wrapper.text()).toContain('小菱·管理权限兼容模块（系统）')
  })

  it.each([
    { category: null, text: '未提供分类' },
    { category: undefined, text: '未提供分类' },
    { category: '', text: '未提供分类' },
    { category: ' \u3000 ', text: '未提供分类' },
    { category: 'future_category', text: '未知分类（future_category）' },
    { category: 'constructor', text: '未知分类（constructor）' },
    { category: '__proto__', text: '未知分类（__proto__）' },
    { category: 3, text: '分类格式异常' },
    { category: { unexpected: true }, text: '分类格式异常' },
  ])('shows honest fallback for category $category', async ({ category, text }) => {
    api.listGovernanceAgents.mockResolvedValue([{ ...makeAgent(), category }])
    const wrapper = mountMode('agents')
    await settle()
    expect(wrapper.get('.agent-category').text()).toBe(text)
  })

  it('keeps unknown category markup as inert text', async () => {
    const category = '<img src=x onerror=alert(1)>'
    api.listGovernanceAgents.mockResolvedValue([makeAgent(category)])
    const wrapper = mountMode('agents')
    await settle()
    expect(wrapper.get('.agent-category').text()).toBe(`未知分类（${category}）`)
    expect(wrapper.find('.agent-card img').exists()).toBe(false)
  })
})

describe('AgentGovernance refresh feedback only', () => {
  it('shows loading, disables repeat refresh and reports the real successful count', async () => {
    const request = deferred<ReturnType<typeof makeAgent>[]>()
    api.listGovernanceAgents.mockReturnValueOnce(request.promise)
    const wrapper = mountMode('agents')
    await settle()
    const refresh = wrapper.get('.page-head button')
    expect(refresh.attributes('disabled')).toBeDefined()
    expect(refresh.attributes('aria-busy')).toBe('true')
    expect(wrapper.get('[role="status"]').text()).toContain('正在加载 Agent 列表')
    expect(wrapper.text()).not.toContain('暂无 Agent 记录')
    await refresh.trigger('click')
    expect(api.listGovernanceAgents).toHaveBeenCalledTimes(1)
    request.resolve([makeAgent()])
    await settle()
    expect(refresh.attributes('disabled')).toBeUndefined()
    expect(wrapper.get('[role="status"]').text()).toContain('已加载 1 个 Agent')
  })

  it('preserves the last rows on refresh failure and recovers on explicit retry', async () => {
    const wrapper = mountMode('agents')
    await settle()
    const request = deferred<ReturnType<typeof makeAgent>[]>()
    api.listGovernanceAgents.mockReturnValueOnce(request.promise)
    const refresh = wrapper.get('.page-head button')
    await refresh.trigger('click')
    expect(refresh.attributes('disabled')).toBeDefined()
    expect(wrapper.get('[role="status"]').text()).toContain('正在刷新 Agent 列表')
    expect(wrapper.get('.agent-card h3').text()).toBe('审查编排')
    await refresh.trigger('click')
    expect(api.listGovernanceAgents).toHaveBeenCalledTimes(2)
    request.reject({ message: '读取超时，请稍后重试' })
    await settle()
    expect(wrapper.get('[role="alert"]').text()).toContain('读取超时，请稍后重试')
    expect(wrapper.get('[role="alert"]').text()).toContain('上次成功')
    expect(wrapper.get('.agent-card h3').text()).toBe('审查编排')
    expect(refresh.attributes('disabled')).toBeUndefined()
    api.listGovernanceAgents.mockResolvedValueOnce([{ ...makeAgent(), code: 'custom_test_agent', name: '更新后的隔离样例' }])
    await refresh.trigger('click')
    await settle()
    expect(wrapper.find('[role="alert"]').exists()).toBe(false)
    expect(wrapper.get('.agent-card h3').text()).toBe('更新后的隔离样例')
    expect(wrapper.get('[role="status"]').text()).toContain('已加载 1 个 Agent')
  })

  it.each([new Error('服务暂不可用'), { message: '' }, null])('distinguishes initial failure from empty data: %s', async (error) => {
    api.listGovernanceAgents.mockRejectedValueOnce(error)
    const wrapper = mountMode('agents')
    await settle()
    expect(wrapper.get('[role="alert"]').text()).toContain(error instanceof Error ? error.message : 'Agent 列表加载失败')
    expect(wrapper.text()).not.toContain('暂无 Agent 记录')
    expect(wrapper.get('.page-head button').attributes('disabled')).toBeUndefined()
  })

  it('shows the empty state only after a successful empty response', async () => {
    api.listGovernanceAgents.mockResolvedValueOnce([])
    const wrapper = mountMode('agents')
    await settle()
    expect(wrapper.text()).toContain('暂无 Agent 记录')
    expect(wrapper.get('[role="status"]').text()).toContain('已加载 0 个 Agent')
    expect(wrapper.find('[role="alert"]').exists()).toBe(false)
  })

  it('shows a busy refresh state while approvals load without Agent-specific copy', async () => {
    const request = deferred<unknown[]>()
    api.listApprovals.mockReturnValueOnce(request.promise)
    const wrapper = mountMode('approvals')
    await settle()
    expect(wrapper.get('.page-head button').text()).toBe('正在加载')
    expect(wrapper.get('.page-head button').attributes('disabled')).toBeDefined()
    expect(wrapper.get('.page-head button').attributes('aria-busy')).toBe('true')
    expect(wrapper.text()).not.toContain('Agent 列表')
    request.resolve([])
    await settle()
    expect(wrapper.get('.page-head button').text()).toBe('刷新')
  })
})


describe('私人治理日志隔离提示', () => {
  it.each(['policies', 'tools'] as const)('%s 中被隔离原文明示原因且不显示旧字段', async (mode) => {
    api.listPolicyDecisions.mockResolvedValue([{ id: 91, subject: 'agent:manager', action: 'knowledge.read', resource: '[按账号隔离]', decision: 'allow', risk_level: 'low', risk_score: 1, reason: '不该出现的私人原文', content_redacted: true }])
    api.listToolCalls.mockResolvedValue([{ id: 92, agent_code: 'manager', tool_code: 'knowledge_read', action: 'knowledge.read', resource: '[按账号隔离]', decision: 'allow', status: 'success', risk_level: 'low', duration_ms: 12, input_summary: '不该出现的私人原文', content_redacted: true }])
    const wrapper = mountMode(mode)
    await settle()
    expect(wrapper.text()).toContain('原文按账号隔离')
    expect(wrapper.text()).not.toContain('不该出现的私人原文')
  })
})
