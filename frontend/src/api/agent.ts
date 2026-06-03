import { get } from './http'
import type {
  AgentOverviewOut,
  AgentProfileOut,
  AgentRuntimeOut,
  AgentRuntimeSummaryOut,
  AgentSituationOut,
  AgentUsageOut,
  ReviewTypeMappingOut,
} from '@/types/agent'

/**
 * 列出所有代理画像
 */
export function listAgents(): Promise<AgentProfileOut[]> {
  return get<AgentProfileOut[]>('/agents')
}

/**
 * 列出审查类型 → 代理组合映射
 */
export function listTypeMappings(): Promise<ReviewTypeMappingOut[]> {
  return get<ReviewTypeMappingOut[]>('/agents/type-mappings')
}

/**
 * 每个代理的调用统计
 */
export function getUsage(): Promise<AgentUsageOut[]> {
  return get<AgentUsageOut[]>('/agents/usage')
}

/**
 * 一次性返回 Agent 中心首屏数据 (v1.0 兼容)
 */
export function getOverview(): Promise<AgentOverviewOut> {
  return get<AgentOverviewOut>('/agents/overview')
}

/**
 * v2.0 真实注册的 Agent 运行时清单
 *
 * @returns AgentRuntimeOut[] 与 AgentRegistry 严格同步的 Agent 元数据列表
 */
export function listRuntimeAgents(): Promise<AgentRuntimeOut[]> {
  return get<AgentRuntimeOut[]>('/agents/runtime')
}

/**
 * v2.0 注册中心汇总: 总数 + category 分桶
 */
export function getRuntimeSummary(): Promise<AgentRuntimeSummaryOut> {
  return get<AgentRuntimeSummaryOut>('/agents/runtime/summary')
}

/**
 * v2.0 态势感知面板数据
 *
 * @param minutes - 调用波形覆盖的最近 N 分钟,默认 60
 */
export function getSituation(minutes = 60): Promise<AgentSituationOut> {
  return get<AgentSituationOut>('/agents/situation', { minutes })
}
