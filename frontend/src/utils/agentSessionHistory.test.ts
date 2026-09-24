import { describe, expect, it } from 'vitest'

import { AgentSessionHistoryWindow } from './agentSessionHistory'

const messages = (start: number, count: number) => Array.from({ length: count }, (_, offset) => ({
  role: 'user' as const,
  content: `消息-${start + offset}`,
}))

describe('服务端会话历史窗口', () => {
  it('127 条消息跨三页按序恢复，后续页面轮询不丢旧页', () => {
    const history = new AgentSessionHistoryWindow()
    history.apply('owner-session', { messages: messages(100, 27), total: 127, oldest_message_index: 100, has_more: true })
    expect(history.hasMore).toBe(true)
    history.apply('owner-session', { messages: messages(50, 50), total: 127, oldest_message_index: 50, has_more: true })
    history.apply('owner-session', { messages: messages(0, 50), total: 127, oldest_message_index: 0, has_more: false })
    expect(history.messages).toHaveLength(127)
    history.apply('owner-session', { messages: messages(28, 100), total: 128, oldest_message_index: 28, has_more: true })
    expect(history.messages.map((item) => item.content)).toEqual(messages(0, 128).map((item) => item.content))
    expect(history.hasMore).toBe(false)
  })

  it('同名会话切换账号时不复用前一账号缓存', () => {
    const history = new AgentSessionHistoryWindow()
    history.apply('owner-session', { messages: [{ role: 'user', content: '旧账号私密消息' }], total: 1, oldest_message_index: 0, has_more: false })
    history.reset('other-account:owner-session')
    history.apply('other-account:owner-session', { messages: [{ role: 'user', content: '新账号消息' }], total: 1, oldest_message_index: 0, has_more: false })
    expect(history.messages.map((item) => item.content)).toEqual(['新账号消息'])
  })
})
