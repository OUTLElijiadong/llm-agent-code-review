import type { AgentResponseSessionMessage, AgentResponseSessionPage } from '@/api/agentResponses'

/** 服务端消息序号是会话内零基位置；只显示连续后缀，缺口始终可继续向前翻页。 */
export class AgentSessionHistoryWindow {
  private sessionId = ''
  private readonly rows = new Map<number, AgentResponseSessionMessage>()
  private total = 0

  reset(sessionId: string): void {
    this.sessionId = sessionId
    this.rows.clear()
    this.total = 0
  }

  apply(sessionId: string, page: AgentResponseSessionPage): void {
    if (sessionId !== this.sessionId) this.reset(sessionId)
    const total = Math.max(0, Math.floor(page.total))
    const start = Math.max(0, Math.floor(page.oldest_message_index))
    if (total < this.total) {
      // 服务端会话被重新建立时，旧消息索引不再可证明属于当前会话。
      this.rows.clear()
    }
    this.total = total
    for (const index of [...this.rows.keys()]) {
      if (index >= total) this.rows.delete(index)
    }
    page.messages.forEach((message, offset) => {
      if (start + offset < total) this.rows.set(start + offset, message)
    })
  }

  get oldestMessageIndex(): number {
    let index = this.total
    while (index > 0 && this.rows.has(index - 1)) index -= 1
    return index
  }

  get hasMore(): boolean {
    return this.oldestMessageIndex > 0
  }

  get messages(): AgentResponseSessionMessage[] {
    const items: AgentResponseSessionMessage[] = []
    for (let index = this.oldestMessageIndex; index < this.total; index += 1) {
      const item = this.rows.get(index)
      if (item) items.push(item)
    }
    return items
  }
}
