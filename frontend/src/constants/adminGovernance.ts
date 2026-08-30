/**
 * 管理端治理域中文标签(红线: 界面枚举必须中文, 集中在此)。
 *
 * 覆盖 任务调度(job_code/job_type/agent_code) 与 策略中心(subject/action/resource)
 * 的数据值汉化; 未命中映射时回退显示原始编码并保留 title 提示, 不丢信息。
 */

/** Agent 编码 → 中文名(注册中心 BaseAgent + 治理默认 Agent 全量) */
export const AGENT_CODE_LABELS: Record<string, string> = {
  orchestrator: '总编排',
  chat_assistant: '小菱(对话助手)',
  manager: '贾维斯(全局运维)',
  review_orchestrator: '审查编排',
  code_reviewer: '代码审查员',
  security_sentinel: '安全哨兵',
  language_detector: '语言检测',
  project_analyzer: '项目分析',
  project_manager: '项目管理',
  code_file_manager: '代码文件管理',
  rule_manager: '规则管理',
  reporter: '报告员',
  dashboard: '仪表盘',
  ai_prompt: 'AI 提示词',
  evolution: '自进化',
  reflection: '自我反思',
  knowledge_distiller: '知识蒸馏',
  test_verifier: '测试验证',
  quality_evaluator: '质量评估',
  approval: '审批',
  policy: '安全策略',
  scheduler: '调度',
  memory_manager: '记忆管理',
  monitor: '监控',
  alert: '告警',
  operations: '全服管理',
}

/** 调度任务类型 → 中文 */
export const JOB_TYPE_LABELS: Record<string, string> = {
  crawl: '知识爬取',
  distill: '知识蒸馏',
  reflection: '自我反思',
  evolution: '自进化',
  skill_evolution: '技能进化',
  manual: '手动触发',
}

/** 常见调度任务编码 → 中文名(未命中走规则化推导) */
const JOB_CODE_LABELS: Record<string, string> = {
  daily_agent_knowledge_crawl: '每日·Agent 知识爬取',
  daily_agent_reflection: '每日·Agent 自我反思',
  daily_agent_evolution: '每日·Agent 自进化',
}

export function jobCodeText(code: string | null | undefined): string {
  const raw = String(code || '').trim()
  if (!raw) return '—'
  if (JOB_CODE_LABELS[raw]) return JOB_CODE_LABELS[raw]
  if (raw.startsWith('daily_skill_evolution_')) {
    const agent = raw.slice('daily_skill_evolution_'.length)
    return `每日·技能进化·${agentCodeText(agent)}`
  }
  return raw.replace(/^daily_/, '每日·').replace(/_/g, ' ')
}

export function jobTypeText(value: string | null | undefined): string {
  const raw = String(value || '').trim()
  return JOB_TYPE_LABELS[raw] || raw || '—'
}

export function agentCodeText(code: string | null | undefined): string {
  const raw = String(code || '').trim()
  if (!raw) return '—'
  if (AGENT_CODE_LABELS[raw]) return AGENT_CODE_LABELS[raw]
  if (raw.startsWith('agent:')) return `Agent·${agentCodeText(raw.slice(6))}`
  return raw
}

/** 策略动作 → 中文(点分命名空间规则化) */
const POLICY_ACTION_LABELS: Record<string, string> = {
  '*': '全部动作',
  'knowledge.read': '读取知识',
  'knowledge.write': '写入知识',
  'artifact.publish': '发布产物',
  'shell.exec': '执行命令',
  'project.read': '读取项目',
  'project.write': '修改项目',
  'file.read': '读取文件',
  'file.write': '修改文件',
  'user.manage': '管理用户',
  'config.write': '修改配置',
  'memory.write': '写入记忆',
  'agent.publish': '发布 Agent',
  'mcp.call': '调用外部工具',
}

const ACTION_NAMESPACE_LABELS: Record<string, string> = {
  knowledge: '知识', artifact: '产物', shell: '命令', project: '项目',
  file: '文件', user: '用户', config: '配置', memory: '记忆',
  agent: 'Agent', mcp: '外部工具', report: '报告', sandbox: '沙箱',
  remote: '外部请求', pentest: '渗透测试', review: '审查',
}

export function policyActionText(value: string | null | undefined): string {
  const raw = String(value || '').trim()
  if (!raw) return '—'
  if (POLICY_ACTION_LABELS[raw]) return POLICY_ACTION_LABELS[raw]
  const [ns, verb] = raw.split('.')
  const nsLabel = ACTION_NAMESPACE_LABELS[ns] || ns
  const verbLabel: Record<string, string> = {
    read: '读取', write: '写入', exec: '执行', publish: '发布',
    manage: '管理', call: '调用', delete: '删除', list: '列举', start: '启动', stop: '停止',
  }
  if (verb && verbLabel[verb]) return `${verbLabel[verb]}${nsLabel}`
  return raw
}

/** 策略主体 → 中文 */
export function policySubjectText(value: string | null | undefined): string {
  const raw = String(value || '').trim()
  if (!raw) return '—'
  if (raw === '*') return '全部主体'
  if (raw === 'agent:*') return '全部 Agent'
  if (raw === 'user:*') return '全部用户'
  if (raw.startsWith('agent:')) return `Agent·${agentCodeText(raw.slice(6))}`
  if (raw.startsWith('user:')) return `用户 ${raw.slice(5)}`
  return raw
}

/** 策略资源 → 中文(前缀命名空间规则化) */
const RESOURCE_PREFIX_LABELS: Array<[string, string]> = [
  ['kb:', '知识库'], ['knowledge:', '知识'], ['project:', '项目'], ['file:', '文件'],
  ['report:', '报告'], ['sandbox:', '沙箱'], ['env:', '沙箱环境'], ['team:', 'Agent 团队'],
  ['mesh:', '消息网'], ['user:', '用户'], ['config:', '配置'], ['memory:', '记忆'],
]

export function policyResourceText(value: string | null | undefined): string {
  const raw = String(value || '').trim()
  if (!raw) return '—'
  if (raw === '*') return '全部资源'
  for (const [prefix, label] of RESOURCE_PREFIX_LABELS) {
    if (raw.startsWith(prefix)) return `${label} ${raw.slice(prefix.length)}`.trim()
  }
  return raw
}

/** 工具调用日志: 工具编码 → 中文 */
export const TOOL_CODE_LABELS: Record<string, string> = {
  shell: '命令执行', ops_execute: '运维操作', admin_execute_operation: '服务器运维操作',
  knowledge_read: '读取知识', knowledge_write: '写入知识', shell_exec: '执行命令',
}

export function toolCodeText(code: string | null | undefined): string {
  const raw = String(code || '').trim()
  if (!raw) return '—'
  return TOOL_CODE_LABELS[raw] || policyActionText(raw)
}

/** 知识来源类型 → 中文 */
export const SOURCE_TYPE_LABELS: Record<string, string> = {
  manual: '手动录入', inline: '内联', url: '链接', official: '官方文档',
  docs: '官方文档', github: 'GitHub', project: '项目',
}

export function sourceTypeText(value: string | null | undefined): string {
  const raw = String(value || '').trim()
  return SOURCE_TYPE_LABELS[raw] || raw || '—'
}

/** 记忆类型 → 中文 */
export const MEMORY_TYPE_LABELS: Record<string, string> = {
  long_term: '长期记忆', short_term: '短期记忆', reflection: '反思',
}

export function memoryTypeText(value: string | null | undefined): string {
  const raw = String(value || '').trim()
  return MEMORY_TYPE_LABELS[raw] || raw || '—'
}

/** Agent 分类 → 中文 */
export const CATEGORY_LABELS: Record<string, string> = {
  meta: '主控', frontline: '前台', governance: '治理', operations: '运维',
  security: '安全', knowledge: '知识', quality: '质量', general: '通用',
}

export function categoryText(value: string | null | undefined): string {
  const raw = String(value || '').trim()
  return CATEGORY_LABELS[raw] || raw || '—'
}

/** 产物类型 → 中文(版本回退) */
export const ARTIFACT_TYPE_LABELS: Record<string, string> = {
  policy: '策略', prompt: '提示词', skill: '技能', knowledge: '知识', code: '代码',
}

export function artifactTypeText(value: string | null | undefined): string {
  const raw = String(value || '').trim()
  return ARTIFACT_TYPE_LABELS[raw] || raw || '—'
}

/** 决策/处理方式 → 中文(allow/deny/escalate 等) */
const DECISION_LABELS: Record<string, string> = {
  allow: '允许', deny: '阻断', deny_all: '全部阻断', escalate: '升级审批',
  permitted: '允许', forbidden: '阻断', ask: '询问后执行',
}

export function decisionText(value: string | null | undefined): string {
  const raw = String(value || '').trim()
  return DECISION_LABELS[raw] || raw || '—'
}
