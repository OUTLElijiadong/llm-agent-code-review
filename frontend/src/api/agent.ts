import { get, post } from './http'
import type {
  AgentOverviewOut,
  AgentProfileOut,
  AgentRuntimeOut,
  AgentRuntimeSummaryOut,
  AgentSituationOut,
  AgentSkillRecordOut,
  AgentUsageOut,
  ReviewTypeMappingOut,
  SkillInvokeIn,
  SkillInvokeOut,
  SkillMetaOut,
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

// =================== v3.0 AgentSkill 升级:Skill 管理 API ===================

/**
 * 列出指定 Agent 挂载的所有 Skill 元数据
 *
 * 调用 GET /api/agents/{agent_name}/skills,返回该 Agent 的
 * self_improvement + proactive 两类 Skill 元数据列表,
 * 供前端展示每个 Agent 的自进化与主动监测能力。
 *
 * @param agentName - Agent name(如 code_reviewer)
 * @returns SkillMetaOut[] Skill 元数据列表
 */
export function listAgentSkills(agentName: string): Promise<SkillMetaOut[]> {
  return get<SkillMetaOut[]>(`/agents/${encodeURIComponent(agentName)}/skills`)
}

/**
 * 手动调用指定 Agent 的指定 Skill(admin only)
 *
 * 调用 POST /api/agents/{agent_name}/skills/{skill_name}/invoke,
 * 触发类型为 manual,自动写 agent_skill_record 与 audit_log。
 *
 * @param agentName - Agent name(如 code_reviewer)
 * @param skillName - Skill name(如 code_reviewer.self_improve)
 * @param payload - SkillInvokeIn 请求体,含 action 与 params
 * @returns SkillInvokeOut 调用结果,含 success/data/effect/duration_ms/record_id
 */
export function invokeAgentSkill(
  agentName: string,
  skillName: string,
  payload: SkillInvokeIn,
): Promise<SkillInvokeOut> {
  return post<SkillInvokeOut>(
    `/agents/${encodeURIComponent(agentName)}/skills/${encodeURIComponent(skillName)}/invoke`,
    payload,
  )
}

/**
 * 查询 Skill 调用记录(admin only)
 *
 * 调用 GET /api/agents/skill-records,支持按 agent_name / skill_name / trigger_type
 * 过滤,返回最近的 Skill 调用记录列表,用于 Skill 管理页面的记录展示。
 *
 * @param params - 过滤参数
 * @param params.agentName - 按 Agent name 过滤(可选)
 * @param params.skillName - 按 Skill name 过滤(可选)
 * @param params.triggerType - 按触发类型过滤(manual/scheduled/event/orchestrator,可选)
 * @param params.limit - 返回条数上限,默认 10
 * @returns AgentSkillRecordOut[] Skill 调用记录列表(按 create_time 倒序)
 */
export function listSkillRecords(params: {
  agentName?: string
  skillName?: string
  triggerType?: string
  limit?: number
} = {}): Promise<AgentSkillRecordOut[]> {
  return get<AgentSkillRecordOut[]>('/agents/skill-records', {
    agent_name: params.agentName,
    skill_name: params.skillName,
    trigger_type: params.triggerType,
    limit: params.limit,
  })
}
