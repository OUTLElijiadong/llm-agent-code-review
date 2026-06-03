<template>
  <div class="report-list-page">
    <div class="page-header">
      <h2>审查报告列表</h2>
    </div>

    <el-card shadow="hover">
      <div class="filter-bar">
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
        />
      </div>

      <el-table
        :data="reports"
        v-loading="loading"
        style="width: 100%"
        @row-click="onRowClick"
        highlight-current-row
      >
        <el-table-column prop="task_name" label="任务名称" min-width="160">
          <template #default="{ row }">
            {{ row.task_name || `审查 #${row.task_id}` }}
          </template>
        </el-table-column>
        <el-table-column prop="project_name" label="所属项目" min-width="140" show-overflow-tooltip />
        <el-table-column prop="score" label="评分" width="80" sortable>
          <template #default="{ row }">
            <span :class="scoreClass(row.score)">{{ row.score }}</span>
          </template>
        </el-table-column>
        <el-table-column prop="total_issues" label="问题数" width="80" sortable />
        <el-table-column prop="create_time" label="创建时间" width="170" sortable>
          <template #default="{ row }">
            {{ formatDateTime(row.create_time, 'YYYY-MM-DD HH:mm') }}
          </template>
        </el-table-column>
        <el-table-column label="操作" width="160" fixed="right">
          <template #default="{ row }">
            <el-button link type="primary" size="small" @click.stop="goDetail(row.task_id)">查看详情</el-button>
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
import { getReports, deleteReport } from '@/api/report'
import { getProjects } from '@/api/project'
import type { ReportListItem } from '@/types/report'
import type { ProjectOut } from '@/types/project'

const router = useRouter()

const loading = ref(false)
const reports = ref<ReportListItem[]>([])
const projects = ref<ProjectOut[]>([])
const total = ref(0)
const page = ref(1)
const pageSize = ref(20)
const filterProjectId = ref<number | null>(null)
const dateRange = ref<[string, string] | null>(null)

function scoreClass(score: number) {
  if (score >= 80) return 'score-high'
  if (score >= 60) return 'score-medium'
  return 'score-low'
}

async function loadData() {
  loading.value = true
  try {
    const params: Record<string, unknown> = {
      page: page.value,
      page_size: pageSize.value,
    }
    if (filterProjectId.value) params.project_id = filterProjectId.value
    if (dateRange.value) {
      params.start = dateRange.value[0]
      params.end = dateRange.value[1]
    }

    const data = await getReports(params)
    reports.value = data.items
    total.value = data.total
  } finally {
    loading.value = false
  }
}

async function loadProjects() {
  const data = await getProjects({ page_size: 100 })
  projects.value = data.items
}

function onRowClick(row: ReportListItem) {
  router.push(`/reports/${row.task_id}`)
}

function goDetail(taskId: number) {
  router.push(`/reports/${taskId}`)
}

/**
 * 删除报告(带二次确认)
 * @param row - 报告行数据
 */
async function handleDelete(row: ReportListItem) {
  try {
    await ElMessageBox.confirm(
      `确定要删除报告「${row.task_name || `审查 #${row.task_id}`}」吗？删除后不可恢复。`,
      '删除报告',
      {
        confirmButtonText: '确定删除',
        cancelButtonText: '取消',
        type: 'warning',
        confirmButtonClass: 'el-button--danger',
      }
    )
    await deleteReport(row.task_id)
    ElMessage.success('报告已删除')
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
.report-list-page {
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
