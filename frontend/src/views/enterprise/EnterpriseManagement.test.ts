import { flushPromises, mount } from '@vue/test-utils'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const api = vi.hoisted(() => ({
  listEnterpriseGroups: vi.fn(), listEnterpriseMembers: vi.fn(), listEnterpriseDirectory: vi.fn(),
  listEnterpriseGroupMembers: vi.fn(), createEnterpriseGroup: vi.fn(), createEnterpriseInvitation: vi.fn(),
  listEnterpriseInvitations: vi.fn(), revokeEnterpriseInvitation: vi.fn(), setEnterpriseMemberRole: vi.fn(),
  removeEnterpriseMember: vi.fn(), setEnterpriseGroupMember: vi.fn(), removeEnterpriseGroupMember: vi.fn(),
  setEnterpriseMemberDefaultGroup: vi.fn(),
}))
vi.mock('@/api/enterprise', () => api)
import EnterpriseManagement from './EnterpriseManagement.vue'

beforeEach(() => {
  sessionStorage.clear()
  localStorage.removeItem('review_token')
  Object.values(api).forEach(fn => fn.mockReset())
  api.listEnterpriseGroups.mockResolvedValue([{ id: 11, enterprise_id: 10, name: '研发组', status: 'active', my_role: 'leader' }])
  api.listEnterpriseMembers.mockResolvedValue([
    { user_id: 1, role: 'owner', status: 'active', default_group_id: null },
    { user_id: 2, role: 'member', status: 'active', default_group_id: null },
  ])
  api.listEnterpriseDirectory.mockResolvedValue([{ user_id: 1, display_name: '甲' }, { user_id: 2, display_name: '乙' }])
  api.listEnterpriseGroupMembers.mockResolvedValue([{ user_id: 1, role: 'leader' }, { user_id: 2, role: 'member' }])
  api.listEnterpriseInvitations.mockResolvedValue([])
})
afterEach(() => vi.restoreAllMocks())
const props = { enterpriseId: 10, currentUserId: 1, role: 'owner' as const }

describe('企业组织管理', () => {
  it('组织列表与小组成员读取绑定发起时认证快照', async () => {
    localStorage.setItem('review_token', 'session-a')
    const wrapper = mount(EnterpriseManagement, { props }); await flushPromises()
    expect(api.listEnterpriseGroups).toHaveBeenCalledWith(10, 'session-a')
    expect(api.listEnterpriseDirectory).toHaveBeenCalledWith(10, 0, 'session-a')
    expect(api.listEnterpriseMembers).toHaveBeenCalledWith(10, 'session-a')
    expect(api.listEnterpriseInvitations).toHaveBeenCalledWith(10, 'session-a')
    await wrapper.get('[data-group="11"]').trigger('click'); await flushPromises()
    expect(api.listEnterpriseGroupMembers).toHaveBeenCalledWith(10, 11, 'session-a')
    wrapper.unmount()
  })

  it('焦点恢复时凭据已变化则清除仍在等待的旧管理数据', async () => {
    let resolve!: (value: unknown) => void
    api.listEnterpriseGroupMembers.mockReturnValue(new Promise(r => { resolve = r }))
    localStorage.setItem('review_token', 'session-a')
    const wrapper = mount(EnterpriseManagement, { props }); await flushPromises()
    expect(wrapper.text()).toContain('研发组')
    await wrapper.get('[data-group="11"]').trigger('click')
    localStorage.setItem('review_token', 'session-b')
    window.dispatchEvent(new Event('focus'))
    await flushPromises()
    expect(wrapper.text()).not.toContain('研发组')
    resolve([{ user_id: 2, role: 'member' }]); await flushPromises()
    expect(wrapper.text()).not.toContain('研发组')
    wrapper.unmount()
  })

  it('所有者可创建邀请及小组、调整企业角色和小组角色', async () => {
    api.createEnterpriseInvitation.mockResolvedValue({ token: 'one-time-token', expires_in_hours: 24 })
    api.createEnterpriseGroup.mockResolvedValue({ id: 12, name: '测试组' })
    api.setEnterpriseMemberRole.mockResolvedValue({})
    api.setEnterpriseGroupMember.mockResolvedValue({})
    const wrapper = mount(EnterpriseManagement, { props }); await flushPromises()
    await wrapper.get('[data-action="invite"]').trigger('click'); await flushPromises()
    expect((wrapper.get('#invite-result').element as HTMLInputElement).value).toBe('one-time-token')
    await wrapper.get('#new-group-name').setValue('测试组')
    await wrapper.get('[data-action="create-group"]').trigger('submit'); await flushPromises()
    expect(api.createEnterpriseGroup).toHaveBeenCalledWith(10, '测试组')
    expect(wrapper.emitted('changed')?.length).toBeGreaterThanOrEqual(2)
    await wrapper.get('[data-member-role="2"]').setValue('admin'); await flushPromises()
    expect(api.setEnterpriseMemberRole).toHaveBeenCalledWith(10, 2, 'admin')
    await wrapper.get('[data-group="11"]').trigger('click'); await flushPromises()
    await wrapper.get('[data-group-role="2"]').setValue('leader'); await flushPromises()
    expect(api.setEnterpriseGroupMember).toHaveBeenCalledWith(10, 11, 2, 'leader')
    wrapper.unmount()
  })

  it('普通组长只看到自己负责小组的管理入口，无企业权限', async () => {
    api.listEnterpriseGroups.mockResolvedValue([
      { id: 11, enterprise_id: 10, name: '研发组', status: 'active', my_role: 'leader' },
      { id: 12, enterprise_id: 10, name: '测试组', status: 'active', my_role: 'member' },
    ])
    const wrapper = mount(EnterpriseManagement, { props: { ...props, role: 'member' } }); await flushPromises()
    expect(api.listEnterpriseMembers).not.toHaveBeenCalled()
    expect(wrapper.find('[data-action="invite"]').exists()).toBe(false)
    expect(wrapper.find('[data-action="create-group"]').exists()).toBe(false)
    expect(wrapper.find('[data-group="11"]').exists()).toBe(true)
    expect(wrapper.find('[data-group="12"]').exists()).toBe(false)
    await wrapper.get('[data-group="11"]').trigger('click'); await flushPromises()
    expect(api.listEnterpriseGroupMembers).toHaveBeenCalledWith(10, 11, null)
    expect(wrapper.find('[data-default-group-set="2"]').exists()).toBe(false)
    expect(wrapper.find('[data-default-group-clear="2"]').exists()).toBe(false)
    wrapper.unmount()
  })

  it.each(['owner', 'admin'] as const)('%s 可以从所属小组设置默认组并从成员列表清除', async (role) => {
    let defaultGroup: number | null = null
    api.listEnterpriseMembers.mockImplementation(async () => [
      { user_id: 1, role: 'owner', status: 'active', default_group_id: null },
      { user_id: 2, role: 'member', status: 'active', default_group_id: defaultGroup },
    ])
    api.setEnterpriseMemberDefaultGroup.mockImplementation(async (_enterprise, _user, group) => {
      defaultGroup = group
      return { user_id: 2, role: 'member', status: 'active', default_group_id: group }
    })
    localStorage.setItem('review_token', 'session-a')
    const wrapper = mount(EnterpriseManagement, { props: { ...props, role } }); await flushPromises()
    expect(wrapper.text()).toContain('未设置默认小组')
    await wrapper.get('[data-group="11"]').trigger('click'); await flushPromises()
    await wrapper.get('[data-default-group-set="2"]').trigger('click'); await flushPromises()
    expect(api.setEnterpriseMemberDefaultGroup).toHaveBeenCalledWith(10, 2, 11, 'session-a')
    expect(wrapper.text()).toContain('默认小组：研发组')
    await wrapper.get('[data-default-group-clear="2"]').trigger('click'); await flushPromises()
    expect(api.setEnterpriseMemberDefaultGroup).toHaveBeenLastCalledWith(10, 2, null, 'session-a')
    expect(wrapper.text()).toContain('未设置默认小组')
    wrapper.unmount()
  })

  it('默认小组写入未完成时锁定操作；切换小组不会改变原请求', async () => {
    let resolve!: (value: unknown) => void
    api.listEnterpriseGroups.mockResolvedValue([
      { id: 11, enterprise_id: 10, name: '研发组', status: 'active' },
      { id: 12, enterprise_id: 10, name: '测试组', status: 'active' },
    ])
    api.setEnterpriseMemberDefaultGroup.mockReturnValue(new Promise(r => { resolve = r }))
    localStorage.setItem('review_token', 'session-a')
    const wrapper = mount(EnterpriseManagement, { props }); await flushPromises()
    await wrapper.get('[data-group="11"]').trigger('click'); await flushPromises()
    await wrapper.get('[data-default-group-set="2"]').trigger('click')
    expect((wrapper.get('[data-default-group-set="2"]').element as HTMLButtonElement).disabled).toBe(true)
    await wrapper.get('[data-group="12"]').trigger('click'); await flushPromises()
    expect(api.setEnterpriseMemberDefaultGroup).toHaveBeenCalledWith(10, 2, 11, expect.anything())
    resolve({ user_id: 2, role: 'member', status: 'active', default_group_id: 11 }); await flushPromises()
    expect(wrapper.get('[data-group="12"]').attributes('aria-pressed')).toBe('true')
    wrapper.unmount()
  })

  it('默认小组请求结果未知时阻止重试，刷新后可核对', async () => {
    api.setEnterpriseMemberDefaultGroup.mockRejectedValue(new Error('网络中断'))
    const wrapper = mount(EnterpriseManagement, { props }); await flushPromises()
    await wrapper.get('[data-group="11"]').trigger('click'); await flushPromises()
    await wrapper.get('[data-default-group-set="2"]').trigger('click'); await flushPromises()
    expect(wrapper.text()).toContain('结果未知')
    expect((wrapper.get('[data-default-group-set="2"]').element as HTMLButtonElement).disabled).toBe(true)
    expect(sessionStorage.getItem('prism:enterprise-management-uncertain:1:10')).not.toBeNull()
    wrapper.unmount()
  })

  it('默认小组请求被撤权时重新读取，不显示未确认结果', async () => {
    api.setEnterpriseMemberDefaultGroup.mockRejectedValue({ code: 40300 })
    const wrapper = mount(EnterpriseManagement, { props }); await flushPromises()
    await wrapper.get('[data-group="11"]').trigger('click'); await flushPromises()
    api.listEnterpriseMembers.mockRejectedValue({ code: 40300 })
    await wrapper.get('[data-default-group-set="2"]').trigger('click'); await flushPromises()
    expect(wrapper.text()).not.toContain('研发组')
    expect(wrapper.emitted('access-lost')).toHaveLength(1)
    wrapper.unmount()
  })

  it('切换企业和账号时丢弃旧默认组操作结果', async () => {
    let resolve!: (value: unknown) => void
    api.setEnterpriseMemberDefaultGroup.mockReturnValue(new Promise(r => { resolve = r }))
    const wrapper = mount(EnterpriseManagement, { props }); await flushPromises()
    await wrapper.get('[data-group="11"]').trigger('click'); await flushPromises()
    await wrapper.get('[data-default-group-set="2"]').trigger('click')
    api.listEnterpriseGroups.mockResolvedValue([])
    api.listEnterpriseMembers.mockResolvedValue([])
    await wrapper.setProps({ enterpriseId: 20, currentUserId: 3 }); await flushPromises()
    resolve({ user_id: 2, role: 'member', status: 'active', default_group_id: 11 }); await flushPromises()
    expect(wrapper.text()).not.toContain('研发组')
    expect(wrapper.emitted('changed')).toBeUndefined()
    wrapper.unmount()
  })

  it('登录凭据变化时立即清空管理内容并丢弃旧请求', async () => {
    let resolve!: (value: unknown) => void
    api.setEnterpriseMemberDefaultGroup.mockReturnValue(new Promise(r => { resolve = r }))
    localStorage.setItem('review_token', 'session-a')
    const wrapper = mount(EnterpriseManagement, { props }); await flushPromises()
    await wrapper.get('[data-group="11"]').trigger('click'); await flushPromises()
    await wrapper.get('[data-default-group-set="2"]').trigger('click')
    localStorage.setItem('review_token', 'session-b')
    window.dispatchEvent(new StorageEvent('storage', { key: 'review_token', oldValue: 'session-a', newValue: 'session-b' }))
    await flushPromises()
    expect(wrapper.text()).not.toContain('研发组')
    resolve({ user_id: 2, role: 'member', status: 'active', default_group_id: 11 }); await flushPromises()
    expect(wrapper.emitted('changed')).toBeUndefined()
    expect(wrapper.text()).not.toContain('研发组')
    wrapper.unmount()
  })

  it('切换企业时丢弃旧请求及一次性邀请码', async () => {
    let resolve!: (value: unknown) => void
    api.listEnterpriseGroups.mockReturnValueOnce(new Promise(r => { resolve = r })).mockResolvedValueOnce([])
    const wrapper = mount(EnterpriseManagement, { props }); await flushPromises()
    await wrapper.setProps({ enterpriseId: 20 }); await flushPromises()
    resolve([{ id: 11, enterprise_id: 10, name: '旧企业小组', my_role: 'leader' }]); await flushPromises()
    expect(wrapper.text()).not.toContain('旧企业小组')
    wrapper.unmount()
  })

  it('切换账号后丢弃旧账号迟到的邀请码', async () => {
    let resolve!: (value: { token: string; expires_in_hours: number }) => void
    api.createEnterpriseInvitation.mockReturnValue(new Promise(r => { resolve = r }))
    const wrapper = mount(EnterpriseManagement, { props }); await flushPromises()
    await wrapper.get('[data-action="invite"]').trigger('click'); await flushPromises()
    await wrapper.setProps({ currentUserId: 2 }); await flushPromises()
    resolve({ token: 'old-secret', expires_in_hours: 24 }); await flushPromises()
    expect(wrapper.find('#invite-result').exists()).toBe(false)
    wrapper.unmount()
  })

  it('加载成员时撤权也清除内容并通知上层', async () => {
    api.listEnterpriseMembers.mockRejectedValue({ code: 40300 })
    const wrapper = mount(EnterpriseManagement, { props }); await flushPromises()
    expect(wrapper.text()).not.toContain('研发组')
    expect(wrapper.text()).not.toContain('乙')
    expect(wrapper.emitted('access-lost')).toHaveLength(1)
    wrapper.unmount()
  })

  it('写入结果未知时保留锁，须核对后才能继续，刷新页面亦保留锁', async () => {
    api.createEnterpriseInvitation.mockRejectedValue(new Error('网络中断'))
    const wrapper = mount(EnterpriseManagement, { props }); await flushPromises()
    await wrapper.get('[data-action="invite"]').trigger('click'); await flushPromises()
    expect(wrapper.text()).toContain('结果未知')
    expect((wrapper.get('[data-action="invite"]').element as HTMLButtonElement).disabled).toBe(true)
    wrapper.unmount()
    const remount = mount(EnterpriseManagement, { props }); await flushPromises()
    expect(remount.text()).toContain('结果未知')
    expect((remount.get('[data-action="invite"]').element as HTMLButtonElement).disabled).toBe(true)
    remount.unmount()
  })

  it('撤权后清除受保护内容并通知上层', async () => {
    api.createEnterpriseInvitation.mockRejectedValue({ code: 40300 })
    const wrapper = mount(EnterpriseManagement, { props }); await flushPromises()
    expect(wrapper.text()).toContain('研发组')
    api.listEnterpriseMembers.mockRejectedValue({ code: 40300 })
    await wrapper.get('[data-action="invite"]').trigger('click'); await flushPromises()
    expect(wrapper.text()).not.toContain('研发组')
    expect(wrapper.emitted('access-lost')).toHaveLength(1)
    wrapper.unmount()
  })

  it('单次操作无权时重新读取权限，成员仍有权限不会误判企业撤权', async () => {
    api.setEnterpriseGroupMember.mockRejectedValue({ code: 40300 })
    const wrapper = mount(EnterpriseManagement, { props }); await flushPromises()
    await wrapper.get('[data-group="11"]').trigger('click'); await flushPromises()
    await wrapper.get('[data-group-role="2"]').setValue('leader'); await flushPromises()
    expect(wrapper.emitted('access-lost')).toBeUndefined()
    expect(wrapper.text()).toContain('操作被拒绝')
    expect(wrapper.text()).toContain('研发组')
    wrapper.unmount()
  })
})
