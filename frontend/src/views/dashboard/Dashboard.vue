<template>
  <div class="dashboard-page">
    <!-- ============ 页头 ============ -->
    <header class="page-head">
      <div>
        <h1 class="page-title font-display">仪表盘</h1>
        <p class="page-sub">
          {{ today }} · 你今天有
          <b class="hl">{{ summary.recent_tasks?.length ?? 0 }}</b>
          个审查任务待复查
        </p>
      </div>
      <div class="page-actions">
        <el-select v-model="timeRange" size="small" style="width: 110px" @change="loadCharts">
          <el-option label="近 7 天" :value="7" />
          <el-option label="近 30 天" :value="30" />
          <el-option label="近 90 天" :value="90" />
        </el-select>
        <el-button @click="onWeeklyReport">导出周报</el-button>
        <el-button type="primary" @click="onNewReview">+ 新建审查</el-button>
      </div>
    </header>

    <PrismLoading
      v-if="loading"
      label="正在加载仪表盘数据"
      sublabel="正在汇总项目、审查任务和风险指标"
    />

    <template v-else>
    <!-- ============ 6 张统计卡 ============ -->
    <section class="stat-grid">
      <div v-for="card in statCards" :key="card.label" class="stat" :class="{ feature: card.feature }">
        <div v-if="card.feature" class="feature-glow"></div>
        <div class="stat-label">
          <span class="stat-ico" :style="card.iconStyle">{{ card.glyph }}</span>
          {{ card.label }}
        </div>
        <div class="stat-num font-display">
          {{ card.value }}<span class="stat-unit">{{ card.unit }}</span>
        </div>
        <div v-if="card.delta" class="stat-delta" :class="card.deltaDir">{{ card.delta }}</div>
        <div v-if="card.feature" class="stat-gauge">
          <div class="gauge-track">
            <div class="gauge-fill" :style="{ width: gaugeWidth }"></div>
          </div>
          <div class="gauge-label">
            <span>风险等级 · {{ riskLevel }}</span>
            <span>目标 90</span>
          </div>
        </div>
      </div>
    </section>

    <!-- ============ v2.1.1 安全态势卡 ============ -->
    <section class="security-row">
      <SecurityPostureCard :days="timeRange" />
    </section>

    <!-- ============ 8 维度极坐标 + Agent 活动流 ============ -->
    <section class="chart-row two-col">
      <article class="chart-card">
        <header class="chart-head">
          <div>
            <h3 class="font-display">8 维度问题分布 · 棱镜光谱</h3>
            <p class="chart-desc">{{ timeRange }} 天内 {{ totalDimCount }} 个问题在 8 个维度上的分布</p>
          </div>
        </header>
        <BaseChart v-if="dimChartReady" :option="dimPolarOption" height="320px" />
        <EmptyState v-else description="正在汇总维度数据" compact />
        <div class="legend-list">
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
            <h3 class="font-display">最近 Agent 审查活动</h3>
            <p class="chart-desc">实时 · 流式</p>
          </div>
          <a class="link" @click="goReviewList">全部 →</a>
        </header>
        <div class="activity-feed">
          <div
            v-for="item in activityFeed"
            :key="item.id"
            class="activity-item"
            :class="{ live: item.live }"
          >
            <div class="ico" :style="{ background: item.color }">{{ item.glyph }}</div>
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

    <!-- ============ 3 个分析图 ============ -->
    <section class="chart-row three-col">
      <article class="chart-card">
        <header class="chart-head">
          <h3 class="font-display">{{ timeRange }} 天审查任务趋势</h3>
        </header>
        <BaseChart v-if="frequencyData.length" :option="trendOption" height="220px" />
        <EmptyState v-else description="暂无趋势数据" compact />
      </article>

      <article class="chart-card">
        <header class="chart-head">
          <h3 class="font-display">严重度分布</h3>
        </header>
        <BaseChart v-if="riskData.length" :option="severityOption" height="220px" />
        <EmptyState v-else description="暂无严重度数据" compact />
      </article>

      <article class="chart-card">
        <header class="chart-head">
          <h3 class="font-display">最近评分 TOP {{ scoreTrendData.length }}</h3>
        </header>
        <div v-if="scoreTrendData.length" class="score-bars">
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
    </template>
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted, computed } from 'vue'
import { useRouter } from 'vue-router'
import dayjs from 'dayjs'
import { ElMessage } from 'element-plus'
import type { EChartsOption } from 'echarts'
import BaseChart from '@/components/chart/BaseChart.vue'
import EmptyState from '@/components/common/EmptyState.vue'
import PrismLoading from '@/components/common/PrismLoading.vue'
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
} from '@/api/dashboard'
import type { RiskItem, IssueTypeItem, ScoreTrendItem, FrequencyItem, SummaryOut } from '@/types/dashboard'

const router = useRouter()
const timeRange = ref(30)
const loading = ref(true)

const summary = ref<SummaryOut>({
  project_count: 0,
  file_count: 0,
  review_count: 0,
  total_issues: 0,
  severe_issues: 0,
  avg_score: 0,
  recent_tasks: [],
})

const riskData = ref<{ name: string; value: number; severity: string }[]>([])
const issueTypeData = ref<{ key: string; name: string; value: number }[]>([])
const scoreTrendData = ref<{ name: string; value: number }[]>([])
const frequencyData = ref<{ name: string; value: number }[]>([])

const today = computed(() => {
  const d = dayjs()
  const weekDays = ['周日', '周一', '周二', '周三', '周四', '周五', '周六']
  return `${d.format('YYYY 年 M 月 D 日')} · ${weekDays[d.day()]}`
})

const dimMeta = DIM_META

const statCards = computed(() => {
  const avgScore = summary.value.avg_score || 0
  const hasReview = summary.value.review_count > 0
  const hasIssue = summary.value.total_issues > 0
  const hasSevere = summary.value.severe_issues > 0
  const hasProject = summary.value.project_count > 0
  const hasFile = summary.value.file_count > 0
  return [
    {
      label: '累计审查任务', value: summary.value.review_count, unit: '次', glyph: '◉',
      iconStyle: { background: 'var(--brand-50)', color: 'var(--brand-600)' },
      delta: hasReview ? '已上线' : '— 暂无数据', deltaDir: hasReview ? 'up' : 'flat',
      feature: false,
    },
    {
      label: '累计发现问题', value: summary.value.total_issues, unit: '个', glyph: '⚠',
      iconStyle: { background: 'rgba(226,92,115,.10)', color: 'var(--dim-bug)' },
      delta: hasIssue ? `共 ${summary.value.severe_issues} 个严重` : '— 暂无',
      deltaDir: 'flat',
      feature: false,
    },
    {
      label: '严重问题', value: summary.value.severe_issues, unit: '个', glyph: '!',
      iconStyle: { background: 'rgba(220,73,97,.10)', color: 'var(--sev-severe)' },
      delta: hasSevere ? '需优先处理' : '— 暂无',
      deltaDir: hasSevere ? 'down' : 'flat',
      feature: false,
    },
    {
      label: '平均代码评分', value: avgScore.toFixed(1), unit: '/100', glyph: '★',
      iconStyle: { background: 'rgba(255,255,255,.16)', color: '#fff' },
      delta: hasReview ? null : '— 暂无审查',
      deltaDir: 'flat',
      feature: true,
    },
    {
      label: '活跃项目', value: summary.value.project_count, unit: '个', glyph: '◫',
      iconStyle: { background: 'rgba(75,155,255,.10)', color: 'var(--dim-naming)' },
      delta: hasProject ? '已建项目' : '— 暂无', deltaDir: 'flat',
      feature: false,
    },
    {
      label: '代码文件', value: summary.value.file_count, unit: '份', glyph: '✦',
      iconStyle: { background: 'rgba(61,188,217,.12)', color: 'var(--accent-600)' },
      delta: hasFile ? '已上传' : '— 暂无', deltaDir: 'flat',
      feature: false,
    },
  ]
})

const gaugeWidth = computed(() => `${Math.min(100, Math.max(0, summary.value.avg_score || 0))}%`)

const riskLevel = computed(() => {
  const s = summary.value.avg_score || 0
  if (s >= 90) return '优秀'
  if (s >= 80) return '良好'
  if (s >= 70) return '一般'
  if (s >= 60) return '及格'
  return '风险'
})

const totalDimCount = computed(() => issueTypeData.value.reduce((s, x) => s + x.value, 0))

const dimSummary = computed(() => {
  return dimMeta
    .map((d) => {
      const found = issueTypeData.value.find((x) => x.key === d.key)
      return { ...d, value: found?.value ?? 0 }
    })
    .sort((a, b) => b.value - a.value)
})

const dimChartReady = computed(() => issueTypeData.value.length > 0 || totalDimCount.value > 0)

const dimPolarOption = computed<EChartsOption>(() => ({
  polar: { radius: ['18%', '78%'] },
  angleAxis: {
    type: 'category',
    data: dimMeta.map((d) => d.name),
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
    data: dimMeta.map((d) => {
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
  if (score >= 85) return '#4FB87A'
  if (score >= 70) return '#D9A857'
  if (score >= 60) return '#E27C4A'
  return '#DC4961'
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
  glyph: string
  color: string
  title: string
  meta: string
  when: string
  live?: boolean
}

const activityFeed = computed<ActivityItem[]>(() => {
  const tasks = (summary.value.recent_tasks ?? []) as Array<Record<string, unknown>>
  if (tasks.length === 0) {
    return [{
      id: 0,
      glyph: 'AG',
      color: 'var(--brand-500)',
      title: '正在等待 Agent 审查任务上线',
      meta: 'DeepSeek V4 · 待命中',
      when: '刚刚',
      live: true,
    }]
  }
  return tasks.slice(0, 6).map((t, i) => {
    const id = (t.id as number) ?? i
    const score = (t.score as number) ?? 0
    const status = (t.status as string) ?? 'pending'
    const projectName = (t.project_name as string) ?? (t.task_name as string) ?? `任务 #${id}`
    const safeProjectName = escapeHtml(projectName)
    const created = t.create_time ? dayjs(t.create_time as string).fromNow?.() ?? dayjs(t.create_time as string).format('M/D HH:mm') : ''
    const live = status === 'running' && i === 0
    const ok = status === 'success'
    return {
      id,
      glyph: live ? 'AG' : ok ? '✓' : '!',
      color: live ? 'var(--brand-500)' : ok ? 'var(--status-fixed)' : 'var(--dim-bug)',
      title: live
        ? `正在审查 <b>${safeProjectName}</b>`
        : ok
          ? `完成 <b>${safeProjectName}</b>，评分 <b style="color: var(--status-fixed);">${score}</b>`
          : `<b>${safeProjectName}</b> 检出问题`,
      meta: `状态：${status}${ok ? ` · 评分 ${score}` : ''}`,
      when: created || '近期',
      live,
    }
  })
})

async function loadSummary() {
  summary.value = await getSummary()
}

async function loadRiskDistribution() {
  const data = await getRiskDistribution(timeRange.value)
  riskData.value = data.map((item: RiskItem) => ({
    severity: item.severity,
    name: severityDisplayLabel(item.severity),
    value: item.count,
  }))
}

async function loadIssueTypeStatistics() {
  const data = await getIssueTypeStatistics(timeRange.value)
  const aggregate: Record<string, { key: string; name: string; value: number }> = {}
  for (const item of data as IssueTypeItem[]) {
    const norm = normalizeDimKey(item.issue_type)
    const key = norm ?? '__other__'
    const meta = dimMeta.find((d) => d.key === key)
    const name = meta?.name ?? item.issue_type ?? '其他'
    if (!aggregate[key]) aggregate[key] = { key, name, value: 0 }
    aggregate[key].value += item.count
  }
  issueTypeData.value = Object.values(aggregate)
}

async function loadScoreTrend() {
  const data = await getScoreTrend(6)
  scoreTrendData.value = data.map((item: ScoreTrendItem) => ({
    name: `#${item.task_id}`,
    value: item.score,
  }))
}

async function loadReviewFrequency() {
  const data = await getReviewFrequency(timeRange.value)
  frequencyData.value = data.map((item: FrequencyItem) => ({
    name: dayjs(item.date).format('M/D'),
    value: item.count,
  }))
}

async function loadCharts() {
  await Promise.all([
    loadRiskDistribution(),
    loadIssueTypeStatistics(),
    loadScoreTrend(),
    loadReviewFrequency(),
  ])
}

function onWeeklyReport() {
  const s = summary.value
  const esc = (v: unknown) => String(v ?? '').replace(/[&<>]/g, (c) =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;' }[c] || c))
  const rows = (arr: { name: string; value: number }[]) =>
    arr.length
      ? arr.map((x) => `<tr><td>${esc(x.name)}</td><td style="text-align:right">${esc(x.value)}</td></tr>`).join('')
      : '<tr><td colspan="2" style="color:#999">暂无数据</td></tr>'
  const taskRows = (s.recent_tasks || []).length
    ? (s.recent_tasks || []).map((t) =>
        `<tr><td>#${esc(t.id)}</td><td style="text-align:right">${esc(t.score)}</td><td>${esc(String(t.create_time || '').slice(0, 10))}</td></tr>`).join('')
    : '<tr><td colspan="3" style="color:#999">暂无审查记录</td></tr>'

  const html = `<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<title>棱镜 Prism 代码审查周报</title>
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
  <h1>🛡 棱镜 Prism · 代码审查周报</h1>
  <div class="sub">统计区间:近 ${esc(timeRange.value)} 天 · 生成时间:${esc(dayjs().format('YYYY-MM-DD HH:mm'))}</div>
  <h2>总体概览</h2>
  <div class="cards">
    <div class="card"><div class="n">${esc(s.review_count)}</div><div class="l">累计审查任务</div></div>
    <div class="card"><div class="n">${esc(s.total_issues)}</div><div class="l">累计发现问题</div></div>
    <div class="card"><div class="n">${esc(s.severe_issues)}</div><div class="l">严重问题</div></div>
    <div class="card"><div class="n">${esc(s.avg_score)}</div><div class="l">平均代码评分</div></div>
    <div class="card"><div class="n">${esc(s.project_count)}</div><div class="l">活跃项目</div></div>
    <div class="card"><div class="n">${esc(s.file_count)}</div><div class="l">代码文件</div></div>
  </div>
  <h2>风险等级分布</h2><table><thead><tr><th>等级</th><th style="text-align:right">数量</th></tr></thead><tbody>${rows(riskData.value)}</tbody></table>
  <h2>问题类型分布</h2><table><thead><tr><th>类型</th><th style="text-align:right">数量</th></tr></thead><tbody>${rows(issueTypeData.value)}</tbody></table>
  <h2>最近审查任务</h2><table><thead><tr><th>任务</th><th style="text-align:right">评分</th><th>日期</th></tr></thead><tbody>${taskRows}</tbody></table>
  <p style="margin-top:32px;color:#aaa;font-size:12px">— 由棱镜 Prism 智能代码审查平台生成 —</p>
</body></html>`

  const w = window.open('', '_blank')
  if (!w) {
    ElMessage.warning('请允许弹出窗口以导出周报')
    return
  }
  w.document.write(html)
  w.document.close()
  w.focus()
  setTimeout(() => w.print(), 300)
  ElMessage.success('周报已生成,可在打印窗口保存为 PDF')
}

function onNewReview() {
  router.push('/reviews/start')
}

function goReviewList() {
  router.push('/reviews')
}

/**
 * 加载仪表盘首屏数据，并在请求期间显示统一的动画加载提示
 * @returns Promise<void>
 */
async function loadDashboard(): Promise<void> {
  loading.value = true
  try {
    await Promise.all([loadSummary(), loadCharts()])
  } finally {
    loading.value = false
  }
}

onMounted(() => {
  loadDashboard()
})
</script>

<style scoped lang="scss">
.dashboard-page {
  display: flex;
  flex-direction: column;
  gap: 16px;
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
  font-size: 26px;
  font-weight: 600;
  letter-spacing: -0.015em;
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
}

/* ============ 6 卡 ============ */
.stat-grid {
  display: grid;
  grid-template-columns: repeat(6, 1fr);
  gap: 14px;
}

@media (max-width: 1280px) {
  .stat-grid { grid-template-columns: repeat(3, 1fr); }
}
@media (max-width: 768px) {
  .stat-grid { grid-template-columns: repeat(2, 1fr); }
}

.stat {
  position: relative;
  background: #fff;
  border: 1px solid var(--gray-100);
  border-radius: 12px;
  padding: 16px;
  display: flex;
  flex-direction: column;
  gap: 8px;
  overflow: hidden;
  transition: all 0.2s ease;

  &:hover {
    border-color: var(--brand-200);
    box-shadow: var(--shadow-2);
    transform: translateY(-1px);
  }

  &.feature {
    background: rgba(26, 30, 44, 0.85);
    backdrop-filter: blur(12px);
    -webkit-backdrop-filter: blur(12px);
    color: #fff;
    border: 1px solid rgba(255, 255, 255, 0.12);

    .feature-glow {
      position: absolute;
      right: -80px;
      top: -80px;
      width: 200px;
      height: 200px;
      background: conic-gradient(from 180deg,
        var(--dim-style), var(--dim-naming), var(--dim-comment),
        var(--dim-maintain), var(--dim-perf), var(--dim-except),
        var(--dim-bug), var(--dim-security), var(--dim-style));
      filter: blur(36px);
      opacity: 0.65;
      border-radius: 50%;
      animation: rotateGlow 15s linear infinite;
      pointer-events: none;
      z-index: 0;
    }

    & > *:not(.feature-glow) {
      position: relative;
      z-index: 1;
    }

    .stat-label { color: rgba(255, 255, 255, 0.75); }
    .stat-num   { color: #fff; }
    .stat-unit  { color: rgba(255, 255, 255, 0.5); }
  }
}

@keyframes rotateGlow {
  0% { transform: rotate(0deg); }
  100% { transform: rotate(360deg); }
}

.stat-label {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 12.5px;
  color: var(--gray-500);
  z-index: 1;
}

.stat-ico {
  width: 22px;
  height: 22px;
  border-radius: 6px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  font-size: 12px;
  font-family: var(--font-mono);
}

.stat-num {
  font-size: 28px;
  font-weight: 600;
  letter-spacing: -0.015em;
  line-height: 1.1;
  color: var(--gray-900);
  z-index: 1;
}

.stat-unit {
  font-size: 13px;
  color: var(--gray-400);
  margin-left: 4px;
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
    height: 6px;
    background: rgba(255, 255, 255, 0.12);
    border-radius: 3px;
    overflow: hidden;
  }
  .gauge-fill {
    height: 100%;
    background: linear-gradient(90deg, var(--brand-300), var(--accent-300));
    border-radius: 3px;
    transition: width 0.6s cubic-bezier(.4, 0, .2, 1);
  }
  .gauge-label {
    display: flex;
    justify-content: space-between;
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

  &.two-col   { grid-template-columns: 1.4fr 1fr; }
  &.three-col { grid-template-columns: 1fr 1fr 1fr; }

  @media (max-width: 1100px) {
    &.two-col, &.three-col { grid-template-columns: 1fr; }
  }
}

.security-row {
  display: block;
}

.chart-card {
  background: #fff;
  border: 1px solid var(--gray-100);
  border-radius: 12px;
  padding: 18px 20px;
  box-shadow: var(--shadow-1);
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

/* ============ 8 维度 legend ============ */
.legend-list {
  display: grid;
  grid-template-columns: repeat(2, 1fr);
  gap: 6px 14px;
  margin-top: 8px;
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
  border: 1px solid var(--gray-100);
  border-radius: 10px;
  background: #fff;
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
    font-size: 12px;
    font-weight: 600;
    flex-shrink: 0;
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
