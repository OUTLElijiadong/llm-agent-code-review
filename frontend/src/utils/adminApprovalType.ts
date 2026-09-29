export const APPROVAL_TYPES = [
  { name: 'agent-release', label: 'Agent 发布' },
  { name: 'execution', label: '执行与其他审批' },
  { name: 'rules', label: '规则提案' },
] as const

export type ApprovalType = (typeof APPROVAL_TYPES)[number]['name']

export function resolveApprovalType(section: unknown, requestedType: unknown): ApprovalType {
  const requested = String(requestedType || '')
  if (APPROVAL_TYPES.some((item) => item.name === requested)) return requested as ApprovalType
  if (section === 'evolution') return 'rules'
  if (section === 'releases') return 'agent-release'
  return 'execution'
}
