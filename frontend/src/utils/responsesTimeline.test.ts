import { describe, expect, it } from 'vitest'

import {
  applyResponseToolEvent,
  attachApprovalToToolCall,
  attachInputToToolCall,
  finishResponseToolCalls,
  formatResponseValue,
  isResponseToolEvent,
  responseAllowsFreeText,
  responseInputOptions,
  responseInputQuestion,
  setResponseToolCallStatus,
  type ResponseToolCall,
} from '@/utils/responsesTimeline'

describe('responsesTimeline', () => {
  it('uses the backend lifecycle identity and success summary', () => {
    const calls: ResponseToolCall[] = []
    applyResponseToolEvent(calls, {
      type: 'response.tool.started',
      call_id: 'call-agent',
      tool_name: 'invoke_published_agent',
      agent_code: 'security_reviewer',
      arguments: { agent_code: 'security_reviewer', task: '检查鉴权' },
    })
    applyResponseToolEvent(calls, {
      type: 'response.tool.completed',
      call_id: 'call-agent',
      tool_name: 'invoke_published_agent',
      agent_code: 'security_reviewer',
      output_summary: { status: 'completed', issues: 2 },
    })

    expect(calls).toHaveLength(1)
    expect(calls[0]).toMatchObject({
      name: 'invoke_published_agent',
      agentCode: 'security_reviewer',
      status: 'completed',
    })
    expect(calls[0].resultPreview).toContain('"issues": 2')
  })

  it('redacts sensitive fields without hiding operation targets', () => {
    const text = formatResponseValue({ user_id: 26, api_key: 'secret-value', password: '123456' })
    expect(text).toContain('"user_id": 26')
    expect(text).not.toContain('secret-value')
    expect(text).not.toContain('123456')
    expect(text).toContain('[已隐藏]')
  })

  it('redacts reasoning, nested JSON secrets, bearer headers, and raw argument deltas', () => {
    const formatted = formatResponseValue({
      query: 'visible',
      reasoning: 'reasoning-secret-marker',
      payload: '{"api_key":"nested-secret-marker","path":"src/main.ts"}',
      error: 'Authorization: Bearer bearer-secret-marker',
    })

    expect(formatted).toContain('visible')
    expect(formatted).toContain('src/main.ts')
    expect(formatted).not.toContain('reasoning-secret-marker')
    expect(formatted).not.toContain('nested-secret-marker')
    expect(formatted).not.toContain('bearer-secret-marker')

    const calls: ResponseToolCall[] = []
    applyResponseToolEvent(calls, {
      type: 'response.output_item.added',
      output_index: 0,
      item: { type: 'function_call', id: 'item-secret', call_id: 'call-secret', name: 'mcp_tool' },
    })
    applyResponseToolEvent(calls, {
      type: 'response.function_call_arguments.delta',
      output_index: 0,
      item_id: 'item-secret',
      delta: '{"authorization":"delta-secret-marker',
    })
    expect(calls[0].argumentsText).not.toContain('delta-secret-marker')

    applyResponseToolEvent(calls, {
      type: 'response.function_call_arguments.done',
      output_index: 0,
      item_id: 'item-secret',
      arguments: '{"authorization":"done-secret-marker","path":"src/main.ts"}',
    })
    applyResponseToolEvent(calls, {
      type: 'response.tool.failed',
      item_id: 'item-secret',
      call_id: 'call-secret',
      tool_name: 'mcp_tool',
      error: 'Authorization: Bearer error-secret-marker',
    })

    expect(calls[0].argumentsText).toContain('src/main.ts')
    expect(calls[0].argumentsText).not.toContain('done-secret-marker')
    expect(calls[0].error).not.toContain('error-secret-marker')
  })

  it('covers output item identity merging, failures, and lifecycle completion', () => {
    const calls: ResponseToolCall[] = []
    const ordinary = { type: 'message', id: 'message-1' }
    applyResponseToolEvent(calls, {
      type: 'response.output_item.added',
      output_index: 0,
      item: ordinary,
    })
    expect(calls).toHaveLength(0)

    applyResponseToolEvent(calls, {
      type: 'response.output_item.added',
      output_index: 1,
      item: { type: 'function_call', call_id: 'call-merge', name: 'lookup', arguments: { query: '李' } },
    })
    applyResponseToolEvent(calls, {
      type: 'response.output_item.done',
      output_index: 1,
      item: {
        type: 'function_call',
        id: 'item-merge',
        call_id: 'call-merge',
        name: 'lookup',
        status: 'failed',
        error: '找不到目标',
      },
    })
    expect(calls[0]).toMatchObject({
      itemId: 'item-merge',
      callId: 'call-merge',
      status: 'failed',
      error: '找不到目标',
    })

    applyResponseToolEvent(calls, {
      type: 'response.function_call_arguments.delta',
      item_id: 'item-new',
      output_index: 2,
      delta: '{',
    })
    applyResponseToolEvent(calls, {
      type: 'response.function_call_arguments.done',
      item_id: 'item-new',
      output_index: 2,
      arguments: '',
    })
    expect(calls[1].status).toBe('running')

    applyResponseToolEvent(calls, {
      type: 'response.tool.started',
      call_id: 'call-run',
      tool_name: 'run_check',
      arguments: { path: 'src' },
    })
    applyResponseToolEvent(calls, {
      type: 'response.tool.completed',
      call_id: 'call-run',
      tool_name: 'run_check',
      output: { ok: true },
    })
    expect(calls[2]).toMatchObject({ status: 'completed', resultPreview: '{\n  "ok": true\n}' })
    applyResponseToolEvent(calls, {
      type: 'response.tool.failed',
      call_id: 'call-missing',
      name: 'run_check',
      error: '',
    })
    expect(calls[3]).toMatchObject({ status: 'failed', error: '工具执行失败' })
    expect(isResponseToolEvent({ type: 'response.tool.failed', error: 'x' })).toBe(true)
    expect(isResponseToolEvent({ type: 'response.completed', response: {} })).toBe(false)
  })

  it('normalizes approval, input questions, options, and terminal statuses', () => {
    const calls: ResponseToolCall[] = []
    const approval = attachApprovalToToolCall(calls, 'call-approve', 'delete_user', { user_id: 9 })
    expect(approval.status).toBe('waiting_approval')
    const input = attachInputToToolCall(calls, {
      type: 'response.input.required',
      run_id: 'run-1',
      call_id: 'call-input',
      arguments: { question: '从参数中读取' },
    })
    expect(input).toMatchObject({ callId: 'call-input', name: 'ask_user', status: 'waiting_input' })
    expect(responseInputQuestion({ type: 'response.input.required', run_id: 'r', question: '  直接问题  ' })).toBe('直接问题')
    expect(responseInputQuestion({ type: 'response.input.required', run_id: 'r', arguments: { question: '参数问题' } })).toBe('参数问题')
    expect(responseInputQuestion({ type: 'response.input.required', run_id: 'r', questions: [{ label: '问题标签' }] })).toBe('问题标签')
    expect(responseInputQuestion({ type: 'response.input.required', run_id: 'r' })).toBe('请补充信息后继续执行')
    expect(responseInputOptions({
      type: 'response.input.required',
      run_id: 'r',
      options: ['a', 2, true, { id: 'b', title: 'B', hint: '说明' }, { value: 'a', label: '重复' }, null, {}],
    })).toEqual([
      { label: 'a', value: 'a' },
      { label: '2', value: '2' },
      { label: 'true', value: 'true' },
      { label: 'B', value: 'b', description: '说明' },
    ])
    expect(responseInputOptions({
      type: 'response.input.required',
      run_id: 'r',
      arguments: { options: [{ code: 'x', text: 'X', description: 'desc' }] },
    })).toEqual([{ label: 'X', value: 'x', description: 'desc' }])
    expect(responseInputOptions({
      type: 'response.input.required',
      run_id: 'r',
      questions: [{ title: '请选择', options: [{ name: 'n' }] }],
    })).toEqual([{ label: 'n', value: 'n' }])
    expect(responseAllowsFreeText({ type: 'response.input.required', run_id: 'r', allow_free_text: false })).toBe(false)
    expect(responseAllowsFreeText({ type: 'response.input.required', run_id: 'r', allow_custom: false })).toBe(false)
    expect(responseAllowsFreeText({ type: 'response.input.required', run_id: 'r', arguments: { allow_free_text: false } })).toBe(false)
    expect(responseAllowsFreeText({ type: 'response.input.required', run_id: 'r' })).toBe(true)
    expect(setResponseToolCallStatus(calls, undefined, 'completed')).toBe(false)
    expect(setResponseToolCallStatus(calls, 'missing', 'failed')).toBe(false)
    expect(setResponseToolCallStatus(calls, 'call-approve', 'rejected', '拒绝')).toBe(true)
    finishResponseToolCalls(calls, 'failed')
    expect(calls.find((call) => call.callId === 'call-input')?.status).toBe('waiting_input')
    expect(calls.find((call) => call.callId === 'call-approve')?.status).toBe('rejected')
    expect(formatResponseValue(['x', { private_key: 'hidden' }])).toContain('[已隐藏]')
    expect(formatResponseValue('not-json')).toBe('not-json')
    expect(formatResponseValue('')).toBe('')
  })
})
