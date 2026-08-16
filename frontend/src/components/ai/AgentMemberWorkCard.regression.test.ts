/**
 * 历史 bug 回归复现集:锁定本 PR 修复的每一类缺陷。
 *
 * 每个用例名标注对应历史 bug,防回归。验证方式:曾以「还原旧代码→测试变红→恢复→变绿」
 * 逐一确认过这些断言确实能抓住旧缺陷,不是恒真的摆设。
 */
import { mount } from '@vue/test-utils'
import { describe, expect, it, vi } from 'vitest'

import AgentMemberWorkCard from './AgentMemberWorkCard.vue'
import type { AgentTeamMember } from '@/api/agentTeams'

const baseMember: AgentTeamMember = {
  member_id: 1,
  member_key: 'reader',
  display_name: '读取 Agent',
  address: 'agent:project_analyzer',
  kind: 'runtime',
  role: 'worker',
  status: 'queued',
}

describe('历史bug回归:子Agent工作卡片', () => {
  it('【修复】排队成员变运行中后计时器必须启动(旧版只在挂载时启动,计时冻结)', async () => {
    vi.useFakeTimers()
    try {
      const startedAt = new Date(Date.now() - 10_000).toISOString()
      const wrapper = mount(AgentMemberWorkCard, {
        props: { member: { ...baseMember, status: 'queued' } },
      })
      // 排队中:无呼吸点、无计时
      expect(wrapper.find('.member-work-pulse').exists()).toBe(false)
      expect(wrapper.find('.member-work-timing').exists()).toBe(false)

      // 轮询推进:成员转为运行中(带 started_at)
      await wrapper.setProps({ member: { ...baseMember, status: 'running', started_at: startedAt } })
      expect(wrapper.find('.member-work-pulse').exists()).toBe(true)
      const before = wrapper.find('.member-work-timing').text()
      expect(before).toMatch(/已工作/)

      // 旧版缺陷:计时器在 onMounted 才决定启动,状态后变不会启动→文本永不更新
      await vi.advanceTimersByTimeAsync(3000)
      expect(wrapper.find('.member-work-timing').text()).not.toBe(before)

      // 终态:计时器停止,文本定格(卸载路径之外不再泄漏 interval)
      await wrapper.setProps({
        member: {
          ...baseMember,
          status: 'completed',
          started_at: startedAt,
          completed_at: new Date().toISOString(),
        },
      })
      const frozen = wrapper.find('.member-work-timing').text()
      await vi.advanceTimersByTimeAsync(5000)
      expect(wrapper.find('.member-work-timing').text()).toBe(frozen)
    } finally {
      vi.useRealTimers()
    }
  })

  it('【修复】运行中→运行中轮询刷新不重置计时(旧版重启计时器会从 started_at 重算,文本抖动)', async () => {
    vi.useFakeTimers()
    try {
      const startedAt = new Date(Date.now() - 30_000).toISOString()
      const wrapper = mount(AgentMemberWorkCard, {
        props: { member: { ...baseMember, status: 'running', started_at: startedAt } },
      })
      vi.advanceTimersByTime(1000)
      const tick1 = wrapper.find('.member-work-timing').text()
      // 同一成员对象引用变化(status 相同)不应让计时读数回跳
      await wrapper.setProps({
        member: { ...baseMember, status: 'running', started_at: startedAt, display_name: '读取 Agent' },
      })
      await vi.advanceTimersByTimeAsync(1000)
      const tick2 = wrapper.find('.member-work-timing').text()
      expect(tick2 >= tick1).toBe(true)
    } finally {
      vi.useRealTimers()
    }
  })

  it('【修复】事件日志兜底不得泄露长任务列表全量标题(旧版把全部任务标题渲染进卡片)', () => {
    const tasks = Array.from({ length: 15 }, (_, index) => ({
      task_id: index + 1,
      task_key: `task-${index + 1}`,
      member_id: 1,
      member_key: 'reader',
      title: `任务 ${index + 1}`,
      depends_on: [],
      status: 'completed' as const,
      priority: 0,
      attempt_count: 1,
      max_attempts: 3,
    }))
    const wrapper = mount(AgentMemberWorkCard, {
      props: { member: { ...baseMember, status: 'running', started_at: null }, tasks },
    })
    expect(wrapper.text()).toContain('已完成 15 项任务')
    expect(wrapper.text()).not.toContain('任务 1')
    expect(wrapper.text()).not.toContain('任务 15')
  })

  it('【修复】状态文案必须中文(旧版输出 started working / working for Ns / queued)', () => {
    const running = mount(AgentMemberWorkCard, {
      props: {
        member: {
          ...baseMember, status: 'running',
          started_at: new Date(Date.now() - 65_000).toISOString(),
        },
      },
    })
    const text = running.text()
    expect(text).not.toContain('started working')
    expect(text).not.toContain('working for')
    expect(text).not.toContain('queued')
    expect(text).toContain('已工作')
    const queued = mount(AgentMemberWorkCard, {
      props: { member: { ...baseMember, status: 'created' } },
    })
    expect(queued.text()).toContain('排队等待派活')
  })
})

describe('历史bug回归:团队悬浮窗遮挡', () => {
  it('【修复】悬浮窗 z-index 必须盖过聊天抽屉(旧版 1060 被 3000 压住,「查看详情」后整窗不可见)', async () => {
    // jsdom 不解析 scoped CSS,zIndex 计算值恒为 0;直接校验组件源码中的声明值
    const source = (await import('./AgentTeamWindow.vue?raw')).default as string
    const match = source.match(/\.agent-team-window\s*\{[^}]*z-index:\s*([^;]+);/s)
    expect(match).not.toBeNull()
    const declared = match![1].trim()
    const value = Number(declared)
    // 允许令牌形式,但禁止回落到 --z-index-popover(=1060,低于聊天抽屉 3000)
    expect(declared).not.toContain('--z-index-popover')
    if (Number.isFinite(value)) {
      // 聊天抽屉硬编码 z-index: 3000;悬浮窗必须高于它
      expect(value).toBeGreaterThan(3000)
    } else {
      // 令牌引用:解析 variables.scss 确认其数值 > 3000
      const vars = (await import('@/assets/styles/variables.scss?raw')).default as string
      const token = declared.match(/var\((--[\w-]+)/)?.[1]
      const tokenValue = token ? Number(vars.match(new RegExp(`${token}:\\s*(\\d+)`))?.[1]) : NaN
      expect(tokenValue).toBeGreaterThan(3000)
    }
  })
})
