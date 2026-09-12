import fs from 'node:fs'
import path from 'node:path'
import { NodeTypes, parse as parseTemplate, type RootNode, type TemplateChildNode } from '@vue/compiler-dom'
import { parse as parseSfc } from '@vue/compiler-sfc'
import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import ElementPlus, { ElTable } from 'element-plus'
import zhCn from 'element-plus/es/locale/lang/zh-cn'
import { nextTick } from 'vue'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const api = vi.hoisted(() => ({
  getGovernanceOverview: vi.fn(),
  listGovernanceAgents: vi.fn(),
  listApprovals: vi.fn(),
  listAlerts: vi.fn(),
  listToolCalls: vi.fn(),
  listToolPermissions: vi.fn(),
  listAgentMemory: vi.fn(),
  listAgentKnowledge: vi.fn(),
  listAgentKnowledgeSources: vi.fn(),
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

type TestedMode = 'overview' | 'agents' | 'approvals' | 'tools' | 'knowledge' | 'rewards' | 'rollback'
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
      [0, 0, '贾维斯(全局运维)'], [0, 1, '命令执行'], [0, 2, '升级审批'], [0, 3, '高风险'],
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

  it('renders all 35 agent cards using the existing eight category labels', async () => {
    const categories = [
      ['meta', '主控'], ['frontline', '前台'], ['governance', '治理'], ['operations', '运维'],
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
    expect(wrapper.get('.agent-card h3').text()).toBe('审查编排测试')
    await refresh.trigger('click')
    expect(api.listGovernanceAgents).toHaveBeenCalledTimes(2)
    request.reject({ message: '读取超时，请稍后重试' })
    await settle()
    expect(wrapper.get('[role="alert"]').text()).toContain('读取超时，请稍后重试')
    expect(wrapper.get('[role="alert"]').text()).toContain('上次成功')
    expect(wrapper.get('.agent-card h3').text()).toBe('审查编排测试')
    expect(refresh.attributes('disabled')).toBeUndefined()
    api.listGovernanceAgents.mockResolvedValueOnce([{ ...makeAgent(), name: '更新后的隔离样例' }])
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

  it('does not add Agent-specific refresh behavior to approvals', async () => {
    const request = deferred<unknown[]>()
    api.listApprovals.mockReturnValueOnce(request.promise)
    const wrapper = mountMode('approvals')
    await settle()
    expect(wrapper.get('.page-head button').text()).toBe('刷新')
    expect(wrapper.get('.page-head button').attributes('disabled')).toBeUndefined()
    expect(wrapper.text()).not.toContain('Agent 列表')
    request.resolve([])
    await settle()
  })
})
