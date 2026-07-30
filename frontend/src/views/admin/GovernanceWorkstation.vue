<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { ElMessage } from 'element-plus/es/components/message/index'

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
  listAlerts,
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

type Mode = 'overview' | 'agents' | 'approvals' | 'policies' | 'tools' | 'knowledge' | 'jobs' | 'observability' | 'rewards' | 'rollback'

const props = defineProps<{
  mode: Mode
}>()

const loading = ref(false)
const overview = ref<GovernanceOverview | null>(null)
const agents = ref<GovernanceAgent[]>([])
const approvals = ref<ApprovalItem[]>([])
const policies = ref<PolicyRule[]>([])
const decisions = ref<PolicyDecision[]>([])
const tools = ref<ToolCallLog[]>([])
const toolPermissions = ref<AgentToolPermission[]>([])
const jobs = ref<AgentJob[]>([])
const alerts = ref<AgentAlert[]>([])
const observability = ref<Record<string, unknown>>({})
const selectedAgent = ref('')
const agentMemory = ref<AgentMemory[]>([])
const agentKnowledge = ref<AgentKnowledgeDoc[]>([])
const knowledgeSources = ref<AgentKnowledgeSource[]>([])
const rewardEvents = ref<AgentRewardEvent[]>([])
const artifactVersions = ref<AgentArtifactVersion[]>([])
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
    overview: 'Agent 治理总览',
    agents: 'Agent 管理',
    approvals: '审批中心',
    policies: '策略中心',
    tools: '工具权限',
    knowledge: '知识与记忆',
    jobs: '任务调度',
    observability: '监控告警',
    rewards: '奖惩趋势',
    rollback: '回滚中心',
  }
  return titles[props.mode]
})

/**
 * 加载当前模式所需的管理端数据。
 * @returns Promise<void>
 */
async function loadData(): Promise<void> {
  loading.value = true
  try {
    if (props.mode === 'overview') {
      overview.value = await getGovernanceOverview()
      agents.value = await listGovernanceAgents()
      approvals.value = await listApprovals()
      alerts.value = await listAlerts()
    } else if (props.mode === 'agents') {
      agents.value = await listGovernanceAgents()
    } else if (props.mode === 'approvals') {
      approvals.value = await listApprovals()
    } else if (props.mode === 'policies') {
      policies.value = await listPolicies()
      decisions.value = await listPolicyDecisions()
    } else if (props.mode === 'tools') {
      const [calls, permissions] = await Promise.all([listToolCalls(), listToolPermissions()])
      tools.value = calls
      toolPermissions.value = permissions
    } else if (props.mode === 'knowledge') {
      agents.value = await listGovernanceAgents()
      selectedAgent.value = selectedAgent.value || agents.value[0]?.code || ''
      await loadAgentKnowledge()
    } else if (props.mode === 'jobs') {
      jobs.value = await listJobs()
      jobEdit.value = Object.fromEntries(jobs.value.map((job) => [job.id, { schedule: job.schedule, status: job.status }]))
    } else if (props.mode === 'observability') {
      observability.value = await getObservabilityOverview()
      alerts.value = await listAlerts()
    } else if (props.mode === 'rewards') {
      observability.value = await getObservabilityOverview()
      rewardEvents.value = await listRewardEvents()
    } else if (props.mode === 'rollback') {
      artifactVersions.value = await listArtifactVersions()
    }
  } finally {
    loading.value = false
  }
}

/**
 * 加载选中 Agent 的知识和记忆。
 * @returns Promise<void>
 */
async function loadAgentKnowledge(): Promise<void> {
  if (!selectedAgent.value) {
    agentMemory.value = []
    agentKnowledge.value = []
    return
  }
  const [memory, knowledge] = await Promise.all([
    listAgentMemory(selectedAgent.value),
    listAgentKnowledge(selectedAgent.value),
  ])
  agentMemory.value = memory
  agentKnowledge.value = knowledge
  knowledgeSources.value = await listAgentKnowledgeSources(selectedAgent.value)
  toolPermissionForm.value.agent_code = selectedAgent.value
  rewardForm.value.agent_code = selectedAgent.value
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
  await upsertPolicy({ ...policyEditor.value, condition_json: {} })
  ElMessage.success('策略规则已保存')
  await loadData()
}

/**
 * 保存工具权限。
 * @returns Promise<void>
 */
async function onSaveToolPermission(): Promise<void> {
  await upsertToolPermission({ ...toolPermissionForm.value })
  ElMessage.success('工具权限已保存')
  await loadData()
}

/**
 * 审批通过指定事项。
 * @param row - 审批事项。
 * @returns Promise<void>
 */
async function onApprove(row: ApprovalItem): Promise<void> {
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
  const data = jobEdit.value[row.id]
  await updateJob(row.id, { schedule: data.schedule, status: data.status })
  ElMessage.success('任务配置已保存')
  await loadData()
}

/**
 * 创建 Agent 独立记忆。
 * @returns Promise<void>
 */
async function onCreateMemory(): Promise<void> {
  if (!selectedAgent.value) return
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
  if (!selectedAgent.value) return
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
  await resolveAlert(row.id, '管理端关闭告警')
  ElMessage.success('告警已关闭')
  await loadData()
}

/**
 * 记录奖惩事件。
 * @returns Promise<void>
 */
async function onCreateReward(): Promise<void> {
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
  await rollbackArtifactVersion(row.id)
  ElMessage.success('已回滚版本')
  await loadData()
}

watch(() => props.mode, loadData)
watch(selectedAgent, loadAgentKnowledge)

onMounted(loadData)
</script>

<template>
  <div v-loading="loading" class="governance-page">
    <div class="page-head">
      <div>
        <h2>{{ pageTitle }}</h2>
        <p>集中治理 Agent、策略、审批、工具、知识、调度和观测数据。</p>
      </div>
      <el-button @click="loadData">刷新</el-button>
    </div>

    <template v-if="mode === 'overview'">
      <div class="metric-grid">
        <div class="metric"><span>Agent 总数</span><strong>{{ overview?.agents_total ?? 0 }}</strong></div>
        <div class="metric"><span>启用 Agent</span><strong>{{ overview?.agents_enabled ?? 0 }}</strong></div>
        <div class="metric"><span>待审批</span><strong>{{ overview?.approvals_pending ?? 0 }}</strong></div>
        <div class="metric"><span>工具调用</span><strong>{{ overview?.tool_calls_today ?? 0 }}</strong></div>
        <div class="metric"><span>开放告警</span><strong>{{ overview?.alerts_open ?? 0 }}</strong></div>
        <div class="metric"><span>知识文档</span><strong>{{ overview?.knowledge_docs_total ?? 0 }}</strong></div>
      </div>
      <div class="content-grid">
        <section class="panel">
          <h3>Agent 状态</h3>
          <el-table :data="agents" height="360">
            <el-table-column prop="name" label="Agent(智能体)" min-width="140" />
            <el-table-column prop="category" label="分类" width="110" />
            <el-table-column prop="status" label="状态" width="100" />
            <el-table-column prop="priority" label="优先级" width="90" />
          </el-table>
        </section>
        <section class="panel">
          <h3>待处理审批</h3>
          <el-table :data="approvals" height="360">
            <el-table-column prop="title" label="事项" min-width="180" />
            <el-table-column prop="risk_level" label="风险" width="90" />
            <el-table-column prop="status" label="状态" width="110" />
          </el-table>
        </section>
      </div>
    </template>

    <section v-else-if="mode === 'agents'" class="panel">
      <el-table :data="agents" stripe>
        <el-table-column prop="name" label="Agent(智能体)" min-width="150" />
        <el-table-column prop="code" label="编码" min-width="140" />
        <el-table-column prop="category" label="分类" width="120" />
        <el-table-column label="Skill(技能)" min-width="220">
          <template #default="{ row }">{{ row.skills.join(', ') }}</template>
        </el-table-column>
        <el-table-column prop="priority" label="优先级" width="90" />
        <el-table-column prop="auto_approval_threshold" label="审批阈值" width="100" />
        <el-table-column prop="memory_count" label="记忆" width="80" />
        <el-table-column prop="knowledge_count" label="知识" width="80" />
      </el-table>
    </section>

    <section v-else-if="mode === 'approvals'" class="panel">
      <el-table :data="approvals" stripe>
        <el-table-column prop="title" label="审批事项" min-width="220" />
        <el-table-column prop="agent_code" label="Agent(智能体)" width="120" />
        <el-table-column prop="action" label="动作" min-width="150" />
        <el-table-column prop="risk_level" label="风险" width="90" />
        <el-table-column prop="status" label="状态" width="120" />
        <el-table-column label="操作" width="150">
          <template #default="{ row }">
            <el-button v-if="row.status === 'pending'" link type="success" @click="onApprove(row)">通过</el-button>
            <el-button v-if="row.status === 'pending'" link type="danger" @click="onReject(row)">驳回</el-button>
          </template>
        </el-table-column>
      </el-table>
    </section>

    <section v-else-if="mode === 'policies'" class="stack">
      <div class="panel">
        <h3>策略试算</h3>
        <div class="form-row">
          <el-input v-model="policyForm.subject" placeholder="主体" />
          <el-input v-model="policyForm.action" placeholder="动作" />
          <el-input v-model="policyForm.resource" placeholder="资源" />
          <el-button type="primary" @click="onEvaluatePolicy">试算</el-button>
        </div>
        <div v-if="policyResult" class="decision-line">
          {{ policyResult.decision }} · {{ policyResult.risk_level }} · {{ policyResult.reason }}
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
            <el-option label="严重(critical)" value="critical" />
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
          <el-table-column prop="name" label="规则" min-width="180" />
          <el-table-column prop="subject" label="主体" width="130" />
          <el-table-column prop="action" label="动作" width="140" />
          <el-table-column prop="effect" label="效果" width="90" />
          <el-table-column prop="risk_level" label="风险" width="90" />
          <el-table-column prop="enabled" label="启用" width="80" />
        </el-table>
      </div>
      <div class="panel">
        <h3>决策日志</h3>
        <el-table :data="decisions" stripe>
          <el-table-column prop="subject" label="主体" width="150" />
          <el-table-column prop="action" label="动作" min-width="160" />
          <el-table-column prop="decision" label="决策" width="90" />
          <el-table-column prop="risk_level" label="风险" width="90" />
          <el-table-column prop="reason" label="原因" min-width="220" show-overflow-tooltip />
        </el-table>
      </div>
    </section>

    <section v-else-if="mode === 'tools'" class="stack">
      <div class="panel">
        <h3>工具权限配置</h3>
        <div class="toolbar-grid">
          <el-input v-model="toolPermissionForm.agent_code" placeholder="Agent 编码" />
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
            <el-option label="严重(critical)" value="critical" />
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
          <el-table-column prop="agent_code" label="Agent(智能体)" width="130" />
          <el-table-column prop="tool_code" label="工具" width="130" />
          <el-table-column prop="permission" label="权限" width="110" />
          <el-table-column prop="risk_level" label="风险" width="90" />
          <el-table-column prop="enabled" label="启用" width="80" />
          <el-table-column prop="note" label="备注" min-width="180" show-overflow-tooltip />
        </el-table>
      </div>
      <div class="panel">
        <h3>工具调用日志</h3>
        <el-table :data="tools" stripe>
          <el-table-column prop="agent_code" label="Agent(智能体)" width="130" />
          <el-table-column prop="tool_code" label="工具" width="130" />
          <el-table-column prop="action" label="动作" min-width="160" />
          <el-table-column prop="decision" label="决策" width="90" />
          <el-table-column prop="status" label="状态" width="100" />
          <el-table-column prop="risk_level" label="风险" width="90" />
          <el-table-column prop="duration_ms" label="耗时(ms)" width="110" />
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
          <el-button type="primary" @click="onCrawlKnowledge">抓取知识</el-button>
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
                <el-option label="严重(critical)" value="critical" />
              </el-select>
              <el-input-number v-model="knowledgeDocForm.confidence" :min="0" :max="1" :step="0.1" />
              <el-button type="primary" @click="onCreateKnowledgeDoc">提交知识</el-button>
            </div>
          </div>
        </div>
      </div>
      <div class="panel">
        <h3>知识来源</h3>
        <div class="toolbar-grid source-grid">
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
          <el-table-column prop="source_type" label="类型" width="100" />
          <el-table-column prop="source_uri" label="来源" min-width="240" show-overflow-tooltip />
          <el-table-column prop="whitelist" label="白名单" width="90" />
          <el-table-column prop="enabled" label="启用" width="80" />
        </el-table>
      </div>
      <div class="content-grid">
        <div class="panel">
          <h3>独立记忆</h3>
          <el-table :data="agentMemory" height="360">
            <el-table-column prop="title" label="标题" min-width="180" />
            <el-table-column prop="memory_type" label="类型" width="110" />
            <el-table-column prop="weight" label="权重" width="90" />
          </el-table>
        </div>
        <div class="panel">
          <h3>知识文档</h3>
          <el-table :data="agentKnowledge" height="360">
            <el-table-column prop="title" label="标题" min-width="180" />
            <el-table-column prop="source_type" label="来源" width="100" />
            <el-table-column prop="risk_level" label="风险" width="90" />
            <el-table-column prop="status" label="状态" width="130" />
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
        <el-table-column prop="job_code" label="任务" min-width="220" />
        <el-table-column prop="job_type" label="类型" width="110" />
        <el-table-column prop="agent_code" label="Agent(智能体)" width="150" />
        <el-table-column label="计划" width="190">
          <template #default="{ row }">
            <el-input v-if="jobEdit[row.id]" v-model="jobEdit[row.id].schedule" size="small" />
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
            <el-button link type="success" @click="onSaveJob(row)">保存</el-button>
          </template>
        </el-table-column>
      </el-table>
    </section>

    <section v-else-if="mode === 'observability'" class="content-grid">
      <div class="panel">
        <h3>观测指标</h3>
        <pre>{{ JSON.stringify(observability, null, 2) }}</pre>
      </div>
      <div class="panel">
        <h3>开放告警</h3>
        <el-table :data="alerts" height="360">
          <el-table-column prop="title" label="告警" min-width="180" />
          <el-table-column prop="severity" label="级别" width="90" />
          <el-table-column prop="status" label="状态" width="100" />
          <el-table-column label="操作" width="90">
            <template #default="{ row }">
              <el-button v-if="row.status === 'open'" link type="primary" @click="onResolveAlert(row)">关闭</el-button>
            </template>
          </el-table-column>
        </el-table>
      </div>
    </section>

    <section v-else-if="mode === 'rewards'" class="stack">
      <div class="panel">
        <h3>记录奖惩</h3>
        <div class="toolbar-grid">
          <el-input v-model="rewardForm.agent_code" placeholder="Agent 编码" />
          <el-select v-model="rewardForm.event_type">
            <el-option label="奖励(reward)" value="reward" />
            <el-option label="惩罚(penalty)" value="penalty" />
          </el-select>
          <el-input-number v-model="rewardForm.score" :min="-100" :max="100" controls-position="right" />
          <el-input v-model="rewardForm.reason" placeholder="原因" />
          <el-button type="primary" @click="onCreateReward">记录</el-button>
        </div>
      </div>
      <div class="panel">
        <h3>奖惩事件</h3>
        <el-table :data="rewardEvents" stripe>
          <el-table-column prop="agent_code" label="Agent(智能体)" width="140" />
          <el-table-column prop="event_type" label="类型" width="100" />
          <el-table-column prop="score" label="分数" width="90" />
          <el-table-column prop="reason" label="原因" min-width="220" show-overflow-tooltip />
        </el-table>
      </div>
    </section>

    <section v-else-if="mode === 'rollback'" class="stack">
      <div class="panel">
        <h3>创建版本</h3>
        <div class="toolbar-grid">
          <el-input v-model="artifactForm.agent_code" placeholder="Agent 编码" />
          <el-select v-model="artifactForm.artifact_type">
            <el-option label="策略(policy)" value="policy" />
            <el-option label="提示词(prompt)" value="prompt" />
            <el-option label="技能(skill)" value="skill" />
            <el-option label="知识(knowledge)" value="knowledge" />
            <el-option label="代码(code)" value="code" />
          </el-select>
          <el-input v-model="artifactForm.version" placeholder="版本" />
          <el-select v-model="artifactForm.status">
            <el-option label="草稿(draft)" value="draft" />
            <el-option label="灰度(gray)" value="gray" />
            <el-option label="稳定(stable)" value="stable" />
          </el-select>
          <el-input v-model="artifactForm.content" placeholder="内容" />
          <el-input v-model="artifactForm.snapshot" placeholder="回滚快照" />
          <el-button type="primary" @click="onCreateArtifactVersion">创建版本</el-button>
        </div>
      </div>
      <div class="panel">
        <h3>版本列表</h3>
        <el-table :data="artifactVersions" stripe>
          <el-table-column prop="agent_code" label="Agent(智能体)" width="130" />
          <el-table-column prop="artifact_type" label="类型" width="110" />
          <el-table-column prop="version" label="版本" min-width="160" />
          <el-table-column prop="status" label="状态" width="120" />
          <el-table-column label="操作" width="90">
            <template #default="{ row }">
              <el-button link type="primary" @click="onRollbackArtifact(row)">回滚</el-button>
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

.form-row {
  display: grid;
  grid-template-columns: repeat(3, minmax(120px, 1fr)) auto;
  gap: 10px;
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
  .form-row {
    grid-template-columns: 1fr;
  }
}
</style>
