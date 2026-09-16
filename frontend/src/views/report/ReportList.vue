<template>
  <div class="report-list-page">
    <div class="page-header">
      <h2>审查报告列表</h2>
      <p class="page-sub">审查任务完成后自动生成报告；导出格式以报告来源和实际制品为准</p>
    </div>

    <section v-if="exportErrorMessage" class="report-export-error" role="alert">
      <strong>{{ exportErrorMessage }}</strong>
      <span v-if="exportErrorNextAction">{{ exportErrorNextAction }}</span>
      <el-button v-if="retryExportRow && retryExportFormat" size="small" @click="retryExport">重试导出</el-button>
    </section>

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

      <div class="report-cards" v-loading="loading" role="list" data-testid="report-cards">
        <EmptyState
          v-if="!reports.length"
          :description="hasFilter ? '该项目还没有审查报告' : '暂无审查报告'"
          :action-text="hasFilter || !canStartReview ? '' : '去启动审查'"
          :action-to="hasFilter || !canStartReview ? '' : '/reviews/start'"
        />
        <article
          v-for="row in reports"
          :key="row.task_id"
          class="report-card"
          :data-status="row.status"
          role="listitem"
          @click="onRowClick(row)"
        >
          <span class="rc-band" :data-status="row.status" aria-hidden="true"></span>
          <div class="rc-main">
            <div class="rc-line1">
              <b class="rc-name" :title="row.task_name || `审查 #${row.task_id}`">{{ row.task_name || `审查 #${row.task_id}` }}</b>
              <el-tag v-if="row.source?.type" size="small" type="info" effect="plain">{{ reviewTypeLabel(row.source.type) }}</el-tag>
              <el-tag :type="row.status === 'success' ? 'success' : 'danger'" size="small">
                {{ row.status === 'success' ? '通过' : '未通过' }}
              </el-tag>
            </div>
            <div class="rc-line2 font-mono">
              <span class="rc-project" :title="row.project_name">{{ row.project_name }}</span>
              <span v-if="row.source?.type === 'sandbox_test'">{{ row.source.report_issue_summary?.total ?? '—' }} 条报告条目</span>
              <span v-else>问题 {{ row.total_issues }}</span>
              <span>{{ formatDateTime(row.create_time, 'YYYY-MM-DD HH:mm') }}</span>
            </div>
          </div>
          <div class="rc-score" :title="`综合评分 ${row.score}`">
            <svg viewBox="0 0 36 36" class="rc-ring" aria-hidden="true">
              <circle cx="18" cy="18" r="15.9" fill="none" stroke="var(--gray-100, #eef0f4)" stroke-width="3.5" />
              <circle
                cx="18" cy="18" r="15.9" fill="none" stroke-width="3.5" stroke-linecap="round"
                :stroke="row.score >= 80 ? '#40a35f' : row.score >= 60 ? '#d9a857' : '#dc4961'"
                :stroke-dasharray="`${Math.max(0, Math.min(100, row.score))} 100`"
                stroke-dashoffset="25"
              />
            </svg>
            <span :class="scoreClass(row.score)">{{ row.score }}</span>
          </div>
          <div class="rc-actions" @click.stop>
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
                aria-label="导出报告"
                @click.stop
              >
                导出<el-icon class="el-icon--right"><ArrowDown /></el-icon>
              </el-button>
              <template #dropdown>
                <el-dropdown-menu>
                  <el-dropdown-item v-if="canExport('json')" command="export:json">导出 JSON</el-dropdown-item>
                  <el-dropdown-item v-if="canExport('html') && !isDomainReport(row)" command="export:html">导出 HTML</el-dropdown-item>
                  <el-dropdown-item v-if="canExport('pdf') && !isDomainReport(row)" command="export:pdf">导出 PDF</el-dropdown-item>
                  <el-dropdown-item v-if="canExport('word') && !isDomainReport(row)" command="export:word">导出 Word</el-dropdown-item>
                  <el-dropdown-item v-if="canDeleteReport" command="delete" divided>
                    <span class="danger-item">删除报告</span>
                  </el-dropdown-item>
                </el-dropdown-menu>
              </template>
            </el-dropdown>
          </div>
        </article>
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
import { computed, ref, onMounted } from 'vue'
import { useRouter } from 'vue-router'

import { ArrowDown, MagicStick, View as ViewIcon } from '@element-plus/icons-vue'
import { formatDateTime } from '@/utils/format'
import { getReports, deleteReport, exportReport } from '@/api/report'
import { getProjects } from '@/api/project'
import type { ReportListItem, ReportFormat } from '@/types/report'
import EmptyState from '@/components/common/EmptyState.vue'
import type { ProjectOut } from '@/types/project'
import { reviewTypeLabel } from '@/constants/reviewType'
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
const canDeleteReport = computed(() => userStore.hasPermission('review:cancel'))
const dateRange = ref<[string, string] | null>(null)
/** 当前正在导出的任务 ID(用于导出按钮 loading 态),null 表示无操作 */
const exportingTaskId = ref<number | null>(null)
const exportErrorMessage = ref('')
const exportErrorNextAction = ref('')
const retryExportRow = ref<ReportListItem | null>(null)
const retryExportFormat = ref<ReportFormat | null>(null)

function isDomainReport(row: ReportListItem): boolean {
  return row.source?.type === 'sandbox_test' || row.source?.type === 'pentest'
}

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
  if (exportingTaskId.value !== null) return
  if (isDomainReport(row) && format !== 'json') {
    showExportError({ message: `领域报告不支持 ${format.toUpperCase()}`, next_action: '请导出真实领域 JSON' }, format, row)
    return
  }
  exportingTaskId.value = row.task_id
  exportErrorMessage.value = ''
  exportErrorNextAction.value = ''
  retryExportRow.value = null
  retryExportFormat.value = null
  try {
    const blob = await exportReport(row.task_id, format, 'detailed')
    const extMap: Record<ReportFormat, string> = {
      json: 'json', html: 'html', pdf: 'pdf', word: 'docx',
    }
    const taskName = row.task_name || `task_${row.task_id}`
    downloadBlob(blob, `review_report_${taskName}_${row.task_id}.${extMap[format]}`)
    ElMessage.success(`${format.toUpperCase()} 报告导出成功`)
  } catch (error) {
    showExportError(error, format, row)
  } finally {
    exportingTaskId.value = null
  }
}

function showExportError(error: unknown, format: ReportFormat, row?: ReportListItem): void {
  const payload = error as { message?: string; next_action?: string }
  exportErrorMessage.value = payload?.message || `${format.toUpperCase()} 报告导出失败`
  exportErrorNextAction.value = payload?.next_action || '请检查报告状态后重试'
  retryExportRow.value = row ?? null
  retryExportFormat.value = row && isDomainReport(row) ? 'json' : (row ? format : null)
  ElMessage.error(exportErrorMessage.value)
}

function retryExport(): void {
  if (retryExportRow.value && retryExportFormat.value) {
    void handleExport(retryExportRow.value, retryExportFormat.value)
  }
}

/**
 * 删除报告(带统一危险确认)
 * @param row - 报告行数据
 */
async function handleDelete(row: ReportListItem) {
  if (!canDeleteReport.value) return
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
 * 卡片「导出」下拉命令分发:export:* 导出,delete 删除。
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

/* ── 报告卡片列表(替代表格:评分色环+任务名+类型徽章为主,项目/问题数/时间降级为次行) ── */
.report-cards { display: grid; gap: 10px; min-height: 120px; }
.report-card {
  position: relative; display: grid;
  grid-template-columns: auto minmax(0, 1fr) auto auto;
  gap: 14px; align-items: center;
  padding: 13px 16px 13px 12px; border-radius: 12px;
  background: #fff; border: 1px solid var(--gray-100, #eef0f4);
  cursor: pointer; transition: transform .16s ease, box-shadow .16s ease, border-color .16s ease;
}
.report-card:hover {
  transform: translateY(-1.5px);
  box-shadow: 0 10px 24px rgba(23, 34, 62, .07);
  border-color: var(--brand-300, #a8c4fa);
}
.rc-band { width: 4px; height: 38px; border-radius: 999px; }
.rc-band[data-status='success'] { background: #40a35f; }
.rc-band[data-status='failed'] { background: var(--sev-severe, #dc4961); }
.rc-main { display: grid; gap: 5px; min-width: 0; }
.rc-line1 { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.rc-name { font-size: 13.5px; font-weight: 600; max-width: 420px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.rc-line2 { display: flex; gap: 14px; flex-wrap: wrap; font-size: 11px; color: var(--gray-500); }
.rc-project { max-width: 220px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.rc-score { display: grid; place-items: center; gap: 2px; min-width: 56px; }
.rc-ring { width: 38px; height: 38px; }
.rc-score span { font-size: 12.5px; font-weight: 700; }
.rc-actions { display: flex; gap: 4px; align-items: center; }

@media (prefers-reduced-motion: reduce) {
  .report-card { transition: none; }
  .report-card:hover { transform: none; }
}
@media (max-width: 760px) {
  .report-card { grid-template-columns: minmax(0, 1fr) auto; padding: 12px; }
  .rc-band { display: none; }
  .rc-score { min-width: 48px; }
  .rc-actions {
    grid-column: 1 / -1; flex-wrap: wrap;
    padding-top: 8px; border-top: 1px dashed var(--gray-100, #eef0f4);
  }
  .rc-name { max-width: 100%; }
  .rc-project { max-width: 60vw; }
}

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
