<template>
  <div class="agent-office-page prism-page-shell">
    <header class="page-head prism-page-head">
      <div>
        <h2 class="page-title font-display">Agent 办公室</h2>
        <p class="page-sub">
          注册中心实时同步 ·
          <b class="hl">{{ runtime.length }}</b> 个 Agent 在岗 ·
          数据与后端 AgentRegistry 严格一致
        </p>
      </div>
      <div class="page-actions">
        <el-select
          v-model="filterCategory"
          placeholder="按角色筛选"
          clearable
          size="small"
          style="width: 140px"
        >
          <el-option
            v-for="opt in categoryOptions"
            :key="opt.value"
            :label="`${opt.label} (${opt.count})`"
            :value="opt.value"
          />
        </el-select>
        <el-button :loading="loading" @click="refreshAll">刷新</el-button>
        <el-button type="primary" @click="goRules">查看审查规则</el-button>
      </div>
    </header>

    <SituationPanel :data="situation" :loading="loading" />

    <PrismLoading
      v-if="loading && !runtime.length"
      label="正在同步 Agent 办公室"
      sublabel="正在从 AgentRegistry 拉取实时元数据"
    />

    <template v-else>
      <section class="overview-strip">
        <article
          v-for="bucket in summary.by_category"
          :key="bucket.category"
          class="bucket-card"
          :class="{ active: filterCategory === bucket.category }"
          @click="toggleCategory(bucket.category)"
        >
          <span class="bucket-num">{{ bucket.count }}</span>
          <span class="bucket-label">{{ categoryLabel(bucket.category) }}</span>
        </article>
      </section>

      <section class="desk-grid">
        <AgentDeskCard
          v-for="agent in filteredAgents"
          :key="agent.code"
          :agent="agent"
          @select="onAgentSelect"
        />
        <EmptyState
          v-if="!filteredAgents.length"
          description="当前筛选下没有匹配的 Agent"
          compact
        />
      </section>

      <section class="type-mapping-card">
        <h3 class="block-title">审查类型 → 代理组合映射</h3>
        <el-descriptions :column="1" border size="small">
          <el-descriptions-item
            v-for="m in typeMappings"
            :key="m.review_type"
            :label="m.label"
          >
            <el-tag
              v-for="code in m.agent_codes"
              :key="code"
              size="small"
              type="info"
              style="margin-right: 6px"
            >
              {{ code }}
            </el-tag>
          </el-descriptions-item>
        </el-descriptions>
      </section>
    </template>

    <el-drawer
      v-model="drawerVisible"
      :title="selectedAgent?.name || 'Agent 详情'"
      direction="rtl"
      size="420px"
    >
      <div v-if="selectedAgent" class="agent-detail">
        <div class="detail-head">
          <AgentAvatar
            :code="selectedAgent.code"
            :color="selectedAgent.color"
            :status="selectedAgent.status"
            :size="72"
          />
          <div>
            <div class="detail-title">{{ selectedAgent.name }}</div>
            <code class="detail-code">{{ selectedAgent.code }}</code>
            <div class="detail-cat">{{ categoryLabel(selectedAgent.category) }}</div>
          </div>
        </div>

        <p class="detail-desc">{{ selectedAgent.description }}</p>

        <div class="detail-section">
          <div class="detail-label">擅长能力</div>
          <div class="detail-skills">
            <el-tag
              v-for="skill in selectedAgent.skills"
              :key="skill"
              size="small"
              type="info"
              effect="plain"
            >
              {{ skill }}
            </el-tag>
          </div>
        </div>

        <div class="detail-section">
          <div class="detail-label">调用统计</div>
          <el-descriptions :column="2" border size="small">
            <el-descriptions-item label="总调用">{{ selectedAgent.call_count }}</el-descriptions-item>
            <el-descriptions-item label="成功">{{ selectedAgent.success_count }}</el-descriptions-item>
            <el-descriptions-item label="失败">{{ selectedAgent.failed_count }}</el-descriptions-item>
            <el-descriptions-item label="最近一次">
              {{ formatLast(selectedAgent.last_called_at) }}
            </el-descriptions-item>
            <el-descriptions-item label="使用模型" :span="2">
              <code>{{ selectedAgent.model || '—' }}</code>
            </el-descriptions-item>
          </el-descriptions>
        </div>

        <div class="detail-footer">
          <el-button type="primary" @click="invokeViaChat(selectedAgent)">
            通过 Agent 助手调用
          </el-button>
        </div>
      </div>
    </el-drawer>

    <AgentDiscussionPanel
      v-if="discussVisible && discussSessionId"
      :session-id="discussSessionId"
      :agents="discussAgents"
      :file-name="discussFileName"
      @close="closeDiscussPanel"
    />
  </div>
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRouter, useRoute } from 'vue-router'
import dayjs from 'dayjs'
import { ElMessage } from 'element-plus'
import AgentAvatar from '@/components/agent/AgentAvatar.vue'
import AgentDeskCard from '@/components/agent/AgentDeskCard.vue'
import SituationPanel from '@/components/agent/SituationPanel.vue'
import EmptyState from '@/components/common/EmptyState.vue'
import PrismLoading from '@/components/common/PrismLoading.vue'
import AgentDiscussionPanel from '@/components/agent/AgentDiscussionPanel.vue'
import { subscribeAgentEvents } from '@/utils/agentEventStream'
import type { AgentEvent, AgentEventType } from '@/types/agentEvent'
import {
  listRuntimeAgents,
  getRuntimeSummary,
  getSituation,
  listTypeMappings,
} from '@/api/agent'
import type {
  AgentRuntimeOut,
  AgentRuntimeSummaryOut,
  AgentSituationOut,
  AgentStatus,
  ReviewTypeMappingOut,
} from '@/types/agent'

const router = useRouter()
const route = useRoute()

const loading = ref(false)
const runtime = ref<AgentRuntimeOut[]>([])
const summary = ref<AgentRuntimeSummaryOut>({ total: 0, by_category: [] })
const situation = ref<AgentSituationOut | null>(null)
const typeMappings = ref<ReviewTypeMappingOut[]>([])
const filterCategory = ref<string>('')

const drawerVisible = ref(false)
const selectedAgent = ref<AgentRuntimeOut | null>(null)

const CATEGORY_LABELS: Record<string, string> = {
  meta: '主控',
  frontline: '前台',
  analyzer: '分析',
  reviewer: '审查',
  manager: '管理',
  orchestrator: '编排',
  analytics: '统计',
  output: '产出',
  security: '安全',
  general: '通用',
}

function categoryLabel(c: string): string {
  return CATEGORY_LABELS[c] ?? c
}

const categoryOptions = computed(() =>
  summary.value.by_category.map((b) => ({
    label: categoryLabel(b.category),
    value: b.category,
    count: b.count,
  })),
)

const EXECUTION_STATUSES: ReadonlySet<AgentStatus> = new Set(['thinking', 'working'])
const STATUS_PRIORITY: Record<AgentStatus, number> = {
  working: 0,
  thinking: 1,
  blocked: 2,
  error: 3,
  idle: 4,
  offline: 5,
}

const filteredAgents = computed(() => {
  const order = new Map(runtime.value.map((a, idx) => [a.code, idx]))
  const base = filterCategory.value
    ? runtime.value.filter((a) => a.category === filterCategory.value)
    : runtime.value
  return [...base].sort((a, b) => {
    const statusDelta = STATUS_PRIORITY[a.status] - STATUS_PRIORITY[b.status]
    if (statusDelta !== 0) return statusDelta
    return (order.get(a.code) ?? 0) - (order.get(b.code) ?? 0)
  })
})

function toggleCategory(c: string): void {
  filterCategory.value = filterCategory.value === c ? '' : c
}

function formatLast(t?: string | null): string {
  if (!t) return '尚未调用'
  return dayjs(t).format('YYYY-MM-DD HH:mm:ss')
}

function onAgentSelect(code: string): void {
  selectedAgent.value = runtime.value.find((a) => a.code === code) ?? null
  drawerVisible.value = !!selectedAgent.value
}

function invokeViaChat(agent: AgentRuntimeOut): void {
  drawerVisible.value = false
  window.dispatchEvent(new CustomEvent('prism:open-agent-chat', {
    detail: { prefill: `@${agent.code} ` },
  }))
}

function goRules(): void {
  router.push('/rules')
}
function goReview(): void {
  router.push('/reviews/start')
}

async function loadAll(): Promise<void> {
  loading.value = true
  try {
    const [r, s, sit, tm] = await Promise.all([
      listRuntimeAgents(),
      getRuntimeSummary(),
      getSituation(60),
      listTypeMappings(),
    ])
    runtime.value = r
    summary.value = s
    situation.value = sit
    typeMappings.value = tm
  } catch {
    ElMessage.error('加载 Agent 办公室数据失败')
  } finally {
    loading.value = false
  }
}

async function refreshAll(): Promise<void> {
  await loadAll()
  ElMessage.success('已同步最新数据')
}

// === v2.0 A2: 订阅 SSE,实时更新工位卡的 status 与 working 计数 ===

const STATUS_BY_EVENT: Record<AgentEventType, AgentStatus> = {
  dispatch: 'thinking',
  thinking: 'thinking',
  progress: 'working',
  complete: 'idle',
  failed: 'error',
  clarify: 'blocked',
}

const ERROR_TIMEOUT_MS = 6_000
const STATS_REFRESH_DELAY_MS = 800
const HEARTBEAT_REFRESH_MS = 15_000
const errorTimers = new Map<string, ReturnType<typeof setTimeout>>()
let stream: ReturnType<typeof subscribeAgentEvents> | null = null

function syncSituationActivityCounts(): void {
  if (!situation.value) return
  const online = situation.value.online || runtime.value.length
  const working = runtime.value.filter((x) => EXECUTION_STATUSES.has(x.status)).length
  situation.value = {
    ...situation.value,
    online,
    working,
    idle: Math.max(0, online - working),
  }
}

function setAgentStatus(code: string, status: AgentStatus): void {
  const a = runtime.value.find((x) => x.code === code)
  if (!a) {
    scheduleStatsRefresh()
    return
  }
  a.status = status
  syncSituationActivityCounts()
}

function clearErrorTimer(code: string): void {
  const t = errorTimers.get(code)
  if (t) {
    clearTimeout(t)
    errorTimers.delete(code)
  }
}

function handleAgentEvent(ev: AgentEvent): void {
  if (!ev.agent) return
  const nextStatus = STATUS_BY_EVENT[ev.type]
  if (!nextStatus) return
  clearErrorTimer(ev.agent)
  setAgentStatus(ev.agent, nextStatus)
  if (nextStatus === 'error') {
    errorTimers.set(ev.agent, setTimeout(() => {
      setAgentStatus(ev.agent, 'idle')
      errorTimers.delete(ev.agent)
    }, ERROR_TIMEOUT_MS))
  }
  // 终态事件触发统计刷新(debounce 合并多 agent 批量事件)
  if (ev.type === 'complete' || ev.type === 'failed' || ev.type === 'clarify') {
    scheduleStatsRefresh()
  }
}

// === 轻量统计刷新: 后端最新 EventBus 状态 + AiCallLog 统计为准 ===

let statsRefreshTimer: ReturnType<typeof setTimeout> | null = null

function scheduleStatsRefresh(): void {
  if (statsRefreshTimer) clearTimeout(statsRefreshTimer)
  statsRefreshTimer = setTimeout(refreshAgentStats, STATS_REFRESH_DELAY_MS)
}

async function refreshAgentStats(): Promise<void> {
  try {
    const [fresh, sit] = await Promise.all([
      listRuntimeAgents(),
      getSituation(60),
    ])
    runtime.value = fresh
    if (selectedAgent.value) {
      selectedAgent.value = runtime.value.find((a) => a.code === selectedAgent.value?.code) ?? null
    }
    situation.value = sit
    syncSituationActivityCounts()
  } catch {
    // 静默失败
  }
}

// === 60s 心跳定期全量刷新 (兜底) ===

let heartbeatTimer: ReturnType<typeof setInterval> | null = null

function ensureStream(): void {
  if (stream) return
  stream = subscribeAgentEvents(handleAgentEvent, {
    replay: 0,
    onError: () => { /* 重连机制内部处理,这里静默 */ },
  })
}

function teardownStream(): void {
  stream?.close()
  stream = null
  errorTimers.forEach((t) => clearTimeout(t))
  errorTimers.clear()
  if (statsRefreshTimer) clearTimeout(statsRefreshTimer)
  if (heartbeatTimer) clearInterval(heartbeatTimer)
}

onMounted(async () => {
  await loadAll()
  ensureStream()
  heartbeatTimer = setInterval(refreshAgentStats, HEARTBEAT_REFRESH_MS)
  // 从 ReviewStart 跳转过来时自动打开讨论面板
  initDiscussionFromRoute()
})

onBeforeUnmount(teardownStream)

// === v2.3 M7: 多 Agent 圆桌讨论 ===

const discussVisible = ref(false)
const discussSessionId = ref('')
const discussAgents = ref<Array<{ code: string; name: string }>>([])
const discussFileName = ref('')

function closeDiscussPanel() {
  discussVisible.value = false
  discussSessionId.value = ''
  discussFileName.value = ''
}

function onStartDiscussion(
  sessionId: string,
  agents: Array<{ code: string; name: string }>,
  fileName = '',
) {
  discussSessionId.value = sessionId
  discussAgents.value = agents
  discussFileName.value = fileName
  discussVisible.value = true
}

defineExpose({ onStartDiscussion })

function initDiscussionFromRoute() {
  const session = route.query.discuss_session as string
  const agentsJson = route.query.discuss_agents as string
  if (session && agentsJson) {
    try {
      const agents = JSON.parse(agentsJson)
      discussSessionId.value = session
      discussAgents.value = agents
      discussFileName.value = (route.query.discuss_file as string) || ''
      discussVisible.value = true
      // 仅清理地址栏查询串,不能用 router.replace:布局的 <router-view> 以
      // fullPath 为 key,任何 query 变化都会重挂载 AgentCenter,导致刚设置的
      // 讨论状态被重置、面板无法弹出。history API 不触发路由导航,故无重挂载。
      window.history.replaceState(window.history.state, '', route.path)
    } catch {
      // ignore
    }
  }
}
</script>

<style scoped lang="scss">
.agent-office-page {
  display: flex;
  flex-direction: column;
  gap: 16px;
}

.page-head {
  display: flex;
  align-items: flex-end;
  justify-content: space-between;
  gap: 16px;
  flex-wrap: wrap;
}

.page-title {
  margin: 0;
  font-size: 22px;
  font-weight: 600;
  letter-spacing: 0;
  color: var(--gray-900);
}

.page-sub {
  margin: 4px 0 0;
  font-size: 13px;
  color: var(--gray-500);

  .hl { color: var(--brand-600); font-weight: 600; }
}

.page-actions {
  display: flex;
  gap: 8px;
  align-items: center;
}

.overview-strip {
  display: flex;
  gap: 10px;
  overflow-x: auto;
  padding-bottom: 4px;
}

.bucket-card {
  flex-shrink: 0;
  background: var(--surface-1);
  border: var(--hairline);
  border-radius: 10px;
  padding: 10px 16px;
  display: flex;
  flex-direction: column;
  gap: 2px;
  cursor: pointer;
  transition: all 0.15s ease;
  min-width: 92px;

  &:hover {
    border-color: var(--brand-300);
    box-shadow: var(--shadow-1);
  }

  &.active {
    border-color: var(--brand-500);
    background: var(--brand-50);
  }

  .bucket-num {
    font-size: 20px;
    font-weight: 600;
    color: var(--gray-900);
    line-height: 1;
  }

  .bucket-label {
    font-size: 11.5px;
    color: var(--gray-500);
  }
}

.desk-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(320px, 1fr));
  gap: 14px;
}

.type-mapping-card {
  background: var(--surface-1);
  border: var(--hairline);
  border-radius: 10px;
  padding: 18px 20px;
}

.block-title {
  margin: 0 0 12px;
  font-size: 14px;
  font-weight: 600;
  color: var(--gray-900);
}

.agent-detail {
  padding: 0 8px;
  display: flex;
  flex-direction: column;
  gap: 18px;
}

.detail-head {
  display: flex;
  align-items: center;
  gap: 14px;
}

.detail-title {
  font-size: 16px;
  font-weight: 600;
  color: var(--gray-900);
}

.detail-code {
  display: inline-block;
  margin-top: 4px;
  font-size: 11px;
  color: var(--gray-500);
}

.detail-cat {
  margin-top: 2px;
  font-size: 11px;
  color: var(--brand-600);
}

.detail-desc {
  margin: 0;
  font-size: 13px;
  color: var(--gray-700);
  line-height: 1.6;
}

.detail-section {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.detail-label {
  font-size: 12px;
  color: var(--gray-500);
  letter-spacing: 0.04em;
}

.detail-skills {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}

.detail-footer {
  border-top: 1px solid var(--gray-100);
  padding-top: 16px;
  display: flex;
  justify-content: flex-end;
}
</style>
