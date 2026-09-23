import { flushPromises, mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { beforeEach, expect, it, vi } from 'vitest'
const api = vi.hoisted(() => ({ list: vi.fn() }))
vi.mock('@/api/audit', () => ({ listAuditLogs: api.list }))
vi.mock('vue-router', () => ({ useRouter: () => ({ push: vi.fn() }) }))
vi.mock('@/router', () => ({ default: { push: vi.fn() } }))
import SystemAudit from './SystemAudit.vue'
import { useUserStore } from '@/stores/user'
const entry = { id: 1, actor_id: 101, actor_name: '账号 A', action: 'agent_team.create', target_type: 'agent_team', target_id: '1', status: 'success', create_time: '2026-09-20T12:50:00' }
beforeEach(() => { setActivePinia(createPinia()); api.list.mockReset() })
function render() {
  return mount(SystemAudit, { global: { directives: { loading: () => undefined }, stubs: {
    'el-card': { template: '<div><slot /></div>' }, 'el-select': true, 'el-option': true, 'el-input': true,
    'el-date-picker': true, 'el-tag': { template: '<span><slot /></span>' }, 'el-button': true,
    'el-icon': true, 'el-pagination': true,
  } } })
}
it('私人操作原文明确隔离且不会通过 title 属性旁路暴露', async () => {
  api.list.mockResolvedValue({ items: [{ ...entry, content_redacted: true, detail: '不应出现的旧字段' }], total: 1 })
  const wrapper = render(); await flushPromises()
  await wrapper.get('.audit-card').trigger('click')
  expect(wrapper.text()).toContain('原文按账号隔离')
  expect(wrapper.html()).not.toContain('不应出现的旧字段')
  expect(wrapper.text()).toContain('账号 A')
  wrapper.unmount()
})
it('旧账号未完成的审计列表不能回写到新账号', async () => {
  const user = useUserStore(); user.profile = { id: 101, username: 'a', role: 'admin', status: 1 }
  let resolve!: (value: unknown) => void
  api.list.mockReturnValueOnce(new Promise(done => { resolve = done }))
  const wrapper = render()
  api.list.mockResolvedValue({ items: [], total: 0 })
  user.profile = { id: 202, username: 'b', role: 'admin', status: 1 }; await flushPromises()
  resolve({ items: [{ ...entry, detail: '账号 A 的私密审计内容' }], total: 1 }); await flushPromises()
  expect(wrapper.find('.audit-card').exists()).toBe(false)
  wrapper.unmount()
})
