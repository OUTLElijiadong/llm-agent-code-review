import { expect, it, vi } from 'vitest'

import { notifyRoundtableToolCompleted } from './roundtableNotifications'

it('只有本账号圆桌启动或续会工具完成时通知全局列表刷新', () => {
  const listener = vi.fn()
  window.addEventListener('prism:roundtable-list-changed', listener)
  try {
    notifyRoundtableToolCompleted({ type: 'response.tool.started', tool_name: 'start_roundtable_discussion' }, 5)
    notifyRoundtableToolCompleted({ type: 'response.tool.completed', tool_name: 'create_agent_team' }, 5)
    notifyRoundtableToolCompleted({ type: 'response.tool.completed', tool_name: 'start_roundtable_discussion' }, undefined)
    expect(listener).not.toHaveBeenCalled()

    notifyRoundtableToolCompleted({ type: 'response.tool.completed', tool_name: 'start_roundtable_discussion' }, 5)
    notifyRoundtableToolCompleted({ type: 'response.tool.completed', tool_name: 'control_roundtable_discussion' }, 5)
    expect(listener).toHaveBeenCalledTimes(2)
    expect((listener.mock.calls[0][0] as CustomEvent).detail).toEqual({ ownerUserId: 5 })
  } finally {
    window.removeEventListener('prism:roundtable-list-changed', listener)
  }
})
