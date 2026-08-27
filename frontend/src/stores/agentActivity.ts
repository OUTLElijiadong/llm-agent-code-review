import { computed, ref } from 'vue'
import { defineStore } from 'pinia'

/**
 * 小菱「帮我操作」全局活动状态。
 *
 * 当小菱通过 user/admin_execute_capability 等能力直调业务 API 帮用户
 * 操作页面时,本 store 记录「正在操作」的全局信号,驱动:
 *  - AgentActivityBorder(页面四周彩色流动边框)
 *  - VirtualCursor(虚拟鼠标滑入 + 目标高亮)
 * 让非技术用户明确感知「小菱正在替我操作页面」。
 *
 * 纯前端状态,不改任何后端契约;由 AgentChatDrawer 在收到 SSE 工具
 * 生命周期事件时 begin/end,多并发用计数避免串扰。
 */

export type AgentActivityPhase = 'idle' | 'acting'

export interface AgentActivityItem {
  /** 唯一标识(优先 call_id,否则自增) */
  key: string
  /** 通俗动作描述,如「小菱正在帮你操作页面…」 */
  label: string
  /** 可选的操作目标语义(用于虚拟鼠标定位提示,如页面区域) */
  targetHint?: string
}

let sequence = 0

export const useAgentActivityStore = defineStore('agentActivity', () => {
  /** 进行中的页面操作集合(key 去重,支持多并发) */
  const active = ref<Map<string, AgentActivityItem>>(new Map())

  const phase = computed<AgentActivityPhase>(() => (active.value.size > 0 ? 'acting' : 'idle'))
  const isActing = computed(() => active.value.size > 0)
  /** 最近一个动作(虚拟鼠标/徽标展示用) */
  const current = computed<AgentActivityItem | null>(() => {
    const items = [...active.value.values()]
    return items.length ? items[items.length - 1] : null
  })
  const count = computed(() => active.value.size)
  const completionTimers = new Map<string, number>()

  /**
   * 标记一个页面操作开始。
   * @param label 通俗动作描述
   * @param key 可选唯一键(call_id);缺省自动生成
   * @param targetHint 可选目标区域语义
   * @returns 实际使用的 key(end 时回传)
   */
  function begin(label: string, key?: string, targetHint?: string): string {
    const id = key?.trim() || `act-${++sequence}`
    const next = new Map(active.value)
    next.set(id, { key: id, label, targetHint })
    active.value = next
    return id
  }

  /**
   * 标记一个页面操作结束。
   * @param key begin 返回的 key;缺省清空最新一个
   */
  function end(key?: string): void {
    if (!active.value.size) return
    const next = new Map(active.value)
    if (key && next.has(key)) {
      window.clearTimeout(completionTimers.get(key))
      completionTimers.delete(key)
      next.delete(key)
    } else {
      const lastKey = [...next.keys()].pop()
      if (lastKey !== undefined) next.delete(lastKey)
    }
    active.value = next
  }

  /** 成功完成后延迟收起，让虚拟鼠标有时间抵达目标并显示点击反馈。 */
  function complete(key: string, delayMs = 1650): void {
    if (!active.value.has(key)) return
    window.clearTimeout(completionTimers.get(key))
    completionTimers.set(key, window.setTimeout(() => {
      completionTimers.delete(key)
      end(key)
    }, Math.max(0, delayMs)))
  }

  /** 清空全部活动(会话切换/登出时兜底)。 */
  function clear(): void {
    for (const timer of completionTimers.values()) window.clearTimeout(timer)
    completionTimers.clear()
    active.value = new Map()
  }

  return { active, phase, isActing, current, count, begin, end, complete, clear }
})
