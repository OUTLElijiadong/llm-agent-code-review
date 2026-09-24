import { flushPromises, mount } from '@vue/test-utils'
import { beforeEach, expect, it, vi } from 'vitest'
import { nextTick } from 'vue'

import type { DiscussionTurn, WsMessage } from '@/utils/discussionStream'

const streamMock = vi.hoisted(() => ({ subscribe: vi.fn() }))
const discussionApi = vi.hoisted(() => ({ detail: vi.fn() }))
vi.mock('@/utils/discussionStream', () => ({ subscribeDiscussion: streamMock.subscribe }))
vi.mock('@/api/discussion', () => ({ getDiscussionSession: discussionApi.detail }))
vi.mock('vue-router', () => ({ useRouter: () => ({ push: vi.fn() }) }))

import AgentDiscussionPanel from './AgentDiscussionPanel.vue'

let receive: (message: WsMessage) => void
let connected: (status: 'connecting' | 'connected' | 'disconnected' | 'error') => void
let send: ReturnType<typeof vi.fn>

function mountPanel(extraProps: Record<string, unknown> = {}) {
  return mount(AgentDiscussionPanel, {
    attachTo: document.body,
    props: {
      sessionId: 'disc-1', fileName: 'dashboard_routes.py',
      agents: [{ code: 'security', name: '安全审查代理' }, { code: 'performance', name: '性能审查代理' }],
      ...extraProps,
    },
    global: {
      stubs: {
        Teleport: true,
        'el-button': { template: '<button><slot /></button>' },
        'el-tag': { template: '<span><slot /></span>' },
        'el-tooltip': { template: '<div><slot /></div>' },
        PrismLoading: true,
        EmptyState: true,
      },
    },
  })
}

function control(action: string, payload: Record<string, unknown> = {}) {
  receive({ type: 'control', session_id: 'disc-1', action, payload })
}

function turn(turnId: number, agentCode: string, seq = 0): DiscussionTurn {
  return {
    turn_id: turnId, agent_code: agentCode, agent_name: agentCode,
    role: 'agent', content: '已完成本轮检查', action: 'speak', timestamp: `2026-09-24T09:00:${String(turnId).padStart(2, '0')}Z`,
    seq,
  }
}

function followupQuestion(seq: number): DiscussionTurn {
  return {
    turn_id: -1, seq, agent_code: 'user', agent_name: '你', role: 'user',
    content: `追问 ${seq}`, timestamp: `2026-09-24T09:01:${String(seq).padStart(2, '0')}Z`,
  }
}

function followupAnswer(questionSeq: number, seq: number): DiscussionTurn {
  return {
    turn_id: questionSeq, seq, agent_code: 'orchestrator', agent_name: '主持人',
    role: 'agent', round_index: -1, reply_to: 'user', content: `回复 ${questionSeq}`,
    timestamp: `2026-09-24T09:02:${String(seq).padStart(2, '0')}Z`,
  }
}

beforeEach(() => {
  discussionApi.detail.mockReset().mockResolvedValue({ turns: [], has_earlier: false, next_before_seq: null })
  streamMock.subscribe.mockReset()
  streamMock.subscribe.mockImplementation((_id, onMessage, opts) => {
    receive = onMessage
    connected = opts.onStatus
    send = vi.fn().mockReturnValue(true)
    return { send, close: vi.fn() }
  })
})

it('刷新后先显示最近 100 条，按服务端游标加载更早发言且保持顺序', async () => {
  const latest = Array.from({ length: 100 }, (_, index) => turn(index + 51, 'security', index + 51))
  discussionApi.detail.mockResolvedValueOnce({
    turns: Array.from({ length: 50 }, (_, index) => turn(index + 1, 'security', index + 1)),
    has_earlier: false, next_before_seq: null,
  })
  const wrapper = mountPanel({ initialTurns: latest, initialHasEarlier: true, initialNextBeforeSeq: 51 })
  expect(wrapper.findAll('.msg-row').length).toBe(100)
  await wrapper.get('.load-earlier').trigger('click')
  await flushPromises()
  expect(discussionApi.detail).toHaveBeenCalledWith('disc-1', 100, 51)
  expect(wrapper.findAll('.msg-row').length).toBe(150)
  expect((wrapper.vm as unknown as { turns: DiscussionTurn[] }).turns.map((item) => item.seq))
    .toEqual(Array.from({ length: 150 }, (_, index) => index + 1))
  expect(wrapper.find('.load-earlier').exists()).toBe(false)
  wrapper.unmount()
})

it('重放和实时帧按 seq 去重；发现跳号后从 REST 补齐缺失发言', async () => {
  const wrapper = mountPanel({ initialTurns: [turn(1, 'security', 1), turn(2, 'performance', 2)] })
  discussionApi.detail.mockResolvedValueOnce({
    turns: Array.from({ length: 5 }, (_, index) => turn(index + 1, 'security', index + 1)),
    has_earlier: false, next_before_seq: null,
  })
  receive({ type: 'discuss', session_id: 'disc-1', turn: turn(2, 'security', 2) })
  receive({ type: 'discuss', session_id: 'disc-1', turn: turn(5, 'security', 5) })
  await flushPromises()
  expect((wrapper.vm as unknown as { turns: DiscussionTurn[] }).turns.map((item) => item.seq)).toEqual([1, 2, 3, 4, 5])
  expect(discussionApi.detail).toHaveBeenCalled()
  wrapper.unmount()
})

it('断档超过一页时连续翻页补齐，保留全部 250 条有序发言', async () => {
  const wrapper = mountPanel({ initialTurns: [turn(1, 'security', 1), turn(2, 'security', 2)] })
  discussionApi.detail
    .mockResolvedValueOnce({
      turns: Array.from({ length: 100 }, (_, index) => turn(index + 151, 'security', index + 151)),
      has_earlier: true, next_before_seq: 151,
    })
    .mockResolvedValueOnce({
      turns: Array.from({ length: 100 }, (_, index) => turn(index + 51, 'security', index + 51)),
      has_earlier: true, next_before_seq: 51,
    })
    .mockResolvedValueOnce({
      turns: Array.from({ length: 50 }, (_, index) => turn(index + 1, 'security', index + 1)),
      has_earlier: false, next_before_seq: null,
    })
  receive({ type: 'discuss', session_id: 'disc-1', turn: turn(250, 'security', 250) })
  await vi.waitFor(() => {
    const seqs = (wrapper.vm as unknown as { turns: DiscussionTurn[] }).turns.map((item) => item.seq)
    expect(seqs).toEqual(Array.from({ length: 250 }, (_, index) => index + 1))
  })
  expect(discussionApi.detail.mock.calls).toEqual([
    ['disc-1', 100], ['disc-1', 100, 151], ['disc-1', 100, 51],
  ])
  wrapper.unmount()
})

it('连接后的首次同步尚未返回时出现跳号，仍会再同步补缺', async () => {
  let resolveFirst!: (value: unknown) => void
  discussionApi.detail
    .mockImplementationOnce(() => new Promise((resolve) => { resolveFirst = resolve }))
    .mockResolvedValueOnce({
      turns: Array.from({ length: 5 }, (_, index) => turn(index + 1, 'security', index + 1)),
      has_earlier: false, next_before_seq: null,
    })
  const wrapper = mountPanel({ initialTurns: [turn(1, 'security', 1)] })
  connected('connected')
  receive({ type: 'discuss', session_id: 'disc-1', turn: turn(5, 'security', 5) })
  resolveFirst({ turns: [turn(1, 'security', 1)], has_earlier: false, next_before_seq: null })
  await vi.waitFor(() => {
    expect((wrapper.vm as unknown as { turns: DiscussionTurn[] }).turns.map((item) => item.seq))
      .toEqual([1, 2, 3, 4, 5])
  })
  wrapper.unmount()
})

it('终态帧到达但尾部发言被队列丢弃时主动补尾', async () => {
  const wrapper = mountPanel({ initialTurns: [turn(1, 'security', 1)] })
  discussionApi.detail.mockResolvedValueOnce({
    turns: [turn(1, 'security', 1), turn(2, 'performance', 2)],
    has_earlier: false, next_before_seq: null,
  })
  control('done', { status: 'success' })
  await flushPromises()
  expect((wrapper.vm as unknown as { turns: DiscussionTurn[] }).turns.map((item) => item.seq)).toEqual([1, 2])
  wrapper.unmount()
})

it('报告完成后可在固定五分钟窗口追问，到期即禁言但历史仍可见', async () => {
  const deadline = Date.now() / 1000 + 300
  const wrapper = mountPanel({
    initialStatus: 'concluded',
    initialProgress: { phase: 'completed', completed_units: 12, total_units: 12, current_round: 2, seq: 9 },
    initialFollowupUntil: deadline,
    initialTurns: [turn(1, 'security', 1)],
  })
  connected('connected')
  await nextTick()
  expect(wrapper.get('.room-input').attributes('disabled')).toBeUndefined()
  expect(wrapper.text()).toContain('追问剩余')
  await wrapper.get('.room-input').setValue('报告中这条结论的证据是什么？')
  await wrapper.get('.room-input').trigger('keydown', { key: 'Enter' })
  expect(send).toHaveBeenCalledWith('user_input', { content: '报告中这条结论的证据是什么？' })

  ;(wrapper.vm as unknown as { nowSeconds: number }).nowSeconds = deadline
  await nextTick()
  expect(wrapper.get('.room-input').attributes('disabled')).toBeDefined()
  expect(wrapper.text()).toContain('已完成本轮检查')
  wrapper.unmount()
})

it('已落库追问逐条显示待答和不定进度，主持人按原问题 seq 回复后减少计数', async () => {
  const progress = { phase: 'completed', completed_units: 13, total_units: 13,
    current_round: 2, seq: 12, followup_start_seq: 13 }
  const wrapper = mountPanel({
    initialStatus: 'concluded', initialProgress: progress,
    initialFollowupUntil: Date.now() / 1000 + 300,
    initialTurns: [turn(13, 'orchestrator', 13), followupQuestion(14), followupQuestion(15)],
  })
  connected('connected')
  await nextTick()
  expect(wrapper.get('.followup-progress').text()).toContain('2 条待答')
  expect(wrapper.get('.followup-progress [role="progressbar"]').attributes('aria-valuetext'))
    .toBe('2 条追问等待主持人回复')
  expect(wrapper.get('.typing-row').text()).toContain('主持人 · 正在处理 2 条追问')
  expect(wrapper.get('.room-progress').text()).toContain('13 / 13')

  receive({ type: 'discuss', session_id: 'disc-1', turn: followupAnswer(14, 16) })
  await nextTick()
  expect(wrapper.get('.followup-progress').text()).toContain('1 条待答')
  expect(wrapper.get('.room-progress').text()).toContain('13 / 13')

  ;(wrapper.vm as unknown as { nowSeconds: number }).nowSeconds = Date.now() / 1000 + 301
  await nextTick()
  expect(wrapper.get('.room-input').attributes('disabled')).toBeDefined()
  expect(wrapper.get('.followup-progress').text()).toContain('1 条待答')
  receive({ type: 'discuss', session_id: 'disc-1', turn: followupAnswer(15, 17) })
  await nextTick()
  expect(wrapper.find('.followup-progress').exists()).toBe(false)
  expect(wrapper.find('.typing-row').exists()).toBe(false)
  wrapper.unmount()
})

it('刷新与 WS 重连时由已存有序发言恢复追问等待状态，不把普通主持总结误认作追问回复', async () => {
  const deadline = Date.now() / 1000 + 300
  const progress = { phase: 'completed', completed_units: 13, total_units: 13,
    current_round: 2, seq: 12, followup_start_seq: 13 }
  const summary = turn(14, 'orchestrator', 14)
  summary.turn_id = 15
  summary.round_index = 2
  const wrapper = mountPanel({
    initialStatus: 'concluded', initialProgress: progress, initialFollowupUntil: deadline,
    initialTurns: [summary, followupQuestion(15)],
  })
  expect(wrapper.get('.followup-progress').text()).toContain('1 条待答')
  discussionApi.detail.mockResolvedValueOnce({
    status: 'concluded', followup_until: deadline, progress,
    turns: [summary, followupQuestion(15), followupAnswer(15, 16)],
    has_earlier: false, next_before_seq: null,
  })
  connected('connected')
  await flushPromises()
  expect(wrapper.find('.followup-progress').exists()).toBe(false)
  expect((wrapper.vm as unknown as { turns: DiscussionTurn[] }).turns.map((item) => item.seq))
    .toEqual([14, 15, 16])
  wrapper.unmount()
})

it('漏掉 session_end 后，REST 对账也能恢复报告后的待答追问', async () => {
  const deadline = Date.now() / 1000 + 300
  discussionApi.detail.mockResolvedValueOnce({
    status: 'concluded', followup_until: deadline,
    progress: { phase: 'completed', completed_units: 13, total_units: 13,
      current_round: 2, seq: 13, followup_start_seq: 13 },
    turns: [turn(13, 'orchestrator', 13), followupQuestion(14)],
    has_earlier: false, next_before_seq: null,
  })
  const wrapper = mountPanel({ initialStatus: 'active' })
  connected('connected')
  await flushPromises()
  expect(wrapper.get('.followup-progress').text()).toContain('1 条待答')
  expect(wrapper.get('.room-input').attributes('disabled')).toBeUndefined()
  wrapper.unmount()
})

it('刷新时尾页超出追问起点，自动补较早页而不漏掉待答问题', async () => {
  const deadline = Date.now() / 1000 + 300
  const progress = { phase: 'completed', completed_units: 13, total_units: 13,
    current_round: 2, seq: 13, followup_start_seq: 13 }
  const later = Array.from({ length: 100 }, (_, index) => followupAnswer(index + 1000, index + 35))
  const earlier = [followupQuestion(14), ...Array.from({ length: 20 }, (_, index) =>
    followupAnswer(index + 2000, index + 15))]
  discussionApi.detail
    .mockResolvedValueOnce({ status: 'concluded', followup_until: deadline, progress,
      turns: later, has_earlier: true, next_before_seq: 35 })
    .mockResolvedValueOnce({ status: 'concluded', followup_until: deadline, progress,
      turns: earlier, has_earlier: false, next_before_seq: null })
  const wrapper = mountPanel({ initialStatus: 'concluded', initialProgress: progress,
    initialFollowupUntil: deadline, initialTurns: later, initialHasEarlier: true })
  expect(wrapper.get('.followup-progress').text()).toContain('正在核对较早的追问记录')
  connected('connected')
  await flushPromises()
  expect(discussionApi.detail.mock.calls).toEqual([['disc-1', 100], ['disc-1', 100, 35]])
  expect(wrapper.get('.followup-progress').text()).toContain('1 条待答')
  expect(wrapper.get('.followup-progress').text()).not.toContain('至少')
  wrapper.unmount()
})

it('运行时收到结束帧后以服务端期限开启追问，失败终态不可追问', async () => {
  const wrapper = mountPanel()
  connected('connected')
  control('done', { status: 'success' })
  receive({ type: 'session_end', followup_until: Date.now() / 1000 + 300 })
  await nextTick()
  expect(wrapper.get('.room-input').attributes('disabled')).toBeUndefined()
  wrapper.unmount()

  const failed = mountPanel({ initialStatus: 'concluded', initialFollowupUntil: Date.now() / 1000 + 300,
    initialProgress: { phase: 'failed', completed_units: 3, total_units: 12, current_round: 1, seq: 9 } })
  connected('connected')
  await nextTick()
  expect(failed.get('.room-input').attributes('disabled')).toBeDefined()
  failed.unmount()
})

it('漏掉终态 WS 帧后由 REST 对账恢复完成进度、报告和五分钟追问', async () => {
  const deadline = Date.now() / 1000 + 300
  discussionApi.detail.mockResolvedValueOnce({
    status: 'concluded', report_task_id: 184, followup_until: deadline,
    progress: { phase: 'completed', completed_units: 13, total_units: 13, current_round: 2, seq: 12 },
    turns: [turn(1, 'security', 1)], has_earlier: false, next_before_seq: null,
  })
  const wrapper = mountPanel({
    initialStatus: 'active',
    initialProgress: { phase: 'reporting', completed_units: 13, total_units: 13, current_round: 2, seq: 11 },
  })
  connected('connected')
  await flushPromises()
  expect(wrapper.get('.room-progress').text()).toContain('圆桌讨论已完成')
  expect(wrapper.get('.room-input').attributes('disabled')).toBeUndefined()
  expect(wrapper.get('.report-btn').text()).toContain('查看报告')
  wrapper.unmount()
})

it('按真实服务端步骤显示圆桌进度，并区分汇总和报告阶段', async () => {
  const wrapper = mountPanel()
  connected('connected')
  control('progress', { phase: 'speaking', completed_units: 2, total_units: 12, current_round: 1, speaker_code: 'security', seq: 3 })
  await flushPromises()

  const bar = wrapper.get('[role="progressbar"]')
  expect(bar.attributes('aria-valuenow')).toBe('2')
  expect(bar.attributes('aria-valuemax')).toBe('12')
  expect(wrapper.get('.room-progress').text()).toContain('2 / 12')
  expect(wrapper.get('.room-progress').text()).toContain('正在发言')

  control('progress', { phase: 'reporting', completed_units: 11, total_units: 12, current_round: 2, seq: 5 })
  control('progress', { phase: 'speaking', completed_units: 1, total_units: 12, seq: 4 })
  await flushPromises()
  expect(wrapper.get('[role="progressbar"]').attributes('aria-valuenow')).toBe('11')
  expect(wrapper.get('.room-progress').text()).toContain('生成报告')
  expect(wrapper.get('.room-input').attributes('placeholder')).toContain('消息会保存')
  wrapper.unmount()
})

it('旧服务端仅有轮次和发言帧时也按实际完成发言推进，不把主持人总结当成完成发言', async () => {
  const wrapper = mountPanel()
  control('round_start', { round: 1, total_rounds: 2 })
  receive({ type: 'discuss', session_id: 'disc-1', turn: turn(1, 'security') })
  receive({ type: 'discuss', session_id: 'disc-1', turn: turn(1, 'security') })
  receive({ type: 'discuss', session_id: 'disc-1', turn: turn(2, 'orchestrator') })
  await flushPromises()

  expect(wrapper.get('[role="progressbar"]').attributes('aria-valuenow')).toBe('1')
  expect(wrapper.get('[role="progressbar"]').attributes('aria-valuemax')).toBe('4')
  expect(wrapper.get('.room-progress').text()).toContain('发言进度')
  wrapper.unmount()
})

it('发言全完但报告未完成及失败终态都不把进度条画成 100%', async () => {
  const wrapper = mountPanel()
  control('round_start', { round: 2, total_rounds: 2 })
  for (let index = 1; index <= 4; index++) {
    receive({ type: 'discuss', session_id: 'disc-1', turn: turn(index, index % 2 ? 'security' : 'performance') })
  }
  await flushPromises()
  expect(wrapper.get('.room-progress-track > span').attributes('style')).not.toContain('100%')

  control('progress', { phase: 'failed', completed_units: 12, total_units: 12, current_round: 2, seq: 8 })
  control('done', { status: 'failed', error: '报告抽取失败' })
  await flushPromises()
  expect(wrapper.get('.header-sub').text()).toContain('失败')
  expect(wrapper.get('.room-progress-track > span').attributes('style')).not.toContain('100%')
  wrapper.unmount()
})

it('已有报告但一轮发言截断时准确标为部分完成，保持禁言与覆盖门禁', async () => {
  const wrapper = mountPanel({
    initialStatus: 'concluded', initialReportTaskId: 185,
    initialProgress: { phase: 'partial', completed_units: 13, total_units: 13,
      current_round: 2, seq: 12, followup_start_seq: 13 },
    initialTurns: [followupQuestion(14)], initialHasEarlier: true,
  })
  connected('connected')
  await flushPromises()
  expect(wrapper.get('.header-sub').text()).toContain('部分完成')
  expect(wrapper.get('.room-progress').text()).toContain('审查覆盖不完整')
  expect(wrapper.get('.room-progress-track > span').attributes('style')).not.toContain('100%')
  expect(wrapper.get('.room-input').attributes('disabled')).toBeDefined()
  expect(wrapper.find('.followup-progress').exists()).toBe(false)
  expect(wrapper.text()).toContain('查看报告')
  wrapper.unmount()
})

it('手机头部有独立状态和进度区，长文件名受限，操作按钮保留触控面积', async () => {
  const source = (await import('./AgentDiscussionPanel.vue?raw')).default as string
  expect(source).toContain('@media (max-width: 520px)')
  expect(source).toMatch(/\.room-header\s*\{[^}]*display:\s*grid;/s)
  expect(source).toMatch(/\.file-chip\s*\{[^}]*text-overflow:\s*ellipsis;/s)
  expect(source).toMatch(/\.header-actions\s+\.el-button\s*\{[^}]*min-width:\s*40px;/s)
})

it('服务端拒绝消息时保留输入，重连也不清空已确认发言', async () => {
  const wrapper = mountPanel()
  connected('connected')
  receive({ type: 'discuss', session_id: 'disc-1', turn: turn(1, 'security') })
  await flushPromises()
  await wrapper.get('.room-input').setValue('请再核对鉴权边界')
  expect((wrapper.get('.room-input').element as HTMLTextAreaElement).value).toBe('请再核对鉴权边界')
  expect(wrapper.get('.room-input').attributes('disabled')).toBeUndefined()
  expect((wrapper.vm as unknown as { userMessage: string }).userMessage).toBe('请再核对鉴权边界')
  await wrapper.get('.room-input').trigger('keydown', { key: 'Enter' })
  expect(send).toHaveBeenCalledWith('user_input', { content: '请再核对鉴权边界' })

  control('input_rejected', { reason: '圆桌已结束，消息未保存' })
  await flushPromises()
  expect((wrapper.get('.room-input').element as HTMLTextAreaElement).value).toBe('请再核对鉴权边界')
  expect(wrapper.get('.error-bar').text()).toContain('消息未保存')
  await wrapper.get('.error-bar button').trigger('click')
  await flushPromises()
  expect(wrapper.text()).toContain('已完成本轮检查')
  wrapper.unmount()
})
