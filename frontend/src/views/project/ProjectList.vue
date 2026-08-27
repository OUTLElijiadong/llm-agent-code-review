<template>
  <div class="project-list">
    <!-- ============ 页头 ============ -->
    <header class="page-head">
      <div>
        <h1 class="page-title font-display">项目管理</h1>
        <p class="page-sub">共 <b class="hl">{{ total }}</b> 个项目<template v-if="showStatusSplit"> · {{ activeCount }} 活跃 · {{ archivedCount }} 归档</template></p>
      </div>
      <div class="page-actions">
        <div class="view-switch" role="group" aria-label="项目视图">
          <button
            type="button"
            class="view-btn"
            :class="{ active: view === 'table' }"
            :aria-pressed="view === 'table'"
            aria-controls="project-table-view"
            @click="view = 'table'"
          >
            <span class="ico">☰</span><span>表格</span>
          </button>
          <button
            type="button"
            class="view-btn"
            :class="{ active: view === 'card' }"
            :aria-pressed="view === 'card'"
            aria-controls="project-card-view"
            @click="view = 'card'"
          >
            <span class="ico">▦</span><span>卡片</span>
          </button>
        </div>
        <el-button
          v-if="userStore.hasPermission('project:import')"
          data-testid="remote-import-button"
          :icon="Connection"
          @click="remoteVisible = true"
        >远程导入</el-button>
        <el-button type="primary" :icon="Plus" @click="handleCreate">新建项目</el-button>
      </div>
    </header>

    <section
      v-if="remoteImportTask || remoteImportRecord || remoteImportError"
      class="remote-import-feedback"
      data-testid="remote-import-status"
      :class="{
        'is-error': Boolean(remoteImportError) || remoteImportTask?.status === 'failed',
        'is-success': remoteImportTask?.status === 'succeeded',
        'is-cancelled': remoteImportTask?.status === 'cancelled',
      }"
    >
      <span class="remote-import-feedback-dot" aria-hidden="true"></span>
      <div class="remote-import-feedback-copy">
        <strong>{{ remoteImportStatusTitle }}</strong>
        <p>{{ remoteImportStatusDescription }}</p>
      </div>
      <el-button
        v-if="canCancelRemoteImport"
        data-testid="remote-import-cancel"
        type="danger"
        plain
        size="small"
        :loading="remoteImportCancelling"
        :disabled="remoteImportCancelling"
        @click="cancelCurrentRemoteImport"
      >取消导入</el-button>
      <el-button
        v-if="remoteImportRecord && remoteImportError"
        size="small"
        :loading="remoteImportRetrying"
        :disabled="remoteImportCancelling"
        @click="retryRemoteImport"
      >继续处理</el-button>
      <el-button
        v-else-if="remoteImportError || remoteImportTask?.status === 'cancelled'"
        size="small"
        @click="dismissRemoteImportFeedback"
      >关闭</el-button>
    </section>

    <!-- ============ 筛选条 ============ -->
    <section class="filter-bar">
      <el-input
        v-model="keyword"
        placeholder="搜索项目名称或描述"
        clearable
        :prefix-icon="Search"
        class="search-input"
        @keyup.enter="handleSearch"
        @clear="handleSearch"
      />
      <el-select v-model="languageFilter" placeholder="语言" clearable class="filter-select" @change="handleSearch">
        <el-option label="Python" value="python" />
        <el-option label="JavaScript" value="javascript" />
        <el-option label="TypeScript" value="typescript" />
        <el-option label="Java" value="java" />
        <el-option label="Go" value="go" />
        <el-option label="C++" value="cpp" />
      </el-select>
      <el-select v-model="statusFilter" placeholder="状态" clearable class="filter-select" @change="handleSearch">
        <el-option label="活跃" value="active" />
        <el-option label="归档" value="archived" />
      </el-select>
      <el-button @click="handleReset">重置</el-button>
      <div class="filter-spacer"></div>
      <span class="filter-result font-mono">{{ projects.length }} / {{ total }} 条</span>
    </section>

    <!-- ============ 表格视图 ============ -->
    <section id="project-table-view" v-show="view === 'table'" class="table-card" v-loading="loading">
      <table class="prism-table">
        <thead>
          <tr>
            <th class="col-name">项目名称</th>
            <th class="col-lang">语言</th>
            <th class="col-status">状态</th>
            <th class="col-score">评分</th>
            <th class="col-files">文件</th>
            <th class="col-runs">Agent 运转</th>
            <th class="col-last">最近审查</th>
            <th class="col-create">创建时间</th>
            <th class="col-act">操作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="row in projects" :key="row.id" @click="handleView(row)" tabindex="0" role="button" @keyup.enter="handleView(row)">
            <td>
              <div class="cell-name">
                <span class="proj-avatar" :style="{ background: languageColor(row.language) }">
                  {{ initials(row.project_name) }}
                </span>
                <div class="proj-meta">
                  <span class="proj-name">{{ row.project_name }}</span>
                  <span class="proj-desc">{{ row.description || '—' }}</span>
                </div>
              </div>
            </td>
            <td>
              <span v-if="row.language" class="lang-chip font-mono">{{ row.language }}</span>
              <span v-else class="muted">-</span>
            </td>
            <td>
              <span class="status-pill" :class="`s-${row.status}`">
                <span class="pill-dot"></span>{{ row.status === 'active' ? '活跃' : '归档' }}
              </span>
            </td>
            <td>
              <div v-if="hasRealScore(row)" class="mini-gauge" :title="`评分 ${displayScore(row)}`">
                <svg viewBox="0 0 36 36" class="gauge-svg">
                  <circle cx="18" cy="18" r="14" fill="none" stroke="var(--gray-100)" stroke-width="3"/>
                  <circle
                    cx="18" cy="18" r="14" fill="none"
                    :stroke="scoreColor(displayScore(row))" stroke-width="3"
                    stroke-linecap="round"
                    :stroke-dasharray="`${displayScore(row) * 0.88} 100`"
                    transform="rotate(-90 18 18)"
                  />
                </svg>
                <span class="gauge-text font-mono" :style="{ color: scoreColor(displayScore(row)) }">
                  {{ displayScore(row) }}
                </span>
              </div>
              <span v-else class="muted font-mono" title="尚未审查或后端未返回评分">—</span>
            </td>
            <td>
              <span class="file-count font-mono">{{ row.file_count }}</span>
            </td>
            <td>
              <span v-if="(row.agent_run_count ?? 0) > 0" class="font-mono muted-2" :title="formatDate(row.last_agent_run_at ?? undefined) || ''">
                {{ row.agent_run_count }} 次
              </span>
              <span v-else class="muted font-mono">—</span>
            </td>
            <td>
              <span v-if="row.last_review_at" class="font-mono muted-2">{{ formatDate(row.last_review_at) }}</span>
              <span v-else class="muted font-mono">暂未审查</span>
            </td>
            <td>
              <span class="font-mono muted-2">{{ formatDate(row.create_time) }}</span>
            </td>
            <td class="col-act" @click.stop>
              <el-button link type="primary" @click="handleView(row)">详情</el-button>
              <el-button v-if="row.can_update" link type="primary" @click="handleEdit(row)">编辑</el-button>
              <el-button v-if="row.can_delete" link type="danger" @click="handleDelete(row.id)">删除</el-button>
            </td>
          </tr>
          <tr v-if="!loading && projects.length === 0">
            <td colspan="9">
              <EmptyState description="还没有项目，点击右上角新建一个吧">
                <el-button type="primary" @click="handleCreate">+ 新建项目</el-button>
              </EmptyState>
            </td>
          </tr>
        </tbody>
      </table>
    </section>

    <!-- ============ 卡片视图 ============ -->
    <section id="project-card-view" v-show="view === 'card'" class="card-grid" v-loading="loading">
      <article
        v-for="row in projects"
        :key="row.id"
        class="proj-card"
        @click="handleView(row)" tabindex="0" role="button" @keyup.enter="handleView(row)"
      >
        <header class="proj-card-head">
          <span class="proj-avatar lg" :style="{ background: languageColor(row.language) }">
            {{ initials(row.project_name) }}
          </span>
          <div class="head-meta">
            <div class="card-name">{{ row.project_name }}</div>
            <div class="card-sub font-mono">{{ row.language || 'unknown' }} · {{ row.file_count }} files</div>
          </div>
          <span class="status-pill" :class="`s-${row.status}`">
            <span class="pill-dot"></span>{{ row.status === 'active' ? '活跃' : '归档' }}
          </span>
        </header>

        <p class="card-desc">{{ row.description || '暂无描述' }}</p>

        <div class="card-spectrum spectrum-bar">
          <div></div><div></div><div></div><div></div>
          <div></div><div></div><div></div><div></div>
        </div>

        <footer class="card-foot">
          <div class="foot-meta">
            <span class="font-mono">{{ formatDate(row.last_review_at) || '暂未审查' }}</span>
            <span v-if="(row.agent_run_count ?? 0) > 0" class="font-mono runs-badge" :title="`最近运转 ${formatDate(row.last_agent_run_at ?? undefined) || ''}`">
              Agent 运转 {{ row.agent_run_count }} 次
            </span>
          </div>
          <div v-if="row.can_update || row.can_delete" class="card-actions" @click.stop>
            <el-tooltip v-if="row.can_update" content="编辑项目" placement="top">
              <el-button
                class="card-action"
                link
                type="primary"
                :icon="EditIcon"
                aria-label="编辑项目"
                @click="handleEdit(row)"
              />
            </el-tooltip>
            <el-tooltip v-if="row.can_delete" content="删除项目" placement="top">
              <el-button
                class="card-action"
                link
                type="danger"
                :icon="DeleteIcon"
                aria-label="删除项目"
                @click="handleDelete(row.id)"
              />
            </el-tooltip>
          </div>
          <div class="mini-gauge sm">
            <svg viewBox="0 0 36 36" class="gauge-svg">
              <circle cx="18" cy="18" r="14" fill="none" stroke="var(--gray-100)" stroke-width="3"/>
              <circle
                cx="18" cy="18" r="14" fill="none"
                :stroke="scoreColor(displayScore(row))" stroke-width="3"
                stroke-linecap="round"
                :stroke-dasharray="`${displayScore(row) * 0.88} 100`"
                transform="rotate(-90 18 18)"
              />
            </svg>
            <span class="gauge-text font-mono" :style="{ color: scoreColor(displayScore(row)) }">
              {{ displayScore(row) }}
            </span>
          </div>
        </footer>
      </article>

      <div v-if="!loading && projects.length === 0" class="card-empty">
        <EmptyState description="还没有项目">
          <el-button type="primary" @click="handleCreate">+ 新建项目</el-button>
        </EmptyState>
      </div>
    </section>

    <!-- ============ 分页 ============ -->
    <div v-if="total > 0" class="pagination-wrap">
      <el-pagination
        v-model:current-page="page"
        v-model:page-size="pageSize"
        :total="total"
        :page-sizes="[10, 20, 50]"
        layout="total, sizes, prev, pager, next, jumper"
        @size-change="fetchProjects"
        @current-change="fetchProjects"
      />
    </div>

    <ProjectForm
      v-model:visible="formVisible"
      :mode="formMode"
      :initial-data="editingProject"
      @submit="onFormSubmit"
    />

    <el-dialog v-model="remoteVisible" title="远程导入源码" width="560px" :close-on-click-modal="false">
      <el-form label-position="top">
        <el-form-item label="源码归档 HTTPS 地址" required>
          <el-input v-model="remoteForm.url" placeholder="https://example.com/project.zip" clearable />
        </el-form-item>
        <el-form-item label="项目名称" required>
          <el-input v-model="remoteForm.project_name" maxlength="100" show-word-limit />
        </el-form-item>
        <el-form-item label="描述">
          <el-input v-model="remoteForm.description" type="textarea" :rows="2" maxlength="500" />
        </el-form-item>
        <el-form-item label="导入模式">
          <el-checkbox v-model="remoteForm.audit_mode">隔离整包审计</el-checkbox>
        </el-form-item>
      </el-form>
      <el-alert
        title="仅接受公开 HTTPS 归档，服务器会先校验地址和压缩包路径，再执行恶意软件扫描。"
        type="info"
        :closable="false"
        show-icon
      />
      <template #footer>
        <el-button @click="remoteVisible = false">取消</el-button>
        <el-button type="primary" :loading="remoteLoading" @click="submitRemoteImport">开始导入</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted, onBeforeUnmount } from 'vue'
import { useRouter } from 'vue-router'
import { Connection, Delete as DeleteIcon, Edit as EditIcon, Plus, Search } from '@element-plus/icons-vue'

import dayjs from 'dayjs'
import {
  getProjects,
  deleteProject,
  createProject,
  updateProject,
  queueRemoteProjectImport,
  getRemoteProjectImport,
  cancelRemoteProjectImport,
} from '@/api/project'
import { uploadFolder } from '@/api/codeFile'
import { useUserStore } from '@/stores/user'
import type { ProjectOut } from '@/types/project'
import type {
  RemoteProjectImportInput,
  RemoteProjectImportTask,
} from '@/api/project'
import ProjectForm from './ProjectForm.vue'
import EmptyState from '@/components/common/EmptyState.vue'
import { ElMessage } from 'element-plus/es/components/message/index'
import { confirmDanger } from '@/composables/useDangerConfirm'

const router = useRouter()
const userStore = useUserStore()

const view = ref<'table' | 'card'>('table')

const loading = ref(false)
const projects = ref<ProjectOut[]>([])
const total = ref(0)
const page = ref(1)
const pageSize = ref(10)
const keyword = ref('')
const languageFilter = ref('')
const statusFilter = ref('')

const formVisible = ref(false)
const formMode = ref<'create' | 'edit'>('create')
const editingProject = ref<ProjectOut | null>(null)
const remoteVisible = ref(false)
const remoteLoading = ref(false)
const remoteForm = ref({ url: '', project_name: '', description: '', audit_mode: false })

const REMOTE_IMPORT_STORAGE_KEY = 'prism:remote-import-task'
const REMOTE_IMPORT_RECORD_VERSION = 1
const REMOTE_IMPORT_POLL_INTERVAL_MS = 1000
const REMOTE_IMPORT_MAX_RETRY_DELAY_MS = 5000
const remoteImportTask = ref<RemoteProjectImportTask | null>(null)
const remoteImportRecord = ref<PersistedRemoteImport | null>(null)
const remoteImportError = ref('')
const remoteImportRetrying = ref(false)
const remoteImportCancelling = ref(false)

interface PersistedRemoteImport {
  version: number
  taskId: string | null
  idempotencyKey: string
  payload: RemoteProjectImportInput
}

const remoteImportStatusTitle = computed(() => {
  if (remoteImportError.value) return '远程导入暂时中断'
  const status = remoteImportTask.value?.status
  if (status === 'queued') return '远程导入已排队'
  if (isActiveRemoteImportStatus(status)) return '远程导入处理中'
  if (status === 'succeeded') return '远程导入完成'
  if (status === 'failed') return '远程导入失败'
  if (status === 'cancelled') return '远程导入已取消'
  return '远程导入准备中'
})

const canCancelRemoteImport = computed(() => Boolean(
  remoteImportRecord.value?.taskId
  && remoteImportTask.value
  && isActiveRemoteImportStatus(remoteImportTask.value.status),
))

const remoteImportStatusDescription = computed(() => {
  if (remoteImportError.value) {
    return remoteImportRecord.value
      ? `${remoteImportError.value}；任务记录已保留，恢复网络后可以继续处理。`
      : `${remoteImportError.value}；可以重新提交远程导入。`
  }
  const task = remoteImportTask.value
  if (!task) return '正在提交任务，请稍候。'
  if (task.status === 'queued') return '任务已提交，正在等待服务器处理。'
  if (isActiveRemoteImportStatus(task.status)) {
    const phase = remoteImportPhaseLabel(task.result?.progress?.phase || task.status)
    const progress = formatRemoteImportProgress(task.result?.progress)
    return `${phase}${progress ? ` · ${progress}` : ''}`
  }
  if (task.status === 'succeeded') {
    const count = task.result?.file_count
    return typeof count === 'number' ? `已导入 ${count} 个文件，正在打开项目。` : '项目已创建，正在打开项目。'
  }
  if (task.status === 'cancelled') {
    return task.cancel_reason || task.error?.message || '任务已取消，未继续写入项目。'
  }
  return task.error?.message || '服务器未提供具体失败原因，请重新提交。'
})

const activeCount = computed(() => projects.value.filter((p) => p.status === 'active').length)
const archivedCount = computed(() => projects.value.filter((p) => p.status === 'archived').length)
/** 活跃/归档拆分仅统计当前页;多页时展示会与总数自相矛盾,此时隐藏拆分。 */
const showStatusSplit = computed(() => total.value <= projects.value.length)

function formatDate(dateStr?: string): string {
  if (!dateStr) return ''
  return dayjs(dateStr).format('YYYY-MM-DD HH:mm')
}

function initials(name: string): string {
  if (!name) return '·'
  const m = name.match(/[A-Za-z一-鿿]/)
  return (m ? m[0] : name[0]).toUpperCase()
}

const langPalette: Record<string, string> = {
  python:     'linear-gradient(135deg,#4B9BFF,#2BBFB9)',
  javascript: 'linear-gradient(135deg,#D4A53A,#E08648)',
  typescript: 'linear-gradient(135deg,#4B9BFF,#5B58E8)',
  java:       'linear-gradient(135deg,#E27C4A,#E25C73)',
  go:         'linear-gradient(135deg,#2BBFB9,#4FB87A)',
  cpp:        'linear-gradient(135deg,#B85AC4,#5B58E8)',
}

function languageColor(lang?: string): string {
  if (!lang) return 'linear-gradient(135deg,#6E7689,#9BA3B0)'
  return langPalette[lang.toLowerCase()] ?? 'linear-gradient(135deg,#5B58E8,#8E88F5)'
}

function displayScore(row: ProjectOut): number {
  // v2.0: 必须来自后端真实评分,不再用 id hash 派生假数字
  if (typeof row.score === 'number') return Math.round(row.score)
  return 0
}

function hasRealScore(row: ProjectOut): boolean {
  return typeof row.score === 'number'
}

function scoreColor(score: number): string {
  if (score === 0) return 'var(--gray-300)'
  if (score >= 85) return 'var(--status-fixed)'
  if (score >= 70) return 'var(--sev-medium)'
  if (score >= 60) return 'var(--sev-high)'
  return 'var(--sev-severe)'
}

function newRemoteImportIdempotencyKey(): string {
  const randomId = typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function'
    ? crypto.randomUUID()
    : `${Date.now()}-${Math.random().toString(36).slice(2)}`
  return `prism-remote-import-${randomId}`
}

function persistRemoteImport(record: PersistedRemoteImport): void {
  remoteImportRecord.value = record
  try {
    window.localStorage.setItem(REMOTE_IMPORT_STORAGE_KEY, JSON.stringify(record))
  } catch {
    // 本地存储不可用时仍继续当前任务,避免因浏览器策略阻断导入请求。
  }
}

function clearPersistedRemoteImport(): void {
  remoteImportRecord.value = null
  try {
    window.localStorage.removeItem(REMOTE_IMPORT_STORAGE_KEY)
  } catch {
    // 清理失败不影响已完成任务的当前页面状态。
  }
}

function readPersistedRemoteImport(): PersistedRemoteImport | null {
  let raw: string | null
  try {
    raw = window.localStorage.getItem(REMOTE_IMPORT_STORAGE_KEY)
  } catch {
    return null
  }
  if (!raw) return null
  try {
    const parsed = JSON.parse(raw) as Partial<PersistedRemoteImport>
    const payload = parsed.payload
    if (
      parsed.version !== REMOTE_IMPORT_RECORD_VERSION
      || typeof parsed.idempotencyKey !== 'string'
      || !parsed.idempotencyKey
      || (parsed.taskId !== null && typeof parsed.taskId !== 'string')
      || !payload
      || typeof payload.url !== 'string'
      || typeof payload.project_name !== 'string'
    ) {
      window.localStorage.removeItem(REMOTE_IMPORT_STORAGE_KEY)
      return null
    }
    return {
      version: REMOTE_IMPORT_RECORD_VERSION,
      taskId: parsed.taskId ?? null,
      idempotencyKey: parsed.idempotencyKey,
      payload: {
        url: payload.url,
        project_name: payload.project_name,
        description: typeof payload.description === 'string' ? payload.description : undefined,
        language: typeof payload.language === 'string' ? payload.language : undefined,
        audit_mode: Boolean(payload.audit_mode),
      },
    }
  } catch {
    try {
      window.localStorage.removeItem(REMOTE_IMPORT_STORAGE_KEY)
    } catch {
      // 忽略损坏记录的清理失败,当前页面仍可继续正常使用。
    }
    return null
  }
}

function isTransientRemoteImportError(error: unknown): boolean {
  if (!error || typeof error !== 'object') return true
  const candidate = error as {
    response?: { status?: unknown }
    code?: unknown
  }
  if (candidate.response) {
    const status = candidate.response.status
    if (typeof status !== 'number') return true
    return status >= 500 || status === 408 || status === 429
  }
  if (typeof candidate.code === 'number') {
    // 后端错误处理器会把 5xx/429 转成 50000/502xx/503xx/429xx 业务码。
    return candidate.code >= 50000
      || (candidate.code >= 42900 && candidate.code < 43000)
      || candidate.code === 429
      || candidate.code === 500
  }
  if (typeof candidate.code === 'string') {
    return new Set([
      'ERR_NETWORK',
      'ECONNABORTED',
      'ETIMEDOUT',
      'ECONNRESET',
      'ERR_BAD_RESPONSE',
    ]).has(candidate.code)
  }
  // 通用 Error 和无响应的 Axios 错误都属于可恢复的连接/传输失败。
  return true
}

function readableRemoteImportError(error: unknown): string {
  if (error && typeof error === 'object') {
    const candidate = error as {
      message?: unknown
      detail?: unknown
      error?: { message?: unknown } | unknown
      response?: { data?: { message?: unknown; detail?: unknown } }
    }
    const responseData = candidate.response?.data
    const values = [
      candidate.error && typeof candidate.error === 'object'
        ? (candidate.error as { message?: unknown }).message
        : undefined,
      candidate.message,
      typeof candidate.detail === 'string' ? candidate.detail : undefined,
      responseData?.message,
      typeof responseData?.detail === 'string' ? responseData.detail : undefined,
    ]
    const message = values.find((value): value is string => typeof value === 'string' && value.trim().length > 0)
    if (message) return message.trim()
  }
  if (error instanceof Error && error.message.trim()) return error.message.trim()
  return '远程导入请求失败，请稍后重试。'
}

function remoteImportPhaseLabel(phase?: string): string {
  const labels: Record<string, string> = {
    downloading: '正在下载源码',
    validating: '正在校验压缩包',
    ingesting: '正在写入项目',
    importing: '正在写入项目',
    scanning: '正在安全扫描',
    project_created: '正在准备项目',
  }
  return labels[phase || ''] || '正在处理源码'
}

function isRemoteImportTask(value: unknown): value is RemoteProjectImportTask {
  if (!value || typeof value !== 'object') return false
  const candidate = value as { task_id?: unknown; status?: unknown }
  return typeof candidate.task_id === 'string'
    && [
      'queued',
      'running',
      'downloading',
      'scanning',
      'ingesting',
      'succeeded',
      'failed',
      'cancelled',
    ].includes(String(candidate.status))
}

function isActiveRemoteImportStatus(status?: RemoteProjectImportTask['status']): boolean {
  return ['queued', 'running', 'downloading', 'scanning', 'ingesting'].includes(status || '')
}

function formatRemoteImportProgress(progress?: RemoteProjectImportTask['result']['progress']): string {
  if (!progress || typeof progress.received_bytes !== 'number') return ''
  const received = formatBytes(progress.received_bytes)
  if (typeof progress.total_bytes === 'number' && progress.total_bytes > 0) {
    return `${received} / ${formatBytes(progress.total_bytes)}`
  }
  return received
}

function formatBytes(value: number): string {
  if (value < 1024) return `${value} B`
  if (value < 1024 * 1024) return `${Math.round(value / 1024)} KB`
  return `${(value / (1024 * 1024)).toFixed(1)} MB`
}

function remoteImportProjectId(task: RemoteProjectImportTask): number | null {
  const value = task.project_id ?? task.result?.id
  if (typeof value === 'number' && Number.isFinite(value)) return value
  if (typeof value === 'string' && value.trim() && Number.isFinite(Number(value))) return Number(value)
  return null
}

function clearRemoteImportPoll(): void {
  if (remoteImportPollTimer !== undefined) {
    clearTimeout(remoteImportPollTimer)
    remoteImportPollTimer = undefined
  }
}

function scheduleRemoteImportPoll(delay = REMOTE_IMPORT_POLL_INTERVAL_MS): void {
  clearRemoteImportPoll()
  if (componentDisposed || !remoteImportRecord.value?.taskId) return
  remoteImportPollTimer = setTimeout(() => {
    remoteImportPollTimer = undefined
    void pollRemoteImportTask()
  }, delay)
}

async function settleRemoteImportTask(task: RemoteProjectImportTask): Promise<void> {
  remoteImportTask.value = task
  if (isActiveRemoteImportStatus(task.status)) {
    scheduleRemoteImportPoll()
    return
  }

  clearRemoteImportPoll()
  if (task.status === 'failed') {
    clearPersistedRemoteImport()
    remoteImportError.value = task.error?.message || '服务器未提供具体失败原因，请重新提交。'
    return
  }

  if (task.status === 'cancelled') {
    clearPersistedRemoteImport()
    remoteImportError.value = ''
    ElMessage.info(task.cancel_reason || task.error?.message || '远程导入已取消')
    return
  }

  remoteImportError.value = ''
  const projectId = remoteImportProjectId(task)
  if (projectId === null) {
    remoteImportError.value = '远程导入已完成，但服务器没有返回项目编号。'
    return
  }
  clearPersistedRemoteImport()
  const count = task.result?.file_count
  ElMessage.success(
    typeof count === 'number' ? `远程源码导入完成，共 ${count} 个文件` : '远程源码导入完成',
  )
  await fetchProjects()
  if (!componentDisposed) await router.push(`/projects/${projectId}`)
}

async function pollRemoteImportTask(): Promise<void> {
  const record = remoteImportRecord.value
  if (componentDisposed || !record?.taskId || remoteImportPollInFlight) return
  remoteImportPollInFlight = true
  try {
    const task = await getRemoteProjectImport(record.taskId)
    if (componentDisposed) return
    if (!isRemoteImportTask(task)) throw new Error('服务器返回的远程导入任务状态无效')
    remoteImportPollFailures = 0
    remoteImportError.value = ''
    await settleRemoteImportTask(task)
  } catch (error) {
    remoteImportPollFailures += 1
    remoteImportError.value = readableRemoteImportError(error)
    if (isTransientRemoteImportError(error)) {
      const delay = Math.min(
        REMOTE_IMPORT_MAX_RETRY_DELAY_MS,
        REMOTE_IMPORT_POLL_INTERVAL_MS * (2 ** Math.min(remoteImportPollFailures - 1, 3)),
      )
      scheduleRemoteImportPoll(delay)
    } else {
      clearRemoteImportPoll()
      clearPersistedRemoteImport()
    }
  } finally {
    remoteImportPollInFlight = false
  }
}

async function enqueuePersistedRemoteImport(record: PersistedRemoteImport, isRetry = false): Promise<void> {
  if (!userStore.hasPermission('project:import')) return
  if (isRetry) remoteImportRetrying.value = true
  else remoteLoading.value = true
  try {
    const task = await queueRemoteProjectImport(record.payload, record.idempotencyKey)
    if (componentDisposed) return
    if (!isRemoteImportTask(task)) throw new Error('服务器返回的远程导入任务状态无效')
    remoteImportTask.value = task
    remoteImportError.value = ''
    persistRemoteImport({ ...record, taskId: task.task_id })
    remoteVisible.value = false
    remoteForm.value = { url: '', project_name: '', description: '', audit_mode: false }
    if (isActiveRemoteImportStatus(task.status)) {
      scheduleRemoteImportPoll()
    } else {
      await settleRemoteImportTask(task)
    }
  } catch (error) {
    remoteImportError.value = readableRemoteImportError(error)
    if (!isTransientRemoteImportError(error)) {
      clearPersistedRemoteImport()
      remoteImportTask.value = null
    }
  } finally {
    if (isRetry) remoteImportRetrying.value = false
    else remoteLoading.value = false
  }
}

async function restoreRemoteImport(): Promise<void> {
  if (!userStore.hasPermission('project:import')) return
  const record = readPersistedRemoteImport()
  if (!record) return
  remoteImportRecord.value = record
  if (record.taskId) {
    await pollRemoteImportTask()
  } else {
    await enqueuePersistedRemoteImport(record, true)
  }
}

async function retryRemoteImport(): Promise<void> {
  const record = remoteImportRecord.value
  if (
    !record
    || remoteImportRetrying.value
    || remoteLoading.value
    || !userStore.hasPermission('project:import')
  ) return
  remoteImportError.value = ''
  remoteImportPollFailures = 0
  if (record.taskId) await pollRemoteImportTask()
  else await enqueuePersistedRemoteImport(record, true)
}

async function cancelCurrentRemoteImport(): Promise<void> {
  const taskId = remoteImportRecord.value?.taskId
  if (!taskId || remoteImportCancelling.value || !canCancelRemoteImport.value) return
  remoteImportCancelling.value = true
  clearRemoteImportPoll()
  try {
    const task = await cancelRemoteProjectImport(taskId, '用户在项目页取消远程导入')
    if (componentDisposed) return
    if (!isRemoteImportTask(task)) throw new Error('服务器返回的远程导入任务状态无效')
    await settleRemoteImportTask(task)
  } catch (error) {
    remoteImportError.value = readableRemoteImportError(error)
    scheduleRemoteImportPoll()
  } finally {
    remoteImportCancelling.value = false
  }
}

function dismissRemoteImportFeedback(): void {
  if (remoteImportRecord.value) return
  remoteImportError.value = ''
  remoteImportTask.value = null
}

async function fetchProjects(): Promise<void> {
  loading.value = true
  try {
    const params: Record<string, unknown> = {
      page: page.value,
      page_size: pageSize.value,
    }
    if (keyword.value) params.keyword = keyword.value
    if (languageFilter.value) params.language = languageFilter.value
    if (statusFilter.value) params.status = statusFilter.value

    const res = await getProjects(params)
    projects.value = res.items
    total.value = res.total
  } catch {
    projects.value = []
    total.value = 0
  } finally {
    loading.value = false
  }
}

function handleSearch(): void {
  page.value = 1
  fetchProjects()
}

function handleReset(): void {
  keyword.value = ''
  languageFilter.value = ''
  statusFilter.value = ''
  handleSearch()
}

function handleCreate(): void {
  formMode.value = 'create'
  editingProject.value = null
  formVisible.value = true
}

function handleEdit(row: ProjectOut): void {
  formMode.value = 'edit'
  editingProject.value = row
  formVisible.value = true
}

function handleView(row: ProjectOut): void {
  router.push(`/projects/${row.id}`)
}

async function handleDelete(id: number): Promise<void> {
  if (!await confirmDanger({ target: '删除该项目' })) return
  try {
    await deleteProject(id)
    ElMessage.success('项目已删除')
    if (projects.value.length === 1 && page.value > 1) {
      page.value--
    }
    await fetchProjects()
  } catch {
    /* http 拦截器已处理 */
  }
}

async function submitRemoteImport(): Promise<void> {
  if (!userStore.hasPermission('project:import')) return
  if (!remoteForm.value.url.trim() || !remoteForm.value.project_name.trim()) {
    ElMessage.warning('请填写源码地址和项目名称')
    return
  }
  if (remoteImportRecord.value) {
    remoteImportError.value = '已有远程导入任务正在处理中，请等待当前任务完成。'
    return
  }
  const payload: RemoteProjectImportInput = {
    url: remoteForm.value.url.trim(),
    project_name: remoteForm.value.project_name.trim(),
    description: remoteForm.value.description.trim() || undefined,
    audit_mode: remoteForm.value.audit_mode,
  }
  const record: PersistedRemoteImport = {
    version: REMOTE_IMPORT_RECORD_VERSION,
    taskId: null,
    idempotencyKey: newRemoteImportIdempotencyKey(),
    payload,
  }
  // 先保存请求，再发起网络调用;响应丢失时可用同一幂等键恢复。
  persistRemoteImport(record)
  await enqueuePersistedRemoteImport(record)
}

async function onFormSubmit(data: { project_name: string; description?: string; language?: string; files?: File[] }): Promise<void> {
  if (formMode.value === 'create') {
    const { files, ...projectData } = data
    const result = await createProject(projectData)
    ElMessage.success('项目创建成功')
    if (files && files.length > 0) {
      ElMessage.info(`正在上传 ${files.length} 个文件...`)
      try {
        const uploadResult = await uploadFolder(result.id, files)
        if (uploadResult.success_count > 0) {
          ElMessage.success(`成功上传 ${uploadResult.success_count} 个文件`)
        }
        if (uploadResult.fail_count > 0) {
          const errMsg = uploadResult.errors.slice(0, 3).map((e: any) => e.error).join('; ')
          ElMessage.warning({ message: `${uploadResult.fail_count} 个文件上传失败: ${errMsg}`, duration: 6000 })
        }
      } catch (e: any) {
        const detail = e?.response?.data?.detail || e?.message || e?.toString() || ''
        ElMessage.error({ message: `文件上传失败: ${detail}`, duration: 6000 })
      }
    }
  } else if (editingProject.value) {
    await updateProject(editingProject.value.id, data)
    ElMessage.success('项目更新成功')
  }
  formVisible.value = false
  await fetchProjects()
}

let taskRefreshTimer: ReturnType<typeof setTimeout> | undefined
let remoteImportPollTimer: ReturnType<typeof setTimeout> | undefined
let remoteImportPollInFlight = false
let remoteImportPollFailures = 0
let componentDisposed = false
function onAgentTaskComplete(): void {
  if (taskRefreshTimer !== undefined) clearTimeout(taskRefreshTimer)
  taskRefreshTimer = setTimeout(() => { void fetchProjects() }, 600)
}

onMounted(() => {
  componentDisposed = false
  fetchProjects()
  window.addEventListener('prism:agent-task-complete', onAgentTaskComplete)
  void restoreRemoteImport()
})

onBeforeUnmount(() => {
  componentDisposed = true
  if (taskRefreshTimer !== undefined) clearTimeout(taskRefreshTimer)
  clearRemoteImportPoll()
  window.removeEventListener('prism:agent-task-complete', onAgentTaskComplete)
})
</script>

<style scoped lang="scss">
.project-list {
  display: flex;
  flex-direction: column;
  gap: 16px;
  min-width: 0;
}

/* ============ 页头 ============ */
.page-head {
  display: flex;
  align-items: flex-end;
  justify-content: space-between;
  flex-wrap: wrap;
  gap: 16px;
}

.page-title {
  font-size: 26px;
  font-weight: 600;
  letter-spacing: 0;
  color: var(--gray-900);
  margin: 0;
}

.page-sub {
  margin-top: 4px;
  font-size: 13px;
  color: var(--gray-500);

  .hl { color: var(--brand-600); font-weight: 600; }
}

.page-actions {
  display: flex;
  align-items: center;
  gap: 12px;
  flex-wrap: wrap;
}

.remote-import-feedback {
  display: flex;
  align-items: center;
  gap: 12px;
  min-width: 0;
  padding: 12px 14px;
  background: var(--brand-50);
  border: 1px solid var(--brand-200);
  border-radius: 8px;
  color: var(--gray-700);

  &.is-error {
    background: rgba(214, 76, 89, 0.06);
    border-color: rgba(214, 76, 89, 0.28);
  }

  &.is-success {
    background: rgba(79, 184, 122, 0.08);
    border-color: rgba(79, 184, 122, 0.3);
  }

  &.is-cancelled {
    background: var(--gray-50);
    border-color: var(--gray-200);
  }
}

.remote-import-feedback-dot {
  width: 9px;
  height: 9px;
  flex: 0 0 auto;
  border-radius: 50%;
  background: var(--brand-500);
  box-shadow: 0 0 0 4px rgba(91, 88, 232, 0.1);

  .is-error & {
    background: var(--sev-severe);
    box-shadow: 0 0 0 4px rgba(214, 76, 89, 0.1);
  }

  .is-success & {
    background: var(--status-fixed);
    box-shadow: 0 0 0 4px rgba(79, 184, 122, 0.1);
  }

  .is-cancelled & {
    background: var(--gray-400);
    box-shadow: 0 0 0 4px rgba(155, 163, 176, 0.12);
  }
}

.remote-import-feedback-copy {
  flex: 1;
  min-width: 0;

  strong {
    display: block;
    color: var(--gray-900);
    font-size: 13px;
    font-weight: 600;
  }

  p {
    margin: 3px 0 0;
    color: var(--gray-600);
    font-size: 12px;
    line-height: 1.5;
    overflow-wrap: anywhere;
  }
}

.view-switch {
  display: inline-flex;
  padding: 4px;
  background: var(--gray-100);
  border-radius: 10px;
}

.view-btn {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  min-width: 72px;
  height: 40px;
  padding: 0 14px;
  border: none;
  background: transparent;
  border-radius: 7px;
  font-size: 12.5px;
  color: var(--gray-500);
  cursor: pointer;
  transition: all 0.15s ease;

  &:hover { color: var(--gray-900); }

  &:focus-visible {
    outline: 2px solid var(--brand-500);
    outline-offset: 2px;
  }

  &.active {
    background: #fff;
    color: var(--gray-900);
    font-weight: 500;
    box-shadow: var(--shadow-1);
  }

  .ico { font-family: var(--font-mono); font-size: 13px; }
}

/* ============ 筛选条 ============ */
.filter-bar {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 14px 16px;
  background: #fff;
  border: 1px solid var(--gray-100);
  border-radius: 12px;
  min-width: 0;
  flex-wrap: wrap;
}

.search-input { width: 280px; }
.filter-select { width: 140px; }
.filter-spacer { flex: 1; }
.filter-result {
  font-size: 11.5px;
  color: var(--gray-500);
}

/* ============ 表格 ============ */
.table-card {
  background: #fff;
  border: 1px solid var(--gray-100);
  border-radius: 12px;
  overflow-x: auto;
  overflow-y: hidden;
  -webkit-overflow-scrolling: touch;
  box-shadow: var(--shadow-1);
}

.prism-table {
  width: 100%;
  min-width: 960px;
  border-collapse: collapse;
  font-size: 13px;

  thead th {
    text-align: left;
    padding: 14px 18px;
    background: var(--gray-50);
    color: var(--gray-500);
    font-weight: 500;
    font-size: 12px;
    letter-spacing: 0.02em;
    border-bottom: 1px solid var(--gray-100);
  }

  tbody tr {
    cursor: pointer;
    transition: background 0.15s ease;

    &:hover { background: var(--brand-50); }
    &:not(:last-child) td { border-bottom: 1px solid var(--gray-100); }
  }

  tbody td {
    padding: 14px 18px;
    color: var(--gray-700);
    vertical-align: middle;
  }

  .col-act { white-space: nowrap; text-align: right; }
  .col-score { width: 90px; }
  .col-files { width: 80px; }
  .col-status { width: 100px; }
  .col-lang { width: 110px; }
  .col-last, .col-create { width: 160px; }
  .col-runs { width: 110px; }
  .runs-badge { color: var(--brand-600, #5b58e8); font-size: 12px; }
}

.cell-name {
  display: flex;
  align-items: center;
  gap: 12px;
  min-width: 0;
}

.proj-avatar {
  width: 36px;
  height: 36px;
  border-radius: 9px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  color: #fff;
  font-family: var(--font-display);
  font-size: 14px;
  font-weight: 600;
  flex-shrink: 0;

  &.lg { width: 44px; height: 44px; border-radius: 11px; font-size: 16px; }
}

.proj-meta {
  display: flex;
  flex-direction: column;
  gap: 2px;
  min-width: 0;
}

.proj-name {
  font-size: 13.5px;
  font-weight: 500;
  color: var(--gray-900);
}

.proj-desc {
  font-size: 11.5px;
  color: var(--gray-500);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  max-width: 320px;
}

.lang-chip {
  display: inline-flex;
  align-items: center;
  height: 22px;
  padding: 0 8px;
  border-radius: 4px;
  background: var(--gray-100);
  color: var(--gray-700);
  font-size: 11px;
}

.status-pill {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  height: 22px;
  padding: 0 10px;
  border-radius: 999px;
  font-size: 11.5px;
  font-weight: 500;

  .pill-dot { width: 6px; height: 6px; border-radius: 50%; }

  &.s-active {
    color: var(--status-fixed);
    background: rgba(79, 184, 122, 0.12);
    .pill-dot { background: var(--status-fixed); }
  }
  &.s-archived {
    color: var(--gray-500);
    background: var(--gray-100);
    .pill-dot { background: var(--gray-400); }
  }
}

.mini-gauge {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  position: relative;
  width: 44px;
  height: 36px;

  .gauge-svg { width: 36px; height: 36px; }
  .gauge-text {
    position: absolute;
    inset: 0;
    display: flex;
    align-items: center;
    justify-content: center;
    font-size: 11px;
    font-weight: 600;
    width: 36px;
    height: 36px;
  }

  &.sm {
    width: 32px;
    height: 32px;
    .gauge-svg, .gauge-text { width: 32px; height: 32px; font-size: 10px; }
  }
}

.file-count { color: var(--gray-700); }
.muted { color: var(--color-text-placeholder); }
.muted-2 { color: var(--gray-500); font-size: 12px; }

/* ============ 卡片视图 ============ */
.card-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(min(300px, 100%), 1fr));
  gap: 14px;
}

.proj-card {
  display: flex;
  flex-direction: column;
  gap: 12px;
  padding: 18px;
  background: #fff;
  border: 1px solid var(--gray-100);
  border-radius: 12px;
  cursor: pointer;
  transition: all 0.2s ease;
  box-shadow: var(--shadow-1);

  &:hover {
    border-color: var(--brand-200);
    box-shadow: var(--shadow-3);
    transform: translateY(-2px);
  }
}

.proj-card-head {
  display: flex;
  align-items: center;
  gap: 12px;
}

.head-meta {
  flex: 1;
  min-width: 0;
}

.card-name {
  font-family: var(--font-display);
  font-size: 15px;
  font-weight: 600;
  color: var(--gray-900);
  margin-bottom: 2px;
}

.card-sub {
  font-size: 11px;
  color: var(--gray-500);
}

.card-desc {
  font-size: 13px;
  color: var(--gray-600);
  line-height: 1.55;
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
  min-height: 40px;
}

.card-spectrum > div { height: 4px; }

.card-foot {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding-top: 10px;
  border-top: 1px solid var(--gray-100);
}

.foot-meta {
  flex: 1;
  min-width: 0;
  font-size: 11px;
  color: var(--gray-500);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.card-actions {
  display: inline-flex;
  align-items: center;
  flex: 0 0 auto;
  gap: 2px;
  margin: 0 6px;
}

.card-action {
  width: 30px;
  height: 30px;
  padding: 0;
}

.card-empty { grid-column: 1 / -1; }

/* ============ 分页 ============ */
.pagination-wrap {
  display: flex;
  justify-content: flex-end;

  :deep(.el-pagination) {
    min-width: 0;
  }
}

@media (max-width: 768px) {
  .project-list {
    gap: 14px;
  }

  .page-actions,
  .view-switch {
    width: 100%;
  }

  .view-btn {
    flex: 1;
    justify-content: center;
  }

  .filter-spacer,
  .filter-result {
    display: none;
  }

  .remote-import-feedback {
    align-items: flex-start;
    flex-wrap: wrap;
  }

  .remote-import-feedback-copy {
    flex-basis: calc(100% - 24px);
  }

  .remote-import-feedback :deep(.el-button) {
    margin-left: 21px;
  }

  .proj-desc {
    max-width: 240px;
  }

  .pagination-wrap {
    justify-content: flex-start;

    :deep(.el-pagination) {
      flex-wrap: wrap;
      gap: 8px;
    }
  }
}

@media (max-width: 520px) {
  .page-title {
    font-size: 22px;
  }

  .proj-card {
    padding: 16px;
  }

  .proj-card-head {
    align-items: flex-start;
    flex-wrap: wrap;
  }

  .proj-card-head .status-pill {
    margin-left: 56px;
  }

  .pagination-wrap {
    :deep(.el-pagination__jump) {
      display: none;
    }
  }
}
</style>
