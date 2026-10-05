<script setup lang="ts">
import EmptyState from '@/components/common/EmptyState.vue'
import { useAgentChatScope } from '@/composables/useAgentChatScope'
import { confirmDanger } from '@/composables/useDangerConfirm'
import { isScheduleValid } from '@/utils/cronValidate'
import { taskDisplayTitle } from '@/utils/taskDisplayTitle'
import { useRouter } from 'vue-router'
const router = useRouter()
import { computed, onMounted, ref, watch } from 'vue'
import { ElMessage } from 'element-plus/es/components/message/index'
import { ElMessageBox } from 'element-plus/es/components/message-box/index'

import {
  activateAgentKnowledgeDoc,
  approveItem,
  createAgentKnowledgeDoc,
  createAgentMemory,
  createArtifactVersion,
  createRewardEvent,
  crawlAgentKnowledgeSources,
  evaluatePolicy,
  getGovernanceOverview,
  getObservabilityOverview,
  listAgentKnowledge,
  listAgentKnowledgeSources,
  listAgentMemory,
  listAlertsPage,
  listArtifactVersions,
  listApprovals,
  listGovernanceAgents,
  listJobs,
  listPolicies,
  listPolicyDecisions,
  listRewardEvents,
  listToolCalls,
  listToolPermissions,
  rejectItem,
  resolveAlert,
  rollbackArtifactVersion,
  runJob,
  updateJob,
  upsertAgentKnowledgeSource,
  upsertPolicy,
  upsertToolPermission,
} from '@/api/adminGovernance'
import type {
  AgentAlert,
  AgentAlertPage,
  AgentArtifactVersion,
  AgentJob,
  AgentKnowledgeDoc,
  AgentKnowledgeSource,
  AgentMemory,
  AgentRewardEvent,
  AgentToolPermission,
  ApprovalItem,
  GovernanceAgent,
  GovernanceOverview,
  PolicyDecision,
  PolicyRule,
  ToolCallLog,
} from '@/types/adminGovernance'
import { useUserStore } from '@/stores/user'

type Mode = 'overview' | 'agents' | 'approvals' | 'policies' | 'tools' | 'knowledge' | 'jobs' | 'observability' | 'rewards' | 'rollback'

const props = defineProps<{
  mode: Mode
}>()
const userStore = useUserStore()
const approvalScope = useAgentChatScope(() => userStore.profile?.id, () => userStore.token, () => '')

const loading = ref(false)
const overview = ref<GovernanceOverview | null>(null)
const agents = ref<GovernanceAgent[]>([])
const showInternalAgents = ref(false)
const INTERNAL_AGENT_CODES = new Set(['manager', 'orchestrator'])
const visibleAgents = computed(() => showInternalAgents.value
  ? agents.value
  : agents.value.filter((agent) => !INTERNAL_AGENT_CODES.has(agent.code)))
const hiddenInternalAgentCount = computed(() => agents.value.filter((agent) => INTERNAL_AGENT_CODES.has(agent.code)).length)
const agentsLoaded = ref(false)
const agentLoadError = ref('')
const expandedAgentCodes = ref(new Set<string>())

function toggleAgentDetails(code: string): void {
  if (expandedAgentCodes.value.has(code)) expandedAgentCodes.value.delete(code)
  else expandedAgentCodes.value.add(code)
}
const approvals = ref<ApprovalItem[]>([])
const selectedApproval = ref<ApprovalItem | null>(null)
const approvalDetailsVisible = ref(false)
const approvalLoadError = ref('')
let approvalRequestVersion = 0
let dataRequestVersion = 0
const policies = ref<PolicyRule[]>([])
const decisions = ref<PolicyDecision[]>([])
const tools = ref<ToolCallLog[]>([])
const toolPermissions = ref<AgentToolPermission[]>([])
const jobs = ref<AgentJob[]>([])
const alerts = ref<AgentAlert[]>([])
const alertPage = ref(1)
const alertPageSize = ref(20)
const alertTotal = ref<number | null>(null)
const alertLoadError = ref('')
const observability = ref<Record<string, unknown>>({})
const selectedAgent = ref('')
const agentMemory = ref<AgentMemory[]>([])
const agentKnowledge = ref<AgentKnowledgeDoc[]>([])
const knowledgeSources = ref<AgentKnowledgeSource[]>([])
const rewardEvents = ref<AgentRewardEvent[]>([])
const artifactVersions = ref<AgentArtifactVersion[]>([])
const canManageKnowledgeSources = computed(() => userStore.isSuperAdmin())
const policyForm = ref({ subject: 'agent:manager', action: 'knowledge.read', resource: 'agent:manager' })
const policyResult = ref<PolicyDecision | null>(null)
const policyEditor = ref({
  rule_code: 'agent_custom_rule',
  name: '自定义策略',
  subject: 'agent:*',
  action: 'knowledge.read',
  resource: '*',
  effect: 'allow',
  risk_level: 'low',
  priority: 100,
  enabled: 1,
})
const toolPermissionForm = ref({
  agent_code: 'manager',
  tool_code: 'shell',
  permission: 'escalate',
  risk_level: 'high',
  enabled: 1,
  note: '',
})
const memoryForm = ref({ title: '', content: '', memory_type: 'long_term', weight: 1 })
const knowledgeDocForm = ref({
  title: '',
  content: '',
  source_type: 'manual',
  risk_level: 'low',
  confidence: 1,
})
const knowledgeSourceForm = ref({
  source_type: 'inline',
  source_uri: '',
  whitelist: 1,
  enabled: 1,
  config_content: '',
})
const jobEdit = ref<Record<number, { schedule: string; status: string }>>({})
const jobSaving = ref<Record<number, boolean>>({})
const jobSaveErrors = ref<Record<number, string>>({})

/** 统计卡入口:跳到对应治理工作台。 */
function goMetric(route: string): void {
  void router.push(route)
}


/** 当前行的 cron 是否合法(空=未编辑不算错)。 */
function scheduleInvalid(rowId: number): boolean {
  const edit = jobEdit.value[rowId]
  if (!edit || !edit.schedule) return false
  return !isScheduleValid(edit.schedule)
}


const rewardForm = ref({ agent_code: 'manager', event_type: 'reward', score: 1, reason: '' })
const artifactForm = ref({
  agent_code: 'policy',
  artifact_type: 'policy',
  version: '',
  content: '',
  snapshot: '',
  status: 'draft',
})

const pageTitle = computed(() => {
  const titles: Record<Mode, string> = {
    overview: '运行总览',
    agents: 'Agent 团队',
    approvals: '待办审批',
    policies: '策略与权限',
    tools: '工具使用',
    knowledge: '知识沉淀',
    jobs: '任务计划',
    observability: '系统观测',
    rewards: '学习效果',
    rollback: '版本回退',
  }
  return titles[props.mode]
})

const pageSubtitle = computed(() => {
  const subtitles: Record<Mode, string> = {
    overview: '先看运行状况、待办和风险，再进入具体处理。',
    agents: '查看每个 Agent 的职责、能力、知识和运行优先级。',
    approvals: '只保留需要人工确认的高风险与危急事项。',
    policies: '用业务动作描述规则；内部编码仅用于高级配置。',
    tools: '查看工具实际执行结果，并控制高风险能力。',
    knowledge: '每条知识都保留来源、风险等级和生效状态。',
    jobs: '查看自动任务的计划和最近一次执行，不需要理解调度代码。',
    observability: '用执行结果、审批和风险分布解释系统状态。',
    rewards: '基于实际任务结果调整 Agent 的优先级和阈值。',
    rollback: '每次变更都有版本和快照，必要时可以恢复。',
  }
  return subtitles[props.mode]
})

import {
  CATEGORY_LABELS,
  agentCodeText,
  artifactTypeText,
  categoryText,
  decisionText,
  jobCodeText,
  jobScheduleText,
  jobTypeText,
  memoryTypeText,
  policyActionText,
  policyResourceText,
  policySubjectText,
  sourceTypeText,
  toolCodeText,
} from '@/constants/adminGovernance'
function agentCategoryText(value: unknown): string {
  if (value === null || value === undefined) return '未提供分类'
  if (typeof value !== 'string') return '分类格式异常'
  const category = value.trim()
  if (!category) return '未提供分类'
  return Object.prototype.hasOwnProperty.call(CATEGORY_LABELS, category)
    ? CATEGORY_LABELS[category]
    : `未知分类（${category}）`
}

function statusText(value: string | number | null | undefined): string {
  const labels: Record<string, string> = {
    active: '生效', idle: '空闲', working: '运行中', disabled: '已停用', error: '异常',
    enabled: '已启用', pending: '待处理', approved: '已通过', rejected: '已驳回',
    auto_approved: '自动通过', success: '成功', failed: '失败', denied: '已阻断',
    escalated: '等待审批', draft: '草稿', gray: '灰度中', stable: '稳定', rolled_back: '已回退',
    pending_approval: '等待审批', allow: '允许', deny: '阻断', escalate: '升级审批',
  }
  return labels[String(value)] || String(value || '-')
}

function riskText(value: string | null | undefined): string {
  return ({ low: '低风险', medium: '中风险', high: '高风险', critical: '危急风险' } as Record<string, string>)[value || ''] || (value || '-')
}

function riskTagType(value: string | null | undefined): 'danger' | 'warning' {
  return value === 'high' || value === 'critical' ? 'danger' : 'warning'
}

function approvalTitle(value: string | null | undefined): string {
  return taskDisplayTitle(String(value || '-').replace(/^Responses Agent 请求执行\s*/, ''), '-')
}

function alertSeverityText(value: string | null | undefined): string {
  return ({ info: '提示', warning: '警告', high: '高等级', critical: '危急' } as Record<string, string>)[value || ''] || (value || '-')
}

function agentBoundaryText(agent: GovernanceAgent): string {
  const config = agent.config_json
  const boundary = config && !Array.isArray(config) && typeof config === 'object'
    ? (config as Record<string, unknown>).governance_boundary
    : null
  if (!boundary || typeof boundary !== 'object' || Array.isArray(boundary)) return '未配置（兼容模式）'
  const scope = String((boundary as Record<string, unknown>).scope || '职责域')
  const allowed = Array.isArray((boundary as Record<string, unknown>).allowed_tools)
    ? ((boundary as Record<string, unknown>).allowed_tools as unknown[]).length
    : 0
  const approval = Array.isArray((boundary as Record<string, unknown>).approval_tools)
    ? ((boundary as Record<string, unknown>).approval_tools as unknown[]).length
    : 0
  return `${scope} · 允许 ${allowed} 项 · 审批 ${approval} 项 · 其余拒绝`
}

function agentDisplayName(agent: GovernanceAgent): string {
  const label = agentCodeText(agent.code)
  return label === agent.code ? agent.name : label
}

const observabilityCards = computed(() => [
  { label: '当前开放告警', value: alertTotal.value ?? '—' },
  { label: '调度执行次数（累计）', value: Number(observability.value.job_runs || 0) },
  { label: '累计奖惩分', value: Number(observability.value.reward_score_total || 0) },
  { label: '工具结果分类数', value: Array.isArray(observability.value.tool_status) ? observability.value.tool_status.length : 0 },
])

/**
 * 加载当前模式所需的管理端数据。
 * @returns Promise<void>
 */
async function loadData(): Promise<void> {
  const requestMode = props.mode
  const isAgentsRequest = requestMode === 'agents'
  const isApprovalsRequest = requestMode === 'approvals'
  if (isAgentsRequest && loading.value) return
  const dataVersion = ++dataRequestVersion
  const accountIsCurrent = isApprovalsRequest ? approvalScope.captureAccount() : () => true
  const requestVersion = isApprovalsRequest ? ++approvalRequestVersion : 0
  const requestIsCurrent = () => (
    dataVersion === dataRequestVersion &&
    props.mode === requestMode &&
    accountIsCurrent() &&
    (!isApprovalsRequest || requestVersion === approvalRequestVersion)
  )
  loading.value = true
  if (isAgentsRequest) agentLoadError.value = ''
  if (isApprovalsRequest) approvalLoadError.value = ''
  if (requestMode === 'observability') alertLoadError.value = ''
  try {
    if (requestMode === 'overview') {
      const nextOverview = await getGovernanceOverview()
      if (!requestIsCurrent()) return
      overview.value = nextOverview
      const nextAgents = await listGovernanceAgents()
      if (!requestIsCurrent()) return
      agents.value = nextAgents
      const nextApprovals = await listApprovals('pending', 'agent_package.publish')
      if (!requestIsCurrent()) return
      approvals.value = nextApprovals
    } else if (requestMode === 'agents') {
      const nextAgents = await listGovernanceAgents()
      if (!requestIsCurrent()) return
      agents.value = nextAgents
      agentsLoaded.value = true
    } else if (isApprovalsRequest) {
      const items = await listApprovals('pending', 'agent_package.publish')
      if (requestIsCurrent()) approvals.value = items
    } else if (requestMode === 'policies') {
      const nextPolicies = await listPolicies()
      if (!requestIsCurrent()) return
      policies.value = nextPolicies
      const nextDecisions = await listPolicyDecisions()
      if (!requestIsCurrent()) return
      decisions.value = nextDecisions
    } else if (requestMode === 'tools') {
      const [calls, permissions] = await Promise.all([listToolCalls(), listToolPermissions()])
      if (!requestIsCurrent()) return
      tools.value = calls
      toolPermissions.value = permissions
    } else if (requestMode === 'knowledge') {
      const nextAgents = await listGovernanceAgents()
      if (!requestIsCurrent()) return
      agents.value = nextAgents
      selectedAgent.value = selectedAgent.value || agents.value[0]?.code || ''
      await loadAgentKnowledge(requestIsCurrent)
    } else if (requestMode === 'jobs') {
      const nextJobs = await listJobs()
      if (!requestIsCurrent()) return
      jobs.value = nextJobs
      // 只初始化缺失行:用户正在编辑的 schedule 不被后台刷新静默覆盖
      for (const job of jobs.value) {
        if (!jobEdit.value[job.id]) {
          jobEdit.value[job.id] = { schedule: job.schedule, status: job.status }
        }
      }
    } else if (requestMode === 'observability') {
      const nextObservability = await getObservabilityOverview()
      if (!requestIsCurrent()) return
      observability.value = nextObservability
      let nextAlertsPage: AgentAlertPage = await listAlertsPage('open', alertPage.value, alertPageSize.value)
      while (requestIsCurrent() && nextAlertsPage.pages > 0 && alertPage.value > nextAlertsPage.pages) {
        alertPage.value = nextAlertsPage.pages
        nextAlertsPage = await listAlertsPage('open', alertPage.value, alertPageSize.value)
      }
      if (!requestIsCurrent()) return
      if (nextAlertsPage.pages === 0) alertPage.value = 1
      alerts.value = nextAlertsPage.items
      alertTotal.value = nextAlertsPage.total
    } else if (requestMode === 'rewards') {
      const nextObservability = await getObservabilityOverview()
      if (!requestIsCurrent()) return
      observability.value = nextObservability
      const nextRewardEvents = await listRewardEvents()
      if (!requestIsCurrent()) return
      rewardEvents.value = nextRewardEvents
    } else if (requestMode === 'rollback') {
      const nextVersions = await listArtifactVersions()
      if (!requestIsCurrent()) return
      artifactVersions.value = nextVersions
    }
  } catch (error) {
    if (!requestIsCurrent()) return
    const message = error instanceof Error
      ? error.message
      : error && typeof error === 'object' ? (error as { message?: unknown }).message : undefined
    const readable = typeof message === 'string' && message.trim() ? message.trim() : ''
    if (isAgentsRequest) agentLoadError.value = readable || 'Agent 列表加载失败，请刷新重试'
    else if (isApprovalsRequest) {
      approvals.value = []
      approvalLoadError.value = readable || '审批列表加载失败，请刷新重试'
    } else if (requestMode === 'observability') {
      alerts.value = []
      alertTotal.value = null
      alertLoadError.value = readable || '监控告警加载失败，请重试'
      ElMessage.error(alertLoadError.value)
    } else throw error
  } finally {
    if (requestIsCurrent()) loading.value = false
  }
}

/**
 * 加载选中 Agent 的知识和记忆。
 * @returns Promise<void>
 */
async function loadAgentKnowledge(isCurrent: () => boolean = () => true): Promise<void> {
  const agentCode = selectedAgent.value
  const requestIsCurrent = () => isCurrent() && selectedAgent.value === agentCode
  if (!agentCode) {
    agentMemory.value = []
    agentKnowledge.value = []
    return
  }
  const [memory, knowledge] = await Promise.all([
    listAgentMemory(agentCode),
    listAgentKnowledge(agentCode),
  ])
  if (!requestIsCurrent()) return
  agentMemory.value = memory
  agentKnowledge.value = knowledge
  const sources = await listAgentKnowledgeSources(agentCode)
  if (!requestIsCurrent()) return
  knowledgeSources.value = sources
  toolPermissionForm.value.agent_code = agentCode
  rewardForm.value.agent_code = agentCode
}

/**
 * 执行策略试算。
 * @returns Promise<void>
 */
async function onEvaluatePolicy(): Promise<void> {
  policyResult.value = await evaluatePolicy({ ...policyForm.value, context: {} })
}

/**
 * 保存策略规则。
 * @returns Promise<void>
 */
async function onSavePolicy(): Promise<void> {
  try {
    await ElMessageBox.confirm('确定要保存此策略规则吗？', '保存确认', {
      confirmButtonText: '保存',
      cancelButtonText: '取消',
      type: 'info',
    })
  } catch { return }
  await upsertPolicy({ ...policyEditor.value, condition_json: {} })
  ElMessage.success('策略规则已保存')
  await loadData()
}

/**
 * 保存工具权限。
 * @returns Promise<void>
 */
async function onSaveToolPermission(): Promise<void> {
  try {
    await ElMessageBox.confirm('确定要保存此工具权限配置吗？', '保存确认', {
      confirmButtonText: '保存',
      cancelButtonText: '取消',
      type: 'info',
    })
  } catch { return }
  await upsertToolPermission({ ...toolPermissionForm.value })
  ElMessage.success('工具权限已保存')
  await loadData()
}

/**
 * Responses 工具审批不能由通用审批接口只改状态；仅来源已核验的请求允许返回原会话。
 */
function isResponseSessionApproval(row: Pick<ApprovalItem, 'requires_session_resume'>): boolean {
  return row.requires_session_resume === true
}

function openApprovalDetails(row: ApprovalItem): void {
  selectedApproval.value = row
  approvalDetailsVisible.value = true
}

function approvalRequestDetails(row: ApprovalItem): string {
  const payload = row.request_json
  if (!payload || (Array.isArray(payload) && payload.length === 0)) return '未记录请求参数'
  if (!Array.isArray(payload) && typeof payload === 'object' && Object.keys(payload).length === 0) {
    return '未记录请求参数'
  }
  return JSON.stringify(payload, null, 2) ?? '未记录请求参数'
}

function returnToApprovalSession(row: ApprovalItem): void {
  if (row.source_trace_status !== 'verified' || !row.source_session_id) {
    ElMessage.info('未能验证原始小菱会话归属；为避免打开其他账号的会话，未执行跳转。')
    return
  }
  approvalDetailsVisible.value = false
  window.dispatchEvent(new CustomEvent('prism:open-admin-copilot', {
    detail: { sessionId: row.source_session_id },
  }))
}

/**
 * 审批通过指定事项。
 * @param row - 审批事项。
 * @returns Promise<void>
 */
async function onApprove(row: ApprovalItem): Promise<void> {
  if (isResponseSessionApproval(row)) {
    ElMessage.info('请返回发起此操作的小菱对话中批准或驳回')
    return
  }
  try {
    await ElMessageBox.confirm(`确定要通过审批「${row.title}」吗？`, '审批确认', {
      confirmButtonText: '通过',
      cancelButtonText: '取消',
      type: 'warning',
    })
  } catch { return }
  await approveItem(row.id, '管理端审批通过')
  ElMessage.success('已审批通过')
  await loadData()
}

/**
 * 驳回指定事项。
 * @param row - 审批事项。
 * @returns Promise<void>
 */
async function onReject(row: ApprovalItem): Promise<void> {
  if (isResponseSessionApproval(row)) {
    ElMessage.info('请返回发起此操作的小菱对话中批准或驳回')
    return
  }
  const ok = await confirmDanger({
    target: `驳回审批「${row.title}」`,
    consequence: '驳回后该事项需要重新发起才能生效。',
    confirmText: '确认驳回',
  })
  if (!ok) return
  await rejectItem(row.id, '管理端驳回')
  ElMessage.success('已驳回')
  await loadData()
}

/**
 * 手动运行调度任务。
 * @param row - 调度任务。
 * @returns Promise<void>
 */
async function onRunJob(row: AgentJob): Promise<void> {
  try {
    await ElMessageBox.confirm(`确定要手动运行任务「${row.job_code}」吗？`, '运行确认', {
      confirmButtonText: '运行',
      cancelButtonText: '取消',
      type: 'info',
    })
  } catch { return }
  await runJob(row.id)
  ElMessage.success('任务已运行')
  await loadData()
}

/**
 * 保存调度任务配置。
 * @param row - 调度任务。
 * @returns Promise<void>
 */
async function onSaveJob(row: AgentJob): Promise<void> {
  try {
    await ElMessageBox.confirm(`确定要保存任务「${row.job_code}」的配置吗？`, '保存确认', {
      confirmButtonText: '保存',
      cancelButtonText: '取消',
      type: 'info',
    })
  } catch { return }
  const data = jobEdit.value[row.id]
  if (data && !isScheduleValid(data.schedule)) {
    ElMessage.warning('调度计划格式不正确，请使用已有格式或五段 cron')
    return
  }
  if (!data || jobSaving.value[row.id]) return
  jobSaving.value[row.id] = true
  delete jobSaveErrors.value[row.id]
  try {
    await updateJob(row.id, { schedule: data.schedule, status: data.status })
    ElMessage.success('任务配置已保存')
    await loadData()
  } catch (error) {
    const message = error instanceof Error ? error.message : (error as { message?: string })?.message
    const display = message || '调度器同步失败，请稍后重试'
    jobSaveErrors.value[row.id] = display
    ElMessage.error(display)
  } finally {
    jobSaving.value[row.id] = false
  }
}

/**
 * 创建 Agent 独立记忆。
 * @returns Promise<void>
 */
async function onCreateMemory(): Promise<void> {
  if (!selectedAgent.value) return
  try {
    await ElMessageBox.confirm('确定要沉淀此记忆吗？', '记忆确认', {
      confirmButtonText: '沉淀',
      cancelButtonText: '取消',
      type: 'info',
    })
  } catch { return }
  await createAgentMemory(selectedAgent.value, { ...memoryForm.value })
  memoryForm.value = { title: '', content: '', memory_type: 'long_term', weight: 1 }
  ElMessage.success('记忆已沉淀')
  await loadAgentKnowledge()
}

/**
 * 创建 Agent 知识文档。
 * @returns Promise<void>
 */
async function onCreateKnowledgeDoc(): Promise<void> {
  if (!selectedAgent.value) return
  try {
    await ElMessageBox.confirm('确定要提交此知识文档吗？', '知识确认', {
      confirmButtonText: '提交',
      cancelButtonText: '取消',
      type: 'info',
    })
  } catch { return }
  await createAgentKnowledgeDoc({
    agent_code: selectedAgent.value,
    title: knowledgeDocForm.value.title,
    content: knowledgeDocForm.value.content,
    source_type: knowledgeDocForm.value.source_type,
    risk_level: knowledgeDocForm.value.risk_level,
    confidence: knowledgeDocForm.value.confidence,
  })
  knowledgeDocForm.value.title = ''
  knowledgeDocForm.value.content = ''
  ElMessage.success('知识文档已提交')
  await loadAgentKnowledge()
}

/**
 * 保存 Agent 知识来源。
 * @returns Promise<void>
 */
async function onSaveKnowledgeSource(): Promise<void> {
  if (!selectedAgent.value || !canManageKnowledgeSources.value) return
  try {
    await ElMessageBox.confirm('确定要保存此知识来源吗？', '来源确认', {
      confirmButtonText: '保存',
      cancelButtonText: '取消',
      type: 'info',
    })
  } catch { return }
  await upsertAgentKnowledgeSource({
    agent_code: selectedAgent.value,
    source_type: knowledgeSourceForm.value.source_type,
    source_uri: knowledgeSourceForm.value.source_uri,
    whitelist: knowledgeSourceForm.value.whitelist,
    enabled: knowledgeSourceForm.value.enabled,
    config_json: { content: knowledgeSourceForm.value.config_content },
  })
  ElMessage.success('知识来源已保存')
  await loadAgentKnowledge()
}

/**
 * 手动抓取 Agent 知识来源。
 * @returns Promise<void>
 */
async function onCrawlKnowledge(): Promise<void> {
  if (!canManageKnowledgeSources.value) return
  try {
    await ElMessageBox.confirm('确定要抓取该 Agent 的知识来源吗？', '抓取确认', {
      confirmButtonText: '抓取',
      cancelButtonText: '取消',
      type: 'info',
    })
  } catch { return }
  const result = await crawlAgentKnowledgeSources(selectedAgent.value)
  ElMessage.success(`抓取完成：${result.doc_count ?? 0} 个文档`)
  await loadAgentKnowledge()
}

/**
 * 激活知识文档。
 * @param row - 知识文档。
 * @returns Promise<void>
 */
async function onActivateKnowledge(row: AgentKnowledgeDoc): Promise<void> {
  try {
    await ElMessageBox.confirm(`确定要激活知识文档「${row.title}」吗？`, '激活确认', {
      confirmButtonText: '激活',
      cancelButtonText: '取消',
      type: 'warning',
    })
  } catch { return }
  await activateAgentKnowledgeDoc(row.id)
  ElMessage.success('知识已生效')
  await loadAgentKnowledge()
}

/**
 * 关闭治理告警。
 * @param row - 告警。
 * @returns Promise<void>
 */
async function onResolveAlert(row: AgentAlert): Promise<void> {
  const ok = await confirmDanger({
    target: `关闭告警「${row.title}」`,
    consequence: '关闭后该告警不再出现在待处理列表。',
    confirmText: '确认关闭',
  })
  if (!ok) return
  await resolveAlert(row.id, '管理端关闭告警')
  ElMessage.success('告警已关闭')
  await loadData()
}

/**
 * 记录奖惩事件。
 * @returns Promise<void>
 */
async function onCreateReward(): Promise<void> {
  try {
    await ElMessageBox.confirm('确定要记录此奖惩事件吗？', '记录确认', {
      confirmButtonText: '记录',
      cancelButtonText: '取消',
      type: 'info',
    })
  } catch { return }
  await createRewardEvent({ ...rewardForm.value })
  rewardForm.value.reason = ''
  ElMessage.success('奖惩事件已记录')
  await loadData()
}

/**
 * 创建 artifact 版本。
 * @returns Promise<void>
 */
async function onCreateArtifactVersion(): Promise<void> {
  try {
    await ElMessageBox.confirm('确定要创建此版本吗？', '版本确认', {
      confirmButtonText: '创建',
      cancelButtonText: '取消',
      type: 'info',
    })
  } catch { return }
  await createArtifactVersion({ ...artifactForm.value })
  artifactForm.value.version = ''
  artifactForm.value.content = ''
  artifactForm.value.snapshot = ''
  ElMessage.success('版本已创建')
  await loadData()
}

/**
 * 回滚 artifact 版本。
 * @param row - artifact 版本。
 * @returns Promise<void>
 */
async function onRollbackArtifact(row: AgentArtifactVersion): Promise<void> {
  const ok = await confirmDanger({
    target: `回退版本「${row.version}」`,
    consequence: '此操作将恢复到该版本的快照,覆盖当前内容。',
    confirmText: '确认回退',
  })
  if (!ok) return
  await rollbackArtifactVersion(row.id)
  ElMessage.success('版本已回退')
  await loadData()
}

watch(() => props.mode, () => {
  dataRequestVersion += 1
  approvalRequestVersion += 1
  loading.value = false
  if (props.mode !== 'approvals') {
    approvals.value = []
    approvalLoadError.value = ''
  }
  void loadData()
}, { flush: 'sync' })
watch([() => userStore.profile?.id, () => userStore.token], () => {
  if (props.mode !== 'approvals') return
  approvalRequestVersion += 1
  approvals.value = []
  approvalLoadError.value = ''
  loading.value = false
  if (userStore.profile && userStore.token) void loadData()
}, { flush: 'sync' })
watch(selectedAgent, () => {
  if (props.mode === 'knowledge') void loadAgentKnowledge(() => props.mode === 'knowledge')
})

onMounted(loadData)
</script>

<template>
  <div v-loading="loading" class="governance-page">
    <div class="page-head">
      <div>
        <h2>{{ pageTitle }}</h2>
        <p>{{ pageSubtitle }}</p>
      </div>
      <el-button
        :loading="(mode === 'agents' || mode === 'approvals') && loading"
        :disabled="(mode === 'agents' || mode === 'approvals') && loading"
        :aria-busy="mode === 'agents' || mode === 'approvals' ? loading : undefined"
        @click="loadData"
      >{{ (mode === 'agents' || mode === 'approvals') && loading ? (mode === 'agents' && agentsLoaded ? '正在刷新' : '正在加载') : '刷新' }}</el-button>
    </div>

    <template v-if="mode === 'overview'">
      <div class="metric-grid">
        <div class="metric"><span>Agent 总数</span><strong>{{ overview?.agents_total ?? 0 }}</strong></div>
        <div class="metric"><span>启用 Agent</span><strong>{{ overview?.agents_enabled ?? 0 }}</strong></div>
        <!-- 统计卡即入口:待审批/开放告警点击直达,数值>0 标警示色 -->
        <button type="button" class="metric is-link" title="去执行审批" @click="goMetric('/admin/governance?section=approvals&approvalType=execution')">
          <span>执行审批待办</span><strong :class="{ 'is-warn': (overview?.approvals_pending ?? 0) > 0 }">{{ overview?.approvals_pending ?? 0 }}</strong>
        </button>
        <div class="metric"><span>工具调用</span><strong>{{ overview?.tool_calls_today ?? 0 }}</strong></div>
        <button type="button" class="metric is-link" title="去可观测中心" @click="goMetric('/admin/operations?section=observability')">
          <span>开放告警</span><strong :class="{ 'is-warn': (overview?.alerts_open ?? 0) > 0 }">{{ overview?.alerts_open ?? 0 }}</strong>
        </button>
        <div class="metric"><span>知识文档</span><strong>{{ overview?.knowledge_docs_total ?? 0 }}</strong></div>
      </div>
      <div class="content-grid">
        <section class="panel">
          <h3>Agent 状态</h3>
          <el-table :data="agents" height="360">
              <template #empty>
                <EmptyState compact description="暂无 Agent 记录" />
              </template>
            <el-table-column prop="name" label="Agent(智能体)" min-width="140" />
            <el-table-column label="分类" width="110">
            <template #default="{ row }"><span :title="row.category">{{ categoryText(row.category) }}</span></template>
          </el-table-column>
            <el-table-column label="状态" width="100"><template #default="{ row }"><el-tag size="small">{{ statusText(row.status) }}</el-tag></template></el-table-column>
            <el-table-column prop="priority" label="优先级" width="90" />
          </el-table>
        </section>
        <section class="panel">
          <h3>待处理审批</h3>
          <el-table :data="approvals" height="360">
              <template #empty>
                <EmptyState compact description="无待审批事项,高风险操作会自动进入这里" />
              </template>
            <el-table-column label="事项" min-width="180">
              <template #default="{ row }"><span :title="approvalTitle(row.title)">{{ approvalTitle(row.title) }}</span></template>
            </el-table-column>
            <el-table-column label="风险" width="100"><template #default="{ row }"><el-tag size="small" :type="riskTagType(row.risk_level)">{{ riskText(row.risk_level) }}</el-tag></template></el-table-column>
            <el-table-column label="状态" width="110"><template #default="{ row }">{{ statusText(row.status) }}</template></el-table-column>
          </el-table>
        </section>
      </div>
    </template>

    <section v-else-if="mode === 'agents'" class="panel agent-directory">
      <p class="agent-load-status" role="status" aria-live="polite" aria-atomic="true">
        <template v-if="loading">{{ agentsLoaded ? '正在刷新 Agent 列表，保留上次成功结果。' : '正在加载 Agent 列表…' }}</template>
        <template v-else-if="agentsLoaded && !agentLoadError">已加载 {{ agents.length }} 个 Agent</template>
      </p>
      <el-alert
        v-if="agentLoadError"
        type="error"
        :title="agentLoadError"
        :closable="false"
        show-icon
      >
        <p>{{ agentsLoaded ? '当前保留上次成功加载的列表，请重新加载。' : '尚未获取 Agent 列表，请重新加载。' }}</p>
        <el-button :loading="loading" :disabled="loading" @click="loadData">重新加载</el-button>
      </el-alert>
      <div v-if="hiddenInternalAgentCount" class="agent-system-disclosure">
        <span>小菱是唯一主控；{{ hiddenInternalAgentCount }} 个内部调度或兼容条目默认收起，监督子 Agent 仍单独显示。</span>
        <el-button
          link
          :aria-expanded="showInternalAgents"
          aria-controls="agent-internal-cards"
          @click="showInternalAgents = !showInternalAgents"
        >{{ showInternalAgents ? '收起系统内部 Agent' : '查看系统内部 Agent' }}</el-button>
      </div>
      <div v-if="visibleAgents.length" id="agent-internal-cards" class="agent-card-grid">
        <article v-for="(agent, index) in visibleAgents" :key="agent.code" class="agent-card" :aria-labelledby="`agent-name-${index}`">
          <header class="agent-card-head">
            <div>
              <span class="agent-category" :title="typeof agent.category === 'string' ? agent.category : undefined">{{ agentCategoryText(agent.category) }}</span>
              <h3 :id="`agent-name-${index}`">{{ agentDisplayName(agent) }}</h3>
            </div>
            <span class="agent-enabled" :class="{ 'is-disabled': agent.is_enabled === 0 }">{{ agent.is_enabled === 1 ? '已启用' : agent.is_enabled === 0 ? '已停用' : '状态未提供' }}</span>
          </header>
          <p class="agent-responsibility" :title="agent.description">{{ agent.description || '暂未提供职责说明' }}</p>
          <dl class="agent-card-metrics">
            <div><dt>优先级</dt><dd>{{ agent.priority ?? '—' }}</dd></div>
            <div><dt>审批阈值</dt><dd>{{ agent.auto_approval_threshold ?? '—' }}</dd></div>
            <div><dt>记忆</dt><dd>{{ agent.memory_count ?? '—' }}</dd></div>
            <div><dt>知识</dt><dd>{{ agent.knowledge_count ?? '—' }}</dd></div>
          </dl>
          <button
            type="button"
            class="agent-detail-toggle"
            :aria-expanded="expandedAgentCodes.has(agent.code)"
            :aria-controls="`agent-details-${index}`"
            @click="toggleAgentDetails(agent.code)"
          >{{ expandedAgentCodes.has(agent.code) ? '收起能力与治理详情' : '查看能力与治理详情' }} <span aria-hidden="true">{{ expandedAgentCodes.has(agent.code) ? '−' : '+' }}</span></button>
          <div v-if="expandedAgentCodes.has(agent.code)" :id="`agent-details-${index}`" class="agent-card-details">
            <dl>
              <div><dt>内部编码</dt><dd><code>{{ agent.code }}</code></dd></div>
              <div><dt>完整职责</dt><dd>{{ agent.description || '暂未提供职责说明' }}</dd></div>
              <div><dt>授权边界</dt><dd>{{ agentBoundaryText(agent) }}</dd></div>
              <div>
                <dt>已登记能力（{{ agent.skills?.length ?? 0 }}）</dt>
                <dd v-if="agent.skills?.length" class="agent-skill-list"><span v-for="skill in agent.skills" :key="skill" :title="skill">{{ toolCodeText(skill) }}</span></dd>
                <dd v-else>暂无已登记能力</dd>
              </div>
            </dl>
          </div>
        </article>
      </div>
      <EmptyState v-else compact :description="agentsLoaded ? '暂无 Agent 记录' : loading ? '正在加载 Agent 列表' : '尚未获取 Agent 列表'" />
    </section>

    <section v-else-if="mode === 'approvals'" class="panel approval-panel">
      <div v-if="approvalLoadError" class="approval-load-error" role="alert">
        <span>{{ approvalLoadError }}</span>
        <el-button :disabled="loading" @click="loadData">重试加载审批</el-button>
      </div>
      <div v-else class="approval-table-scroll" role="region" aria-label="待办审批列表，可横向滚动查看全部列" :aria-busy="loading" tabindex="0" data-testid="approval-table-scroll">
        <p class="approval-scroll-hint">左右滑动可查看状态和操作列</p>
        <el-table class="approval-table-content" :data="approvals" stripe>
          <template #empty>
            <p v-if="loading" class="approval-loading-state" role="status">正在加载待审批事项</p>
            <EmptyState v-else compact description="无待审批事项" />
          </template>
        <el-table-column label="审批事项" min-width="220">
          <template #default="{ row }"><span :title="row.title">{{ approvalTitle(row.title) }}</span></template>
        </el-table-column>
        <el-table-column label="请求来源" width="150">
            <template #default="{ row }">
              <span v-if="row.action.startsWith('responses.') && row.source_trace_status === 'verified'">小菱管理员会话</span>
              <span v-else-if="row.action.startsWith('responses.')" class="approval-source-unverified">小菱请求（来源未核验）</span>
              <span v-else :title="row.agent_code">{{ agentCodeText(row.agent_code) }}</span>
            </template>
          </el-table-column>
        <el-table-column label="动作" min-width="150">
            <template #default="{ row }"><span :title="row.action">{{ policyActionText(row.action) }}</span></template>
          </el-table-column>
        <el-table-column label="风险" width="100"><template #default="{ row }"><el-tag size="small" :type="riskTagType(row.risk_level)">{{ riskText(row.risk_level) }}</el-tag></template></el-table-column>
        <el-table-column label="状态" width="110" fixed="right"><template #default="{ row }">{{ statusText(row.status) }}</template></el-table-column>
        <el-table-column label="操作" width="220" fixed="right">
          <template #default="{ row }">
            <div class="approval-row-actions">
              <el-button link type="primary" @click="openApprovalDetails(row)">查看详情</el-button>
              <template v-if="row.status === 'pending' && isResponseSessionApproval(row)">
                <span v-if="row.source_trace_status === 'verified'" class="approval-session-only">须在原小菱会话处理</span>
              </template>
              <template v-else-if="row.status === 'pending'">
                <el-button link type="success" @click="onApprove(row)">通过</el-button>
                <el-button link type="danger" @click="onReject(row)">驳回</el-button>
              </template>
            </div>
            </template>
        </el-table-column>
        </el-table>
      </div>
      <el-dialog v-model="approvalDetailsVisible" title="审批请求详情" width="min(680px, calc(100vw - 32px))">
        <template v-if="selectedApproval">
          <dl class="approval-detail-grid">
            <div><dt>审批编号</dt><dd>{{ selectedApproval.id }}</dd></div>
            <div><dt>创建时间</dt><dd>{{ selectedApproval.create_time || '未记录' }}</dd></div>
            <div><dt>风险等级</dt><dd>{{ riskText(selectedApproval.risk_level) }}</dd></div>
            <div><dt>请求动作</dt><dd><code>{{ selectedApproval.action }}</code></dd></div>
            <div><dt>目标资源</dt><dd><code>{{ selectedApproval.resource }}</code></dd></div>
            <div v-if="selectedApproval.source_tool_name"><dt>工具来源</dt><dd><code>{{ selectedApproval.source_tool_name }}</code></dd></div>
            <div v-if="selectedApproval.action.startsWith('responses.') && selectedApproval.agent_code">
              <dt>治理登记 Agent</dt><dd>{{ agentCodeText(selectedApproval.agent_code) }}（记录身份，不代表账号归属）</dd>
            </div>
            <div v-if="selectedApproval.source_run_id"><dt>关联运行</dt><dd><code>{{ selectedApproval.source_run_id }}</code></dd></div>
          </dl>
          <p v-if="selectedApproval.requires_session_resume && selectedApproval.source_trace_status === 'verified'" class="approval-detail-notice">
            服务端已核验此请求与当前管理员账号下的小菱会话关联。通用审批中心不能批准或驳回，请从本页返回已核验的原会话继续处理。
          </p>
          <p v-if="selectedApproval.requires_session_resume && selectedApproval.source_trace_status !== 'verified'" class="approval-detail-warning" role="status">
            当前记录没有可验证的有效运行检查点与来源会话关联，无法确认发起此请求的小菱会话。为避免跨账号跳转或误操作，本页不会尝试打开会话；请结合脱敏参数与审计记录核对。
          </p>
          <section class="approval-request-context" aria-label="已脱敏的审批请求参数">
            <h4>请求参数与预览（服务端已脱敏）</h4>
            <pre>{{ approvalRequestDetails(selectedApproval) }}</pre>
          </section>
        </template>
        <template #footer>
          <el-button @click="approvalDetailsVisible = false">关闭</el-button>
          <el-button
            v-if="selectedApproval?.requires_session_resume && selectedApproval.source_trace_status === 'verified' && selectedApproval.source_session_id"
            type="primary"
            @click="returnToApprovalSession(selectedApproval)"
          >返回发起会话</el-button>
        </template>
      </el-dialog>
    </section>

    <section v-else-if="mode === 'policies'" class="stack">
      <div class="panel">
        <h3>策略试算</h3>
        <div class="form-row">
          <label class="form-control"><span>执行主体</span><el-input v-model="policyForm.subject" placeholder="例如：Agent 团队" /></label>
          <label class="form-control"><span>业务动作</span><el-input v-model="policyForm.action" placeholder="例如：读取知识" /></label>
          <label class="form-control"><span>目标资源</span><el-input v-model="policyForm.resource" placeholder="例如：项目知识库" /></label>
          <el-button type="primary" @click="onEvaluatePolicy">试算</el-button>
        </div>
        <div v-if="policyResult" class="decision-line">
          结果：{{ statusText(policyResult.decision) }} · {{ riskText(policyResult.risk_level) }} · {{ policyResult.reason }}
        </div>
      </div>
      <div class="panel">
        <h3>策略编辑</h3>
        <div class="toolbar-grid">
          <el-input v-model="policyEditor.rule_code" placeholder="规则编码" />
          <el-input v-model="policyEditor.name" placeholder="规则名称" />
          <el-input v-model="policyEditor.subject" placeholder="主体" />
          <el-input v-model="policyEditor.action" placeholder="动作" />
          <el-input v-model="policyEditor.resource" placeholder="资源" />
          <el-select v-model="policyEditor.effect">
            <el-option label="允许(allow)" value="allow" />
            <el-option label="拒绝(deny)" value="deny" />
            <el-option label="升级审批(escalate)" value="escalate" />
          </el-select>
          <el-select v-model="policyEditor.risk_level">
            <el-option label="低(low)" value="low" />
            <el-option label="中(medium)" value="medium" />
            <el-option label="高(high)" value="high" />
            <el-option label="危急（critical）" value="critical" />
          </el-select>
          <el-input-number v-model="policyEditor.priority" :min="0" :max="10000" controls-position="right" />
          <el-select v-model="policyEditor.enabled">
            <el-option label="启用" :value="1" />
            <el-option label="停用" :value="0" />
          </el-select>
          <el-button type="primary" @click="onSavePolicy">保存策略</el-button>
        </div>
      </div>
      <div class="panel">
        <h3>策略规则</h3>
        <el-table :data="policies" stripe>
          <template #empty>
            <EmptyState compact description="暂无策略配置" />
          </template>
          <el-table-column prop="name" label="规则" min-width="180" />
          <el-table-column label="主体" width="130">
            <template #default="{ row }">
              <span :title="row.subject">{{ policySubjectText(row.subject) }}</span>
            </template>
          </el-table-column>
          <el-table-column label="动作" width="140">
            <template #default="{ row }">
              <span :title="row.action">{{ policyActionText(row.action) }}</span>
            </template>
          </el-table-column>
          <el-table-column label="资源" width="130">
            <template #default="{ row }">
              <span :title="row.resource">{{ policyResourceText(row.resource) }}</span>
            </template>
          </el-table-column>
          <el-table-column label="处理方式" width="110"><template #default="{ row }">{{ statusText(row.effect) }}</template></el-table-column>
          <el-table-column label="风险" width="100"><template #default="{ row }">{{ riskText(row.risk_level) }}</template></el-table-column>
          <el-table-column label="状态" width="80"><template #default="{ row }">{{ row.enabled ? '启用' : '停用' }}</template></el-table-column>
        </el-table>
      </div>
      <div class="panel">
        <h3>决策日志</h3>
        <el-table :data="decisions" stripe>
          <template #empty>
            <EmptyState compact description="暂无策略决策记录" />
          </template>
          <el-table-column label="主体" width="150">
            <template #default="{ row }">
              <span :title="row.subject">{{ policySubjectText(row.subject) }}</span>
            </template>
          </el-table-column>
          <el-table-column label="动作" min-width="160">
            <template #default="{ row }">
              <span :title="row.action">{{ policyActionText(row.action) }}</span>
            </template>
          </el-table-column>
          <el-table-column label="决策" width="110"><template #default="{ row }">{{ statusText(row.decision) }}</template></el-table-column>
          <el-table-column label="风险" width="100"><template #default="{ row }">{{ riskText(row.risk_level) }}</template></el-table-column>
          <el-table-column label="原因" min-width="220" show-overflow-tooltip>
            <template #default="{ row }"><span>{{ row.content_redacted ? '原文按账号隔离' : (row.reason || '-') }}</span></template>
          </el-table-column>
        </el-table>
      </div>
    </section>

    <section v-else-if="mode === 'tools'" class="stack">
      <div class="panel">
        <h3>工具权限配置</h3>
        <div class="toolbar-grid">
          <el-select v-model="toolPermissionForm.agent_code" filterable placeholder="Agent 编码" style="min-width: 180px">
            <el-option v-for="agent in agents" :key="agent.code" :label="`${agent.name} (${agent.code})`" :value="agent.code" />
          </el-select>
          <el-input v-model="toolPermissionForm.tool_code" placeholder="工具编码" />
          <el-select v-model="toolPermissionForm.permission">
            <el-option label="允许(allow)" value="allow" />
            <el-option label="拒绝(deny)" value="deny" />
            <el-option label="升级审批(escalate)" value="escalate" />
          </el-select>
          <el-select v-model="toolPermissionForm.risk_level">
            <el-option label="低(low)" value="low" />
            <el-option label="中(medium)" value="medium" />
            <el-option label="高(high)" value="high" />
            <el-option label="危急（critical）" value="critical" />
          </el-select>
          <el-select v-model="toolPermissionForm.enabled">
            <el-option label="启用" :value="1" />
            <el-option label="停用" :value="0" />
          </el-select>
          <el-input v-model="toolPermissionForm.note" placeholder="备注" />
          <el-button type="primary" @click="onSaveToolPermission">保存权限</el-button>
        </div>
      </div>
      <div class="panel">
        <h3>权限列表</h3>
        <el-table :data="toolPermissions" stripe>
          <template #empty>
            <EmptyState compact description="暂无工具授权" />
          </template>
          <el-table-column label="Agent(智能体)" width="130">
            <template #default="{ row }"><span :title="row.agent_code">{{ agentCodeText(row.agent_code) }}</span></template>
          </el-table-column>
          <el-table-column label="工具" width="130">
            <template #default="{ row }"><span :title="row.tool_code">{{ toolCodeText(row.tool_code) }}</span></template>
          </el-table-column>
          <el-table-column label="权限" width="110">
            <template #default="{ row }"><span :title="row.permission">{{ decisionText(row.permission) }}</span></template>
          </el-table-column>
          <el-table-column label="风险" width="90">
            <template #default="{ row }"><span :title="row.risk_level">{{ riskText(row.risk_level) }}</span></template>
          </el-table-column>
          <el-table-column label="启用" width="80"><template #default="{ row }">{{ row.enabled ? '启用' : '停用' }}</template></el-table-column>
          <el-table-column prop="note" label="备注" min-width="180" show-overflow-tooltip />
        </el-table>
      </div>
      <div class="panel">
        <h3>工具调用日志</h3>
        <el-table :data="tools" stripe>
          <template #empty>
            <EmptyState compact description="暂无工具注册" />
          </template>
          <el-table-column label="Agent(智能体)" width="130">
            <template #default="{ row }"><span :title="row.agent_code">{{ agentCodeText(row.agent_code) }}</span></template>
          </el-table-column>
          <el-table-column label="工具" width="130">
            <template #default="{ row }"><span :title="row.tool_code">{{ toolCodeText(row.tool_code) }}</span></template>
          </el-table-column>
          <el-table-column label="动作" min-width="150">
            <template #default="{ row }"><span :title="row.action">{{ policyActionText(row.action) }}</span></template>
          </el-table-column>
          <el-table-column label="决策" width="90">
            <template #default="{ row }"><span :title="row.decision">{{ decisionText(row.decision) }}</span></template>
          </el-table-column>
          <el-table-column label="状态" width="100">
            <template #default="{ row }"><span :title="row.status">{{ statusText(row.status) }}</span></template>
          </el-table-column>
          <el-table-column label="风险" width="90">
            <template #default="{ row }"><span :title="row.risk_level">{{ riskText(row.risk_level) }}</span></template>
          </el-table-column>
          <el-table-column prop="duration_ms" label="耗时(ms)" width="110" />
          <el-table-column label="说明" min-width="160">
            <template #default="{ row }"><span>{{ row.content_redacted ? '原文按账号隔离' : '-' }}</span></template>
          </el-table-column>
        </el-table>
      </div>
    </section>

    <section v-else-if="mode === 'knowledge'" class="stack">
      <div class="panel">
        <h3>Agent 选择</h3>
        <div class="action-row">
          <el-select v-model="selectedAgent" filterable style="width: 280px">
            <el-option v-for="agent in agents" :key="agent.code" :label="agent.name" :value="agent.code" />
          </el-select>
          <el-button v-if="canManageKnowledgeSources" type="primary" @click="onCrawlKnowledge">抓取知识</el-button>
        </div>
      </div>
      <div class="content-grid">
        <div class="panel">
          <h3>新增记忆</h3>
          <div class="stack compact">
            <el-input v-model="memoryForm.title" placeholder="标题" />
            <el-input v-model="memoryForm.content" type="textarea" :rows="4" placeholder="内容" />
            <div class="form-row two">
              <el-select v-model="memoryForm.memory_type">
                <el-option label="长期记忆(long_term)" value="long_term" />
                <el-option label="短期记忆(short_term)" value="short_term" />
                <el-option label="反思(reflection)" value="reflection" />
              </el-select>
              <el-input-number v-model="memoryForm.weight" :min="0" :max="10" controls-position="right" />
              <el-button type="primary" @click="onCreateMemory">沉淀记忆</el-button>
            </div>
          </div>
        </div>
        <div class="panel">
          <h3>新增知识</h3>
          <div class="stack compact">
            <el-input v-model="knowledgeDocForm.title" placeholder="标题" />
            <el-input v-model="knowledgeDocForm.content" type="textarea" :rows="4" placeholder="内容" />
            <div class="form-row two">
              <el-select v-model="knowledgeDocForm.risk_level">
                <el-option label="低(low)" value="low" />
                <el-option label="中(medium)" value="medium" />
                <el-option label="高(high)" value="high" />
                <el-option label="危急（critical）" value="critical" />
              </el-select>
              <el-input-number v-model="knowledgeDocForm.confidence" :min="0" :max="1" :step="0.1" />
              <el-button type="primary" @click="onCreateKnowledgeDoc">提交知识</el-button>
            </div>
          </div>
        </div>
      </div>
      <div class="panel">
        <div class="panel-heading">
          <h3>知识来源</h3>
          <el-tag v-if="!canManageKnowledgeSources" size="small" type="info">只读</el-tag>
        </div>
        <div v-if="canManageKnowledgeSources" class="toolbar-grid source-grid">
          <el-select v-model="knowledgeSourceForm.source_type">
            <el-option label="内联(inline)" value="inline" />
            <el-option label="项目(project)" value="project" />
            <el-option label="链接(url)" value="url" />
            <el-option label="官方(official)" value="official" />
            <el-option label="GitHub(github)" value="github" />
          </el-select>
          <el-input v-model="knowledgeSourceForm.source_uri" placeholder="来源 URI" />
          <el-select v-model="knowledgeSourceForm.whitelist">
            <el-option label="白名单" :value="1" />
            <el-option label="待审" :value="0" />
          </el-select>
          <el-select v-model="knowledgeSourceForm.enabled">
            <el-option label="启用" :value="1" />
            <el-option label="停用" :value="0" />
          </el-select>
          <el-input v-model="knowledgeSourceForm.config_content" placeholder="内联内容" />
          <el-button type="primary" @click="onSaveKnowledgeSource">保存来源</el-button>
        </div>
        <el-table :data="knowledgeSources" stripe>
          <template #empty>
            <EmptyState compact description="暂无知识源" />
          </template>
          <el-table-column label="类型" width="100">
            <template #default="{ row }"><span :title="row.source_type">{{ sourceTypeText(row.source_type) }}</span></template>
          </el-table-column>
          <el-table-column prop="source_uri" label="来源" min-width="240" show-overflow-tooltip />
          <el-table-column label="白名单" width="90"><template #default="{ row }">{{ row.whitelist ? '白名单' : '需审核' }}</template></el-table-column>
          <el-table-column prop="enabled" label="启用" width="80" />
        </el-table>
      </div>
      <div class="content-grid">
        <div class="panel">
          <h3>独立记忆</h3>
          <el-table :data="agentMemory" height="360">
              <template #empty>
                <EmptyState compact description="该 Agent 暂无记忆" />
              </template>
            <el-table-column prop="title" label="标题" min-width="180" />
            <el-table-column label="类型" width="110">
            <template #default="{ row }"><span :title="row.memory_type">{{ memoryTypeText(row.memory_type) }}</span></template>
          </el-table-column>
            <el-table-column prop="weight" label="权重" width="90" />
          </el-table>
        </div>
        <div class="panel">
          <h3>知识文档</h3>
          <el-table :data="agentKnowledge" height="360">
              <template #empty>
                <EmptyState compact description="该 Agent 暂无知识条目" />
              </template>
            <el-table-column prop="title" label="标题" min-width="180" />
            <el-table-column label="来源" width="100">
            <template #default="{ row }"><span :title="row.source_type">{{ sourceTypeText(row.source_type) }}</span></template>
          </el-table-column>
            <el-table-column prop="risk_level" label="风险" width="90" />
            <el-table-column label="状态" width="130">
            <template #default="{ row }"><span :title="row.status">{{ statusText(row.status) }}</span></template>
          </el-table-column>
            <el-table-column prop="chunk_count" label="切片" width="80" />
            <el-table-column label="操作" width="90">
              <template #default="{ row }">
                <el-button v-if="row.status === 'pending_approval'" link type="primary" @click="onActivateKnowledge(row)">
                  生效
                </el-button>
              </template>
            </el-table-column>
          </el-table>
        </div>
      </div>
    </section>

    <section v-else-if="mode === 'jobs'" class="panel">
      <el-table :data="jobs" stripe>
          <template #empty>
            <EmptyState compact description="暂无调度任务,点上方「新建」创建" />
          </template>
        <el-table-column label="任务" min-width="220">
          <template #default="{ row }">
            <span :title="row.job_code">{{ jobCodeText(row.job_code) }}</span>
          </template>
        </el-table-column>
        <el-table-column label="类型" width="110">
          <template #default="{ row }">
            <span :title="row.job_type">{{ jobTypeText(row.job_type) }}</span>
          </template>
        </el-table-column>
        <el-table-column label="Agent(智能体)" width="150">
          <template #default="{ row }">
            <span :title="row.agent_code">{{ agentCodeText(row.agent_code) }}</span>
          </template>
        </el-table-column>
        <el-table-column label="计划" width="190">
          <template #default="{ row }">
            <div v-if="jobEdit[row.id]" class="job-schedule-editor">
              <el-input v-model="jobEdit[row.id].schedule" size="small" placeholder="未设置；例如 0 3 * * *（每天 3 点）" :class="{ 'is-cron-invalid': scheduleInvalid(row.id) }" />
              <small v-if="jobScheduleText(jobEdit[row.id].schedule) !== jobEdit[row.id].schedule" class="job-schedule-human">
                {{ jobScheduleText(jobEdit[row.id].schedule) }}
              </small>
            </div>
            <span v-else :title="row.schedule || '未设置'">{{ jobScheduleText(row.schedule) }}</span>
          </template>
        </el-table-column>
        <el-table-column label="状态" width="130">
          <template #default="{ row }">
            <el-select v-if="jobEdit[row.id]" v-model="jobEdit[row.id].status" size="small">
              <el-option label="启用(enabled)" value="enabled" />
              <el-option label="停用(disabled)" value="disabled" />
            </el-select>
          </template>
        </el-table-column>
        <el-table-column label="操作" width="160">
          <template #default="{ row }">
            <el-button link type="primary" @click="onRunJob(row)">运行</el-button>
            <el-button
              link
              type="success"
              :loading="jobSaving[row.id]"
              :disabled="jobSaving[row.id]"
              :aria-busy="jobSaving[row.id] ? 'true' : undefined"
              :data-testid="`save-job-${row.id}`"
              @click="onSaveJob(row)"
            >保存</el-button>
          </template>
        </el-table-column>
      </el-table>
    </section>

    <section v-else-if="mode === 'observability'" class="content-grid">
      <div class="panel">
        <h3>运行指标</h3>
        <p class="observability-scope-note">工具调用、调度执行和奖惩均为累计记录；审批按事项当前状态分组；开放告警只统计当前未关闭项。</p>
        <div class="metric-grid compact-metrics">
          <div v-for="item in observabilityCards" :key="item.label" class="metric">
            <span>{{ item.label }}</span><strong>{{ item.value }}</strong>
          </div>
        </div>
        <h4>工具执行结果（累计）</h4>
        <el-table :data="Array.isArray(observability.tool_status) ? observability.tool_status : []" size="small">
          <template #empty>
            <EmptyState compact description="工具状态为空" />
          </template>
          <el-table-column prop="status" label="结果"><template #default="{ row }">{{ statusText(row.status) }}</template></el-table-column>
          <el-table-column prop="count" label="次数" width="100" />
        </el-table>
        <h4>审批事项当前状态分布</h4>
        <el-table :data="Array.isArray(observability.approval_status) ? observability.approval_status : []" size="small">
          <template #empty>
            <EmptyState compact description="暂无审批事项状态记录" />
          </template>
          <el-table-column prop="status" label="状态"><template #default="{ row }">{{ statusText(row.status) }}</template></el-table-column>
          <el-table-column prop="count" label="事项数" width="100" />
        </el-table>
      </div>
      <div class="panel">
        <h3>开放告警（{{ alertTotal ?? '—' }}）</h3>
        <el-table :data="alerts" height="360" v-loading="loading">
          <template #empty>
            <div v-if="alertLoadError" class="alert-list-error">
              <span>{{ alertLoadError }}</span>
              <el-button link type="primary" @click="loadData">重试</el-button>
            </div>
            <EmptyState v-else compact description="当前没有未关闭告警。历史失败记录请查看工具执行统计。" />
          </template>
          <el-table-column prop="title" label="告警" min-width="180" />
          <el-table-column label="级别" width="110"><template #default="{ row }">{{ alertSeverityText(row.severity) }}</template></el-table-column>
          <el-table-column label="状态" width="100"><template #default="{ row }">{{ statusText(row.status) }}</template></el-table-column>
          <el-table-column label="操作" width="90">
            <template #default="{ row }">
              <el-button v-if="row.status === 'open'" link type="primary" @click="onResolveAlert(row)">关闭</el-button>
            </template>
          </el-table-column>
        </el-table>
        <div v-if="alertTotal !== null" class="alert-pagination-wrapper">
          <el-pagination
            v-model:current-page="alertPage"
            v-model:page-size="alertPageSize"
            :total="alertTotal ?? 0"
            :page-sizes="[20, 50, 100]"
            layout="total, sizes, prev, pager, next"
            @change="loadData"
          />
        </div>
      </div>
    </section>

    <section v-else-if="mode === 'rewards'" class="stack">
      <div class="panel">
        <h3>记录奖惩</h3>
        <div class="toolbar-grid">
          <label class="form-control"><span>Agent</span><el-input v-model="rewardForm.agent_code" placeholder="选择或输入 Agent 编码" /></label>
          <el-select v-model="rewardForm.event_type">
            <el-option label="奖励(reward)" value="reward" />
            <el-option label="惩罚(penalty)" value="penalty" />
          </el-select>
          <el-input-number v-model="rewardForm.score" :min="-100" :max="100" controls-position="right" />
          <label class="form-control"><span>依据</span><el-input v-model="rewardForm.reason" placeholder="关联真实任务或验收结果" /></label>
          <el-button type="primary" @click="onCreateReward">记录</el-button>
        </div>
      </div>
      <div class="panel">
        <h3>奖惩事件</h3>
        <el-table :data="rewardEvents" stripe>
          <template #empty>
            <EmptyState compact description="暂无激励事件" />
          </template>
          <el-table-column label="Agent(智能体)" width="140">
            <template #default="{ row }"><span :title="row.agent_code">{{ agentCodeText(row.agent_code) }}</span></template>
          </el-table-column>
          <el-table-column label="类型" width="100"><template #default="{ row }">{{ row.event_type === 'reward' ? '奖励' : '惩罚' }}</template></el-table-column>
          <el-table-column prop="score" label="分数" width="90" />
          <el-table-column prop="reason" label="原因" min-width="220" show-overflow-tooltip />
        </el-table>
      </div>
    </section>

    <section v-else-if="mode === 'rollback'" class="stack">
      <div class="panel">
        <h3>创建版本</h3>
        <div class="toolbar-grid">
          <label class="form-control"><span>归属 Agent</span><el-input v-model="artifactForm.agent_code" placeholder="例如：策略 Agent" /></label>
          <el-select v-model="artifactForm.artifact_type">
            <el-option label="策略(policy)" value="policy" />
            <el-option label="提示词(prompt)" value="prompt" />
            <el-option label="技能(skill)" value="skill" />
            <el-option label="知识(knowledge)" value="knowledge" />
            <el-option label="代码(code)" value="code" />
          </el-select>
          <label class="form-control"><span>版本名称</span><el-input v-model="artifactForm.version" placeholder="例如：2026-07-30 验证版" /></label>
          <el-select v-model="artifactForm.status">
            <el-option label="草稿(draft)" value="draft" />
            <el-option label="灰度(gray)" value="gray" />
            <el-option label="稳定(stable)" value="stable" />
          </el-select>
          <label class="form-control"><span>本次内容</span><el-input v-model="artifactForm.content" placeholder="本次调整的内容" /></label>
          <label class="form-control"><span>恢复快照</span><el-input v-model="artifactForm.snapshot" placeholder="回退时恢复的内容" /></label>
          <el-button type="primary" @click="onCreateArtifactVersion">创建版本</el-button>
        </div>
      </div>
      <div class="panel">
        <h3>版本列表</h3>
        <el-table :data="artifactVersions" stripe>
          <template #empty>
            <EmptyState compact description="暂无版本产物" />
          </template>
          <el-table-column prop="agent_code" label="Agent(智能体)" width="130" />
          <el-table-column label="类型" width="110">
            <template #default="{ row }"><span :title="row.artifact_type">{{ artifactTypeText(row.artifact_type) }}</span></template>
          </el-table-column>
          <el-table-column prop="version" label="版本" min-width="160" />
          <el-table-column label="状态" width="120"><template #default="{ row }">{{ statusText(row.status) }}</template></el-table-column>
          <el-table-column label="操作" width="90">
            <template #default="{ row }">
              <el-button link type="primary" @click="onRollbackArtifact(row)">回退</el-button>
            </template>
          </el-table-column>
        </el-table>
      </div>
    </section>
  </div>
</template>

<style scoped lang="scss">
.governance-page {
  min-width: 0;
}

.page-head {
  display: flex;
  align-items: flex-end;
  justify-content: space-between;
  gap: 16px;
  margin-bottom: 18px;
}

.page-head h2 {
  margin: 0;
  font-size: 22px;
  line-height: 1.2;
  letter-spacing: 0;
}

.page-head p {
  margin-top: 6px;
  color: var(--gray-500);
  font-size: 13px;
}

.metric-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
  gap: 12px;
  margin-bottom: 16px;
}

.metric,
.panel {
  background: rgba(255, 255, 255, 0.92);
  border: 1px solid rgba(224, 227, 234, 0.9);
  border-radius: 8px;
  box-shadow: var(--shadow-1);
}

.approval-panel { overflow: visible; }
.approval-table-scroll {
  min-width: 0;
  overflow-x: auto;
  -webkit-overflow-scrolling: touch;
}
.approval-table-scroll :deep(.approval-table-content) {
  /* 审批表保持可读的最小列宽；窄视口可横向滚动，状态和操作列固定在右侧。 */
  min-width: 900px;
}
.approval-table-scroll:focus-visible {
  outline: 2px solid var(--brand-500, #5b58e8);
  outline-offset: 2px;
}
.approval-scroll-hint { display: none; }
.approval-loading-state {
  margin: 0;
  color: var(--color-text-secondary, #737b8d);
}
.approval-session-only {
  display: inline-block;
  color: var(--color-text-secondary, #737b8d);
  font-size: 12px;
  line-height: 1.4;
  white-space: normal;
}
.approval-source-unverified { color: var(--color-text-secondary, #737b8d); }
.approval-row-actions { display: flex; flex-wrap: wrap; align-items: center; gap: 2px 6px; }
.approval-detail-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 12px 20px; margin: 0; }
.approval-detail-grid > div { min-width: 0; }
.approval-detail-grid dt { color: var(--color-text-secondary, #737b8d); font-size: 12px; }
.approval-detail-grid dd { margin: 4px 0 0; overflow-wrap: anywhere; line-height: 1.5; }
.approval-detail-grid code { white-space: normal; overflow-wrap: anywhere; }
.approval-detail-notice, .approval-detail-warning { margin: 16px 0 0; padding: 10px 12px; border-radius: 8px; font-size: 13px; line-height: 1.55; }
.approval-detail-notice { color: var(--brand-700, #4e49bd); background: var(--brand-50, #f1efff); }
.approval-detail-warning { color: var(--color-danger, #d9304f); background: var(--color-danger-light, #fff2f3); }
.approval-request-context { margin-top: 18px; }
.approval-request-context h4 { margin: 0 0 8px; font-size: 13px; }
.approval-request-context pre { max-height: min(45vh, 360px); overflow: auto; padding: 12px; border-radius: 8px; background: var(--color-bg-page, #f5f6fa); overflow-wrap: anywhere; }
.approval-load-error {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  color: var(--color-danger, #d9304f);
}
@media (max-width: 860px) {
  .approval-scroll-hint {
    display: block;
    margin: 0 0 8px;
    color: var(--color-text-secondary, #737b8d);
    font-size: 12px;
  }
}

@media (max-width: 560px) {
  .approval-detail-grid { grid-template-columns: 1fr; }
}

.metric {
  padding: 14px 16px;
}

.metric span {
  display: block;
  color: var(--gray-500);
  font-size: 12px;
}

.metric strong {
  display: block;
  margin-top: 6px;
  font-size: 24px;
}

.compact-metrics {
  grid-template-columns: repeat(2, minmax(0, 1fr));
}

.panel h4 {
  margin: 18px 0 8px;
  font-size: 13px;
  color: var(--gray-700);
}

.observability-scope-note {
  margin: -2px 0 12px;
  color: var(--gray-500);
  font-size: 12px;
  line-height: 1.5;
}

.alert-list-error {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 8px;
  min-height: 64px;
  color: var(--color-danger, #d9304f);
}

.alert-pagination-wrapper {
  display: flex;
  justify-content: flex-end;
  margin-top: 12px;
  overflow-x: auto;
}

.alert-pagination-wrapper :deep(.el-pagination) {
  flex-wrap: wrap;
  justify-content: flex-end;
  row-gap: 8px;
}

.job-schedule-editor { display: grid; gap: 4px; }
.job-schedule-human {
  color: var(--gray-500);
  font-size: 11px;
  line-height: 1.3;
}

.form-control {
  display: grid;
  gap: 5px;
  min-width: 0;
  color: var(--gray-700);
  font-size: 12px;
  font-weight: 600;
}

.form-control :deep(.el-input),
.form-control :deep(.el-select) {
  width: 100%;
}

.content-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 16px;
}

.stack {
  display: grid;
  gap: 16px;
}

.panel {
  min-width: 0;
  padding: 16px;
  overflow-x: auto;
}

.panel h3 {
  margin: 0 0 12px;
  font-size: 15px;
}

.agent-load-status {
  margin: 0 0 12px;
  color: var(--gray-700);
  font-size: 13px;
}

.agent-directory { overflow-x: visible; }
.agent-system-disclosure {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  justify-content: space-between;
  gap: 8px 16px;
  margin: 4px 0 12px;
  padding: 10px 12px;
  border: 1px solid var(--color-border-light, #e5e7eb);
  border-radius: 8px;
  color: var(--color-text-secondary, #646b7a);
  font-size: 13px;
}
.agent-card-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(min(100%, 340px), 1fr));
  align-items: start;
  gap: 16px;
  margin-top: 12px;
}
.agent-card {
  min-width: 0;
  padding: 18px;
  border: 1px solid var(--color-border-light, #e5e7eb);
  border-radius: 12px;
  background: var(--color-bg-card, #fff);
  overflow-wrap: anywhere;
}
.agent-card-head { display: flex; align-items: flex-start; justify-content: space-between; gap: 12px; }
.agent-card-head > div { min-width: 0; }
.agent-card-head h3 { margin: 8px 0 0; font-size: 16px; line-height: 1.45; }
.agent-category { color: var(--brand-600, #5751ce); background: var(--brand-50, #f1efff); border-radius: 6px; padding: 3px 7px; font-size: 12px; }
.agent-enabled { flex-shrink: 0; padding-top: 3px; color: var(--color-text-secondary, #646b7a); font-size: 12px; }
.agent-enabled.is-disabled { color: var(--color-text-placeholder, #7c8493); }
.agent-responsibility { display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden; min-height: 40px; margin: 12px 0 16px; color: var(--color-text-secondary, #646b7a); font-size: 13px; line-height: 20px; }
.agent-card-metrics { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 8px; margin: 0 0 16px; padding: 12px 0; border-top: 1px solid var(--color-border-light, #e5e7eb); border-bottom: 1px solid var(--color-border-light, #e5e7eb); }
.agent-card-metrics dt, .agent-card-details dt { color: var(--color-text-secondary, #646b7a); font-size: 12px; }
.agent-card-metrics dd { margin: 6px 0 0; color: var(--color-text-primary, #303133); font-size: 18px; font-weight: 600; font-variant-numeric: tabular-nums; }
.agent-detail-toggle { display: flex; justify-content: space-between; gap: 12px; width: 100%; padding: 6px 0; border: 0; background: transparent; color: var(--brand-600, #5751ce); font: inherit; font-size: 13px; text-align: left; cursor: pointer; }
.agent-detail-toggle:focus-visible { outline: 2px solid var(--brand-500, #6a63df); outline-offset: 3px; border-radius: 3px; }
.agent-card-details { margin-top: 12px; padding-top: 12px; border-top: 1px dashed var(--color-border-light, #e5e7eb); }
.agent-card-details dl { display: grid; gap: 12px; margin: 0; }
.agent-card-details dd { margin: 5px 0 0; font-size: 13px; line-height: 1.6; }
.agent-card-details code { white-space: normal; overflow-wrap: anywhere; }
.agent-skill-list { display: flex; flex-wrap: wrap; gap: 6px; }
.agent-skill-list span { max-width: 100%; padding: 3px 7px; border-radius: 5px; background: var(--color-bg-page, #f5f6fa); }

.panel-heading {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  margin-bottom: 12px;
}

.panel-heading h3 {
  margin-bottom: 0;
}

.form-row {
  display: grid;
  grid-template-columns: repeat(3, minmax(120px, 1fr)) auto;
  gap: 10px;
}

.form-row.two {
  grid-template-columns: repeat(2, minmax(0, 1fr));
}

.toolbar-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(min(100%, 220px), 1fr));
  align-items: end;
  gap: 10px;
}

.toolbar-grid > * {
  min-width: 0;
}

.decision-line {
  margin-top: 10px;
  color: var(--gray-700);
  font-size: 13px;
}

pre {
  margin: 0;
  white-space: pre-wrap;
  font-size: 12px;
  color: var(--gray-700);
}

@media (max-width: 900px) {
  .content-grid,
  .form-row,
  .form-row.two {
    grid-template-columns: 1fr;
  }
}

/* cron 非法:输入框描红 */
:deep(.el-input.is-cron-invalid .el-input__wrapper) {
  box-shadow: 0 0 0 1px var(--color-danger) inset;
}

/* 统计卡入口态与警示数字 */
.metric.is-link {
  border: 0;
  padding: 0;
  font: inherit;
  text-align: inherit;
  cursor: pointer;
  transition: transform 0.15s ease;
}
.metric.is-link:hover { transform: translateY(-1px); }
.metric.is-link:hover span { color: var(--brand-600); }
.metric strong.is-warn { color: var(--color-danger); }
</style>
