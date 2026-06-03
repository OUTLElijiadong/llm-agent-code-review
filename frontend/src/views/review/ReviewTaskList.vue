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
        :data="tasks"
        v-loading="loading"
        style="width: 100%"
        size="default"
        @row-click="onRowClick"
        highlight-current-row
      >
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
import { ref, onMounted } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { formatDateTime } from '@/utils/format'
import { Plus } from '@element-plus/icons-vue'
import { getReviewTasks, deleteReviewTask, cancelReviewTask } from '@/api/review'
import { getProjects } from '@/api/project'
import type { TaskOut } from '@/types/review'
import type { ProjectOut } from '@/types/project'
import { reviewTypeLabel } from '@/constants/reviewType'

const router = useRouter()

const loading = ref(false)
const tasks = ref<TaskOut[]>([])
const projects = ref<ProjectOut[]>([])
const total = ref(0)
const page = ref(1)
const pageSize = ref(20)
const filterStatus = ref('')
const filterProjectId = ref<number | null>(null)
const dateRange = ref<[string, string] | null>(null)

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

async function loadData() {
  loading.value = true
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
  } finally {
    loading.value = false
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
  try {
    await ElMessageBox.confirm(
      `确定要删除任务「${row.task_name || `审查 #${row.id}`}」吗？删除后不可恢复。`,
      '删除审查任务',
      {
        confirmButtonText: '确定删除',
        cancelButtonText: '取消',
        type: 'warning',
        confirmButtonClass: 'el-button--danger',
      }
    )
    await deleteReviewTask(row.id)
    ElMessage.success('任务已删除')
    await loadData()
  } catch {
    /* 用户取消或 http 拦截器已处理 */
  }
}

async function handleCancel(row: TaskOut) {
  try {
    await ElMessageBox.confirm(
      `确定要停止任务「${row.task_name || `审查 #${row.id}`}」吗？已处理的部分将保留。`,
      '停止审查任务',
      {
        confirmButtonText: '确定停止',
        cancelButtonText: '取消',
        type: 'warning',
      }
    )
    await cancelReviewTask(row.id)
    ElMessage.success('任务已停止')
    await loadData()
  } catch {
    /* 用户取消或 http 拦截器已处理 */
  }
}

onMounted(() => {
  loadProjects()
  loadData()
})
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

.score-high { color: #67c23a; font-weight: 600; }
.score-medium { color: #e6a23c; font-weight: 600; }
.score-low { color: #f56c6c; font-weight: 600; }
</style>
