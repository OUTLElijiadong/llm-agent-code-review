<template>
  <div class="review-detail-page">
    <!-- ============ 页面加载中 ============ -->
    <PrismLoading
      v-if="pageLoading"
      label="正在加载审查详情"
      sublabel="正在请求审查数据与问题列表"
      overlay
    />

    <!-- ============ 加载失败 ============ -->
    <div v-else-if="pageError" class="page-error">
      <EmptyState :description="pageError" />
      <el-button type="primary" :icon="RefreshRight" @click="loadAllData" style="margin-top: 12px">
        重新加载
      </el-button>
    </div>

    <!-- ============ 正常内容 ============ -->
    <template v-else>
    <!-- ============ AI 进度光带（运行中显示）============ -->
    <AiLoadingBeam
      v-if="task?.status === 'running'"
      class="ai-ribbon"
      :title="`正在审查 ${task.task_name || '任务 #' + taskId}`"
      :status="`DeepSeek V4 · 已处理 ${task.processed_files ?? 0}/${task.total_files ?? 0} 文件`"
    />

    <!-- ============ 顶部摘要条 ============ -->
    <header class="task-head">
      <el-button link class="back-btn" @click="$router.back()">
        <el-icon><ArrowLeft /></el-icon>返回
      </el-button>

      <div class="head-title">
        <h1 class="font-display">{{ task?.task_name || `审查任务 #${taskId}` }}</h1>
        <div class="head-meta font-mono">
          <span>{{ reviewTypeLabel(task?.review_type) }}</span>
          <span class="dot">·</span>
          <span>{{ task?.model_name || 'DeepSeek V4' }}</span>
          <span class="dot">·</span>
          <span>{{ formatDuration(Number(task?.duration_ms ?? 0)) }}</span>
        </div>
        <div class="trace-meta">
          <span class="font-mono">任务 #{{ taskId }}</span>
          <el-button v-if="task?.project_id" link type="primary" @click="goProject(task.project_id)">
            {{ task.project_name || `项目 #${task.project_id}` }}
          </el-button>
          <el-button link type="primary" @click="goReport(taskId)">报告 #{{ taskId }}</el-button>
        </div>
      </div>

      <div class="head-score">
        <div class="score-orb" :style="{ background: scoreGradient(animatedScore) }">
          <span class="score-val font-display">{{ animatedScore }}</span>
          <span class="score-out font-mono">/100</span>
        </div>
        <div class="score-meta">
          <div class="score-label">代码质量</div>
          <div class="score-status" :style="{ color: scoreFlatColor(animatedScore) }">{{ riskLevel }}</div>
        </div>
      </div>

      <div class="head-tally">
        <div class="tally-item">
          <span class="t-val font-display" :style="{ color: 'var(--sev-severe)' }">{{ task?.severe_issues ?? 0 }}</span>
          <span class="t-label">危急</span>
        </div>
        <div class="tally-item">
          <span class="t-val font-display" :style="{ color: 'var(--sev-high)' }">{{ task?.high_issues ?? 0 }}</span>
          <span class="t-label">高</span>
        </div>
        <div class="tally-item">
          <span class="t-val font-display" :style="{ color: 'var(--sev-medium)' }">{{ task?.medium_issues ?? 0 }}</span>
          <span class="t-label">中</span>
        </div>
        <div class="tally-item">
          <span class="t-val font-display" :style="{ color: 'var(--sev-low)' }">{{ task?.low_issues ?? 0 }}</span>
          <span class="t-label">低</span>
        </div>
        <div class="tally-divider"></div>
        <div class="tally-item">
          <span class="t-val font-display">{{ task?.total_issues ?? 0 }}</span>
          <span class="t-label">总计</span>
        </div>
      </div>

      <div class="head-actions">
        <el-button
          v-if="task?.severe_issues || task?.high_issues || task?.medium_issues"
          :icon="Lock"
          size="small"
          type="danger"
          plain
          @click="securityScanVisible = true"
        >
          🛡 安全复审
        </el-button>
        <el-button
          v-if="task?.total_issues && task.total_issues > 0"
          :icon="MagicStick"
          size="small"
          type="primary"
          plain
          @click="aiPromptVisible = true"
        >
          AI 修复包
        </el-button>
        <span class="status-pill" :class="`s-${task?.status ?? 'pending'}`">
          <span class="pill-dot"></span>{{ statusLabel(task?.status ?? '') }}
        </span>
      </div>
    </header>

    <AiPromptModal
      v-model="aiPromptVisible"
      source="task"
      :ref-id="taskId"
    />

    <SecurityScanModal
      v-model="securityScanVisible"
      source="task"
      :ref-id="taskId"
      :auto-start="true"
    />

    <!-- ============ 三栏工作台 ============ -->
    <section class="workbench">
      <!-- 左：文件树 -->
      <aside class="pane pane-files">
        <header class="pane-head">
          <span class="font-display">文件列表</span>
          <span class="font-mono pane-count">{{ fileList.length }}</span>
        </header>
        <div class="pane-body">
          <div
            v-for="f in fileList"
            :key="f.file_id"
            class="file-row"
            :class="{ active: currentFileId === f.file_id }"
            @click="onFilePick(f.file_id)"
          >
            <span class="file-ico font-mono">{{ fileGlyph(f.file_name) }}</span>
            <span class="file-name">{{ f.file_name }}</span>
            <span v-if="fileIssueCount(f.file_id) > 0" class="file-issues" :style="{ color: scoreFlatColor(80 - fileIssueCount(f.file_id) * 5) }">
              {{ fileIssueCount(f.file_id) }}
            </span>
            <el-button
              v-if="f.project_id"
              class="file-open"
              link
              size="small"
              type="primary"
              @click.stop="goFile(f.project_id, f.file_id)"
            >
              打开
            </el-button>
          </div>
          <EmptyState v-if="fileList.length === 0" description="暂无审查文件" compact />
        </div>
      </aside>

      <!-- 中：Monaco 代码 -->
      <main class="pane pane-code">
        <header class="pane-head pane-head-code">
          <div class="code-file font-mono">
            <span class="file-ico">{{ fileGlyph(currentFileName) }}</span>
            <span>{{ currentFileName || '请选择文件' }}</span>
            <span v-if="currentLanguage" class="lang-chip font-mono">{{ currentLanguage }}</span>
          </div>
          <div class="code-tools">
            <el-button-group>
              <el-button size="small" :icon="ZoomIn" />
              <el-button size="small" :icon="ZoomOut" />
            </el-button-group>
          </div>
        </header>
        <div class="pane-body code-body">
          <div v-if="loadingCode" class="loading-box"><el-skeleton :rows="14" animated /></div>
          <EmptyState v-else-if="!codeContent" description="请从左侧选择一个文件查看代码" />
          <CodeViewer
            v-else
            ref="codeViewerRef"
            :code="codeContent"
            :language="currentLanguage"
            height="100%"
            :highlight-lines="highlightLines"
          />
        </div>
      </main>

      <!-- 右：问题列表 -->
      <aside class="pane pane-issues">
        <header class="pane-head">
          <span class="font-display">问题列表</span>
          <span class="font-mono pane-count">{{ issueTotal }}</span>
        </header>

        <!-- 严重度过滤 -->
        <div class="filter-chips">
          <button
            v-for="sev in severityChips"
            :key="sev.key"
            class="chip"
            :class="{ active: filter.severity === sev.value }"
            @click="toggleSeverity(sev.value)"
          >
            <span class="chip-dot" :style="{ background: sev.color }"></span>
            <span>{{ sev.label }}</span>
            <span class="chip-num font-mono">{{ sev.count }}</span>
          </button>
        </div>

        <!-- 维度过滤 -->
        <div class="filter-dims">
          <button
            v-for="d in dimChips"
            :key="d.key"
            class="dim-chip"
            :class="{ active: filter.issue_type === d.key }"
            :style="{ '--dim-color': d.color }"
            @click="toggleDim(d.key)"
          >
            <span class="dim-dot" :style="{ background: d.color }"></span>{{ d.label }}
          </button>
        </div>

        <div class="pane-body issue-body">
          <div
            v-for="issue in issues"
            :key="issue.id"
            class="issue-row"
            :class="{ active: selectedIssue?.id === issue.id }"
            @click="onIssueClick(issue)"
          >
            <span class="sev" :class="`sev-${severityClass(issue.severity)}`">{{ severityDisplayLabel(issue.severity) }}</span>
            <div class="issue-meta">
              <div class="issue-title">{{ issue.title || issue.description.slice(0, 40) || '未命名问题' }}</div>
              <div class="issue-sub font-mono">
                {{ issue.file_name || '未知文件' }} · L{{ issue.line_number ?? '?' }}
                <span class="dim-dot" :style="{ background: dimColor(issue.issue_type) }"></span>
                {{ issueTypeLabel(issue.issue_type) }}
              </div>
            </div>
            <span class="issue-status font-mono" :class="`is-${issue.status}`">{{ issueStatusLabel(issue.status) }}</span>
          </div>
          <EmptyState v-if="issues.length === 0" description="没有匹配的问题" compact />
        </div>

        <footer v-if="issueTotal > issuePageSize" class="pane-foot">
          <el-pagination
            v-model:current-page="issuePage"
            :page-size="issuePageSize"
            :total="issueTotal"
            layout="prev, pager, next"
            small
            @current-change="onIssuePage"
          />
        </footer>
      </aside>
    </section>

    <IssueDetailDrawer
      v-model="drawerVisible"
      :issue="selectedIssue"
    />
    </template>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted, onUnmounted, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ArrowLeft, Lock, MagicStick, ZoomIn, ZoomOut, RefreshRight } from '@element-plus/icons-vue'
import { ElMessage } from 'element-plus'
import CodeViewer from '@/components/code/CodeViewer.vue'
import IssueDetailDrawer from '@/components/issue/IssueDetailDrawer.vue'
import AiPromptModal from '@/components/issue/AiPromptModal.vue'
import SecurityScanModal from '@/components/security/SecurityScanModal.vue'
import EmptyState from '@/components/common/EmptyState.vue'
import AiLoadingBeam from '@/components/common/AiLoadingBeam.vue'
import PrismLoading from '@/components/common/PrismLoading.vue'
import { getReviewTaskDetail, getTaskIssues } from '@/api/review'
import { getDetail as getCodeFileDetail } from '@/api/codeFile'
import type { TaskDetailOut, TaskFileOut, IssueOut } from '@/types/review'
import { PRISM_SEVERITY_COLORS } from '@/components/chart/prismTheme'
import { SEVERITY_OPTIONS, severityClass, severityDisplayLabel } from '@/constants/severity'
import { DIM_META, normalizeDimKey, dimColor as resolveDimColor, dimLabel as resolveDimLabel } from '@/constants/dim'
import { reviewTypeLabel } from '@/constants/reviewType'

const route = useRoute()
const router = useRouter()
const taskId = Number(route.params.id)

type TraceFileItem = Partial<TaskFileOut> & {
  file_id: number
  file_name: string
  project_id?: number
}

const pageLoading = ref(true)
const pageError = ref('')
const task = ref<TaskDetailOut | null>(null)
const animatedScore = ref(0)

function animateScore(target: number): void {
  const duration = 1200 // 动画时长 (ms)
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

watch(() => task.value?.score, (newVal) => {
  if (typeof newVal === 'number') {
    animateScore(newVal)
  }
})
const issues = ref<IssueOut[]>([])
const issueTotal = ref(0)
const issuePage = ref(1)
const issuePageSize = ref(50)
const filter = ref<{ severity: string; issue_type: string; status: string }>({
  severity: '',
  issue_type: '',
  status: '',
})

const currentFileId = ref<number | null>(null)
const currentFileName = ref('')
const currentLanguage = ref('text')
const codeContent = ref('')
const loadingCode = ref(false)
const fileList = ref<TraceFileItem[]>([])

const drawerVisible = ref(false)
const selectedIssue = ref<IssueOut | null>(null)
const codeViewerRef = ref<InstanceType<typeof CodeViewer> | null>(null)
const aiPromptVisible = ref(false)
const securityScanVisible = ref(false)

const statusLabels: Record<string, string> = {
  pending: '待处理',
  running: '运行中',
  success: '已完成',
  failed: '失败',
  cancelled: '已取消',
}

const dimMeta = DIM_META.map((d) => ({ key: d.key, label: d.name, color: d.color }))

const issueStatusMap: Record<string, string> = {
  unfixed: '未修复', fixed: '已修复', ignored: '已忽略', pending_review: '待审核',
}

const severityChips = computed(() => {
  const countMap = {
    严重: task.value?.severe_issues ?? 0,
    高: task.value?.high_issues ?? 0,
    中: task.value?.medium_issues ?? 0,
    低: task.value?.low_issues ?? 0,
  }
  return SEVERITY_OPTIONS.map((option) => ({
    ...option,
    color: PRISM_SEVERITY_COLORS[option.key],
    count: countMap[option.value],
  }))
})

const dimChips = computed(() => dimMeta)

const riskLevel = computed(() => {
  const s = task.value?.score ?? 0
  if (s >= 90) return '优秀 · 可发布'
  if (s >= 80) return '良好 · 关注潜在风险'
  if (s >= 70) return '一般 · 建议修复'
  if (s >= 60) return '及格 · 需要重构'
  return '风险 · 必须处理'
})

/**
 * 获取审查任务状态的中文文案
 * @param s - 任务状态枚举值
 * @returns 中文状态文案
 */
function statusLabel(s: string): string {
  return statusLabels[s] ?? s
}

/**
 * 获取问题类型的中文文案 (v2.0: 使用 normalizeDimKey 兼容多种枚举写法)
 * @param s - 问题类型枚举值
 * @returns 中文问题类型文案
 */
function issueTypeLabel(s: string): string {
  return resolveDimLabel(s)
}

/**
 * 获取问题类型对应的主题色 (v2.0: 通过归一化兜底)
 * @param s - 问题类型枚举值
 * @returns CSS 颜色值
 */
function dimColor(s: string): string {
  if (normalizeDimKey(s)) return resolveDimColor(s)
  return 'var(--gray-400)'
}

/**
 * 获取问题处理状态的中文文案
 * @param s - 问题处理状态枚举值
 * @returns 中文状态文案
 */
function issueStatusLabel(s: string): string {
  return issueStatusMap[s] ?? s
}

function fileGlyph(name?: string): string {
  if (!name) return '·'
  const ext = name.split('.').pop()?.toLowerCase() ?? ''
  return ext.slice(0, 3).toUpperCase() || '·'
}

function fileIssueCount(fileId: number): number {
  return issues.value.filter((i) => i.file_id === fileId).length
}

function formatDuration(ms: number): string {
  if (!ms) return '—'
  if (ms < 1000) return `${ms}ms`
  if (ms < 60000) return `${(ms / 1000).toFixed(1)}s`
  return `${Math.floor(ms / 60000)}m${Math.round((ms % 60000) / 1000)}s`
}

function scoreGradient(score: number): string {
  if (score >= 85) return 'linear-gradient(135deg, #4FB87A, #2BBFB9)'
  if (score >= 70) return 'linear-gradient(135deg, #D9A857, #4FB87A)'
  if (score >= 60) return 'linear-gradient(135deg, #E27C4A, #D9A857)'
  return 'linear-gradient(135deg, #DC4961, #E27C4A)'
}

function scoreFlatColor(score: number): string {
  if (score >= 85) return 'var(--status-fixed)'
  if (score >= 70) return 'var(--sev-medium)'
  if (score >= 60) return 'var(--sev-high)'
  return 'var(--sev-severe)'
}

const highlightLines = computed(() => {
  if (!selectedIssue.value || selectedIssue.value.file_id !== currentFileId.value) return []
  const start = selectedIssue.value.line_number ?? 0
  const end = selectedIssue.value.end_line ?? start
  if (!start) return []
  const lines: number[] = []
  for (let i = start; i <= end; i++) lines.push(i)
  return lines
})

async function loadTaskDetail(silent = false) {
  try {
    task.value = await getReviewTaskDetail(taskId)
    fileList.value = (task.value.files ?? []).map((item) => ({ ...item }))
  } catch {
    if (!silent) pageError.value = '加载审查任务详情失败'
  }
}

async function doLoadIssues(silent = false) {
  try {
    const params: Record<string, unknown> = {
      page: issuePage.value,
      page_size: issuePageSize.value,
      ...Object.fromEntries(Object.entries(filter.value).filter(([, v]) => v)),
    }
    if (!params.status) params.status = 'all'
    const data = await getTaskIssues(taskId, params)
    issues.value = data.items
    issueTotal.value = data.total

    const fileSet = new Map<number, TraceFileItem>()
    fileList.value.forEach((item) => fileSet.set(item.file_id, item))
    data.items.forEach((item) => {
      if (item.file_id && item.file_name && !fileSet.has(item.file_id)) {
        fileSet.set(item.file_id, {
          file_id: item.file_id,
          file_name: item.file_name,
          project_id: task.value?.project_id,
        })
      }
    })
    fileList.value = Array.from(fileSet.values())
  } catch {
    if (!silent) ElMessage.error('加载问题列表失败')
  }
}

async function loadAllData() {
  pageLoading.value = true
  pageError.value = ''
  try {
    await Promise.all([loadTaskDetail(), doLoadIssues()])
  } catch {
    if (!pageError.value) pageError.value = '加载审查详情失败'
  } finally {
    pageLoading.value = false
    schedulePollIfRunning()
  }
}

// ── 运行中任务轮询: 后端异步执行审查,前端定时刷新进度直至终态 ──
const POLL_INTERVAL = 3000
let pollTimer: ReturnType<typeof setTimeout> | null = null

function stopPolling() {
  if (pollTimer) {
    clearTimeout(pollTimer)
    pollTimer = null
  }
}

function schedulePollIfRunning() {
  stopPolling()
  if (task.value?.status !== 'running') return
  pollTimer = setTimeout(async () => {
    // 静默刷新,避免轮询期间反复弹出错误提示或闪烁整页骨架屏
    await loadTaskDetail(true)
    await doLoadIssues(true)
    schedulePollIfRunning()
  }, POLL_INTERVAL)
}

async function loadFileCode(fileId: number) {
  loadingCode.value = true
  try {
    const file = await getCodeFileDetail(fileId)
    codeContent.value = file.content
    currentLanguage.value = file.language
    currentFileName.value = file.file_name
  } finally {
    loadingCode.value = false
  }
}

function onFilePick(fileId: number) {
  if (currentFileId.value === fileId) return
  currentFileId.value = fileId
  loadFileCode(fileId)
}

function onIssueClick(issue: IssueOut) {
  selectedIssue.value = issue
  if (issue.file_id && currentFileId.value !== issue.file_id) {
    currentFileId.value = issue.file_id
    loadFileCode(issue.file_id).then(() => {
      if (issue.line_number) {
        setTimeout(() => codeViewerRef.value?.revealLine(issue.line_number!), 300)
      }
    })
  } else if (issue.line_number) {
    codeViewerRef.value?.revealLine(issue.line_number)
  }
  drawerVisible.value = true
}

/**
 * 切换严重度筛选条件，并使用后端认可的中文枚举值请求数据
 * @param value - 后端严重度枚举值
 * @returns void
 */
function toggleSeverity(value: string): void {
  filter.value.severity = filter.value.severity === value ? '' : value
  issuePage.value = 1
  doLoadIssues()
}

function toggleDim(key: string) {
  filter.value.issue_type = filter.value.issue_type === key ? '' : key
  issuePage.value = 1
  doLoadIssues()
}

function onIssuePage(p: number) {
  issuePage.value = p
  doLoadIssues()
}

/**
 * 跳转到项目详情页。
 * @param projectId - 项目 ID
 * @returns void
 */
function goProject(projectId: number): void {
  router.push(`/projects/${projectId}`)
}

/**
 * 跳转到报告详情页。
 * @param id - 审查任务/报告 ID
 * @returns void
 */
function goReport(id: number): void {
  router.push(`/reports/${id}`)
}

/**
 * 跳转到代码文件编辑页。
 * @param projectId - 文件所属项目 ID
 * @param fileId - 代码文件 ID
 * @returns void
 */
function goFile(projectId: number, fileId: number): void {
  router.push(`/code/${projectId}/file/${fileId}`)
}

onMounted(() => {
  loadAllData()
})

onUnmounted(() => {
  stopPolling()
})
</script>

<style scoped lang="scss">
.review-detail-page {
  display: flex;
  flex-direction: column;
  gap: 14px;
  height: 100%;
  min-height: 0;
}

.page-error {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  min-height: 320px;
}

/* ============ AI 进度光带 ============ */
.ai-ribbon {
  margin-bottom: 0;
}

/* ============ 顶部摘要条 ============ */
.task-head {
  display: grid;
  grid-template-columns: auto 1fr auto auto auto;
  gap: 18px;
  align-items: center;
  padding: 16px 20px;
  background: #fff;
  border: 1px solid var(--gray-100);
  border-radius: 12px;
  box-shadow: var(--shadow-1);
}

.back-btn {
  font-size: 13px;
}

.head-title {
  min-width: 0;

  h1 {
    margin: 0;
    font-size: 18px;
    font-weight: 600;
    color: var(--gray-900);
    letter-spacing: 0;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .head-meta {
    display: flex;
    align-items: center;
    gap: 6px;
    margin-top: 4px;
    font-size: 11.5px;
    color: var(--gray-500);

    .dot { opacity: 0.5; }
  }

  .trace-meta {
    display: flex;
    align-items: center;
    gap: 8px;
    margin-top: 4px;
    color: var(--gray-500);
    font-size: 12px;
  }
}

.head-score {
  display: flex;
  align-items: center;
  gap: 12px;
}

.score-orb {
  position: relative;
  width: 58px;
  height: 58px;
  border-radius: 50%;
  display: flex;
  align-items: baseline;
  justify-content: center;
  color: #fff;
  font-weight: 600;
  box-shadow: 0 8px 20px -8px rgba(91, 88, 232, 0.4);
}

.score-val {
  font-size: 22px;
  line-height: 1;
}

.score-out {
  font-size: 10px;
  opacity: 0.85;
}

.score-meta {
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.score-label {
  font-size: 11px;
  color: var(--gray-500);
  font-family: var(--font-mono);
}

.score-status {
  font-size: 13px;
  font-weight: 600;
  font-family: var(--font-display);
}

.head-tally {
  display: inline-flex;
  align-items: center;
  gap: 14px;
  padding: 10px 14px;
  background: var(--gray-50);
  border: 1px solid var(--gray-100);
  border-radius: 10px;
}

.tally-item {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 2px;
}

.t-val {
  font-size: 18px;
  font-weight: 600;
  line-height: 1;
}

.t-label {
  font-size: 10.5px;
  color: var(--gray-500);
  font-family: var(--font-mono);
}

.tally-divider {
  width: 1px;
  height: 26px;
  background: var(--gray-200);
}

.head-actions {
  display: flex;
  align-items: center;
  gap: 8px;
}

.status-pill {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  height: 24px;
  padding: 0 12px;
  border-radius: 999px;
  font-size: 11.5px;
  font-weight: 500;

  .pill-dot { width: 6px; height: 6px; border-radius: 50%; }

  &.s-running {
    color: var(--brand-600); background: var(--brand-50);
    .pill-dot { background: var(--brand-500); box-shadow: 0 0 8px var(--brand-500); animation: prismPulse 1.6s ease-in-out infinite; }
  }
  &.s-success {
    color: var(--status-fixed); background: rgba(79, 184, 122, 0.12);
    .pill-dot { background: var(--status-fixed); }
  }
  &.s-failed {
    color: var(--sev-severe); background: var(--sev-severe-bg);
    .pill-dot { background: var(--sev-severe); }
  }
  &.s-pending, &.s-cancelled {
    color: var(--gray-500); background: var(--gray-100);
    .pill-dot { background: var(--gray-400); }
  }
}

/* ============ 三栏 ============ */
.workbench {
  display: grid;
  grid-template-columns: 240px 1fr 360px;
  gap: 14px;
  flex: 1;
  min-height: 0;
}

@media (max-width: 1280px) {
  .workbench { grid-template-columns: 200px 1fr 320px; }
}

@media (max-width: 900px) {
  .review-detail-page {
    height: auto;
  }

  .task-head {
    grid-template-columns: 1fr;
    align-items: stretch;
  }

  .head-score,
  .head-tally,
  .head-actions {
    justify-content: flex-start;
  }

  .head-tally {
    flex-wrap: wrap;
  }

  .workbench {
    grid-template-columns: minmax(0, 1fr);
    min-height: auto;
  }

  .pane {
    min-height: 260px;
  }

  .pane-code {
    min-height: 420px;
  }
}

.pane {
  display: flex;
  flex-direction: column;
  background: #fff;
  border: 1px solid var(--gray-100);
  border-radius: 12px;
  overflow: hidden;
  box-shadow: var(--shadow-1);
  min-height: 0;
}

.pane-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  padding: 12px 14px;
  border-bottom: 1px solid var(--gray-100);
  font-size: 13px;
  font-weight: 600;
  color: var(--gray-900);
}

.pane-count {
  font-size: 11px;
  padding: 2px 8px;
  background: var(--gray-100);
  color: var(--gray-600);
  border-radius: 999px;
  font-weight: 500;
}

.pane-body {
  flex: 1;
  overflow-y: auto;
  padding: 8px;
  min-height: 0;
}

.pane-head-code {
  padding: 10px 14px;
}

.code-file {
  display: inline-flex;
  align-items: center;
  gap: 10px;
  font-size: 12.5px;
  color: var(--gray-800);

  .file-ico {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    width: 24px;
    height: 24px;
    border-radius: 6px;
    background: var(--gray-100);
    color: var(--gray-600);
    font-size: 9.5px;
  }

  .lang-chip {
    height: 20px;
    padding: 0 8px;
    border-radius: 4px;
    background: var(--brand-50);
    color: var(--brand-600);
    font-size: 10px;
    display: inline-flex;
    align-items: center;
  }
}

.code-body {
  padding: 0;
  position: relative;
}

.code-body :deep(.monaco-editor-wrapper) {
  border: none;
  border-radius: 0;
  height: 100%;
}

.loading-box {
  padding: 24px;
  height: 100%;
}

/* ============ 文件列表 ============ */
.file-row {
  display: grid;
  grid-template-columns: 24px 1fr auto auto;
  align-items: center;
  gap: 8px;
  padding: 8px 10px;
  border-radius: 8px;
  font-size: 12.5px;
  color: var(--gray-700);
  cursor: pointer;
  transition: all 0.12s ease;

  &:hover { background: var(--gray-50); color: var(--gray-900); }

  &.active {
    background: var(--brand-50);
    color: var(--brand-700);
    font-weight: 500;
  }

  .file-ico {
    width: 24px;
    height: 24px;
    border-radius: 6px;
    background: var(--gray-100);
    display: inline-flex;
    align-items: center;
    justify-content: center;
    font-size: 9.5px;
    color: var(--gray-600);
  }

  .file-name {
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .file-issues {
    font-size: 11px;
    font-family: var(--font-mono);
    font-weight: 600;
  }

  .file-open {
    opacity: 0;
    transition: opacity 0.12s ease;
  }

  &:hover .file-open,
  &.active .file-open {
    opacity: 1;
  }
}

/* ============ 问题过滤 chip ============ */
.filter-chips {
  display: flex;
  gap: 6px;
  padding: 10px 12px 6px;
  flex-wrap: wrap;
}

.chip {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  height: 28px;
  padding: 0 10px;
  background: var(--gray-50);
  border: 1px solid var(--gray-100);
  border-radius: 7px;
  font-size: 11.5px;
  color: var(--gray-700);
  cursor: pointer;
  transition: all 0.12s ease;

  &:hover { border-color: var(--brand-200); }
  &.active {
    background: var(--brand-50);
    border-color: var(--brand-200);
    color: var(--brand-700);
  }

  .chip-dot { width: 6px; height: 6px; border-radius: 50%; }
  .chip-num { color: var(--gray-500); font-size: 10.5px; }
  &.active .chip-num { color: var(--brand-600); }
}

.filter-dims {
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
  padding: 0 12px 10px;
  border-bottom: 1px solid var(--gray-100);
}

.dim-chip {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  height: 22px;
  padding: 0 8px;
  background: transparent;
  border: 1px solid var(--gray-100);
  border-radius: 999px;
  font-size: 10.5px;
  color: var(--gray-600);
  cursor: pointer;
  transition: all 0.12s ease;

  .dim-dot { width: 6px; height: 6px; border-radius: 50%; }

  &:hover { border-color: var(--dim-color); color: var(--dim-color); }
  &.active {
    background: color-mix(in srgb, var(--dim-color) 12%, transparent);
    border-color: var(--dim-color);
    color: var(--dim-color);
    font-weight: 500;
  }
}

/* ============ 问题列表 ============ */
.issue-body {
  padding: 4px 8px;
}

.issue-row {
  display: grid;
  grid-template-columns: auto 1fr auto;
  gap: 10px;
  align-items: flex-start;
  padding: 10px;
  border-radius: 8px;
  cursor: pointer;
  transition: all 0.12s ease;

  &:hover { background: var(--gray-50); }

  &.active {
    background: var(--brand-50);
    box-shadow: inset 3px 0 0 var(--brand-500);
  }
}

.issue-meta {
  min-width: 0;
}

.issue-title {
  font-size: 12.5px;
  font-weight: 500;
  color: var(--gray-900);
  line-height: 1.5;
  overflow: hidden;
  text-overflow: ellipsis;
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
}

.issue-sub {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  margin-top: 4px;
  font-size: 10.5px;
  color: var(--gray-500);

  .dim-dot {
    width: 6px;
    height: 6px;
    border-radius: 50%;
    margin-left: 4px;
  }
}

.issue-status {
  font-size: 10.5px;
  padding: 2px 7px;
  border-radius: 4px;
  font-weight: 500;
  white-space: nowrap;

  &.is-unfixed       { color: var(--status-unfix); background: var(--sev-high-bg); }
  &.is-fixed         { color: var(--status-fixed); background: rgba(79, 184, 122, 0.12); }
  &.is-ignored       { color: var(--gray-500);    background: var(--gray-100); }
  &.is-pending_review{ color: var(--brand-600);   background: var(--brand-50); }
}

.pane-foot {
  border-top: 1px solid var(--gray-100);
  padding: 8px 12px;
  display: flex;
  justify-content: center;
}
</style>
