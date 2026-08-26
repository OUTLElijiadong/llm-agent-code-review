<template>
  <div class="report-detail-page">
    <!-- ============ 顶部操作栏（打印时隐藏）============ -->
    <header class="page-head no-print">
      <el-button link class="back-btn" @click="goBack(router, '/reports')">
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

    <!-- ============ T15 报告操作工具栏(模板类型 / 生成 / 预览 / 导出)============ -->
    <section v-if="report" class="report-toolbar no-print">
      <div class="toolbar-left">
        <span class="toolbar-label font-mono">模板类型</span>
        <el-select v-model="templateType" size="small" style="width: 200px">
          <el-option
            v-for="opt in templateTypeOptions"
            :key="opt.value"
            :label="opt.label"
            :value="opt.value"
          />
        </el-select>
      </div>
      <div class="toolbar-right">
        <el-button-group>
          <el-button
            size="small"
            :loading="generatingFormat === 'json'"
            @click="handleGenerate('json')"
          >生成 JSON</el-button>
          <el-button
            size="small"
            :loading="generatingFormat === 'html'"
            @click="handleGenerate('html')"
          >生成 HTML</el-button>
          <el-button
            size="small"
            :loading="generatingFormat === 'pdf'"
            @click="handleGenerate('pdf')"
          >生成 PDF</el-button>
          <el-button
            size="small"
            :loading="generatingFormat === 'word'"
            @click="handleGenerate('word')"
          >生成 Word</el-button>
        </el-button-group>
        <el-button
          ref="previewButtonRef"
          size="small"
          :loading="previewing"
          data-testid="report-preview-button"
          @click="handlePreview"
        >
          <el-icon><View /></el-icon>预览 HTML
        </el-button>
        <el-dropdown trigger="click" @command="handleExport">
          <el-button size="small" :loading="exportingFormat !== null">
            <el-icon><Download /></el-icon>导出报告
            <el-icon class="el-icon--right"><ArrowDown /></el-icon>
          </el-button>
          <template #dropdown>
            <el-dropdown-menu>
              <el-dropdown-item :command="'json'">JSON</el-dropdown-item>
              <el-dropdown-item :command="'html'">HTML</el-dropdown-item>
              <el-dropdown-item :command="'pdf'">PDF</el-dropdown-item>
              <el-dropdown-item :command="'word'">Word</el-dropdown-item>
            </el-dropdown-menu>
          </template>
        </el-dropdown>
      </div>
    </section>

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
              <span
                v-for="agent in agentReleases"
                :key="Number(agent.release_id)"
                class="cover-tag"
              >{{ agent.agent_name }} v{{ agent.agent_version }}</span>
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

        <p v-if="report.summary" class="cover-summary">{{ summaryText }}</p>
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

      <!-- ============ T15 v3 字段:CVSS 评分分布 ============ -->
      <section v-if="hasV3Data" class="card v3-card no-break" v-loading="issuesLoading">
        <header class="card-head">
          <h3 class="font-display">
            <span class="prism-mark sm"></span>CVSS 评分分布
          </h3>
          <p class="card-desc">基于 CVSS 3.x 基础评分的四档分布(共 {{ cvssTotal }} 个有评分的问题)</p>
        </header>
        <div v-if="cvssTotal > 0" class="cvss-rows">
          <div v-for="r in cvssRows" :key="r.key" class="cvss-row">
            <span class="cv-label">
              <span class="cv-dot" :style="{ background: r.color }"></span>{{ r.label }}
            </span>
            <div class="cv-bar">
              <div class="cv-fill" :style="{ width: `${r.percent}%`, background: r.color }"></div>
            </div>
            <span class="cv-val font-mono">{{ r.value }}</span>
          </div>
        </div>
        <EmptyState v-else description="暂无 CVSS 评分数据" compact />
      </section>

      <!-- ============ T15 v3 字段:合规映射概览 ============ -->
      <section v-if="hasV3Data" class="card v3-card no-break">
        <header class="card-head">
          <h3 class="font-display">
            <span class="prism-mark sm"></span>合规映射概览
          </h3>
          <p class="card-desc">ISO 27001 / GDPR / PCI DSS / HIPAA 四大标准命中条目数</p>
        </header>
        <div v-if="hasCompliance" class="compliance-grid">
          <el-statistic title="ISO 27001" :value="complianceStats.iso27001" />
          <el-statistic title="GDPR" :value="complianceStats.gdpr" />
          <el-statistic title="PCI DSS" :value="complianceStats.pci_dss" />
          <el-statistic title="HIPAA" :value="complianceStats.hipaa" />
        </div>
        <EmptyState v-else description="暂无合规映射数据" compact />
      </section>

      <!-- ============ T15 v3 字段:Top 10 高危漏洞 ============ -->
      <section v-if="top10Issues.length > 0" class="card v3-card no-break">
        <header class="card-head">
          <h3 class="font-display">
            <span class="prism-mark sm"></span>Top 10 高危漏洞
          </h3>
          <p class="card-desc">按 CVSS 评分降序排列,点击行查看详细修复方案</p>
        </header>
        <table class="paper-table top10-table">
          <thead>
            <tr>
              <th class="col-rank">#</th>
              <th>漏洞标题</th>
              <th class="col-num">CVSS</th>
              <th class="col-num">CWE</th>
              <th class="col-num">严重度</th>
              <th class="col-num">操作</th>
            </tr>
          </thead>
          <tbody>
            <tr
              v-for="(it, idx) in top10Issues"
              :key="it.id"
              :class="{ 'row-selected': selectedRemediationIssue?.id === it.id }"
              :aria-current="selectedRemediationIssue?.id === it.id ? 'true' : undefined"
              @click="selectRemediation(it.id)"
            >
              <td class="font-mono col-rank">{{ idx + 1 }}</td>
              <td class="issue-title">
                <span class="it-name">{{ it.title || it.issue_type }}</span>
                <span v-if="it.file_name" class="it-file font-mono">{{ it.file_name }}:{{ it.line_number ?? '?' }}</span>
              </td>
              <td class="font-mono col-num" :style="{ color: cvssSeverityColor(it.cvss_score), fontWeight: 600 }">
                {{ it.cvss_score?.toFixed(1) ?? '-' }}
              </td>
              <td class="font-mono col-num">{{ it.cwe || it.issue_type || '-' }}</td>
              <td class="col-num">
                <span
                  class="sev-tag"
                  :style="{ color: cvssSeverityColor(it.cvss_score), borderColor: cvssSeverityColor(it.cvss_score) }"
                >{{ cvssSeverityLabel(it.cvss_score) }}</span>
              </td>
              <td class="col-num">
                <el-button
                  link
                  type="primary"
                  size="small"
                  @click.stop="selectRemediation(it.id)"
                >查看修复方案</el-button>
              </td>
            </tr>
          </tbody>
        </table>
      </section>

      <!-- ============ T15 v3 字段:详细修复方案 ============ -->
      <section
        v-if="selectedRemediationIssue"
        id="remediation-detail"
        ref="remediationSectionRef"
        class="card v3-card no-break remediation-detail"
        data-testid="remediation-detail"
        aria-labelledby="remediation-heading"
      >
        <header class="card-head">
          <h3
            id="remediation-heading"
            ref="remediationHeadingRef"
            class="font-display remediation-heading"
            data-testid="remediation-heading"
            tabindex="-1"
          >
            <span class="prism-mark sm"></span>详细修复方案
          </h3>
          <p class="card-desc">
            {{ selectedRemediationIssue.title || selectedRemediationIssue.issue_type }}
            <span v-if="selectedRemediationIssue.cvss_score" class="card-desc-meta font-mono">
              · CVSS {{ selectedRemediationIssue.cvss_score.toFixed(1) }}
              <span v-if="selectedRemediationIssue.cvss_vector"> · {{ selectedRemediationIssue.cvss_vector }}</span>
            </span>
          </p>
        </header>
        <div class="remediation-block">
          <div v-if="selectedRemediationIssue.remediation" class="remediation-content">
            <pre>{{ selectedRemediationIssue.remediation }}</pre>
          </div>
          <div v-else class="remediation-empty">
            <EmptyState description="该漏洞暂无详细修复方案" compact />
          </div>
          <div v-if="selectedRemediationIssue.suggestion" class="remediation-suggestion">
            <div class="rs-label font-mono">建议</div>
            <pre>{{ selectedRemediationIssue.suggestion }}</pre>
          </div>
          <div v-if="selectedRemediationIssue.fixed_code" class="remediation-code">
            <div class="rs-label font-mono">修复代码</div>
            <pre>{{ selectedRemediationIssue.fixed_code }}</pre>
          </div>
        </div>
      </section>

      <!-- ============ AI 总结 ============ -->
      <section v-if="report.summary" class="card ai-card no-break">
        <header class="card-head">
          <h3 class="font-display">
            <span class="prism-mark sm"></span>AI 智能体总结建议
          </h3>
        </header>
        <div class="ai-summary" v-html="summaryHtml"></div>
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

    <el-dialog
      v-model="previewFallbackVisible"
      title="HTML 报告预览"
      width="min(1100px, 94vw)"
      top="5vh"
      append-to-body
      destroy-on-close
      class="report-preview-dialog no-print"
      @closed="handlePreviewFallbackClosed"
    >
      <iframe
        v-if="previewFallbackHtml"
        class="report-preview-frame"
        data-testid="report-preview-fallback"
        title="HTML 报告预览内容"
        :srcdoc="previewFallbackHtml"
        sandbox=""
        referrerpolicy="no-referrer"
      ></iframe>
    </el-dialog>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, nextTick, onBeforeUnmount, onMounted, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import { ArrowLeft, ArrowDown, Document, Download, Printer, View } from '@element-plus/icons-vue'
import dayjs from 'dayjs'
import type { EChartsCoreOption as EChartsOption } from 'echarts/core'
import BaseChart from '@/components/chart/BaseChart.vue'
import EmptyState from '@/components/common/EmptyState.vue'
import PrismLoading from '@/components/common/PrismLoading.vue'
import {
  getReportDetail,
  exportWord as apiExportWord,
  exportPdf as apiExportPdf,
  generateReport as apiGenerateReport,
  previewReport as apiPreviewReport,
  exportReport as apiExportReport,
} from '@/api/report'
import { getTaskIssues } from '@/api/review'
import type { ReportDetailOut, ReportIssue, ReportFormat, ReportTemplateType } from '@/types/report'
import { PRISM_SEVERITY_COLORS, PRISM_DIM_COLORS } from '@/components/chart/prismTheme'
import { reviewTypeLabel } from '@/constants/reviewType'
import { goBack } from '@/utils/navigation'
import { renderMarkdown, stripMarkdown } from '@/utils/markdown'

const route = useRoute()
const router = useRouter()
const taskId = Number(route.params.id)

const loading = ref(true)
const report = ref<ReportDetailOut | null>(null)
const exportingWord = ref(false)
const exportingPdf = ref(false)

// ===== T15 新增:报告生成 / 预览 / 导出状态 =====
/** 当前选择的模板类型(simple/detailed/compliance),影响生成/预览/导出 */
const templateType = ref<ReportTemplateType>('detailed')
/** 当前正在生成的格式(用于按钮 loading 态),null 表示无操作 */
const generatingFormat = ref<ReportFormat | null>(null)
/** 预览 HTML 报告的 loading 状态 */
const previewing = ref(false)
/** 弹窗被拦截时的页内安全预览。 */
const previewFallbackVisible = ref(false)
const previewFallbackHtml = ref('')
type PreviewButtonTarget = { $el?: HTMLElement; focus?: () => void }
const previewButtonRef = ref<PreviewButtonTarget | null>(null)
/** 统一管理新窗口 Blob URL，避免请求失败或页面卸载时泄漏。 */
const previewUrlTimers = new Map<string, ReturnType<typeof setTimeout>>()
/** 当前正在导出的格式(用于导出下拉按钮 loading 态),null 表示无操作 */
const exportingFormat = ref<ReportFormat | null>(null)
/** 报告关联的全部问题列表(含 v3 字段,用于 CVSS/合规/Top10/修复方案展示) */
const issues = ref<ReportIssue[]>([])
/** issues 是否正在加载 */
const issuesLoading = ref(false)

/** 模板类型下拉选项 */
const templateTypeOptions: Array<{ label: string; value: ReportTemplateType }> = [
  { label: '简洁模板 (Simple)', value: 'simple' },
  { label: '详细模板 (Detailed)', value: 'detailed' },
  { label: '合规模板 (Compliance)', value: 'compliance' },
]

const stats = computed<Record<string, unknown>>(() => report.value?.stats ?? {})

// AI 总结:封面用纯文本(剥离 markdown 符号),AI 总结卡片用消毒后的 markdown 渲染
const summaryText = computed(() => stripMarkdown((report.value?.summary as string) ?? ''))
const summaryHtml = computed(() => renderMarkdown((report.value?.summary as string) ?? ''))

const projectName = computed(() => (report.value?.project?.project_name as string) ?? '未命名项目')
const taskName    = computed(() => (report.value?.task?.task_name as string) ?? `任务 #${taskId}`)
const reviewType  = computed(() => reviewTypeLabel(report.value?.task?.review_type as string))
const language    = computed(() => (report.value?.project?.language as string) ?? '')
const totalFiles  = computed(() => Number(report.value?.task?.total_files ?? 0))
const durationMs  = computed(() => Number(report.value?.task?.duration_ms ?? 0))
const ruleCount   = computed(() => report.value?.rules_snapshot?.length ?? 0)
const agentReleases = computed<Array<Record<string, unknown>>>(() => (
  (report.value?.task?.agent_releases as Array<Record<string, unknown>>) ?? []
))

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
import { ElMessage } from 'element-plus/es/components/message/index'

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

// ===== T15 新增:加载 issues(含 v3 字段)=====

/**
 * 加载审查任务关联的全部问题(含 CVSS / 合规映射 / 修复方案 v3 字段)。
 * 后端报告详情不含 issues,需单独调用 review API 获取。
 */
async function loadIssues(): Promise<void> {
  issuesLoading.value = true
  try {
    // 一次取足够多,Top10 与分布统计需要全量数据
    const data = await getTaskIssues(taskId, { page: 1, page_size: 1000 })
    issues.value = (data.items as unknown as ReportIssue[]) ?? []
  } catch {
    // v3 字段展示为增强信息,加载失败不阻塞主报告
    issues.value = []
  } finally {
    issuesLoading.value = false
  }
}

// ===== T15 新增:v3 字段计算属性 =====

/** CVSS 评分四档分布(0-3.9 低 / 4-6.9 中 / 7-8.9 高 / 9-10 危急) */
const cvssDistribution = computed(() => {
  const dist = { low: 0, medium: 0, high: 0, critical: 0 }
  for (const it of issues.value) {
    const s = it.cvss_score
    if (typeof s !== 'number') continue
    if (s >= 9) dist.critical += 1
    else if (s >= 7) dist.high += 1
    else if (s >= 4) dist.medium += 1
    else dist.low += 1
  }
  return dist
})

/** CVSS 分布总数(仅统计有评分的问题) */
const cvssTotal = computed(() =>
  cvssDistribution.value.low + cvssDistribution.value.medium
  + cvssDistribution.value.high + cvssDistribution.value.critical,
)

/** CVSS 四档分布行(带颜色与百分比,用于进度条展示) */
const cvssRows = computed(() => {
  const total = Math.max(1, cvssTotal.value)
  return [
    {
      key: 'critical', label: '危急 (9.0-10.0)', value: cvssDistribution.value.critical,
      percent: (cvssDistribution.value.critical / total) * 100, color: '#DC4961',
    },
    {
      key: 'high', label: '高 (7.0-8.9)', value: cvssDistribution.value.high,
      percent: (cvssDistribution.value.high / total) * 100, color: '#E27C4A',
    },
    {
      key: 'medium', label: '中 (4.0-6.9)', value: cvssDistribution.value.medium,
      percent: (cvssDistribution.value.medium / total) * 100, color: '#D9A857',
    },
    {
      key: 'low', label: '低 (0-3.9)', value: cvssDistribution.value.low,
      percent: (cvssDistribution.value.low / total) * 100, color: '#4FB87A',
    },
  ]
})

/** 合规映射概览:四个标准各自的命中条目数 */
const complianceStats = computed(() => {
  const counts = { iso27001: 0, gdpr: 0, pci_dss: 0, hipaa: 0 }
  for (const it of issues.value) {
    const m = it.compliance_mapping
    if (!m) continue
    counts.iso27001 += Array.isArray(m.iso27001) ? m.iso27001.length : 0
    counts.gdpr += Array.isArray(m.gdpr) ? m.gdpr.length : 0
    counts.pci_dss += Array.isArray(m.pci_dss) ? m.pci_dss.length : 0
    counts.hipaa += Array.isArray(m.hipaa) ? m.hipaa.length : 0
  }
  return counts
})

/** 是否有任何合规映射数据 */
const hasCompliance = computed(() => {
  const s = complianceStats.value
  return s.iso27001 + s.gdpr + s.pci_dss + s.hipaa > 0
})

/** Top 10 高危漏洞列表(按 cvss_score 降序,无评分的排除) */
const top10Issues = computed<ReportIssue[]>(() => {
  return issues.value
    .filter((it) => typeof it.cvss_score === 'number')
    .slice()
    .sort((a, b) => (b.cvss_score ?? 0) - (a.cvss_score ?? 0))
    .slice(0, 10)
})

/** 选中展示修复方案的 issue(默认 Top1,可点击切换) */
const selectedRemediationId = ref<number | null>(null)

/** 当前展示修复方案的 issue 对象 */
const selectedRemediationIssue = computed<ReportIssue | null>(() => {
  if (!top10Issues.value.length) return null
  const id = selectedRemediationId.value
  if (id !== null) {
    return top10Issues.value.find((it) => it.id === id) ?? top10Issues.value[0]
  }
  return top10Issues.value[0]
})

/** 是否有 v3 字段数据(决定整个 v3 区块是否显示) */
const hasV3Data = computed(() => {
  return cvssTotal.value > 0 || hasCompliance.value || top10Issues.value.length > 0
})

// ===== T15 新增:报告生成 / 预览 / 导出方法 =====

/**
 * 根据 cvss_score 返回严重度标签文本。
 * @param score - CVSS 评分
 * @returns 严重度中文标签
 */
function cvssSeverityLabel(score?: number): string {
  if (typeof score !== 'number') return '-'
  if (score >= 9) return '危急'
  if (score >= 7) return '高'
  if (score >= 4) return '中'
  return '低'
}

/**
 * 根据 cvss_score 返回严重度颜色(与 cvssRows 颜色一致)。
 * @param score - CVSS 评分
 * @returns 颜色十六进制字符串
 */
function cvssSeverityColor(score?: number): string {
  if (typeof score !== 'number') return '#909399'
  if (score >= 9) return '#DC4961'
  if (score >= 7) return '#E27C4A'
  if (score >= 4) return '#D9A857'
  return '#4FB87A'
}

/**
 * 生成报告(JSON/HTML/PDF/Word)。
 * JSON/HTML 返回字符串(触发下载为文本文件),PDF/Word 返回 Blob 触发下载。
 * @param format - 报告格式
 */
async function handleGenerate(format: ReportFormat): Promise<void> {
  generatingFormat.value = format
  try {
    const result = await apiGenerateReport(taskId, format, templateType.value)
    if (result instanceof Blob) {
      // pdf / word
      const ext = format === 'pdf' ? 'pdf' : 'docx'
      downloadBlob(result, `review_report_${taskId}.${ext}`)
      ElMessage.success(`${format.toUpperCase()} 报告生成成功`)
    } else {
      // json / html 返回字符串
      const ext = format === 'json' ? 'json' : 'html'
      const blob = new Blob([result], {
        type: format === 'json' ? 'application/json' : 'text/html',
      })
      downloadBlob(blob, `review_report_${taskId}.${ext}`)
      ElMessage.success(`${format.toUpperCase()} 报告生成成功`)
    }
  } catch {
    ElMessage.error(`${format.toUpperCase()} 报告生成失败`)
  } finally {
    generatingFormat.value = null
  }
}

/**
 * 释放预览 Blob URL 及对应计时器。
 */
function releasePreviewUrl(url: string): void {
  const timer = previewUrlTimers.get(url)
  if (timer) clearTimeout(timer)
  previewUrlTimers.delete(url)
  window.URL.revokeObjectURL(url)
}

/** 在新窗口完成加载后延迟释放 Blob URL。 */
function schedulePreviewUrlRelease(url: string): void {
  const timer = setTimeout(() => releasePreviewUrl(url), 60_000)
  previewUrlTimers.set(url, timer)
}

/** 清理页内预览内容，不在对话框关闭后长期保留报告 HTML。 */
function clearPreviewFallback(): void {
  previewFallbackHtml.value = ''
}

/** 关闭页内预览后把键盘焦点还给触发按钮。 */
async function handlePreviewFallbackClosed(): Promise<void> {
  clearPreviewFallback()
  await nextTick()
  const target = previewButtonRef.value
  const element = target?.$el ?? target
  element?.focus?.()
}

/** 同步预开与当前页面断开关系的窗口；安全属性设置失败时改走页内预览。 */
function preopenPreviewWindow(): Window | null {
  let candidate: Window | null
  try {
    candidate = window.open('about:blank', '_blank')
  } catch {
    return null
  }
  if (!candidate) return null

  try {
    candidate.opener = null
  } catch {
    try { candidate.close() } catch { /* 页内预览仍可继续 */ }
    return null
  }

  try {
    candidate.document.title = '正在准备 HTML 报告'
    candidate.document.body.textContent = '正在准备 HTML 报告…'
  } catch {
    // 部分浏览器在断开 opener 后不允许读写预开页，不影响后续导航。
  }
  return candidate
}

/**
 * 预览 HTML 报告。
 * 用户点击时同步预开窗口，请求完成后再导航到 Blob URL；
 * 弹窗被拦截或在等待期间被关闭时，改用 sandbox iframe 页内预览。
 */
async function handlePreview(): Promise<void> {
  const popup = preopenPreviewWindow()

  previewing.value = true
  previewFallbackVisible.value = false
  try {
    const html = await apiPreviewReport(taskId, templateType.value)
    if (popup && !popup.closed) {
      const blob = new Blob([html], { type: 'text/html;charset=utf-8' })
      const url = window.URL.createObjectURL(blob)
      try {
        popup.location.replace(url)
        schedulePreviewUrlRelease(url)
        ElMessage.success('HTML 报告已在新窗口打开')
        return
      } catch {
        releasePreviewUrl(url)
        try { popup.close() } catch { /* 页内预览仍可继续 */ }
      }
    }

    previewFallbackHtml.value = html
    previewFallbackVisible.value = true
    await nextTick()
    ElMessage.success('HTML 报告已在当前页面打开')
  } catch {
    if (popup && !popup.closed) {
      try { popup.close() } catch { /* 忽略浏览器关闭窗口限制 */ }
    }
    ElMessage.error('预览报告失败')
  } finally {
    previewing.value = false
  }
}

/**
 * 导出报告(调用 exportReport 接口下载文件)。
 * @param format - 导出格式
 */
async function handleExport(format: ReportFormat): Promise<void> {
  exportingFormat.value = format
  try {
    const blob = await apiExportReport(taskId, format, templateType.value)
    const extMap: Record<ReportFormat, string> = {
      json: 'json', html: 'html', pdf: 'pdf', word: 'docx',
    }
    downloadBlob(blob, `review_report_${taskId}.${extMap[format]}`)
    ElMessage.success(`${format.toUpperCase()} 报告导出成功`)
  } catch {
    ElMessage.error(`${format.toUpperCase()} 报告导出失败`)
  } finally {
    exportingFormat.value = null
  }
}

/**
 * 选中某个 issue 展示其修复方案。
 * @param id - issue ID
 */
const remediationSectionRef = ref<HTMLElement | null>(null)
const remediationHeadingRef = ref<HTMLElement | null>(null)

async function selectRemediation(id: number): Promise<void> {
  selectedRemediationId.value = id
  await nextTick()
  const section = remediationSectionRef.value
  const heading = remediationHeadingRef.value
  if (!section) return
  const reducedMotion = window.matchMedia?.('(prefers-reduced-motion: reduce)').matches ?? false
  section.scrollIntoView({ behavior: reducedMotion ? 'auto' : 'smooth', block: 'start' })
  if (heading) {
    try {
      heading.focus({ preventScroll: true })
    } catch {
      heading.focus()
    }
  }
}

onMounted(() => {
  loadReport()
  loadIssues()
  // 报告列表"生成报告"按钮跳转携带 ?generate=1,此处消费该 query 自动触发生成
  if (route.query.generate === '1') {
    // 消费后立即清掉 query,避免刷新/前进后退时重复触发生成
    router.replace({ query: {} })
    handleGenerate('html')
  }
})

onBeforeUnmount(() => {
  for (const url of [...previewUrlTimers.keys()]) releasePreviewUrl(url)
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
  letter-spacing: 0;
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
  letter-spacing: 0;
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
  letter-spacing: 0;
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

  :deep(p) { margin: 0 0 10px; }
  :deep(p:last-child) { margin-bottom: 0; }
  :deep(h1), :deep(h2), :deep(h3), :deep(h4) {
    margin: 14px 0 8px;
    font-weight: 600;
    color: var(--gray-900);
  }
  :deep(h1), :deep(h2) { font-size: 16px; }
  :deep(h3), :deep(h4) { font-size: 14px; }
  :deep(ul), :deep(ol) { padding-left: 20px; margin: 6px 0; }
  :deep(li) { margin: 3px 0; }
  :deep(strong) { font-weight: 600; color: var(--gray-900); }
  :deep(code) {
    background: var(--gray-100);
    padding: 1px 5px;
    border-radius: 4px;
    font-size: 12px;
  }
  :deep(pre) {
    background: var(--gray-100);
    padding: 10px 12px;
    border-radius: 8px;
    overflow-x: auto;
    margin: 8px 0;
  }
  :deep(pre code) { background: transparent; padding: 0; }
}

/* ============ T15 报告操作工具栏 ============ */
.report-toolbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  padding: 12px 16px;
  background: #fff;
  border: 1px solid var(--gray-100);
  border-radius: 12px;
  box-shadow: var(--shadow-1);
  flex-wrap: wrap;
}

.toolbar-left {
  display: flex;
  align-items: center;
  gap: 10px;
}

.toolbar-label {
  font-size: 12px;
  color: var(--gray-600);
  letter-spacing: 0.04em;
}

.toolbar-right {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}

/* ============ T15 v3 字段卡片 ============ */
.v3-card {
  background: linear-gradient(180deg, #FAFCFE 0%, #FFFFFF 100%);
}

.card-desc-meta {
  margin-left: 6px;
  font-size: 11.5px;
  color: var(--gray-500);
}

/* ============ CVSS 评分分布 ============ */
.cvss-rows {
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.cvss-row {
  display: grid;
  grid-template-columns: 140px 1fr 48px;
  align-items: center;
  gap: 12px;
  font-size: 12.5px;
}

.cv-label {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  color: var(--gray-700);
}

.cv-dot {
  width: 10px;
  height: 10px;
  border-radius: 50%;
  flex-shrink: 0;
}

.cv-bar {
  height: 10px;
  background: var(--gray-100);
  border-radius: 5px;
  overflow: hidden;
}

.cv-fill {
  height: 100%;
  border-radius: 5px;
  transition: width 0.6s ease;
}

.cv-val {
  text-align: right;
  font-weight: 600;
  color: var(--gray-800);
}

/* ============ 合规映射概览 ============ */
.compliance-grid {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: 16px;
}

@media (max-width: 900px) {
  .compliance-grid {
    grid-template-columns: repeat(2, 1fr);
  }
}

/* ============ Top 10 高危漏洞 ============ */
.top10-table {
  .col-rank {
    width: 48px;
    text-align: center;
    color: var(--gray-500);
  }

  .issue-title {
    display: flex;
    flex-direction: column;
    gap: 2px;
  }

  .it-name {
    font-weight: 500;
    color: var(--gray-800);
  }

  .it-file {
    font-size: 11px;
    color: var(--gray-500);
  }

  tbody tr {
    cursor: pointer;
    transition: background 0.2s;

    &:hover {
      background: var(--gray-50);
    }
  }

  .row-selected {
    background: rgba(91, 88, 232, 0.06) !important;
  }
}

.sev-tag {
  display: inline-block;
  padding: 2px 10px;
  border: 1px solid;
  border-radius: 999px;
  font-size: 11px;
  font-weight: 600;
  line-height: 1.6;
}

/* ============ 详细修复方案 ============ */
.remediation-detail {
  scroll-margin-top: 24px;
}

.remediation-heading:focus-visible {
  outline: 2px solid var(--brand-500);
  outline-offset: 4px;
  border-radius: 2px;
}

.remediation-block {
  display: flex;
  flex-direction: column;
  gap: 16px;
}

.remediation-content,
.remediation-suggestion,
.remediation-code {
  pre {
    margin: 8px 0 0;
    padding: 14px 16px;
    background: var(--gray-50);
    border: 1px solid var(--gray-100);
    border-radius: 8px;
    font-family: var(--font-mono, 'SFMono-Regular', Consolas, monospace);
    font-size: 12.5px;
    line-height: 1.7;
    color: var(--gray-800);
    white-space: pre-wrap;
    word-break: break-word;
    overflow-x: auto;
  }
}

.remediation-content pre {
  background: #FFFBF5;
  border-color: #F5E8D0;
}

.remediation-code pre {
  background: #F6F8FA;
  border-color: var(--gray-100);
}

.rs-label {
  font-size: 11px;
  letter-spacing: 0.06em;
  color: var(--gray-500);
  text-transform: uppercase;
}

.remediation-empty {
  padding: 12px 0;
}

:global(.report-preview-dialog .el-dialog__body) {
  padding: 0;
}

.report-preview-frame {
  display: block;
  width: 100%;
  height: min(72vh, 820px);
  border: 0;
  background: #fff;
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
