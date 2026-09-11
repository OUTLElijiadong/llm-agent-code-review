<template>
  <div class="review-task-list-page">
    <div class="page-header">
      <h2>审查任务列表</h2>
      <el-button v-if="canStartReview" type="primary" @click="startReview">
        <el-icon><Plus /></el-icon>启动审查
      </el-button>
    </div>

    <el-card shadow="hover">
      <div class="filter-bar">
        <el-select
          v-model="filterStatus"
          placeholder="任务状态"
          clearable
          style="width: 140px"
          @change="loadData()"
        >
          <el-option label="待处理" value="pending" />
          <el-option label="运行中" value="running" />
          <el-option label="成功" value="success" />
          <el-option label="失败" value="failed" />
          <el-option label="已取消" value="cancelled" />
        </el-select>

        <el-select
          v-model="filterProjectId"
          placeholder="选择项目"
          clearable
          filterable
          style="width: 200px"
          @change="loadData()"
        >
          <el-option
            v-for="p in projects"
            :key="p.id"
            :label="p.project_name"
            :value="p.id"
          />
        </el-select>

        <el-date-picker
          v-model="dateRange"
          type="daterange"
          range-separator="至"
          start-placeholder="开始日期"
          end-placeholder="结束日期"
          value-format="YYYY-MM-DD"
          style="width: 260px"
          @change="loadData()"
          size="default"
        />
      </div>

      <el-alert v-if="loadError" type="warning" :closable="false" show-icon title="任务状态刷新失败，后台最新状态尚未确认">
        <p>{{ loadError }}。保留最近成功读取的结果；自动重试间隔 {{ retryDelayMs / 1000 }} 秒。</p>
        <el-button :loading="refreshing" :disabled="refreshing" @click="loadData()">立即重试</el-button>
      </el-alert>
      <el-alert v-if="projectsError" type="warning" :closable="false" :title="projectsError">
        <el-button @click="loadProjects">重试项目筛选加载</el-button>
      </el-alert>
      <div role="status" aria-live="polite">
        <span v-if="refreshing">正在读取任务状态…</span>
        <span v-else-if="lastUpdatedAt">最近成功读取：{{ formatDateTime(lastUpdatedAt, 'YYYY-MM-DD HH:mm:ss') }}</span>
        <el-button v-if="!loadError" link :disabled="refreshing" @click="loadData()">刷新任务状态</el-button>
      </div>

      <div class="task-cards" v-loading="loading" role="list" data-testid="task-cards">
        <EmptyState
          v-if="!tasks.length"
          :description="loadError ? '任务列表读取失败，请重试' : (hasFilter ? '当前筛选条件下没有审查任务,试试放宽条件' : '还没有审查任务')"
          :action-text="loadError || hasFilter || !canStartReview ? '' : '启动第一个审查'"
          :action-to="loadError || hasFilter || !canStartReview ? '' : '/reviews/start'"
        />
        <article
          v-for="row in tasks"
          :key="row.id"
          class="task-card"
          :data-status="row.status"
          role="listitem"
          @click="onRowClick(row)"
        >
          <label v-if="canCancelReview" class="tc-check" @click.stop>
            <input
              type="checkbox"
              :checked="selectedRows.some((t) => t.id === row.id)"
              :aria-label="`选择 ${row.task_name || '任务'}`"
              @change="toggleSelect(row)"
            >
          </label>
          <span class="tc-band" :data-status="row.status" aria-hidden="true"></span>
          <div class="tc-main">
            <div class="tc-line1">
              <b class="tc-name">{{ row.task_name || `审查 #${row.id}` }}</b>
              <el-tag size="small" type="info" effect="plain">{{ reviewTypeLabel(row.review_type) }}</el-tag>
              <el-tag :type="statusType(row.status)" size="small">{{ statusLabel(row.status) }}</el-tag>
            </div>
            <div class="tc-line2 font-mono">
              <span class="tc-project">{{ row.project_name }}</span>
              <span>问题 {{ row.review_type === 'sandbox_test' ? (row.report_issue_summary?.total ?? '—') : row.total_issues }}</span>
              <span>{{ formatDuration(row.duration_ms) }}</span>
              <span>{{ formatDateTime(row.create_time, 'YYYY-MM-DD HH:mm') }}</span>
            </div>
            <div v-if="row.status === 'running' && row.total_files" class="tc-progress" :title="`${row.processed_files}/${row.total_files} 文件`">
              <span class="tc-progress-fill" :style="{ width: `${Math.min(100, Math.round(((row.processed_files || 0) / row.total_files) * 100))}%` }"></span>
            </div>
          </div>
          <div class="tc-score">
            <svg v-if="row.status === 'success'" viewBox="0 0 36 36" class="tc-ring" aria-hidden="true">
              <circle cx="18" cy="18" r="15.9" fill="none" stroke="var(--gray-100, #eef0f4)" stroke-width="3.5" />
              <circle
                cx="18" cy="18" r="15.9" fill="none" stroke-width="3.5" stroke-linecap="round"
                :stroke="row.score >= 80 ? '#40a35f' : row.score >= 60 ? '#d9a857' : '#dc4961'"
                :stroke-dasharray="`${Math.max(0, Math.min(100, row.score))} 100`"
                stroke-dashoffset="25"
              />
            </svg>
            <span v-if="row.status === 'success'" :class="scoreClass(row.score)">{{ row.score }}</span>
            <span v-else class="no-score">未形成评分</span>
          </div>
          <div class="tc-actions" @click.stop>
            <el-button
              v-if="row.status === 'running'"
              link type="warning" size="small"
              @click="handleCancel(row)"
            >停止</el-button>
            <el-button link type="danger" size="small" @click="handleDelete(row)">删除</el-button>
          </div>
        </article>
      </div>

      <div v-if="canCancelReview && selectedRows.length" class="batch-bar">
        <span class="batch-info">已选 {{ selectedRows.length }} 项</span>
        <el-button
          size="small"
          type="warning"
          plain
          :disabled="!selectedRunning.length"
          :loading="batchStopping"
          @click="handleBatchStop"
        >批量停止{{ selectedRunning.length ? ` (${selectedRunning.length})` : '' }}</el-button>
        <el-button
          size="small"
          type="danger"
          plain
          :loading="batchDeleting"
          @click="handleBatchDelete"
        >批量删除</el-button>
        <el-button size="small" link @click="clearSelection">取消选择</el-button>
      </div>

      <div class="pagination-wrapper">
        <el-pagination
          v-model:current-page="page"
          v-model:page-size="pageSize"
          :total="total"
          :page-sizes="[20, 50, 100]"
          layout="total, sizes, prev, pager, next"
          @change="loadData()"
        />
      </div>
    </el-card>
  </div>
</template>

<script setup lang="ts">
import EmptyState from '@/components/common/EmptyState.vue'
import { ref, computed, onMounted, onUnmounted } from 'vue'
import { useRouter } from 'vue-router'

import { formatDateTime } from '@/utils/format'
import { Plus } from '@element-plus/icons-vue'
import { getReviewTasks, deleteReviewTask, cancelReviewTask } from '@/api/review'
import { getProjects } from '@/api/project'
import type { TaskOut } from '@/types/review'
import type { ProjectOut } from '@/types/project'
import { reviewTypeLabel } from '@/constants/reviewType'
import { ElMessage } from 'element-plus/es/components/message/index'
import { confirmDanger } from '@/composables/useDangerConfirm'
import { useUserStore } from '@/stores/user'

const router = useRouter()
const userStore = useUserStore()

const loading = ref(false)
const refreshing = ref(false)
const loadError = ref('')
const projectsError = ref('')
const lastUpdatedAt = ref('')
const retryDelayMs = ref(4000)
const tasks = ref<TaskOut[]>([])
const projects = ref<ProjectOut[]>([])
const total = ref(0)
const page = ref(1)
const pageSize = ref(20)
const filterStatus = ref('')
const filterProjectId = ref<number | null>(null)
const dateRange = ref<[string, string] | null>(null)

/** 空态文案依据:是否有筛选条件(区分「没有任务」与「筛选无结果」)。 */
const hasFilter = computed(() => Boolean(filterStatus.value || filterProjectId.value || dateRange.value))
const canStartReview = computed(() => userStore.hasPermission('review:start'))
const canCancelReview = computed(() => userStore.hasPermission('review:cancel'))

function startReview(): void {
  if (!canStartReview.value) return
  router.push('/reviews/start')
}

const statusLabels: Record<string, string> = {
  pending: '待处理',
  running: '运行中',
  success: '成功',
  failed: '失败',
  cancelled: '已取消',
}

const statusTypeMap: Record<string, string> = {
  pending: 'info',
  running: 'warning',
  success: 'success',
  failed: 'danger',
  cancelled: 'info',
}

function statusLabel(status: string) {
  return statusLabels[status] ?? status
}

function statusType(status: string) {
  return statusTypeMap[status] ?? 'info'
}

function scoreClass(score: number) {
  if (score >= 80) return 'score-high'
  if (score >= 60) return 'score-medium'
  return 'score-low'
}

function compareScores(first: TaskOut, second: TaskOut): number {
  const firstScore = first.status === 'success' ? first.score : -1
  const secondScore = second.status === 'success' ? second.score : -1
  return firstScore - secondScore
}

function formatDuration(ms: number): string {
  if (ms < 1000) return `${ms}ms`
  if (ms < 60000) return `${(ms / 1000).toFixed(1)}s`
  return `${Math.floor(ms / 60000)}m${Math.round((ms % 60000) / 1000)}s`
}

let pollTimer: ReturnType<typeof setTimeout> | null = null
const POLL_INTERVAL = 4000
const MAX_POLL_INTERVAL = 30000
let failureCount = 0
let loadRequest = 0
let requestKey = ''
let disposed = false

function clearPoll() {
  if (pollTimer) {
    clearTimeout(pollTimer)
    pollTimer = null
  }
}

function maybeSchedulePoll() {
  clearPoll()
  if (disposed || refreshing.value) return
  if (loadError.value || tasks.value.some(task => task.status === 'running' || task.status === 'pending')) {
    retryDelayMs.value = loadError.value
      ? Math.min(POLL_INTERVAL * 2 ** Math.max(0, failureCount - 1), MAX_POLL_INTERVAL)
      : POLL_INTERVAL
    pollTimer = setTimeout(() => {
      pollTimer = null
      void loadData(true)
    }, retryDelayMs.value)
  }
}

async function loadData(silent = false) {
  if (disposed) return
  const params: Record<string, unknown> = {
    page: page.value,
    page_size: pageSize.value,
  }
  if (filterStatus.value) params.status = filterStatus.value
  if (filterProjectId.value) params.project_id = filterProjectId.value
  if (dateRange.value) {
    params.start = dateRange.value[0]
    params.end = dateRange.value[1]
  }
  const nextKey = JSON.stringify(params)
  if (refreshing.value && requestKey === nextKey) return
  requestKey = nextKey
  const request = ++loadRequest
  clearPoll()
  refreshing.value = true
  if (!silent) loading.value = true
  try {
    const data = await getReviewTasks(params)
    if (disposed || request !== loadRequest) return
    tasks.value = data.items
    total.value = data.total
    loadError.value = ''
    lastUpdatedAt.value = new Date().toISOString()
    failureCount = 0
  } catch (error) {
    if (disposed || request !== loadRequest) return
    loadError.value = error && typeof error === 'object' && 'message' in error && typeof error.message === 'string'
      ? error.message || '任务状态读取失败' : '任务状态读取失败'
    failureCount = Math.min(failureCount + 1, 4)
  } finally {
    if (!disposed && request === loadRequest) {
      loading.value = false
      refreshing.value = false
      maybeSchedulePoll()
    }
  }
}

async function loadProjects() {
  try {
    const data = await getProjects({ page_size: 100 })
    if (disposed) return
    projects.value = data.items
    projectsError.value = ''
  } catch {
    if (!disposed) projectsError.value = '项目筛选加载失败，仍可查看任务列表或重试'
  }
}

function onRowClick(row: TaskOut) {
  router.push(`/reviews/${row.id}`)
}

async function handleDelete(row: TaskOut) {
  if (!canCancelReview.value) return
  const ok = await confirmDanger({ target: `删除任务「${row.task_name || `审查 #${row.id}`}」` })
  if (!ok) return
  try {
    await deleteReviewTask(row.id)
    ElMessage.success('任务已删除')
    await loadData()
  } catch {
    /* http 拦截器已处理 */
  }
}

async function handleCancel(row: TaskOut) {
  if (!canCancelReview.value) return
  const ok = await confirmDanger({
    target: `停止任务「${row.task_name || `审查 #${row.id}`}」`,
    consequence: '已处理的部分将保留',
    confirmText: '确定停止',
  })
  if (!ok) return
  try {
    await cancelReviewTask(row.id)
    ElMessage.success('任务已停止')
    await loadData()
  } catch {
    /* http 拦截器已处理 */
  }
}

// ── 批量操作 ──
const selectedRows = ref<TaskOut[]>([])
const batchStopping = ref(false)
const batchDeleting = ref(false)

const selectedRunning = computed(() => selectedRows.value.filter((t) => t.status === 'running'))

function toggleSelect(row: TaskOut) {
  if (!canCancelReview.value) return
  const index = selectedRows.value.findIndex((t) => t.id === row.id)
  if (index >= 0) selectedRows.value.splice(index, 1)
  else selectedRows.value.push(row)
}

function clearSelection() {
  selectedRows.value = []
}

async function handleBatchStop() {
  if (!canCancelReview.value) return
  const targets = selectedRunning.value
  if (!targets.length) return
  const ok = await confirmDanger({
    target: `停止选中的 ${targets.length} 个运行中任务`,
    consequence: '各任务已处理的部分将保留',
    confirmText: '确定停止',
  })
  if (!ok) return
  batchStopping.value = true
  let failed = 0
  try {
    for (const t of targets) {
      try {
        await cancelReviewTask(t.id)
      } catch {
        failed++
      }
    }
    if (failed) ElMessage.warning(`${failed} 个任务停止失败，其余已停止`)
    else ElMessage.success(`已停止 ${targets.length} 个任务`)
    await loadData()
  } finally {
    batchStopping.value = false
  }
}

async function handleBatchDelete() {
  if (!canCancelReview.value) return
  const targets = selectedRows.value
  if (!targets.length) return
  const ok = await confirmDanger({ target: `删除选中的 ${targets.length} 个任务` })
  if (!ok) return
  batchDeleting.value = true
  let failed = 0
  try {
    for (const t of targets) {
      try {
        await deleteReviewTask(t.id)
      } catch {
        failed++
      }
    }
    if (failed) ElMessage.warning(`${failed} 个任务删除失败，其余已删除`)
    else ElMessage.success(`已删除 ${targets.length} 个任务`)
    clearSelection()
    await loadData()
  } finally {
    batchDeleting.value = false
  }
}

onMounted(() => {
  loadProjects()
  loadData()
})

onUnmounted(() => {
  disposed = true
  loadRequest++
  clearPoll()
})
</script>

<style scoped lang="scss">

/* ── 任务卡片列表(替代表格:突出任务本身,次要信息降级为次行) ── */
.task-cards { display: grid; gap: 10px; min-height: 120px; }
.task-card {
  position: relative; display: grid;
  grid-template-columns: auto auto minmax(0, 1fr) auto auto;
  gap: 14px; align-items: center;
  padding: 13px 16px 13px 12px; border-radius: 12px;
  background: #fff; border: 1px solid var(--gray-100, #eef0f4);
  cursor: pointer; transition: transform .16s ease, box-shadow .16s ease, border-color .16s ease;
}
.task-card:hover {
  transform: translateY(-1.5px);
  box-shadow: 0 10px 24px rgba(23, 34, 62, .07);
  border-color: var(--brand-300, #a8c4fa);
}
.tc-check { display: grid; place-items: center; cursor: pointer; }
.tc-check input { width: 15px; height: 15px; accent-color: var(--brand-500, #4078f4); cursor: pointer; }
.tc-band { width: 4px; height: 38px; border-radius: 999px; }
.tc-band[data-status='running'] { background: var(--brand-500, #4078f4); }
.tc-band[data-status='pending'] { background: var(--gray-300, #cfd4dc); }
.tc-band[data-status='success'] { background: #40a35f; }
.tc-band[data-status='failed'] { background: var(--sev-severe, #dc4961); }
.tc-band[data-status='cancelled'] { background: var(--gray-200, #e3e6eb); }
.tc-main { display: grid; gap: 5px; min-width: 0; }
.tc-line1 { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.tc-name { font-size: 13.5px; font-weight: 600; max-width: 420px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.tc-line2 { display: flex; gap: 14px; flex-wrap: wrap; font-size: 11px; color: var(--gray-500); }
.tc-project { max-width: 220px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.tc-progress { position: relative; height: 5px; border-radius: 999px; background: var(--gray-100, #eef0f4); overflow: hidden; max-width: 360px; }
.tc-progress-fill {
  position: absolute; inset: 0 auto 0 0; border-radius: 999px;
  background: linear-gradient(90deg, var(--brand-400, #6f9df7), var(--brand-600, #2f5ce0));
  transition: width .5s ease;
}
.tc-score { display: grid; place-items: center; gap: 2px; min-width: 56px; }
.tc-ring { width: 38px; height: 38px; transform: rotate(0deg); }
.tc-score span { font-size: 12.5px; font-weight: 700; }
.no-score { font-size: 11px; color: var(--gray-400); font-weight: 400; }
.tc-actions { display: flex; gap: 4px; }

@media (prefers-reduced-motion: reduce) {
  .task-card, .tc-progress-fill { transition: none; }
}
@media (max-width: 760px) {
  .task-card { grid-template-columns: auto minmax(0, 1fr) auto; }
  .tc-band, .tc-actions { display: none; }
  .tc-name { max-width: 46vw; }
}
.review-task-list-page {
  .page-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    margin-bottom: 20px;

    h2 {
      margin: 0;
      font-size: 20px;
      font-weight: 600;
    }
  }
}

.filter-bar {
  display: flex;
  gap: 12px;
  flex-wrap: wrap;
  margin-bottom: 16px;
}

.pagination-wrapper {
  display: flex;
  justify-content: flex-end;
  margin-top: 16px;
}

.batch-bar {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-top: 12px;
  padding: 8px 12px;
  background: var(--el-fill-color-light);
  border-radius: 8px;

  .batch-info {
    font-size: 13px;
    color: var(--el-text-color-secondary);
  }
}

.score-high { color: #67c23a; font-weight: 600; }
.score-medium { color: #e6a23c; font-weight: 600; }
.score-low { color: #f56c6c; font-weight: 600; }
</style>
