import type { AgentTeamDetail } from '@/api/agentTeams'
import type { AgentMeshMessage } from '@/api/agentMesh'
import type { AgentResponseSession } from '@/api/agentResponses'

const TERMINAL_TEAM_STATUSES = new Set(['completed', 'failed', 'blocked', 'cancelled', 'expired'])

/** 团队账本仅用于恢复进度；聊天结论必须来自小菱的真实汇总。 */
export function isScopedTerminalAgentTeam(
  team: AgentTeamDetail,
  expected: { teamId: number; surface: 'user' | 'admin'; sessionId: string },
): boolean {
  return team.team_id === expected.teamId
    && team.surface === expected.surface
    && team.session_id === expected.sessionId
    && TERMINAL_TEAM_STATUSES.has(team.status)
}

export function agentTeamReceiptKey(messageId: string): string {
  return `team-terminal:${messageId}`
}

/**
 * 找出失败 Responses 运行确实关联的团队终态回执。
 * 优先使用 run.mesh_message_id；普通聊天中由工具创建团队时，改用同一 run
 * 的持久化 create_agent_team 完成事件给出的 team_id。只接受发往当前 surface/session 的回执。
 */
export function terminalTeamReceiptsForFailedRun(session: AgentResponseSession): AgentMeshMessage[] {
  const run = session.run
  if (
    !run
    || !['failed', 'incomplete', 'max_rounds_exceeded'].includes(run.status)
    || !['user', 'admin'].includes(session.surface)
    || !session.session_id
  ) return []

  const expectedAddress = `session:${session.surface}:${session.session_id}`
  const directMessageId = run.mesh_message_id?.trim() ?? ''
  const teamIds = new Set<number>()
  for (const event of session.events ?? []) {
    const item = event as Record<string, unknown>
    if (item.type !== 'response.tool.completed') continue
    const toolName = String(item.tool_name ?? item.name ?? '')
    if (!['create_agent_team', 'retry_agent_team', 'user_execute_capability', 'admin_execute_capability'].includes(toolName)) continue

    let output = item.output ?? item.output_summary ?? item.preview
    if (typeof output === 'string') {
      try { output = JSON.parse(output) as unknown } catch { output = null }
    }
    const outputRecord = output && typeof output === 'object' && !Array.isArray(output)
      ? output as Record<string, unknown> : null
    const dataRecord = outputRecord?.data && typeof outputRecord.data === 'object' && !Array.isArray(outputRecord.data)
      ? outputRecord.data as Record<string, unknown> : null
    const rawTeamId = item.team_id ?? outputRecord?.team_id ?? dataRecord?.team_id
    const teamId = Number(rawTeamId)
    if (Number.isSafeInteger(teamId) && teamId > 0) teamIds.add(teamId)
  }

  return (session.mesh_messages ?? []).filter((message) => {
    if (message.context?.team_terminal !== true || message.send_to !== expectedAddress) return false
    if (directMessageId && message.message_id === directMessageId) return true
    const teamId = Number(message.payload?.team_id ?? message.context?.team_id)
    return Number.isSafeInteger(teamId) && teamIds.has(teamId)
  })
}
