<template>
  <section class="enterprise-team-progress" aria-label="企业 Agent 团队进度">
    <header class="progress-header">
      <div>
        <h3>小菱协作进度</h3>
        <p>仅显示当前会话中你有权查看的团队和任务结果。</p>
      </div>
      <div class="progress-actions">
        <button type="button" :disabled="loading" @click="refresh">
          {{ loading ? '刷新中…' : '刷新' }}
        </button>
        <button type="button" class="close-progress" aria-label="收起团队进度" @click="emit('close')">收起</button>
      </div>
    </header>

    <p v-if="securityReportAccessNotice" class="security-report-access-notice" role="status">
      {{ securityReportAccessNotice }}
      <button type="button" @click="securityReportAccessNotice = ''">知道了</button>
    </p>

    <p v-if="error" class="progress-error" role="alert">
      {{ error }}
      <button type="button" :disabled="loading" @click="refresh">重试读取</button>
    </p>
    <p v-else-if="loading && !teams.length" class="progress-empty" role="status">正在读取团队任务…</p>
    <p v-else-if="!teams.length && nextBeforeId === null" class="progress-empty">当前会话还没有可查看的协作团队。</p>

    <template v-else>
      <div class="team-list" aria-label="会话团队列表">
        <button
          v-for="team in teams"
          :key="team.team_id"
          type="button"
          class="team-choice"
          :class="{ selected: team.team_id === selectedTeamId }"
          :aria-pressed="team.team_id === selectedTeamId"
          @click="selectTeam(team.team_id)"
        >
          <span class="team-choice-copy">
            <strong>{{ team.title || `协作团队 ${team.team_id}` }}</strong>
            <small>{{ team.completed_task_count }}/{{ team.task_count }} 项已完成 · {{ statusLabel(team.status) }}</small>
          </span>
          <span class="team-status-dot" :class="`status-${team.status}`" aria-hidden="true" />
        </button>
        <button v-if="nextBeforeId !== null" type="button" class="load-more-teams"
          :disabled="loadingMore || loading" @click="loadMoreTeams">
          {{ loadingMore ? '加载中…' : '加载更多' }}
        </button>
      </div>

      <article v-if="detail" class="team-detail" :aria-label="`${detail.title}任务详情`">
        <div class="detail-heading">
          <div>
            <h4>{{ detail.title }}</h4>
            <p>{{ detail.objective }}</p>
          </div>
          <span class="detail-status">{{ statusLabel(detail.status) }}</span>
        </div>
        <div class="progress-meter" role="progressbar" :aria-valuenow="settledCount" :aria-valuemin="0"
          :aria-valuemax="detail.tasks.length" :aria-label="`已结束 ${settledCount}/${detail.tasks.length} 项任务`">
          <span :style="{ width: `${progressPercent}%` }" />
        </div>
        <p class="progress-count">已结束 {{ settledCount }}/{{ detail.tasks.length }} 项 · {{ completedCount }} 项完成</p>
        <ul class="task-list">
          <li v-for="task in detail.tasks" :key="task.task_id" class="task-row">
            <div class="task-heading">
              <div>
                <strong>{{ task.title }}</strong>
                <small>{{ task.agent_name }} · {{ task.attempt_count }} 次尝试</small>
              </div>
              <span class="task-status">{{ statusLabel(task.status) }}</span>
            </div>
            <p v-if="task.result.summary" class="task-summary">{{ task.result.summary }}</p>
            <p v-else-if="task.errors.length && task.status === 'retryable'" class="task-summary task-error">
              上次执行未产生已确认费用，符合安全重试条件。
            </p>
            <p v-else-if="task.errors.length && task.status === 'failed'" class="task-summary task-error">
              任务失败。请查看小菱会话回执；若费用状态不明，不要重试。
            </p>
            <button
              v-if="task.status === 'retryable' && canControlSelectedTeam"
              type="button"
              class="retry-task"
              :disabled="busyAction !== null"
              @click="retryTask(task.task_id)"
            >
              {{ busyAction === `retry:${task.task_id}` ? '提交中…' : '重试未计费任务' }}
            </button>
            <button
              v-if="hasSecurityArtifact(task)"
              :id="`security-report-trigger-${task.task_id}`"
              type="button"
              class="security-report-trigger"
              :aria-expanded="securityReportTaskId === task.task_id"
              @click="openSecurityReport(task)"
            >查看安全报告</button>
          </li>
        </ul>
        <div v-if="canControlSelectedTeam && canCancel(detail.status)" class="team-control">
          <button type="button" :disabled="busyAction !== null" @click="cancelTeam">
            {{ busyAction === 'cancel' ? '正在停止…' : '停止协作团队' }}
          </button>
          <small>当前已发送的模型请求会先结束；后续任务将停止。</small>
        </div>
      </article>

      <section v-if="securityReportTaskId !== null" :id="`security-report-panel-${securityReportTaskId}`"
        class="security-report-panel" aria-label="安全报告详情">
        <header class="security-report-heading">
          <div>
            <h4>项目安全报告</h4>
            <p>报告按需读取，并按当前团队会话与项目权限重新校验。</p>
          </div>
          <button type="button" class="security-report-close" aria-label="关闭安全报告" @click="closeSecurityReport">关闭</button>
        </header>
        <p v-if="securityReportLoading" class="security-report-state" role="status">正在读取安全报告…</p>
        <p v-else-if="securityReportError" class="security-report-error" role="alert">
          {{ securityReportError }}
          <button type="button" class="security-report-retry" @click="retrySecurityReport">重试读取</button>
        </p>
        <template v-else-if="securityReport">
          <div class="security-report-meta">
            <span :class="['coverage-badge', `coverage-${securityReport.coverage_status}`]">
              {{ coverageLabel(securityReport.coverage_status) }}
            </span>
            <span>项目 {{ securityReport.project_id }}</span>
            <span>扫描模式 {{ scanModeLabel }}</span>
            <span>发现 {{ findingCount }} 项</span>
            <span>模型请求 {{ providerRequestCount }} 次</span>
          </div>
          <p class="security-report-summary">{{ reportSummary }}</p>
          <p v-if="isTriageScan" class="security-report-coverage-note">
            triage 仅扫描风险优先子集，不表示完整项目覆盖。
          </p>
          <p v-if="securityReport.termination_reason" class="security-report-termination">
            未完成原因：{{ securityReport.termination_reason }}
          </p>
          <dl class="security-report-digests">
            <div><dt>源码摘要</dt><dd>{{ securityReport.source_sha256 }}</dd></div>
            <div><dt>报告摘要</dt><dd>{{ securityReport.report_sha256 }}</dd></div>
          </dl>
          <h5>发现项</h5>
          <p v-if="!visibleFindings.length" class="security-report-state">报告没有保存发现项。</p>
          <ol v-else class="security-findings">
            <li v-for="(finding, index) in visibleFindings" :key="`${index}:${findingTitle(finding)}`">
              <div class="finding-heading">
                <strong>{{ findingTitle(finding) || `发现项 ${index + 1}` }}</strong>
                <span>{{ findingSeverity(finding) }}</span>
              </div>
              <small v-if="findingLocation(finding)">{{ findingLocation(finding) }}</small>
              <p v-if="findingDescription(finding)">{{ findingDescription(finding) }}</p>
            </li>
          </ol>
          <p v-if="findingCount > visibleFindings.length" class="security-report-limit">
            当前显示前 {{ visibleFindings.length }} 项；报告共 {{ findingCount }} 项。
          </p>
        </template>
      </section>
    </template>
  </section>
</template>

<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import {
  cancelEnterpriseAgentTeam,
  checkEnterpriseTeamSecurityArtifactAccess,
  getEnterpriseAgentTeam,
  listEnterpriseAgentTeams,
  retryEnterpriseAgentTeamTask,
  getEnterpriseTeamSecurityArtifact,
  type EnterpriseAgentTeamDetail,
  type EnterpriseAgentTeamStatus,
  type EnterpriseAgentTeamSummary,
  type EnterpriseAgentTeamTask,
  type EnterpriseTeamSecurityArtifact,
} from '@/api/enterpriseAgentTeams'
import { getToken } from '@/utils/token'

const props = defineProps<{
  enterpriseId: number
  conversationId: number
  currentUserId: number
  roomKind: 'company' | 'group' | 'direct'
  canManageCompany: boolean
  canManageGroup: boolean
}>()
const emit = defineEmits<{ close: [] }>()

const teams = ref<EnterpriseAgentTeamSummary[]>([])
const selectedTeamId = ref<number | null>(null)
const detail = ref<EnterpriseAgentTeamDetail | null>(null)
const loading = ref(false)
const loadingMore = ref(false)
const nextBeforeId = ref<number | null>(null)
const busyAction = ref<string | null>(null)
const error = ref('')
const securityReport = ref<EnterpriseTeamSecurityArtifact | null>(null)
const securityReportTaskId = ref<number | null>(null)
const securityReportArtifactId = ref<number | null>(null)
const securityReportLoading = ref(false)
const securityReportError = ref('')
const securityReportAccessNotice = ref('')
let generation = 0
let refreshSequence = 0
let pageSequence = 0
let detailSequence = 0
let securityReportSequence = 0
let securityReportAccessSequence = 0
let securityReportAccessRequest: { key: string; promise: Promise<void> } | null = null
let pollTimer: ReturnType<typeof setInterval> | null = null
let lastResumeRefreshAt = Number.NEGATIVE_INFINITY
let scopeToken = getToken()
let scopeInvalid = false

const selectedSummary = computed(() => teams.value.find(team => team.team_id === selectedTeamId.value) ?? null)
const canControlSelectedTeam = computed(() => {
  const summary = selectedSummary.value
  if (!summary) return false
  if (summary.initiator_user_id === props.currentUserId) return true
  if (props.roomKind === 'direct') return false
  return props.canManageCompany || (props.roomKind === 'group' && props.canManageGroup)
})
const completedCount = computed(() => detail.value?.tasks.filter(task => task.status === 'completed').length ?? 0)
const settledCount = computed(() => detail.value?.tasks.filter(task => isTerminal(task.status)).length ?? 0)
const progressPercent = computed(() => detail.value?.tasks.length
  ? Math.round(settledCount.value * 100 / detail.value.tasks.length)
  : 0)
const visibleFindings = computed(() => {
  const raw = securityReport.value?.report.findings
  return Array.isArray(raw) ? raw.filter(isRecord).slice(0, 100) : []
})
const findingCount = computed(() => {
  const count = securityReport.value?.summary.finding_count
  return typeof count === 'number' && Number.isSafeInteger(count) && count >= 0
    ? count
    : (Array.isArray(securityReport.value?.report.findings) ? securityReport.value.report.findings.length : 0)
})
const reportSummary = computed(() => displayValue(securityReport.value?.summary.summary) || '报告已生成。')
const reportCompliance = computed(() => {
  const value = securityReport.value?.report.compliance
  return isRecord(value) ? value : {}
})
const scanModeLabel = computed(() => {
  const mode = displayValue(reportCompliance.value.scan_mode)
  const labels: Record<string, string> = {
    triage: '风险优先子集',
    full: '全量',
    static_full: '静态全量',
  }
  return labels[mode] || mode || '未知'
})
const isTriageScan = computed(() => displayValue(reportCompliance.value.scan_mode) === 'triage')
const providerRequestCount = computed(() => {
  const value = reportCompliance.value.provider_request_count
  return typeof value === 'number' && Number.isSafeInteger(value) && value >= 0 ? value : '未报告'
})

function isTerminal(status: string) {
  return ['completed', 'failed', 'cancelled', 'suppressed'].includes(status)
}
function canCancel(status: EnterpriseAgentTeamStatus) {
  return !['completed', 'failed', 'cancelled', 'suppressed'].includes(status)
}
function statusLabel(status: string) {
  const labels: Record<string, string> = {
    queued: '排队中', running: '执行中', retryable: '等待安全重试',
    awaiting_reconciliation: '等待账单核对', completed: '已完成', failed: '未完成',
    cancelled: '已停止', suppressed: '已拦截', blocked: '已拦截',
  }
  return labels[status] || '状态待确认'
}
function isCurrent(requestGeneration: number) {
  if (!scopeInvalid && getToken() !== scopeToken) invalidateScope()
  return !scopeInvalid && requestGeneration === generation
}

async function loadSelectedDetail(requestGeneration: number, requestSequence: number) {
  const detailRequest = ++detailSequence
  const teamId = selectedTeamId.value
  if (!teamId) {
    detail.value = null
    return
  }
  const result = await getEnterpriseAgentTeam(props.enterpriseId, teamId, getToken())
  if (!isCurrent(requestGeneration) || requestSequence !== refreshSequence || detailRequest !== detailSequence
      || selectedTeamId.value !== teamId
      || result.enterprise_id !== props.enterpriseId || result.conversation_id !== props.conversationId) return
  detail.value = result
  if (securityReportTaskId.value !== null
      && !hasReportReference(result, securityReportTaskId.value, securityReportArtifactId.value)) {
    clearSecurityReport()
  } else if (securityReport.value && securityReportTaskId.value !== null
      && securityReportArtifactId.value !== null) {
    void revalidateSecurityReportAccess(
      requestGeneration,
      teamId,
      securityReportTaskId.value,
      securityReportArtifactId.value,
      securityReportSequence,
    )
  }
}

async function refresh() {
  const requestGeneration = generation
  if (!isCurrent(requestGeneration)) return
  const requestSequence = ++refreshSequence
  ++pageSequence
  loadingMore.value = false
  const oldestLoadedId = teams.value.reduce<number | null>(
    (oldest, team) => oldest === null ? team.team_id : Math.min(oldest, team.team_id), null,
  )
  const token = getToken()
  loading.value = true
  error.value = ''
  try {
    let before: number | undefined
    const visible: EnterpriseAgentTeamSummary[] = []
    let next: number | null
    do {
      const result = before === undefined
        ? await listEnterpriseAgentTeams(props.enterpriseId, props.conversationId, token)
        : await listEnterpriseAgentTeams(props.enterpriseId, props.conversationId, token, before)
      if (!isCurrent(requestGeneration) || requestSequence !== refreshSequence) return
      appendVisibleTeams(visible, result.items)
      next = validNextCursor(result, before)
      before = next ?? undefined
      if (oldestLoadedId === null || result.items.some(item => item.team_id <= oldestLoadedId)) break
    } while (next !== null)
    teams.value = visible
    nextBeforeId.value = next
    if (!visible.some(team => team.team_id === selectedTeamId.value)) {
      clearSecurityReport()
      detail.value = null
      selectedTeamId.value = visible[0]?.team_id ?? null
    }
    await loadSelectedDetail(requestGeneration, requestSequence)
  } catch {
    if (!isCurrent(requestGeneration) || requestSequence !== refreshSequence) return
    clearTeamAccess()
  } finally {
    if (isCurrent(requestGeneration) && requestSequence === refreshSequence) loading.value = false
  }
}

function appendVisibleTeams(target: EnterpriseAgentTeamSummary[], items: EnterpriseAgentTeamSummary[]) {
  const existing = new Set(target.map(item => item.team_id))
  for (const item of items) {
    if (item.can_read_conversation && item.enterprise_id === props.enterpriseId
        && item.conversation_id === props.conversationId && !existing.has(item.team_id)) {
      target.push(item)
      existing.add(item.team_id)
    }
  }
}

function validNextCursor(result: { has_more: boolean; next_before_id: number | null }, before?: number) {
  if (!result.has_more) return null
  const cursor = result.next_before_id
  if (!Number.isSafeInteger(cursor) || cursor === null || cursor <= 0 || (before !== undefined && cursor >= before)) {
    throw new Error('团队分页游标无效')
  }
  return cursor
}

function clearTeamAccess() {
  teams.value = []
  selectedTeamId.value = null
  detail.value = null
  nextBeforeId.value = null
  clearSecurityReport()
  error.value = '当前账号无法读取此会话的团队进度。刷新权限后可重试。'
}

async function loadMoreTeams() {
  const before = nextBeforeId.value
  if (before === null || loading.value || loadingMore.value) return
  const requestGeneration = generation
  if (!isCurrent(requestGeneration)) return
  const requestSequence = refreshSequence
  const requestPage = ++pageSequence
  loadingMore.value = true
  try {
    const result = await listEnterpriseAgentTeams(props.enterpriseId, props.conversationId, getToken(), before)
    if (!isCurrent(requestGeneration) || requestSequence !== refreshSequence || requestPage !== pageSequence) return
    const combined = [...teams.value]
    appendVisibleTeams(combined, result.items)
    teams.value = combined
    nextBeforeId.value = validNextCursor(result, before)
  } catch {
    if (isCurrent(requestGeneration) && requestSequence === refreshSequence && requestPage === pageSequence) {
      clearTeamAccess()
    }
  } finally {
    if (isCurrent(requestGeneration) && requestSequence === refreshSequence && requestPage === pageSequence) {
      loadingMore.value = false
    }
  }
}

function selectTeam(teamId: number) {
  if (!teams.value.some(team => team.team_id === teamId)) return
  if (!isCurrent(generation)) return
  clearSecurityReport()
  selectedTeamId.value = teamId
  detail.value = null
  const requestGeneration = generation
  const requestSequence = refreshSequence
  void loadSelectedDetail(requestGeneration, requestSequence).catch(() => {
    if (isCurrent(requestGeneration) && requestSequence === refreshSequence && selectedTeamId.value === teamId) {
      clearTeamAccess()
    }
  })
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value && typeof value === 'object' && !Array.isArray(value))
}
function displayValue(value: unknown, maxLength = 4000): string {
  if (typeof value === 'string') return value.slice(0, maxLength)
  if (typeof value === 'number' || typeof value === 'boolean') return String(value)
  if (Array.isArray(value) || isRecord(value)) {
    try { return JSON.stringify(value).slice(0, maxLength) } catch { return '' }
  }
  return ''
}
function hasSecurityArtifact(task: EnterpriseAgentTeamTask) {
  return task.agent_code === 'security_sentinel'
    && typeof task.result.security_artifact_id === 'number'
    && Number.isSafeInteger(task.result.security_artifact_id)
    && task.result.security_artifact_id > 0
}
function hasReportReference(source: EnterpriseAgentTeamDetail | null, taskId: number, artifactId: number | null) {
  if (!source || artifactId === null) return false
  return source.tasks.some(task => task.task_id === taskId && hasSecurityArtifact(task)
    && task.result.security_artifact_id === artifactId)
}
function coverageLabel(status: string) {
  if (status === 'complete' && isTriageScan.value) return '风险优先扫描已完成'
  return status === 'complete' ? '覆盖完整' : status === 'incomplete' ? '覆盖未完成' : '报告失败'
}
function findingField(finding: Record<string, unknown>, keys: string[]) {
  for (const key of keys) {
    const value = displayValue(finding[key])
    if (value) return value
  }
  return ''
}
function findingTitle(finding: Record<string, unknown>) {
  return findingField(finding, ['title', 'name', 'category', 'type'])
}
function findingSeverity(finding: Record<string, unknown>) {
  return findingField(finding, ['severity', 'risk_level', 'level']) || '等级未标注'
}
function findingLocation(finding: Record<string, unknown>) {
  const path = findingField(finding, ['file_path', 'file', 'file_name'])
  const line = finding.line_number ?? finding.line
  const suffix = typeof line === 'number' && line > 0 ? `:${line}` : ''
  return `${path}${suffix}`
}
function findingDescription(finding: Record<string, unknown>) {
  return findingField(finding, ['description', 'exploit_scenario', 'summary', 'evidence'])
}
function clearSecurityReport(accessNotice = '') {
  securityReportSequence += 1
  securityReportAccessSequence += 1
  securityReport.value = null
  securityReportTaskId.value = null
  securityReportArtifactId.value = null
  securityReportLoading.value = false
  securityReportError.value = ''
  securityReportAccessNotice.value = accessNotice
}
async function revalidateSecurityReportAccess(
  requestGeneration: number,
  teamId: number,
  taskId: number,
  artifactId: number,
  reportSequence: number,
) {
  const key = `${props.enterpriseId}:${teamId}:${taskId}:${artifactId}:${reportSequence}`
  if (securityReportAccessRequest?.key === key) {
    await securityReportAccessRequest.promise
    return
  }
  const accessSequence = ++securityReportAccessSequence
  const isCurrentReport = () => isCurrent(requestGeneration)
    && accessSequence === securityReportAccessSequence
    && reportSequence === securityReportSequence
    && selectedTeamId.value === teamId
    && securityReportTaskId.value === taskId
    && securityReportArtifactId.value === artifactId
    && securityReport.value !== null
  const request = checkEnterpriseTeamSecurityArtifactAccess(
    props.enterpriseId, teamId, artifactId, getToken(),
  ).catch(() => {
    if (isCurrentReport()) {
      clearSecurityReport('安全报告正文已清除：当前权限已失效或暂时无法重新确认。恢复权限或网络后可重新打开。')
    }
  })
  const pending = { key, promise: request }
  securityReportAccessRequest = pending
  try {
    await request
  } finally {
    if (securityReportAccessRequest === pending) securityReportAccessRequest = null
  }
}
function closeSecurityReport() {
  const taskId = securityReportTaskId.value
  clearSecurityReport()
  if (taskId === null) return
  void nextTick(() => document.getElementById(`security-report-trigger-${taskId}`)?.focus())
}
async function openSecurityReport(task: EnterpriseAgentTeamTask) {
  const teamId = selectedTeamId.value
  const artifactId = task.result.security_artifact_id
  if (!teamId || !hasSecurityArtifact(task) || typeof artifactId !== 'number') return
  if (!isCurrent(generation)) return
  clearSecurityReport()
  securityReportTaskId.value = task.task_id
  securityReportArtifactId.value = artifactId
  securityReportLoading.value = true
  const requestGeneration = generation
  const requestSequence = ++securityReportSequence
  const sessionToken = getToken()
  try {
    const result = await getEnterpriseTeamSecurityArtifact(props.enterpriseId, teamId, artifactId, sessionToken)
    if (!isCurrent(requestGeneration) || requestSequence !== securityReportSequence
        || selectedTeamId.value !== teamId || detail.value?.team_id !== teamId
        || !hasReportReference(detail.value, task.task_id, artifactId)) return
    if (result.enterprise_id !== props.enterpriseId || result.team_id !== teamId
        || result.artifact_id !== artifactId || result.task_id !== task.task_id) {
      throw new Error('报告范围与当前团队不匹配')
    }
    await checkEnterpriseTeamSecurityArtifactAccess(props.enterpriseId, teamId, artifactId, sessionToken)
    if (!isCurrent(requestGeneration) || requestSequence !== securityReportSequence
        || selectedTeamId.value !== teamId || !hasReportReference(detail.value, task.task_id, artifactId)) return
    securityReport.value = result
  } catch {
    if (!isCurrent(requestGeneration) || requestSequence !== securityReportSequence
        || selectedTeamId.value !== teamId) return
    securityReportError.value = '安全报告读取失败。请确认当前账号仍有会话和项目查看权限后重试。'
  } finally {
    if (isCurrent(requestGeneration) && requestSequence === securityReportSequence) {
      securityReportLoading.value = false
    }
  }
}
function retrySecurityReport() {
  const taskId = securityReportTaskId.value
  const task = detail.value?.tasks.find(item => item.task_id === taskId)
  if (task) void openSecurityReport(task)
}

async function cancelTeam() {
  const teamId = selectedTeamId.value
  if (!teamId || !canControlSelectedTeam.value || busyAction.value) return
  const requestGeneration = generation
  if (!isCurrent(requestGeneration)) return
  busyAction.value = 'cancel'
  try {
    await cancelEnterpriseAgentTeam(props.enterpriseId, teamId, '从企业会话进度面板停止', getToken())
    if (isCurrent(requestGeneration)) await refresh()
  } catch {
    if (isCurrent(requestGeneration)) error.value = '停止请求未确认。请刷新团队状态后再操作。'
  } finally {
    if (isCurrent(requestGeneration)) busyAction.value = null
  }
}

async function retryTask(taskId: number) {
  const teamId = selectedTeamId.value
  if (!teamId || !canControlSelectedTeam.value || busyAction.value) return
  const requestGeneration = generation
  if (!isCurrent(requestGeneration)) return
  busyAction.value = `retry:${taskId}`
  try {
    await retryEnterpriseAgentTeamTask(props.enterpriseId, teamId, taskId, getToken())
    if (isCurrent(requestGeneration)) await refresh()
  } catch {
    if (isCurrent(requestGeneration)) error.value = '重试未提交。仅确认没有模型费用的任务可以重试，请刷新后检查状态。'
  } finally {
    if (isCurrent(requestGeneration)) busyAction.value = null
  }
}

function invalidateScope() {
  if (scopeInvalid) return
  scopeInvalid = true
  generation += 1
  refreshSequence += 1
  pageSequence += 1
  detailSequence += 1
  clearSecurityReport()
  teams.value = []
  detail.value = null
  selectedTeamId.value = null
  nextBeforeId.value = null
  loading.value = false
  loadingMore.value = false
  busyAction.value = null
  error.value = '登录状态已变化，请重新进入会话。'
}
function resetScope() {
  invalidateScope()
  scopeToken = getToken()
  scopeInvalid = false
  error.value = ''
  void refresh()
}
function handleTokenStorage(event: StorageEvent) {
  if ((event.key === 'review_token' || event.key === null) && getToken() !== scopeToken) invalidateScope()
}
function refreshOnResume() {
  if (document.visibilityState !== 'visible') return
  const now = Date.now()
  if (now - lastResumeRefreshAt < 1000) return
  lastResumeRefreshAt = now
  void refresh()
}
function pollVisibleTeamProgress() {
  if (document.visibilityState === 'visible' && isCurrent(generation) && !loading.value && !loadingMore.value) {
    void refresh()
  }
}

watch(
  () => [props.enterpriseId, props.conversationId, props.currentUserId] as const,
  resetScope,
)
onMounted(() => {
  void refresh()
  window.addEventListener('focus', refreshOnResume)
  window.addEventListener('storage', handleTokenStorage)
  window.addEventListener('prism:auth-expired', invalidateScope)
  document.addEventListener('visibilitychange', refreshOnResume)
  pollTimer = setInterval(pollVisibleTeamProgress, 5000)
})
onBeforeUnmount(() => {
  generation += 1
  refreshSequence += 1
  pageSequence += 1
  detailSequence += 1
  clearSecurityReport()
  window.removeEventListener('focus', refreshOnResume)
  window.removeEventListener('storage', handleTokenStorage)
  window.removeEventListener('prism:auth-expired', invalidateScope)
  document.removeEventListener('visibilitychange', refreshOnResume)
  if (pollTimer) clearInterval(pollTimer)
  pollTimer = null
})
</script>

<style scoped>
.enterprise-team-progress { flex: 0 0 auto; max-height: 42%; overflow: auto; border-top: 1px solid #e4e9f0; background: #fbfcfe; color: #243248; }
.progress-header, .detail-heading, .task-heading, .progress-actions { display: flex; align-items: center; justify-content: space-between; gap: 10px; }
.progress-header { padding: 12px 16px 8px; }
.progress-header h3, .detail-heading h4 { margin: 0; font-size: 14px; }
.progress-header p, .detail-heading p, .progress-count, .team-control small { margin: 3px 0 0; color: #69778c; font-size: 12px; }
.progress-actions button, .team-control button, .retry-task, .progress-error button { border: 1px solid #d5deea; border-radius: 7px; background: #fff; color: #324761; padding: 6px 10px; cursor: pointer; }
.progress-actions button:disabled, .team-control button:disabled, .retry-task:disabled { cursor: wait; opacity: .6; }
.close-progress { color: #66758a !important; }
.progress-error, .progress-empty { margin: 8px 16px 14px; padding: 10px 12px; border-radius: 8px; background: #f1f5fa; color: #596a80; font-size: 13px; }
.progress-error { background: #fff3f0; color: #a13d2c; }
.progress-error button { margin-left: 8px; color: inherit; }
.security-report-access-notice { margin: 8px 16px 12px; padding: 9px 11px; border-radius: 8px; background: #fff6e9; color: #80551b; font-size: 12px; }
.security-report-access-notice button { margin-left: 8px; border: 0; background: transparent; color: inherit; text-decoration: underline; cursor: pointer; }
.team-list { display: flex; gap: 8px; overflow-x: auto; padding: 4px 16px 10px; }
.team-choice { display: flex; min-width: 190px; align-items: center; justify-content: space-between; gap: 10px; padding: 8px 10px; border: 1px solid #dbe3ed; border-radius: 9px; background: #fff; color: inherit; text-align: left; cursor: pointer; }
.load-more-teams { flex: none; padding: 8px 10px; border: 1px solid #dbe3ed; border-radius: 9px; background: #fff; color: inherit; cursor: pointer; }
.load-more-teams:disabled { cursor: wait; opacity: .6; }
.team-choice.selected { border-color: #7398ce; background: #f1f6ff; }
.team-choice-copy { display: grid; gap: 3px; min-width: 0; }
.team-choice strong { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-size: 12px; }
.team-choice small { color: #6d7b90; font-size: 11px; white-space: nowrap; }
.team-status-dot { width: 9px; height: 9px; flex: none; border-radius: 50%; background: #8b99aa; }
.status-running { background: #3775d6; box-shadow: 0 0 0 3px #dce9fb; }
.status-completed { background: #31a36b; }
.status-failed, .status-awaiting_reconciliation { background: #d78a25; }
.team-detail { margin: 0 16px 12px; padding: 10px 12px; border: 1px solid #e1e7ef; border-radius: 10px; background: #fff; }
.detail-heading { align-items: flex-start; }
.detail-heading p { max-width: 620px; line-height: 1.4; }
.detail-status, .task-status { flex: none; color: #61728b; font-size: 11px; }
.progress-meter { height: 5px; overflow: hidden; margin-top: 10px; border-radius: 4px; background: #e8edf4; }
.progress-meter span { display: block; height: 100%; background: #4e84d5; transition: width .2s ease; }
.progress-count { margin-top: 4px; }
.task-list { display: grid; gap: 7px; margin: 10px 0 0; padding: 0; list-style: none; }
.task-row { padding: 8px 9px; border-radius: 7px; background: #f7f9fc; }
.task-heading strong { font-size: 12px; }
.task-heading small { display: block; margin-top: 2px; color: #748197; font-size: 11px; }
.task-summary { margin: 6px 0 0; color: #43536a; font-size: 12px; white-space: pre-wrap; overflow-wrap: anywhere; }
.task-error { color: #9a5730; }
.retry-task { margin-top: 7px; padding: 4px 8px; font-size: 11px; }
.team-control { display: flex; align-items: center; gap: 9px; margin-top: 10px; }
.team-control button { border-color: #eccac4; color: #a23e32; }
.security-report-trigger { margin: 7px 0 0 6px; padding: 4px 8px; border: 1px solid #ccd9eb; border-radius: 6px; background: #fff; color: #315f9b; font-size: 11px; cursor: pointer; }
.security-report-panel { margin: 0 16px 12px; padding: 12px; border: 1px solid #c9d8ea; border-radius: 10px; background: #f8fbff; }
.security-report-heading { display: flex; align-items: flex-start; justify-content: space-between; gap: 12px; }
.security-report-heading h4 { margin: 0; font-size: 14px; }
.security-report-heading p { margin: 3px 0 0; color: #69778c; font-size: 11px; }
.security-report-close, .security-report-retry { flex: none; padding: 5px 9px; border: 1px solid #d1dbea; border-radius: 6px; background: #fff; color: #405672; cursor: pointer; }
.security-report-state, .security-report-error { margin: 10px 0 0; padding: 9px; border-radius: 7px; background: #edf3fb; color: #50647e; font-size: 12px; }
.security-report-error { background: #fff1ee; color: #9e392c; }
.security-report-retry { margin-left: 8px; color: inherit; }
.security-report-meta { display: flex; flex-wrap: wrap; align-items: center; gap: 6px; margin-top: 10px; color: #5d6d82; font-size: 11px; }
.security-report-meta > span:not(.coverage-badge) { padding: 4px 7px; border-radius: 5px; background: #eef2f7; }
.coverage-badge { padding: 4px 8px; border-radius: 999px; font-weight: 700; }
.coverage-complete { background: #e1f5e9; color: #247a4e; }
.coverage-incomplete, .coverage-failed { background: #fff0da; color: #965b12; }
.security-report-summary, .security-report-termination { margin: 10px 0 0; color: #384b63; font-size: 12px; line-height: 1.55; white-space: pre-wrap; overflow-wrap: anywhere; }
.security-report-termination { color: #945820; }
.security-report-digests { display: grid; gap: 5px; margin: 10px 0; }
.security-report-digests div { display: grid; grid-template-columns: 76px minmax(0, 1fr); gap: 8px; font-size: 10px; }
.security-report-digests dt { color: #69778c; }
.security-report-digests dd { min-width: 0; margin: 0; color: #40516a; overflow-wrap: anywhere; font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }
.security-report-panel h5 { margin: 12px 0 6px; font-size: 12px; }
.security-findings { display: grid; gap: 7px; max-height: 220px; overflow: auto; margin: 0; padding-left: 23px; }
.security-findings li { padding: 8px; border-radius: 7px; background: #fff; color: #3d4f66; font-size: 11px; }
.finding-heading { display: flex; justify-content: space-between; gap: 8px; }
.finding-heading span { flex: none; color: #9d5338; }
.security-findings small { display: block; margin-top: 4px; color: #708097; overflow-wrap: anywhere; }
.security-findings p { margin: 5px 0 0; line-height: 1.45; white-space: pre-wrap; overflow-wrap: anywhere; }
.security-report-limit { margin: 6px 0 0; color: #718097; font-size: 10px; }
@media (max-width: 760px) {
  .enterprise-team-progress { max-height: 38%; }
  .progress-header { align-items: flex-start; }
  .progress-header p { max-width: 240px; }
  .team-choice { min-width: 170px; }
  .team-control { align-items: flex-start; flex-direction: column; }
}
</style>
