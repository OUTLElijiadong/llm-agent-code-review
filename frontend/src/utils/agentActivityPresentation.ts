export interface AgentActivityUsageInput {
  calls_today: number
  model_calls_today?: number
  model_tokens_today?: number
  tool_calls_today?: number
}

export interface AgentActivityUsageText {
  label: string
  title: string
}

const integerFormatter = new Intl.NumberFormat('zh-CN', { maximumFractionDigits: 0 })

function safeCount(value: number | undefined): number {
  return Number.isFinite(value) ? Math.max(0, Math.round(value ?? 0)) : 0
}

function compactTokens(value: number): string {
  if (value >= 1_000_000) return `${Math.round(value / 1_000_000)}M`
  if (value >= 1_000) return `${Math.round(value / 1_000)}K`
  return integerFormatter.format(value)
}

/**
 * 管理大屏必须把付费模型调用和免费本地工具活动分开呈现。
 * 旧接口没有拆分字段时只称为“活动”，避免把总数误报成模型消费。
 */
export function formatAgentActivityUsage(activity: AgentActivityUsageInput): AgentActivityUsageText {
  const hasSplitUsage = activity.model_calls_today !== undefined || activity.tool_calls_today !== undefined
  if (!hasSplitUsage) {
    return {
      label: `活动 ${integerFormatter.format(safeCount(activity.calls_today))}`,
      title: '当前接口尚未区分模型调用与本地工具活动',
    }
  }

  const modelCalls = safeCount(activity.model_calls_today)
  const modelTokens = safeCount(activity.model_tokens_today)
  const toolCalls = safeCount(activity.tool_calls_today)
  const label = [`模型 ${integerFormatter.format(modelCalls)}`]
  if (modelTokens > 0) label.push(`${compactTokens(modelTokens)} Token`)
  if (toolCalls > 0) label.push(`工具 ${integerFormatter.format(toolCalls)}`)

  return {
    label: label.join(' · '),
    title: (
      `今日模型调用 ${integerFormatter.format(modelCalls)} 次，共 ${integerFormatter.format(modelTokens)} Token；` +
      `本地工具调用 ${integerFormatter.format(toolCalls)} 次，不调用模型、不产生模型 Token 费用`
    ),
  }
}
