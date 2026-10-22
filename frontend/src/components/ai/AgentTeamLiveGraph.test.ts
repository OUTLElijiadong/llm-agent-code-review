import { mount } from '@vue/test-utils'
import { nextTick } from 'vue'
import { afterEach, describe, expect, it, vi } from 'vitest'
import type { AgentTeamDetail } from '@/api/agentTeams'
import AgentTeamLiveGraph from './AgentTeamLiveGraph.vue'

const originalResizeObserver = window.ResizeObserver

function team(): AgentTeamDetail {
  return {
    team_id: 7, title: '代码质量与安全', surface: 'user', session_id: 'session-a',
    status: 'running', max_active_children: 2, trace_id: 'trace-a',
    members: [
      { member_id: 1, member_key: 'security', display_name: '安全检查', address: 'agent:security', kind: 'registered', role: 'worker', status: 'queued' },
      { member_id: 2, member_key: 'review', display_name: '临时审查员', address: 'temporary:review', kind: 'temporary', role: 'worker', status: 'created' },
    ],
    tasks: [{ task_id: 9, task_key: 'scan', member_id: 1, title: '检查输入边界', depends_on: [], status: 'queued', priority: 1, attempt_count: 0, max_attempts: 1 }],
    events: [], messages: [],
  }
}

describe('实时团队星图', () => {
  afterEach(() => {
    window.ResizeObserver = originalResizeObserver
    vi.restoreAllMocks()
  })

  it('窄浮窗按容器宽度切为单列，姓名、状态和任务分行避免标签碰撞', async () => {
    let resize: ResizeObserverCallback | undefined
    class TestResizeObserver implements ResizeObserver {
      constructor(callback: ResizeObserverCallback) { resize = callback }
      observe() {}
      unobserve() {}
      disconnect() {}
    }
    window.ResizeObserver = TestResizeObserver
    const detail = team()
    detail.members[0]!.display_name = '非常长的安全检查成员名称用于验证窄浮窗超长标签不会彼此遮挡'
    const wrapper = mount(AgentTeamLiveGraph, { props: { teams: [detail] } })
    await nextTick()
    expect(resize).toBeDefined()
    resize?.([{ contentRect: { width: 320 } } as ResizeObserverEntry], {} as ResizeObserver)
    await nextTick()

    const svg = wrapper.find('svg')
    expect(svg.attributes('viewBox')).toMatch(/^0 0 320 /)
    const rects = wrapper.findAll('.live-node rect')
    expect(rects).toHaveLength(2)
    expect(rects[0]!.attributes('x')).toBe(rects[1]!.attributes('x'))
    expect(Number(rects[0]!.attributes('width'))).toBeLessThan(320)

    const node = wrapper.find('[data-member-id="1"]')
    const labelYs = ['.live-name', '.live-status', '.live-task']
      .map(selector => node.find(selector).attributes('y'))
    expect(new Set(labelYs).size).toBe(3)
    expect(node.find('.live-name').text().length).toBeLessThan(detail.members[0]!.display_name.length)
  })

  it('空挂载后组建与真实团队出现时绑定测宽，消失重现时断开旧观察并重新绑定', async () => {
    const observers: TestResizeObserver[] = []
    class TestResizeObserver implements ResizeObserver {
      observe = vi.fn()
      unobserve = vi.fn()
      disconnect = vi.fn()
      constructor(readonly callback: ResizeObserverCallback) { observers.push(this) }
      resize(width: number) {
        this.callback([{ contentRect: { width } } as ResizeObserverEntry], this)
      }
    }
    window.ResizeObserver = TestResizeObserver
    const wrapper = mount(AgentTeamLiveGraph, { props: { teams: [], forming: false } })
    try {
      expect(wrapper.find('section').exists()).toBe(false)
      expect(observers).toHaveLength(0)

      await wrapper.setProps({ forming: true })
      expect(observers).toHaveLength(1)
      const first = observers[0]!
      const firstRoot = wrapper.get('section').element
      expect(first.observe).toHaveBeenCalledExactlyOnceWith(firstRoot)

      await wrapper.setProps({ forming: false, teams: [team()] })
      expect(observers).toHaveLength(1)
      expect(first.disconnect).not.toHaveBeenCalled()
      first.resize(320)
      await nextTick()
      expect(wrapper.get('svg').attributes('viewBox')).toMatch(/^0 0 320 /)
      const nodes = wrapper.findAll('.live-node rect')
      expect(nodes[0]!.attributes('x')).toBe(nodes[1]!.attributes('x'))

      await wrapper.setProps({ teams: [], forming: false })
      expect(wrapper.find('section').exists()).toBe(false)
      expect(first.disconnect).toHaveBeenCalledTimes(1)

      await wrapper.setProps({ teams: [team()] })
      expect(observers).toHaveLength(2)
      const second = observers[1]!
      const secondRoot = wrapper.get('section').element
      expect(secondRoot).not.toBe(firstRoot)
      expect(second.observe).toHaveBeenCalledExactlyOnceWith(secondRoot)
      second.resize(280)
      await nextTick()
      expect(wrapper.get('svg').attributes('viewBox')).toMatch(/^0 0 280 /)

      first.resize(900)
      await nextTick()
      expect(wrapper.get('svg').attributes('viewBox')).toMatch(/^0 0 280 /)
    } finally {
      wrapper.unmount()
    }
    expect(observers).toHaveLength(2)
    expect(observers[1]!.disconnect).toHaveBeenCalledTimes(1)
  })

  it('缩放控件有可访问名称、具有边界，且总览恢复适配比例和滚动位置', async () => {
    const wrapper = mount(AgentTeamLiveGraph, { props: { teams: [team()] } })
    const zoomIn = wrapper.get('button[aria-label="放大星图"]')
    const zoomOut = wrapper.get('button[aria-label="缩小星图"]')
    const overview = wrapper.get('button[aria-label="总览星图"]')
    const svg = wrapper.get('svg')

    expect(svg.attributes('style')).toContain('width: 100%')
    await zoomIn.trigger('click')
    expect(svg.attributes('style')).toContain('width: 125%')
    for (let i = 0; i < 8; i += 1) await zoomIn.trigger('click')
    expect(svg.attributes('style')).toContain('width: 200%')
    expect((zoomIn.element as HTMLButtonElement).disabled).toBe(true)
    for (let i = 0; i < 10; i += 1) await zoomOut.trigger('click')
    expect(svg.attributes('style')).toContain('width: 75%')
    expect((zoomOut.element as HTMLButtonElement).disabled).toBe(true)

    const scroll = wrapper.get('.live-map-scroll').element
    scroll.scrollLeft = 80
    scroll.scrollTop = 90
    await overview.trigger('click')
    expect(svg.attributes('style')).toContain('width: 100%')
    expect(scroll.scrollLeft).toBe(0)
    expect(scroll.scrollTop).toBe(0)
  })

  it('组队请求开始即展示等待真实账本，不编造成员', () => {
    const wrapper = mount(AgentTeamLiveGraph, { props: { teams: [], forming: true } })
    expect(wrapper.text()).toContain('正在组建团队')
    expect(wrapper.text()).toContain('等待服务端确认成员')
    expect(wrapper.findAll('[data-member-id]')).toHaveLength(0)
  })
  it('存在历史终态团队时仍明确标记新团队正在组建', () => {
    const previous = team()
    previous.status = 'completed'
    previous.tasks[0]!.status = 'completed'
    const wrapper = mount(AgentTeamLiveGraph, { props: { teams: [previous], forming: true } })

    expect(wrapper.text()).toContain('正在组建新团队')
    expect(wrapper.text()).toContain('新成员以服务端账本确认为准')
    expect(wrapper.text()).toContain('2 个子 Agent')
    expect(wrapper.findAll('[data-member-id]')).toHaveLength(2)
  })
  it('立即展示注册和临时成员，并随任务状态更新', async () => {
    const detail = team()
    const wrapper = mount(AgentTeamLiveGraph, { props: { teams: [detail] } })
    expect(wrapper.text()).toContain('2 个子 Agent')
    expect(wrapper.findAll('[data-member-id]')).toHaveLength(2)
    expect(wrapper.text()).toContain('临时审查员')
    expect(wrapper.text()).toContain('检查输入边界')
    expect(wrapper.text()).toContain('等待派发')
    const updated = { ...detail, tasks: [{ ...detail.tasks[0]!, status: 'running' as const }] }
    await wrapper.setProps({ teams: [updated] })
    expect(wrapper.find('[data-member-id="1"]').attributes('data-status')).toBe('running')
    expect(wrapper.text()).toContain('执行中')
    await wrapper.setProps({ teams: [{ ...updated, status: 'blocked', tasks: [{ ...updated.tasks[0]!, status: 'blocked' }] }] })
    expect(wrapper.find('[data-member-id="1"]').attributes('data-status')).toBe('blocked')
    expect(wrapper.text()).toContain('已阻塞')
  })
  it('团队取消或过期后停止活动展示', () => {
    for (const status of ['cancelled', 'expired'] as const) {
      const detail = team()
      detail.status = status
      detail.tasks[0]!.status = 'running'
      const wrapper = mount(AgentTeamLiveGraph, { props: { teams: [detail] } })
      expect(wrapper.find('[data-member-id="1"]').attributes('data-status')).toBe(status)
      expect(wrapper.find('.is-running').exists()).toBe(false)
    }
  })
  it('团队完成不能将从未派发的成员或失败任务标成完成', () => {
    const detail = team()
    detail.status = 'completed'
    detail.tasks[0]!.status = 'dead_letter'
    const wrapper = mount(AgentTeamLiveGraph, { props: { teams: [detail] } })
    expect(wrapper.find('[data-member-id="1"]').attributes('data-status')).toBe('dead_letter')
    expect(wrapper.find('[data-member-id="2"]').attributes('data-status')).toBe('created')
    expect(wrapper.findAll('.is-completed')).toHaveLength(0)
  })
  it('团队终态下未确认完成的活动任务停止动画并保留未完成语义', () => {
    const detail = team()
    detail.status = 'completed'
    detail.tasks[0]!.status = 'running'
    const wrapper = mount(AgentTeamLiveGraph, { props: { teams: [detail] } })
    expect(wrapper.find('[data-member-id="1"]').attributes('data-status')).toBe('stopped')
    expect(wrapper.text()).toContain('未确认完成')
    expect(wrapper.find('.is-running').exists()).toBe(false)
    expect(wrapper.find('.is-completed').exists()).toBe(false)
  })

})
