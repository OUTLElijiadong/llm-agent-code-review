/**
 * Agent 资产状态中文标签
 * 统一 Agent 工坊 / 发布审批 / Skill 审批等页面的状态展示,
 * 避免界面直接展示 draft / pending / testing 等英文原始值。
 * 参照 reviewType.ts 的集中映射约定。
 */

/**
 * Agent 资产(自定义 Agent / Skill)状态 → 中文标签
 * 覆盖 draft/testing/pending_approval/approved/published/rejected/disabled 等
 */
export const AGENT_ASSET_STATUS_LABELS: Record<string, string> = {
  draft: '草稿',
  testing: '已测试',
  pending_approval: '待审批',
  approved: '已批准',
  published: '已发布',
  rejected: '已驳回',
  disabled: '已停用',
  enabled: '已启用',
  active: '已启用',
}

/**
 * 发布审批单(approval/release)状态 → 中文标签
 */
export const AGENT_APPROVAL_STATUS_LABELS: Record<string, string> = {
  pending: '待审批',
  approved: '已批准',
  rejected: '已驳回',
  withdrawn: '已撤回',
  published: '已发布',
  rolled_back: '已回滚',
  draft: '草稿',
}

/**
 * 获取 Agent 资产状态的中文展示文案,未知状态回退为原值
 * @param status - 后端状态原始值
 * @returns 用户可读的中文状态文案
 */
export function agentAssetStatusLabel(status?: string | null): string {
  if (!status) return '—'
  return AGENT_ASSET_STATUS_LABELS[status] ?? status
}

/**
 * 获取发布审批状态的中文展示文案,未知状态回退为原值
 * @param status - 后端审批状态原始值
 * @returns 用户可读的中文状态文案
 */
export function agentApprovalStatusLabel(status?: string | null): string {
  if (!status) return '—'
  return AGENT_APPROVAL_STATUS_LABELS[status] ?? status
}

/**
 * MCP Server / Sandbox Worker 健康状态 → 中文标签
 */
export const MCP_HEALTH_STATUS_LABELS: Record<string, string> = {
  healthy: '健康',
  unhealthy: '异常',
  blocked: '已阻断',
  unknown: '未知',
  registered: '已登记',
  credential_required: '待配置凭据',
  disabled: '已停用',
}

/**
 * 获取 MCP 服务/沙箱节点健康状态的中文展示文案,未知状态回退为原值
 * @param status - 后端健康状态原始值
 * @returns 用户可读的中文状态文案
 */
export function mcpHealthStatusLabel(status?: string | null): string {
  if (!status) return '—'
  return MCP_HEALTH_STATUS_LABELS[status] ?? status
}
