import { mount, flushPromises } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import type { AgentTeamDetail, AgentTeamMessage, AgentTeamTask } from '@/api/agentTeams'
import AgentTeamWindow from './AgentTeamWindow.vue'

const global = { stubs: { Teleport: true, Transition: false, 'el-icon': true } }

function message(messageId: string, payload: Record<string, unknown>): AgentTeamMessage {
  return {
    message_id: messageId,
    trace_id: `trace-${messageId}`,
    correlation_id: `correlation-${messageId}`,
    sent_from: 'agent:code_reviewer',
    send_to: 'manager',
    message_type: 'task.result',
    subject: '审查结果',
    status: 'completed',
    payload,
    create_time: '2026-10-01T12:00:00Z',
  }
}

function taskWithLongError(): AgentTeamTask {
  return {
    task_id: 7,
    task_key: 'audit-error',
    member_id: 1,
    member_key: 'code_reviewer',
    title: '错误证据样本',
    depends_on: [],
    status: 'failed',
    priority: 1,
    attempt_count: 1,
    max_attempts: 2,
    errors: [{
      type: 'verification',
      details: `${'中文错误证据'.repeat(100)}ERROR_EVIDENCE_TAIL_最后一行`,
    }],
  }
}

function makeTeam(messages: AgentTeamMessage[], tasks: AgentTeamTask[] = []): AgentTeamDetail {
  return {
    team_id: 82,
    title: '全文展开测试团队',
    surface: 'user',
    session_id: 'full-payload-session',
    status: 'completed',
    max_active_children: 2,
    trace_id: 'full-payload-trace',
    created_at: '2026-10-01T12:00:00Z',
    members: [],
    tasks,
    counts: { total: tasks.length, completed: 0, running: 0, queued: 0, failed: tasks.length, blocked: 0 },
    events: [],
    messages,
  }
}

describe('AgentTeamWindow 完整协作内容', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
  })

  it('长中文 payload 默认折叠，能展开到尾部、复制全文并再次收起', async () => {
    const tail = 'TAIL_EVIDENCE_账号隔离与最后一个复现步骤'
    const fullText = `${'中文审计事实与复现过程。'.repeat(35)}${tail}`
    const wrapper = mount(AgentTeamWindow, {
      props: { visible: true, team: makeTeam([message('msg-cn', { report: fullText })]) },
      global: { ...global, plugins: [createPinia()] },
    })

    const row = wrapper.get('.team-chat-row')
    const toggle = row.get('.team-chat-payload-toggle')
    expect(toggle.attributes('aria-expanded')).toBe('false')
    expect(row.get('.team-chat-payload-preview').text()).toContain('…')
    expect(row.find('.team-chat-payload-full').exists()).toBe(false)

    await toggle.trigger('click')
    expect(row.get('.team-chat-payload-full').text()).toContain(tail)
    expect(row.get('.team-chat-payload-toggle').attributes('aria-expanded')).toBe('true')

    const writeText = vi.fn().mockResolvedValue(undefined)
    Object.defineProperty(navigator, 'clipboard', {
      configurable: true,
      value: { writeText },
    })
    await row.get('.team-chat-payload-copy').trigger('click')
    await flushPromises()
    expect(writeText).toHaveBeenCalledWith(JSON.stringify({ report: fullText }, null, 2))

    await row.get('.team-chat-payload-toggle').trigger('click')
    expect(row.find('.team-chat-payload-full').exists()).toBe(false)
    expect(row.get('.team-chat-payload-toggle').attributes('aria-expanded')).toBe('false')
    wrapper.unmount()
  })

  it('原始170字符截断样本在展开后能看到尾部复现依据', async () => {
    const tail = 'TAIL_EVIDENCE_关键复现依据'
    const wrapper = mount(AgentTeamWindow, {
      props: {
        visible: true,
        team: makeTeam([message('msg-original-f06', { report: `${'x'.repeat(170)}${tail}` })]),
      },
      global: { ...global, plugins: [createPinia()] },
    })
    const row = wrapper.get('.team-chat-row')
    expect(row.get('.team-chat-payload-preview').text()).toContain('…')
    expect(row.get('.team-chat-payload-preview').text()).not.toContain(tail)
    await row.get('.team-chat-payload-toggle').trigger('click')
    expect(row.get('.team-chat-payload-full').text()).toContain(tail)
    wrapper.unmount()
  })

  it('长 JSON 展开保留末尾键值，未展开的其他消息仍独立折叠', async () => {
    const payload = {
      findings: Array.from({ length: 28 }, (_, index) => ({
        id: index + 1,
        title: `中文问题 ${index + 1}`,
        evidence: '长证据'.repeat(12),
      })),
      final_marker: 'JSON_TAIL_保留完整结构',
    }
    const wrapper = mount(AgentTeamWindow, {
      props: {
        visible: true,
        team: makeTeam([message('msg-json', payload), message('msg-short', { status: 'ok' })]),
      },
      global: { ...global, plugins: [createPinia()] },
    })
    const [longRow, shortRow] = wrapper.findAll('.team-chat-row')

    expect(longRow.get('.team-chat-payload-toggle').attributes('aria-expanded')).toBe('false')
    expect(shortRow.find('.team-chat-payload-toggle').exists()).toBe(false)
    await longRow.get('.team-chat-payload-toggle').trigger('click')
    expect(longRow.get('.team-chat-payload-full').text()).toContain('JSON_TAIL_保留完整结构')
    expect(longRow.get('.team-chat-payload-full').text()).toContain('"findings"')
    expect(shortRow.find('.team-chat-payload-full').exists()).toBe(false)
    wrapper.unmount()
  })

  it('复制接口不可用时自动展开全文，便于手动复制', async () => {
    const tail = 'COPY_FALLBACK_TAIL_完整报告尾部'
    const fullText = `${'复制降级样本'.repeat(40)}${tail}`
    Object.defineProperty(navigator, 'clipboard', {
      configurable: true,
      value: { writeText: vi.fn().mockRejectedValue(new Error('permission denied')) },
    })
    const wrapper = mount(AgentTeamWindow, {
      props: { visible: true, team: makeTeam([message('msg-copy-fallback', { report: fullText })]) },
      global: { ...global, plugins: [createPinia()] },
    })
    const row = wrapper.get('.team-chat-row')
    await row.get('.team-chat-payload-copy').trigger('click')
    await flushPromises()

    expect(row.get('.team-chat-payload-toggle').attributes('aria-expanded')).toBe('true')
    expect(row.get('.team-chat-payload-full').text()).toContain(tail)
    wrapper.unmount()
  })

  it('切换团队会清除上一会话的展开状态', async () => {
    const longText = `${'跨会话状态'.repeat(40)}SESSION_TAIL`
    const wrapper = mount(AgentTeamWindow, {
      props: { visible: true, team: makeTeam([message('reused-id', { report: longText })]) },
      global: { ...global, plugins: [createPinia()] },
    })
    await wrapper.get('.team-chat-payload-toggle').trigger('click')
    expect(wrapper.find('.team-chat-payload-full').exists()).toBe(true)

    await wrapper.setProps({
      team: {
        ...makeTeam([message('reused-id', { report: longText })]),
        team_id: 83,
        session_id: 'different-account-session',
      },
    })
    expect(wrapper.get('.team-chat-payload-toggle').attributes('aria-expanded')).toBe('false')
    expect(wrapper.find('.team-chat-payload-preview').exists()).toBe(true)
    wrapper.unmount()
  })

  it('同团队轮询更新数据时保留已展开状态，不要求重复展开全文', async () => {
    const original = makeTeam([message('same-team-message', { report: '完整证据'.repeat(50) })])
    const wrapper = mount(AgentTeamWindow, { props: { visible: true, team: original }, global: { ...global, plugins: [createPinia()] } })
    await wrapper.get('.team-chat-payload-toggle').trigger('click')
    await wrapper.setProps({ team: { ...original, updated_at: '2026-10-04T12:00:02Z' } })
    expect(wrapper.get('.team-chat-payload-toggle').attributes('aria-expanded')).toBe('true')
    wrapper.unmount()
  })

  it('复制失败在异步返回前切换团队时，不展开新会话复用的消息ID', async () => {
    let rejectCopy!: (reason?: unknown) => void
    Object.defineProperty(navigator, 'clipboard', {
      configurable: true,
      value: {
        writeText: vi.fn(() => new Promise<void>((_resolve, reject) => { rejectCopy = reject })),
      },
    })
    const longText = `${'待复制内容'.repeat(40)}OLD_SESSION_TAIL`
    const wrapper = mount(AgentTeamWindow, {
      props: { visible: true, team: makeTeam([message('shared-message-id', { report: longText })]) },
      global: { ...global, plugins: [createPinia()] },
    })
    await wrapper.get('.team-chat-payload-copy').trigger('click')
    await wrapper.setProps({
      team: {
        ...makeTeam([message('shared-message-id', { report: longText })]),
        team_id: 84,
        session_id: 'new-session-after-pending-copy',
      },
    })
    rejectCopy(new Error('clipboard permission denied'))
    await flushPromises()

    expect(wrapper.get('.team-chat-payload-toggle').attributes('aria-expanded')).toBe('false')
    expect(wrapper.find('.team-chat-payload-full').exists()).toBe(false)
    wrapper.unmount()
  })

  it('复制回调等待期间离开再回到原团队，也不能重新应用旧回调', async () => {
    let rejectCopy!: (reason?: unknown) => void
    Object.defineProperty(navigator, 'clipboard', { configurable: true, value: {
      writeText: vi.fn(() => new Promise<void>((_resolve, reject) => { rejectCopy = reject })),
    } })
    const original = makeTeam([message('reused-message', { report: '原团队长报告'.repeat(50) })])
    const wrapper = mount(AgentTeamWindow, { props: { visible: true, team: original }, global: { ...global, plugins: [createPinia()] } })
    await wrapper.get('.team-chat-payload-copy').trigger('click')
    await wrapper.setProps({ team: { ...original, team_id: 99 } })
    await wrapper.setProps({ team: original })
    rejectCopy(new Error('late clipboard rejection'))
    await flushPromises()
    expect(wrapper.get('.team-chat-payload-toggle').attributes('aria-expanded')).toBe('false')
    wrapper.unmount()
  })

  it('错误证据保持完整，窄屏样式具有换行与独立滚动约束', async () => {
    const wrapper = mount(AgentTeamWindow, {
      props: { visible: true, team: makeTeam([], [taskWithLongError()]) },
      global: { ...global, plugins: [createPinia()] },
    })
    const errorDetails = wrapper.get('.team-window-task-error')
    await errorDetails.get('summary').trigger('click')
    expect((errorDetails.element as HTMLDetailsElement).open).toBe(true)
    expect(errorDetails.get('code').text()).toContain('ERROR_EVIDENCE_TAIL_最后一行')
    expect(errorDetails.get('code').text()).toContain('中文错误证据')

    const component = await import('./AgentTeamWindow.vue?raw')
    expect(component.default).toContain('@media (max-width: 520px)')
    expect(component.default).toContain('inset: auto 12px 12px 12px !important; width: auto')
    expect(component.default).toContain('overflow-wrap: anywhere')
    expect(component.default).toContain('overflow-y: auto')
    wrapper.unmount()
  })
})
