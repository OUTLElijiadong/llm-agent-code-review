<template>
  <div class="issue-hub-page">
    <div class="page-header">
      <div>
        <h2>问题追踪中心</h2>
        <p class="page-sub">跨项目统一查看所有审查问题，支持筛选与状态流转</p>
      </div>
      <el-button type="primary" @click="goReviewList">查看审查任务</el-button>
    </div>

    <el-card shadow="hover" class="filter-card">
      <el-alert
        v-if="projectLoadError"
        data-testid="issue-project-load-error"
        class="load-error"
        title="项目筛选选项加载失败"
        type="warning"
        :closable="false"
        show-icon
      >
        <span class="load-error-message">{{ projectLoadError }}</span>
        <el-button :loading="projectsLoading" @click="loadProjects">重新加载项目</el-button>
      </el-alert>
      <div class="filter-bar">
        <el-select
          v-model="filters.project_id"
          placeholder="项目"
          clearable
          style="width: 180px"
          @change="reload"
        >
          <el-option
            v-for="p in projects"
            :key="p.id"
            :label="p.project_name"
            :value="p.id"
          />
        </el-select>
        <el-select v-model="filters.severity" placeholder="严重程度" clearable style="width: 140px" @change="reload">
          <el-option label="严重" value="严重" />
          <el-option label="高" value="高" />
          <el-option label="中" value="中" />
          <el-option label="低" value="低" />
        </el-select>
        <el-select v-model="filters.issue_type" placeholder="问题类型" clearable style="width: 160px" @change="reload">
          <el-option label="代码规范" value="代码规范" />
          <el-option label="潜在Bug" value="潜在Bug" />
          <el-option label="安全漏洞" value="安全漏洞" />
          <el-option label="性能问题" value="性能问题" />
          <el-option label="异常处理" value="异常处理" />
          <el-option label="命名规范" value="命名规范" />
          <el-option label="可维护性" value="可维护性" />
          <el-option label="注释完整性" value="注释完整性" />
        </el-select>
        <el-select v-model="filters.status" placeholder="状态" clearable style="width: 140px" @change="reload">
          <el-option label="全部状态" value="all" />
          <el-option label="未修复" value="unfixed" />
          <el-option label="已修复" value="fixed" />
          <el-option label="已忽略" value="ignored" />
          <el-option label="待复查" value="pending_review" />
        </el-select>
        <el-input
          v-model="filters.keyword"
          placeholder="搜索标题或描述"
          clearable
          style="width: 220px"
          @input="reloadDebounced"
          @change="reload"
        />
        <el-button v-if="canBatchIssues" type="primary" :disabled="!selected.length" @click="onBatchMarkFixed">
          批量标记已修复 ({{ selected.length }})
        </el-button>
      </div>
    </el-card>

    <el-card shadow="hover">
      <el-alert
        v-if="issueLoadError"
        data-testid="issue-load-error"
        class="load-error"
        title="问题列表加载失败"
        type="error"
        :closable="false"
        show-icon
      >
        <span class="load-error-message">{{ issueLoadError }}</span>
        <el-button :loading="loading" @click="loadIssues">重新加载问题</el-button>
      </el-alert>
      <div class="issue-cards" v-loading="loading" role="list" data-testid="issue-cards">
        <EmptyState v-if="!rows.length && !loading && !issueLoadError" description="暂无问题" />
        <article
          v-for="row in rows"
          :key="row.id"
          class="issue-card"
          :class="{ 'is-expanded': expandedIds.has(row.id), 'has-selection': canBatchIssues }"
          role="listitem"
        >
          <label v-if="canBatchIssues" class="ic-check" @click.stop>
            <input
              type="checkbox"
              :checked="selected.some((i) => i.id === row.id)"
              :aria-label="`选择 ${row.title || '问题'}`"
              @change="toggleSelect(row)"
            >
          </label>
          <span class="ic-band" :data-severity="String(severityClass(row.severity))" aria-hidden="true"></span>
          <div class="ic-main">
            <div class="ic-line1">
              <el-tag :type="severityTag(row.severity)" size="small">{{ severityLabel(row.severity) }}</el-tag>
              <b class="ic-title" :title="row.title || ''">{{ row.title || `问题 #${row.id}` }}</b>
              <el-tag :type="statusTag(row.status)" size="small">{{ statusLabel(row.status) }}</el-tag>
              <el-tag size="small" type="info" effect="plain">{{ typeLabel(row.issue_type) }}</el-tag>
            </div>
            <div class="ic-line2 font-mono">
              <span class="ic-file" :title="`${row.file_name || '未知文件'}${row.line_number ? ':' + row.line_number : ''}`">
                {{ row.file_name || '未知文件' }}{{ row.line_number ? `:${row.line_number}` : '' }}
              </span>
              <span class="ic-project" :title="row.project_name">{{ row.project_name }}</span>
              <span :title="`所属任务 #${row.task_id}`">{{ row.task_name || `任务 #${row.task_id}` }}</span>
              <span :title="row.create_time">{{ formatDateTime(row.create_time, 'YYYY-MM-DD HH:mm') }}</span>
            </div>
            <div v-if="expandedIds.has(row.id)" :id="`issue-description-${row.id}`" class="ic-desc">
              <span class="ic-desc-label">问题描述</span>
              <p class="ic-desc-text">{{ row.description || '（无描述）' }}</p>
            </div>
          </div>
          <div class="ic-actions">
            <button
              type="button"
              class="ic-toggle"
              :aria-expanded="expandedIds.has(row.id)"
              :aria-controls="`issue-description-${row.id}`"
              @click="toggleExpand(row.id)"
            >
              <el-icon :class="{ 'is-open': expandedIds.has(row.id) }"><ArrowDown /></el-icon>
              {{ expandedIds.has(row.id) ? '收起' : '详情' }}
            </button>
            <el-button link type="primary" size="small" @click="onJump(row)">查看任务</el-button>
            <el-dropdown v-if="canHandleIssues" trigger="click" @command="(s: string) => onSetStatus(row, s)">
              <el-button link type="primary" size="small">改状态<el-icon><ArrowDown /></el-icon></el-button>
              <template #dropdown>
                <el-dropdown-menu>
                  <el-dropdown-item command="fixed">标记已修复</el-dropdown-item>
                  <el-dropdown-item command="ignored">标记已忽略</el-dropdown-item>
                  <el-dropdown-item command="pending_review">标记待复查</el-dropdown-item>
                  <el-dropdown-item command="unfixed">恢复未修复</el-dropdown-item>
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
          @change="loadIssues"
        />
      </div>
    </el-card>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { useRouter } from 'vue-router'

import EmptyState from '@/components/common/EmptyState.vue'
import { ArrowDown } from '@element-plus/icons-vue'
import { list as listIssues, updateStatus, batchUpdateStatus } from '@/api/issue'
import { getProjects } from '@/api/project'
import { formatDateTime } from '@/utils/format'
import type { IssueListItemOut } from '@/types/review'
import type { ProjectOut } from '@/types/project'
import { severityClass, severityDisplayLabel } from '@/constants/severity'
import { dimLabel } from '@/constants/dim'
import { ElMessageBox } from 'element-plus/es/components/message-box/index'
import { ElMessage } from 'element-plus/es/components/message/index'
import { useUserStore } from '@/stores/user'

const router = useRouter()
const userStore = useUserStore()
const canHandleIssues = computed(() => userStore.hasPermission('issue:handle'))
const canBatchIssues = computed(() => userStore.hasPermission('issue:batch'))

const loading = ref(false)
const projectsLoading = ref(false)
const projectLoadError = ref('')
const issueLoadError = ref('')
const rows = ref<IssueListItemOut[]>([])
const selected = ref<IssueListItemOut[]>([])
const total = ref(0)
const page = ref(1)
const pageSize = ref(20)
const projects = ref<ProjectOut[]>([])

const filters = reactive({
  project_id: undefined as number | undefined,
  severity: '',
  issue_type: '',
  status: '',
  keyword: '',
})

function severityTag(s: string): 'danger' | 'warning' | 'primary' | 'info' {
  // v2.0: 兼容中英文混存 (severityClass 把 high/medium/severe 归一化为 key)
  const key = severityClass(s)
  if (key === 'severe') return 'danger'
  if (key === 'high') return 'warning'
  if (key === 'medium') return 'primary'
  return 'info'
}

function severityLabel(s: string): string {
  return severityDisplayLabel(s)
}

function typeLabel(s: string): string {
  return dimLabel(s)
}

function statusTag(s: string): 'success' | 'info' | 'warning' | 'primary' {
  switch (s) {
    case 'fixed':
      return 'success'
    case 'ignored':
      return 'info'
    case 'pending_review':
      return 'warning'
    default:
      return 'primary'
  }
}

function statusLabel(s: string): string {
  return { unfixed: '未修复', fixed: '已修复', ignored: '已忽略', pending_review: '待复查' }[s] || s
}

async function loadProjects(): Promise<void> {
  if (projectsLoading.value) return
  projectsLoading.value = true
  try {
    const data = await getProjects({ page: 1, page_size: 100 })
    projects.value = data.items
    projectLoadError.value = ''
  } catch (error) {
    projectLoadError.value = requestErrorMessage(error, '暂时无法读取项目选项，请稍后重试。')
  } finally {
    projectsLoading.value = false
  }
}

function requestErrorMessage(error: unknown, fallback: string): string {
  if (error && typeof error === 'object' && 'message' in error && typeof error.message === 'string') {
    return error.message || fallback
  }
  return fallback
}

async function loadIssues(): Promise<void> {
  loading.value = true
  try {
    const data = await listIssues({
      project_id: filters.project_id,
      severity: filters.severity || undefined,
      issue_type: filters.issue_type || undefined,
      status: filters.status || undefined,
      keyword: filters.keyword || undefined,
      page: page.value,
      page_size: pageSize.value,
    })
    rows.value = data.items
    total.value = data.total
    issueLoadError.value = ''
    // 数据刷新后勾选可能指向已不存在的问题,按新列表收敛(与原表格 selection 随数据重置一致)
    selected.value = selected.value.filter((s) => rows.value.some((r) => r.id === s.id))
  } catch (error) {
    const message = requestErrorMessage(error, '暂时无法读取问题，请稍后重试。')
    issueLoadError.value = rows.value.length ? `${message} 当前显示上次成功加载的结果。` : message
  } finally {
    loading.value = false
  }
}

function reload(): void {
  page.value = 1
  loadIssues()
}

// 关键词搜索防抖:输入停顿 400ms 后才触发,避免每次击键都请求
let searchTimer: ReturnType<typeof setTimeout> | null = null
function reloadDebounced(): void {
  if (searchTimer) clearTimeout(searchTimer)
  searchTimer = setTimeout(() => {
    reload()
  }, 400)
}

/** 展开区(描述全文)状态:按问题 id 记录,默认全部收起 */
const expandedIds = ref<Set<number>>(new Set())

function toggleExpand(id: number): void {
  if (expandedIds.value.has(id)) expandedIds.value.delete(id)
  else expandedIds.value.add(id)
}

function toggleSelect(row: IssueListItemOut): void {
  if (!canBatchIssues.value) return
  const index = selected.value.findIndex((i) => i.id === row.id)
  if (index >= 0) selected.value.splice(index, 1)
  else selected.value.push(row)
}

async function onSetStatus(row: IssueListItemOut, status: string): Promise<void> {
  if (!canHandleIssues.value) return
  try {
    await updateStatus(row.id, { status })
    ElMessage.success('状态已更新')
    if (status === 'fixed' || status === 'ignored') {
      loadIssues()
    } else {
      row.status = status
    }
  } catch {
    ElMessage.error('状态更新失败')
  }
}

async function onBatchMarkFixed(): Promise<void> {
  if (!canBatchIssues.value) return
  if (!selected.value.length) return
  try {
    // 二次确认,避免误点一次批量改几十条状态
    await ElMessageBox.confirm(
      `确定将选中的 ${selected.value.length} 条问题标记为已修复吗?`,
      '批量标记已修复',
      { confirmButtonText: '确定', cancelButtonText: '取消', type: 'warning' },
    )
  } catch {
    return // 用户取消
  }
  try {
    await batchUpdateStatus({
      ids: selected.value.map((r) => r.id),
      status: 'fixed',
    })
    ElMessage.success(`已批量标记 ${selected.value.length} 条为已修复`)
    selected.value = []
    loadIssues()
  } catch {
    ElMessage.error('批量更新失败')
  }
}

function onJump(row: IssueListItemOut): void {
  router.push(`/reviews/${row.task_id}`)
}

function goReviewList(): void {
  router.push('/reviews')
}

onMounted(async () => {
  await Promise.all([loadProjects(), loadIssues()])
})
</script>

<style scoped lang="scss">
.issue-hub-page {
  min-width: 0;
}

.page-header {
  display: flex;
  justify-content: space-between;
  align-items: flex-end;
  margin-bottom: var(--spacing-lg);
  gap: var(--spacing-md);
  flex-wrap: wrap;

  h2 {
    margin: 0 0 4px;
    font-size: 20px;
    font-weight: 600;
  }

  .page-sub {
    margin: 0;
    color: var(--color-text-secondary, #909399);
    font-size: 13px;
  }
}

.filter-card {
  margin-bottom: var(--spacing-md);
}

.load-error {
  margin-bottom: var(--spacing-md);
}

.load-error-message {
  display: block;
  margin-bottom: var(--spacing-sm);
}

.filter-bar {
  display: flex;
  gap: 12px;
  flex-wrap: wrap;
  align-items: center;
}

.pagination-wrapper {
  display: flex;
  justify-content: flex-end;
  margin-top: 16px;
}

/* ── 问题卡片列表(替代表格:标题为主行,文件/任务/时间为次行,描述全文进展开区) ── */
.issue-cards {
  container-type: inline-size;
  display: grid;
  gap: 10px;
  min-height: 120px;
}

.issue-card {
  display: grid;
  grid-template-columns: 4px minmax(0, 1fr) auto;
  grid-template-areas: 'band main actions';
  gap: 14px;
  align-items: start;
  padding: 13px 16px 13px 12px;
  border-radius: 12px;
  background: var(--el-bg-color, #fff);
  border: 1px solid var(--gray-100, #eef0f4);
  transition: transform .16s ease, box-shadow .16s ease, border-color .16s ease;
}

/* 勾选框按权限存在，显式命名列避免普通用户的正文被放入 auto 列。 */
.issue-card.has-selection {
  grid-template-columns: 16px 4px minmax(0, 1fr) auto;
  grid-template-areas: 'check band main actions';
}

.issue-card:hover {
  transform: translateY(-1.5px);
  box-shadow: 0 10px 24px rgba(23, 34, 62, .07);
  border-color: var(--brand-300, #a8c4fa);
}

.ic-check {
  grid-area: check;
  display: grid;
  place-items: center;
  padding-top: 3px;
  cursor: pointer;
}

.ic-check input {
  width: 15px;
  height: 15px;
  accent-color: var(--brand-500, #4078f4);
  cursor: pointer;
}

/* 严重度色带:危急红/高黄/中蓝/低灰,随展开区一起拉伸 */
.ic-band {
  grid-area: band;
  width: 4px;
  align-self: stretch;
  margin: 2px 0;
  border-radius: 999px;
}

.ic-band[data-severity='severe'] { background: var(--sev-severe, #dc4961); }
.ic-band[data-severity='high'] { background: #e6a23c; }
.ic-band[data-severity='medium'] { background: var(--brand-500, #4078f4); }
.ic-band[data-severity='low'] { background: var(--gray-300, #cfd4dc); }

.ic-main {
  grid-area: main;
  display: grid;
  grid-template-columns: minmax(0, 1fr);
  gap: 6px;
  min-width: 0;
}

.ic-line1 {
  display: flex;
  align-items: flex-end;
  min-height: 24px;
  gap: 8px;
  flex-wrap: wrap;
}

.ic-title {
  font-size: 13.5px;
  font-weight: 600;
  max-width: min(100%, 420px);
  min-width: 0;
  line-height: 20px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.ic-toggle {
  display: inline-flex;
  align-items: center;
  gap: 2px;
  justify-content: center;
  padding: 0;
  width: 48px;
  height: 24px;
  font-size: 12px;
  color: var(--gray-500);
  background: transparent;
  border: none;
  border-radius: 6px;
  cursor: pointer;
}

.ic-toggle:hover,
.ic-toggle:focus-visible {
  color: var(--brand-600, #2f5ce0);
  background: var(--gray-100, #eef0f4);
}

.ic-toggle .el-icon { transition: transform .18s ease; }
.ic-toggle .el-icon.is-open { transform: rotate(180deg); }

.ic-line2 {
  display: flex;
  gap: 14px;
  flex-wrap: wrap;
  font-size: 11px;
  color: var(--gray-500);

  > span {
    min-width: 0;
    max-width: 100%;
    overflow-wrap: anywhere;
  }
}

.ic-line2 .ic-file,
.ic-line2 .ic-project {
  max-width: min(100%, 240px);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.ic-desc {
  display: grid;
  gap: 4px;
  padding: 8px 10px;
  border-radius: 8px;
  background: var(--gray-50, #f7f8fa);
  border: 1px dashed var(--gray-200, #e3e6eb);
}

.ic-desc-label {
  font-size: 11px;
  color: var(--gray-400);
}

.ic-desc-text {
  margin: 0;
  font-size: 12.5px;
  line-height: 1.65;
  color: var(--gray-700, #4e5969);
  white-space: pre-wrap;
  word-break: break-word;
}

.ic-actions {
  grid-area: actions;
  display: flex;
  align-items: flex-end;
  gap: 12px;
  min-width: 0;
  white-space: nowrap;

  :deep(.el-button) {
    margin: 0;
    min-height: 24px;
    padding: 2px 0;
  }
}

@media (prefers-reduced-motion: reduce) {
  .issue-card,
  .ic-toggle .el-icon {
    transition: none;
  }

  .issue-card:hover {
    transform: none;
  }
}

/* 以卡片容器宽度响应，桌面收起/展开侧栏后也保持可用。 */
@container (max-width: 680px) {
  .issue-card {
    grid-template-columns: 4px minmax(0, 1fr);
    grid-template-areas: 'band main' 'band actions';
    gap: 10px;
  }

  .issue-card.has-selection {
    grid-template-columns: 16px 4px minmax(0, 1fr);
    grid-template-areas: 'check band main' '. band actions';
  }

  .ic-actions {
    flex-wrap: wrap;
    justify-content: flex-start;
    padding-top: 8px;
    border-top: 1px solid var(--gray-100, #eef0f4);
  }
}

@media (max-width: 768px) {
  .ic-toggle,
  .ic-actions :deep(.el-button) {
    min-height: 40px;
  }
}
</style>
