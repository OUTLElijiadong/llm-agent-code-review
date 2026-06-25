export interface AgentProfileOut {
  code: string
  name: string
  focus: string
  issue_types: string[]
  instruction: string
  enabled: boolean
}

export interface AgentUsageOut {
  code: string
  name: string
  call_count: number
  success_count: number
  failed_count: number
  last_called_at?: string | null
}

export interface ReviewTypeMappingOut {
  review_type: string
  label: string
  agent_codes: string[]
}

export interface AgentOverviewOut {
  agents: AgentProfileOut[]
  type_mappings: ReviewTypeMappingOut[]
  usage: AgentUsageOut[]
}

// === v2.0 新增 ===

export type AgentStatus =
  | 'idle' | 'thinking' | 'working' | 'blocked' | 'error' | 'offline'

export interface AgentRuntimeOut {
  code: string
  name: string
  description: string
  icon: string
  color: string
  category: string
  skills: string[]
  status: AgentStatus
  model: string
  call_count: number
  success_count: number
  failed_count: number
  last_called_at?: string | null
}

export interface AgentRuntimeSummaryOut {
  total: number
  by_category: { category: string; count: number }[]
}

export interface AgentSituationOut {
  online: number
  working: number
  idle: number
  today_calls: number
  spectrum: { bucket: string; count: number }[]
  hotspots: { code: string; name: string; count: number }[]
}

// === v3.0 AgentSkill 升级新增 ===

/**
 * Skill 类型枚举,与后端 BaseSkill.SkillType 对齐
 * - self_improvement: 自我进化类 Skill(沉淀经验/蒸馏规则/评估闸门)
 * - proactive: 主动监测类 Skill(主动巡检/发现潜在问题)
 */
export type SkillType = 'self_improvement' | 'proactive'

/**
 * Skill 元数据,对齐后端 SkillMetaOut schema
 * 由 GET /api/agents/{agent_name}/skills 返回
 */
export interface SkillMetaOut {
  /** Skill 唯一名称,如 code_reviewer.self_improve */
  name: string
  /** Skill 描述(中文,供前端展示) */
  description: string
  /** Skill 类型(self_improvement / proactive) */
  type: SkillType
  /** 是否可手动调用(invocable=true 的 Skill 才会出现在手动调用列表) */
  invocable: boolean
  /** 所属 Agent name(如 code_reviewer) */
  agent_name: string
}

/**
 * Skill 调用入参,对齐后端 SkillInvokeIn schema
 * 用于 POST /api/agents/{agent_name}/skills/{skill_name}/invoke
 */
export interface SkillInvokeIn {
  /** 可选 action(如 evolve / check_proactive),留空则由 Skill 默认 action */
  action?: string
  /** Skill 参数 dict(各 Skill 自定义) */
  params?: Record<string, unknown>
}

/**
 * Skill 调用出参,对齐后端 SkillInvokeOut schema
 */
export interface SkillInvokeOut {
  /** 是否成功 */
  success: boolean
  /** Skill 返回数据(各 Skill 自定义结构) */
  data?: Record<string, unknown> | unknown
  /** 错误信息(success=false 时填充) */
  error?: string
  /** 效果标记(success / no_change / failed,与 agent_skill_record.effect 对齐) */
  effect?: string
  /** 执行耗时(毫秒) */
  duration_ms?: number
  /** agent_skill_record 主键(可用于查询调用记录详情) */
  record_id?: number
}

/**
 * Skill 调用记录,对齐后端 AgentSkillRecordOut schema
 * 由 GET /api/agents/skill-records 返回
 */
export interface AgentSkillRecordOut {
  /** 记录主键 */
  id: number
  /** Agent name(如 code_reviewer) */
  agent_name: string
  /** Skill name(如 code_reviewer.self_improve) */
  skill_name: string
  /** 触发类型(manual / scheduled / event / orchestrator) */
  trigger_type: string
  /** 触发来源描述(如 "api:POST /agents/.../invoke") */
  trigger_source?: string | null
  /** 效果标记(success / no_change / failed) */
  effect?: string | null
  /** 是否成功(由 effect === 'success' 派生) */
  success: boolean
  /** 执行耗时(毫秒) */
  duration_ms?: number | null
  /** 输出摘要(Skill 返回数据的精简描述) */
  output_summary?: string | null
  /** 创建时间(ISO8601 字符串) */
  create_time?: string | null
}
