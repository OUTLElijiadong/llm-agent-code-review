import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'

const meshApi = vi.hoisted(() => ({ list: vi.fn() }))
vi.mock('@/api/agentMesh', () => ({ listAgentMeshAgents: meshApi.list }))

import AgentSessionSwitcher from './AgentSessionSwitcher.vue'
import {
  loadAgentChatSessions,
  markAgentChatLoginFreshStart,
  saveActiveAgentChatSession,
  saveAgentChatSnapshot,
} from '@/utils/agentChatSessions'

const WELCOME = '你好,我是小菱!'

function seedIndex(metas: Array<{ id: string; title: string; createdAt: number }>): void {
  window.localStorage.setItem('prism-agent-sessions:user', JSON.stringify(metas))
}

function seedSnapshot(id: string, messages: Array<{ role: 'user' | 'assistant'; content: string }>, runStatus: string | null): void {
  saveAgentChatSnapshot(id, { messages, runStatus, updatedAt: Date.now() })
}

async function mountSwitcher(): Promise<VueWrapper> {
  const wrapper = mount(AgentSessionSwitcher, {
    props: {
      storageKey: 'user',
      legacyKey: 'legacy',
      idPrefix: 'user',
      welcomeText: WELCOME,
    },
    global: {
      stubs: {
        'el-icon': { template: '<span class="el-icon-stub"><slot /></span>' },
        'el-button': { template: '<button><slot /></button>' },
      },
    },
  })
  await flushPromises()
  return wrapper
}

beforeEach(() => {
  window.localStorage.clear()
  window.sessionStorage.clear()
  meshApi.list.mockReset().mockResolvedValue({ items: [], total: 0, by_kind: {} })
})

function lastSelect(wrapper: VueWrapper): string | undefined {
  const events = wrapper.emitted('select')
  if (!events || !events.length) return undefined
  return events[events.length - 1][0] as string
}

describe('AgentSessionSwitcher 登录周期选择权', () => {
  for (const surface of ['user', 'admin'] as const) {
    it(`${surface} 成功登录后首次挂载新建一次,同次登录重挂载继续当前会话`, async () => {
      window.localStorage.setItem(`prism-agent-sessions:${surface}`, JSON.stringify([
        { id: `${surface}-default`, title: '默认对话', createdAt: 2 },
        { id: `${surface}-running`, title: '后台运行', createdAt: 1 },
      ]))
      saveActiveAgentChatSession(surface, `${surface}-default`)
      seedSnapshot(`${surface}-running`, [], 'running')
      markAgentChatLoginFreshStart()

      const first = mount(AgentSessionSwitcher, {
        props: {
          storageKey: surface,
          legacyKey: `legacy-${surface}`,
          idPrefix: surface,
          welcomeText: WELCOME,
        },
      })
      await flushPromises()
      const freshId = lastSelect(first)
      expect(freshId?.startsWith(`${surface}-`)).toBe(true)
      expect(freshId).not.toBe(`${surface}-default`)
      expect(freshId).not.toBe(`${surface}-running`)
      expect(loadAgentChatSessions(surface, `legacy-${surface}`, surface)).toHaveLength(3)
      first.unmount()

      const reopened = mount(AgentSessionSwitcher, {
        props: {
          storageKey: surface,
          legacyKey: `legacy-${surface}`,
          idPrefix: surface,
          welcomeText: WELCOME,
        },
      })
      await flushPromises()
      expect(lastSelect(reopened)).toBe(freshId)
      expect(loadAgentChatSessions(surface, `legacy-${surface}`, surface)).toHaveLength(3)
      reopened.unmount()
    })
  }

  it('目录刷新遇到最近且运行中的历史会话也保持用户新建的当前会话', async () => {
    seedIndex([
      { id: 'user-new', title: '新对话', createdAt: 5 },
      { id: 'user-default', title: '默认对话', createdAt: 4 },
    ])
    saveActiveAgentChatSession('user', 'user-new')
    meshApi.list.mockResolvedValue({
      items: [
        { kind: 'session', session_id: 'user-new', surface: 'user', name: '新对话', last_seen_at: '2026-08-13T00:00:00Z', active_run_status: 'completed' },
        { kind: 'session', session_id: 'user-default', surface: 'user', name: '默认对话', last_seen_at: new Date().toISOString(), active_run_status: 'running' },
      ],
      total: 2,
      by_kind: { session: 2 },
    })
    const wrapper = mount(AgentSessionSwitcher, {
      props: { storageKey: 'user', legacyKey: 'legacy', idPrefix: 'user', welcomeText: WELCOME, discoverRemote: true },
    })
    await flushPromises()
    expect(lastSelect(wrapper)).toBe('user-new')

    await (wrapper.vm as unknown as { refreshFromAgentMesh(): Promise<void> }).refreshFromAgentMesh()
    expect(lastSelect(wrapper)).toBe('user-new')
    wrapper.unmount()
  })

  it('用户明确点击历史会话后允许切换,后续目录刷新保持该选择', async () => {
    seedIndex([
      { id: 'user-new', title: '新对话', createdAt: 5 },
      { id: 'user-history', title: '历史审计', createdAt: 4 },
    ])
    saveActiveAgentChatSession('user', 'user-new')
    meshApi.list.mockResolvedValue({
      items: [
        { kind: 'session', session_id: 'user-new', surface: 'user', name: '新对话', last_seen_at: new Date().toISOString(), active_run_status: 'completed' },
        { kind: 'session', session_id: 'user-history', surface: 'user', name: '历史审计', last_seen_at: '2026-08-13T00:00:00Z', active_run_status: 'completed' },
      ],
      total: 2,
      by_kind: { session: 2 },
    })
    const wrapper = mount(AgentSessionSwitcher, {
      props: { storageKey: 'user', legacyKey: 'legacy', idPrefix: 'user', welcomeText: WELCOME, discoverRemote: true },
    })
    await flushPromises()
    await wrapper.find('.session-current').trigger('click')
    const history = wrapper.findAll('.session-item').find((item) => item.text().includes('历史审计'))
    await history?.trigger('click')
    expect(lastSelect(wrapper)).toBe('user-history')

    await (wrapper.vm as unknown as { refreshFromAgentMesh(): Promise<void> }).refreshFromAgentMesh()
    expect(lastSelect(wrapper)).toBe('user-history')
    wrapper.unmount()
  })
})

describe('AgentSessionSwitcher.ensureFreshOnOpen', () => {
  it('重新挂载时恢复最后查看的历史会话', async () => {
    seedIndex([
      { id: 'user-default', title: '默认对话', createdAt: 2 },
      { id: 'user-history', title: '历史团队审计', createdAt: 1 },
    ])
    seedSnapshot('user-history', [{ role: 'assistant', content: '历史结论' }], 'completed')
    saveActiveAgentChatSession('user', 'user-history')
    const wrapper = await mountSwitcher()
    expect(lastSelect(wrapper)).toBe('user-history')
  })

  it('历史存在未完成会话且当前是空对话时,仍保持当前会话', async () => {
    seedIndex([
      { id: 'user-s2', title: '新对话', createdAt: 2 },
      { id: 'user-s1', title: '等待输入', createdAt: 1 },
    ])
    seedSnapshot('user-s1', [], 'waiting_input')
    const wrapper = await mountSwitcher()
    ;(wrapper.vm as unknown as { ensureFreshOnOpen(): void }).ensureFreshOnOpen()
    expect(lastSelect(wrapper)).toBe('user-s2')
  })

  it('没有登录新建标记时,重开非占位历史会话不会擅自新建', async () => {
    seedIndex([{ id: 'user-stale', title: '小菱生产验收发…', createdAt: 1 }])
    const wrapper = await mountSwitcher()
    ;(wrapper.vm as unknown as { ensureFreshOnOpen(): void }).ensureFreshOnOpen()
    expect(lastSelect(wrapper)).toBe('user-stale')
    expect(loadAgentChatSessions('user', 'legacy', 'user')).toHaveLength(1)
  })

  it('历史都已完成且当前是空的新对话时,保留当前不新建', async () => {
    seedIndex([
      { id: 'user-fresh', title: '新对话', createdAt: 2 },
      { id: 'user-done', title: '完成对话', createdAt: 1 },
    ])
    seedSnapshot('user-done', [{ role: 'user', content: '查询项目' }], 'completed')
    const wrapper = await mountSwitcher()
    const before = wrapper.emitted('select')?.length ?? 0
    ;(wrapper.vm as unknown as { ensureFreshOnOpen(): void }).ensureFreshOnOpen()
    // 没有新的 select(保留当前空对话)
    expect((wrapper.emitted('select')?.length ?? 0)).toBe(before)
    expect(lastSelect(wrapper)).toBe('user-fresh')
  })

  it('历史都已完成且存在其他空会话时,仍保持当前完成对话', async () => {
    seedIndex([
      { id: 'user-done', title: '完成对话', createdAt: 1 },
      { id: 'user-fresh', title: '新对话', createdAt: 2 },
    ])
    seedSnapshot('user-done', [{ role: 'user', content: '查询项目' }], 'completed')
    const wrapper = await mountSwitcher()
    ;(wrapper.vm as unknown as { ensureFreshOnOpen(): void }).ensureFreshOnOpen()
    expect(lastSelect(wrapper)).toBe('user-done')
  })

  it('存在默认对话时也不从当前完成对话自动切换', async () => {
    seedIndex([
      { id: 'user-done', title: '完成对话', createdAt: 1 },
      { id: 'user-default', title: '默认对话', createdAt: 2 },
    ])
    seedSnapshot('user-done', [{ role: 'user', content: '查询项目' }], 'completed')
    const wrapper = await mountSwitcher()
    ;(wrapper.vm as unknown as { ensureFreshOnOpen(): void }).ensureFreshOnOpen()
    expect(lastSelect(wrapper)).toBe('user-done')
  })

  it('存在只有欢迎语的空会话时也不自动切换', async () => {
    seedIndex([
      { id: 'user-done', title: '完成对话', createdAt: 1 },
      { id: 'user-titled-welcome', title: '已命名空会话', createdAt: 2 },
    ])
    seedSnapshot('user-done', [{ role: 'user', content: '查询项目' }], 'completed')
    seedSnapshot('user-titled-welcome', [{ role: 'assistant', content: WELCOME }], 'completed')
    const wrapper = await mountSwitcher()
    ;(wrapper.vm as unknown as { ensureFreshOnOpen(): void }).ensureFreshOnOpen()
    expect(lastSelect(wrapper)).toBe('user-done')
  })

  it('没有登录新建标记时,历史完成也不自动创建空对话', async () => {
    seedIndex([{ id: 'user-done', title: '完成对话', createdAt: 1 }])
    seedSnapshot('user-done', [{ role: 'user', content: '查询项目' }], 'completed')
    const wrapper = await mountSwitcher()
    ;(wrapper.vm as unknown as { ensureFreshOnOpen(): void }).ensureFreshOnOpen()
    expect(lastSelect(wrapper)).toBe('user-done')
    expect(loadAgentChatSessions('user', 'legacy', 'user')).toHaveLength(1)
  })
})

describe('AgentSessionSwitcher Agent Mesh discovery', () => {
  it('只合并当前 surface 的服务端会话并保留本地标题', async () => {
    seedIndex([
      { id: 'user-local', title: '本地命名', createdAt: 10 },
      { id: 'user-stale', title: '不应继续显示', createdAt: 9 },
    ])
    meshApi.list.mockResolvedValue({
      items: [
        { kind: 'session', session_id: 'user-local', surface: 'user', name: '服务端名称', last_seen_at: '2026-08-12T00:00:00Z' },
        { kind: 'session', session_id: 'user-remote', surface: 'user', name: '另一会话', last_seen_at: '2026-08-12T00:01:00Z' },
        { kind: 'session', session_id: 'admin-remote', surface: 'admin', name: '管理会话', last_seen_at: '2026-08-12T00:02:00Z' },
        { kind: 'runtime', session_id: '', surface: 'user', name: '运行时 Agent', last_seen_at: '' },
      ],
      total: 4,
      by_kind: { session: 3, runtime: 1 },
    })
    const wrapper = await mount(AgentSessionSwitcher, {
      props: { storageKey: 'user', legacyKey: 'legacy', idPrefix: 'user', discoverRemote: true },
    })
    await flushPromises()
    await wrapper.find('.session-current').trigger('click')

    expect(wrapper.findAll('.session-item')).toHaveLength(2)
    expect(wrapper.text()).toContain('本地命名')
    expect(wrapper.text()).toContain('另一会话')
    expect(wrapper.text()).not.toContain('不应继续显示')
    expect(wrapper.text()).not.toContain('管理会话')
    expect(meshApi.list).toHaveBeenCalledOnce()
    wrapper.unmount()
  })

  it('管理端只纳入 admin 会话', async () => {
    meshApi.list.mockResolvedValue({
      items: [
        { kind: 'session', session_id: 'user-remote', surface: 'user', name: '用户会话', last_seen_at: '' },
        { kind: 'session', session_id: 'admin-remote', surface: 'admin', name: '管理会话', last_seen_at: '' },
      ],
      total: 2,
      by_kind: { session: 2 },
    })
    const wrapper = await mount(AgentSessionSwitcher, {
      props: { storageKey: 'admin', legacyKey: 'legacy-admin', idPrefix: 'admin', discoverRemote: true },
    })
    await flushPromises()
    await wrapper.find('.session-current').trigger('click')

    expect(wrapper.text()).toContain('管理会话')
    expect(wrapper.text()).not.toContain('用户会话')
    wrapper.unmount()
  })
})

describe('AgentSessionSwitcher authoritative remote busy state', () => {
  it('服务端说已完成时会更新忙碌判断,但不会自动新建或切换', async () => {
    seedIndex([{ id: 'user-stale-busy', title: '旧任务', createdAt: 1 }])
    seedSnapshot('user-stale-busy', [{ role: 'user', content: '旧任务内容' }], 'waiting_input')
    meshApi.list.mockResolvedValue({
      items: [
        {
          kind: 'session',
          session_id: 'user-stale-busy',
          surface: 'user',
          name: '旧任务',
          last_seen_at: '2026-08-13T00:00:00Z',
          active_run_status: 'completed',
        },
      ],
      total: 1,
      by_kind: { session: 1 },
    })
    const wrapper = await mount(AgentSessionSwitcher, {
      props: { storageKey: 'user', legacyKey: 'legacy', idPrefix: 'user', discoverRemote: true },
    })
    await flushPromises()
    expect(lastSelect(wrapper)).toBe('user-stale-busy')
  })

  it('服务端说 running 时保持已选中的同一会话', async () => {
    seedIndex([{ id: 'user-running', title: '运行中任务', createdAt: 1 }])
    meshApi.list.mockResolvedValue({
      items: [
        {
          kind: 'session',
          session_id: 'user-running',
          surface: 'user',
          name: '运行中任务',
          last_seen_at: '2026-08-13T00:00:00Z',
          active_run_status: 'running',
        },
      ],
      total: 1,
      by_kind: { session: 1 },
    })
    const wrapper = await mount(AgentSessionSwitcher, {
      props: { storageKey: 'user', legacyKey: 'legacy', idPrefix: 'user', discoverRemote: true },
    })
    await flushPromises()
    expect(lastSelect(wrapper)).toBe('user-running')
  })

  it('当前停在空会话时,最近活跃的服务端会话也不能抢焦点', async () => {
    seedIndex([{ id: 'user-new', title: '新对话', createdAt: 5 }])
    const freshTs = new Date().toISOString()
    meshApi.list.mockResolvedValue({
      items: [
        { kind: 'session', session_id: 'user-new', surface: 'user', name: '新对话', last_seen_at: '2026-08-13T00:00:00Z', active_run_status: 'completed' },
        { kind: 'session', session_id: 'user-lively', surface: 'user', name: '最新活跃对话', last_seen_at: freshTs, active_run_status: 'running' },
      ],
      total: 2,
      by_kind: { session: 2 },
    })
    const wrapper = await mount(AgentSessionSwitcher, {
      props: { storageKey: 'user', legacyKey: 'legacy', idPrefix: 'user', welcomeText: WELCOME, discoverRemote: true },
    })
    await flushPromises()
    expect(lastSelect(wrapper)).toBe('user-new')
    wrapper.unmount()
  })

  it('当前会话有内容或忙碌时,不自动聚焦到其他会话', async () => {
    // 当前会话有真实内容(非空),不应被「刚活跃的新对话」顶掉。
    // user-busy 是本地第一项(最近使用),discovery 也发现了它。
    // user-lively 仅「刚活跃」(recent),非 running——否则会被 step1 的忙碌跳回逻辑接管。
    seedIndex([
      { id: 'user-busy', title: '进行中的工作', createdAt: 5 },
      { id: 'user-lively', title: '最新活跃对话', createdAt: 4 },
    ])
    seedSnapshot('user-busy', [{ role: 'user', content: '帮我审查这个项目' }], 'completed')
    const freshTs = new Date().toISOString()
    meshApi.list.mockResolvedValue({
      items: [
        { kind: 'session', session_id: 'user-busy', surface: 'user', name: '进行中的工作', last_seen_at: '2026-08-13T00:00:00Z', active_run_status: 'completed' },
        { kind: 'session', session_id: 'user-lively', surface: 'user', name: '最新活跃对话', last_seen_at: freshTs, active_run_status: 'completed' },
      ],
      total: 2,
      by_kind: { session: 2 },
    })
    const wrapper = await mount(AgentSessionSwitcher, {
      props: { storageKey: 'user', legacyKey: 'legacy', idPrefix: 'user', welcomeText: WELCOME, discoverRemote: true },
    })
    await flushPromises()
    expect(lastSelect(wrapper)).not.toBe('user-lively')
    wrapper.unmount()
  })
})

describe('AgentSessionSwitcher remote empty run state', () => {
  it('服务端无运行记录时更新忙碌判断,但保持当前会话', async () => {
    seedIndex([{ id: 'user-empty-run', title: '无运行记录旧会话', createdAt: 1 }])
    seedSnapshot('user-empty-run', [{ role: 'user', content: '旧内容' }], 'waiting_input')
    meshApi.list.mockResolvedValue({
      items: [
        {
          kind: 'session',
          session_id: 'user-empty-run',
          surface: 'user',
          name: '无运行记录旧会话',
          last_seen_at: '2026-08-13T00:00:00Z',
          active_run_status: '',
        },
      ],
      total: 1,
      by_kind: { session: 1 },
    })
    const wrapper = await mount(AgentSessionSwitcher, {
      props: { storageKey: 'user', legacyKey: 'legacy', idPrefix: 'user', discoverRemote: true },
    })
    await flushPromises()
    expect(lastSelect(wrapper)).toBe('user-empty-run')
  })
})

describe('AgentSessionSwitcher 会话管理(搜索/置顶/删除确认)', () => {
  it('搜索框按标题过滤会话,清空后恢复全部', async () => {
    seedIndex([
      { id: 'user-a', title: '项目审查', createdAt: 3 },
      { id: 'user-b', title: '知识库问答', createdAt: 2 },
      { id: 'user-c', title: '新对话', createdAt: 1 },
    ])
    const wrapper = await mountSwitcher()
    await wrapper.find('.session-current').trigger('click')

    expect(wrapper.findAll('.session-item')).toHaveLength(3)
    await wrapper.find('.session-search').setValue('知识库')
    expect(wrapper.findAll('.session-item')).toHaveLength(1)
    expect(wrapper.findAll('.session-item-name').map((item) => item.text())).toEqual(['知识库问答'])

    await wrapper.find('.session-search').setValue('')
    expect(wrapper.findAll('.session-item')).toHaveLength(3)
  })

  it('置顶会话排在最前并持久化到本地索引', async () => {
    seedIndex([
      { id: 'user-a', title: '项目审查', createdAt: 3 },
      { id: 'user-b', title: '知识库问答', createdAt: 2 },
      { id: 'user-c', title: '新对话', createdAt: 1 },
    ])
    const wrapper = await mountSwitcher()
    await wrapper.find('.session-current').trigger('click')

    await wrapper.findAll('.session-pin')[1].trigger('click')
    const names = wrapper.findAll('.session-item-name').map((item) => item.text())
    expect(names[0]).toBe('知识库问答')

    const stored = loadAgentChatSessions('user', 'legacy', 'user')
    expect(stored.find((item) => item.id === 'user-b')?.pinned).toBe(true)
  })

  it('删除需要二次确认,确认后才真正移除', async () => {
    seedIndex([
      { id: 'user-a', title: '项目审查', createdAt: 3 },
      { id: 'user-b', title: '知识库问答', createdAt: 2 },
      { id: 'user-c', title: '新对话', createdAt: 1 },
    ])
    const wrapper = await mountSwitcher()
    await wrapper.find('.session-current').trigger('click')

    await wrapper.findAll('.session-delete')[1].trigger('click')
    expect(wrapper.find('.session-confirm').exists()).toBe(true)
    expect(loadAgentChatSessions('user', 'legacy', 'user')).toHaveLength(3)

    await wrapper.find('.session-confirm-yes').trigger('click')
    expect(wrapper.emitted('archive')).toEqual([['user-b']])
    // 服务端归档成功前只进入归档中状态,不本地移除;由父组件确认成功后调用 removeSession。
    expect(loadAgentChatSessions('user', 'legacy', 'user')).toHaveLength(3)
    ;(wrapper.vm as unknown as { removeSession(id: string): void }).removeSession('user-b')
    const remaining = loadAgentChatSessions('user', 'legacy', 'user')
    expect(remaining.map((item) => item.id)).not.toContain('user-b')

    // 取消路径不删除
    await wrapper.findAll('.session-delete')[0].trigger('click')
    await wrapper.find('.session-confirm-no').trigger('click')
    expect(loadAgentChatSessions('user', 'legacy', 'user')).toHaveLength(2)
  })

  it('运行中的会话不显示删除入口,显示锁标记', async () => {
    seedIndex([
      { id: 'user-a', title: '项目审查', createdAt: 2 },
      { id: 'user-b', title: '运行中任务', createdAt: 1 },
    ])
    seedSnapshot('user-b', [{ role: 'user', content: '开始审查' }], 'running')
    const wrapper = await mountSwitcher()
    await wrapper.find('.session-current').trigger('click')

    expect(wrapper.findAll('.session-lock')).toHaveLength(1)
    const busyItem = wrapper.findAll('.session-item').find((item) => item.text().includes('运行中任务'))
    expect(busyItem?.find('.session-delete').exists()).toBe(false)
  })
})
