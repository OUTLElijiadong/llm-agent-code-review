import { flushPromises, mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'

const mock = vi.hoisted(() => ({ list: vi.fn(), decide: vi.fn(), revoke: vi.fn() }))
const auth = vi.hoisted(() => ({ token: 'platform-session' }))
vi.mock('@/api/enterpriseAssets', () => ({ listPlatformEnterpriseAssetPublications: mock.list,
  decidePlatformEnterpriseAssetPublication: mock.decide,
  revokePlatformEnterpriseAssetPublication: mock.revoke }))
vi.mock('@/utils/token', () => ({ getToken: () => auth.token }))

import EnterpriseAssetPublicationReview from './EnterpriseAssetPublicationReview.vue'
import { useUserStore } from '@/stores/user'

const pending = { id: 31, enterprise_id: 12, draft_id: 8, version_id: 19, version_number: 3, requested_by: 22,
  kind: 'agent', scope: 'global', status: 'pending', title: '自动归纳 Agent', definition: { policy: 'read-only' },
  content_sha256: 'd'.repeat(64), decided_by: null, decided_at: null, decision_reason: '',
  revoked_by: null, revoked_at: null, revocation_reason: '' }

beforeEach(() => {
  setActivePinia(createPinia())
  const user = useUserStore(); user.token = 'platform-session'
  user.profile = { id: 1, username: 'admin', role: 'admin', status: 1 }
  auth.token = 'platform-session'
  Object.values(mock).forEach(fn => fn.mockReset())
  mock.list.mockResolvedValue([pending]); mock.decide.mockResolvedValue({ ...pending, status: 'approved' })
  mock.revoke.mockResolvedValue({ ...pending, status: 'revoked' })
})

describe('平台全局资产审批', () => {
  it('展示固定申请快照并将批准记录写回服务端', async () => {
    const wrapper = mount(EnterpriseAssetPublicationReview)
    await flushPromises()
    expect(mock.list).toHaveBeenCalledWith('pending', 'platform-session')
    expect(wrapper.text()).toContain('自动归纳 Agent')
    expect(wrapper.text()).toContain('SHA-256')
    await wrapper.get('#global-review-reason').setValue('检查了配置边界')
    await wrapper.findAll('button').find(button => button.text() === '批准进入全局目录')!.trigger('click')
    await flushPromises()
    expect(mock.decide).toHaveBeenCalledWith(31, { decision: 'approve', reason: '检查了配置边界' }, 'platform-session')
    expect(wrapper.text()).toContain('已批准该固定版本进入全局目录。')
    expect(wrapper.text()).toContain('Agent/Skill 执行状态：关闭')
    wrapper.unmount()
  })

  it('切换状态筛选并仅对已发布条目提供撤销操作', async () => {
    mock.list.mockResolvedValueOnce([{ ...pending, status: 'approved' }]).mockResolvedValueOnce([])
    const prompt = vi.spyOn(window, 'prompt').mockReturnValue('全局策略变更')
    const wrapper = mount(EnterpriseAssetPublicationReview)
    await flushPromises()
    expect(wrapper.text()).toContain('已发布')
    expect(wrapper.findAll('button').some(button => button.text() === '撤销全局发布')).toBe(true)
    expect(wrapper.findAll('button').some(button => button.text() === '批准进入全局目录')).toBe(false)
    await wrapper.findAll('button').find(button => button.text() === '撤销全局发布')!.trigger('click')
    await flushPromises()
    expect(mock.revoke).toHaveBeenCalledWith(31, { reason: '全局策略变更' }, 'platform-session')
    expect(wrapper.text()).toContain('全局发布已撤销')
    wrapper.unmount()
    prompt.mockRestore()
  })
})
