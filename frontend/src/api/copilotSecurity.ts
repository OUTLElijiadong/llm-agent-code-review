/**
 * 小菱安全中心 API 封装
 * 统一前缀 /admin/copilot-security（http 实例自带 /api baseURL）。
 * 读与演练需管理员 + security:view；写仅超级管理员，页面侧另有按钮禁用。
 */
import { get, post, put } from './http'
import type {
  CopilotSecurityCenterOut,
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
