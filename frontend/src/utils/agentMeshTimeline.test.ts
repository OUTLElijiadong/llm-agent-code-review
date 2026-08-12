import { describe, expect, it } from 'vitest'

import type { ResponseToolCall } from './responsesTimeline'
import {
  agentMeshToolCalls,
  findAgentMeshTimeline,
  settleAgentMeshTimeline,
  settleAgentMeshToolCalls,
} from './agentMeshTimeline'

const receivingCall: ResponseToolCall = {
  key: 'mesh-msg-test',
  callId: 'msg_test',
  name: 'receive_message',
  argumentsText: '{}',
  status: 'running',
}

describe('Agent Mesh timeline settlement', () => {
  it.each([
    ['queued', 'queued'],
    ['delivered', 'delivered'],
    ['acknowledged', 'acknowledged'],
    ['processing', 'processing'],
    ['completed', 'completed'],
    ['dead_letter', 'failed'],
  ] as const)('保留消息协议状态 %s 的人类可读阶段', (protocolStatus, timelineStatus) => {
    const calls = agentMeshToolCalls([{
      schema_version: '1.0', message_id: `msg-${protocolStatus}`, idempotency_key: `idem-${protocolStatus}`,
      trace_id: 'trace-readable', correlation_id: 'corr-readable', causation_id: '',
      sent_from: 'agent:data-analysis', send_to: 'session:admin:admin-test',
      message_type: 'task.result', priority: 'normal', subject: '数据异常分析完成',
      status: protocolStatus, payload: { anomaly_count: 2 }, context: {}, artifacts: [], errors: [],
      last_error: protocolStatus === 'dead_letter' ? '结果无法回传：会话已归档' : '',
      requires_ack: true, max_attempts: 3, attempt_count: 1,
      expires_at: '', create_time: '', update_time: '',
    }], 'session:admin:admin-test')

    expect(calls[0]).toMatchObject({
      status: timelineStatus,
      subject: '数据异常分析完成',
      direction: 'receive',
      agentCode: 'agent:data-analysis',
      traceId: 'trace-readable',
      protocolStatus,
    })
  })

  it('失败消息展示服务端真实原因而不是笼统状态', () => {
    const calls = agentMeshToolCalls([{
      schema_version: '1.0', message_id: 'msg-failed', idempotency_key: 'idem-failed',
      trace_id: 'trace-failed', correlation_id: 'corr-failed', causation_id: '',
      sent_from: 'session:admin:test', send_to: 'agent:monitor', message_type: 'task.request',
      priority: 'normal', subject: '监控查询', status: 'dead_letter', payload: {}, context: {},
      artifacts: [], errors: [], last_error: 'Agent 结果无法回传：会话尚未注册', requires_ack: true,
      max_attempts: 3, attempt_count: 4, expires_at: '', create_time: '', update_time: '',
    }], 'session:admin:test')

    expect(calls[0].error).toBe('Agent 结果无法回传：会话尚未注册')
    expect(calls[0].resultPreview).toContain('会话尚未注册')
  })

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

  it('按消息 ID 复用已恢复的时间线并在集合上收敛状态', () => {
    const restored = { toolCalls: [receivingCall] }
    const entries = [{ toolCalls: [] }, restored]

    expect(findAgentMeshTimeline(entries, 'msg_test')).toBe(restored)
    expect(settleAgentMeshTimeline(entries, 'msg_test', true, 'completed')).toBe(true)
    expect(restored.toolCalls).not.toEqual([receivingCall])
    expect(restored.toolCalls[0].status).toBe('completed')
    expect(settleAgentMeshTimeline(entries, 'missing', true, 'completed')).toBe(false)
  })
})
