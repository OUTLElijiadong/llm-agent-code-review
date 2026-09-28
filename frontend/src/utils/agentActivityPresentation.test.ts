import { describe, expect, it } from 'vitest'

import { formatAgentActivityUsage } from './agentActivityPresentation'

describe('formatAgentActivityUsage', () => {
  it('明确区分模型调用和不计模型 Token 的本地工具活动', () => {
    expect(formatAgentActivityUsage({
      calls_today: 1651,
      model_calls_today: 0,
      model_tokens_today: 0,
      tool_calls_today: 1651,
    })).toEqual({
      label: '模型 0 · 工具 1,651',
      title: '今日模型调用 0 次，共 0 Token；本地工具调用 1,651 次，不调用模型、不产生模型 Token 费用',
    })
  })

  it('展示真实模型次数和 Token 用量', () => {
    expect(formatAgentActivityUsage({
      calls_today: 9,
      model_calls_today: 7,
      model_tokens_today: 168_042,
      tool_calls_today: 2,
    })).toEqual({
      label: '模型 7 · 168K Token · 工具 2',
      title: '今日模型调用 7 次，共 168,042 Token；本地工具调用 2 次，不调用模型、不产生模型 Token 费用',
    })
  })

  it('将缺少完整 Token 用量的模型调用单独标记，避免把已知部分冒充总量', () => {
    expect(formatAgentActivityUsage({
      calls_today: 3,
      model_calls_today: 2,
      model_tokens_today: 125,
      model_unknown_usage_calls_today: 1,
      tool_calls_today: 1,
    })).toEqual({
      label: '模型 2 · 125 Token · 用量未知 1 · 工具 1',
      title: '今日模型调用 2 次，可核算 125 Token；1 次调用缺少完整用量，实际总量可能更高；本地工具调用 1 次，不调用模型、不产生模型 Token 费用',
    })
  })

  it('旧接口未拆分时不把总活动次数冒充模型调用', () => {
    expect(formatAgentActivityUsage({ calls_today: 12 })).toEqual({
      label: '活动 12',
      title: '当前接口尚未区分模型调用与本地工具活动',
    })
  })
})
