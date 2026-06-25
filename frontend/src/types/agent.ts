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

// === v2.4 MetaGPT 编排层 ===

/**
 * MetaGPT 模块信息,对齐后端 GET /api/agents/metagpt/info 返回结构
 * 用于前端展示多 Agent 宏观调控能力面板
 */
export interface MetaGPTInfoOut {
  /** 模块版本(如 "v2.4") */
  version: string
  /** 模块描述 */
  description: string
  /** 核心组件说明(Environment/Role/RoleAdapter/Message) */
  components: Record<string, string>
  /** 可用工厂函数说明 */
  factories: Record<string, string>
  /** 已注册可适配为 Role 的 Agent 列表 */
  adaptable_agents: MetaGPTAdaptableAgent[]
  /** 默认参与审查环境的 Agent name 列表 */
  default_review_agents: string[]
  /** 默认参与讨论环境的 Agent name 列表 */
  default_discussion_agents: string[]
}

/**
 * 可适配为 MetaGPT Role 的 Agent 元数据
 */
export interface MetaGPTAdaptableAgent {
  name: string
  description: string
  category: string
  icon: string
  color: string
}

/**
 * MetaGPT Environment 预览角色信息
 * 对齐后端 RoleAdapter.to_dict() + 补充字段
 */
export interface MetaGPTRoleInfo {
  /** 角色 code(等于 Agent name) */
  name: string
  /** 角色显示名 */
  profile: string
  /** 角色目标 */
  goal: string
  /** 角色约束 */
  constraints: string
  /** 角色当前状态(idle/thinking/acting/done/error) */
  state: string
  /** 本地记忆消息数 */
  memory_size: number
  /** 关联 Agent name */
  agent_name: string
  /** 关联 Agent 描述 */
  agent_description: string
  /** 反应时发出的 cause_by */
  react_action: string
  /** 订阅的 cause_by 列表(空列表表示接收所有消息) */
  watch_actions: string[]
  /** Agent 图标(补充字段) */
  agent_icon?: string
  /** Agent 颜色(补充字段) */
  agent_color?: string
  /** Agent 分类(补充字段) */
  agent_category?: string
}

/**
 * MetaGPT Environment 预览结果,对齐后端 GET /api/agents/metagpt/preview 返回结构
 */
export interface MetaGPTEnvironmentPreviewOut {
  /** 环境模式(review / discussion) */
  mode: string
  /** 环境名称 */
  env_name: string
  /** 追踪 ID */
  trace_id: string
  /** 最大对话深度 */
  max_depth: number
  /** 角色列表 */
  roles: MetaGPTRoleInfo[]
  /** 已注册 Agent 总数 */
  registered_agent_count: number
}
