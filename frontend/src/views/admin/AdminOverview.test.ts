import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import { nextTick } from 'vue'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { AgentActivity, GeoPoint, SecurityPosture, SystemStatus } from '@/api/adminOverview'
import type { AgentEvent } from '@/types/agentEvent'
import type { UserOut } from '@/types/auth'

const auth = vi.hoisted(() => ({ profile: null as UserOut | null, roles: [] as string[] }))

const mocks = vi.hoisted(() => ({
  getSystemStatus: vi.fn(),
  getSecurityPosture: vi.fn(),
  getLoginGeo: vi.fn(),
  getAgentsActivity: vi.fn(),
  isSuperAdmin: vi.fn(),
  subscribeAgentEvents: vi.fn(),
  closeStream: vi.fn(),
  routerPush: vi.fn(),
  setOption: vi.fn(),
  resize: vi.fn(),
  dispose: vi.fn(),
}))

vi.mock('@/api/adminOverview', () => ({
  getSystemStatus: mocks.getSystemStatus,
  getSecurityPosture: mocks.getSecurityPosture,
  getLoginGeo: mocks.getLoginGeo,
  getAgentsActivity: mocks.getAgentsActivity,
}))
vi.mock('@/stores/user', () => ({ useUserStore: () => ({ ...auth, isSuperAdmin: mocks.isSuperAdmin }) }))
vi.mock('vue-router', () => ({ useRouter: () => ({ push: mocks.routerPush }) }))
vi.mock('@/utils/agentEventStream', () => ({ subscribeAgentEvents: mocks.subscribeAgentEvents }))
vi.mock('echarts/core', () => ({
  use: vi.fn(),
  registerMap: vi.fn(),
  init: vi.fn(() => ({ setOption: mocks.setOption, resize: mocks.resize, dispose: mocks.dispose })),
}))

import AdminOverview from './AdminOverview.vue'

function security(overrides: Partial<SecurityPosture> = {}): SecurityPosture {
  return {
    level: 'ok',
    signals: [],
    brute_force_ips: [],
    login_failed_24h: 0,
    login_success_24h: 23,
    malware_infected_total: 0,
    malware_infected_24h: 0,
    top_login_ips: [],
    note: '基于应用日志',
    collected_at: '2026-09-06T00:00:00Z',
    ...overrides,
  }
}

const system: SystemStatus = {
  available: true,
  collected_at: '2026-09-06T00:00:00Z',
  process_uptime_seconds: 60,
  cpu_percent: 37,
  memory_percent: 42,
  disk_percent: 55,
  disk_used_gb: 55,
  disk_total_gb: 100,
}
const geo: GeoPoint[] = [{ ip: '192.0.2.1', country: '测试来源', latitude: 22, longitude: 114, count: 7 }]
const agents: AgentActivity[] = [{
  agent_code: 'review_agent',
  name: '审查代理',
  status: 'working',
  calls_today: 9,
  purpose: '检查当前任务',
  is_enabled: 1,
}]
const sections = [
  { title: '服务器状态', request: mocks.getSystemStatus, value: system, content: '37%' },
  { title: '安全态势', request: mocks.getSecurityPosture, value: security(), content: '23' },
  { title: '登录来源分布', request: mocks.getLoginGeo, value: geo, content: '1 个来源' },
  { title: 'Agent 活跃状态', request: mocks.getAgentsActivity, value: agents, content: '审查代理' },
]
const wrappers: VueWrapper[] = []

function deferred<T>() {
  let resolve!: (value: T) => void
  let reject!: (reason: unknown) => void
  const promise = new Promise<T>((resolvePromise, rejectPromise) => {
    resolve = resolvePromise
    reject = rejectPromise
  })
  return { promise, resolve, reject }
}

function mountPage(): VueWrapper {
  const wrapper = mount(AdminOverview, {
    global: { stubs: { 'el-icon': { template: '<span><slot /></span>' } } },
  })
  wrappers.push(wrapper)
  return wrapper
}

function card(wrapper: VueWrapper, title: string) {
  const result = wrapper.findAll('section').find((section) => section.find('h3').text() === title)
  if (!result) throw new Error(`缺少分区：${title}`)
  return result
}

function mapOption() {
  return mocks.setOption.mock.lastCall?.[0]
}

beforeEach(() => {
  vi.useFakeTimers()
  Object.values(mocks).forEach((mock) => mock.mockReset())
  mocks.getSystemStatus.mockResolvedValue({ ...system })
  mocks.getSecurityPosture.mockResolvedValue(security())
  mocks.getLoginGeo.mockResolvedValue(geo.map((point) => ({ ...point })))
  mocks.getAgentsActivity.mockResolvedValue(agents.map((agent) => ({ ...agent })))
  mocks.isSuperAdmin.mockReturnValue(true)
  auth.profile = { id: 27, username: 'admin', role: 'super_admin', status: 1 }
  auth.roles = ['super_admin']
  mocks.subscribeAgentEvents.mockReturnValue({ close: mocks.closeStream })
})

afterEach(() => {
  wrappers.splice(0).forEach((wrapper) => wrapper.unmount())
  vi.useRealTimers()
})

describe('AdminOverview 真实状态与分区反馈', () => {
  it('首载不把未返回的接口伪装为正常、零值或空列表', async () => {
    sections.forEach(({ request }) => request.mockReturnValue(deferred().promise))
    const wrapper = mountPage()
    await nextTick()

    expect(wrapper.find('.posture-badge').text()).toContain('加载中')
    expect(wrapper.find('.posture-badge').classes()).not.toContain('lv-ok')
    expect(wrapper.text()).not.toContain('系统正常')
    expect(wrapper.text()).not.toContain('未发现')
    expect(wrapper.text()).not.toContain('暂无 Agent 数据')
    expect(wrapper.text()).not.toContain('psutil')
    expect(card(wrapper, '安全态势').findAll('.p-num')[2].text()).toBe('未知')
    expect(card(wrapper, 'Agent 活跃状态').text()).not.toContain('0 个运行中')
    expect(card(wrapper, '登录来源分布').text()).not.toContain('0 个来源')
    sections.forEach(({ title }) => {
      expect(card(wrapper, title).attributes('aria-busy')).toBe('true')
      expect(card(wrapper, title).find('[role="status"]').text()).toContain('加载')
    })
  })

  it('安全态势先完成时立即显示成功分区，不等待其它接口', async () => {
    mocks.getSystemStatus.mockReturnValue(deferred().promise)
    mocks.getLoginGeo.mockReturnValue(deferred().promise)
    mocks.getAgentsActivity.mockReturnValue(deferred().promise)
    const wrapper = mountPage()
    await flushPromises()

    expect(card(wrapper, '安全态势').text()).toContain('23')
    expect(card(wrapper, '安全态势').attributes('data-state')).toBe('success')
    expect(card(wrapper, '安全态势').attributes('aria-busy')).toBe('false')
    expect(card(wrapper, '服务器状态').attributes('aria-busy')).toBe('true')
    expect(wrapper.classes()).not.toContain('is-booting')
  })

  it('安全态势失败提供可见错误，不宣称没有异常', async () => {
    mocks.getSecurityPosture.mockRejectedValue({ message: '日志查询失败', next_action: '请稍后重试' })
    const wrapper = mountPage()
    await flushPromises()

    expect(card(wrapper, '安全态势').find('[role="alert"]').text()).toContain('日志查询失败')
    expect(card(wrapper, '安全态势').text()).toContain('请稍后重试')
    expect(wrapper.find('.posture-badge').text()).toContain('未知')
    expect(wrapper.find('.posture-badge').classes()).not.toContain('lv-ok')
    expect(card(wrapper, '安全态势').text()).not.toContain('未发现')
    expect(card(wrapper, '安全态势').findAll('.p-num')[2].text()).toBe('未知')
  })

  it('成功响应没有安全态势对象时显示未取得，而非正常', async () => {
    mocks.getSecurityPosture.mockResolvedValue(null)
    const wrapper = mountPage()
    await flushPromises()

    expect(card(wrapper, '安全态势').attributes('data-state')).toBe('empty')
    expect(card(wrapper, '安全态势').text()).toContain('未取得安全态势')
    expect(wrapper.find('.posture-badge').text()).toContain('未知')
    expect(card(wrapper, '安全态势').text()).not.toContain('未发现')
  })

  it('未返回 signals 时不把缺失的字段视为无风险证据', async () => {
    const partial = security()
    Reflect.deleteProperty(partial, 'signals')
    mocks.getSecurityPosture.mockResolvedValue(partial)
    const wrapper = mountPage()
    await flushPromises()

    expect(card(wrapper, '安全态势').text()).toContain('信号数据未取得')
    expect(card(wrapper, '安全态势').text()).not.toContain('未发现')
    expect(wrapper.find('.posture-badge').classes()).not.toContain('lv-ok')
  })

  it.each([undefined, null])('malware 为 %s 时显示未知而非 0', async (value) => {
    mocks.getSecurityPosture.mockResolvedValue({ ...security(), malware_infected_24h: value })
    const wrapper = mountPage()
    await flushPromises()

    expect(card(wrapper, '安全态势').findAll('.p-num')[2].text()).toBe('未知')
  })

  it('保留接口确实返回的 0，健康徽章仅描述应用日志安全态势', async () => {
    const wrapper = mountPage()
    await flushPromises()

    expect(wrapper.find('.posture-badge').text()).toBe('安全态势：日志未见异常')
    expect(wrapper.find('.posture-badge').classes()).toContain('lv-ok')
    expect(wrapper.text()).not.toContain('系统正常')
    expect(card(wrapper, '安全态势').text()).toContain('当前应用日志未发现异常信号')
    expect(card(wrapper, '安全态势').findAll('.p-num')[2].text()).toBe('0')
  })

  it('真实攻击信号与原有处置跳转保持可用', async () => {
    mocks.getSecurityPosture.mockResolvedValue(security({
      level: 'attack',
      signals: [{ type: 'brute_force', severity: 'high', title: '登录爆破', detail: '日志记录需核验' }],
    }))
    const wrapper = mountPage()
    await flushPromises()

    expect(wrapper.find('.posture-badge').text()).toBe('安全态势：检测到攻击迹象')
    await wrapper.find('.signals li').trigger('click')
    expect(mocks.routerPush).toHaveBeenCalledWith({ path: '/admin/audit', query: { keyword: '登录' } })
  })

  it('Agent 失败不是空态，也不是零个运行中', async () => {
    mocks.getAgentsActivity.mockRejectedValue(new Error('Agent 查询失败'))
    const wrapper = mountPage()
    await flushPromises()

    const section = card(wrapper, 'Agent 活跃状态')
    expect(section.attributes('data-state')).toBe('error')
    expect(section.find('[role="alert"]').text()).toContain('Agent 查询失败')
    expect(section.text()).not.toContain('暂无 Agent 数据')
    expect(section.text()).not.toContain('0 个运行中')
    expect(section.text()).toContain('运行状态未知')
  })

  it('Agent 真实空数组可显示空态及零个运行中', async () => {
    mocks.getAgentsActivity.mockResolvedValue([])
    const wrapper = mountPage()
    await flushPromises()

    const section = card(wrapper, 'Agent 活跃状态')
    expect(section.attributes('data-state')).toBe('empty')
    expect(section.text()).toContain('暂无 Agent 数据')
    expect(section.text()).toContain('0 个运行中')
    expect(section.find('[role="alert"]').exists()).toBe(false)
  })

  it('Agent 响应缺失与真实空数组区分，不能显示零个运行中', async () => {
    mocks.getAgentsActivity.mockResolvedValue(null)
    const wrapper = mountPage()
    await flushPromises()

    const section = card(wrapper, 'Agent 活跃状态')
    expect(section.text()).toContain('未取得 Agent 数据')
    expect(section.text()).not.toContain('暂无 Agent 数据')
    expect(section.text()).not.toContain('0 个运行中')
  })

  it.each([
    { code: 40322, message: '仅超级管理员 admin 可执行此操作' },
    { response: { status: 403 } },
  ])('服务器 403 保持权限拒绝语义，不归因为 psutil：%j', async (error) => {
    mocks.getSystemStatus.mockRejectedValue(error)
    const wrapper = mountPage()
    await flushPromises()

    const section = card(wrapper, '服务器状态')
    expect(section.attributes('data-state')).toBe('error')
    expect(section.find('[role="alert"]').text()).toMatch(/超级管理员|权限.*403/)
    expect(section.text()).not.toContain('psutil')
    expect(section.text()).not.toContain('指标采集不可用')
    expect(card(wrapper, '安全态势').text()).toContain('23')
    expect(card(wrapper, 'Agent 活跃状态').text()).toContain('审查代理')
  })

  it('服务器明确返回 available=false 时才显示采集不可用', async () => {
    mocks.getSystemStatus.mockResolvedValue({ ...system, available: false })
    const wrapper = mountPage()
    await flushPromises()

    expect(card(wrapper, '服务器状态').attributes('data-state')).toBe('empty')
    expect(card(wrapper, '服务器状态').text()).toContain('指标采集不可用')
    expect(card(wrapper, '服务器状态').find('[role="alert"]').exists()).toBe(false)
  })

  it('非超级管理员不请求或展示服务器指标，也不影响可读分区', async () => {
    mocks.isSuperAdmin.mockReturnValue(false)
    auth.profile = { id: 27, username: 'admin', role: 'admin', status: 1 }
    auth.roles = ['admin']
    const wrapper = mountPage()
    await flushPromises()
    await vi.advanceTimersByTimeAsync(5_000)

    expect(mocks.getSystemStatus).not.toHaveBeenCalled()
    expect(wrapper.text()).not.toContain('服务器状态')
    expect(mocks.getSecurityPosture).toHaveBeenCalledTimes(2)
    expect(mocks.getAgentsActivity).toHaveBeenCalledTimes(2)
  })

  it.each([
    { name: '非 admin 用户名', profile: { id: 1, username: 'other', role: 'super_admin', status: 1 }, roles: ['super_admin'] },
    { name: '旧角色不一致', profile: { id: 1, username: 'admin', role: 'admin', status: 1 }, roles: ['super_admin'] },
    { name: '账号未启用', profile: { id: 1, username: 'admin', role: 'super_admin', status: 0 }, roles: ['super_admin'] },
    { name: '无有效超级管理员角色绑定', profile: { id: 1, username: 'admin', role: 'super_admin', status: 1 }, roles: [] },
    { name: '账号信息未取得', profile: null, roles: ['super_admin'] },
  ])('$name 时不因宽松的超级管理员标志而请求服务器', async ({ profile, roles }) => {
    auth.profile = profile
    auth.roles = roles
    const wrapper = mountPage()
    await flushPromises()
    await vi.advanceTimersByTimeAsync(10_000)

    expect(mocks.getSystemStatus).not.toHaveBeenCalled()
    expect(wrapper.text()).not.toContain('服务器状态')
    expect(card(wrapper, '安全态势').text()).toContain('23')
  })

  it('服务器只读身份契约不要求 id=1', async () => {
    expect(auth.profile?.id).toBe(27)
    const wrapper = mountPage()
    await flushPromises()

    expect(mocks.getSystemStatus).toHaveBeenCalledOnce()
    expect(card(wrapper, '服务器状态').text()).toContain('37%')
  })

  it.each([
    { code: 40322, message: '仅超级管理员 admin 可执行此操作' },
    { response: { status: 403 } },
  ])('后端拒绝服务器读取后暂停自动请求，只有手动重试成功才恢复：%j', async (error) => {
    mocks.getSystemStatus.mockRejectedValueOnce(error)
    const wrapper = mountPage()
    await flushPromises()
    await vi.advanceTimersByTimeAsync(20_000)

    expect(mocks.getSystemStatus).toHaveBeenCalledOnce()
    expect(mocks.getSecurityPosture).toHaveBeenCalledTimes(5)
    expect(card(wrapper, '服务器状态').text()).toContain('自动刷新已暂停')
    expect(card(wrapper, '服务器状态').attributes('data-state')).toBe('error')
    await card(wrapper, '服务器状态').find('button.retry-button').trigger('click')
    await flushPromises()
    expect(mocks.getSystemStatus).toHaveBeenCalledTimes(2)
    expect(card(wrapper, '服务器状态').attributes('data-state')).toBe('success')
    await vi.advanceTimersByTimeAsync(5_000)
    expect(mocks.getSystemStatus).toHaveBeenCalledTimes(3)
  })

  it('暂时的 503 错误仍允许现有轮询恢复，而不是权限拒绝', async () => {
    mocks.getSystemStatus.mockRejectedValueOnce({ response: { status: 503 }, message: '服务暂不可用' })
    const wrapper = mountPage()
    await flushPromises()

    expect(card(wrapper, '服务器状态').text()).not.toContain('自动刷新已暂停')
    await vi.advanceTimersByTimeAsync(5_000)
    expect(mocks.getSystemStatus).toHaveBeenCalledTimes(2)
    expect(card(wrapper, '服务器状态').attributes('data-state')).toBe('success')
  })

  it.each([[], null])('地图无数据 %j 时不显示 0-1 强度图例', async (value) => {
    mocks.getLoginGeo.mockResolvedValue(value)
    const wrapper = mountPage()
    await flushPromises()

    expect(card(wrapper, '登录来源分布').attributes('data-state')).toBe('empty')
    expect(mapOption()?.visualMap.show).toBe(false)
    expect(mapOption()?.series[0].data).toEqual([])
    if (value === null) {
      expect(card(wrapper, '登录来源分布').text()).not.toContain('0 个来源')
    } else {
      expect(card(wrapper, '登录来源分布').text()).toContain('暂无可定位的成功登录来源')
    }
  })

  it('地图数据失败不能显示真实空态或虚构来源数', async () => {
    mocks.getLoginGeo.mockRejectedValue(new Error('定位查询失败'))
    const wrapper = mountPage()
    await flushPromises()

    const section = card(wrapper, '登录来源分布')
    expect(section.find('[role="alert"]').text()).toContain('定位查询失败')
    expect(section.text()).not.toContain('暂无可定位')
    expect(section.text()).not.toContain('0 个来源')
    expect(mapOption()?.visualMap.show).toBe(false)
  })

  it('地图绘制异常仅影响地图分区，不阻断其它接口或吞掉重试反馈', async () => {
    mocks.setOption.mockImplementationOnce(() => { throw new Error('地图绘制不可用') })
    const wrapper = mountPage()
    await flushPromises()

    expect(card(wrapper, '登录来源分布').find('[role="alert"]').text()).toContain('地图绘制不可用')
    expect(card(wrapper, '安全态势').attributes('data-state')).toBe('success')
    expect(card(wrapper, 'Agent 活跃状态').text()).toContain('审查代理')
    await card(wrapper, '登录来源分布').find('button.retry-button').trigger('click')
    await flushPromises()
    expect(card(wrapper, '登录来源分布').attributes('data-state')).toBe('success')
  })

  it.each(sections)('$title 独立重试反馈进度、禁止重复请求并保留其它成功分区', async ({ title, request, value }) => {
    request.mockRejectedValueOnce(new Error('暂时失败'))
    const wrapper = mountPage()
    await flushPromises()
    const recovery = deferred()
    request.mockReturnValueOnce(recovery.promise)

    const button = card(wrapper, title).find('button.retry-button')
    expect(button.text()).toBe('重试')
    await button.trigger('click')
    expect(card(wrapper, title).attributes('aria-busy')).toBe('true')
    expect(button.attributes('disabled')).toBeDefined()
    expect(button.text()).toContain('加载中')
    await button.trigger('click')
    expect(request).toHaveBeenCalledTimes(2)
    sections.filter((section) => section.title !== title).forEach((section) => {
      expect(section.request).toHaveBeenCalledTimes(1)
      expect(card(wrapper, section.title).text()).toContain(section.content)
      expect(card(wrapper, section.title).attributes('data-state')).toBe('success')
    })

    recovery.resolve(value)
    await flushPromises()
    expect(card(wrapper, title).attributes('data-state')).toBe('success')
    expect(card(wrapper, title).attributes('aria-busy')).toBe('false')
    expect(card(wrapper, title).find('[role="alert"]').exists()).toBe(false)
  })

  it.each(sections)('$title 刷新失败保留上次成功数据，并持续标记过期直到恢复', async ({ title, request, content, value }) => {
    const wrapper = mountPage()
    await flushPromises()
    request.mockRejectedValueOnce(new Error('刷新失败'))
    await vi.advanceTimersByTimeAsync(5_000)
    await flushPromises()

    const section = card(wrapper, title)
    expect(section.attributes('data-state')).toBe('error')
    expect(section.text()).toContain(content)
    expect(section.text()).toContain('上次成功数据已过期')
    if (title === '安全态势') {
      expect(wrapper.find('.posture-badge').classes()).not.toContain('lv-ok')
      expect(wrapper.find('.posture-badge').text()).toContain('未知')
      expect(section.text()).not.toContain('当前应用日志未发现')
    }
    const recovery = deferred()
    request.mockReturnValueOnce(recovery.promise)
    await section.find('button.retry-button').trigger('click')
    expect(section.text()).toContain('上次成功数据已过期')
    recovery.resolve(value)
    await flushPromises()
    expect(section.attributes('data-state')).toBe('success')
    expect(section.text()).not.toContain('已过期')
  })

  it('某个接口持续未完成也不阻止其它分区轮询', async () => {
    mocks.getAgentsActivity.mockReturnValue(deferred().promise)
    const wrapper = mountPage()
    await flushPromises()
    await vi.advanceTimersByTimeAsync(10_000)

    expect(mocks.getAgentsActivity).toHaveBeenCalledTimes(1)
    expect(mocks.getSecurityPosture).toHaveBeenCalledTimes(3)
    expect(card(wrapper, '安全态势').text()).toContain('23')
  })

  it('SSE 连接或单条事件不能掩盖 Agent 汇总接口失败', async () => {
    const wrapper = mountPage()
    await flushPromises()
    mocks.getAgentsActivity.mockRejectedValueOnce(new Error('汇总查询失败'))
    await vi.advanceTimersByTimeAsync(5_000)
    const [onEvent, options] = mocks.subscribeAgentEvents.mock.calls[0]
    options.onStatus('connected')
    onEvent({ type: 'progress', agent: 'review_agent', message: '单条实时事件', timestamp: '2026-09-06T00:01:00Z' } as AgentEvent)
    await nextTick()

    const section = card(wrapper, 'Agent 活跃状态')
    expect(section.text()).toContain('单条实时事件')
    expect(section.text()).toContain('上次成功数据已过期')
    expect(section.attributes('data-state')).toBe('error')
    expect(section.text()).toContain('运行状态未知')
  })

  it('卸载后清理轮询、事件流及地图，忽略尚未返回的数据', async () => {
    const pendingGeo = deferred<GeoPoint[]>()
    mocks.getLoginGeo.mockReturnValue(pendingGeo.promise)
    const wrapper = mountPage()
    await flushPromises()
    wrapper.unmount()
    wrappers.splice(wrappers.indexOf(wrapper), 1)
    const chartCalls = mocks.setOption.mock.calls.length
    pendingGeo.resolve(geo)
    await flushPromises()
    await vi.advanceTimersByTimeAsync(10_000)

    expect(mocks.closeStream).toHaveBeenCalledOnce()
    expect(mocks.dispose).toHaveBeenCalledOnce()
    expect(mocks.setOption).toHaveBeenCalledTimes(chartCalls)
    expect(mocks.getSecurityPosture).toHaveBeenCalledOnce()
  })
})
