<template>
  <div class="review-task-list-page">
    <div class="page-header">
      <h2>审查任务列表</h2>
      <el-button type="primary" @click="$router.push('/reviews/start')">
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
          @change="loadData"
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
          @change="loadData"
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
          @change="loadData"
          size="default"
        />
      </div>

      <el-table
        ref="tableRef"
        :data="tasks"
        v-loading="loading"
        style="width: 100%"
        size="default"
        @row-click="onRowClick"
        @selection-change="onSelectionChange"
        highlight-current-row
      >
        <template #empty>
          <EmptyState
            :description="hasFilter ? '当前筛选条件下没有审查任务,试试放宽条件' : '还没有审查任务'"
            :action-text="hasFilter ? '' : '启动第一个审查'"
            :action-to="hasFilter ? '' : '/reviews/start'"
          />
        </template>
        <el-table-column type="selection" width="44" />
        <el-table-column prop="task_name" label="任务名称" min-width="160" show-overflow-tooltip>
          <template #default="{ row }">
            {{ row.task_name || `审查 #${row.id}` }}
          </template>
        </el-table-column>
        <el-table-column prop="project_name" label="所属项目" min-width="140" show-overflow-tooltip />
        <el-table-column prop="review_type" label="审查类型" width="100">
          <template #default="{ row }">
            <el-tag size="small" type="info">{{ reviewTypeLabel(row.review_type) }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="status" label="状态" width="100">
          <template #default="{ row }">
            <el-tag :type="statusType(row.status)" size="small">{{ statusLabel(row.status) }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="score" label="评分" width="80" sortable>
          <template #default="{ row }">
            <span :class="scoreClass(row.score)">{{ row.score }}</span>
          </template>
        </el-table-column>
        <el-table-column prop="total_issues" label="问题数" width="80" sortable />
        <el-table-column prop="duration_ms" label="耗时" width="100">
          <template #default="{ row }">
            {{ formatDuration(row.duration_ms) }}
          </template>
        </el-table-column>
        <el-table-column prop="create_time" label="创建时间" width="170" sortable>
          <template #default="{ row }">
            {{ formatDateTime(row.create_time, 'YYYY-MM-DD HH:mm') }}
          </template>
        </el-table-column>
        <el-table-column label="操作" width="160" fixed="right">
          <template #default="{ row }">
            <el-button
              v-if="row.status === 'running'"
              link
              type="warning"
              size="small"
              @click.stop="handleCancel(row)"
            >停止</el-button>
            <el-button link type="danger" size="small" @click.stop="handleDelete(row)">删除</el-button>
          </template>
        </el-table-column>
      </el-table>

      <div v-if="selectedRows.length" class="batch-bar">
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
          @change="loadData"
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

const router = useRouter()
const tableRef = ref()

const loading = ref(false)
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

function formatDuration(ms: number): string {
  if (ms < 1000) return `${ms}ms`
  if (ms < 60000) return `${(ms / 1000).toFixed(1)}s`
  return `${Math.floor(ms / 60000)}m${Math.round((ms % 60000) / 1000)}s`
}

// 审查在后台异步执行,列表里存在 running 任务时轮询刷新,跑完即停。
let pollTimer: ReturnType<typeof setTimeout> | null = null
const POLL_INTERVAL = 4000

function clearPoll() {
  if (pollTimer) {
    clearTimeout(pollTimer)
    pollTimer = null
  }
}

function maybeSchedulePoll() {
  clearPoll()
  if (tasks.value.some((t) => t.status === 'running')) {
    pollTimer = setTimeout(() => loadData(true), POLL_INTERVAL)
  }
}

async function loadData(silent = false) {
  if (!silent) loading.value = true
  try {
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

    const data = await getReviewTasks(params)
    tasks.value = data.items
    total.value = data.total
    maybeSchedulePoll()
  } finally {
    if (!silent) loading.value = false
  }
}

async function loadProjects() {
  const data = await getProjects({ page_size: 100 })
  projects.value = data.items
}

function onRowClick(row: TaskOut) {
  router.push(`/reviews/${row.id}`)
}

async function handleDelete(row: TaskOut) {
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

function onSelectionChange(rows: TaskOut[]) {
  selectedRows.value = rows
}

function clearSelection() {
  tableRef.value?.clearSelection()
}

async function handleBatchStop() {
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

onUnmounted(clearPoll)
</script>

<style scoped lang="scss">
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
