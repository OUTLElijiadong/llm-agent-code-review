import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'

const meshApi = vi.hoisted(() => ({ list: vi.fn() }))
vi.mock('@/api/agentMesh', () => ({ listAgentMeshAgents: meshApi.list }))

import AgentSessionSwitcher from './AgentSessionSwitcher.vue'
import { saveAgentChatSnapshot } from '@/utils/agentChatSessions'

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
  meshApi.list.mockReset().mockResolvedValue({ items: [], total: 0, by_kind: {} })
})

function lastSelect(wrapper: VueWrapper): string | undefined {
  const events = wrapper.emitted('select')
  if (!events || !events.length) return undefined
  return events[events.length - 1][0] as string
}

describe('AgentSessionSwitcher.ensureFreshOnOpen', () => {
  it('历史存在未完成会话且当前是空对话时,跳转到未完成会话', async () => {
    // sessions 顺序:s2(空对话,active) -> s1(未完成 waiting_input)
    seedIndex([
      { id: 'user-s2', title: '新对话', createdAt: 2 },
      { id: 'user-s1', title: '等待输入', createdAt: 1 },
    ])
    seedSnapshot('user-s1', [], 'waiting_input')
    const wrapper = await mountSwitcher()
    ;(wrapper.vm as unknown as { ensureFreshOnOpen(): void }).ensureFreshOnOpen()
    expect(lastSelect(wrapper)).toBe('user-s1')
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

  it('历史都已完成且当前是完成对话、存在空会话时,复用到空会话', async () => {
    seedIndex([
      { id: 'user-done', title: '完成对话', createdAt: 1 },
      { id: 'user-fresh', title: '新对话', createdAt: 2 },
    ])
    seedSnapshot('user-done', [{ role: 'user', content: '查询项目' }], 'completed')
    const wrapper = await mountSwitcher()
    ;(wrapper.vm as unknown as { ensureFreshOnOpen(): void }).ensureFreshOnOpen()
    expect(lastSelect(wrapper)).toBe('user-fresh')
  })

  it('历史都已完成且无空会话时,新建空对话', async () => {
    seedIndex([{ id: 'user-done', title: '完成对话', createdAt: 1 }])
    seedSnapshot('user-done', [{ role: 'user', content: '查询项目' }], 'completed')
    const wrapper = await mountSwitcher()
    ;(wrapper.vm as unknown as { ensureFreshOnOpen(): void }).ensureFreshOnOpen()
    const selected = lastSelect(wrapper)
    expect(selected?.startsWith('user-')).toBe(true)
    expect(selected).not.toBe('user-done')
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
