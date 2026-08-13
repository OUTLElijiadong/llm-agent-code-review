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
          v-if="activeTab === 'office'"
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
        <el-button v-if="activeTab === 'office'" :loading="loading" @click="refreshAll">刷新</el-button>
        <el-button v-if="activeTab === 'office'" type="primary" @click="goRules">查看审查规则</el-button>
      </div>
    </header>

    <el-tabs v-model="activeTab" class="agent-tabs">
      <el-tab-pane label="Agent 办公室" name="office">
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
      </el-tab-pane>

      <el-tab-pane label="MetaGPT 编排" name="metagpt">
        <MetaGPTOrchestrationPanel />
      </el-tab-pane>
    </el-tabs>

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
            <el-tag v-if="selectedAgent.source === 'custom'" size="small" type="success" effect="plain">
              自定义 · v{{ selectedAgent.version_number }} · 发布 #{{ selectedAgent.release_id }}
            </el-tag>
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

        <!-- v3.0 AgentSkill 升级:per-Agent 自进化能力展示 -->
        <div class="detail-section">
          <div class="detail-label">
            自进化能力
            <span class="detail-label-hint">(Self-Improvement + Proactive)</span>
          </div>
          <PrismLoading
            v-if="skillsLoading"
            label="加载 Skill 元数据"
            sublabel="从 SkillRegistry 拉取 per-Agent 专属能力"
            compact
          />
          <template v-else-if="agentSkills.length">
            <div class="skill-cards">
              <div
                v-for="sk in agentSkills"
                :key="sk.name"
                class="skill-card"
                :class="`skill-${sk.type}`"
              >
                <div class="skill-card-head">
                  <span class="skill-type-badge" :class="`badge-${sk.type}`">
                    {{ skillTypeLabel(sk.type) }}
                  </span>
                  <code class="skill-name">{{ sk.name }}</code>
                </div>
                <p class="skill-desc">{{ sk.description }}</p>
                <div class="skill-card-foot">
                  <el-tag size="small" :type="sk.invocable ? 'success' : 'info'" effect="plain">
                    {{ sk.invocable ? '可手动调用' : '仅自动触发' }}
                  </el-tag>
                </div>
              </div>
            </div>
          </template>
          <EmptyState
            v-else
            description="该 Agent 暂未挂载 Skill"
            compact
          />
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
          <el-button
            v-if="isAdmin"
            type="warning"
            :loading="triggering"
            @click="triggerSelfImprove(selectedAgent)"
          >
            触发自进化
          </el-button>
          <el-button type="primary" @click="invokeViaChat(selectedAgent)">
            通过 Agent 助手调用
          </el-button>
        </div>
      </div>
    </el-drawer>

    <AgentDiscussionPanel
      v-if="discussVisible && discussSessionId"
      :session-id="discussSessionId"
      :ws-url="discussWsUrl"
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

import AgentAvatar from '@/components/agent/AgentAvatar.vue'
import AgentDeskCard from '@/components/agent/AgentDeskCard.vue'
import SituationPanel from '@/components/agent/SituationPanel.vue'
import EmptyState from '@/components/common/EmptyState.vue'
import PrismLoading from '@/components/common/PrismLoading.vue'
import AgentDiscussionPanel from '@/components/agent/AgentDiscussionPanel.vue'
import MetaGPTOrchestrationPanel from '@/components/agent/MetaGPTOrchestrationPanel.vue'
import { subscribeAgentEvents } from '@/utils/agentEventStream'
import type { AgentEvent, AgentEventType } from '@/types/agentEvent'
import {
  listRuntimeAgents,
  getRuntimeSummary,
  getSituation,
  listTypeMappings,
  listAgentSkills,
} from '@/api/agent'
import { triggerEvolution } from '@/api/evolution'
import { useUserStore } from '@/stores/user'
import { ElMessage } from 'element-plus/es/components/message/index'
import type {
  AgentRuntimeOut,
  AgentRuntimeSummaryOut,
  AgentSituationOut,
  AgentStatus,
  ReviewTypeMappingOut,
  SkillMetaOut,
  SkillType,
} from '@/types/agent'

const router = useRouter()
const route = useRoute()
const userStore = useUserStore()

const loading = ref(false)
const runtime = ref<AgentRuntimeOut[]>([])
const summary = ref<AgentRuntimeSummaryOut>({ total: 0, by_category: [] })
const situation = ref<AgentSituationOut | null>(null)
const typeMappings = ref<ReviewTypeMappingOut[]>([])
const filterCategory = ref<string>('')

// === v2.4 F2: Agent 中心 Tab 切换(办公室 / MetaGPT 编排) ===
const activeTab = ref<'office' | 'metagpt'>('office')

const drawerVisible = ref(false)
const selectedAgent = ref<AgentRuntimeOut | null>(null)

// === v3.0 AgentSkill 升级:per-Agent Skill 元数据 ===
const agentSkills = ref<SkillMetaOut[]>([])
const skillsLoading = ref(false)
const triggering = ref(false)
const isAdmin = computed(() => userStore.profile?.role === 'admin')

/**
 * Skill 类型中文标签
 * @param t - Skill 类型(self_improvement / proactive)
 * @returns 中文标签
 */
function skillTypeLabel(t: SkillType): string {
  return t === 'self_improvement' ? '自我进化' : '主动监测'
}

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

/**
 * 加载指定 Agent 的 Skill 元数据
 * 在抽屉打开时调用,从 SkillRegistry 拉取 per-Agent 专属 Skill 列表。
 * @param agentCode - Agent code
 */
async function loadAgentSkills(agentCode: string): Promise<void> {
  skillsLoading.value = true
  agentSkills.value = []
  try {
    agentSkills.value = await listAgentSkills(agentCode)
  } catch {
    // 抽屉内已有 EmptyState 兜底展示;网络/权限错误由 http 拦截器提示,不再重复弹
  } finally {
    skillsLoading.value = false
  }
}

/**
 * 手动触发指定 Agent 的自进化 Skill(admin only)
 * 调用 POST /api/evolution/trigger,通过 Orchestrator.invoke_skill
 * 调用 {agent_name}.self_improve Skill(trigger_type=manual)。
 * @param agent - 选中的 Agent
 */
async function triggerSelfImprove(agent: AgentRuntimeOut): Promise<void> {
  triggering.value = true
  try {
    const res = await triggerEvolution(agent.code, 90)
    const effect = (res as Record<string, string>).effect || 'unknown'
    const duration = (res as Record<string, number>).duration_ms
    if (effect === 'success') {
      ElMessage.success(`${agent.name} 自进化完成(${duration ?? 0}ms)`)
    } else if (effect === 'no_change') {
      ElMessage.info(`${agent.name} 自进化完成,本轮无新提案(样本不足或已收敛)`)
    } else {
      ElMessage.warning(`${agent.name} 自进化效果:${effect}`)
    }
    // 重新加载 Skill 列表以刷新展示
    await loadAgentSkills(agent.code)
  } catch {
    ElMessage.error('触发自进化失败')
  } finally {
    triggering.value = false
  }
}

// 选中 Agent 变化时自动加载 Skill 元数据
watch(selectedAgent, (agent) => {
  if (agent) {
    loadAgentSkills(agent.code)
  } else {
    agentSkills.value = []
  }
})

function invokeViaChat(agent: AgentRuntimeOut): void {
  drawerVisible.value = false
  window.dispatchEvent(new CustomEvent('prism:open-agent-chat', {
    detail: { prefill: `@${agent.code} ` },
  }))
}

function goRules(): void {
  router.push('/rules')
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
  // 安全告警为系统级事件,不反映某个 agent 工位状态,由 handleAgentEvent 显式跳过
  admin_alert: 'idle',
}

const ERROR_TIMEOUT_MS = 6_000
const STATS_REFRESH_DELAY_MS = 800
const HEARTBEAT_REFRESH_MS = 60_000
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
  // 安全告警为系统级事件,不更新任何 agent 工位状态
  if (ev.type === 'admin_alert') return
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
    // SSE 事件流为主、此处为轻量轮询兜底,失败不打断用户;错误由 http 拦截器提示
  }
}

// === 60s 心跳定期全量刷新 (兜底) ===
// SSE 事件流已驱动统计增量刷新,此轮询仅作断流兜底,60s 足够,无需更密

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
const discussWsUrl = ref('')
const discussAgents = ref<Array<{ code: string; name: string }>>([])
const discussFileName = ref('')

function closeDiscussPanel() {
  discussVisible.value = false
  discussSessionId.value = ''
  discussWsUrl.value = ''
  discussFileName.value = ''
}

function onStartDiscussion(
  sessionId: string,
  agents: Array<{ code: string; name: string }>,
  fileName = '',
  wsUrl = '',
) {
  discussSessionId.value = sessionId
  discussWsUrl.value = wsUrl
  discussAgents.value = agents
  discussFileName.value = fileName
  discussVisible.value = true
}

defineExpose({ onStartDiscussion })

function initDiscussionFromRoute() {
  const session = route.query.discuss_session as string
  const wsUrl = route.query.discuss_ws as string
  const agentsJson = route.query.discuss_agents as string
  if (session && agentsJson) {
    try {
      const agents = JSON.parse(agentsJson)
      discussSessionId.value = session
      discussWsUrl.value = wsUrl || ''
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

/* v2.4 F2: Agent 中心 Tab 切换 */
.agent-tabs {
  :deep(.el-tabs__header) {
    margin-bottom: 16px;
  }

  :deep(.el-tabs__content) {
    overflow: visible;
  }
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

.detail-label-hint {
  margin-left: 6px;
  font-size: 10.5px;
  color: var(--gray-400);
  font-weight: 400;
  letter-spacing: 0;
}

/* v3.0 AgentSkill 升级:per-Agent Skill 卡片样式 */
.skill-cards {
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.skill-card {
  background: var(--surface-1);
  border: 1px solid var(--gray-150, #EEF0F4);
  border-radius: 10px;
  padding: 12px 14px;
  display: flex;
  flex-direction: column;
  gap: 6px;
  transition: border-color 0.15s ease;

  &:hover {
    border-color: var(--brand-300);
  }

  &.skill-self_improvement {
    border-left: 3px solid var(--brand-500, #5B58E8);
  }

  &.skill-proactive {
    border-left: 3px solid #2BBFB9;
  }
}

.skill-card-head {
  display: flex;
  align-items: center;
  gap: 8px;
}

.skill-type-badge {
  display: inline-block;
  padding: 2px 8px;
  border-radius: 4px;
  font-size: 10.5px;
  font-weight: 600;
  letter-spacing: 0.02em;

  &.badge-self_improvement {
    background: rgba(91, 88, 232, 0.10);
    color: var(--brand-600, #5B58E8);
  }

  &.badge-proactive {
    background: rgba(43, 191, 185, 0.12);
    color: #1A8F8A;
  }
}

.skill-name {
  font-size: 11.5px;
  color: var(--gray-600);
  font-family: var(--font-mono, monospace);
}

.skill-desc {
  margin: 0;
  font-size: 12.5px;
  color: var(--gray-700);
  line-height: 1.55;
}

.skill-card-foot {
  display: flex;
  gap: 6px;
  align-items: center;
}

.detail-footer {
  border-top: 1px solid var(--gray-100);
  padding-top: 16px;
  display: flex;
  justify-content: flex-end;
}
</style>
