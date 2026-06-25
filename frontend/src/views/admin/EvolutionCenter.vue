<template>
  <div class="evolution-page">
    <div class="page-header">
      <div>
        <h2>Agent 自进化中心</h2>
        <p class="page-sub">
          从审查反馈沉淀经验、蒸馏规则提案；经黄金集评估闸门 + 人工审批后生效，全程可解释、可回滚
        </p>
      </div>
      <div class="header-actions">
        <span class="window-label">反馈窗口</span>
        <el-select v-model="windowDays" style="width: 110px" @change="reloadAll">
          <el-option :value="30" label="近 30 天" />
          <el-option :value="90" label="近 90 天" />
          <el-option :value="180" label="近 180 天" />
          <el-option :value="365" label="近 365 天" />
        </el-select>
        <el-button :loading="running" type="primary" @click="onRun">运行一轮进化</el-button>
        <el-button :icon="Refresh" :loading="loading" @click="reloadAll">刷新</el-button>
      </div>
    </div>

    <!-- 反馈指标看板 -->
    <div class="stat-grid">
      <el-card shadow="hover" class="stat-card">
        <div class="stat-label">意见采纳率</div>
        <div class="stat-value ok">{{ pct(feedback?.overall_acceptance_rate) }}</div>
        <div class="stat-foot">已决样本 {{ feedback?.total_decided ?? 0 }} · 修复 {{ feedback?.total_fixed ?? 0 }}</div>
      </el-card>
      <el-card shadow="hover" class="stat-card">
        <div class="stat-label">假阳性率（噪声）</div>
        <div class="stat-value" :class="fpClass(feedback?.overall_false_positive_rate)">
          {{ pct(feedback?.overall_false_positive_rate) }}
        </div>
        <div class="stat-foot">忽略 {{ feedback?.total_ignored ?? 0 }} 条</div>
      </el-card>
      <el-card shadow="hover" class="stat-card">
        <div class="stat-label">待审批提案</div>
        <div class="stat-value">{{ pendingCount }}</div>
        <div class="stat-foot">提案总数 {{ proposals.length }}</div>
      </el-card>
      <el-card shadow="hover" class="stat-card">
        <div class="stat-label">经验记忆 / 黄金集</div>
        <div class="stat-value">{{ experiences.length }} / {{ evalCases.length }}</div>
        <div class="stat-foot">高权重经验注入审查 Prompt</div>
      </el-card>
    </div>

    <!-- v3.0 AgentSkill 升级:per-Agent 自进化控制台 -->
    <el-card shadow="never" class="per-agent-card">
      <template #header>
        <div class="per-agent-head">
          <div>
            <h3 class="block-title">per-Agent 自进化控制台</h3>
            <p class="block-sub">
              选择单个 Agent 触发其专属 self_improve Skill,查看该 Agent 最近的自进化调用记录;
              与全局"运行一轮进化"互补,支持细粒度按 Agent 治理
            </p>
          </div>
          <div class="per-agent-actions">
            <el-select
              v-model="selectedAgentName"
              placeholder="选择 Agent"
              filterable
              style="width: 220px"
              @change="onAgentChange"
            >
              <el-option
                v-for="a in runtimeAgents"
                :key="a.code"
                :label="`${a.name} (${a.code})`"
                :value="a.code"
              />
            </el-select>
            <el-button
              type="warning"
              :loading="triggering"
              :disabled="!selectedAgentName"
              @click="onTriggerAgent"
            >
              触发该 Agent 自进化
            </el-button>
            <el-button :icon="Refresh" :loading="recordsLoading" :disabled="!selectedAgentName" @click="loadAgentRecords">
              刷新记录
            </el-button>
          </div>
        </div>
      </template>

      <el-table
        v-loading="recordsLoading"
        :data="agentSkillRecords"
        stripe
        empty-text="选择 Agent 后展示其 self_improve / proactive Skill 调用记录"
        max-height="320"
      >
        <el-table-column label="Skill" min-width="200">
          <template #default="{ row }">
            <code class="rec-skill">{{ row.skill_name }}</code>
          </template>
        </el-table-column>
        <el-table-column label="触发类型" width="120">
          <template #default="{ row }">
            <el-tag size="small" :type="triggerTypeTag(row.trigger_type)">
              {{ triggerTypeLabel(row.trigger_type) }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="效果" width="100">
          <template #default="{ row }">
            <span :class="effectClass(row.effect)">{{ row.effect || '-' }}</span>
          </template>
        </el-table-column>
        <el-table-column label="耗时" width="100">
          <template #default="{ row }">{{ row.duration_ms ? `${row.duration_ms}ms` : '-' }}</template>
        </el-table-column>
        <el-table-column label="触发来源" min-width="200" show-overflow-tooltip>
          <template #default="{ row }">
            <span class="text-muted">{{ row.trigger_source || '-' }}</span>
          </template>
        </el-table-column>
        <el-table-column label="输出摘要" min-width="220" show-overflow-tooltip>
          <template #default="{ row }">{{ row.output_summary || '-' }}</template>
        </el-table-column>
        <el-table-column label="时间" width="170">
          <template #default="{ row }">{{ row.create_time ? formatDateTime(row.create_time) : '-' }}</template>
        </el-table-column>
      </el-table>
    </el-card>

    <el-card shadow="never" class="main-card">
      <el-tabs v-model="activeTab">
        <!-- 提案审批台 -->
        <el-tab-pane name="proposals">
          <template #label>提案审批台 <el-badge v-if="pendingCount" :value="pendingCount" type="warning" /></template>
          <div class="filter-bar">
            <el-select v-model="statusFilter" placeholder="全部状态" clearable style="width: 160px" @change="loadProposals">
              <el-option v-for="(label, key) in STATUS_LABELS" :key="key" :label="label" :value="key" />
            </el-select>
            <span class="hint">提案默认不生效，需先「评估」过闸门，再由管理员「审批」写入规则；已生效可「回滚」。</span>
          </div>
          <el-table v-loading="loading" :data="proposals" stripe empty-text="暂无提案，点右上角「运行一轮进化」生成">
            <el-table-column label="类型" width="110">
              <template #default="{ row }">
                <el-tag size="small" :type="typeTag(row.proposal_type)">{{ typeLabel(row.proposal_type) }}</el-tag>
              </template>
            </el-table-column>
            <el-table-column prop="title" label="提案" min-width="240" show-overflow-tooltip />
            <el-table-column label="证据" min-width="180" show-overflow-tooltip>
              <template #default="{ row }">
                <span class="text-muted">{{ evidenceBrief(row) }}</span>
              </template>
            </el-table-column>
            <el-table-column label="闸门" width="120">
              <template #default="{ row }">
                <span v-if="row.eval_score" :class="gatePassed(row) ? 'gate-ok' : 'gate-bad'">
                  {{ gateBrief(row) }}
                </span>
                <span v-else class="text-muted">未评估</span>
              </template>
            </el-table-column>
            <el-table-column label="状态" width="110">
              <template #default="{ row }">
                <el-tag size="small" :type="statusTag(row.status)">{{ statusLabel(row.status) }}</el-tag>
              </template>
            </el-table-column>
            <el-table-column label="操作" width="280" fixed="right">
              <template #default="{ row }">
                <el-button link type="info" size="small" @click="openDetail(row)">详情</el-button>
                <el-button
                  v-if="canEvaluate(row.status)" link type="primary" size="small"
                  :loading="busyId === row.id" @click="onEvaluate(row)"
                >评估</el-button>
                <el-button
                  v-if="row.status === 'eval_passed'" link type="success" size="small"
                  :loading="busyId === row.id" @click="onApprove(row)"
                >审批生效</el-button>
                <el-button
                  v-if="canReject(row.status)" link type="warning" size="small"
                  @click="onReject(row)"
                >驳回</el-button>
                <el-button
                  v-if="row.status === 'promoted'" link type="danger" size="small"
                  @click="onRollback(row)"
                >回滚</el-button>
              </template>
            </el-table-column>
          </el-table>
        </el-tab-pane>

        <!-- 经验记忆 -->
        <el-tab-pane name="experiences" label="经验记忆">
          <el-table v-loading="loading" :data="experiences" stripe empty-text="暂无经验，运行进化后从已修复问题沉淀">
            <el-table-column prop="issue_type" label="类型" width="110" />
            <el-table-column prop="title" label="代表问题" min-width="200" show-overflow-tooltip />
            <el-table-column prop="language" label="语言" width="90" />
            <el-table-column label="确认/忽略" width="110">
              <template #default="{ row }">
                <span class="gate-ok">{{ row.accepted_count }}</span> /
                <span class="text-muted">{{ row.rejected_count }}</span>
              </template>
            </el-table-column>
            <el-table-column label="权重" width="100" sortable :sort-method="(a:any,b:any)=>a.weight-b.weight">
              <template #default="{ row }">{{ row.weight?.toFixed(2) }}</template>
            </el-table-column>
            <el-table-column prop="canonical_suggestion" label="参考修复" min-width="220" show-overflow-tooltip />
            <el-table-column label="最近出现" width="170">
              <template #default="{ row }">{{ row.last_seen ? formatDateTime(row.last_seen) : '-' }}</template>
            </el-table-column>
          </el-table>
        </el-tab-pane>

        <!-- 黄金集 -->
        <el-tab-pane name="eval" label="黄金回归集">
          <p class="hint pad">评估闸门基准：进化提案 promote 前须在这些人工锚点上召回不退化、噪声不上升。</p>
          <el-table v-loading="loading" :data="evalCases" stripe empty-text="暂无黄金集">
            <el-table-column prop="name" label="用例" min-width="180" />
            <el-table-column prop="language" label="语言" width="90" />
            <el-table-column prop="tags" label="标签" width="130" />
            <el-table-column label="期望命中" min-width="200">
              <template #default="{ row }">
                <span class="text-muted">{{ expectedBrief(row) }}</span>
              </template>
            </el-table-column>
            <el-table-column label="来源" width="120">
              <template #default="{ row }">
                <el-tag size="small" :type="row.source === 'seed' ? 'info' : 'success'">
                  {{ row.source === 'seed' ? '内置锚点' : '反馈固化' }}
                </el-tag>
              </template>
            </el-table-column>
            <el-table-column label="启用" width="80">
              <template #default="{ row }">{{ row.enabled ? '是' : '否' }}</template>
            </el-table-column>
          </el-table>
        </el-tab-pane>

        <!-- 反馈明细 -->
        <el-tab-pane name="feedback" label="反馈明细">
          <el-table :data="feedback?.by_issue_type ?? []" stripe empty-text="暂无已决反馈">
            <el-table-column prop="issue_type" label="问题类型" width="120" />
            <el-table-column prop="rule_type" label="规则类型" width="120" />
            <el-table-column label="采纳率" width="110">
              <template #default="{ row }">{{ pct(row.acceptance_rate) }}</template>
            </el-table-column>
            <el-table-column label="假阳性率" width="110">
              <template #default="{ row }">
                <span :class="fpClass(row.false_positive_rate)">{{ pct(row.false_positive_rate) }}</span>
              </template>
            </el-table-column>
            <el-table-column prop="decided" label="已决数" width="90" />
            <el-table-column prop="fixed" label="修复" width="80" />
            <el-table-column prop="ignored" label="忽略" width="80" />
            <el-table-column prop="distinct_ignored_tasks" label="忽略跨任务" width="110" />
          </el-table>
        </el-tab-pane>
      </el-tabs>
    </el-card>

    <!-- 提案详情抽屉 -->
    <el-drawer v-model="detailVisible" title="进化提案详情" size="46%">
      <div v-if="detail" class="detail">
        <el-descriptions :column="1" border>
          <el-descriptions-item label="提案">{{ detail.title }}</el-descriptions-item>
          <el-descriptions-item label="类型">{{ typeLabel(detail.proposal_type) }}</el-descriptions-item>
          <el-descriptions-item label="状态">
            <el-tag size="small" :type="statusTag(detail.status)">{{ statusLabel(detail.status) }}</el-tag>
          </el-descriptions-item>
          <el-descriptions-item label="创建">{{ detail.create_time ? formatDateTime(detail.create_time) : '-' }}</el-descriptions-item>
          <el-descriptions-item v-if="detail.note" label="备注">{{ detail.note }}</el-descriptions-item>
        </el-descriptions>
        <h4>提案内容 payload</h4>
        <pre class="json">{{ pretty(detail.payload) }}</pre>
        <h4>支撑证据 evidence</h4>
        <pre class="json">{{ pretty(detail.evidence) }}</pre>
        <h4>评估闸门跑分 eval_score</h4>
        <pre class="json">{{ detail.eval_score ? pretty(detail.eval_score) : '（尚未评估）' }}</pre>
      </div>
    </el-drawer>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Refresh } from '@element-plus/icons-vue'
import { formatDateTime } from '@/utils/format'
import {
  approveProposal,
  evaluateProposal,
  getFeedback,
  listEvalCases,
  listExperiences,
  listProposals,
  rejectProposal,
  rollbackProposal,
  runEvolution,
  triggerEvolution,
} from '@/api/evolution'
import { listRuntimeAgents, listSkillRecords } from '@/api/agent'
import type {
  EvalCase,
  EvolutionProposal,
  FeedbackSummary,
  ReviewExperience,
} from '@/types/evolution'
import type {
  AgentRuntimeOut,
  AgentSkillRecordOut,
} from '@/types/agent'

const STATUS_LABELS: Record<string, string> = {
  pending: '待评估',
  eval_passed: '闸门通过',
  eval_failed: '闸门未过',
  approved: '已审批',
  promoted: '已生效',
  rejected: '已驳回',
  rolled_back: '已回滚',
}

const TYPE_LABELS: Record<string, string> = {
  new_rule: '新增规则',
  disable_rule: '禁用规则',
  adjust_severity: '调整严重度',
  narrow_language: '收窄语言',
  new_fewshot: '新增示例',
}

const windowDays = ref(90)
const activeTab = ref('proposals')
const statusFilter = ref('')
const loading = ref(false)
const running = ref(false)
const busyId = ref<number | null>(null)

const feedback = ref<FeedbackSummary | null>(null)
const proposals = ref<EvolutionProposal[]>([])
const experiences = ref<ReviewExperience[]>([])
const evalCases = ref<EvalCase[]>([])

const detailVisible = ref(false)
const detail = ref<EvolutionProposal | null>(null)

const pendingCount = computed(
  () => proposals.value.filter((p) => ['pending', 'eval_passed', 'eval_failed'].includes(p.status)).length,
)

// === v3.0 AgentSkill 升级:per-Agent 自进化控制台 ===
const runtimeAgents = ref<AgentRuntimeOut[]>([])
const selectedAgentName = ref<string>('')
const agentSkillRecords = ref<AgentSkillRecordOut[]>([])
const recordsLoading = ref(false)
const triggering = ref(false)

/**
 * 触发类型中文标签
 * @param t - 触发类型(manual/scheduled/event/orchestrator)
 * @returns 中文标签
 */
function triggerTypeLabel(t: string): string {
  const map: Record<string, string> = {
    manual: '手动',
    scheduled: '定时',
    event: '事件',
    orchestrator: '调度',
  }
  return map[t] ?? t
}

/**
 * 触发类型对应的 el-tag type
 * @param t - 触发类型
 * @returns el-tag type
 */
function triggerTypeTag(t: string): 'primary' | 'success' | 'warning' | 'info' | 'danger' {
  const map: Record<string, 'primary' | 'success' | 'warning' | 'info' | 'danger'> = {
    manual: 'warning',
    scheduled: 'info',
    event: 'primary',
    orchestrator: 'success',
  }
  return map[t] ?? 'info'
}

/**
 * 效果标记样式类
 * @param e - 效果标记(success/no_change/failed)
 * @returns CSS 类名
 */
function effectClass(e?: string | null): string {
  if (e === 'success') return 'gate-ok'
  if (e === 'failed') return 'gate-bad'
  if (e === 'no_change') return 'text-muted'
  return ''
}

/**
 * Agent 选择变化时加载该 Agent 的 Skill 调用记录
 */
function onAgentChange(): void {
  if (selectedAgentName.value) {
    loadAgentRecords()
  } else {
    agentSkillRecords.value = []
  }
}

/**
 * 加载选中 Agent 的 Skill 调用记录
 * 查询该 Agent 最近 20 条 self_improve / proactive Skill 记录
 */
async function loadAgentRecords(): Promise<void> {
  if (!selectedAgentName.value) return
  recordsLoading.value = true
  try {
    agentSkillRecords.value = await listSkillRecords({
      agentName: selectedAgentName.value,
      limit: 20,
    })
  } catch {
    ElMessage.error('加载 Agent Skill 调用记录失败')
  } finally {
    recordsLoading.value = false
  }
}

/**
 * 触发选中 Agent 的自进化 Skill
 * 调用 POST /api/evolution/trigger?agent_name=xxx&window_days=90
 */
async function onTriggerAgent(): Promise<void> {
  if (!selectedAgentName.value) return
  triggering.value = true
  try {
    const res = await triggerEvolution(selectedAgentName.value, windowDays.value)
    const effect = (res as Record<string, string>).effect || 'unknown'
    const duration = (res as Record<string, number>).duration_ms
    if (effect === 'success') {
      ElMessage.success(`已触发 ${selectedAgentName.value} 自进化(${duration ?? 0}ms)`)
    } else if (effect === 'no_change') {
      ElMessage.info(`${selectedAgentName.value} 自进化完成,本轮无新提案(样本不足或已收敛)`)
    } else {
      ElMessage.warning(`${selectedAgentName.value} 自进化效果:${effect}`)
    }
    // 刷新记录与全局提案
    await loadAgentRecords()
    await loadProposals()
  } catch {
    ElMessage.error('触发自进化失败')
  } finally {
    triggering.value = false
  }
}

function pct(v?: number): string {
  return v === undefined || v === null ? '-' : `${(v * 100).toFixed(1)}%`
}
function fpClass(v?: number): string {
  if (v === undefined || v === null) return ''
  return v >= 0.6 ? 'bad' : v >= 0.3 ? 'warn' : 'ok'
}
function statusLabel(s: string): string {
  return STATUS_LABELS[s] ?? s
}
function statusTag(s: string): 'success' | 'warning' | 'danger' | 'info' | 'primary' {
  const map: Record<string, 'success' | 'warning' | 'danger' | 'info' | 'primary'> = {
    pending: 'info',
    eval_passed: 'primary',
    eval_failed: 'danger',
    promoted: 'success',
    rejected: 'info',
    rolled_back: 'warning',
  }
  return map[s] ?? 'primary'
}
function typeLabel(t: string): string {
  return TYPE_LABELS[t] ?? t
}
function typeTag(t: string): 'success' | 'warning' | 'danger' | 'info' | 'primary' {
  const map: Record<string, 'success' | 'warning' | 'danger' | 'info' | 'primary'> = {
    new_rule: 'success',
    disable_rule: 'danger',
    adjust_severity: 'warning',
    narrow_language: 'info',
    new_fewshot: 'success',
  }
  return map[t] ?? 'primary'
}

function canEvaluate(s: string): boolean {
  return ['pending', 'eval_passed', 'eval_failed'].includes(s)
}
function canReject(s: string): boolean {
  return ['pending', 'eval_passed', 'eval_failed'].includes(s)
}

function evidenceBrief(row: EvolutionProposal): string {
  const e = (row.evidence ?? {}) as Record<string, unknown>
  if (e.false_positive_rate !== undefined) {
    return `假阳性率 ${pct(Number(e.false_positive_rate))} · 样本 ${e.decided ?? '?'} · 跨 ${e.distinct_ignored_tasks ?? '?'} 任务`
  }
  if (e.accepted_count !== undefined) {
    return `历史确认 ${e.accepted_count} 次 · 权重 ${e.weight ?? '?'}`
  }
  return '-'
}
function gatePassed(row: EvolutionProposal): boolean {
  return !!(row.eval_score as Record<string, unknown> | null)?.passed
}
function gateBrief(row: EvolutionProposal): string {
  const s = (row.eval_score ?? {}) as Record<string, unknown>
  if (s.reason === 'no_eval_cases') return '无黄金集'
  if (s.recall_before === undefined) return gatePassed(row) ? '通过' : '未过'
  return `召回 ${s.recall_before}→${s.recall_after}`
}
function expectedBrief(row: EvalCase): string {
  const arr = Array.isArray(row.expected_issues) ? row.expected_issues : []
  return arr.map((x: Record<string, unknown>) => x.issue_type).filter(Boolean).join('、') || '-'
}
function pretty(v: unknown): string {
  if (v === null || v === undefined) return '（空）'
  return JSON.stringify(v, null, 2)
}

async function loadFeedback(): Promise<void> {
  feedback.value = await getFeedback(windowDays.value)
}
async function loadProposals(): Promise<void> {
  proposals.value = await listProposals(statusFilter.value)
}
async function loadExperiences(): Promise<void> {
  experiences.value = await listExperiences(100)
}
async function loadEvalCases(): Promise<void> {
  evalCases.value = await listEvalCases()
}

async function reloadAll(): Promise<void> {
  loading.value = true
  try {
    await Promise.all([
      loadFeedback(),
      loadProposals(),
      loadExperiences(),
      loadEvalCases(),
      loadRuntimeAgents(),
    ])
  } finally {
    loading.value = false
  }
}

/**
 * 加载 AgentRegistry 中的所有 Agent,供 per-Agent 选择器使用
 */
async function loadRuntimeAgents(): Promise<void> {
  try {
    runtimeAgents.value = await listRuntimeAgents()
  } catch {
    // 静默失败,选择器为空时用户仍可使用全局进化
  }
}

async function onRun(): Promise<void> {
  running.value = true
  try {
    const r = await runEvolution(windowDays.value)
    ElMessage.success(
      `进化完成：沉淀经验 ${r.harvest.clusters} 条，新增提案 ${r.agent.created ?? 0} 个`
      + (r.agent.skipped ? `（去重跳过 ${r.agent.skipped}）` : ''),
    )
    await reloadAll()
  } finally {
    running.value = false
  }
}

async function onEvaluate(row: EvolutionProposal): Promise<void> {
  busyId.value = row.id
  try {
    const p = await evaluateProposal(row.id)
    ElMessage[p.status === 'eval_passed' ? 'success' : 'warning'](
      p.status === 'eval_passed' ? '评估通过，可审批生效' : '未通过闸门（召回退化或无黄金集）',
    )
    await Promise.all([loadProposals(), loadFeedback()])
  } finally {
    busyId.value = null
  }
}

async function onApprove(row: EvolutionProposal): Promise<void> {
  await ElMessageBox.confirm(
    `确认审批生效？将写入审查规则并在下次审查自动生效。\n${row.title}`,
    '审批生效', { type: 'warning' },
  )
  busyId.value = row.id
  try {
    await approveProposal(row.id)
    ElMessage.success('已生效，可在「审查规则」查看；如需撤回可在此回滚')
    await loadProposals()
  } finally {
    busyId.value = null
  }
}

async function onReject(row: EvolutionProposal): Promise<void> {
  const { value } = await ElMessageBox.prompt('请填写驳回原因', '驳回提案', {
    inputPlaceholder: '为何不采纳该提案',
  })
  await rejectProposal(row.id, value || '')
  ElMessage.success('已驳回')
  await loadProposals()
}

async function onRollback(row: EvolutionProposal): Promise<void> {
  const { value } = await ElMessageBox.prompt('请填写回滚说明', '回滚已生效提案', {
    inputPlaceholder: '为何撤回',
    type: 'warning',
  })
  await rollbackProposal(row.id, value || '')
  ElMessage.success('已回滚，规则恢复改动前状态')
  await loadProposals()
}

function openDetail(row: EvolutionProposal): void {
  detail.value = row
  detailVisible.value = true
}

onMounted(reloadAll)
</script>

<style scoped lang="scss">
.evolution-page {
  padding: var(--spacing-lg, 24px);
}

.page-header {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  gap: 16px;
  flex-wrap: wrap;
  margin-bottom: var(--spacing-lg, 24px);

  h2 {
    margin: 0 0 4px;
    font-size: 20px;
    font-weight: 600;
  }

  .page-sub {
    margin: 0;
    color: var(--color-text-secondary, #909399);
    font-size: 13px;
    max-width: 720px;
  }
}

.header-actions {
  display: flex;
  align-items: center;
  gap: 10px;

  .window-label {
    font-size: 13px;
    color: var(--color-text-secondary, #909399);
  }
}

.stat-grid {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: 16px;
  margin-bottom: 16px;
}

/* v3.0 AgentSkill 升级:per-Agent 自进化控制台卡片 */
.per-agent-card {
  margin-bottom: 16px;
  border-left: 3px solid var(--brand-500, #5B58E8);
}

.per-agent-head {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  gap: 16px;
  flex-wrap: wrap;
}

.per-agent-head .block-title {
  margin: 0 0 4px;
  font-size: 15px;
  font-weight: 600;
  color: var(--gray-900, #161A24);
}

.per-agent-head .block-sub {
  margin: 0;
  font-size: 12px;
  color: var(--color-text-secondary, #909399);
  max-width: 560px;
  line-height: 1.5;
}

.per-agent-actions {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
}

.rec-skill {
  font-size: 11.5px;
  color: var(--brand-600, #5B58E8);
  font-family: var(--font-mono, monospace);
}

.stat-card {
  .stat-label {
    font-size: 13px;
    color: var(--color-text-secondary, #909399);
  }
  .stat-value {
    font-size: 28px;
    font-weight: 600;
    margin: 6px 0 4px;
    &.ok { color: #2f9e44; }
    &.warn { color: #e8a33d; }
    &.bad { color: #e5484d; }
  }
  .stat-foot {
    font-size: 12px;
    color: var(--color-text-secondary, #909399);
  }
}

.main-card {
  :deep(.el-tabs__header) {
    margin-bottom: 12px;
  }
}

.filter-bar {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-bottom: 12px;
  flex-wrap: wrap;
}

.hint {
  font-size: 12px;
  color: var(--color-text-secondary, #909399);
  &.pad { display: block; margin-bottom: 10px; }
}

.text-muted { color: var(--color-text-secondary, #909399); }
.gate-ok, .ok { color: #2f9e44; }
.gate-bad, .bad { color: #e5484d; }
.warn { color: #e8a33d; }

.detail {
  h4 { margin: 18px 0 6px; font-size: 13px; }
  .json {
    background: var(--color-bg-page, #f5f7fa);
    border-radius: 6px;
    padding: 12px;
    font-size: 12px;
    line-height: 1.5;
    overflow-x: auto;
    white-space: pre-wrap;
    word-break: break-all;
    margin: 0;
  }
}

@media (max-width: 900px) {
  .stat-grid { grid-template-columns: repeat(2, 1fr); }
}
</style>
