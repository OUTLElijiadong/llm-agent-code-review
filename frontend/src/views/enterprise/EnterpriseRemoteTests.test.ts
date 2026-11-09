import { flushPromises, mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'

const mock = vi.hoisted(() => ({
  cancelEnterpriseRemoteApproval: vi.fn(),
  decideEnterpriseRemoteApproval: vi.fn(),
  listEnterpriseProjects: vi.fn(),
  listEnterpriseRemoteApprovals: vi.fn(),
  startEnterpriseRemoteApproval: vi.fn(),
  submitEnterpriseRemoteApproval: vi.fn(),
  routerPush: vi.fn(),
}))
vi.mock('@/api/enterprise', () => mock)
vi.mock('@/utils/token', () => ({ getToken: () => null }))
vi.mock('vue-router', () => ({ useRouter: () => ({ push: mock.routerPush }) }))

import EnterpriseRemoteTests from './EnterpriseRemoteTests.vue'

const project = {
  project_id: 500, project_name: '甲组代码审查', enterprise_id: 10, group_id: 22,
  group_name: '研发组', role: 'reviewer' as const,
}
function request(id: number, groupId: number, ownerId: number) {
  return {
    id, enterprise_id: 10, group_id: groupId, project_id: 500, owner_id: ownerId,
    target_url: `https://target-${id}.example.com/`, target_host: `target-${id}.example.com`,
    authorization_basis: '已取得该企业测试域名的明确书面授权', scope_notes: '仅检查根路径的安全响应头',
    requested_window_hours: 4, status: 'pending' as const, decided_by: null, decision_reason: null,
    decided_at: null, create_time: '2026-11-07T08:00:00', execution_task_public_id: null,
    execution_available: false,
  }
}

beforeEach(() => {
  Object.values(mock).forEach(fn => fn.mockReset())
  mock.listEnterpriseProjects.mockResolvedValue([project])
  mock.listEnterpriseRemoteApprovals.mockResolvedValue({ items: [], total: 0 })
  mock.submitEnterpriseRemoteApproval.mockResolvedValue(request(1, 22, 1))
  mock.decideEnterpriseRemoteApproval.mockResolvedValue(request(1, 22, 2))
  mock.cancelEnterpriseRemoteApproval.mockResolvedValue(request(1, 22, 1))
  mock.startEnterpriseRemoteApproval.mockResolvedValue({
    request: request(1, 22, 1), execution: { public_id: 'remote-task-1', status: 'running' },
  })
  ;(window as Window & { confirm: (message?: string) => boolean }).confirm = () => true
  ;(window as Window & { prompt: (message?: string, defaultValue?: string) => string | null }).prompt = () => '范围已核验'
})

describe('企业远测申请与审批', () => {
  it('普通成员只能从可执行企业项目提交固定范围申请', async () => {
    const wrapper = mount(EnterpriseRemoteTests, {
      props: { enterpriseId: 10, currentUserId: 1, role: 'member', groups: [{ id: 22, enterprise_id: 10, name: '研发组', status: 'active', my_role: 'member' }] },
    })
    await flushPromises()
    expect((wrapper.find('#remote-project').element as HTMLSelectElement).value).toBe('500')
    expect(wrapper.text()).toContain('owner/admin 可审批本人申请')
    expect(wrapper.text()).toContain('小组长仅能审批本组其他成员')

    await wrapper.get('#remote-target-url').setValue('https://Example.com/')
    await wrapper.get('#remote-authorization-basis').setValue('已取得目标资产的明确书面授权')
    await wrapper.get('#remote-scope-notes').setValue('只检查根路径')
    await wrapper.get('.acknowledgement input').setValue(true)
    await wrapper.get('.request-card').trigger('submit')
    await flushPromises()

    expect(mock.submitEnterpriseRemoteApproval).toHaveBeenCalledWith(10, {
      group_id: 22,
      project_id: 500,
      target_url: 'https://example.com/',
      authorization_basis: '已取得目标资产的明确书面授权',
      scope_notes: '只检查根路径',
      requested_window_hours: 4,
      ack_rules: true,
    }, null)
    wrapper.unmount()
  })

  it('组长只能审批本组其他成员，不能审批自己或其他小组', async () => {
    const first = request(1, 22, 2)
    const crossGroup = request(2, 23, 3)
    const own = request(3, 22, 1)
    mock.listEnterpriseRemoteApprovals.mockResolvedValue({ items: [first, crossGroup, own], total: 3 })
    const wrapper = mount(EnterpriseRemoteTests, {
      props: {
        enterpriseId: 10, currentUserId: 1, role: 'member',
        groups: [
          { id: 22, enterprise_id: 10, name: '研发组', status: 'active', my_role: 'leader' },
          { id: 23, enterprise_id: 10, name: '测试组', status: 'active', my_role: 'member' },
        ],
      },
    })
    await flushPromises()
    const cards = wrapper.findAll('.request-item')
    expect(cards).toHaveLength(3)
    expect(cards[0].find('.approve').exists()).toBe(true)
    expect(cards[1].find('.approve').exists()).toBe(false)
    expect(cards[2].find('.approve').exists()).toBe(false)
    wrapper.unmount()
  })

  it('企业管理员可审批自己的申请，但操作结果未知时必须先刷新', async () => {
    const own = request(4, 22, 1)
    mock.listEnterpriseRemoteApprovals.mockResolvedValueOnce({ items: [own], total: 1 })
      .mockResolvedValueOnce({ items: [], total: 0 })
    mock.decideEnterpriseRemoteApproval.mockRejectedValueOnce(new Error('transport timeout'))
    const wrapper = mount(EnterpriseRemoteTests, {
      props: { enterpriseId: 10, currentUserId: 1, role: 'owner', groups: [] },
    })
    await flushPromises()
    const approve = wrapper.find('.request-item .approve')
    expect(approve.exists()).toBe(true)
    await approve.trigger('click')
    await flushPromises()
    expect(wrapper.text()).toContain('审批结果未知')
    expect(wrapper.find('.request-item .approve').attributes('disabled')).toBeDefined()

    await wrapper.get('.workbench-header button').trigger('click')
    await flushPromises()
    expect(wrapper.text()).toContain('暂时没有远测申请')
    wrapper.unmount()
  })

  it('小组权限变化时先清除已加载申请详情，再按新权限刷新', async () => {
    const privateRequest = request(9, 22, 2)
    let resolveReload: ((value: { items: ReturnType<typeof request>[]; total: number }) => void) | undefined
    mock.listEnterpriseRemoteApprovals.mockResolvedValueOnce({ items: [privateRequest], total: 1 })
      .mockReturnValueOnce(new Promise(resolve => { resolveReload = resolve }))
    const wrapper = mount(EnterpriseRemoteTests, {
      props: {
        enterpriseId: 10, currentUserId: 1, role: 'member',
        groups: [{ id: 22, enterprise_id: 10, name: '研发组', status: 'active', my_role: 'leader' }],
      },
    })
    await flushPromises()
    expect(wrapper.text()).toContain('target-9.example.com')
    expect(wrapper.find('.request-item .approve').exists()).toBe(true)

    await wrapper.setProps({
      groups: [{ id: 22, enterprise_id: 10, name: '研发组', status: 'active', my_role: 'member' }],
    })
    expect(wrapper.text()).not.toContain('target-9.example.com')
    expect(wrapper.findAll('.request-item')).toHaveLength(0)
    expect(wrapper.text()).toContain('正在读取企业远测申请')

    resolveReload?.({ items: [], total: 0 })
    await flushPromises()
    expect(wrapper.text()).not.toContain('target-9.example.com')
    expect(wrapper.text()).toContain('暂时没有远测申请')
    wrapper.unmount()
  })

  it('拒绝带路径的目标且不会自动触发探测', async () => {
    const wrapper = mount(EnterpriseRemoteTests, {
      props: { enterpriseId: 10, currentUserId: 1, role: 'member', groups: [] },
    })
    await flushPromises()
    await wrapper.get('#remote-target-url').setValue('https://example.com/private')
    await wrapper.get('#remote-authorization-basis').setValue('已取得目标资产的明确书面授权')
    await wrapper.get('.acknowledgement input').setValue(true)
    await wrapper.get('.request-card').trigger('submit')
    await flushPromises()
    expect(wrapper.text()).toContain('不能包含凭据、端口、路径')
    expect(mock.submitEnterpriseRemoteApproval).not.toHaveBeenCalled()
    expect(mock.startEnterpriseRemoteApproval).not.toHaveBeenCalled()
    wrapper.unmount()
  })
})
