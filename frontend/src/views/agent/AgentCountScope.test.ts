import { mount, flushPromises, type VueWrapper } from '@vue/test-utils'
import ElementPlus from 'element-plus'
import { createPinia, setActivePinia } from 'pinia'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { useUserStore } from '@/stores/user'
import type { AgentEvent } from '@/types/agentEvent'

const api = vi.hoisted(() => ({ listRuntimeAgents: vi.fn(), getRuntimeSummary: vi.fn(), getSituation: vi.fn(), listTypeMappings: vi.fn(), listAgentSkills: vi.fn(), getMetaGPTInfo: vi.fn(), previewMetaGPTEnvironment: vi.fn() }))
const stream = vi.hoisted(() => ({ callback: null as null | ((event: AgentEvent) => void), close: vi.fn() }))
vi.mock('@/api/agent', () => api)
vi.mock('vue-router', async original => ({ ...await original<typeof import('vue-router')>(), useRouter: () => ({ push: vi.fn() }), useRoute: () => ({ query: {}, path: '/agents' }) }))
vi.mock('@/utils/agentEventStream', () => ({ subscribeAgentEvents: (callback: (event: AgentEvent) => void) => { stream.callback = callback; return { close: stream.close } } }))
vi.mock('@/utils/tilt', () => ({ useTilt: vi.fn() }))
vi.mock('element-plus/es/components/message/index', () => ({ ElMessage: { error: vi.fn(), success: vi.fn() } }))
import AgentCenter from './AgentCenter.vue'
import SituationPanel from '@/components/agent/SituationPanel.vue'

let wrapper: VueWrapper
let pinia: ReturnType<typeof createPinia>
const runtime = Array.from({ length: 5 }, (_, i) => ({ code: `qa_${i}`, name: `合成代理${i}`, category: 'reviewer', skills: [], status: 'idle', call_count: 1, success_count: 1, failed_count: 0, description: '测试数据' }))
beforeEach(() => {
  pinia = createPinia(); setActivePinia(pinia)
  useUserStore().profile = { id: 104, username: 'qa_member', role: 'user', status: 1 }
  useUserStore().permissions = new Set(['agent:view'])
  api.listRuntimeAgents.mockResolvedValue(runtime.map(row => ({ ...row })))
  api.getRuntimeSummary.mockResolvedValue({ total: 19, by_category: [{ category: 'reviewer', count: 19 }] })
  api.getSituation.mockResolvedValue({ online: 19, working: 0, idle: 19, today_calls: 6, spectrum: [], hotspots: [] })
  api.listTypeMappings.mockResolvedValue([])
  api.listAgentSkills.mockResolvedValue([])
  api.getMetaGPTInfo.mockResolvedValue({ version: 'v2.4', description: '编排', components: {}, factories: {}, adaptable_agents: Array.from({ length: 17 }, (_, i) => ({ name: `builtin_${i}`, description: '' })), default_review_agents: [], default_discussion_agents: [], catalog_scope: 'builtin_registry' })
  api.previewMetaGPTEnvironment.mockResolvedValue({ roles: [], registered_agent_count: 17, catalog_scope: 'builtin_registry' })
})
afterEach(() => { wrapper?.unmount(); document.body.innerHTML = '' })
async function render() { wrapper = mount(AgentCenter, { global: { plugins: [ElementPlus, pinia] } }); await flushPromises() }

it('列表5、全局快照19、内置编排17分别明确范围，列表/分类/态势同为5', async () => {
  await render()
  expect(wrapper.get('.page-sub').text()).toContain('本账号运行过的可见 Agent')
  expect(wrapper.get('.page-sub').text()).not.toContain('在岗')
  expect(wrapper.get('.bucket-num').text()).toBe('5')
  const metrics = wrapper.findComponent(SituationPanel).findAll('.metric-num')
  expect(metrics[0].text()).toBe('5')
  expect(wrapper.findComponent(SituationPanel).text()).toContain('可见目录')
  expect(wrapper.text()).toContain('内置可适配 Agent')
  expect(wrapper.text()).toContain('17')
})

it('零历史账号保持0，不把全平台目录数当作可见或在岗', async () => {
  api.listRuntimeAgents.mockResolvedValue([])
  await render()
  expect(wrapper.findComponent(SituationPanel).findAll('.metric-num')[0].text()).toBe('0')
  expect(wrapper.find('.bucket-num').exists()).toBe(false)
})

it('普通账号不把他人或无归属系统事件标为本人执行，自己事件可更新', async () => {
  await render()
  const event = { type: 'progress', agent: 'qa_0', trace_id: 'scope', timestamp: new Date().toISOString(), payload: {} } as AgentEvent
  stream.callback?.({ ...event, user_id: 105 }); await flushPromises()
  stream.callback?.(event); await flushPromises()
  expect(wrapper.findComponent(SituationPanel).findAll('.metric-num')[1].text()).toBe('0')
  stream.callback?.({ ...event, user_id: 104 }); await flushPromises()
  expect(wrapper.findComponent(SituationPanel).findAll('.metric-num')[1].text()).toBe('1')
})

it('超级管理员明确全平台目录，不以普通成员口径解释', async () => {
  useUserStore().profile = { id: 1, username: 'qa_super', role: 'super_admin', status: 1 }
  await render()
  expect(wrapper.get('.page-sub').text()).toContain('全平台内置及已发布 Agent')
})


it('超过90秒或无法解析的本人事件不制造近期执行数', async () => {
  await render()
  const event = { type: 'progress', agent: 'qa_0', trace_id: 'old', user_id: 104, payload: {} } as AgentEvent
  stream.callback?.({ ...event, timestamp: new Date(Date.now() - 120_000).toISOString() })
  stream.callback?.({ ...event, timestamp: 'not-a-time' }); await flushPromises()
  expect(wrapper.findComponent(SituationPanel).findAll('.metric-num')[1].text()).toBe('0')
})
