import { beforeEach, describe, expect, it } from 'vitest'

import {
  createAgentChatSession,
  findPristineAgentChatSession,
  isPristineAgentChatSession,
  loadAgentChatSessions,
  mergeAgentChatSessions,
  saveAgentChatSnapshot,
  type AgentChatSessionMeta,
} from './agentChatSessions'

const WELCOME = '你好,我是小菱!'

function seedSessions(metas: AgentChatSessionMeta[]): void {
  window.localStorage.setItem('prism-agent-sessions:user', JSON.stringify(metas))
}

function seedSnapshot(id: string, messages: Array<{ role: 'user' | 'assistant'; content: string }>, runStatus: string | null): void {
  saveAgentChatSnapshot(id, { messages, runStatus, updatedAt: Date.now() })
}

beforeEach(() => {
  window.localStorage.clear()
})

describe('isPristineAgentChatSession', () => {
  it('无快照视为空的新对话', () => {
    expect(isPristineAgentChatSession('user-empty', WELCOME)).toBe(true)
  })

  it('仅有欢迎语视为空的新对话', () => {
    seedSnapshot('user-welcome', [{ role: 'assistant', content: WELCOME }], 'completed')
    expect(isPristineAgentChatSession('user-welcome', WELCOME)).toBe(true)
  })

  it('有真实输入输出则不是空对话', () => {
    seedSnapshot('user-real', [{ role: 'user', content: '查询项目' }], 'completed')
    expect(isPristineAgentChatSession('user-real', WELCOME)).toBe(false)
  })

  it('运行中/等待审批/等待输入的会话不是空对话', () => {
    seedSnapshot('user-busy', [], 'waiting_input')
    expect(isPristineAgentChatSession('user-busy', WELCOME)).toBe(false)
  })
})

describe('findPristineAgentChatSession', () => {
  it('跳过 busy 会话并返回可复用的空会话', () => {
    seedSessions([
      { id: 'user-a', title: '新对话', createdAt: 1 },
      { id: 'user-b', title: '运行中', createdAt: 2 },
      { id: 'user-c', title: '完成', createdAt: 3 },
    ])
    seedSnapshot('user-b', [], 'running')
    seedSnapshot('user-c', [{ role: 'user', content: '查询' }], 'completed')
    const found = findPristineAgentChatSession(
      loadAgentChatSessions('user', 'legacy', 'user'),
      WELCOME,
      new Set(['user-b']),
    )
    expect(found?.id).toBe('user-a')
  })

  it('无空会话时返回 undefined', () => {
    seedSessions([{ id: 'user-done', title: '完成', createdAt: 1 }])
    seedSnapshot('user-done', [{ role: 'user', content: '查询' }], 'completed')
    expect(findPristineAgentChatSession(loadAgentChatSessions('user', 'legacy', 'user'), WELCOME)).toBeUndefined()
  })
})

describe('createAgentChatSession 保持既有会话', () => {
  it('新建会话插入最前且不丢历史', () => {
    seedSessions([{ id: 'user-old', title: '旧对话', createdAt: 1 }])
    const created = createAgentChatSession('user', 'user')
    const all = loadAgentChatSessions('user', 'legacy', 'user')
    expect(created.id.startsWith('user-')).toBe(true)
    expect(all[0].id).toBe(created.id)
    expect(all.some((item) => item.id === 'user-old')).toBe(true)
  })
})

describe('mergeAgentChatSessions 服务端发现优先', () => {
  it('只保留当前 surface 已发现的会话,并使用本地标题和顺序', () => {
    const merged = mergeAgentChatSessions(
      [
        { id: 'user-local', title: '本地标题', createdAt: 20 },
        { id: 'user-stale', title: '旧账户标题', createdAt: 10 },
      ],
      [
        { id: 'user-local', title: '服务端标题', surface: 'user', kind: 'session', lastSeenAt: '2026-08-12T00:00:00Z' },
        { id: 'user-remote', title: '远程会话', surface: 'user', kind: 'session', lastSeenAt: '2026-08-12T00:01:00Z' },
        { id: 'admin-remote', title: '管理会话', surface: 'admin', kind: 'session', lastSeenAt: '2026-08-12T00:02:00Z' },
      ],
      'user',
    )

    expect(merged.map((item) => item.id)).toEqual(['user-local', 'user-remote'])
    expect(merged[0].title).toBe('本地标题')
    expect(merged[1].title).toBe('远程会话')
  })
})
