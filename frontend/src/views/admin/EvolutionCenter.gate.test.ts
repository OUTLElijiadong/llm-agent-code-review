import { flushPromises, mount } from '@vue/test-utils'
import ElementPlus from 'element-plus'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import EvolutionCenter from './EvolutionCenter.vue'

const api = vi.hoisted(() => ({
  list: vi.fn(),
  approve: vi.fn(),
  evaluate: vi.fn(),
  confirm: vi.fn(),
}))

vi.mock('@/api/evolution', () => ({
  listProposals: api.list,
  approveProposal: api.approve,
  evaluateProposal: api.evaluate,
  getFeedback: vi.fn().mockResolvedValue({}),
  listExperiences: vi.fn().mockResolvedValue([]),
  listEvalCases: vi.fn().mockResolvedValue([]),
  rejectProposal: vi.fn(),
  rollbackProposal: vi.fn(),
  runEvolution: vi.fn(),
  triggerEvolution: vi.fn(),
}))
vi.mock('@/api/agent', () => ({
  listRuntimeAgents: vi.fn().mockResolvedValue([]),
  listSkillRecords: vi.fn().mockResolvedValue([]),
}))
vi.mock('@/composables/useDangerConfirm', () => ({ confirmDanger: api.confirm }))
vi.mock('element-plus/es/components/message/index', () => ({
  ElMessage: { success: vi.fn(), warning: vi.fn() },
}))

const wrappers: ReturnType<typeof mount>[] = []
const sample = {
  id: 12,
  title: '历史缺证据',
  proposal_type: 'disable_rule',
  status: 'eval_passed',
  eval_score: null,
}

function render() {
  const wrapper = mount(EvolutionCenter, { global: { plugins: [ElementPlus] } })
  wrappers.push(wrapper)
  return wrapper
}

beforeEach(() => {
  vi.clearAllMocks()
  api.list.mockResolvedValue([sample])
  api.confirm.mockResolvedValue(false)
})
afterEach(() => {
  wrappers.splice(0).forEach(wrapper => wrapper.unmount())
})

it.each([null, {}, [], { passed: false }, { passed: 'true' }, { passed: 1 }])(
  '缺失或非法评估证据禁用审批且保留重新评估入口 %j',
  async score => {
    api.list.mockResolvedValue([{ ...sample, eval_score: score }])
    const wrapper = render()
    await flushPromises()

    expect(wrapper.text()).toContain('评估证据缺失，请重新评估')
    const approve = wrapper.findAll('button').find(button => button.text() === '审批生效')!
    expect(approve.attributes('disabled')).toBeDefined()
    await approve.trigger('click')
    expect(api.confirm).not.toHaveBeenCalled()
    expect(api.approve).not.toHaveBeenCalled()
    expect(wrapper.findAll('button').some(button => (
      button.text() === '评估' && button.attributes('disabled') === undefined
    ))).toBe(true)
  },
)

it('重新评估取得通过证据后恢复审批，取消确认不调用写接口', async () => {
  const wrapper = render()
  await flushPromises()
  const passed = { ...sample, eval_score: { passed: true, recall_before: 1, recall_after: 1 } }
  api.evaluate.mockResolvedValue(passed)
  api.list.mockResolvedValue([passed])

  await wrapper.findAll('button').find(button => button.text() === '评估')!.trigger('click')
  await flushPromises()
  expect(api.evaluate).toHaveBeenCalledWith(sample.id)
  expect(wrapper.text()).not.toContain('评估证据缺失，请重新评估')

  const approve = wrapper.findAll('button').find(button => button.text() === '审批生效')!
  expect(approve.attributes('disabled')).toBeUndefined()
  await approve.trigger('click')
  await flushPromises()
  expect(api.confirm).toHaveBeenCalledTimes(1)
  expect(api.approve).not.toHaveBeenCalled()
})

it('真实打开详情显示当前待重新评估，原记录状态单列且不改历史数据', async () => {
  const wrapper = render()
  await flushPromises()
  await wrapper.findAll('button').find(button => button.text() === '详情')!.trigger('click')
  await flushPromises()

  const detail = wrapper.get('.detail')
  expect(detail.text()).toContain('评估证据缺失，请重新评估')
  expect(detail.get('.el-tag').text()).toBe('待重新评估')
  expect(detail.text()).toContain('原记录状态')
  expect(detail.text()).toContain('闸门通过（历史记录，当前评估证据不足）')
  expect(detail.text()).not.toContain('（尚未评估）')
  expect(sample.status).toBe('eval_passed')
  expect(sample.eval_score).toBeNull()
  expect(api.evaluate).not.toHaveBeenCalled()
  expect(api.approve).not.toHaveBeenCalled()
})
