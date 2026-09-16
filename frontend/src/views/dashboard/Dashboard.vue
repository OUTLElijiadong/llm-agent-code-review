<template>
  <div class="dashboard-page prism-page-shell">
    <!-- ============ 页头 ============ -->
    <header class="page-head prism-page-head prism-rise">
      <div>
        <h1 class="page-title font-display">仪表盘</h1>
        <p class="page-sub">
          {{ today }}
          <template v-if="summaryState === 'success' && summary"> · 最近
            <b class="hl">{{ summary.recent_tasks.length }}</b> 个已完成审查
          </template>
        </p>
      </div>
      <div class="page-actions">
        <el-select v-model="timeRange" size="small" style="width: 110px" @change="loadCharts">
          <el-option label="近 7 天" :value="7" />
          <el-option label="近 30 天" :value="30" />
          <el-option label="近 90 天" :value="90" />
          <el-option label="累计" :value="0" />
        </el-select>
        <el-button :loading="loading" @click="loadDashboard">刷新数据</el-button>
        <el-button v-if="canExportWeeklyReport" data-testid="export-dashboard" :disabled="!canExportCurrentData" @click="onWeeklyReport">导出统计报告</el-button>
        <el-button v-if="canStartReview" type="primary" @click="onNewReview">+ 新建审查</el-button>
      </div>
    </header>

    <p class="load-feedback" role="status" aria-live="polite" data-testid="dashboard-progress">
      {{ loading ? '正在读取仪表盘' : '仪表盘读取结束' }} · 已返回 {{ completedReads }} / 5 项
      <span v-if="failedReads"> · {{ failedReads }} 项读取失败，请在对应分区重试</span>
    </p>
    <section data-section="summary" :data-state="summaryState" :aria-busy="summaryState === 'loading'">
      <PrismLoading v-if="summaryState === 'loading'" label="正在读取摘要" sublabel="正在查询当前账号可见的项目与已完成审查" />
      <p v-else-if="summaryState === 'error'" class="load-feedback error" role="alert">
        摘要读取失败，无法确认统计数值。
        <button class="link" type="button" @click="loadSummary">重试摘要</button>
      </p>

    <!-- ============ 6 张统计卡 ============ -->
    <section v-if="summaryState === 'success'" class="stat-grid prism-stagger">
      <div v-for="card in statCards" :key="card.label" class="stat" :class="{ feature: card.feature }">
        <div v-if="card.feature" class="feature-spectrum"></div>
        <div class="stat-label">
          <span class="stat-ico" :style="card.iconStyle">
            <el-icon><component :is="card.icon" /></el-icon>
          </span>
          {{ card.label }}
        </div>
        <div class="stat-num prism-number font-display">
          {{ card.value }}<span class="stat-unit">{{ card.unit }}</span>
        </div>
        <div v-if="card.delta" class="stat-delta" :class="card.deltaDir">{{ card.delta }}</div>
        <div v-if="card.feature && hasAverageScore" class="stat-gauge">
          <div class="gauge-track">
            <FluidProgress class="gauge-fluid" :progress="avgScoreAnim" :height="6" />
          </div>
          <div class="gauge-label">
            <span>风险等级 · {{ riskLevel }}</span>
            <span>有效代码审查均分</span>
          </div>
        </div>
      </div>
    </section>
    </section>

    <!-- ============ 后台进行中(隐藏设计:无任务时整块不渲染) ============ -->
    <section
      v-if="runningState !== 'success' || hasRunningTasks"
      class="running-panel prism-rise"
      data-testid="running-panel"
    >
      <header class="running-head">
        <h3 class="font-display">
          <span class="running-pulse" aria-hidden="true"></span>
          后台进行中
          <span class="running-count font-mono">{{ (runningData?.reviews.length ?? 0) + (runningData?.agents.length ?? 0) }}</span>
        </h3>
        <p class="running-sub">进行中每 5 秒更新</p>
      </header>
      <p v-if="runningState === 'loading' && !runningData" class="load-feedback" role="status">正在读取后台进度</p>
      <p v-if="runningState === 'error'" class="load-feedback error" role="alert">
        后台进度读取失败。<span v-if="runningData">以下为上次读取结果，尚未确认最新状态。</span>
        <button class="link" type="button" @click="loadRunning">重试后台进度</button>
      </p>
      <div v-if="runningData" class="running-list">
        <button
          v-for="item in runningData.reviews"
          :key="`r-${item.id}`"
          type="button"
          class="running-item review"
          :disabled="!canViewReviews"
          @click="goReviewDetail(item.id)"
        >
          <span class="ri-main">
            <b>{{ item.task_name }}</b>
            <span class="ri-meta font-mono">{{ item.project_name }} · {{ item.status === 'pending' ? '排队中' : `${item.processed_files}/${item.total_files || '?'} 文件` }}</span>
          </span>
          <span class="ri-bar" :class="{ indeterminate: item.status === 'pending' || !item.total_files }"
            role="progressbar" :aria-label="`${item.task_name}已处理文件`"
            :aria-valuenow="item.status === 'pending' || !item.total_files ? undefined : reviewProgress(item)"
            :aria-valuemin="0" :aria-valuemax="100"
          >
            <span class="ri-fill" :style="{ width: `${reviewProgress(item)}%` }"></span>
          </span>
          <span class="ri-pct font-mono">{{ item.status === 'pending' ? '…' : item.total_files ? `${reviewProgress(item)}%` : '运行中' }}</span>
        </button>
        <div
          v-for="item in runningData.agents"
          :key="`a-${item.run_id}`"
          class="running-item agent"
          :title="item.session_key"
        >
          <span class="agent-dot" aria-hidden="true"></span>
          <span class="ri-main">
            <b>{{ item.surface === 'admin' ? '贾维斯' : '小菱' }}会话</b>
            <span class="ri-meta font-mono">{{ AGENT_RUN_STATUS_LABELS[item.status] || item.status }}</span>
          </span>
          <span class="ri-tag">Agent</span>
        </div>
      </div>
    </section>

    <!-- ============ v2.1.1 安全态势卡 ============ -->
    <section v-if="canViewSecurity" class="security-row prism-rise" style="--rise-delay: 180ms">
      <SecurityPostureCard :days="securityDays" />
    </section>

    <!-- ============ 8 维度极坐标 + Agent 活动流 ============ -->
    <section class="chart-row two-col prism-stagger">
      <article class="chart-card" data-section="dimension" :data-state="chartStates.dimension" :aria-busy="chartStates.dimension === 'loading'">
        <header class="chart-head">
          <div>
            <h3 class="font-display">问题类型分布 · 棱镜光谱</h3>
            <p class="chart-desc">{{ rangeLabel }}的问题分布<span v-if="chartStates.dimension === 'success'"> · {{ totalDimCount }} 个</span></p>
          </div>
        </header>
        <p v-if="chartStates.dimension === 'loading'" role="status">正在读取维度数据</p>
        <p v-else-if="chartStates.dimension === 'error'" class="load-feedback error" role="alert">维度数据读取失败。<button class="link" type="button" @click="loadIssueTypeStatistics">重试维度数据</button></p>
        <BaseChart v-else-if="dimChartReady" :option="dimPolarOption" height="320px" />
        <EmptyState v-else description="暂无维度数据" compact />
        <div v-if="chartStates.dimension === 'success' && dimChartReady" class="legend-list">
          <div v-for="d in dimSummary" :key="d.key" class="legend-item">
            <span class="dot" :style="{ background: d.color }"></span>
            <span class="name">{{ d.name }}</span>
            <span class="val font-mono">{{ d.value.toLocaleString() }}</span>
          </div>
        </div>
      </article>

      <article class="chart-card activity">
        <header class="chart-head">
          <div>
            <h3 class="font-display">最近已完成审查</h3>
            <p class="chart-desc">最近记录 · 随摘要刷新</p>
          </div>
          <button v-if="canViewReviews" class="link" type="button" @click="goReviewList">全部 →</button>
        </header>
        <p v-if="summaryState === 'loading'" role="status">正在读取最近审查</p>
        <p v-else-if="summaryState === 'error'">最近审查读取失败，请重试摘要。</p>
        <div v-else class="activity-feed">
          <div
            v-for="item in activityFeed"
            :key="item.id"
            class="activity-item"
            :class="{ live: item.live }"
          >
            <div class="ico" :style="{ background: item.color }">
              <el-icon><component :is="item.icon" /></el-icon>
            </div>
            <div class="body">
              <div class="title" v-html="item.title"></div>
              <div class="meta font-mono">{{ item.meta }}</div>
            </div>
            <div class="when font-mono">{{ item.when }}</div>
          </div>
          <EmptyState v-if="!activityFeed.length" description="暂无最近活动" compact />
        </div>
      </article>
    </section>

    <!-- ============ 3 个分析图(渐进披露:默认折叠,可展开并记住偏好) ============ -->
    <section class="analysis-fold prism-rise">
      <button type="button" class="analysis-toggle" :aria-expanded="analysisOpen" @click="analysisOpen = !analysisOpen">
        <span class="at-chevron" :class="{ open: analysisOpen }" aria-hidden="true">▸</span>
        <b class="font-display">深度分析</b>
        <span class="at-sub">趋势 · 严重度 · 评分明细</span>
      </button>
      <section v-show="analysisOpen" class="chart-row three-col prism-stagger">
      <article class="chart-card" data-section="frequency" :data-state="chartStates.frequency" :aria-busy="chartStates.frequency === 'loading'">
        <header class="chart-head">
          <h3 class="font-display">{{ rangeLabel }}审查任务趋势</h3>
        </header>
        <p v-if="chartStates.frequency === 'loading'" role="status">正在读取趋势数据</p>
        <p v-else-if="chartStates.frequency === 'error'" class="load-feedback error" role="alert">趋势数据读取失败。<button class="link" type="button" @click="loadReviewFrequency">重试趋势数据</button></p>
        <BaseChart v-else-if="frequencyData.length" :option="trendOption" height="220px" />
        <EmptyState v-else description="暂无趋势数据" compact />
      </article>

      <article class="chart-card" data-section="risk" :data-state="chartStates.risk" :aria-busy="chartStates.risk === 'loading'">
        <header class="chart-head">
          <div>
            <h3 class="font-display">严重度分布</h3>
            <p class="chart-desc">{{ rangeLabel }}的问题分布<span v-if="chartStates.risk === 'success' && riskTotal > 0"> · {{ riskTotal }} 个</span></p>
          </div>
        </header>
        <p v-if="chartStates.risk === 'loading'" role="status">正在读取严重度数据</p>
        <p v-else-if="chartStates.risk === 'error'" class="load-feedback error" role="alert">严重度数据读取失败。<button class="link" type="button" @click="loadRiskDistribution">重试严重度数据</button></p>
        <template v-else-if="riskData.some((item) => item.value > 0)">
          <BaseChart :option="severityOption" height="220px" />
          <ul class="severity-values" aria-label="严重度数量">
            <li v-for="item in riskData" :key="item.severity">
              <span><i :style="{ background: severityColor(item.severity) }" aria-hidden="true" />{{ item.name }}</span>
              <b class="font-mono">{{ item.value }}</b>
            </li>
          </ul>
        </template>
        <template v-else>
          <EmptyState :description="`${rangeLabel}暂无严重度数据`" compact />
          <p v-if="timeRange !== 0 && cumulativeIssueCount > 0" class="empty-hint" data-testid="risk-cumulative-hint">
            统计卡为累计口径，本窗口没有新问题；累计共
            <b class="hl font-mono">{{ cumulativeIssueCount }}</b> 个 ·
            <button class="link" type="button" @click="switchToCumulative">切换累计查看</button>
          </p>
        </template>
      </article>

      <article class="chart-card" data-section="score" :data-state="chartStates.score" :aria-busy="chartStates.score === 'loading'">
        <header class="chart-head">
          <h3 class="font-display">最近完成评分</h3>
        </header>
        <p v-if="chartStates.score === 'loading'" role="status">正在读取评分数据</p>
        <p v-else-if="chartStates.score === 'error'" class="load-feedback error" role="alert">评分数据读取失败。<button class="link" type="button" @click="loadScoreTrend">重试评分数据</button></p>
        <div v-else-if="scoreTrendData.length" class="score-bars">
          <div v-for="s in scoreTrendData" :key="s.name" class="score-bar">
            <div class="row">
              <span class="bar-name">{{ s.name }}</span>
              <span class="bar-val font-mono" :style="{ color: scoreColor(s.value) }">{{ s.value }}</span>
            </div>
            <div class="bar-track">
              <div
                class="bar-fill"
                :style="{
                  width: `${s.value}%`,
                  background: `linear-gradient(90deg, var(--brand-400), ${scoreColor(s.value)})`,
                }"
              ></div>
            </div>
          </div>
        </div>
        <EmptyState v-else description="暂无评分数据" compact />
      </article>
    </section>
    </section>
  </div>
</template>

<script setup lang="ts">
import { ref, reactive, onMounted, onBeforeUnmount, computed, watch } from 'vue'
import { useRouter } from 'vue-router'
import dayjs from 'dayjs'

import type { EChartsCoreOption as EChartsOption } from 'echarts/core'
import BaseChart from '@/components/chart/BaseChart.vue'
import EmptyState from '@/components/common/EmptyState.vue'
import PrismLoading from '@/components/common/PrismLoading.vue'
import FluidProgress from '@/components/common/FluidProgress.vue'
import { useCountUp } from '@/composables/useCountUp'
import { reviewRiskLevel, reviewScoreColor } from '@/utils/reviewScore'
import SecurityPostureCard from '@/components/security/SecurityPostureCard.vue'
import { PRISM_SEVERITY_COLORS } from '@/components/chart/prismTheme'
import { severityClass, severityDisplayLabel } from '@/constants/severity'
import { DIM_META, normalizeDimKey } from '@/constants/dim'
import {
  getSummary,
  getRiskDistribution,
  getIssueTypeStatistics,
  getScoreTrend,
  getReviewFrequency,
  getRunning,
} from '@/api/dashboard'
import type { RunningOut, RunningReviewItem, RiskItem, IssueTypeItem, ScoreTrendItem, FrequencyItem, SummaryOut, RecentTaskOut } from '@/types/dashboard'
import { ElMessage } from 'element-plus/es/components/message/index'
import { useUserStore } from '@/stores/user'

const router = useRouter()
const userStore = useUserStore()
const canViewReviews = computed(() => userStore.hasPermission('review:view'))
const canStartReview = computed(() => userStore.hasPermission('review:start'))
const canViewSecurity = computed(() => userStore.hasPermission('security:view'))
const canExportWeeklyReport = computed(() => userStore.hasPermission('report:export:html'))
const timeRange = ref(30)
type LoadState = 'loading' | 'success' | 'error'
type ChartKey = 'risk' | 'dimension' | 'score' | 'frequency'
const summary = ref<SummaryOut | null>(null)
const summaryState = ref<LoadState>('loading')
const chartStates = reactive<Record<ChartKey, LoadState>>({ risk: 'loading', dimension: 'loading', score: 'loading', frequency: 'loading' })
const chartVersions: Record<ChartKey, number> = { risk: 0, dimension: 0, score: 0, frequency: 0 }
let summaryVersion = 0
let disposed = false
const readStates = computed(() => [summaryState.value, ...Object.values(chartStates)])
const loading = computed(() => readStates.value.includes('loading'))
const completedReads = computed(() => readStates.value.filter((state) => state !== 'loading').length)
const failedReads = computed(() => readStates.value.filter((state) => state === 'error').length)
const canExportCurrentData = computed(() => readStates.value.every((state) => state === 'success'))
const hasAverageScore = computed(() => Boolean(summary.value && (summary.value.code_review_count ?? 0) > 0 && Number.isFinite(summary.value.avg_score)))

/* 数字滚动:统计卡数值从旧值平滑滚动到新值 */
const reviewCountSrc = computed(() => summary.value?.review_count ?? 0)
const totalIssuesSrc = computed(() => summary.value?.total_issues ?? 0)
const severeIssuesSrc = computed(() => summary.value?.severe_issues ?? 0)
const avgScoreSrc = computed(() => summary.value?.avg_score ?? 0)
const projectCountSrc = computed(() => summary.value?.project_count ?? 0)
const fileCountSrc = computed(() => summary.value?.file_count ?? 0)
const reviewCountAnim = useCountUp(reviewCountSrc)
const totalIssuesAnim = useCountUp(totalIssuesSrc)
const severeIssuesAnim = useCountUp(severeIssuesSrc)
const avgScoreAnim = useCountUp(avgScoreSrc)
const projectCountAnim = useCountUp(projectCountSrc)
const fileCountAnim = useCountUp(fileCountSrc)

const riskData = ref<{ name: string; value: number; severity: string }[]>([])

/* 后台进度独立读取；单次请求结束后再调度，隐藏页面不轮询。 */
const runningData = ref<RunningOut | null>(null)
const runningState = ref<LoadState>('loading')
const hasRunningTasks = computed(() => Boolean(runningData.value && (runningData.value.reviews.length || runningData.value.agents.length)))
const AGENT_RUN_STATUS_LABELS: Record<string, string> = {
  running: '运行中', approving: '审批处理中', rejecting: '驳回处理中',
  answering: '回答处理中', waiting_approval: '等待审批', waiting_input: '等待输入',
}
let runningTimer: ReturnType<typeof setTimeout> | undefined
let runningRequest: Promise<void> | null = null

function stopRunningPolling(): void {
  if (runningTimer) clearTimeout(runningTimer)
  runningTimer = undefined
}

function validateRunning(data: RunningOut): void {
  if (!data || typeof data !== 'object') throw new Error('Invalid running data')
  assertRows(data.reviews, (item) => isCount(item.id) && item.id > 0 && isCount(item.project_id) && item.project_id > 0
    && isText(item.task_name) && isText(item.project_name) && isText(item.review_type)
    && ['pending', 'running'].includes(String(item.status)) && isCount(item.processed_files) && isCount(item.total_files))
  assertRows(data.agents, (item) => isText(item.run_id) && isText(item.session_key)
    && ['user', 'admin'].includes(String(item.surface)) && Object.prototype.hasOwnProperty.call(AGENT_RUN_STATUS_LABELS, String(item.status)))
}

function loadRunning(): Promise<void> {
  if (disposed) return Promise.resolve()
  if (runningRequest) return runningRequest
  stopRunningPolling()
  runningState.value = 'loading'
  runningRequest = (async () => {
    // 先让出一次微任务，确保同步抛错也发生在 request 引用赋值之后。
    await Promise.resolve()
    if (disposed) return
    try {
      const data = await getRunning()
      if (disposed) return
      validateRunning(data)
      runningData.value = data
      runningState.value = 'success'
    } catch {
      if (!disposed) runningState.value = 'error'
    } finally {
      runningRequest = null
      if (!disposed && !document.hidden) {
        const interval = runningState.value === 'error' ? 15000 : hasRunningTasks.value ? 5000 : 30000
        runningTimer = setTimeout(() => { void loadRunning() }, interval)
      }
    }
  })()
  return runningRequest
}

function reviewProgress(item: RunningReviewItem): number {
  if (item.status === 'pending' || !item.total_files) return 0
  return Math.max(0, Math.min(100, Math.round((item.processed_files / item.total_files) * 100)))
}

function onRunningVisibility(): void {
  if (document.hidden) stopRunningPolling()
  else void loadRunning()
}

/* 分析区折叠(不重要内容渐进披露),偏好持久化 */
function readAnalysisOpen(): boolean {
  try { return localStorage.getItem('prism:dashboard-analysis-open') !== '0' } catch { return true }
}
const analysisOpen = ref(readAnalysisOpen())
watch(analysisOpen, (open) => {
  try { localStorage.setItem('prism:dashboard-analysis-open', open ? '1' : '0') } catch { /* ignore */ }
})
const issueTypeData = ref<{ key: string; name: string; value: number }[]>([])
const scoreTrendData = ref<{ name: string; value: number }[]>([])
const frequencyData = ref<{ name: string; value: number }[]>([])

/* 时间窗口:0 表示累计;图表副标题统一口径,防"统计卡累计 vs 图表窗口"认知错位 */
const rangeLabel = computed(() => (timeRange.value === 0 ? '累计' : `近 ${timeRange.value} 天`))
const securityDays = computed(() => (timeRange.value === 0 ? 365 : timeRange.value))
const riskTotal = computed(() => riskData.value.reduce((total, item) => total + item.value, 0))
const cumulativeIssueCount = computed(() => summary.value?.total_issues ?? 0)

function switchToCumulative() {
  if (timeRange.value === 0) return
  timeRange.value = 0
  loadCharts()
}

const today = computed(() => {
  const d = dayjs()
  const weekDays = ['周日', '周一', '周二', '周三', '周四', '周五', '周六']
  return `${d.format('YYYY 年 M 月 D 日')} · ${weekDays[d.day()]}`
})

const dimMeta = DIM_META
const otherDimMeta = { key: '__other__', name: '未归类', color: '#9BA3B0' }
const displayDimMeta = computed(() => issueTypeData.value.some((item) => item.key === '__other__' && item.value > 0)
  ? [...dimMeta, otherDimMeta]
  : dimMeta)

const statCards = computed(() => {
  const hasReview = reviewCountSrc.value > 0
  const hasIssue = totalIssuesSrc.value > 0
  const hasSevere = severeIssuesSrc.value > 0
  const hasProject = projectCountSrc.value > 0
  const hasFile = fileCountSrc.value > 0
  return [
    {
      label: '累计成功任务', value: Math.round(reviewCountAnim.value), unit: '次', icon: 'DocumentChecked',
      iconStyle: { background: 'var(--brand-50)', color: 'var(--brand-600)' },
      delta: hasReview ? '含代码审查与测试' : '— 暂无数据', deltaDir: 'flat',
      feature: false,
    },
    {
      label: '累计发现问题', value: Math.round(totalIssuesAnim.value), unit: '个', icon: 'Warning',
      iconStyle: { background: 'rgba(226,92,115,.10)', color: 'var(--dim-bug)' },
      delta: hasIssue ? `共 ${Math.round(severeIssuesAnim.value)} 个严重` : '— 暂无',
      deltaDir: 'flat',
      feature: false,
    },
    {
      label: '严重问题', value: Math.round(severeIssuesAnim.value), unit: '个', icon: 'CircleClose',
      iconStyle: { background: 'rgba(220,73,97,.10)', color: 'var(--sev-severe)' },
      delta: hasSevere ? '需优先处理' : '— 暂无',
      deltaDir: hasSevere ? 'down' : 'flat',
      feature: false,
    },
    {
      label: '平均代码评分', value: hasAverageScore.value ? avgScoreAnim.value.toFixed(1) : '—', unit: '/100', icon: 'TrendCharts',
      iconStyle: { background: 'rgba(255,255,255,.16)', color: '#fff' },
      delta: hasAverageScore.value ? `${summary.value?.code_review_count} 份有效代码审查 · 不含测试` : '— 暂无代码评分样本',
      deltaDir: 'flat',
      feature: true,
    },
    {
      label: '可见项目', value: Math.round(projectCountAnim.value), unit: '个', icon: 'FolderOpened',
      iconStyle: { background: 'rgba(75,155,255,.10)', color: 'var(--dim-naming)' },
      delta: hasProject ? '持续更新中' : '— 暂无', deltaDir: 'flat',
      feature: false,
    },
    {
      label: '代码库文件', value: Math.round(fileCountAnim.value), unit: '份', icon: 'Document',
      iconStyle: { background: 'rgba(61,188,217,.12)', color: 'var(--accent-600)' },
      delta: (summary.value?.archive_file_count ?? 0) > 0 ? `另有 ${summary.value?.archive_file_count} 个整包归档文件` : hasFile ? '当前有效的入库文件' : '— 暂无入库文件', deltaDir: 'flat',
      feature: false,
    },
  ]
})

const riskLevel = computed(() => hasAverageScore.value ? reviewRiskLevel(summary.value?.avg_score) : '未知')

const totalDimCount = computed(() => issueTypeData.value.reduce((s, x) => s + x.value, 0))

const dimSummary = computed(() => {
  return displayDimMeta.value
    .map((d) => {
      const found = issueTypeData.value.find((x) => x.key === d.key)
      return { ...d, value: found?.value ?? 0 }
    })
    .sort((a, b) => b.value - a.value)
})

const dimChartReady = computed(() => totalDimCount.value > 0)

const dimPolarOption = computed<EChartsOption>(() => ({
  polar: { radius: ['18%', '78%'] },
  angleAxis: {
    type: 'category',
    data: displayDimMeta.value.map((d) => d.name),
    axisLine: { show: false },
    axisTick: { show: false },
    axisLabel: { color: '#4F5667', fontSize: 11, fontFamily: '"Noto Sans SC", sans-serif' },
  },
  radiusAxis: {
    axisLine: { show: false },
    axisTick: { show: false },
    axisLabel: { show: false },
    splitLine: { lineStyle: { color: '#EEF0F4' } },
  },
  tooltip: { trigger: 'item', formatter: '{b}<br/><b>{c}</b> 个问题' },
  series: [{
    type: 'bar',
    coordinateSystem: 'polar',
    data: displayDimMeta.value.map((d) => {
      const found = issueTypeData.value.find((x) => x.key === d.key)
      return { value: found?.value ?? 0, itemStyle: { color: d.color, borderRadius: 4 } }
    }),
  }],
}))

const trendOption = computed<EChartsOption>(() => ({
  grid: { left: 36, right: 18, top: 18, bottom: 28 },
  xAxis: {
    type: 'category',
    data: frequencyData.value.map((x) => x.name),
    boundaryGap: false,
  },
  yAxis: { type: 'value' },
  tooltip: { trigger: 'axis' },
  series: [{
    type: 'line',
    data: frequencyData.value.map((x) => x.value),
    areaStyle: {
      color: {
        type: 'linear',
        x: 0, y: 0, x2: 0, y2: 1,
        colorStops: [
          { offset: 0, color: 'rgba(91,88,232,.32)' },
          { offset: 1, color: 'rgba(91,88,232,0)' },
        ],
      },
    },
    itemStyle: { color: '#5B58E8' },
    lineStyle: { color: '#5B58E8', width: 2 },
    showSymbol: false,
  }],
}))

function severityColor(sev: string): string {
  const key = severityClass(sev) as keyof typeof PRISM_SEVERITY_COLORS
  return PRISM_SEVERITY_COLORS[key] ?? '#9BA3B0'
}

const severityOption = computed<EChartsOption>(() => ({
  grid: { left: 8, right: 8, top: 16, bottom: 36, containLabel: true },
  tooltip: { trigger: 'item', formatter: '{b}<br/><b>{c}</b> · {d}%' },
  legend: { bottom: 0, icon: 'circle', textStyle: { fontSize: 11 } },
  series: [{
    type: 'pie',
    stillShowZeroSum: false,
    radius: ['52%', '76%'],
    avoidLabelOverlap: true,
    itemStyle: { borderRadius: 6, borderColor: '#fff', borderWidth: 2 },
    label: { show: false },
    emphasis: { label: { show: true, fontSize: 13, fontWeight: 'bold' } },
    data: riskData.value.map((x) => ({
      name: x.name,
      value: x.value,
      itemStyle: { color: severityColor(x.severity) },
    })),
  }],
}))

/**
 * 根据评分返回趋势条颜色
 * @param score - 代码质量评分
 * @returns 十六进制颜色值
 */
function scoreColor(score: number): string {
  return reviewScoreColor(score)
}

/**
 * 转义活动流中的动态文本，避免任务名通过 v-html 注入标记
 * @param value - 需要插入 HTML 片段的动态文本
 * @returns 转义后的安全文本
 */
function escapeHtml(value: string): string {
  return value
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;')
}

interface ActivityItem {
  id: number
  icon: string
  color: string
  title: string
  meta: string
  when: string
  live?: boolean
}

function taskScoreLabel(task: RecentTaskOut): string {
  if (['sandbox_test', 'pentest'].includes(task.review_type || '')) return '测试评分'
  if (['quick', 'standard', 'security', 'performance', 'full'].includes(task.review_type || '')) return '代码评分'
  return '历史评分（类型未确认）'
}

const activityFeed = computed<ActivityItem[]>(() => {
  const tasks = (summary.value?.recent_tasks ?? []) as RecentTaskOut[]
  return tasks.slice(0, 6).map((t, i) => {
    const id = t.id ?? i
    const score = t.score ?? '未提供'
    const scoreLabel = taskScoreLabel(t)
    const status = t.status || 'pending'
    const taskName = t.task_name || `任务 #${id}`
    const projectName = t.project_name || ''
    const displayName = projectName ? `${projectName} · ${taskName}` : taskName
    const safeDisplayName = escapeHtml(displayName)
    const created = t.create_time ? dayjs(t.create_time).fromNow?.() ?? dayjs(t.create_time).format('M/D HH:mm') : ''
    const live = status === 'running' && i === 0
    const ok = status === 'success'
    return {
      id,
      icon: live ? 'Cpu' : ok ? 'Select' : 'Warning',
      color: live ? 'var(--brand-500)' : ok ? 'var(--status-fixed)' : 'var(--dim-bug)',
      title: live
        ? `正在审查 <b>${safeDisplayName}</b>`
        : ok
          ? `完成 <b>${safeDisplayName}</b>，${scoreLabel} <b style="color: var(--status-fixed);">${score}</b>`
          : `<b>${safeDisplayName}</b> 检出问题`,
      meta: `状态：${status}${ok ? ` · ${scoreLabel} ${score}` : ''}`,
      when: created || '时间未记录',
      live,
    }
  })
})

function isCount(value: unknown): value is number {
  return typeof value === 'number' && Number.isSafeInteger(value) && value >= 0
}

function isScore(value: unknown): value is number {
  return typeof value === 'number' && Number.isFinite(value) && value >= 0 && value <= 100
}

function isText(value: unknown): value is string {
  return typeof value === 'string' && value.trim().length > 0
}

function isCalendarDate(value: unknown): value is string {
  if (typeof value !== 'string' || !/^\d{4}-\d{2}-\d{2}$/.test(value) || Number(value.slice(0, 4)) < 1) return false
  const date = new Date(`${value}T00:00:00Z`)
  return Number.isFinite(date.getTime()) && date.toISOString().slice(0, 10) === value
}

function isTimestamp(value: unknown): value is string {
  return typeof value === 'string'
    && /^\d{4}-\d{2}-\d{2}[T ](?:[01]\d|2[0-3]):[0-5]\d:[0-5]\d(?:\.\d{1,6})?(?:Z|[+-](?:[01]\d|2[0-3]):[0-5]\d)?$/.test(value)
    && isCalendarDate(value.slice(0, 10)) && Number.isFinite(Date.parse(value))
}

function isRecentTask(value: unknown): value is RecentTaskOut {
  if (!value || typeof value !== 'object') return false
  const task = value as RecentTaskOut
  return isCount(task.id) && task.id > 0 && isCount(task.project_id) && task.project_id > 0
    && isText(task.task_name) && isText(task.project_name) && task.status === 'success'
    && (task.score === null || isScore(task.score))
    && (task.review_type == null || isText(task.review_type)) && (task.create_time === null || isTimestamp(task.create_time))
}

function assertRows(data: unknown, validate: (row: Record<string, unknown>) => boolean): void {
  if (!Array.isArray(data) || !data.every((row) => row && typeof row === 'object' && !Array.isArray(row) && validate(row))) {
    throw new Error('Invalid dashboard rows')
  }
}

async function loadSummary() {
  const version = ++summaryVersion
  summaryState.value = 'loading'
  try {
    const data = await getSummary()
    if (disposed || version !== summaryVersion) return
    const counts = data && [data.project_count, data.file_count, data.review_count, data.total_issues, data.severe_issues]
    if (!counts || !counts.every(isCount) || !isScore(data.avg_score)
      || (data.code_review_count !== undefined && (!isCount(data.code_review_count) || data.code_review_count > data.review_count))
      || (data.archive_file_count !== undefined && !isCount(data.archive_file_count))
      || !Array.isArray(data.recent_tasks) || !data.recent_tasks.every(isRecentTask)) throw new Error('Invalid dashboard summary')
    summary.value = data
    summaryState.value = 'success'
  } catch {
    if (!disposed && version === summaryVersion) summaryState.value = 'error'
  }
}

async function loadChart<Value>(key: ChartKey, request: () => Promise<Value>, accept: (value: Value) => void) {
  const version = ++chartVersions[key]
  chartStates[key] = 'loading'
  try {
    const data = await request()
    if (disposed || version !== chartVersions[key]) return
    accept(data)
    chartStates[key] = 'success'
  } catch {
    if (!disposed && version === chartVersions[key]) chartStates[key] = 'error'
  }
}

async function loadRiskDistribution() {
  await loadChart('risk', () => getRiskDistribution(timeRange.value), (data) => {
    assertRows(data, (row) => isText(row.severity) && isCount(row.count))
    if (!isCount(data.reduce((total, item) => total + item.count, 0))) throw new Error('Invalid risk total')
    riskData.value = data.map((item: RiskItem) => ({
      severity: item.severity,
      name: severityDisplayLabel(item.severity),
      value: item.count,
    }))
  })
}

async function loadIssueTypeStatistics() {
  await loadChart('dimension', () => getIssueTypeStatistics(timeRange.value), (data) => {
    assertRows(data, (row) => isText(row.issue_type) && isCount(row.count))
    if (!isCount(data.reduce((total, item) => total + item.count, 0))) throw new Error('Invalid dimension total')
    const aggregate: Record<string, { key: string; name: string; value: number }> = {}
    for (const item of data as IssueTypeItem[]) {
      const norm = normalizeDimKey(item.issue_type)
      const meta = dimMeta.find((dimension) => dimension.key === norm)
      const key = meta?.key ?? otherDimMeta.key
      const name = meta?.name ?? otherDimMeta.name
      if (!aggregate[key]) aggregate[key] = { key, name, value: 0 }
      aggregate[key].value += item.count
    }
    issueTypeData.value = Object.values(aggregate)
  })
}

async function loadScoreTrend() {
  await loadChart('score', () => getScoreTrend(6), (data) => {
    assertRows(data, (row) => isCount(row.task_id) && row.task_id > 0 && isScore(row.score) && isTimestamp(row.create_time))
    scoreTrendData.value = data.map((item: ScoreTrendItem) => ({
      name: `#${item.task_id}`,
      value: item.score,
    }))
  })
}

async function loadReviewFrequency() {
  await loadChart('frequency', () => getReviewFrequency(timeRange.value), (data) => {
    assertRows(data, (row) => isCalendarDate(row.date) && isCount(row.count))
    frequencyData.value = data.map((item: FrequencyItem) => ({
      name: timeRange.value === 0 ? dayjs(item.date).format('YYYY/M/D') : dayjs(item.date).format('M/D'),
      value: item.count,
    }))
  })
}

async function loadCharts() {
  await Promise.allSettled([
    loadRiskDistribution(),
    loadIssueTypeStatistics(),
    loadScoreTrend(),
    loadReviewFrequency(),
  ])
}

function onWeeklyReport() {
  if (!canExportWeeklyReport.value) return
  if (!canExportCurrentData.value || !summary.value) {
    ElMessage.warning('请等待数据读取完成，并重试失败分区后再导出')
    return
  }
  const s = summary.value
  const esc = (v: unknown) => String(v ?? '').replace(/[&<>]/g, (c) =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;' }[c] || c))
  const rows = (arr: { name: string; value: number }[]) =>
    arr.length
      ? arr.map((x) => `<tr><td>${esc(x.name)}</td><td style="text-align:right">${esc(x.value)}</td></tr>`).join('')
      : '<tr><td colspan="2" style="color:#999">暂无数据</td></tr>'
  const taskRows = (s.recent_tasks || []).length
    ? (s.recent_tasks || []).map((t) =>
        `<tr><td>#${esc(t.id)}</td><td>${esc(t.project_name)}</td><td>${esc(t.task_name)}</td><td style="text-align:right">${esc(taskScoreLabel(t))}：${esc(t.score ?? '未提供')}</td><td>${esc(String(t.create_time || '').slice(0, 10))}</td></tr>`).join('')
    : '<tr><td colspan="5" style="color:#999">暂无审查记录</td></tr>'

  const html = `<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<title>棱镜 Prism 代码审查统计报告</title>
<style>
  body{font-family:-apple-system,"PingFang SC","Microsoft YaHei",sans-serif;color:#1f2452;margin:40px;line-height:1.6}
  h1{font-size:24px;margin:0 0 4px} .sub{color:#888;font-size:13px;margin-bottom:24px}
  h2{font-size:16px;margin:28px 0 10px;border-left:4px solid #6366f1;padding-left:10px}
  .cards{display:flex;flex-wrap:wrap;gap:12px} .card{flex:1;min-width:140px;border:1px solid #e8e8ef;border-radius:10px;padding:14px}
  .card .n{font-size:26px;font-weight:700} .card .l{font-size:12px;color:#888}
  table{width:100%;border-collapse:collapse;font-size:13px} td,th{border-bottom:1px solid #eee;padding:7px 10px;text-align:left}
  th{color:#888;font-weight:600}
  @media print{body{margin:16px}}
</style></head><body>
  <h1>棱镜 Prism · 代码审查统计报告</h1>
  <div class="sub">风险与问题类型图表区间:${esc(rangeLabel.value)}；概览为累计值，最近审查不受区间限制。生成时间:${esc(dayjs().format('YYYY-MM-DD HH:mm'))}</div>
  <h2>累计概览</h2>
  <div class="cards">
    <div class="card"><div class="n">${esc(s.review_count)}</div><div class="l">累计成功任务（含测试）</div></div>
    <div class="card"><div class="n">${esc(s.total_issues)}</div><div class="l">累计发现问题</div></div>
    <div class="card"><div class="n">${esc(s.severe_issues)}</div><div class="l">严重问题</div></div>
    <div class="card"><div class="n">${hasAverageScore.value ? esc(s.avg_score) : '暂无代码评分样本'}</div><div class="l">平均代码评分（${esc(s.code_review_count ?? 0)} 份有效代码审查，不含测试）</div></div>
    <div class="card"><div class="n">${esc(s.project_count)}</div><div class="l">可见项目</div></div>
    <div class="card"><div class="n">${esc(s.file_count)}</div><div class="l">代码库文件</div></div>
  </div>
  <p>代码库文件为当前有效入库文件；另有 ${esc(s.archive_file_count ?? 0)} 个整包归档文件。</p>
  <h2>风险等级分布</h2><table><thead><tr><th>等级</th><th style="text-align:right">数量</th></tr></thead><tbody>${rows(riskData.value)}</tbody></table>
  <h2>问题类型分布</h2><table><thead><tr><th>类型</th><th style="text-align:right">数量</th></tr></thead><tbody>${rows(issueTypeData.value)}</tbody></table>
  <h2>最近审查任务</h2><table><thead><tr><th>ID</th><th>项目</th><th>任务</th><th style="text-align:right">评分</th><th>日期</th></tr></thead><tbody>${taskRows}</tbody></table>
  <p style="margin-top:32px;color:#aaa;font-size:12px">— 由棱镜 Prism 智能代码审查平台生成 —</p>
</body></html>`

  // 沿用报告页的文件下载方式；所有动态字段已转义，不依赖弹出窗口。
  let url: string | undefined
  let link: HTMLAnchorElement | undefined
  try {
    url = window.URL.createObjectURL(new Blob([html], { type: 'text/html;charset=utf-8' }))
    link = document.createElement('a')
    link.href = url
    link.download = `prism-statistics-${timeRange.value}d-${dayjs().format('YYYYMMDD-HHmmss')}.html`
    document.body.appendChild(link)
    link.click()
  } catch {
    if (url) window.URL.revokeObjectURL(url)
    ElMessage.error('无法下载统计报告，请检查浏览器下载设置后重试')
    return
  } finally {
    link?.remove()
  }
  // 延迟回收 URL，给浏览器时间接管下载。
  const downloadUrl = url
  setTimeout(() => window.URL.revokeObjectURL(downloadUrl), 60_000)
  ElMessage.info('已请求下载统计报告（HTML），请查看浏览器下载列表。打开文件后可打印保存为 PDF')
}

function onNewReview() {
  if (!canStartReview.value) return
  router.push('/reviews/start')
}

function goReviewDetail(id: number) {
  if (!canViewReviews.value) return
  router.push(`/reviews/${id}`)
}

function goReviewList() {
  if (!canViewReviews.value) return
  router.push('/reviews')
}

/**
 * 加载仪表盘首屏数据，并在请求期间显示统一的动画加载提示
 * @returns Promise<void>
 */
let taskRefreshTimer: ReturnType<typeof setTimeout> | undefined
function onAgentTaskComplete(): void {
  if (taskRefreshTimer) clearTimeout(taskRefreshTimer)
  taskRefreshTimer = setTimeout(() => { void loadDashboard() }, 600)
}

async function loadDashboard(): Promise<void> {
  await Promise.allSettled([loadSummary(), loadCharts(), loadRunning()])
}

onMounted(() => {
  loadDashboard()
  document.addEventListener('visibilitychange', onRunningVisibility)
  window.addEventListener('prism:agent-task-complete', onAgentTaskComplete)
})

onBeforeUnmount(() => {
  disposed = true
  stopRunningPolling()
  document.removeEventListener('visibilitychange', onRunningVisibility)
  if (taskRefreshTimer) clearTimeout(taskRefreshTimer)
  window.removeEventListener('prism:agent-task-complete', onAgentTaskComplete)
})
</script>

<style scoped lang="scss">
.load-feedback {
  color: var(--gray-500);
  font-size: 13px;
  line-height: 1.6;
  margin: 0;

  &.error { color: var(--sev-severe); }
}

button.link {
  border: 0;
  background: transparent;
  font: inherit;
  cursor: pointer;
  padding: 4px 8px;

  &:focus-visible { outline: 2px solid var(--brand-500); outline-offset: 2px; }
}

.dashboard-page {
  display: flex;
  flex-direction: column;
  gap: 16px;
  min-width: 0;
}

/* ============ 页头 ============ */
.page-head {
  display: flex;
  align-items: flex-end;
  justify-content: space-between;
  gap: 24px;
  flex-wrap: wrap;
}

.page-title {
  font-size: var(--fs-2xl);
  font-weight: 600;
  letter-spacing: 0;
  color: var(--gray-900);
  margin: 0;
}

.page-sub {
  margin-top: 4px;
  font-size: 13.5px;
  color: var(--gray-500);

  .hl { color: var(--brand-600); font-weight: 600; }
}

.page-actions {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}

/* ============ 6 卡 ============ */
.stat-grid {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 14px;
}

@media (min-width: 1680px) {
  .stat-grid { grid-template-columns: repeat(6, minmax(0, 1fr)); }
}
@media (max-width: 768px) {
  .stat-grid { grid-template-columns: repeat(2, 1fr); }
}
/* ── 后台进行中面板 ── */
.running-panel {
  border: 1px solid rgba(64, 120, 244, .22);
  background: linear-gradient(180deg, rgba(64, 120, 244, .05), #fff 65%);
  border-radius: 14px; padding: 16px 18px;
}
.running-head { display: flex; justify-content: space-between; align-items: baseline; gap: 12px; margin-bottom: 12px; }
.running-head h3 { margin: 0; font-size: 15.5px; display: flex; align-items: center; gap: 8px; }
.running-count {
  padding: 1px 9px; border-radius: 999px; font-size: 11.5px;
  background: var(--brand-500, #4078f4); color: #fff;
}
.running-sub { margin: 0; font-size: 11.5px; color: var(--gray-400); }
.running-pulse {
  width: 9px; height: 9px; border-radius: 50%; background: #40a35f;
  box-shadow: 0 0 0 0 rgba(64, 163, 99, .5);
  animation: running-ping 1.6s ease-out infinite;
}
@keyframes running-ping {
  0% { box-shadow: 0 0 0 0 rgba(64, 163, 99, .5); }
  70% { box-shadow: 0 0 0 8px rgba(64, 163, 99, 0); }
  100% { box-shadow: 0 0 0 0 rgba(64, 163, 99, 0); }
}
.running-list { display: grid; gap: 8px; }
.running-item {
  display: grid; grid-template-columns: minmax(0, 1fr) 150px 52px; gap: 14px;
  align-items: center; padding: 10px 14px; border-radius: 10px;
  background: #fff; border: 1px solid var(--gray-100, #eef0f4); text-align: left;
}
.running-item.review { cursor: pointer; transition: border-color .15s ease, transform .15s ease; }
.running-item.review:disabled { cursor: default; color: inherit; }
.running-item.review:focus-visible { outline: 2px solid var(--brand-500); outline-offset: 2px; }
.running-item.review:not(:disabled):hover { border-color: var(--brand-300, #a8c4fa); transform: translateY(-1px); }
.running-item.agent { grid-template-columns: auto minmax(0, 1fr) auto; }
.ri-main { display: grid; min-width: 0; }
.ri-main b { font-size: 13px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.ri-meta { overflow-wrap: anywhere; font-size: 11px; color: var(--gray-500); margin-top: 2px; }
.ri-bar { position: relative; height: 7px; border-radius: 999px; background: var(--gray-100, #eef0f4); overflow: hidden; }
.ri-fill {
  position: absolute; inset: 0 auto 0 0; border-radius: 999px;
  background: linear-gradient(90deg, var(--brand-400, #6f9df7), var(--brand-600, #2f5ce0));
  transition: width .5s ease;
}
.ri-bar.indeterminate::after {
  content: ''; position: absolute; left: -40%; top: 0; bottom: 0; width: 40%;
  border-radius: 999px; background: var(--brand-300, #a8c4fa);
  animation: ri-slide 1.4s ease-in-out infinite;
}
@keyframes ri-slide { to { left: 100%; } }
.ri-pct { font-size: 11.5px; color: var(--gray-600); text-align: right; }
.ri-tag {
  padding: 2px 9px; border-radius: 999px; font-size: 10.5px;
  background: rgba(143, 139, 255, .12); color: #6f6bd8;
}
.agent-dot {
  width: 8px; height: 8px; border-radius: 50%; background: #8f8bff;
  animation: running-ping 1.6s ease-out infinite;
}

@media (max-width: 640px) {
  .running-head { flex-wrap: wrap; }
  .running-item { grid-template-columns: minmax(0, 1fr) 48px; gap: 8px; padding: 10px; }
  .running-item.review .ri-main { grid-column: 1 / -1; }
}
.severity-values { display: grid; gap: 6px; list-style: none; padding: 0; margin: 8px 0 0; }
.severity-values li, .severity-values li > span { display: flex; align-items: center; gap: 7px; }
.severity-values li { justify-content: space-between; font-size: 12px; }
.severity-values i { width: 8px; height: 8px; border-radius: 50%; }

/* ── 分析区折叠 ── */
.analysis-fold { border: 1px dashed var(--gray-200); border-radius: 12px; overflow: hidden; }
.analysis-toggle {
  width: 100%; display: flex; align-items: center; gap: 10px; padding: 12px 16px;
  background: #fbfcfe; border: none; cursor: pointer; text-align: left;
}
.analysis-toggle:hover { background: #f5f8ff; }
.at-chevron { color: var(--gray-400); transition: transform .2s ease; font-size: 12px; }
.at-chevron.open { transform: rotate(90deg); }
.at-sub { font-size: 11.5px; color: var(--gray-400); }
.analysis-fold .chart-row { margin-top: 0; }

@media (prefers-reduced-motion: reduce) {
  .ri-fill, .at-chevron { transition: none; }
  .ri-bar.indeterminate::after, .running-pulse, .agent-dot { animation: none; }
}

@media (max-width: 520px) {
  .stat-grid { grid-template-columns: 1fr; }
}

.stat {
  position: relative;
  background: var(--surface-glass);
  border: var(--hairline);
  border-radius: 10px;
  padding: 16px;
  display: flex;
  flex-direction: column;
  gap: 8px;
  overflow: hidden;
  transition: all 0.2s ease;
  backdrop-filter: blur(14px) saturate(1.08);
  -webkit-backdrop-filter: blur(14px) saturate(1.08);

  /* hover 时顶部掠过一记光谱细线,强化「棱镜」品牌感 */
  &::before {
    content: '';
    position: absolute;
    top: 0;
    left: -30%;
    width: 55%;
    height: 2px;
    border-radius: 999px;
    background: linear-gradient(90deg,
      transparent,
      var(--brand-300),
      var(--accent-400),
      transparent);
    opacity: 0;
    transition: opacity 0.25s ease;
    pointer-events: none;
  }

  &:hover {
    border-color: var(--brand-200);
    box-shadow: var(--panel-shadow);
    transform: translateY(-2px);

    &::before {
      opacity: 1;
      animation: statSheen 0.9s ease forwards;
    }
  }

  &.feature {
    background:
      linear-gradient(145deg, rgba(22, 26, 36, 0.96), rgba(31, 35, 48, 0.96));
    color: #fff;
    border: 1px solid rgba(255, 255, 255, 0.12);

    .feature-spectrum {
      position: absolute;
      left: 16px;
      right: 16px;
      bottom: 12px;
      height: 2px;
      background: linear-gradient(90deg,
        var(--dim-style), var(--dim-naming), var(--dim-comment),
        var(--dim-maintain), var(--dim-perf), var(--dim-except),
        var(--dim-bug), var(--dim-security), var(--dim-style));
      opacity: 0.8;
      border-radius: 999px;
      pointer-events: none;
      z-index: 0;
    }

    & > *:not(.feature-spectrum) {
      position: relative;
      z-index: 1;
    }

    .stat-label { color: rgba(255, 255, 255, 0.75); }
    .stat-num   { color: #fff; }
    .stat-unit  { color: rgba(255, 255, 255, 0.5); }
  }
}

.stat-label {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 12.5px;
  color: var(--gray-500);
  z-index: 1;
}

@keyframes statSheen {
  from { left: -30%; }
  to   { left: 100%; }
}

@media (prefers-reduced-motion: reduce) {
  .stat:hover::before { animation: none; opacity: 0.7; }
}

.stat-ico {
  width: 26px;
  height: 26px;
  border-radius: 8px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  font-size: 15px;
  font-family: var(--font-mono);

  .el-icon {
    font-size: inherit;
  }
}

.stat-num {
  display: flex;
  align-items: baseline;
  flex-wrap: wrap;
  gap: 4px;
  font-variant-numeric: tabular-nums;
  font-size: 28px;
  font-weight: 600;
  letter-spacing: 0;
  line-height: 1.1;
  color: var(--gray-900);
  z-index: 1;
}

.stat-unit {
  font-size: 13px;
  color: var(--gray-400);
  margin-left: 0;
  font-weight: 500;
}

.stat-delta {
  font-size: 11px;
  font-family: var(--font-mono);
  color: var(--gray-500);

  &.up   { color: var(--status-fixed); }
  &.down { color: var(--sev-severe); }
}

.stat-gauge {
  margin-top: auto;
  z-index: 1;

  .gauge-track {
    border-radius: 3px;
    overflow: hidden;

    /* 深色卡上的流体进度:轨道透明化,融进卡片底色 */
    .gauge-fluid {
      background: rgba(255, 255, 255, 0.12);
      box-shadow: none;
    }
  }
  .gauge-label {
    display: flex;
    flex-direction: column;
    align-items: flex-start;
    gap: 3px;
    line-height: 1.5;
    font-family: var(--font-mono);
    font-size: 10px;
    color: rgba(255, 255, 255, 0.55);
    margin-top: 6px;
  }
}

/* ============ 图表卡 ============ */
.chart-row {
  display: grid;
  gap: 14px;
  min-width: 0;

  &.two-col   { grid-template-columns: 1.4fr 1fr; }
  &.three-col { grid-template-columns: 1fr 1fr 1fr; }

  @media (max-width: 1100px) {
    &.two-col, &.three-col { grid-template-columns: 1fr; }
  }
}

.security-row {
  display: block;
  min-width: 0;
  overflow: hidden;
}

.chart-card {
  min-width: 0;
  background: var(--surface-glass);
  border: var(--hairline);
  border-radius: 10px;
  padding: 18px 20px;
  box-shadow: var(--shadow-1);
  backdrop-filter: blur(14px) saturate(1.08);
  -webkit-backdrop-filter: blur(14px) saturate(1.08);
  transition: border-color var(--transition-base), box-shadow var(--transition-base), transform var(--transition-base);
}

.chart-card:hover {
  border-color: var(--brand-200);
  box-shadow: var(--shadow-2);
  transform: translateY(-2px);
}

.chart-head {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  gap: 12px;
  margin-bottom: 12px;

  h3 {
    margin: 0;
    font-size: 15px;
    font-weight: 600;
    color: var(--gray-900);
  }
}

.chart-desc {
  margin-top: 3px;
  font-size: 11.5px;
  color: var(--gray-500);
}

.link {
  font-size: 12.5px;
  color: var(--brand-500);
  cursor: pointer;

  &:hover { text-decoration: underline; }
}

/* 窗口全零但累计有数据时的口径提示 */
.empty-hint {
  margin: 6px 0 0;
  font-size: 12px;
  color: var(--gray-500);

  .hl { color: var(--brand-500); }
}

/* ============ 8 维度 legend ============ */
.legend-list {
  display: grid;
  grid-template-columns: repeat(2, 1fr);
  gap: 6px 14px;
  margin-top: 8px;
}

@media (max-width: 520px) {
  /* Dashboard 的局部页头规则比全局响应式规则更具体，必须显式拉伸首列，
     否则标题会按内容宽度贴到右侧，手机上看起来像被截断。 */
  .page-head {
    align-items: stretch;
  }

  .chart-card {
    padding: 16px;
  }

  .chart-head {
    flex-direction: column;
    align-items: stretch;
  }

  .legend-list {
    grid-template-columns: 1fr;
  }
}

@media (prefers-reduced-motion: reduce) {
  .chart-card:hover {
    transform: none;
  }
}

.legend-item {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 12px;
  color: var(--gray-700);

  .dot { width: 8px; height: 8px; border-radius: 50%; }
  .name { flex: 1; }
  .val { color: var(--gray-900); font-weight: 500; }
}

/* ============ Agent 活动流 ============ */
.activity-feed {
  display: flex;
  flex-direction: column;
  gap: 12px;
  max-height: 380px;
  overflow-y: auto;
  padding-right: 4px;
}

.activity-item {
  display: grid;
  grid-template-columns: 32px 1fr auto;
  align-items: center;
  gap: 12px;
  padding: 10px 12px;
  border: var(--hairline);
  border-radius: 8px;
  background: rgba(255, 255, 255, 0.72);
  transition: all 0.15s ease;

  &:hover { border-color: var(--brand-100); background: var(--gray-50); }

  &.live {
    background: linear-gradient(135deg, #FAFAFE, #F0EFFE);
    border-color: var(--brand-100);
    position: relative;
    overflow: hidden;

    &::after {
      content: '';
      position: absolute;
      inset: 0;
      background: linear-gradient(90deg, transparent, rgba(143, 136, 245, 0.18), transparent);
      transform: translateX(-100%);
      animation: lightSweep 2.4s ease-in-out infinite;
    }
  }

  .ico {
    width: 32px;
    height: 32px;
    border-radius: 8px;
    display: flex;
    align-items: center;
    justify-content: center;
    color: #fff;
    font-family: var(--font-mono);
    font-size: 15px;
    font-weight: 600;
    flex-shrink: 0;

    .el-icon {
      font-size: inherit;
    }
  }

  .body { min-width: 0; }

  .title {
    font-size: 13px;
    color: var(--gray-900);
    line-height: 1.5;
    :deep(b) { color: var(--gray-900); font-weight: 600; }
  }

  .meta {
    margin-top: 2px;
    font-size: 11px;
    color: var(--gray-500);
  }

  .when {
    font-size: 11px;
    color: var(--gray-400);
    white-space: nowrap;
  }
}

@media (max-width: 520px) {
  .activity-item {
    grid-template-columns: 32px 1fr;

    .when {
      grid-column: 2;
      justify-self: flex-start;
    }
  }
}

/* ============ TOP6 评分柱 ============ */
.score-bars {
  display: flex;
  flex-direction: column;
  gap: 14px;
  margin-top: 4px;
}

.score-bar {
  .row {
    display: flex;
    justify-content: space-between;
    font-size: 12.5px;
    margin-bottom: 4px;
    color: var(--gray-700);
  }

  .bar-name { color: var(--gray-800); }
  .bar-val  { font-weight: 600; }

  .bar-track {
    height: 6px;
    background: var(--gray-100);
    border-radius: 3px;
    overflow: hidden;
  }
  .bar-fill {
    height: 100%;
    border-radius: 3px;
    transition: width 0.6s ease;
  }
}
</style>
