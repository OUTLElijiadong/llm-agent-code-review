import { describe, expect, it } from 'vitest'

import type { ResponseToolCall } from './responsesTimeline'
import { settleAgentMeshToolCalls } from './agentMeshTimeline'

const receivingCall: ResponseToolCall = {
  key: 'mesh-msg-test',
  callId: 'msg_test',
  name: 'receive_message',
  argumentsText: '{}',
  status: 'running',
}

describe('Agent Mesh timeline settlement', () => {
  it('流式响应完成后生成新数组并收敛为已完成', () => {
    const original = [receivingCall]
    const settled = settleAgentMeshToolCalls(original, true, 'completed')

    expect(settled).not.toBe(original)
    expect(settled[0]).not.toBe(receivingCall)
    expect(settled[0].status).toBe('completed')
    expect(receivingCall.status).toBe('running')
  })

  it('失败或取消时显示终态错误', () => {
    expect(settleAgentMeshToolCalls([receivingCall], false, 'failed', '模型超时')[0]).toMatchObject({
      status: 'failed',
      error: '模型超时',
    })
    expect(settleAgentMeshToolCalls([receivingCall], false, 'cancelled')[0]).toMatchObject({
      status: 'failed',
      error: '响应已取消',
    })
  })

  it('等待审批或追问时保持执行中', () => {
    expect(settleAgentMeshToolCalls([receivingCall], true, 'waiting_approval')[0].status).toBe('running')
    expect(settleAgentMeshToolCalls([receivingCall], true, 'waiting_input')[0].status).toBe('running')
  })
})
