import { flushPromises, mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'

const mock = vi.hoisted(() => ({
  listEnterpriseAssistantApprovals: vi.fn(),
  decideEnterpriseAssistantApproval: vi.fn(),
}))
vi.mock('@/api/enterprise', () => mock)

import EnterpriseAssistantApprovals from './EnterpriseAssistantApprovals.vue'

const request = {
  id: 51, run_id: 'ear_51', conversation_id: 9, group_id: 3, initiator_user_id: 4,
  required_role: 'enterprise_admin', tool: 'propose_enterprise_announcement',
  title: '本周维护计划', content: '周五 22:00 开始维护。', status: 'pending',
  create_time: '2026-11-01T08:00:00',
}

beforeEach(() => {
  mock.listEnterpriseAssistantApprovals.mockReset().mockResolvedValue({ items: [request] })
  mock.decideEnterpriseAssistantApproval.mockReset().mockResolvedValue({ run_id: request.run_id, status: 'running' })
})

describe('企业小菱操作审批', () => {
  it('显示公告的完整精确内容，批准后打开原会话', async () => {
    const wrapper = mount(EnterpriseAssistantApprovals, { props: { enterpriseId: 10 } })
    await flushPromises()
    expect(wrapper.text()).toContain(request.title)
    expect(wrapper.text()).toContain(request.content)
    await wrapper.findAll('button').find(button => button.text() === '批准并发布')!.trigger('click')
    await flushPromises()
    expect(mock.decideEnterpriseAssistantApproval).toHaveBeenCalledWith(10, 51, 'approve', '', null)
    expect(wrapper.emitted('open-conversation')).toEqual([[9]])
    expect(wrapper.text()).toContain('当前没有待审批请求')
    wrapper.unmount()
  })

  it('拒绝时保留审计理由，并在结果未知时不自动重复决定', async () => {
    mock.decideEnterpriseAssistantApproval.mockRejectedValueOnce(new Error('timeout'))
    const wrapper = mount(EnterpriseAssistantApprovals, { props: { enterpriseId: 10 } })
    await flushPromises()
    await wrapper.findAll('button').find(button => button.text() === '拒绝')!.trigger('click')
    await flushPromises()
    expect(mock.decideEnterpriseAssistantApproval).toHaveBeenCalledWith(
      10, 51, 'reject', '企业管理员拒绝发布此公告', null,
    )
    expect(wrapper.text()).toContain('刷新列表确认服务器状态')
    expect(wrapper.text()).toContain(request.title)
    wrapper.unmount()
  })

  it('切换企业后不显示上个企业的审批结果', async () => {
    let finish!: (value: unknown) => void
    mock.listEnterpriseAssistantApprovals.mockImplementationOnce(() => new Promise(resolve => { finish = resolve }))
      .mockResolvedValueOnce({ items: [] })
    const wrapper = mount(EnterpriseAssistantApprovals, { props: { enterpriseId: 10 } })
    await wrapper.setProps({ enterpriseId: 20 })
    await flushPromises()
    finish({ items: [request] })
    await flushPromises()
    expect(wrapper.text()).not.toContain(request.title)
    wrapper.unmount()
  })
})
