import { beforeEach, describe, expect, it } from 'vitest'

import {
  createAgentChatSession,
  findPristineAgentChatSession,
  isPlaceholderAgentChatTitle,
  isPristineAgentChatSession,
  loadActiveAgentChatSession,
  loadAgentChatSnapshot,
  loadAgentChatSessions,
  mergeAgentChatSessions,
  markAgentChatLoginFreshStart,
  consumeAgentChatLoginFreshStart,
  saveActiveAgentChatSession,
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
  window.sessionStorage.clear()
})

describe('登录周期新对话标记', () => {
  it('普通端与管理端各消费一次,再次登录可重新置位', () => {
    markAgentChatLoginFreshStart()
    expect(consumeAgentChatLoginFreshStart('user')).toBe(true)
    expect(consumeAgentChatLoginFreshStart('user')).toBe(false)
    expect(consumeAgentChatLoginFreshStart('admin')).toBe(true)
    expect(consumeAgentChatLoginFreshStart('admin')).toBe(false)

    markAgentChatLoginFreshStart()
    expect(consumeAgentChatLoginFreshStart('user')).toBe(true)
    expect(consumeAgentChatLoginFreshStart('admin')).toBe(true)
  })
})

describe('isPlaceholderAgentChatTitle', () => {
  it('「新对话」和「默认对话」是占位标题', () => {
    expect(isPlaceholderAgentChatTitle('新对话')).toBe(true)
    expect(isPlaceholderAgentChatTitle('默认对话')).toBe(true)
  })

  it('带业务语义的标题不是占位标题', () => {
    expect(isPlaceholderAgentChatTitle('小菱生产验收发…')).toBe(false)
  })
})

describe('isPristineAgentChatSession', () => {
  it('未传入标题且无快照时仍视为空的新对话,兼容旧调用', () => {
    expect(isPristineAgentChatSession('user-empty', WELCOME)).toBe(true)
  })

  it('仅有欢迎语视为空的新对话', () => {
    seedSnapshot('user-welcome', [{ role: 'assistant', content: WELCOME }], 'completed')
    expect(isPristineAgentChatSession('user-welcome', WELCOME)).toBe(true)
  })

  it('无快照且标题为新对话时仍视为空的新对话', () => {
    expect(isPristineAgentChatSession('user-empty-fresh', WELCOME, '新对话')).toBe(true)
  })

  it('无快照且标题非占位时不是空的新对话', () => {
    expect(isPristineAgentChatSession('user-empty-titled', WELCOME, '小菱生产验收发…')).toBe(false)
  })

  it('有快照时仍按消息内容判断,不受标题影响', () => {
    seedSnapshot('user-titled-welcome', [{ role: 'assistant', content: WELCOME }], 'completed')
    expect(isPristineAgentChatSession('user-titled-welcome', WELCOME, '小菱生产验收发…')).toBe(true)
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

  it('跳过无快照且标题非占位的会话,复用占位空会话', () => {
    seedSessions([
      { id: 'user-stale', title: '小菱生产验收发…', createdAt: 1 },
      { id: 'user-fresh', title: '新对话', createdAt: 2 },
    ])
    const found = findPristineAgentChatSession(
      loadAgentChatSessions('user', 'legacy', 'user'),
      WELCOME,
    )
    expect(found?.id).toBe('user-fresh')
  })

  it('仅有标题非占位且无快照的会话时返回 undefined', () => {
    seedSessions([{ id: 'user-stale', title: '小菱生产验收发…', createdAt: 1 }])
    expect(findPristineAgentChatSession(loadAgentChatSessions('user', 'legacy', 'user'), WELCOME)).toBeUndefined()
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

describe('当前会话持久化', () => {
  it('保存并恢复最后查看的会话', () => {
    expect(loadActiveAgentChatSession('user')).toBe('')
    saveActiveAgentChatSession('user', 'user-history')
    expect(loadActiveAgentChatSession('user')).toBe('user-history')
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

  it('心跳未落库的 preserve 会话(新建/当前)始终排在最前不被误切', () => {
    // preserve 的会话是「本地新建、服务端 heartbeat 尚未落库」的,因此只出现在 local,
    // 不出现在 discovered——即使服务端已有会话,它仍被保留在列表中避免误切。
    const merged = mergeAgentChatSessions(
      [{ id: 'user-current', title: '当前对话', createdAt: 30 }],
      [
        { id: 'user-fresh', title: '最新对话', surface: 'user', kind: 'session', lastSeenAt: '2026-08-12T00:05:00Z' },
      ],
      'user',
      new Set(['user-current']),
    )

    expect(merged.some((item) => item.id === 'user-current')).toBe(true)
    expect(merged[0].id).toBe('user-current')
  })
})

describe('Agent Team 快照锚点', () => {
  it('保留无正文但带团队 ID 的调用时间线消息', () => {
    saveAgentChatSnapshot('user-team', {
      messages: [
        { role: 'user', content: '启动白盒测试' },
        { role: 'assistant', content: '', teamIds: [42] },
      ],
      runStatus: 'running',
      updatedAt: Date.now(),
    })

    expect(loadAgentChatSnapshot('user-team')?.messages).toEqual([
      { role: 'user', content: '启动白盒测试' },
      { role: 'assistant', content: '', teamIds: [42] },
    ])
  })

  it('保留团队最小摘要,团队接口暂时失败时仍能恢复卡片', () => {
    saveAgentChatSnapshot('user-team-summary', {
      messages: [{ role: 'assistant', content: '', teamIds: [42] }],
      teams: [{
        team_id: 42,
        title: '白盒核验团队',
        surface: 'user',
        session_id: 'user-team-summary',
        status: 'running',
        max_active_children: 3,
        trace_id: 'trace-42',
        counts: { total: 2, completed: 0, running: 2, queued: 0, failed: 0, blocked: 0 },
      }],
      runStatus: 'running',
      updatedAt: Date.now(),
    })

    expect(loadAgentChatSnapshot('user-team-summary')?.teams?.[0]).toMatchObject({
      team_id: 42,
      title: '白盒核验团队',
      status: 'running',
    })
  })
})
