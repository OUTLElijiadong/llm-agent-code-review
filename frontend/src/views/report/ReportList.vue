<template>
  <div class="report-list-page">
    <div class="page-header">
      <h2>审查报告列表</h2>
      <p class="page-sub">审查任务完成后自动生成报告,可导出 Word/PDF</p>
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
        <template #empty>
          <EmptyState
            :description="hasFilter ? '该项目还没有审查报告' : '暂无审查报告'"
            :action-text="hasFilter || !canStartReview ? '' : '去启动审查'"
            :action-to="hasFilter || !canStartReview ? '' : '/reviews/start'"
          />
        </template>
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
        <el-table-column prop="status" label="结论" width="90">
          <template #default="{ row }">
            <el-tag :type="row.status === 'success' ? 'success' : 'danger'" size="small">
              {{ row.status === 'success' ? '通过' : '未通过' }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="total_issues" label="问题数" width="80" sortable />
        <el-table-column prop="create_time" label="创建时间" width="170" sortable>
          <template #default="{ row }">
            {{ formatDateTime(row.create_time, 'YYYY-MM-DD HH:mm') }}
          </template>
        </el-table-column>
        <el-table-column label="操作" width="170" fixed="right" align="center">
          <template #default="{ row }">
            <el-tooltip content="查看详情" placement="top">
              <el-button link type="primary" size="small" :icon="ViewIcon" aria-label="查看详情" @click.stop="goDetail(row.task_id)" />
            </el-tooltip>
            <el-tooltip content="生成报告" placement="top">
              <el-button link type="primary" size="small" :icon="MagicStick" aria-label="生成报告" @click.stop="goGenerate(row.task_id)" />
            </el-tooltip>
            <el-dropdown trigger="click" @command="(cmd: string) => handleRowCommand(row, cmd)">
              <el-button
                link
                type="primary"
                size="small"
                :loading="exportingTaskId === row.task_id"
                aria-label="更多操作"
                @click.stop
              >
                <el-icon class="el-icon--right"><ArrowDown /></el-icon>
              </el-button>
              <template #dropdown>
                <el-dropdown-menu>
                  <el-dropdown-item v-if="canExport('json')" :command="'export:json'">导出 JSON</el-dropdown-item>
                  <el-dropdown-item v-if="canExport('html')" :command="'export:html'">导出 HTML</el-dropdown-item>
                  <el-dropdown-item v-if="canExport('pdf')" :command="'export:pdf'">导出 PDF</el-dropdown-item>
                  <el-dropdown-item v-if="canExport('word')" :command="'export:word'">导出 Word</el-dropdown-item>
                  <el-dropdown-item :command="'delete'" divided>
                    <span class="danger-item">删除报告</span>
                  </el-dropdown-item>
                </el-dropdown-menu>
              </template>
            </el-dropdown>
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
import { computed, ref, onMounted } from 'vue'
import { useRouter } from 'vue-router'

import { ArrowDown, MagicStick, View as ViewIcon } from '@element-plus/icons-vue'
import { formatDateTime } from '@/utils/format'
import { getReports, deleteReport, exportReport } from '@/api/report'
import { getProjects } from '@/api/project'
import type { ReportListItem, ReportFormat } from '@/types/report'
import EmptyState from '@/components/common/EmptyState.vue'
import type { ProjectOut } from '@/types/project'
import { ElMessage } from 'element-plus/es/components/message/index'
import { confirmDanger } from '@/composables/useDangerConfirm'
import { useUserStore } from '@/stores/user'

const router = useRouter()
const userStore = useUserStore()

const loading = ref(false)
const reports = ref<ReportListItem[]>([])
const projects = ref<ProjectOut[]>([])
const total = ref(0)
const page = ref(1)
const pageSize = ref(20)
const filterProjectId = ref<number | null>(null)

/** 空态文案依据:是否筛选了项目。 */
const hasFilter = computed(() => Boolean(filterProjectId.value))
const canStartReview = computed(() => userStore.hasPermission('review:start'))
const dateRange = ref<[string, string] | null>(null)
/** 当前正在导出的任务 ID(用于导出按钮 loading 态),null 表示无操作 */
const exportingTaskId = ref<number | null>(null)

function canExport(format: ReportFormat): boolean {
  return userStore.hasPermission(`report:export:${format}`)
}

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
 * 跳转到报告详情页并自动触发生成(通过 query 参数 generate=1,
 * 详情页 onMounted 时检测并调用 handleGenerate 自动生成 HTML 报告)。
 * @param taskId - 审查任务 ID
 */
function goGenerate(taskId: number): void {
  router.push({ path: `/reports/${taskId}`, query: { generate: '1' } })
}

/**
 * 触发浏览器下载 Blob 文件。
 * @param blob - 文件二进制内容
 * @param filename - 下载文件名
 */
function downloadBlob(blob: Blob, filename: string): void {
  const url = window.URL.createObjectURL(blob)
  const link = document.createElement('a')
  link.href = url
  link.download = filename
  document.body.appendChild(link)
  link.click()
  document.body.removeChild(link)
  window.URL.revokeObjectURL(url)
}

/**
 * 导出报告(在列表页直接调用 exportReport 下载文件)。
 * @param row - 报告行数据
 * @param format - 导出格式 json/html/pdf/word
 */
async function handleExport(row: ReportListItem, format: ReportFormat): Promise<void> {
  if (!canExport(format)) return
  exportingTaskId.value = row.task_id
  try {
    const blob = await exportReport(row.task_id, format, 'detailed')
    const extMap: Record<ReportFormat, string> = {
      json: 'json', html: 'html', pdf: 'pdf', word: 'docx',
    }
    const taskName = row.task_name || `task_${row.task_id}`
    downloadBlob(blob, `review_report_${taskName}_${row.task_id}.${extMap[format]}`)
    ElMessage.success(`${format.toUpperCase()} 报告导出成功`)
  } catch {
    ElMessage.error(`${format.toUpperCase()} 报告导出失败`)
  } finally {
    exportingTaskId.value = null
  }
}

/**
 * 删除报告(带统一危险确认)
 * @param row - 报告行数据
 */
async function handleDelete(row: ReportListItem) {
  const ok = await confirmDanger({ target: `删除报告「${row.task_name || `审查 #${row.task_id}`}」` })
  if (!ok) return
  try {
    await deleteReport(row.task_id)
    ElMessage.success('报告已删除')
    await loadData()
  } catch {
    /* http 拦截器已处理 */
  }
}

/**
 * 操作列「更多」下拉命令分发:export:* 导出,delete 删除。
 */
function handleRowCommand(row: ReportListItem, cmd: string): void {
  if (cmd === 'delete') {
    void handleDelete(row)
    return
  }
  if (cmd.startsWith('export:')) {
    void handleExport(row, cmd.slice('export:'.length) as ReportFormat)
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

.danger-item { color: var(--el-color-danger); }
</style>
