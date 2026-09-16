import { flushPromises, mount } from '@vue/test-utils'
import ElementPlus from 'element-plus'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
const api = vi.hoisted(() => ({ listGovernanceAgents: vi.fn() }))
vi.mock('@/api/adminGovernance', () => api)
vi.mock('@/stores/user', () => ({ useUserStore: () => ({ isSuperAdmin: () => true }) }))
vi.mock('vue-router', () => ({ useRouter: () => ({ push: vi.fn() }) }))
import AgentGovernance from './AgentGovernance.vue'
const wrappers: ReturnType<typeof mount>[] = []
const sample = { code: 'review_orchestrator', name: '审查编排', description: '协调项目审查并汇总发现', category: 'orchestrator', status: 'idle', is_enabled: 1, priority: 52, auto_approval_threshold: 0.74, memory_count: 12, knowledge_count: 8, skills: ['fixture.internal.skill.'.repeat(12)] }
function render() { const w = mount(AgentGovernance, { global: { plugins: [ElementPlus] } }); wrappers.push(w); return w }
beforeEach(() => { api.listGovernanceAgents.mockReset().mockResolvedValue([sample]) })
afterEach(() => { wrappers.splice(0).forEach(w => w.unmount()) })
it('默认卡片突出职责和四项指标，内部编码与长能力仅点击展开后出现', async () => {
  const wrapper = render(); await flushPromises()
  const card = wrapper.get('.agent-card')
  expect(wrapper.find('table').exists()).toBe(false)
  expect(card.text()).toContain('协调项目审查并汇总发现')
  expect(card.get('.agent-category').text()).toBe('任务编排')
  expect(card.get('.agent-card-metrics').text()).toContain('优先级52')
  expect(card.get('.agent-card-metrics').text()).toContain('审批阈值0.74')
  expect(card.get('.agent-card-metrics').text()).toContain('记忆12')
  expect(card.get('.agent-card-metrics').text()).toContain('知识8')
  expect(card.text()).not.toContain(sample.skills[0])
  expect(card.find('code').exists()).toBe(false)
  const toggle = card.get('button[aria-expanded]')
  expect(toggle.attributes('aria-expanded')).toBe('false')
  await toggle.trigger('click')
  expect(toggle.attributes('aria-expanded')).toBe('true')
  expect(card.text()).toContain(sample.skills[0])
  expect(card.get('code').text()).toBe(sample.code)
  await toggle.trigger('click')
  expect(card.find('.agent-card-details').exists()).toBe(false)
  expect(api.listGovernanceAgents).toHaveBeenCalledTimes(1)
})
it('35个真实分类目录项全部使用中文卡片，缺失数值仍显示破折号', async () => {
  const categories = [['analytics', '数据分析'], ['analyzer', '分析检测'], ['custom_review', '自定义审查'], ['manager', '协调管理'], ['orchestrator', '任务编排'], ['output', '报告输出'], ['reviewer', '代码审查']]
  api.listGovernanceAgents.mockResolvedValue(Array.from({ length: 35 }, (_, i) => ({ ...sample, code: `fixture-${i}`, category: categories[i % categories.length]![0], memory_count: undefined })))
  const wrapper = render(); await flushPromises()
  const cards = wrapper.findAll('.agent-card')
  expect(cards).toHaveLength(35)
  cards.forEach((card, i) => { expect(card.get('.agent-category').text()).toBe(categories[i % categories.length]![1]); expect(card.text()).toContain('记忆—') })
})
it('首次加载有状态，失败常驻反馈，就地重试成功恢复卡片', async () => {
  let reject!: (error: Error) => void
  api.listGovernanceAgents.mockReturnValueOnce(new Promise((_, fail) => { reject = fail }))
  const wrapper = render(); await flushPromises()
  expect(wrapper.get('[role="status"]').text()).toContain('正在加载')
  expect(wrapper.find('.agent-card').exists()).toBe(false)
  reject(new Error('网络暂不可用')); await flushPromises()
  const alert = wrapper.get('[role="alert"]')
  expect(alert.text()).toContain('网络暂不可用')
  expect(wrapper.text()).not.toContain('暂无 Agent 记录')
  await alert.get('button').trigger('click'); await flushPromises()
  expect(wrapper.find('[role="alert"]').exists()).toBe(false)
  expect(wrapper.findAll('.agent-card')).toHaveLength(1)
  expect(api.listGovernanceAgents).toHaveBeenCalledTimes(2)
})
