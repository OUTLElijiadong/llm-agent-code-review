import type { AgentTeamDetail, AgentTeamTask } from '@/api/agentTeams'

const TERMINAL_TEAM_STATUSES = new Set(['completed', 'failed', 'cancelled', 'expired'])
const FAILED_TASK_STATUSES = new Set(['failed', 'blocked', 'cancelled', 'dead_letter', 'expired'])
const TASK_SUMMARY_LIMIT = 700
const MAX_TASK_SUMMARIES = 16
const MAX_FINDINGS = 24

function readable(value: unknown, limit = TASK_SUMMARY_LIMIT): string {
  if (typeof value !== 'string') return ''
  const text = value.replace(/\s+/g, ' ').trim()
  if (!text) return ''
  if (text.length <= limit) return text
  return `${text.slice(0, limit)}…（摘要预览，完整内容见团队详情）`
}

function taskStatusLabel(task: AgentTeamTask): string {
  if (task.status === 'completed') return '已完成'
  if (task.status === 'running') return '执行中'
  if (task.status === 'queued' || task.status === 'waiting_dependency') return '排队中'
  if (task.status === 'blocked') return '受阻'
  if (task.status === 'cancelled') return '已取消'
  if (task.status === 'expired') return '已过期'
  if (task.status === 'dead_letter') return '已停止重试'
  return '失败'
}

function upstreamFailureLabel(error: string): string {
  if (/\b402\b|billing|quota|额度|计费|余额/i.test(error)) {
    return '上游模型服务返回 402（额度或计费拒绝）'
  }
  if (/\b429\b|rate.?limit|限流/i.test(error)) return '上游模型服务暂时限流'
  return '小菱的最终模型汇总未成功生成'
}

function taskFindings(task: AgentTeamTask): Array<Record<string, unknown>> {
  const findings = task.result?.findings
  if (!Array.isArray(findings)) return []
  return findings.filter((item): item is Record<string, unknown> => (
    Boolean(item) && typeof item === 'object' && !Array.isArray(item)
  ))
}

function findingLabel(finding: Record<string, unknown>): string {
  const severity = readable(finding.severity, 40)
  const title = readable(finding.title || finding.name || finding.description, 220)
  const file = readable(finding.file_name || finding.path, 180)
  const line = Number.isInteger(finding.line_number) && Number(finding.line_number) > 0
    ? `:${finding.line_number}` : ''
  const quote = readable(finding.evidence_quote, 180)
  const pieces = [severity ? `[${severity}]` : '', title, file ? `(${file}${line})` : '', quote ? `证据：“${quote}”` : '']
    .filter(Boolean)
  return `- ${pieces.join(' — ')}`
}

/**
 * Produces a truthful, account-scoped fallback from a terminal team ledger when
 * Xiaoling's final model response cannot be completed. It never upgrades a task
 * failure or an incomplete team into a successful review.
 */
export function buildAgentTeamFallback(
  team: AgentTeamDetail,
  expected: { teamId: number; surface: 'user' | 'admin'; sessionId: string; upstreamError: string },
): string | null {
  if (
    team.team_id !== expected.teamId
    || team.surface !== expected.surface
    || team.session_id !== expected.sessionId
    || !TERMINAL_TEAM_STATUSES.has(team.status)
  ) return null

  const tasks = Array.isArray(team.tasks) ? team.tasks : []
  const counts = team.counts
  const failures = tasks.filter((task) => FAILED_TASK_STATUSES.has(task.status)).length
  const completed = tasks.filter((task) => task.status === 'completed').length
  const summary = readable(team.summary?.final_result && typeof team.summary.final_result === 'object'
    ? (team.summary.final_result as Record<string, unknown>).summary
    : team.summary?.summary)
  const lines = [
    '### 团队审查部分结果（自动整理）',
    `团队「${readable(team.title, 160) || `#${team.team_id}`}」已进入终态：${team.status}；${completed}/${counts?.total ?? tasks.length} 个任务完成，${failures} 个任务失败或受阻。`,
    `${upstreamFailureLabel(expected.upstreamError)}。以下内容直接取自当前账号团队记录，未经小菱最终模型复核；不得将未完成或失败任务视为通过。完整记录仍保存在下方团队详情中。`,
  ]
  if (summary) lines.push('', `团队已保存摘要：${summary}`)

  if (tasks.length) {
    lines.push('', '执行节点：')
    for (const task of tasks.slice(0, MAX_TASK_SUMMARIES)) {
      lines.push(`- [${taskStatusLabel(task)}] ${readable(task.title, 180) || readable(task.task_key, 80)}`)
      const taskSummary = readable(task.result?.summary)
      if (taskSummary) lines.push(`  ${taskSummary}`)
      const errors = (task.errors ?? [])
        .map((item) => readable(item.message || item.code, 240))
        .filter(Boolean)
        .slice(0, 2)
      for (const error of errors) lines.push(`  失败原因：${error}`)
    }
    if (tasks.length > MAX_TASK_SUMMARIES) {
      lines.push(`- 另有 ${tasks.length - MAX_TASK_SUMMARIES} 个执行节点未在此摘要展开；可在团队详情查看。`)
    }
  }

  const findings = tasks.flatMap((task) => taskFindings(task).map((finding) => ({ task, finding })))
  if (findings.length) {
    lines.push('', `子 Agent 记录的问题（最多展开 ${Math.min(MAX_FINDINGS, findings.length)} 项）：`)
    for (const { finding } of findings.slice(0, MAX_FINDINGS)) {
      lines.push(findingLabel(finding))
      const description = readable(finding.description, 360)
      if (description && description !== readable(finding.title || finding.name || finding.description, 220)) {
        lines.push(`  ${description}`)
      }
    }
    if (findings.length > MAX_FINDINGS) {
      lines.push(`另有 ${findings.length - MAX_FINDINGS} 项问题仅保存在团队详情中。`)
    }
  }
  return lines.join('\n')
}

export function agentTeamFallbackKey(messageId: string): string {
  return `team-terminal:${messageId}`
}
