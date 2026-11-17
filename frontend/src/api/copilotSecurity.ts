/**
 * 小菱安全中心 API 封装
 * 统一前缀 /admin/copilot-security（http 实例自带 /api baseURL）。
 * 读与演练需管理员 + security:view；写仅超级管理员，页面侧另有按钮禁用。
 */
import { get, post, put } from './http'
import type {
  CopilotSecurityCenterOut,
  SecOpsBaselineOut,
  SecOpsBlockOut,
  SecOpsCandidatesOut,
  SecOpsDigestOut,
  SecOpsPatrolOut,
  CopilotSecurityEventListOut,
  CopilotSecurityEventQuery,
  CopilotSecurityKillSwitchIn,
  CopilotSecurityPolicyIn,
  CopilotSecurityPolicyOut,
  CopilotSecurityPolicySavedOut,
  CopilotSecurityPostureOut,
  CopilotSecurityPresetIn,
  CopilotSecurityScanIn,
  CopilotSecurityScanOut,
} from '@/types/copilotSecurity'

const BASE = '/admin/copilot-security'

/** 一次拿全：当前策略 + 态势 + 预设目录 + 事件类型目录。 */
export function getCopilotSecurityCenter(): Promise<CopilotSecurityCenterOut> {
  return get<CopilotSecurityCenterOut>(`${BASE}/center`)
}

/** 读取当前生效策略。 */
export function getCopilotSecurityPolicy(): Promise<CopilotSecurityPolicyOut> {
  return get<CopilotSecurityPolicyOut>(`${BASE}/policy`)
}

/** 局部更新策略：只提交被改动的字段，未提交的保持原值。 */
export function updateCopilotSecurityPolicy(
  patch: CopilotSecurityPolicyIn,
): Promise<CopilotSecurityPolicySavedOut> {
  return put<CopilotSecurityPolicySavedOut>(`${BASE}/policy`, patch)
}

/** 套用预设；切到 max 必须 confirm=true。 */
export function applyCopilotSecurityPreset(
  payload: CopilotSecurityPresetIn,
): Promise<CopilotSecurityPolicySavedOut> {
  return post<CopilotSecurityPolicySavedOut>(`${BASE}/policy/preset`, payload)
}

/** 紧急停机开关。 */
export function toggleCopilotSecurityKillSwitch(
  payload: CopilotSecurityKillSwitchIn,
): Promise<CopilotSecurityPolicySavedOut> {
  return post<CopilotSecurityPolicySavedOut>(`${BASE}/kill-switch`, payload)
}

/** 态势评分与整改建议。 */
export function getCopilotSecurityPosture(): Promise<CopilotSecurityPostureOut> {
  return get<CopilotSecurityPostureOut>(`${BASE}/posture`)
}

/** 注入检测演练：不落库、不拦截。 */
export function scanCopilotSecurityText(
  payload: CopilotSecurityScanIn,
): Promise<CopilotSecurityScanOut> {
  return post<CopilotSecurityScanOut>(`${BASE}/scan`, payload)
}

/** 分页查询安全事件。 */
export function listCopilotSecurityEvents(
  query: CopilotSecurityEventQuery,
): Promise<CopilotSecurityEventListOut> {
  return get<CopilotSecurityEventListOut>(`${BASE}/events`, query)
}

// ── 小菱安全运营（自动化攻防）────────────────────────────────────────────────

/** 多源攻击研判 */
export function getSecOpsDigest(hours = 24, limit = 200): Promise<SecOpsDigestOut> {
  return get<SecOpsDigestOut>('/admin/secops/digest', { hours, limit })
}

/** 自身攻击面盘点 */
export function getSecOpsSurface(): Promise<{ available: boolean; surface: Record<string, unknown> }> {
  return get<{ available: boolean; surface: Record<string, unknown> }>('/admin/secops/surface')
}

/** 自动封禁候选 */
export function getSecOpsCandidates(): Promise<SecOpsCandidatesOut> {
  return get<SecOpsCandidatesOut>('/admin/secops/candidates')
}

/** 自身安全基线 */
export function getSecOpsBaseline(): Promise<SecOpsBaselineOut> {
  return get<SecOpsBaselineOut>('/admin/secops/baseline')
}

/** 巡检状态 */
export function getSecOpsPatrol(): Promise<SecOpsPatrolOut> {
  return get<SecOpsPatrolOut>('/admin/secops/patrol')
}

/** 封禁一个攻击源（需超级管理员 + confirm） */
export function blockSecOpsSource(payload: {
  ip: string
  reason: string
  duration_seconds: number
  confirm: boolean
}): Promise<SecOpsBlockOut> {
  return post<SecOpsBlockOut>('/admin/secops/block', payload)
}

/** 解封一个来源 */
export function releaseSecOpsSource(payload: { ip: string; reason: string }): Promise<SecOpsBlockOut> {
  return post<SecOpsBlockOut>('/admin/secops/release', payload)
}
