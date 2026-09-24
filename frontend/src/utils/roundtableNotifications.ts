/** 圆桌在小菱后台创建后，通知当前账号的全局入口重新读取服务端列表。 */
export function notifyRoundtableToolCompleted(
  event: { type: string; tool_name?: unknown },
  ownerUserId: number | null | undefined,
): void {
  if (event.type !== 'response.tool.completed'
    || !['start_roundtable_discussion', 'control_roundtable_discussion'].includes(
      typeof event.tool_name === 'string' ? event.tool_name : '',
    )
    || !Number.isSafeInteger(ownerUserId) || Number(ownerUserId) <= 0) return
  window.dispatchEvent(new CustomEvent('prism:roundtable-list-changed', {
    detail: { ownerUserId },
  }))
}
