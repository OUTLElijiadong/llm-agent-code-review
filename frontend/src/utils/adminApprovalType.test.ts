import { describe, expect, it } from 'vitest'
import { APPROVAL_TYPES, resolveApprovalType } from './adminApprovalType'

describe('管理员审批类型入口', () => {
  it('把历史发布审批、通用审批和自进化路径映射到明确类型', () => {
    expect(resolveApprovalType('releases', undefined)).toBe('agent-release')
    expect(resolveApprovalType('approvals', undefined)).toBe('execution')
    expect(resolveApprovalType('evolution', undefined)).toBe('rules')
  })

  it('保留当前审批类型参数并提供清楚的分类文案', () => {
    expect(resolveApprovalType('approvals', 'rules')).toBe('rules')
    expect(APPROVAL_TYPES.map((item) => item.label)).toEqual(['Agent 发布', '执行与其他审批', '规则提案'])
  })
})
