<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue'
import {
  ArrowRight,
  CircleClose,
  Clock,
  Connection,
  Cpu,
  Download,
  Monitor,
  Promotion,
  Refresh,
  Search,
} from '@element-plus/icons-vue'

import {
  createSandbox,
  createSandboxPreviewSession,
  downloadSandboxArtifact,
  extendSandbox,
  getSandbox,
  listSandboxes,
  searchSandboxCapabilities,
  stopSandbox,
} from '@/api/sandbox'
import { listSandboxWorkers } from '@/api/mcpGovernance'
import { getProjectDetail, getProjects } from '@/api/project'
import { useUserStore } from '@/stores/user'
import type { ProjectOut } from '@/types/project'
import type { SandboxArtifact, SandboxEnvironment, SandboxLanguage, SandboxPurpose, SandboxTestMode } from '@/types/sandbox'
import type { SandboxWorker } from '@/types/mcpGovernance'
import {
  canStopSandbox,
  stageLabel,
  hasSandboxConclusion,
  isRemoteAuthorizationRequired,
  isSandboxActive,
  projectSandboxLanguage,
  sortSandboxEvents,
} from '@/utils/sandboxPresentation'
import { ElMessage } from 'element-plus/es/components/message/index'
import { ElMessageBox } from 'element-plus/es/components/message-box/index'

const POLL_INTERVAL_MS = 2500
const userStore = useUserStore()
const projects = ref<ProjectOut[]>([])
const sourceRevisions = ref<Array<{ id: number; revision_no: number; source_sha256: string; repaired_files: string[]; repair_notes: string; create_time?: string | null }>>([])
const workers = ref<SandboxWorker[]>([])
const environments = ref<SandboxEnvironment[]>([])
const selectedId = ref('')
const loading = ref(false)
const submitting = ref(false)
const mutating = ref(false)
const capabilitiesLoading = ref(false)
const capabilityQuery = ref('')
const capabilityResults = ref<Awaited<ReturnType<typeof searchSandboxCapabilities>>>([])
let pollTimer: ReturnType<typeof setInterval> | null = null
const pollInFlight = ref(false)

const form = reactive({
  project_id: null as number | null,
  purpose: 'test' as SandboxPurpose,
  language: 'python' as SandboxLanguage,
  test_mode: 'whitebox' as SandboxTestMode,
  db_type: 'none' as 'none' | 'sqlite' | 'mysql',
  worker_code: '',
  source_revision_id: null as number | null,
  ttl_hours: 72,
  remote_target_url: '',
  remote_target_authorized: false,
})

const languageOptions: Array<{ value: SandboxLanguage; label: string }> = [
  { value: 'python', label: 'Python' },
  { value: 'node', label: 'Node.js' },
  { value: 'java', label: 'Java' },
  { value: 'go', label: 'Go' },
  { value: 'php', label: 'PHP' },
]
const testModes: Array<{ value: Exclude<SandboxTestMode, 'deploy'>; label: string; hint: string }> = [
  { value: 'whitebox', label: '白盒', hint: '源码整体扫描与本地测试' },
  { value: 'blackbox', label: '黑盒', hint: '运行态或授权远程目标探测' },
  { value: 'combined', label: '组合', hint: '先白盒再黑盒核验' },
]
const dbTypes: Array<{ value: 'none' | 'sqlite' | 'mysql'; label: string; hint: string }> = [
  { value: 'none', label: '无', hint: '不使用数据库' },
  { value: 'sqlite', label: 'SQLite', hint: '沙箱内置 SQLite,可做真实 SQL 注入探测' },
  { value: 'mysql', label: 'MySQL', hint: '连接独立沙箱测试库,可做真实 SQL 注入探测' },
]

const selected = computed(() => environments.value.find((item) => item.public_id === selectedId.value) || null)

function sourceRevisionNo(revisionId: number | null | undefined): number | '-' {
  if (!revisionId) return '-'
  return sourceRevisions.value.find((rev) => rev.id === revisionId)?.revision_no ?? '-'
}
const selectedFormProject = computed(() => projects.value.find((item) => item.id === form.project_id) || null)
const selectedProjectLanguage = computed(() => projectSandboxLanguage(selectedFormProject.value?.language))
const selectedEvents = computed(() => sortSandboxEvents(selected.value?.events || []))
const selectedProjectName = computed(() => {
  const project = projects.value.find((item) => item.id === selected.value?.project_id)
  return project?.project_name || `项目 #${selected.value?.project_id || '-'}`
})
const remoteAuthorizationRequired = computed(() => (
  form.purpose === 'test'
  && isRemoteAuthorizationRequired(form.test_mode, form.remote_target_url)
))
const availableWorkers = computed(() => workers.value.filter((worker) => (
  worker.enabled
  && worker.status === 'healthy'
  && worker.supported_languages.includes(form.language)
  && worker.supported_modes.includes(
    form.purpose === 'deploy'
      ? 'deploy'
      : (form.remote_target_url.trim() && form.test_mode === 'blackbox' ? 'whitebox' : form.test_mode),
  )
)))
const submitDisabled = computed(() => (
  !form.project_id
  || submitting.value
  || (form.purpose === 'deploy' && !selectedProjectLanguage.value)
  || (remoteAuthorizationRequired.value && !form.remote_target_authorized)
))

function syncProjectLanguage(projectId: number | null): void {
  const project = projects.value.find((item) => item.id === projectId)
  const language = projectSandboxLanguage(project?.language)
  if (language) form.language = language
}

function statusLabel(status: string): string {
  return ({
    queued: '排队中', dispatching: '调度中', running: '运行中', ready: '预览就绪',
    stopping: '关闭中', succeeded: '已通过', failed: '失败', blocked: '已阻断',
    stopped: '已关闭', expired: '已到期',
  } as Record<string, string>)[status] || status
}

function statusType(status: string): 'success' | 'warning' | 'danger' | 'info' | 'primary' {
  if (status === 'succeeded' || status === 'ready') return 'success'
  if (status === 'failed' || status === 'blocked') return 'danger'
  if (status === 'queued' || status === 'dispatching' || status === 'running' || status === 'stopping') return 'warning'
  return 'info'
}

function purposeLabel(item: SandboxEnvironment): string {
  if (item.purpose === 'deploy') return '部署'
  return ({ whitebox: '白盒测试', blackbox: '黑盒测试', combined: '组合测试' } as Record<string, string>)[item.test_mode] || '测试'
}

function formatTime(value?: string | null): string {
  if (!value) return '-'
  const date = new Date(value.endsWith('Z') || /[+-]\d\d:\d\d$/.test(value) ? value : `${value}Z`)
  return Number.isNaN(date.getTime()) ? value : new Intl.DateTimeFormat('zh-CN', {
    month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false,
  }).format(date)
}

function formatScore(value: number): string {
  return `${Math.round(value * 100)}%`
}

function formatBytes(value: number): string {
  if (value < 1024) return `${value} B`
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KiB`
  return `${(value / (1024 * 1024)).toFixed(1)} MiB`
}

function resultSummary(item: SandboxEnvironment): string {
  const summary = item.result?.summary
  if (typeof summary === 'string' && summary) return summary
  if (item.error) return item.error
  return item.status === 'ready' ? '部署已启动，可创建预览会话。' : '任务已结束，未返回摘要。'
}

function resultEvidence(item: SandboxEnvironment): string {
  const evidence = item.result?.evidence ?? item.result
  return JSON.stringify(evidence || {}, null, 2)
}

async function loadInitial(): Promise<void> {
  loading.value = true
  try {
    const [projectPage, sandboxRows] = await Promise.all([
      getProjects({ page: 1, page_size: 100, status: 'active' }),
      listSandboxes(),
    ])
    projects.value = projectPage.items.filter((item) => item.status === 'active')
    environments.value = sandboxRows
    if (!selectedId.value && sandboxRows.length) selectedId.value = sandboxRows[0].public_id
    if (!form.project_id && projects.value.length) {
      form.project_id = projects.value[0].id
      syncProjectLanguage(form.project_id)
    }
    if (userStore.isSuperAdmin()) {
      try { workers.value = await listSandboxWorkers() } catch { workers.value = [] }
    }
  } finally {
    loading.value = false
  }
}

async function refreshSelected(silent = false): Promise<void> {
  if (pollInFlight.value) return
  pollInFlight.value = true
  try {
    const rows = await listSandboxes()
    environments.value = rows
    if (selectedId.value) {
      const detail = await getSandbox(selectedId.value)
      const index = environments.value.findIndex((item) => item.public_id === detail.public_id)
      if (index >= 0) environments.value.splice(index, 1, detail)
      else environments.value.unshift(detail)
    }
    if (!selectedId.value && rows.length) selectedId.value = rows[0].public_id
    if (!silent) ElMessage.success('任务状态已刷新')
  } catch {
    if (!silent) ElMessage.error('刷新沙箱状态失败')
  } finally {
    pollInFlight.value = false
  }
}

async function submit(): Promise<void> {
  if (!form.project_id) {
    ElMessage.warning('请选择项目')
    return
  }
  if (remoteAuthorizationRequired.value && !form.remote_target_authorized) {
    ElMessage.warning('请确认本次远程目标测试已获得授权')
    return
  }
  const deploymentLanguage = form.purpose === 'deploy'
    ? projectSandboxLanguage(selectedFormProject.value?.language)
    : null
  if (form.purpose === 'deploy' && !deploymentLanguage) {
    ElMessage.warning('项目主语言无法映射到受控运行时，请先更新项目语言')
    return
  }
  const language = deploymentLanguage || form.language
  submitting.value = true
  try {
    const created = await createSandbox({
      project_id: form.project_id,
      purpose: form.purpose,
      language,
      test_mode: form.purpose === 'deploy' ? 'deploy' : form.test_mode,
      db_type: form.purpose === 'test' ? form.db_type : undefined,
      worker_code: form.worker_code || undefined,
      source_revision_id: form.source_revision_id || undefined,
      ttl_hours: form.ttl_hours,
      remote_target_url: form.remote_target_url.trim() || undefined,
      remote_target_authorized: remoteAuthorizationRequired.value && form.remote_target_authorized,
    })
    environments.value.unshift(created)
    selectedId.value = created.public_id
    ElMessage.success('任务已交给专用 Agent，调用过程将在右侧持续更新')
  } finally {
    submitting.value = false
  }
}

async function stopCurrent(): Promise<void> {
  if (!selected.value) return
  try {
    await ElMessageBox.confirm('关闭后运行环境会立即回收，是否继续？', '关闭沙箱', {
      type: 'warning', confirmButtonText: '关闭', cancelButtonText: '取消',
    })
  } catch { return }
  mutating.value = true
  try {
    const updated = await stopSandbox(selected.value.public_id)
    replaceEnvironment(updated)
    ElMessage.success('沙箱已关闭')
  } finally { mutating.value = false }
}

async function extendCurrent(): Promise<void> {
  if (!selected.value) return
  mutating.value = true
  try {
    const updated = await extendSandbox(selected.value.public_id, 24)
    replaceEnvironment(updated)
    ElMessage.success('已续期 24 小时')
  } finally { mutating.value = false }
}

async function openPreview(): Promise<void> {
  if (!selected.value) return
  // 用户点击时先同步创建空窗口，避免等待会话接口后被浏览器当作非用户触发弹窗拦截。
  const previewWindow = window.open('about:blank', '_blank')
  if (previewWindow) previewWindow.opener = null
  mutating.value = true
  try {
    const session = await createSandboxPreviewSession(selected.value.public_id)
    const previewPath = session.path || session.preview_path
    if (!previewPath) throw new Error('preview path missing')
    const target = new URL(previewPath, window.location.origin).toString()
    if (previewWindow) previewWindow.location.replace(target)
    else window.open(target, '_blank', 'noopener,noreferrer')
  } catch {
    previewWindow?.close()
    ElMessage.error('预览会话创建失败')
  } finally { mutating.value = false }
}

function reviewReportArtifact(env: SandboxEnvironment | null): SandboxArtifact | null {
  return env?.artifacts?.find((artifact) => artifact.artifact_type === 'review_report') ?? null
}

const reviewReport = computed(() => reviewReportArtifact(selected.value))

async function downloadArtifact(artifact: SandboxArtifact): Promise<void> {
  if (!selected.value) return
  mutating.value = true
  try {
    const blob = await downloadSandboxArtifact(selected.value.public_id, artifact.id)
    const href = URL.createObjectURL(blob)
    const anchor = document.createElement('a')
    anchor.href = href
    anchor.download = artifact.file_name
    anchor.click()
    URL.revokeObjectURL(href)
  } finally { mutating.value = false }
}

function replaceEnvironment(updated: SandboxEnvironment): void {
  const index = environments.value.findIndex((item) => item.public_id === updated.public_id)
  if (index >= 0) environments.value.splice(index, 1, updated)
  else environments.value.unshift(updated)
}

async function searchCapabilities(): Promise<void> {
  capabilitiesLoading.value = true
  try {
    capabilityResults.value = await searchSandboxCapabilities(capabilityQuery.value.trim(), 8)
  } finally { capabilitiesLoading.value = false }
}

watch(() => form.purpose, (purpose) => {
  if (purpose === 'deploy') {
    syncProjectLanguage(form.project_id)
    form.test_mode = 'deploy'
    form.remote_target_url = ''
    form.remote_target_authorized = false
  } else if (form.test_mode === 'deploy') {
    form.test_mode = 'whitebox'
  }
  form.worker_code = ''
})

watch(() => form.test_mode, (mode) => {
  if (mode === 'whitebox' || mode === 'deploy') {
    form.remote_target_url = ''
    form.remote_target_authorized = false
  }
  form.worker_code = ''
})

watch(() => form.language, () => { form.worker_code = '' })
watch(() => form.project_id, async (projectId) => {
  syncProjectLanguage(projectId)
  form.worker_code = ''
  form.source_revision_id = null
  sourceRevisions.value = []
  if (projectId) {
    try {
      const detail = await getProjectDetail(projectId)
      sourceRevisions.value = detail.source_revisions || []
    } catch { /* 副本列表失败不影响主流程 */ }
  }
})
watch(() => form.remote_target_url, () => { form.remote_target_authorized = false })

onMounted(async () => {
  await loadInitial()
  pollTimer = setInterval(() => {
    if (environments.value.some((item) => isSandboxActive(item.status))) void refreshSelected(true)
  }, POLL_INTERVAL_MS)
})

let taskRefreshTimer: ReturnType<typeof setTimeout> | undefined
function onAgentTaskComplete(): void {
  if (taskRefreshTimer) clearTimeout(taskRefreshTimer)
  taskRefreshTimer = setTimeout(() => { void refreshSelected(true) }, 600)
}

onMounted(async () => {
  await loadInitial()
  window.addEventListener('prism:agent-task-complete', onAgentTaskComplete)
  pollTimer = setInterval(() => {
    if (environments.value.some((item) => isSandboxActive(item.status))) void refreshSelected(true)
  }, POLL_INTERVAL_MS)
})

onBeforeUnmount(() => {
  if (pollTimer) clearInterval(pollTimer)
  if (taskRefreshTimer) clearTimeout(taskRefreshTimer)
  window.removeEventListener('prism:agent-task-complete', onAgentTaskComplete)
})
</script>

<template>
  <div class="sandbox-workstation" v-loading="loading">
    <header class="page-heading">
      <div>
        <div class="eyebrow font-mono">ISOLATED EXECUTION</div>
        <h1>代码沙箱</h1>
        <p>在隔离 worker 中执行源码整体白盒测试、运行态黑盒测试或临时部署。</p>
      </div>
      <el-button :icon="Refresh" :loading="pollInFlight" @click="refreshSelected(false)">刷新</el-button>
    </header>

    <div class="workstation-grid">
      <section class="config-panel" aria-label="创建沙箱任务">
        <div class="section-title">
          <span>任务配置</span>
          <el-tag size="small" type="info">默认保留 72h</el-tag>
        </div>

        <el-form label-position="top" @submit.prevent="submit">
          <el-form-item label="项目源码">
            <el-select v-model="form.project_id" filterable placeholder="选择有权访问的项目" style="width: 100%">
              <el-option v-for="project in projects" :key="project.id" :label="project.project_name" :value="project.id" />
            </el-select>
          </el-form-item>

          <el-form-item v-if="sourceRevisions.length" label="源码版本">
            <el-select v-model="form.source_revision_id" clearable placeholder="原始源码(默认)" style="width: 100%">
              <el-option v-for="rev in sourceRevisions" :key="rev.id" :value="rev.id" :label="`修复副本 rev#${rev.revision_no} · ${rev.repaired_files.length} 个文件`">
                <span>{{ `修复副本 rev#${rev.revision_no}` }}</span>
                <span class="field-hint">修复 {{ rev.repaired_files.join(', ').slice(0, 40) }}</span>
              </el-option>
            </el-select>
            <div class="field-hint">不选使用原始源码；选副本使用语法修复 Agent 修复后的源码。</div>
          </el-form-item>

          <el-form-item label="执行目标">
            <el-radio-group v-model="form.purpose" class="segmented-control">
              <el-radio-button value="test"><el-icon><Cpu /></el-icon> 测试</el-radio-button>
              <el-radio-button value="deploy"><el-icon><Monitor /></el-icon> 部署</el-radio-button>
            </el-radio-group>
          </el-form-item>

          <el-form-item label="语言运行时">
            <el-select v-model="form.language" :disabled="form.purpose === 'deploy'" style="width: 100%">
              <el-option v-for="language in languageOptions" :key="language.value" :label="language.label" :value="language.value" />
            </el-select>
            <div v-if="form.purpose === 'deploy' && selectedProjectLanguage" class="field-hint">部署 Agent 已按项目主语言自动选择受控运行时。</div>
            <div v-else-if="form.purpose === 'deploy'" class="field-hint">项目主语言无法映射到受控运行时，请先更新项目语言。</div>
          </el-form-item>

          <el-form-item v-if="form.purpose === 'test'" label="数据库">
            <div class="mode-options">
              <label v-for="db in dbTypes" :key="db.value" class="mode-option">
                <input v-model="form.db_type" type="radio" :value="db.value">
                <span class="mode-option-body">
                  <b>{{ db.label }}</b>
                  <small>{{ db.hint }}</small>
                </span>
              </label>
            </div>
          </el-form-item>
          <el-form-item v-if="form.purpose === 'test'" label="测试模式">
            <div class="mode-options">
              <label v-for="mode in testModes" :key="mode.value" class="mode-option" :class="{ active: form.test_mode === mode.value }">
                <input v-model="form.test_mode" type="radio" :value="mode.value">
                <span class="mode-title">{{ mode.label }}</span>
                <span class="mode-hint">{{ mode.hint }}</span>
              </label>
            </div>
          </el-form-item>

          <template v-if="form.purpose === 'test' && (form.test_mode === 'blackbox' || form.test_mode === 'combined')">
            <el-form-item label="远程目标（可选）">
              <el-input v-model="form.remote_target_url" type="url" placeholder="https://authorized-target.example" :prefix-icon="Connection" />
              <div class="field-hint">留空时测试沙箱内运行态；填写时仅允许公开 HTTP(S) 目标。</div>
            </el-form-item>
            <div v-if="remoteAuthorizationRequired" class="authorization-row">
              <el-checkbox v-model="form.remote_target_authorized">
                我确认已获得该目标本次测试授权
              </el-checkbox>
            </div>
          </template>

          <div class="inline-fields">
            <el-form-item label="保留时长">
              <el-input-number v-model="form.ttl_hours" :min="1" :max="userStore.isSuperAdmin() ? 720 : 168" controls-position="right" />
              <span class="unit">小时</span>
            </el-form-item>
            <el-form-item v-if="userStore.isSuperAdmin()" label="指定 worker">
              <el-select v-model="form.worker_code" clearable placeholder="自动调度">
                <el-option v-for="worker in availableWorkers" :key="worker.code" :label="`${worker.name} · ${worker.runtime}`" :value="worker.code" />
              </el-select>
            </el-form-item>
          </div>

          <el-button class="submit-button" native-type="submit" type="primary" :icon="Promotion" :loading="submitting" :disabled="submitDisabled">
            调用 {{ form.purpose === 'deploy' ? '部署' : '测试' }} Agent
          </el-button>
        </el-form>

        <div class="capability-search">
          <div class="section-title compact"><span>能力检索</span></div>
          <el-input v-model="capabilityQuery" placeholder="输入近义词，如：渗透、整包扫描" clearable @keyup.enter="searchCapabilities">
            <template #append><el-button :icon="Search" :loading="capabilitiesLoading" aria-label="搜索能力" @click="searchCapabilities" /></template>
          </el-input>
          <button v-for="item in capabilityResults" :key="item.code" type="button" class="capability-result" @click="capabilityQuery = item.name">
            <span><b>{{ item.name }}</b><small>{{ item.description }}</small></span>
            <span class="capability-score font-mono">{{ formatScore(item.score) }}</span>
          </button>
        </div>
      </section>

      <section class="execution-panel" aria-label="沙箱任务与 Agent 输出">
        <div class="task-strip">
          <div class="section-title">
            <span>任务队列</span>
            <span class="task-count font-mono">{{ environments.length }}</span>
          </div>
          <div v-if="environments.length" class="task-list">
            <button
              v-for="item in environments"
              :key="item.public_id"
              type="button"
              class="task-row"
              :class="{ active: selectedId === item.public_id }"
              @click="selectedId = item.public_id"
            >
              <span class="status-dot" :class="item.status"></span>
              <span class="task-main">
                <b>{{ purposeLabel(item) }} · {{ item.language }}</b>
                <small>{{ item.public_id }} · {{ formatTime(item.expires_at) }} 到期</small>
              </span>
              <el-tag size="small" :type="statusType(item.status)">{{ statusLabel(item.status) }}</el-tag>
              <el-icon><ArrowRight /></el-icon>
            </button>
          </div>
          <el-empty v-else description="暂无沙箱任务" :image-size="72" />
        </div>

        <div v-if="selected" class="task-detail">
          <div class="detail-toolbar">
            <div>
              <div class="detail-title">{{ selectedProjectName }}</div>
              <div class="detail-meta font-mono">{{ selected.agent_code }} / {{ selected.worker_code || 'auto' }} / {{ selected.runtime }}</div>
            </div>
            <div class="detail-actions">
              <el-button v-if="selected.status === 'ready' && selected.preview_path" type="primary" :icon="Monitor" :loading="mutating" @click="openPreview">打开预览</el-button>
              <el-button v-if="isSandboxActive(selected.status)" :icon="Clock" :loading="mutating" @click="extendCurrent">续期 24h</el-button>
              <el-button v-if="canStopSandbox(selected.status)" type="danger" plain :icon="CircleClose" :loading="mutating" @click="stopCurrent">关闭</el-button>
            </div>
          </div>

          <dl class="fact-grid">
            <div><dt>状态</dt><dd><el-tag size="small" :type="statusType(selected.status)">{{ statusLabel(selected.status) }}</el-tag></dd></div>
            <div><dt>源码指纹</dt><dd class="font-mono">{{ selected.source_sha256.slice(0, 16) }}</dd></div>
            <div><dt>源码来源</dt><dd>{{ selected.source_revision_id ? `修复副本 rev#${sourceRevisionNo(selected.source_revision_id)}` : '原始源码' }}</dd></div>
            <div><dt>执行方式</dt><dd>{{ purposeLabel(selected) }}</dd></div>
            <div><dt>到期时间</dt><dd>{{ formatTime(selected.expires_at) }}</dd></div>
          </dl>

          <section class="agent-timeline" data-testid="agent-timeline">
            <div class="section-title">
              <span>Agent 调用与进度</span>
              <el-tag v-if="isSandboxActive(selected.status)" size="small" type="warning">自动刷新</el-tag>
            </div>
            <ol v-if="selectedEvents.length" class="event-list">
              <li v-for="event in selectedEvents" :key="event.id" :class="event.event_type">
                <span class="event-marker"></span>
                <div class="event-body">
                  <div class="event-head">
                    <b>{{ stageLabel(event.stage) }}</b>
                    <time class="font-mono">{{ formatTime(event.create_time) }}</time>
                  </div>
                  <p>{{ event.message }}</p>
                </div>
              </li>
            </ol>
            <div v-else class="empty-line">Agent 尚未输出事件</div>
          </section>

          <section v-if="hasSandboxConclusion(selected)" class="conclusion-panel" data-testid="agent-conclusion">
            <div class="section-title"><span>执行结论</span></div>
            <el-alert
              :type="selected.status === 'succeeded' || selected.status === 'ready' ? 'success' : 'error'"
              :title="resultSummary(selected)"
              :closable="false"
              show-icon
            />
            <pre v-if="Object.keys(selected.result || {}).length" class="evidence-output">{{ resultEvidence(selected) }}</pre>
          </section>

          <section v-if="reviewReport" class="review-report-panel" data-testid="review-report-panel">
            <div class="section-title"><span>多 Agent 测试审查报告</span></div>
            <p class="report-hint">黑白盒测试完成后由多 Agent 审查编排生成(4 角色),点击下载 Markdown 报告。</p>
            <button type="button" class="artifact-row" @click="downloadArtifact(reviewReport)">
              <span><b>{{ reviewReport.file_name }}</b><small class="font-mono">review_report · {{ formatBytes(reviewReport.byte_size) }}</small></span>
              <el-icon><Download /></el-icon>
            </button>
          </section>

          <section v-if="selected.artifacts?.length" class="artifact-panel" data-testid="sandbox-artifacts">
            <div class="section-title"><span>证据制品</span><span class="task-count font-mono">{{ selected.artifacts.length }}</span></div>
            <div class="artifact-list">
              <button v-for="artifact in selected.artifacts" :key="artifact.id" type="button" class="artifact-row" @click="downloadArtifact(artifact)">
                <span><b>{{ artifact.file_name }}</b><small class="font-mono">{{ artifact.artifact_type }} · {{ formatBytes(artifact.byte_size) }} · {{ artifact.sha256.slice(0, 12) }}</small></span>
                <el-icon><Download /></el-icon>
              </button>
            </div>
          </section>
        </div>
        <el-empty v-else class="detail-empty" description="选择任务查看 Agent 调用和测试结论" />
      </section>
    </div>
  </div>
</template>

<style scoped lang="scss">
.sandbox-workstation { max-width: 1520px; margin: 0 auto; }
.page-heading { display: flex; align-items: flex-start; justify-content: space-between; gap: 20px; margin-bottom: 18px; }
.page-heading h1 { margin: 3px 0 4px; font-size: 24px; line-height: 1.25; letter-spacing: 0; }
.page-heading p { margin: 0; color: var(--gray-500); font-size: 13px; }
.eyebrow { color: var(--color-primary); font-size: 10px; }
.workstation-grid { display: grid; grid-template-columns: minmax(320px, 380px) minmax(0, 1fr); gap: 16px; align-items: start; }
.config-panel, .execution-panel { background: var(--surface-1); border: var(--hairline); border-radius: 8px; box-shadow: var(--panel-shadow); }
.config-panel { padding: 18px; }
.execution-panel { min-width: 0; overflow: hidden; }
.section-title { min-height: 30px; display: flex; align-items: center; justify-content: space-between; gap: 10px; font-size: 14px; font-weight: 650; }
.section-title.compact { margin-bottom: 6px; }
.segmented-control { width: 100%; display: grid; grid-template-columns: 1fr 1fr; }
.segmented-control :deep(.el-radio-button) { width: 100%; }
.segmented-control :deep(.el-radio-button__inner) { width: 100%; display: inline-flex; justify-content: center; align-items: center; gap: 5px; }
.mode-options { width: 100%; display: grid; grid-template-columns: repeat(3, 1fr); gap: 7px; }
.mode-option { min-width: 0; min-height: 76px; padding: 10px; display: flex; flex-direction: column; gap: 5px; border: 1px solid var(--color-border-base); border-radius: 6px; cursor: pointer; background: var(--surface-2); }
.mode-option:hover, .mode-option.active { border-color: var(--color-primary); background: var(--color-primary-light-9); }
.mode-option input { position: absolute; opacity: 0; pointer-events: none; }
.mode-title { font-size: 13px; font-weight: 650; color: var(--gray-800); }
.mode-hint { font-size: 11px; line-height: 1.4; color: var(--gray-500); }
.field-hint { margin-top: 5px; font-size: 11px; line-height: 1.45; color: var(--gray-500); }
.authorization-row { margin: -7px 0 16px; padding: 9px 11px; border-left: 3px solid var(--color-warning); background: var(--color-warning-light); }
.authorization-row :deep(.el-checkbox) { height: auto; white-space: normal; }
.inline-fields { display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 1.15fr); gap: 10px; }
.inline-fields :deep(.el-input-number), .inline-fields :deep(.el-select) { width: 100%; }
.unit { margin-left: 7px; font-size: 12px; color: var(--gray-500); }
.submit-button { width: 100%; }
.capability-search { margin-top: 18px; padding-top: 14px; border-top: var(--hairline); }
.capability-result { width: 100%; padding: 8px 2px; display: flex; align-items: center; justify-content: space-between; gap: 10px; text-align: left; border: 0; border-bottom: var(--hairline); background: transparent; cursor: pointer; }
.capability-result span:first-child { display: flex; flex-direction: column; min-width: 0; }
.capability-result b { font-size: 12px; color: var(--gray-800); }
.capability-result small { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: var(--gray-500); }
.capability-score { font-size: 10px; color: var(--color-primary); }
.task-strip { border-bottom: var(--hairline); }
.task-strip > .section-title { padding: 14px 18px 8px; }
.task-count { min-width: 25px; padding: 2px 7px; border-radius: 4px; text-align: center; color: var(--gray-600); background: var(--gray-100); font-size: 11px; }
.task-list { max-height: 248px; overflow: auto; padding: 0 8px 9px; }
.task-row { width: 100%; min-height: 54px; display: grid; grid-template-columns: 8px minmax(0, 1fr) auto 16px; align-items: center; gap: 10px; padding: 8px 10px; border: 0; border-bottom: var(--hairline); background: transparent; color: inherit; cursor: pointer; text-align: left; }
.task-row:hover, .task-row.active { background: var(--surface-hover); }
.status-dot { width: 7px; height: 7px; border-radius: 50%; background: var(--gray-400); }
.status-dot.queued, .status-dot.dispatching, .status-dot.running, .status-dot.stopping { background: var(--color-warning); box-shadow: 0 0 0 4px rgba(217, 168, 87, .13); }
.status-dot.succeeded, .status-dot.ready { background: var(--color-success); }
.status-dot.failed, .status-dot.blocked { background: var(--color-danger); }
.task-main { min-width: 0; display: flex; flex-direction: column; gap: 2px; }
.task-main b { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-size: 12px; }
.task-main small { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-size: 10px; color: var(--gray-500); }
.task-detail { padding: 18px; }
.detail-toolbar { display: flex; align-items: flex-start; justify-content: space-between; gap: 16px; }
.detail-title { font-size: 16px; font-weight: 650; }
.detail-meta { margin-top: 4px; font-size: 10px; color: var(--gray-500); }
.detail-actions { display: flex; flex-wrap: wrap; justify-content: flex-end; gap: 7px; }
.artifact-panel { margin-top: 16px; padding-top: 14px; border-top: var(--hairline); }
.artifact-list { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 8px; }
.artifact-row { min-width: 0; min-height: 54px; padding: 9px 11px; display: flex; align-items: center; justify-content: space-between; gap: 10px; text-align: left; color: inherit; background: var(--surface-2); border: var(--hairline); border-radius: 6px; cursor: pointer; }
.artifact-row:hover { border-color: var(--color-primary); background: var(--surface-hover); }
.artifact-row span { min-width: 0; display: flex; flex-direction: column; }
.artifact-row b, .artifact-row small { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.artifact-row small { color: var(--gray-500); font-size: 10px; }
.fact-grid { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 1px; margin: 16px 0; padding: 1px; background: var(--color-border-light); }
.fact-grid div { min-width: 0; padding: 10px; background: var(--surface-1); }
.fact-grid dt { margin-bottom: 5px; font-size: 10px; color: var(--gray-500); }
.fact-grid dd { margin: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-size: 12px; color: var(--gray-800); }
.agent-timeline, .conclusion-panel { padding-top: 14px; border-top: var(--hairline); }
.event-list { list-style: none; margin: 5px 0 15px; padding: 0; }
.event-list li { position: relative; display: grid; grid-template-columns: 16px minmax(0, 1fr); gap: 9px; padding-bottom: 12px; }
.event-list li:not(:last-child)::before { content: ''; position: absolute; left: 6px; top: 12px; bottom: 0; width: 1px; background: var(--color-border-base); }
.event-marker { position: relative; z-index: 1; width: 13px; height: 13px; margin-top: 4px; border: 3px solid var(--color-primary-light-9); border-radius: 50%; background: var(--color-primary); }
.event-list li.failed .event-marker { border-color: var(--color-danger-light); background: var(--color-danger); }
.event-list li.complete .event-marker { border-color: var(--color-success-light); background: var(--color-success); }
.event-head { display: flex; align-items: center; justify-content: space-between; gap: 10px; }
.event-head b { font-size: 11px; color: var(--gray-700); text-transform: uppercase; }
.event-head time { font-size: 9px; color: var(--gray-400); }
.event-body p { margin: 2px 0 0; font-size: 12px; line-height: 1.5; color: var(--gray-600); overflow-wrap: anywhere; }
.conclusion-panel { margin-top: 2px; }
.evidence-output { max-height: 300px; margin: 10px 0 0; padding: 12px; overflow: auto; border: var(--hairline); border-radius: 5px; background: #161A24; color: #D9E1F2; font-size: 11px; line-height: 1.55; white-space: pre-wrap; overflow-wrap: anywhere; }
.empty-line { padding: 18px 0; color: var(--gray-500); font-size: 12px; }
.detail-empty { min-height: 380px; }

@media (max-width: 1100px) {
  .workstation-grid { grid-template-columns: 330px minmax(0, 1fr); }
  .fact-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .mode-options { grid-template-columns: 1fr; }
  .mode-option { min-height: 58px; }
}

@media (max-width: 820px) {
  .workstation-grid { grid-template-columns: 1fr; }
  .config-panel { padding: 15px; }
  .page-heading { align-items: center; }
  .task-list { max-height: 300px; }
}

@media (max-width: 560px) {
  .page-heading h1 { font-size: 21px; }
  .page-heading p { max-width: 28rem; }
  .inline-fields, .fact-grid, .artifact-list { grid-template-columns: 1fr; }
  .detail-toolbar { flex-direction: column; }
  .detail-actions { width: 100%; justify-content: flex-start; }
  .task-row { grid-template-columns: 8px minmax(0, 1fr) auto; }
  .task-row > .el-icon { display: none; }
}
</style>
