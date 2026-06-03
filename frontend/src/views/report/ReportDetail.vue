<template>
  <div class="report-detail-page">
    <!-- ============ 顶部操作栏（打印时隐藏）============ -->
    <header class="page-head no-print">
      <el-button link class="back-btn" @click="$router.back()">
        <el-icon><ArrowLeft /></el-icon>返回列表
      </el-button>
      <h1 class="font-display">审查报告</h1>
      <div class="head-actions">
        <el-button @click="onPrint">
          <el-icon><Printer /></el-icon>打印
        </el-button>
        <el-button :loading="exportingWord" @click="downloadWord">
          <el-icon><Document /></el-icon>导出 Word
        </el-button>
        <el-button type="primary" :loading="exportingPdf" @click="downloadPdf">
          <el-icon><Download /></el-icon>导出 PDF
        </el-button>
      </div>
    </header>

    <div v-if="report" class="report-paper">
      <!-- ============ 暗色封面 ============ -->
      <section class="cover">
        <header class="cover-head">
          <span class="prism-mark"></span>
          <div class="cover-id font-mono">PRISM REPORT · #{{ taskId }}</div>
          <div class="cover-date font-mono">{{ formatDate(report.task?.create_time as string) }}</div>
        </header>

        <div class="cover-main">
          <div class="cover-meta">
            <div class="cover-label font-mono">PROJECT</div>
            <h2 class="cover-project font-display">{{ projectName }}</h2>
            <p class="cover-task font-mono">{{ taskName }} · {{ reviewType }}</p>

            <div class="cover-tags">
              <span class="cover-tag">{{ language || 'unknown' }}</span>
              <span class="cover-tag">{{ totalFiles }} 文件</span>
              <span class="cover-tag">{{ formatDuration(durationMs) }}</span>
              <span class="cover-tag">{{ ruleCount }} 规则</span>
            </div>
          </div>

          <div class="cover-score">
            <div class="score-ring">
              <svg viewBox="0 0 120 120" width="160" height="160">
                <circle cx="60" cy="60" r="52" fill="none" stroke="rgba(255,255,255,.10)" stroke-width="6"/>
                <circle
                  cx="60" cy="60" r="52" fill="none"
                  :stroke="scoreFlatColor(animatedScore)" stroke-width="6"
                  stroke-linecap="round"
                  :stroke-dasharray="`${animatedScore * 3.26} 326`"
                  transform="rotate(-90 60 60)"
                />
              </svg>
              <div class="ring-text">
                <div class="ring-val font-display">{{ animatedScore }}</div>
                <div class="ring-out font-mono">/ 100</div>
              </div>
            </div>
            <div class="risk-tag" :style="{ color: scoreFlatColor(animatedScore), borderColor: scoreFlatColor(animatedScore) }">
              {{ riskLevel }}
            </div>
          </div>
        </div>

        <p v-if="report.summary" class="cover-summary">{{ report.summary }}</p>
      </section>

      <!-- ============ 严重度 + 修复进度 + 评分历史 ============ -->
      <section class="row-grid no-break">
        <div class="rg-card">
          <div class="rg-title font-display">严重度分布</div>
          <div class="sev-rows">
            <div v-for="r in severityRows" :key="r.key" class="sev-row">
              <span class="sr-label">
                <span class="sr-dot" :style="{ background: r.color }"></span>{{ r.label }}
              </span>
              <div class="sr-bar">
                <div class="sr-fill" :style="{ width: `${r.percent}%`, background: r.color }"></div>
              </div>
              <span class="sr-val font-mono">{{ r.value }}</span>
            </div>
          </div>
        </div>

        <div class="rg-card">
          <div class="rg-title font-display">修复进度</div>
          <div class="fix-gauge">
            <svg viewBox="0 0 120 70" width="100%">
              <path d="M 10 60 A 50 50 0 0 1 110 60" fill="none" stroke="var(--gray-100)" stroke-width="10" stroke-linecap="round"/>
              <path
                d="M 10 60 A 50 50 0 0 1 110 60" fill="none"
                stroke="url(#fix-grad)" stroke-width="10" stroke-linecap="round"
                :stroke-dasharray="`${fixedPercent * 1.57} 1000`"
              />
              <defs>
                <linearGradient id="fix-grad" x1="0" x2="1">
                  <stop offset="0" stop-color="#5B58E8"/>
                  <stop offset="1" stop-color="#4FB87A"/>
                </linearGradient>
              </defs>
            </svg>
            <div class="fix-text">
              <span class="fix-val font-display">{{ fixedPercent }}%</span>
              <span class="fix-sub font-mono">已修复 {{ fixedCount }} / {{ totalIssues }}</span>
            </div>
          </div>
        </div>

        <div class="rg-card">
          <div class="rg-title font-display">代码规模</div>
          <div class="scale-rows">
            <div class="scale-row">
              <span class="font-mono scale-label">总文件</span>
              <span class="font-display scale-val">{{ totalFiles }}</span>
            </div>
            <div class="scale-row">
              <span class="font-mono scale-label">问题</span>
              <span class="font-display scale-val">{{ totalIssues }}</span>
            </div>
            <div class="scale-row">
              <span class="font-mono scale-label">耗时</span>
              <span class="font-display scale-val">{{ formatDuration(durationMs) }}</span>
            </div>
            <div class="scale-row">
              <span class="font-mono scale-label">规则</span>
              <span class="font-display scale-val">{{ ruleCount }}</span>
            </div>
          </div>
        </div>
      </section>

      <!-- ============ 8 维度雷达 ============ -->
      <section class="card no-break">
        <header class="card-head">
          <h3 class="font-display">8 维度评分雷达</h3>
          <p class="card-desc">代码规范 / 命名 / 注释 / 维护 / 性能 / 异常 / Bug / 安全</p>
        </header>
        <BaseChart v-if="hasDimData" :option="radarOption" height="360px" />
        <EmptyState v-else description="暂无 8 维度数据" compact />
      </section>

      <!-- ============ 文件列表 ============ -->
      <section class="card">
        <header class="card-head">
          <h3 class="font-display">审查文件 ({{ report.files.length }})</h3>
        </header>
        <table class="paper-table">
          <thead>
            <tr>
              <th>文件名</th>
              <th class="col-num">语言</th>
              <th class="col-num">问题</th>
              <th class="col-num">严重</th>
              <th class="col-num">评分</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="(f, i) in report.files" :key="i">
              <td class="font-mono">{{ f.file_name }}</td>
              <td>{{ f.language || '-' }}</td>
              <td class="font-mono">{{ f.issue_count }}</td>
              <td class="font-mono" :style="{ color: f.severe_count ? 'var(--sev-severe)' : 'inherit' }">
                {{ f.severe_count ?? 0 }}
              </td>
              <td class="font-mono" :style="{ color: scoreFlatColor(Number(f.score) || 0) }">
                {{ f.score ?? '-' }}
              </td>
            </tr>
          </tbody>
        </table>
      </section>

      <!-- ============ 使用规则 ============ -->
      <section v-if="report.rules_snapshot?.length" class="card">
        <header class="card-head">
          <h3 class="font-display">使用规则 ({{ report.rules_snapshot.length }})</h3>
        </header>
        <div class="rules-list">
          <span
            v-for="(rule, i) in report.rules_snapshot"
            :key="i"
            class="rule-chip font-mono"
          >
            {{ ruleLabel(rule) }}
          </span>
        </div>
      </section>

      <!-- ============ AI 总结 ============ -->
      <section v-if="report.summary" class="card ai-card no-break">
        <header class="card-head">
          <h3 class="font-display">
            <span class="prism-mark sm"></span>AI 智能体总结建议
          </h3>
        </header>
        <div class="ai-summary">{{ report.summary }}</div>
      </section>

      <footer class="paper-foot font-mono">
        PRISM v1.0 · 基于大模型智能体的代码质量审查管理系统 · 生成于 {{ formatDate(new Date().toISOString()) }}
      </footer>
    </div>

    <PrismLoading
      v-else-if="loading"
      label="正在加载审查报告"
      sublabel="正在整理评分、问题和导出信息"
    />
    <EmptyState v-else-if="!loading" description="报告数据加载失败" />
  </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted, watch } from 'vue'
import { useRoute } from 'vue-router'
import { ElMessage } from 'element-plus'
import { ArrowLeft, Document, Download, Printer } from '@element-plus/icons-vue'
import dayjs from 'dayjs'
import type { EChartsOption } from 'echarts'
import BaseChart from '@/components/chart/BaseChart.vue'
import EmptyState from '@/components/common/EmptyState.vue'
import PrismLoading from '@/components/common/PrismLoading.vue'
import { getReportDetail, exportWord as apiExportWord, exportPdf as apiExportPdf } from '@/api/report'
import type { ReportDetailOut } from '@/types/report'
import { PRISM_SEVERITY_COLORS, PRISM_DIM_COLORS } from '@/components/chart/prismTheme'
import { reviewTypeLabel } from '@/constants/reviewType'

const route = useRoute()
const taskId = Number(route.params.id)

const loading = ref(true)
const report = ref<ReportDetailOut | null>(null)
const exportingWord = ref(false)
const exportingPdf = ref(false)

const stats = computed<Record<string, unknown>>(() => report.value?.stats ?? {})

const projectName = computed(() => (report.value?.project?.project_name as string) ?? '未命名项目')
const taskName    = computed(() => (report.value?.task?.task_name as string) ?? `任务 #${taskId}`)
const reviewType  = computed(() => reviewTypeLabel(report.value?.task?.review_type as string))
const language    = computed(() => (report.value?.project?.language as string) ?? '')
const totalFiles  = computed(() => Number(report.value?.task?.total_files ?? 0))
const durationMs  = computed(() => Number(report.value?.task?.duration_ms ?? 0))
const ruleCount   = computed(() => report.value?.rules_snapshot?.length ?? 0)

const score        = computed(() => Math.round(Number(stats.value.score ?? 0)))
const animatedScore = ref(0)

function animateScore(target: number): void {
  const duration = 1500 // 动画时长 (ms)
  const startTime = performance.now()
  const startVal = animatedScore.value

  function step(now: number) {
    const elapsed = now - startTime
    const progress = Math.min(elapsed / duration, 1)
    const easeProgress = progress * (2 - progress) // easeOutQuad
    animatedScore.value = Math.round(startVal + (target - startVal) * easeProgress)

    if (progress < 1) {
      requestAnimationFrame(step)
    }
  }
  requestAnimationFrame(step)
}

watch(score, (newVal) => {
  if (typeof newVal === 'number' && !Number.isNaN(newVal)) {
    animateScore(newVal)
  }
})
const totalIssues  = computed(() => Number(stats.value.total_issues ?? 0))
const severeCount  = computed(() => Number(stats.value.severe ?? 0))
const highCount    = computed(() => Number(stats.value.high ?? 0))
const mediumCount  = computed(() => Number(stats.value.medium ?? 0))
const lowCount     = computed(() => Number(stats.value.low ?? 0))
const fixedCount   = computed(() => Number(stats.value.fixed ?? 0))
const fixedPercent = computed(() => totalIssues.value > 0 ? Math.round((fixedCount.value / totalIssues.value) * 100) : 0)

const severityRows = computed(() => {
  const total = Math.max(1, totalIssues.value)
  return [
    { key: 'severe', label: '危急', value: severeCount.value, percent: (severeCount.value / total) * 100, color: PRISM_SEVERITY_COLORS.severe },
    { key: 'high',   label: '高',   value: highCount.value,   percent: (highCount.value   / total) * 100, color: PRISM_SEVERITY_COLORS.high   },
    { key: 'medium', label: '中',   value: mediumCount.value, percent: (mediumCount.value / total) * 100, color: PRISM_SEVERITY_COLORS.medium },
    { key: 'low',    label: '低',   value: lowCount.value,    percent: (lowCount.value    / total) * 100, color: PRISM_SEVERITY_COLORS.low    },
  ]
})

import { DIM_KEYS, DIM_LABELS, normalizeDimKey } from '@/constants/dim'

const dimKeys = DIM_KEYS
const dimLabels = dimKeys.map((k) => DIM_LABELS[k])

const dimScores = computed<number[]>(() => {
  const rawMap: Record<string, number> = {}
  const dim = (stats.value.dim_scores as Record<string, number>) ?? {}
  // 兼容后端可能用 maintainability/bug 等同义键
  for (const [k, v] of Object.entries(dim)) {
    const norm = normalizeDimKey(k)
    if (norm && typeof v === 'number') rawMap[norm] = v
  }
  return dimKeys.map((k) => {
    const v = rawMap[k]
    // 缺失维度用 NaN, ECharts 雷达图会显示为缺口,不再用整体 score 兜底掩盖
    return typeof v === 'number' && v > 0 ? Math.round(v) : NaN
  })
})

const hasDimData = computed(() => dimScores.value.some((v) => !Number.isNaN(v) && v > 0))

const radarOption = computed<EChartsOption>(() => ({
  tooltip: {},
  radar: {
    indicator: dimLabels.map((name) => ({ name, max: 100 })),
    radius: '68%',
    splitArea: { areaStyle: { color: ['#FAFAFE', '#F7F8FA'] } },
    axisLine: { lineStyle: { color: '#E0E3EA' } },
    splitLine: { lineStyle: { color: '#E0E3EA' } },
    axisName: { color: '#383E4D', fontSize: 11.5 },
  },
  series: [{
    type: 'radar',
    name: '8 维度评分',
    data: [{
      value: dimScores.value,
      name: '本次评分',
      areaStyle: { color: 'rgba(91, 88, 232, 0.18)' },
      lineStyle: { color: '#5B58E8', width: 2 },
      itemStyle: { color: '#5B58E8' },
    }],
  }],
  color: PRISM_DIM_COLORS,
}))

const riskLevel = computed(() => {
  if (animatedScore.value >= 90) return '优秀 · 可发布'
  if (animatedScore.value >= 80) return '良好 · 关注潜在风险'
  if (animatedScore.value >= 70) return '一般 · 建议修复'
  if (animatedScore.value >= 60) return '及格 · 需重构'
  return '风险 · 必须处理'
})

function formatDate(s?: string): string {
  if (!s) return '-'
  return dayjs(s).format('YYYY-MM-DD HH:mm')
}

function formatDuration(ms: number): string {
  if (!ms) return '—'
  if (ms < 1000) return `${ms}ms`
  if (ms < 60000) return `${(ms / 1000).toFixed(1)}s`
  return `${Math.floor(ms / 60000)}m${Math.round((ms % 60000) / 1000)}s`
}

function scoreFlatColor(score: number): string {
  if (score >= 85) return '#4FB87A'
  if (score >= 70) return '#D9A857'
  if (score >= 60) return '#E27C4A'
  return '#DC4961'
}

function ruleLabel(rule: Record<string, unknown>): string {
  return (rule.rule_name as string) || (rule.name as string)
    || (rule.rule_code as string) || (rule.code as string) || '—'
}

async function loadReport() {
  loading.value = true
  try {
    report.value = await getReportDetail(taskId)
  } finally {
    loading.value = false
  }
}

function downloadBlob(response: Blob, filename: string) {
  const url = window.URL.createObjectURL(response)
  const link = document.createElement('a')
  link.href = url
  link.download = filename
  document.body.appendChild(link)
  link.click()
  document.body.removeChild(link)
  window.URL.revokeObjectURL(url)
}

async function downloadWord() {
  exportingWord.value = true
  try {
    const response = await apiExportWord(taskId)
    downloadBlob(response as unknown as Blob, `review_report_${taskId}.docx`)
    ElMessage.success('Word 报告导出成功')
  } catch {
    ElMessage.error('导出失败')
  } finally {
    exportingWord.value = false
  }
}

async function downloadPdf() {
  exportingPdf.value = true
  try {
    const response = await apiExportPdf(taskId)
    downloadBlob(response as unknown as Blob, `review_report_${taskId}.pdf`)
    ElMessage.success('PDF 报告导出成功')
  } catch {
    ElMessage.error('导出失败')
  } finally {
    exportingPdf.value = false
  }
}

function onPrint() {
  window.print()
}

onMounted(() => {
  loadReport()
})
</script>

<style scoped lang="scss">
.report-detail-page {
  display: flex;
  flex-direction: column;
  gap: 16px;
}

/* ============ 顶部 ============ */
.page-head {
  display: flex;
  align-items: center;
  gap: 14px;

  h1 {
    margin: 0;
    flex: 1;
    font-size: 22px;
    font-weight: 600;
    color: var(--gray-900);
  }
}

.head-actions {
  display: flex;
  gap: 8px;
}

/* ============ 报告纸 ============ */
.report-paper {
  background: #fff;
  border: 1px solid var(--gray-100);
  border-radius: 16px;
  overflow: hidden;
  box-shadow: var(--shadow-2);
  display: flex;
  flex-direction: column;
}

/* ============ 暗色封面 ============ */
.cover {
  position: relative;
  background: linear-gradient(135deg, #161A24 0%, #1F1A3A 55%, #2D2B82 100%);
  color: #fff;
  padding: 40px 48px;
  overflow: hidden;

  &::before {
    content: '';
    position: absolute;
    right: -160px;
    top: -180px;
    width: 540px;
    height: 540px;
    background: conic-gradient(from 220deg,
      var(--dim-style), var(--dim-naming), var(--dim-comment), var(--dim-maintain),
      var(--dim-perf), var(--dim-except), var(--dim-bug), var(--dim-security), var(--dim-style));
    filter: blur(80px);
    opacity: 0.42;
    border-radius: 50%;
    pointer-events: none;
  }

  & > * { position: relative; z-index: 1; }
}

.cover-head {
  display: flex;
  align-items: center;
  gap: 14px;
  padding-bottom: 24px;
  border-bottom: 1px solid rgba(255, 255, 255, 0.08);
}

.cover-id {
  font-size: 11px;
  letter-spacing: 0.14em;
  color: var(--brand-300);
}

.cover-date {
  margin-left: auto;
  font-size: 11px;
  color: rgba(255, 255, 255, 0.5);
}

.cover-main {
  display: grid;
  grid-template-columns: 1fr auto;
  gap: 48px;
  align-items: center;
  margin: 32px 0;
}

.cover-label {
  font-size: 11px;
  letter-spacing: 0.14em;
  color: var(--brand-300);
  text-transform: uppercase;
}

.cover-project {
  font-size: 40px;
  font-weight: 600;
  letter-spacing: -0.02em;
  line-height: 1.1;
  margin: 12px 0 8px;
  color: #fff;
}

.cover-task {
  font-size: 13px;
  color: rgba(255, 255, 255, 0.65);
  margin: 0;
}

.cover-tags {
  display: flex;
  gap: 8px;
  margin-top: 20px;
  flex-wrap: wrap;
}

.cover-tag {
  padding: 4px 12px;
  border-radius: 999px;
  background: rgba(255, 255, 255, 0.06);
  border: 1px solid rgba(255, 255, 255, 0.12);
  font-size: 11.5px;
  font-family: var(--font-mono);
  color: rgba(255, 255, 255, 0.75);
}

.cover-score {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 14px;
}

.score-ring {
  position: relative;
  width: 160px;
  height: 160px;
}

.ring-text {
  position: absolute;
  inset: 0;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
}

.ring-val {
  font-size: 56px;
  font-weight: 600;
  letter-spacing: -0.02em;
  line-height: 1;
}

.ring-out {
  font-size: 12px;
  color: rgba(255, 255, 255, 0.55);
  margin-top: 4px;
}

.risk-tag {
  padding: 4px 14px;
  border: 1.5px solid;
  border-radius: 999px;
  font-size: 12px;
  font-family: var(--font-display);
  font-weight: 600;
}

.cover-summary {
  font-size: 13.5px;
  line-height: 1.85;
  color: rgba(255, 255, 255, 0.78);
  max-width: 720px;
  padding-top: 24px;
  border-top: 1px solid rgba(255, 255, 255, 0.08);
  margin: 0;
}

/* ============ 多列卡片行 ============ */
.row-grid {
  display: grid;
  grid-template-columns: 1.2fr 1fr 1fr;
  gap: 14px;
  padding: 24px;
  border-bottom: 1px solid var(--gray-100);
}

@media (max-width: 1100px) {
  .row-grid { grid-template-columns: 1fr; }
}

.rg-card {
  padding: 18px;
  border: 1px solid var(--gray-100);
  border-radius: 12px;
  background: #fff;
}

.rg-title {
  font-size: 13px;
  font-weight: 600;
  color: var(--gray-900);
  margin-bottom: 12px;
}

.sev-rows {
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.sev-row {
  display: grid;
  grid-template-columns: 70px 1fr 40px;
  align-items: center;
  gap: 10px;
  font-size: 12px;
}

.sr-label {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  color: var(--gray-700);
}

.sr-dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
}

.sr-bar {
  height: 8px;
  background: var(--gray-100);
  border-radius: 4px;
  overflow: hidden;
}

.sr-fill {
  height: 100%;
  border-radius: 4px;
  transition: width 0.6s ease;
}

.sr-val {
  text-align: right;
  font-weight: 600;
  color: var(--gray-800);
}

.fix-gauge {
  position: relative;
  text-align: center;
}

.fix-text {
  margin-top: -16px;
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.fix-val {
  font-size: 32px;
  font-weight: 600;
  color: var(--gray-900);
  line-height: 1;
}

.fix-sub {
  font-size: 11px;
  color: var(--gray-500);
}

.scale-rows {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 12px 18px;
}

.scale-row {
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.scale-label {
  font-size: 10px;
  letter-spacing: 0.06em;
  color: var(--gray-500);
  text-transform: uppercase;
}

.scale-val {
  font-size: 22px;
  font-weight: 600;
  color: var(--gray-900);
  letter-spacing: -0.01em;
}

/* ============ 通用卡片 ============ */
.card {
  padding: 24px 28px;
  border-bottom: 1px solid var(--gray-100);

  &:last-of-type { border-bottom: none; }
}

.card-head {
  margin-bottom: 14px;

  h3 {
    display: inline-flex;
    align-items: center;
    gap: 10px;
    margin: 0;
    font-size: 16px;
    font-weight: 600;
    color: var(--gray-900);
  }
}

.card-desc {
  margin-top: 4px;
  font-size: 12px;
  color: var(--gray-500);
}

/* ============ 表格 ============ */
.paper-table {
  width: 100%;
  border-collapse: collapse;
  font-size: 12.5px;

  th, td {
    padding: 10px 14px;
    text-align: left;
    border-bottom: 1px solid var(--gray-100);
  }

  th {
    background: var(--gray-50);
    color: var(--gray-600);
    font-weight: 500;
    font-size: 11.5px;
    letter-spacing: 0.02em;
  }

  td { color: var(--gray-700); }

  .col-num { text-align: right; width: 80px; }
}

/* ============ 规则列表 ============ */
.rules-list {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}

.rule-chip {
  padding: 4px 10px;
  background: var(--gray-50);
  border: 1px solid var(--gray-100);
  border-radius: 4px;
  font-size: 11px;
  color: var(--gray-700);
}

/* ============ AI 总结 ============ */
.ai-card {
  background: linear-gradient(135deg, #FAFAFE, #F0EFFE);
}

.ai-summary {
  font-size: 13.5px;
  line-height: 1.85;
  color: var(--gray-800);
  white-space: pre-wrap;
}

/* ============ 页脚 ============ */
.paper-foot {
  padding: 20px 28px;
  font-size: 11px;
  color: var(--gray-400);
  letter-spacing: 0.05em;
  text-align: center;
  background: var(--gray-50);
}

/* ============ 打印样式 ============ */
@media print {
  .no-print { display: none !important; }

  .report-detail-page {
    background: #fff;
    padding: 0;
  }

  .report-paper {
    border: none;
    border-radius: 0;
    box-shadow: none;
  }

  .cover {
    page-break-after: always;
  }

  .no-break {
    page-break-inside: avoid;
  }

  .card {
    page-break-inside: avoid;
  }

  body, html, #app { background: #fff !important; }

  /* Element Plus 按钮等不打印 */
  .el-button { display: none; }
}

@page {
  size: A4;
  margin: 1.5cm;
}
</style>
