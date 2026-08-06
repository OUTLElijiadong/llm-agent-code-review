import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'

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
