import { onBeforeUnmount, ref, watch, type Ref } from 'vue'

/** 距离底部多少像素内视为“正在看最新内容”。 */
const STICK_THRESHOLD = 48

/**
 * 小菱消息区的底部跟随：用户停在底部时新内容（结论、审批、追问）自动保持可见；
 * 用户主动上翻阅读历史时不打断。消息区尺寸变化（星图展开、输入框长高、窗口缩放）
 * 时若原本在底部，也重新贴底，避免最新结论被挤出视野。
 */
export function useChatAutoScroll(container: Ref<HTMLElement | null | undefined>) {
  const pinned = ref(true)
  let observer: ResizeObserver | null = null

  function atBottom(element: HTMLElement): boolean {
    return element.scrollHeight - element.scrollTop - element.clientHeight <= STICK_THRESHOLD
  }

  function onScroll(): void {
    const element = container.value
    if (element) pinned.value = atBottom(element)
  }

  /** 强制贴底：用户发送消息、切换会话等明确意图。 */
  function scrollToBottom(): void {
    const element = container.value
    if (!element) return
    element.scrollTop = element.scrollHeight
    pinned.value = true
  }

  /** 仅当用户原本在底部时跟随新内容。 */
  function followIfPinned(): void {
    if (pinned.value) scrollToBottom()
  }

  watch(container, (element, previous) => {
    previous?.removeEventListener('scroll', onScroll)
    observer?.disconnect()
    observer = null
    if (!element) return
    element.addEventListener('scroll', onScroll, { passive: true })
    if (typeof ResizeObserver !== 'undefined') {
      observer = new ResizeObserver(() => followIfPinned())
      observer.observe(element)
    }
  }, { immediate: true, flush: 'post' })

  onBeforeUnmount(() => {
    container.value?.removeEventListener('scroll', onScroll)
    observer?.disconnect()
  })

  return { pinned, scrollToBottom, followIfPinned }
}

/**
 * 把待处理的审批/追问挂到最新一条助手消息上，保证“小菱的询问”出现在对话最底部，
 * 而不是本轮较早创建的工具时间线条目里。返回实际承载的条目。
 */
export function latestAssistantTarget<T extends { role: string }>(messages: T[], fallback: T | null): T | null {
  for (let index = messages.length - 1; index >= 0; index -= 1) {
    const item = messages[index]
    if (item.role === 'assistant') return item
    if (item.role === 'user') break
  }
  return fallback
}
