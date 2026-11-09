import { flushPromises, mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'

const api = vi.hoisted(() => ({ getPolicy: vi.fn(), updatePolicy: vi.fn() }))
const session = vi.hoisted(() => ({ token: 'session-a' }))
vi.mock('@/api/enterpriseProactive', () => api)
vi.mock('@/utils/token', () => ({ getToken: () => session.token }))
import EnterpriseProactiveSettings from './EnterpriseProactiveSettings.vue'

const policy = { enabled: false, monthly_credits: 0, version: 0, metering_ready: true, worker_ready: true }
function deferred<T>() {
  let resolve!: (value: T) => void
  const promise = new Promise<T>(done => { resolve = done })
  return { promise, resolve }
}
beforeEach(() => {
  vi.clearAllMocks(); session.token = 'session-a'
  api.getPolicy.mockResolvedValue({ ...policy }); api.updatePolicy.mockResolvedValue({ ...policy })
})
describe('企业小菱主动讨论设置', () => {
  it('默认关闭，开启时提交企业月预算与当前会话', async () => {
    const wrapper = mount(EnterpriseProactiveSettings, { props: { enterpriseId: 10, currentUserId: 1 } })
    await flushPromises()
    expect(api.getPolicy).toHaveBeenCalledWith(10, 'session-a')
    expect((wrapper.get('#proactive-enabled').element as HTMLInputElement).checked).toBe(false)
    await wrapper.get('#proactive-enabled').setValue(true); await wrapper.get('#proactive-budget').setValue(300)
    await wrapper.get('form').trigger('submit'); await flushPromises()
    expect(api.updatePolicy).toHaveBeenCalledWith(10, { enabled: true, monthly_credits: 300, expected_version: 0 }, 'session-a')
    wrapper.unmount()
  })
  it('计量不可用仍能关闭已启用设置', async () => {
    api.getPolicy.mockResolvedValue({ ...policy, enabled: true, monthly_credits: 100, metering_ready: false })
    const wrapper = mount(EnterpriseProactiveSettings, { props: { enterpriseId: 10, currentUserId: 1 } })
    await flushPromises(); await wrapper.get('#proactive-enabled').setValue(false)
    await wrapper.get('form').trigger('submit'); await flushPromises()
    expect(api.updatePolicy).toHaveBeenCalledWith(10, { enabled: false, monthly_credits: 100, expected_version: 0 }, 'session-a')
    wrapper.unmount()
  })
  it('服务关闭时不能开启但仍可关闭已启用设置', async () => {
    api.getPolicy.mockResolvedValue({ ...policy, enabled: true, monthly_credits: 100, worker_ready: false })
    const wrapper = mount(EnterpriseProactiveSettings, { props: { enterpriseId: 10, currentUserId: 1 } })
    await flushPromises()
    expect(wrapper.text()).toContain('主动讨论服务未开启')
    await wrapper.get('#proactive-enabled').setValue(false)
    await wrapper.get('form').trigger('submit'); await flushPromises()
    expect(api.updatePolicy).toHaveBeenCalledWith(10, { enabled: false, monthly_credits: 100, expected_version: 0 }, 'session-a')
    wrapper.unmount()
  })
  it('开启时预算必须为正整数', async () => {
    const wrapper = mount(EnterpriseProactiveSettings, { props: { enterpriseId: 10, currentUserId: 1 } })
    await flushPromises(); await wrapper.get('#proactive-enabled').setValue(true)
    expect((wrapper.get('button[type=submit]').element as HTMLButtonElement).disabled).toBe(true)
    await wrapper.get('#proactive-budget').setValue(1.5)
    expect((wrapper.get('button[type=submit]').element as HTMLButtonElement).disabled).toBe(true)
    await wrapper.get('#proactive-budget').setValue(1)
    expect((wrapper.get('button[type=submit]').element as HTMLButtonElement).disabled).toBe(false)
    wrapper.unmount()
  })
  it('企业切换及账号会话变更时忽略过期响应', async () => {
    const old = deferred<typeof policy>()
    api.getPolicy.mockReturnValueOnce(old.promise).mockResolvedValueOnce({ ...policy, monthly_credits: 42 })
    const wrapper = mount(EnterpriseProactiveSettings, { props: { enterpriseId: 10, currentUserId: 1 } })
    await wrapper.setProps({ enterpriseId: 20 }); await flushPromises()
    old.resolve({ ...policy, enabled: true, monthly_credits: 999 }); await flushPromises()
    expect((wrapper.get('#proactive-budget').element as HTMLInputElement).value).toBe('42')
    await wrapper.get('#proactive-enabled').setValue(true); session.token = 'session-b'
    await wrapper.get('form').trigger('submit'); await flushPromises()
    expect(api.updatePolicy).not.toHaveBeenCalled(); wrapper.unmount()
  })
  it('保存结果不明时要求重新读取，防止误报成功', async () => {
    api.updatePolicy.mockRejectedValueOnce(new Error('network'))
    const wrapper = mount(EnterpriseProactiveSettings, { props: { enterpriseId: 10, currentUserId: 1 } })
    await flushPromises(); await wrapper.get('#proactive-enabled').setValue(true)
    await wrapper.get('#proactive-budget').setValue(100); await wrapper.get('form').trigger('submit'); await flushPromises()
    expect(wrapper.text()).toContain('保存结果未知')
    expect((wrapper.get('button[type=submit]').element as HTMLButtonElement).disabled).toBe(true)
    await wrapper.get('[data-action=refresh]').trigger('click'); await flushPromises()
    expect(api.getPolicy).toHaveBeenCalledTimes(2); expect(wrapper.text()).not.toContain('保存结果未知')
    wrapper.unmount()
  })
  it('其他管理员已修改时读取新设置并要求再次确认，不自动覆盖', async () => {
    api.updatePolicy.mockRejectedValueOnce({ code: 40901 })
    api.getPolicy.mockResolvedValueOnce({ ...policy }).mockResolvedValueOnce({ ...policy, monthly_credits: 77, version: 1 })
    const wrapper = mount(EnterpriseProactiveSettings, { props: { enterpriseId: 10, currentUserId: 1 } })
    await flushPromises(); await wrapper.get('#proactive-enabled').setValue(true)
    await wrapper.get('#proactive-budget').setValue(100); await wrapper.get('form').trigger('submit'); await flushPromises()
    expect(wrapper.text()).toContain('设置已被修改')
    expect((wrapper.get('#proactive-budget').element as HTMLInputElement).value).toBe('77')
    expect(api.updatePolicy).toHaveBeenCalledTimes(1)
    wrapper.unmount()
  })
})
