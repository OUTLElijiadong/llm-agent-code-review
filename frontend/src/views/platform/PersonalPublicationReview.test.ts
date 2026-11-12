import { flushPromises, mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'

const api = vi.hoisted(() => ({ list: vi.fn(), decide: vi.fn(), revoke: vi.fn() }))
vi.mock('@/api/personalPublications', () => ({ listPersonalPublicationReviews: api.list,
  decidePersonalPublication: api.decide, revokePersonalPublication: api.revoke }))
vi.mock('@/utils/token', () => ({ getToken: () => 'admin-token' }))
import Review from './PersonalPublicationReview.vue'
import { useUserStore } from '@/stores/user'
const row = { id: 4, agent_id: 2, version_number: 3, title: '审核助手', status: 'pending',
  owner_id: 8, prompt: '固定快照', content_hash: 'a'.repeat(64), decision_reason: '', revocation_reason: '' }

beforeEach(() => {
  setActivePinia(createPinia())
  const user = useUserStore(); user.token = 'admin-token'
  user.profile = { id: 1, username: 'admin', role: 'admin', status: 1 }
  Object.values(api).forEach(fn => fn.mockReset()); api.list.mockResolvedValue([row])
})

describe('个人 Agent 发布审核', () => {
  it('查看固定快照并提交审批决定', async () => {
    api.decide.mockResolvedValue({})
    const wrapper = mount(Review); await flushPromises()
    expect(wrapper.text()).toContain('固定快照')
    await wrapper.get('#personal-review-reason').setValue('内容符合要求')
    await wrapper.get('[data-approve="4"]').trigger('click'); await flushPromises()
    expect(api.decide).toHaveBeenCalledWith(4, { approve: true, reason: '内容符合要求' }, 'admin-token')
    wrapper.unmount()
  })
  it('已批准记录可下架，附带说明', async () => {
    api.list.mockImplementation(async status => status === 'approved' ? [{ ...row, status: 'approved' }] : [])
    api.revoke.mockResolvedValue({})
    vi.spyOn(window, 'confirm').mockReturnValue(true)
    const wrapper = mount(Review); await flushPromises()
    await wrapper.get('[data-status="approved"]').trigger('click'); await flushPromises()
    await wrapper.get('#personal-review-reason').setValue('维护调整')
    await wrapper.get('[data-revoke="4"]').trigger('click'); await flushPromises()
    expect(api.revoke).toHaveBeenCalledWith(4, '维护调整', 'admin-token')
    wrapper.unmount()
  })
})
