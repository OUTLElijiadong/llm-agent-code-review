import { isNavigationFailure, NavigationFailureType, type Router } from 'vue-router'
import { loadAgentChatSnapshot, saveAgentChatSnapshot, type AgentChatSnapshotMessage } from './agentChatSessions'
import { hasReachedNavigationTarget } from './agentNavigation'
import { requestXiaolingNavigation } from './xiaolingNavigation'

export type LocalNavigationAction = NonNullable<AgentChatSnapshotMessage['localAction']>
type NavigationResult = 'completed' | 'failed'

/** 只合并本地导航显示记录；不会把缓存转换为可执行动作或模型重试请求。 */
export function mergeLocalNavigationMessages<T>(
  restored: readonly T[],
  cached: readonly AgentChatSnapshotMessage[],
  snapshot: (message: T) => AgentChatSnapshotMessage,
  hydrate: (message: AgentChatSnapshotMessage) => T,
): T[] {
  const remote = restored.filter(message => !snapshot(message).localAction)
  const anchors = new Map<number, number>()
  // 本地只缓存末尾窗口，从后向前对齐服务端消息，重复正文也按出现顺序匹配。
  let cursor = remote.length - 1
  for (let index = cached.length - 1; index >= 0; index -= 1) {
    const message = cached[index]!
    if (message.localAction) continue
    for (let candidate = cursor; candidate >= 0; candidate -= 1) {
      const target = snapshot(remote[candidate]!)
      if (target.role === message.role && target.content === message.content) {
        anchors.set(index, candidate)
        cursor = candidate - 1
        break
      }
    }
  }
  const slots = new Map<number, T[]>()
  const seen = new Set<string>()
  let previous: number | undefined
  for (let index = 0; index < cached.length; index += 1) {
    if (anchors.has(index)) previous = anchors.get(index)
    const message = cached[index]!, action = message.localAction
    if (action?.kind !== 'navigation' || !action.id || !['user', 'assistant'].includes(message.role)) continue
    const key = `${action.id}:${message.role}`
    if (seen.has(key)) continue
    seen.add(key)
    const next = [...anchors].filter(([position]) => position > index).sort(([left], [right]) => left - right)[0]?.[1]
    const position = previous !== undefined ? previous + 1 : next ?? remote.length
    const items = slots.get(position) ?? []
    items.push(hydrate(action.status === 'pending' && message.role === 'assistant'
      ? { ...message, content: '这次页面跳转尚未确认，请重新发起导航。' } : message))
    slots.set(position, items)
  }
  return remote.flatMap((message, index) => [...(slots.get(index) ?? []), message])
    .concat(slots.get(remote.length) ?? [])
}

/** 路由可能卸载聊天组件；结果仍只结算到发起时的账号、入口和会话。 */
export function settleLocalNavigationSnapshot(
  sessionId: string, storageKey: string, actionId: string, status: NavigationResult, content: string,
): void {
  const cached = loadAgentChatSnapshot(sessionId, storageKey)
  if (!cached?.messages.some(message => message.localAction?.id === actionId)) return
  saveAgentChatSnapshot(sessionId, {
    ...cached,
    messages: cached.messages.map(message => message.localAction?.id === actionId ? {
      ...message, content: message.role === 'assistant' ? content : message.content,
      localAction: { ...message.localAction, status },
    } : message),
    updatedAt: Date.now(),
  }, storageKey)
}

/** 虚拟鼠标接收请求不代表导航成功；仅真实路由完成且目标一致时结算成功。 */
export function performLocalNavigation(
  router: Router, route: string, label: string, canExecute: () => boolean,
  onResult: (status: NavigationResult, content: string) => void,
  ownsAccount: () => boolean = canExecute,
): Promise<void> {
  return new Promise(resolve => {
    let settled = false
    const finish = (status: NavigationResult, content: string) => {
      if (settled) return
      settled = true
      try { onResult(status, content) } finally { resolve() }
    }
    const confirmed = () => ownsAccount() && hasReachedNavigationTarget(router, route)
    const finishNavigation = (success: boolean) => {
      if (success && confirmed()) finish('completed', `已打开「${label}」。`)
      else finish('failed', '未能确认目标页面已打开，请检查当前页面后重新发起导航。')
    }
    const accepted = requestXiaolingNavigation(route, label, async () => {
      if (!canExecute()) { finish('failed', '会话已变化，未执行这次页面跳转。'); return }
      try {
        const result = await router.push(route)
        finishNavigation(!isNavigationFailure(result) || isNavigationFailure(result, NavigationFailureType.duplicated))
      } catch {
        finish('failed', '页面跳转失败，请重新发起导航。')
      }
    }, undefined, { canExecute, confirmResult: confirmed, onSettled: finishNavigation })
    if (!accepted) finish('failed', '目标页面无效，未执行页面跳转。')
  })
}
